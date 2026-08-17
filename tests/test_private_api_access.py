from __future__ import annotations

import unittest

from stock_tracker.api.server import _private_api_access_allowed


_PRIVATE_PATHS = (
    "/api/brief/today",
    "/api/overview",
    "/api/portfolio",
    "/api/portfolio/profile",
    "/api/portfolio/positions",
    "/api/portfolio/positions/pos-1",
    "/api/positions",
    "/api/watchlist",
    "/api/watch",
    "/api/watch/remove",
    "/api/events",
    "/api/radar",
    "/api/config",
    "/api/stream",
    "/api/signal/signal-1",
)
_STRONG_ACCESS = "stage1-private-access-value-0123456789abcdef"


class TestPrivateAPIAccess(unittest.TestCase):
    def allowed(
        self,
        *,
        path: str = "/api/brief/today",
        client_host: str = "203.0.113.10",
        request_host: str = "stock.example",
        has_forwarding_headers: bool = False,
        request_origin: str = "",
        sec_fetch_site: str = "",
        authorization: str = "",
        configured_access: str = "",
    ) -> bool:
        return _private_api_access_allowed(
            path=path,
            client_host=client_host,
            request_host=request_host,
            has_forwarding_headers=has_forwarding_headers,
            request_origin=request_origin,
            sec_fetch_site=sec_fetch_site,
            authorization=authorization,
            configured_access=configured_access,
        )

    def test_non_private_market_data_and_health_remain_public(self) -> None:
        for path in (
            "/api/markets",
            "/api/provider_health",
            "/api/sectors",
            "/api/quote/600000.SH",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.allowed(path=path))

    def test_loopback_private_api_is_available_without_configuration(self) -> None:
        for path in _PRIVATE_PATHS:
            with self.subTest(path=path):
                self.assertTrue(
                    self.allowed(
                        path=path,
                        client_host="127.0.0.1",
                        request_host="127.0.0.1:8080",
                    )
                )
        self.assertTrue(
            self.allowed(client_host="::1", request_host="[::1]:8080")
        )
        self.assertTrue(
            self.allowed(client_host="127.0.0.1", request_host="localhost:8080")
        )
        self.assertTrue(
            self.allowed(
                client_host="127.0.0.1",
                request_host="127.0.0.1:8080",
                request_origin="http://127.0.0.1:8080",
                sec_fetch_site="same-origin",
            )
        )

    def test_loopback_proxy_or_cross_site_request_does_not_bypass_auth(self) -> None:
        unsafe_cases = (
            {
                "client_host": "127.0.0.1",
                "request_host": "stock.example",
            },
            {
                "client_host": "127.0.0.1",
                "request_host": "127.0.0.1:8080",
                "has_forwarding_headers": True,
            },
            {
                "client_host": "127.0.0.1",
                "request_host": "127.0.0.1:8080",
                "request_origin": "https://evil.example",
            },
            {
                "client_host": "127.0.0.1",
                "request_host": "127.0.0.1:8080",
                "sec_fetch_site": "cross-site",
            },
        )
        for path in _PRIVATE_PATHS:
            for case in unsafe_cases:
                with self.subTest(path=path, case=case):
                    self.assertFalse(self.allowed(path=path, **case))

    def test_remote_private_apis_are_disabled_without_configuration(self) -> None:
        for path in _PRIVATE_PATHS:
            with self.subTest(path=path):
                self.assertFalse(self.allowed(path=path))

    def test_short_or_whitespace_access_values_are_not_accepted(self) -> None:
        for value in (
            "configured-value",
            " " + _STRONG_ACCESS,
            _STRONG_ACCESS + " ",
            _STRONG_ACCESS[:-1] + "\n",
        ):
            with self.subTest(value=repr(value)):
                self.assertFalse(
                    self.allowed(
                        configured_access=value,
                        authorization="Bearer " + value,
                    )
                )

    def test_remote_private_apis_require_exact_strong_bearer_value(self) -> None:
        for path in _PRIVATE_PATHS:
            with self.subTest(path=path):
                self.assertFalse(
                    self.allowed(path=path, configured_access=_STRONG_ACCESS)
                )
                self.assertFalse(
                    self.allowed(
                        path=path,
                        configured_access=_STRONG_ACCESS,
                        authorization="Bearer wrong-value",
                    )
                )
                self.assertTrue(
                    self.allowed(
                        path=path,
                        configured_access=_STRONG_ACCESS,
                        authorization="Bearer " + _STRONG_ACCESS,
                    )
                )

    def test_private_prefix_does_not_match_similar_public_path(self) -> None:
        self.assertTrue(self.allowed(path="/api/portfolio-summary"))
        self.assertTrue(self.allowed(path="/api/signals-summary"))


if __name__ == "__main__":
    unittest.main()
