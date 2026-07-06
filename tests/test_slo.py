"""Unit tests for the SLO calculator (13 tests)."""
from __future__ import annotations
import pytest
from datetime import datetime, timedelta, timezone
from slo.calculator import SLOCalculator

def _ts(hours_ago=0): return datetime.now(timezone.utc) - timedelta(hours=hours_ago)
def _calc(target=0.9995, window_days=30): return SLOCalculator(target_slo=target, window_days=window_days)

def test_invalid_target_raises():
    with pytest.raises(ValueError): SLOCalculator(target_slo=1.5)
    with pytest.raises(ValueError): SLOCalculator(target_slo=0.0)

def test_perfect_compliance():
    calc = _calc(target=0.999)
    calc.record(good=10_000, total=10_000)
    result = calc.compute("payment")
    assert result.current_compliance == 1.0
    assert result.is_slo_met
    assert result.error_budget_remaining_pct == pytest.approx(100.0, abs=0.01)

def test_slo_just_met():
    calc = _calc(target=0.9990)
    calc.record(good=9990, total=10_000)
    result = calc.compute("checkout")
    assert result.current_compliance == pytest.approx(0.999, rel=1e-4)
    assert result.is_slo_met

def test_slo_violated():
    calc = _calc(target=0.9995)
    calc.record(good=9990, total=10_000)
    result = calc.compute("payment")
    assert not result.is_slo_met

def test_error_budget_full_when_perfect():
    calc = _calc(target=0.999)
    calc.record(good=10_000, total=10_000)
    assert calc.compute("svc").error_budget_remaining_pct == pytest.approx(100.0, abs=0.1)

def test_error_budget_half_consumed():
    calc = _calc(target=0.99)
    calc.record(good=9950, total=10_000)
    result = calc.compute("svc")
    assert result.error_budget_remaining_pct == pytest.approx(50.0, abs=2.0)

def test_no_fast_burn_on_perfect_data():
    calc = _calc()
    calc.record(good=10_000, total=10_000)
    assert not calc.compute("svc").is_fast_burn_alert

def test_fast_burn_triggered_on_high_error_rate():
    calc = _calc(target=0.9995)
    calc.record(good=100, total=10_000, timestamp=_ts(0.1))
    assert calc.compute("svc").is_fast_burn_alert

def test_old_windows_evicted():
    calc = SLOCalculator(target_slo=0.999, window_days=1)
    calc.record(good=0, total=10_000, timestamp=_ts(hours_ago=48))
    calc.record(good=10_000, total=10_000, timestamp=_ts(hours_ago=1))
    assert calc.compute("svc").current_compliance == pytest.approx(1.0, abs=0.001)

def test_no_data_returns_perfect():
    calc = _calc()
    result = calc.compute("empty")
    assert result.current_compliance == 1.0
    assert result.total_requests == 0

def test_good_exceeds_total_raises():
    calc = _calc()
    with pytest.raises(ValueError): calc.record(good=1001, total=1000)

def test_negative_good_raises():
    calc = _calc()
    with pytest.raises(ValueError): calc.record(good=-1, total=100)

def test_result_to_dict_shape():
    calc = _calc(target=0.999)
    calc.record(good=9990, total=10_000)
    d = calc.compute("payment").to_dict()
    assert d["service"] == "payment"
    assert "error_budget" in d
    assert "burn_rates" in d
