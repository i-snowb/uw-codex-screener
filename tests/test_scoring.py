from datetime import datetime, timedelta, timezone
import unittest

from morning_edge.scoring import Action, Evidence, MorningInputs, PortfolioPosition, Provenance, score


NOW = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)


def observed(value: float, **kwargs: object) -> Evidence:
    return Evidence(
        value=value, as_of=kwargs.get("as_of", NOW),
        available_at=kwargs.get("available_at"),
        provenance=kwargs.get("provenance", Provenance.OBSERVED), source="test",
        quality=kwargs.get("quality", 1.0),
    )


def inferred(value: float, **kwargs: object) -> Evidence:
    return observed(value, provenance=Provenance.INFERRED, **kwargs)


def modeled(value: float, **kwargs: object) -> Evidence:
    return observed(value, provenance=Provenance.MODELED, **kwargs)


def valid_inputs(**changes: object) -> MorningInputs:
    values = dict(
        ticker="QCOM", captured_at=NOW, price=observed(162.5), trend=modeled(.8),
        flow=inferred(.8), oi_change=observed(.7), gex=modeled(.6), iv_rank=modeled(45),
        bid_ask_spread_pct=observed(2), catalyst=inferred(.55), event_risk=modeled(.1),
        execution_ready=True,
        calibration_ready=True,
    )
    values.update(changes)
    return MorningInputs(**values)


class ScoringTests(unittest.TestCase):
    def test_buy_requires_separate_entry_gates(self) -> None:
        result = score(valid_inputs())
        self.assertIs(result.action, Action.BUY)
        self.assertGreaterEqual(result.setup_score, 68)
        self.assertGreaterEqual(result.directional_probability, .60)
        self.assertTrue(result.data_gate.passed)

    def test_stale_critical_field_blocks_new_entry(self) -> None:
        result = score(valid_inputs(price=observed(162.5, as_of=NOW - timedelta(minutes=6))))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertFalse(result.data_gate.passed)
        self.assertTrue(any("stale" in reason.lower() for reason in result.reasons))

    def test_missing_critical_field_blocks_new_entry(self) -> None:
        result = score(valid_inputs(iv_rank=None))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertTrue(any("missing required iv rank" in reason.lower() for reason in result.reasons))

    def test_next_day_open_interest_is_required_for_new_entry(self) -> None:
        result = score(valid_inputs(oi_change=None))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertTrue(any("missing required oi change" in reason.lower() for reason in result.reasons))

    def test_future_dated_evidence_is_rejected(self) -> None:
        result = score(valid_inputs(flow=inferred(.8, available_at=NOW + timedelta(seconds=1))))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertTrue(any("future-dated" in reason.lower() for reason in result.reasons))

    def test_direct_observation_field_rejects_modeled_provenance(self) -> None:
        result = score(valid_inputs(price=modeled(162.5)))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertTrue(any("expected observed" in reason.lower() for reason in result.reasons))

    def test_derived_fields_require_truthful_provenance(self) -> None:
        allowed = score(valid_inputs())
        mislabeled = score(valid_inputs(flow=observed(.8)))
        self.assertTrue(allowed.data_gate.passed)
        self.assertFalse(mislabeled.data_gate.passed)
        self.assertTrue(any("flow provenance is observed" in reason.lower() for reason in mislabeled.reasons))

    def test_conflicting_trend_and_flow_blocks_new_entry(self) -> None:
        result = score(valid_inputs(trend=modeled(.9), flow=inferred(-.9)))
        self.assertIs(result.action, Action.NO_ACTION)
        self.assertTrue(any("conflict" in reason.lower() for reason in result.reasons))

    def test_open_position_trim_prioritizes_risk_management(self) -> None:
        result = score(valid_inputs(position=PortfolioPosition(contracts=2, unrealized_return_pct=80, days_to_expiry=30)))
        self.assertIs(result.action, Action.TRIM)
        self.assertTrue(any("+75%" in reason for reason in result.reasons))

    def test_open_position_exits_near_expiry_even_if_snapshot_is_stale(self) -> None:
        result = score(valid_inputs(
            price=observed(162.5, as_of=NOW - timedelta(minutes=6)),
            position=PortfolioPosition(contracts=1, unrealized_return_pct=5, days_to_expiry=4),
        ))
        self.assertIs(result.action, Action.EXIT)
        self.assertFalse(result.data_gate.passed)

    def test_directional_probability_is_not_setup_score(self) -> None:
        result = score(valid_inputs())
        self.assertNotEqual(result.setup_score, round(result.directional_probability * 100))

    def test_preopen_snapshot_can_never_emit_buy(self) -> None:
        result = score(valid_inputs(execution_ready=False))
        self.assertIs(result.action, Action.WATCH)
        self.assertFalse(result.execution_ready)
        self.assertTrue(any("Execution is not ready" in reason for reason in result.reasons))

    def test_shadow_mode_can_never_emit_buy(self) -> None:
        result = score(valid_inputs(calibration_ready=False))
        self.assertIs(result.action, Action.WATCH)
        self.assertFalse(result.calibration_ready)
        self.assertTrue(any("Calibration is not ready" in reason for reason in result.reasons))

    def test_preopen_allows_prior_session_reference_quotes_for_watch(self) -> None:
        prior_available = NOW - timedelta(hours=17)
        result = score(valid_inputs(
            execution_ready=False,
            price=observed(162.5, as_of=prior_available - timedelta(minutes=5), available_at=prior_available),
            bid_ask_spread_pct=observed(2, as_of=prior_available - timedelta(minutes=5), available_at=prior_available),
        ))
        self.assertTrue(result.data_gate.passed)
        self.assertIs(result.action, Action.WATCH)

    def test_execution_ready_rejects_same_prior_session_quotes(self) -> None:
        prior_available = NOW - timedelta(hours=17)
        result = score(valid_inputs(
            execution_ready=True,
            price=observed(162.5, as_of=prior_available - timedelta(minutes=5), available_at=prior_available),
            bid_ask_spread_pct=observed(2, as_of=prior_available - timedelta(minutes=5), available_at=prior_available),
        ))
        self.assertFalse(result.data_gate.passed)
        self.assertIs(result.action, Action.NO_ACTION)

    def test_prior_session_derived_evidence_is_allowed_by_field_policy(self) -> None:
        prior_available = NOW - timedelta(hours=17)
        result = score(valid_inputs(
            trend=modeled(.8, as_of=prior_available - timedelta(minutes=10), available_at=prior_available),
            flow=inferred(.8, as_of=prior_available - timedelta(minutes=10), available_at=prior_available),
            oi_change=observed(.7, as_of=prior_available - timedelta(minutes=10), available_at=prior_available),
            iv_rank=modeled(45, as_of=prior_available - timedelta(minutes=10), available_at=prior_available),
        ))
        self.assertTrue(result.data_gate.passed)

    def test_older_but_accepted_evidence_reduces_reliability(self) -> None:
        current = score(valid_inputs(execution_ready=False))
        prior = NOW - timedelta(days=2)
        older = score(valid_inputs(
            execution_ready=False,
            price=observed(162.5, as_of=prior),
            trend=modeled(.8, as_of=prior),
            flow=inferred(.8, as_of=prior),
            oi_change=observed(.7, as_of=prior),
            iv_rank=modeled(45, as_of=prior),
            bid_ask_spread_pct=observed(2, as_of=prior),
        ))
        self.assertTrue(older.data_gate.passed)
        self.assertLess(older.data_gate.reliability, current.data_gate.reliability)

    def test_price_and_spread_remain_execution_freshness_requirements(self) -> None:
        result = score(valid_inputs(bid_ask_spread_pct=observed(2, as_of=NOW - timedelta(minutes=6))))
        self.assertFalse(result.data_gate.passed)
        self.assertTrue(any("Bid Ask Spread Pct is stale" in reason for reason in result.reasons))
