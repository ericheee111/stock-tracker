from __future__ import annotations

import json
import unittest
from pathlib import Path

from stock_tracker.deployment.hybrid_h5 import PublicAccessMode, public_access_preflight

from stock_tracker.core.config import load_configs
from stock_tracker.core.security import PRIVATE_ACCESS_ENV

ROOT = Path(__file__).resolve().parents[1]
_STRONG_ACCESS = "hybrid-h5-test-only-" + ("x" * 32)


class TestHybridH5PublicGate(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_configs(str(ROOT / "config"))

    def test_trusted_tailnet_is_the_only_passable_sharing_mode(self) -> None:
        result = public_access_preflight(
            self.bundle,
            mode=PublicAccessMode.TRUSTED_TAILNET,
            environ={PRIVATE_ACCESS_ENV: _STRONG_ACCESS},
        )
        self.assertTrue(result["passed"])
        self.assertFalse(result["mutates_host_or_network"])
        self.assertNotIn(_STRONG_ACCESS, json.dumps(result))

    def test_public_modes_remain_blocked_even_with_ack_and_review(self) -> None:
        runtime = self.bundle.app.runtime
        runtime.deployment_mode = "HYBRID_PUBLIC_AUTH"
        runtime.cors_allowed_origins = ["https://app.example"]
        for mode in (
            PublicAccessMode.TAILSCALE_FUNNEL,
            PublicAccessMode.CLOUDFLARE_TUNNEL,
        ):
            with self.subTest(mode=mode):
                result = public_access_preflight(
                    self.bundle,
                    mode=mode,
                    environ={PRIVATE_ACCESS_ENV: _STRONG_ACCESS},
                    acknowledge_public_exposure=True,
                    independent_review_id="review-fixture",
                )
                self.assertFalse(result["passed"])
                self.assertIn("PUBLIC_RATE_LIMIT_NOT_IMPLEMENTED", result["blockers"])
                self.assertIn("PUBLIC_ENABLE_ACTION_NOT_IMPLEMENTED", result["blockers"])
                self.assertFalse(result["mutates_host_or_network"])

    def test_public_mode_rejects_wildcard_or_http_cors(self) -> None:
        runtime = self.bundle.app.runtime
        runtime.deployment_mode = "HYBRID_PUBLIC_AUTH"
        for origins in (["*"], ["http://app.example"], []):
            runtime.cors_allowed_origins = origins
            with self.subTest(origins=origins):
                result = public_access_preflight(
                    self.bundle,
                    mode=PublicAccessMode.TAILSCALE_FUNNEL,
                    environ={PRIVATE_ACCESS_ENV: _STRONG_ACCESS},
                )
                self.assertIn("PUBLIC_CORS_NOT_EXACT_HTTPS", result["blockers"])


if __name__ == "__main__":
    unittest.main()
