from __future__ import annotations

import unittest
from dataclasses import replace

from stock_tracker.core.types import Market
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.core.market_isolation import (
    CrossMarketTransferKind,
    CrossMarketTransferRequest,
    CrossMarketTransferState,
    MarketAccessScope,
    MarketIsolationBundle,
    MarketIsolationContractError,
    MarketIsolationState,
    MarketResearchProfile,
    SettlementCurrency,
)
from stock_tracker.quant.data.bar_artifact import DataTrustTier


def _id(scope: MarketAccessScope, field: str) -> str:
    return fingerprint(
        {
            "schema": "test-market-profile-id-v1",
            "scope": scope.value,
            "field": field,
        }
    )


def _profile(
    scope: MarketAccessScope,
    *,
    synthetic: bool = False,
) -> MarketResearchProfile:
    market = {
        MarketAccessScope.A_DOMESTIC: Market.A,
        MarketAccessScope.HK_CONNECT: Market.HK,
        MarketAccessScope.HK_BROAD: Market.HK,
        MarketAccessScope.US_CASH: Market.US,
    }[scope]
    currency = {
        Market.A: SettlementCurrency.CNY,
        Market.HK: SettlementCurrency.HKD,
        Market.US: SettlementCurrency.USD,
    }[market]
    timezone_name = {
        Market.A: "Asia/Shanghai",
        Market.HK: "Asia/Hong_Kong",
        Market.US: "America/New_York",
    }[market]
    horizons = {
        Market.A: (3, 5, 10, 20),
        Market.HK: (5, 10, 20, 40),
        Market.US: (20, 40, 60, 120),
    }[market]
    kwargs = {
        name: _id(scope, name)
        for name in (
            "config_id",
            "calendar_snapshot_id",
            "universe_snapshot_id",
            "market_rule_id",
            "cost_schedule_id",
            "data_snapshot_id",
            "feature_policy_id",
            "label_policy_id",
            "model_id",
            "calibration_id",
            "scoreboard_id",
        )
    }
    return MarketResearchProfile(
        market=market,
        access_scope=scope,
        currency=currency,
        timezone_name=timezone_name,
        horizons=horizons,
        trust_tier=(
            DataTrustTier.BEST_EFFORT
            if synthetic
            else DataTrustTier.OPERATIONAL_VERIFIED
        ),
        verified=not synthetic,
        complete=not synthetic,
        synthetic_fixture_only=synthetic,
        provenance_ids=(_id(scope, "provenance"),) if not synthetic else (),
        **kwargs,
    )


def _profiles() -> tuple[MarketResearchProfile, ...]:
    return (
        _profile(MarketAccessScope.A_DOMESTIC),
        _profile(MarketAccessScope.HK_CONNECT),
        _profile(MarketAccessScope.US_CASH),
    )


class TestMarketResearchProfile(unittest.TestCase):
    def test_market_scope_currency_timezone_and_horizons_are_bound(self) -> None:
        profile = _profile(MarketAccessScope.HK_CONNECT)
        self.assertEqual(profile.market, Market.HK)
        self.assertEqual(profile.currency, SettlementCurrency.HKD)
        self.assertEqual(profile.timezone_name, "Asia/Hong_Kong")
        self.assertEqual(profile.horizons, (5, 10, 20, 40))

    def test_wrong_scope_currency_timezone_and_horizon_fail_closed(self) -> None:
        a_share = _profile(MarketAccessScope.A_DOMESTIC)
        with self.assertRaises(MarketIsolationContractError):
            replace(a_share, access_scope=MarketAccessScope.HK_CONNECT)
        with self.assertRaises(MarketIsolationContractError):
            replace(a_share, currency=SettlementCurrency.USD)
        with self.assertRaises(MarketIsolationContractError):
            replace(a_share, timezone_name="UTC")
        with self.assertRaises(MarketIsolationContractError):
            replace(a_share, horizons=(20, 10))

    def test_synthetic_profile_cannot_claim_high_trust(self) -> None:
        synthetic = _profile(MarketAccessScope.US_CASH, synthetic=True)
        self.assertEqual(synthetic.trust_tier, DataTrustTier.BEST_EFFORT)
        with self.assertRaises(MarketIsolationContractError):
            replace(
                synthetic,
                verified=True,
                trust_tier=DataTrustTier.RESEARCH_GRADE,
            )

    def test_verified_profile_requires_bound_provenance(self) -> None:
        profile = _profile(MarketAccessScope.A_DOMESTIC)
        with self.assertRaises(MarketIsolationContractError):
            replace(profile, provenance_ids=())
        with self.assertRaises(MarketIsolationContractError):
            replace(profile, verified=False)

    def test_profile_id_cannot_be_injected(self) -> None:
        with self.assertRaises(TypeError):
            replace(
                _profile(MarketAccessScope.A_DOMESTIC),
                profile_id="f" * 64,
            )


class TestMarketIsolationBundle(unittest.TestCase):
    def test_independent_a_hk_connect_us_profiles_are_isolated(self) -> None:
        bundle = MarketIsolationBundle(tuple(reversed(_profiles())))
        self.assertEqual(bundle.state, MarketIsolationState.ISOLATED)
        self.assertEqual(bundle.blockers, ())
        self.assertEqual(
            tuple(item.access_scope for item in bundle.profiles),
            (
                MarketAccessScope.A_DOMESTIC,
                MarketAccessScope.HK_CONNECT,
                MarketAccessScope.US_CASH,
            ),
        )

    def test_shared_calibration_model_or_rules_block_isolation(self) -> None:
        a_share, hk_connect, us = _profiles()
        hk_connect = replace(
            hk_connect,
            calibration_id=a_share.calibration_id,
            model_id=a_share.model_id,
            market_rule_id=a_share.market_rule_id,
        )
        bundle = MarketIsolationBundle((a_share, hk_connect, us))
        self.assertEqual(bundle.state, MarketIsolationState.BLOCKED)
        self.assertTrue(
            any(item.startswith("SHARED_CALIBRATION_ID:") for item in bundle.blockers)
        )
        self.assertTrue(
            any(item.startswith("SHARED_MODEL_ID:") for item in bundle.blockers)
        )
        self.assertTrue(
            any(item.startswith("SHARED_MARKET_RULE_ID:") for item in bundle.blockers)
        )

    def test_missing_scope_is_explicit(self) -> None:
        a_share, hk_connect, _ = _profiles()
        bundle = MarketIsolationBundle((a_share, hk_connect))
        self.assertEqual(bundle.state, MarketIsolationState.BLOCKED)
        self.assertIn("MISSING_SCOPE:US_CASH", bundle.blockers)

    def test_synthetic_bundle_remains_diagnostic(self) -> None:
        a_share, hk_connect, _ = _profiles()
        synthetic_us = _profile(MarketAccessScope.US_CASH, synthetic=True)
        bundle = MarketIsolationBundle((a_share, hk_connect, synthetic_us))
        self.assertEqual(bundle.state, MarketIsolationState.DIAGNOSTIC_ONLY)
        self.assertIn("PROFILE_UNVERIFIED:US_CASH", bundle.blockers)

    def test_bundle_state_and_identity_cannot_be_relabelled(self) -> None:
        bundle = MarketIsolationBundle(_profiles())
        for changes in (
            {"state": MarketIsolationState.BLOCKED},
            {"blockers": ()},
            {"bundle_id": "f" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(bundle, **changes)


class TestCrossMarketTransfer(unittest.TestCase):
    def test_explicit_target_validation_only_allows_shadow_lane(self) -> None:
        source, target, _ = _profiles()
        request = CrossMarketTransferRequest(
            source=source,
            target=target,
            transfer_kind=CrossMarketTransferKind.MODEL_ARTIFACT,
            target_validation_id="a" * 64,
            approval_evidence_ids=("b" * 64,),
            shadow_only=True,
            production_weight_zero=True,
            orders_created=False,
            synthetic_fixture_only=False,
        )
        self.assertEqual(request.state, CrossMarketTransferState.SHADOW_ONLY)
        self.assertEqual(request.blockers, ())
        self.assertFalse(request.allows_production_reuse)
        self.assertFalse(request.deploys_model)
        self.assertFalse(request.changes_runtime_weight)
        self.assertFalse(request.creates_order)

    def test_threshold_and_calibration_never_become_production_reuse(self) -> None:
        source, target, _ = _profiles()
        for kind in (
            CrossMarketTransferKind.SCORE_THRESHOLD,
            CrossMarketTransferKind.CALIBRATION,
        ):
            with self.subTest(kind=kind):
                request = CrossMarketTransferRequest(
                    source=source,
                    target=target,
                    transfer_kind=kind,
                    target_validation_id="a" * 64,
                    approval_evidence_ids=("b" * 64,),
                    shadow_only=True,
                    production_weight_zero=True,
                    orders_created=False,
                    synthetic_fixture_only=False,
                )
                self.assertEqual(request.state, CrossMarketTransferState.SHADOW_ONLY)
                self.assertFalse(request.allows_production_reuse)

    def test_low_trust_profiles_cannot_enter_cross_market_shadow(self) -> None:
        source, target, _ = _profiles()
        source = replace(source, trust_tier=DataTrustTier.BEST_EFFORT)
        request = CrossMarketTransferRequest(
            source=source,
            target=target,
            transfer_kind=CrossMarketTransferKind.FEATURE_DEFINITION,
            target_validation_id="a" * 64,
            approval_evidence_ids=("b" * 64,),
            shadow_only=True,
            production_weight_zero=True,
            orders_created=False,
            synthetic_fixture_only=False,
        )
        self.assertEqual(request.state, CrossMarketTransferState.BLOCKED)
        self.assertIn(
            "SOURCE_OR_TARGET_PROFILE_TRUST_INSUFFICIENT",
            request.blockers,
        )

    def test_missing_evidence_or_nonzero_weight_blocks_transfer(self) -> None:
        source, target, _ = _profiles()
        request = CrossMarketTransferRequest(
            source=source,
            target=target,
            transfer_kind=CrossMarketTransferKind.FEATURE_DEFINITION,
            target_validation_id=None,
            approval_evidence_ids=(),
            shadow_only=False,
            production_weight_zero=False,
            orders_created=True,
            synthetic_fixture_only=False,
        )
        self.assertEqual(request.state, CrossMarketTransferState.BLOCKED)
        self.assertIn("TARGET_MARKET_VALIDATION_MISSING", request.blockers)
        self.assertIn("TRANSFER_APPROVAL_EVIDENCE_MISSING", request.blockers)
        self.assertIn("CROSS_MARKET_PRODUCTION_REUSE_FORBIDDEN", request.blockers)
        self.assertIn("TRANSFER_PRODUCTION_WEIGHT_NOT_ZERO", request.blockers)
        self.assertIn("TRANSFER_CREATED_ORDERS", request.blockers)

    def test_same_market_is_not_a_cross_market_transfer(self) -> None:
        source = _profile(MarketAccessScope.HK_CONNECT)
        target = _profile(MarketAccessScope.HK_BROAD)
        with self.assertRaises(MarketIsolationContractError):
            CrossMarketTransferRequest(
                source=source,
                target=target,
                transfer_kind=CrossMarketTransferKind.STRATEGY_RULE,
                target_validation_id="a" * 64,
                approval_evidence_ids=("b" * 64,),
                shadow_only=True,
                production_weight_zero=True,
                orders_created=False,
                synthetic_fixture_only=False,
            )

    def test_transfer_synthetic_flag_must_match_profile_provenance(self) -> None:
        source = _profile(MarketAccessScope.A_DOMESTIC, synthetic=True)
        target = _profile(MarketAccessScope.HK_CONNECT)
        with self.assertRaises(MarketIsolationContractError):
            CrossMarketTransferRequest(
                source=source,
                target=target,
                transfer_kind=CrossMarketTransferKind.FEATURE_DEFINITION,
                target_validation_id="a" * 64,
                approval_evidence_ids=("b" * 64,),
                shadow_only=True,
                production_weight_zero=True,
                orders_created=False,
                synthetic_fixture_only=False,
            )
        diagnostic = CrossMarketTransferRequest(
            source=source,
            target=target,
            transfer_kind=CrossMarketTransferKind.FEATURE_DEFINITION,
            target_validation_id="a" * 64,
            approval_evidence_ids=("b" * 64,),
            shadow_only=True,
            production_weight_zero=True,
            orders_created=False,
            synthetic_fixture_only=True,
        )
        self.assertEqual(
            diagnostic.state,
            CrossMarketTransferState.DIAGNOSTIC_ONLY,
        )

    def test_transfer_derived_state_cannot_be_injected(self) -> None:
        source, target, _ = _profiles()
        request = CrossMarketTransferRequest(
            source=source,
            target=target,
            transfer_kind=CrossMarketTransferKind.LABEL_DEFINITION,
            target_validation_id="a" * 64,
            approval_evidence_ids=("b" * 64,),
            shadow_only=True,
            production_weight_zero=True,
            orders_created=False,
            synthetic_fixture_only=False,
        )
        for changes in (
            {"state": CrossMarketTransferState.BLOCKED},
            {"blockers": ()},
            {"request_id": "f" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                replace(request, **changes)


if __name__ == "__main__":
    unittest.main()
