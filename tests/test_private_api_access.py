from __future__ import annotations

import unittest

from stock_tracker.api.server import _private_api_access_allowed


class TestPrivateAPIAccess(unittest.TestCase):
    def allowed(
        self,
        *,
        path: str = "/api/brief/today",
        client_host: str = "203.0.113.10",
        request_host: str = "stock.example",
        authorization: str = "",
        configured_access: str = "",
    ) -> bool:
        return _private_api_access_allowed(
            path=path,
            client_host=client_host,
            request_host=request_host,
            authorization=authorization,
            configured_access=configured_access,
        )

    def test_non_private_api_remains_public(self) -> None:
        self.assertTrue(self.allowed(path="/api/overview"))

    def test_loopback_private_api_is_available_without_configuration(self) -> None:
        self.assertTrue(
            self.allowed(client_host="127.0.0.1", request_host="127.0.0.1:8080")
        )
        self.assertTrue(
            self.allowed(client_host="::1", request_host="[::1]:8080")
        )
        self.assertTrue(
            self.allowed(client_host="127.0.0.1", request_host="localhost:8080")
        )

    def test_loopback_proxy_with_public_host_does_not_bypass_auth(self) -> None:
        self.assertFalse(
            self.allowed(client_host="127.0.0.1", request_host="stock.example")
        )

    def test_remote_private_api_is_disabled_without_configuration(self) -> None:
        self.assertFalse(self.allowed())
        self.assertFalse(self.allowed(path="/api/portfolio"))
        self.assertFalse(self.allowed(path="/api/portfolio/positions/pos-1"))

    def test_remote_private_api_requires_exact_bearer_value(self) -> None:
        configured = "configured-value"
        self.assertFalse(self.allowed(configured_access=configured))
        self.assertFalse(
            self.allowed(
                configured_access=configured,
                authorization="Bearer wrong-value",
            )
        )
        self.assertTrue(
            self.allowed(
                configured_access=configured,
                authorization="Bearer configured-value",
            )
        )

    def test_private_prefix_does_not_match_similar_public_path(self) -> None:
        self.assertTrue(self.allowed(path="/api/portfolio-summary"))


if __name__ == "__main__":
    unittest.main()
