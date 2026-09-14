"""Offline Runtime HTTPS policy tests; never request real providers."""
from __future__ import annotations

import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request

from stock_tracker.collector import provider as module
from stock_tracker.collector.tencent import TencentProvider
from stock_tracker.core.config import ProviderConfig


class TestRuntimeHTTPSPolicy(unittest.TestCase):
    def setUp(self):
        self.provider = TencentProvider(ProviderConfig(name="fixture", cls="TencentProvider", markets=["a"]))
        self.opener = MagicMock()
        self.opener.open.return_value.__enter__.return_value.read.return_value = b"fixture-bytes"
        self.build = self.enterContext(patch.object(module.urllib_request, "build_opener", return_value=self.opener))
        self.legacy = self.enterContext(patch.object(module.urllib_request, "urlopen", return_value=self.opener.open.return_value))

    def test_default_context_verifies_ca_and_hostname(self):
        context = module._ssl_ctx()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_exact_response_bytes_and_secure_context(self):
        self.assertEqual(self.provider._request("https://fixture.invalid/quotes", {"Referer": "https://fixture.invalid"}), b"fixture-bytes")
        self.build.assert_called_once()
        handlers = self.build.call_args.args
        https = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        self.assertTrue(any(type(h) is module._NoRedirectHandler for h in handlers))
        self.legacy.assert_not_called()
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], self.provider.timeout)

    def test_certificate_failure_is_not_retried_or_downgraded(self):
        error = ssl.SSLCertVerificationError("synthetic certificate rejection")
        self.opener.open.side_effect = error
        self.legacy.side_effect = error
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.provider._request("https://fixture.invalid/quotes")
        self.assertEqual(self.opener.open.call_count, 1)
        self.legacy.assert_not_called()

    def test_plain_http_and_non_network_schemes_rejected_before_io(self):
        for url in ("http://fixture.invalid/q", "file:///tmp/q", "ftp://fixture.invalid/q"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.provider._request(url)
        self.opener.open.assert_not_called()
        self.legacy.assert_not_called()

    def test_noncanonical_urls_and_credentials_rejected_before_io(self):
        for url in (" https://fixture.invalid/q", "https://user:pass@fixture.invalid/q", "https://fixture.invalid/q#fragment", "https://fixture.invalid/\nq", "https://fixture.invalid/\\q", "https:///no-host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.provider._request(url)
        self.opener.open.assert_not_called()
        self.legacy.assert_not_called()

    def test_redirect_never_followed_including_https_to_http(self):
        handler = module._NoRedirectHandler()
        for status in (301, 302, 303, 307, 308):
            for target in ("http://fixture.invalid", "https://other.invalid"):
                with self.subTest(status=status, target=target):
                    self.assertIsNone(handler.redirect_request(Request("https://fixture.invalid"), None, status, "redirect", {}, target))
        error = HTTPError("https://fixture.invalid", 302, "redirect", {}, None)
        self.opener.open.side_effect = error
        self.legacy.side_effect = error
        try:
            with self.assertRaises(HTTPError):
                self.provider._request("https://fixture.invalid/q")
            self.assertEqual(self.opener.open.call_count, 1)
        finally:
            error.close()

    def test_host_override_does_not_disable_tls(self):
        self.provider.host_override = "fixture-local.invalid:8443"
        self.provider._request("https://fixture.invalid/q")
        self.assertEqual(self.opener.open.call_args.args[0].full_url, "https://fixture-local.invalid:8443/q")
        self.assertTrue(module._ssl_ctx().check_hostname)

    def test_host_override_cannot_inject_credentials(self):
        self.provider.host_override = "user:secret@fixture.invalid"
        with self.assertRaises(ValueError):
            self.provider._request("https://fixture.invalid/q")
        self.opener.open.assert_not_called()
        self.legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
