from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.research import (
    assign_chronological_splits,
    bars_to_frame,
    benjamini_hochberg,
    candidate_features,
    confirmation_entry,
    fixed_entry,
    latest_candidate_availability,
    passes_dip_gate,
    performance_metrics,
    scheduled_midday_scanner_cutoff,
    segment_matches,
    select_validation_record,
    simulate_trial,
    wilson_lower_bound,
)

NY = ZoneInfo("America/New_York")


def frame(rows):
    return pd.DataFrame(rows)


def test_fixed_entry_uses_requested_bar_open():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:30", tz=NY), "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 100, "vwap": 10.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:31", tz=NY), "open": 9.8, "high": 10.0, "low": 9.7, "close": 9.9, "volume": 100, "vwap": 9.85},
    ])
    idx, row, reason = fixed_entry(df, date(2026, 7, 1), "12:31")
    assert idx == 1
    assert row["open"] == pytest.approx(9.8)
    assert reason == "fixed_1231_et"


def test_same_bar_target_and_stop_assumes_stop_first():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:31", tz=NY), "open": 100.0, "high": 104.0, "low": 94.0, "close": 101.0, "volume": 100, "vwap": 100.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:32", tz=NY), "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 100, "vwap": 101.0},
    ])
    result = simulate_trial(df, "fixed_1231_et", 0, "test", 3.5, 5.0)
    assert result.stop_hit is True
    assert result.target_hit is False
    assert result.same_bar_ambiguous is True
    assert result.exit_reason == "stop_same_bar_conservative"
    assert result.gross_return_pct == pytest.approx(-5.0)


def test_target_before_stop_is_preserved():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:31", tz=NY), "open": 100.0, "high": 103.6, "low": 99.0, "close": 103.0, "volume": 100, "vwap": 101.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:32", tz=NY), "open": 103.0, "high": 104.0, "low": 94.0, "close": 95.0, "volume": 100, "vwap": 100.0},
    ])
    result = simulate_trial(df, "fixed_1231_et", 0, "test", 3.5, 5.0)
    assert result.target_hit is True
    assert result.stop_hit is False
    assert result.exit_reason == "target"
    assert result.gross_return_pct == pytest.approx(3.5)


def test_candidate_gate_is_explicit():
    features = {
        "cutoff_price": 10.0,
        "dollar_volume_to_cutoff": 10_000_000,
        "drawdown_from_high_pct": -4.0,
        "open_to_cutoff_pct": -2.0,
        "below_vwap": True,
    }
    config = {
        "min_price": 2,
        "max_price": 50,
        "min_dollar_volume": 5_000_000,
        "dip_from_high_pct": 3,
        "dip_from_open_pct": 1,
        "require_below_vwap": True,
    }
    assert passes_dip_gate(features, config) == (True, [])


def test_chronological_splits_are_not_random():
    dates = [date(2026, 1, day) for day in range(1, 11)]
    result = assign_chronological_splits(dates, 0.6, 0.2)
    assert [result[d] for d in dates[:6]] == ["discovery"] * 6
    assert [result[d] for d in dates[6:8]] == ["validation"] * 2
    assert [result[d] for d in dates[8:]] == ["sealed_test"] * 2


def test_bh_adjustment_is_monotonic_by_rank():
    q = benjamini_hochberg([0.01, 0.04, 0.03])
    assert all(0 <= value <= 1 for value in q)
    assert q[0] <= q[2] <= q[1]


def test_fixed_entry_never_forward_fills_a_missing_minute():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:30", tz=NY), "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 100, "vwap": 10.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:32", tz=NY), "open": 9.8, "high": 10.0, "low": 9.7, "close": 9.9, "volume": 100, "vwap": 9.85},
    ])
    assert fixed_entry(df, date(2026, 7, 1), "12:31") is None


def test_candidate_features_do_not_use_cutoff_bar():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 09:30", tz=NY), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100, "vwap": 100.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:29", tz=NY), "open": 98.0, "high": 98.5, "low": 97.0, "close": 97.5, "volume": 100, "vwap": 98.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:30", tz=NY), "open": 97.5, "high": 120.0, "low": 97.0, "close": 119.0, "volume": 100, "vwap": 110.0},
    ])
    features = candidate_features(df, datetime(2026, 7, 1, 12, 30, tzinfo=NY))
    assert features["cutoff_price"] == pytest.approx(97.5)
    assert features["morning_high"] == pytest.approx(101.0)


def test_confirmation_requires_the_immediately_consecutive_entry_bar():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:29", tz=NY), "open": 10.0, "high": 10.2, "low": 9.8, "close": 9.9, "volume": 100, "vwap": 10.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:30", tz=NY), "open": 9.9, "high": 10.3, "low": 9.9, "close": 10.2, "volume": 100, "vwap": 10.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:32", tz=NY), "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.3, "volume": 100, "vwap": 10.1},
    ])
    # 12:30 confirms above the prior high, but the 12:31 bar is absent.
    assert confirmation_entry(df, datetime(2026, 7, 1, 12, 30, tzinfo=NY), "prior_bar_high") is None


def test_price_segments_are_frozen_and_non_overlapping():
    assert segment_matches({"cutoff_price": 4.99}, "price_2_to_5")
    assert not segment_matches({"cutoff_price": 5.00}, "price_2_to_5")
    assert segment_matches({"cutoff_price": 5.00}, "price_5_to_20")
    assert segment_matches({"cutoff_price": 20.00}, "price_20_to_50")
    assert not segment_matches({"cutoff_price": 20.00}, "price_5_to_20")


def test_net_three_percent_success_and_wilson_bound_are_reported():
    df = pd.DataFrame([
        {"symbol": "A", "trade_date": date(2026, 7, 1), "gross_return_pct": 3.5, "target_hit": True, "stop_hit": False, "same_bar_ambiguous": False, "max_gain_pct": 3.5, "max_drawdown_pct": -1.0},
        {"symbol": "B", "trade_date": date(2026, 7, 2), "gross_return_pct": -5.0, "target_hit": False, "stop_hit": True, "same_bar_ambiguous": False, "max_gain_pct": 0.5, "max_drawdown_pct": -5.0},
    ])
    metrics = performance_metrics(df, cost_bps=50, bootstrap_iterations=100, net_target_pct=3.0)
    assert metrics["net_target_success_rate_pct"] == pytest.approx(50.0)
    assert 0 < metrics["net_target_wilson_low_pct"] < 50.0
    assert metrics["loss_5pct_rate_pct"] == pytest.approx(50.0)
    assert wilson_lower_bound(0, 0) is None


def test_latest_candidate_availability_uses_actual_late_creation_time():
    trade_date = date(2026, 3, 20)
    scheduled = datetime(2026, 3, 20, 12, 0, tzinfo=NY)
    created = datetime(2026, 3, 20, 12, 41, tzinfo=NY)
    assert latest_candidate_availability([scheduled, created], trade_date) == created


def test_path_extremes_stop_at_the_simulated_exit():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:31", tz=NY), "open": 100.0, "high": 103.6, "low": 99.0, "close": 103.5, "volume": 100, "vwap": 101.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:32", tz=NY), "open": 103.5, "high": 104.0, "low": 60.0, "close": 65.0, "volume": 100, "vwap": 90.0},
    ])
    result = simulate_trial(df, "fixed_1231_et", 0, "test", 3.5, 5.0)
    assert result.exit_reason == "target"
    assert result.max_drawdown_pct == pytest.approx(-1.0)
    assert result.max_gain_pct == pytest.approx(3.5)


def test_market_calendar_early_close_bounds_regular_session_bars():
    raw = [
        {"t": "2026-11-27T17:59:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 10},  # 12:59 ET
        {"t": "2026-11-27T18:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 10},  # 13:00 ET
    ]
    session_open = datetime(2026, 11, 27, 9, 30, tzinfo=NY)
    session_close = datetime(2026, 11, 27, 13, 0, tzinfo=NY)
    result = bars_to_frame(raw, session_open, session_close)
    assert list(result["timestamp"].dt.strftime("%H:%M")) == ["12:59"]


def test_invalid_ohlc_bar_is_removed_before_research():
    raw = [
        {"t": "2026-07-01T16:30:00Z", "o": 10, "h": 9, "l": 11, "c": 10, "v": 100},
        {"t": "2026-07-01T16:31:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 100},
    ]
    result = bars_to_frame(raw)
    assert list(result["timestamp"].dt.strftime("%H:%M")) == ["12:31"]


def test_london_scanner_cutoff_respects_dst_mismatch_weeks():
    assert scheduled_midday_scanner_cutoff(date(2026, 3, 20)).strftime("%H:%M") == "13:00"
    assert scheduled_midday_scanner_cutoff(date(2026, 4, 1)).strftime("%H:%M") == "12:00"


def test_selection_filters_small_samples_before_ranking():
    tiny = {
        "variant_key": "tiny", "segment_key": "all", "q_value": 0.001,
        "metrics": {
            "observations": 1, "independent_dates": 1, "mean_net_return_pct": 3.5,
            "median_net_return_pct": 3.5, "net_target_success_rate_pct": 100.0,
            "net_target_wilson_low_pct": 20.0, "net_target_daily_ci_low_pct": 100.0,
            "loss_5pct_rate_pct": 0.0, "bootstrap_ci_low_pct": None,
            "profit_factor": None, "best_symbol_profit_share": 1.0,
        },
    }
    robust = {
        "variant_key": "robust", "segment_key": "price_5_to_20", "q_value": 0.01,
        "metrics": {
            "observations": 120, "independent_dates": 24, "mean_net_return_pct": 0.8,
            "median_net_return_pct": 0.4, "net_target_success_rate_pct": 60.0,
            "net_target_wilson_low_pct": 50.0, "net_target_daily_ci_low_pct": 45.0,
            "loss_5pct_rate_pct": 20.0, "bootstrap_ci_low_pct": 0.1,
            "profit_factor": 1.5, "best_symbol_profit_share": 0.20,
        },
    }
    winner, diagnostic, tier = select_validation_record([tiny, robust])
    assert diagnostic["variant_key"] == "tiny"
    assert winner["variant_key"] == "robust"
    assert tier == "strong"


def test_performance_reports_actual_confirmation_entry_time_distribution():
    df = pd.DataFrame([
        {"symbol": "A", "trade_date": date(2026, 7, 1), "entry_at": datetime(2026, 7, 1, 12, 35, tzinfo=NY), "gross_return_pct": 3.5, "target_hit": True, "stop_hit": False, "same_bar_ambiguous": False, "max_gain_pct": 3.5, "max_drawdown_pct": -1.0},
        {"symbol": "B", "trade_date": date(2026, 7, 2), "entry_at": datetime(2026, 7, 2, 12, 45, tzinfo=NY), "gross_return_pct": 3.5, "target_hit": True, "stop_hit": False, "same_bar_ambiguous": False, "max_gain_pct": 3.5, "max_drawdown_pct": -1.0},
    ])
    metrics = performance_metrics(df, cost_bps=50, bootstrap_iterations=100, net_target_pct=3.0)
    assert metrics["median_entry_time_et"] == "12:40"
    assert metrics["p25_entry_time_et"] in {"12:37", "12:38"}
    assert metrics["p75_entry_time_et"] in {"12:42", "12:43"}


def test_gap_through_stop_uses_worse_opening_fill():
    df = frame([
        {"timestamp": pd.Timestamp("2026-07-01 12:31", tz=NY), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100, "vwap": 100.0},
        {"timestamp": pd.Timestamp("2026-07-01 12:40", tz=NY), "open": 85.0, "high": 90.0, "low": 80.0, "close": 88.0, "volume": 100, "vwap": 87.0},
    ])
    result = simulate_trial(df, "fixed_1231_et", 0, "test", 3.5, 5.0)
    assert result.exit_reason == "stop_gap_through"
    assert result.stop_hit is True
    assert result.gross_return_pct == pytest.approx(-15.0)
    assert result.max_drawdown_pct == pytest.approx(-15.0)
