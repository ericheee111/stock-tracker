from __future__ import annotations

import ssl
import unittest
from unittest.mock import patch
from urllib import error as urllib_error
from urllib import request as urllib_request

from stock_tracker.collector.eastmoney import EastmoneyProvider
from stock_tracker.core.config import ProviderConfig


class _Response:
    def __init__(
        self,
        *,
        url: str,
        body: bytes = b'{"ok":true}',
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._body = body
        self.status = status
        self.headers = (
            {"Content-Type": "application/json"}
            if headers is None
            else headers
        )

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class _Opener:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.response


class TestResearchExactRawRequest(unittest.TestCase):
    @staticmethod
    def _provider(*, host: str = "") -> EastmoneyProvider:
        return EastmoneyProvider(
            ProviderConfig(
                name="eastmoney",
                cls="EastmoneyProvider",
                markets=["a", "hk", "us"],
                host=host,
                timeout_ms=3000,
                max_rps=100,
            )
        )

    def test_uses_system_ca_empty_proxy_and_no_redirect_handler(self) -> None:
        url = "https://example.com/api/bars?symbol=600519.SH"
        response = _Response(
            url=url,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": "11",
            },
        )
        opener = _Opener(response=response)
        with patch(
            "stock_tracker.collector.provider.urllib_request.build_opener",
            return_value=opener,
        ) as build_opener:
            raw = self._provider()._request_research(url)
        self.assertEqual(raw, b'{"ok":true}')
        self.assertEqual(opener.request.full_url, url)
        self.assertEqual(opener.request.method, "GET")
        self.assertEqual(opener.timeout, 3.0)
        handlers = build_opener.call_args.args
        proxy = next(item for item in handlers if isinstance(item, urllib_request.ProxyHandler))
        https = next(item for item in handlers if isinstance(item, urllib_request.HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        context = https._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn("_NoRedirectHandler", {type(item).__name__ for item in handlers})

    def test_host_override_and_unsafe_urls_fail_before_network(self) -> None:
        with patch(
            "stock_tracker.collector.provider.urllib_request.build_opener"
        ) as build_opener:
            with self.assertRaisesRegex(ValueError, "host override"):
                self._provider(host="127.0.0.1:9")._request_research(
                    "https://example.com/api"
                )
            for url in (
                "http://example.com/api",
                "https://user@example.com/api",
                "https://example.com:8443/api",
                "https://example.com/api#fragment",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self._provider()._request_research(url)
        build_opener.assert_not_called()

    def test_authority_credential_and_malformed_headers_fail_before_network(self) -> None:
        url = "https://example.com/api"
        cases = (
            ({"Host": "other.example"}, "authority or credential"),
            ({"Authorization": "Bearer secret"}, "authority or credential"),
            ({"X-Api-Key": "secret"}, "authority or credential"),
            ({"X-Test\nHeader": "value"}, "header name"),
            ({"X-Test": "value\r\nInjected: true"}, "header value"),
        )
        with patch(
            "stock_tracker.collector.provider.urllib_request.build_opener"
        ) as build_opener:
            with self.assertRaisesRegex(ValueError, "must be a dictionary"):
                self._provider()._request_research(url, headers=[("X-Test", "value")])  # type: ignore[arg-type]
            for headers, expected in cases:
                with self.subTest(headers=headers), self.assertRaisesRegex(
                    ValueError,
                    expected,
                ):
                    self._provider()._request_research(url, headers=headers)
        build_opener.assert_not_called()

    def test_noncanonical_url_fails_before_network(self) -> None:
        with patch(
            "stock_tracker.collector.provider.urllib_request.build_opener"
        ) as build_opener:
            for url in (
                " https://example.com/api",
                "https://example.com\\api",
                "https://example.com/api\n",
            ):
                with self.subTest(url=url), self.assertRaisesRegex(
                    ValueError,
                    "not canonical",
                ):
                    self._provider()._request_research(url)
        build_opener.assert_not_called()

    def test_authority_credential_and_invalid_headers_fail_before_network(self) -> None:
        cases = (
            ({"Host": "other.example"}, "authority or credential"),
            ({"Authorization": "Bearer secret"}, "authority or credential"),
            ({"Cookie": "session=secret"}, "authority or credential"),
            ({"X-Api-Key": "secret"}, "authority or credential"),
            ({"Accept": "application/json", "accept": "text/plain"}, "duplicate names"),
            ({"X-Test": "bad\nvalue"}, "header value is invalid"),
        )
        with patch(
            "stock_tracker.collector.provider.urllib_request.build_opener"
        ) as build_opener:
            for headers, expected in cases:
                with self.subTest(headers=headers), self.assertRaisesRegex(
                    ValueError,
                    expected,
                ):
                    self._provider()._request_research(
                        "https://example.com/api",
                        headers=headers,
                    )
        build_opener.assert_not_called()

    def test_redirect_changed_url_html_size_and_empty_payload_fail_closed(self) -> None:
        url = "https://example.com/api"
        cases = (
            (
                _Opener(response=_Response(url="https://other.example/api")),
                "response URL changed",
            ),
            (
                _Opener(
                    response=_Response(
                        url=url,
                        headers={"Content-Type": "text/html"},
                    )
                ),
                "returned HTML",
            ),
            (
                _Opener(
                    response=_Response(
                        url=url,
                        body=b"  <!DOCTYPE html><html><body>upstream error</body></html>",
                        headers={"Content-Type": "text/plain"},
                    )
                ),
                "HTML error page",
            ),
            (
                _Opener(
                    response=_Response(
                        url=url,
                        body=(
                            b"\xef\xbb\xbf  <html><body>upstream error</body></html>"
                        ),
                        headers={"Content-Type": "text/plain"},
                    )
                ),
                "HTML error page",
            ),
            (
                _Opener(
                    response=_Response(
                        url=url,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": "1000",
                        },
                    )
                ),
                "size limit",
            ),
            (
                _Opener(
                    response=_Response(
                        url=url,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": "12",
                        },
                    )
                ),
                "differs from Content-Length",
            ),
            (_Opener(response=_Response(url=url, body=b"")), "response is empty"),
            (
                _Opener(
                    error=urllib_error.HTTPError(
                        url,
                        302,
                        "Found",
                        {},
                        None,
                    )
                ),
                "redirects are forbidden",
            ),
        )
        for opener, expected in cases:
            with self.subTest(expected=expected), patch(
                "stock_tracker.collector.provider.urllib_request.build_opener",
                return_value=opener,
            ), self.assertRaisesRegex(ValueError, expected):
                self._provider()._request_research(
                    url,
                    max_response_bytes=100,
                )

    def test_non_json_content_type_and_invalid_content_length_fail_closed(self) -> None:
        url = "https://example.com/api"
        cases = (
            ({}, "content type"),
            (
                {"Content-Type": "application/octet-stream"},
                "content type",
            ),
            (
                {
                    "Content-Type": "application/json",
                    "Content-Length": "not-an-int",
                },
                "Content-Length is invalid",
            ),
        )
        for headers, expected in cases:
            opener = _Opener(response=_Response(url=url, headers=headers))
            with self.subTest(expected=expected), patch(
                "stock_tracker.collector.provider.urllib_request.build_opener",
                return_value=opener,
            ), self.assertRaisesRegex(ValueError, expected):
                self._provider()._request_research(url)


if __name__ == "__main__":
    unittest.main()
