from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")


def normalise_candidate_availability(value: Any, trade_date: date) -> datetime | None:
    """Convert an auditable candidate timestamp into an aware datetime."""
    if value is None:
        return None
    if isinstance(value, time):
        return datetime.combine(trade_date, value, tzinfo=NY)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=NY)
    raise TypeError(f"Unsupported candidate availability value: {type(value).__name__}")


def latest_candidate_availability(values: Iterable[Any], trade_date: date) -> datetime | None:
    """Return when a candidate was actually knowable, using the latest audit timestamp."""
    normalised = [normalise_candidate_availability(value, trade_date) for value in values]
    available = [value for value in normalised if value is not None]
    return max(available) if available else None



def job_marks_historical_calibration(row: dict[str, Any]) -> bool:
    """Return True only when scanner metadata explicitly marks a historical calibration job."""
    decision = str(row.get("source_decision") or "").strip().lower()
    if decision == "calibration":
        return True

    job_source = str(row.get("job_source") or "").strip().lower()
    if "calibrat" in job_source:
        return True

    parameters = row.get("job_parameters")
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except Exception:
            parameters = {}
    if not isinstance(parameters, dict):
        parameters = {}

    for key in ("calibration_request_id", "calibration_id", "is_calibration", "calibration"):
        value = parameters.get(key)
        if value not in (None, False, "", 0, "0", "false", "False"):
            return True
    for key in ("mode", "source", "job_type", "request_type"):
        if "calibrat" in str(parameters.get(key) or "").lower():
            return True
    return False

def scheduled_midday_scanner_cutoff(trade_date: date) -> datetime:
    """Convert the scanner's frozen 17:00 Europe/London cutoff into New York time."""
    london_cutoff = datetime.combine(trade_date, time(17, 0), tzinfo=LONDON)
    return london_cutoff.astimezone(NY)


@dataclass(frozen=True)
class TrialResult:
    variant_key: str
    entry_at: datetime
    entry_price: float
    entry_reason: str
    target_price: float
    stop_price: float
    target_hit: bool
    first_target_hit_at: datetime | None
    stop_hit: bool
    first_stop_hit_at: datetime | None
    exit_at: datetime
    exit_price: float
    exit_reason: str
    gross_return_pct: float
    max_gain_pct: float
    max_drawdown_pct: float
    same_bar_ambiguous: bool


def bars_to_frame(
    raw_bars: list[dict[str, Any]],
    session_open: datetime | None = None,
    session_close: datetime | None = None,
) -> pd.DataFrame:
    if not raw_bars:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "vwap", "trade_count"])
    frame = pd.DataFrame(raw_bars).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap", "n": "trade_count"}
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(NY)
    for column in ["open", "high", "low", "close", "volume", "vwap", "trade_count"]:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame[
        (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (frame["volume"] >= 0)
        & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    ]
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if session_open is not None and session_close is not None:
        open_ts = pd.Timestamp(session_open).tz_convert(NY)
        close_ts = pd.Timestamp(session_close).tz_convert(NY)
        frame = frame[(frame["timestamp"] >= open_ts) & (frame["timestamp"] < close_ts)]
    else:
        frame = frame[(frame["timestamp"].dt.time >= time(9, 30)) & (frame["timestamp"].dt.time < time(16, 0))]
    frame = frame.reset_index(drop=True)
    return frame


def cumulative_vwap(frame: pd.DataFrame) -> pd.Series:
    price = frame["vwap"].where(frame["vwap"].notna(), (frame["high"] + frame["low"] + frame["close"]) / 3)
    volume = frame["volume"].fillna(0).clip(lower=0)
    denominator = volume.cumsum().replace(0, np.nan)
    return (price * volume).cumsum() / denominator


def candidate_features(frame: pd.DataFrame, cutoff_at: datetime) -> dict[str, Any]:
    signal = frame[frame["timestamp"] < cutoff_at].copy()
    if signal.empty:
        raise ValueError("No completed regular-session bars before cutoff")
    signal["cum_vwap"] = cumulative_vwap(signal)
    opening = float(signal.iloc[0]["open"])
    cutoff_price = float(signal.iloc[-1]["close"])
    morning_high = float(signal["high"].max())
    dollar_volume = float((signal["close"] * signal["volume"]).sum())
    vwap = float(signal.iloc[-1]["cum_vwap"]) if pd.notna(signal.iloc[-1]["cum_vwap"]) else cutoff_price
    return {
        "opening_price": opening,
        "cutoff_price": cutoff_price,
        "morning_high": morning_high,
        "cumulative_vwap": vwap,
        "open_to_cutoff_pct": (cutoff_price / opening - 1) * 100,
        "drawdown_from_high_pct": (cutoff_price / morning_high - 1) * 100,
        "below_vwap": cutoff_price < vwap,
        "dollar_volume_to_cutoff": dollar_volume,
        "bars_to_cutoff": int(len(signal)),
    }


def passes_dip_gate(features: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    price = float(features["cutoff_price"])
    if price < float(config["min_price"]):
        reasons.append("price_below_minimum")
    if price > float(config["max_price"]):
        reasons.append("price_above_maximum")
    if float(features["dollar_volume_to_cutoff"]) < float(config["min_dollar_volume"]):
        reasons.append("insufficient_dollar_volume")
    if float(features["drawdown_from_high_pct"]) > -abs(float(config["dip_from_high_pct"])):
        reasons.append("insufficient_pullback_from_high")
    if float(features["open_to_cutoff_pct"]) > -abs(float(config["dip_from_open_pct"])):
        reasons.append("insufficient_decline_from_open")
    if bool(config.get("require_below_vwap", True)) and not bool(features["below_vwap"]):
        reasons.append("not_below_vwap")
    return not reasons, reasons


def segment_matches(features: dict[str, Any], segment_key: str) -> bool:
    """Pre-registered, non-overlapping cutoff-price segments plus the all-candidate view."""
    price = float(features["cutoff_price"])
    if segment_key == "all":
        return True
    if segment_key == "price_2_to_5":
        return 2.0 <= price < 5.0
    if segment_key == "price_5_to_20":
        return 5.0 <= price < 20.0
    if segment_key == "price_20_to_50":
        return 20.0 <= price <= 50.0
    raise ValueError(f"Unknown segment: {segment_key}")


def fixed_entry(frame: pd.DataFrame, trade_date: date, hhmm: str) -> tuple[int, pd.Series, str] | None:
    """Return an exact requested minute; never forward-fill across a missing or halted bar."""
    hour, minute = [int(x) for x in hhmm.split(":")]
    entry_time = pd.Timestamp(datetime.combine(trade_date, time(hour, minute), tzinfo=NY))
    eligible = frame.index[frame["timestamp"] == entry_time]
    if len(eligible) == 0:
        return None
    idx = int(eligible[0])
    return idx, frame.loc[idx], f"fixed_{hhmm.replace(':', '')}_et"


def confirmation_entry(frame: pd.DataFrame, cutoff_at: datetime, kind: str) -> tuple[int, pd.Series, str] | None:
    work = frame.copy()
    work["cum_vwap"] = cumulative_vwap(work)
    indices = list(work.index[work["timestamp"] >= cutoff_at])
    for idx in indices:
        if idx <= 0 or idx + 1 >= len(work):
            continue
        current = work.loc[idx]
        previous = work.loc[idx - 1]
        triggered = False
        if kind == "higher_low":
            triggered = current["low"] > previous["low"] and current["close"] > current["open"]
        elif kind == "vwap_reclaim":
            triggered = (
                pd.notna(previous["cum_vwap"])
                and pd.notna(current["cum_vwap"])
                and previous["close"] <= previous["cum_vwap"]
                and current["close"] > current["cum_vwap"]
            )
        elif kind == "prior_bar_high":
            triggered = current["close"] > previous["high"]
        elif kind == "five_bar_break":
            if idx >= 5:
                triggered = current["close"] > work.loc[idx - 5 : idx - 1, "high"].max()
        else:
            raise ValueError(f"Unknown confirmation kind: {kind}")
        if triggered:
            entry_idx = idx + 1
            expected_entry_at = current["timestamp"] + pd.Timedelta(minutes=1)
            if work.loc[entry_idx, "timestamp"] != expected_entry_at:
                # A halt or missing minute means the next-bar open was not observable.
                continue
            return entry_idx, work.loc[entry_idx], f"confirmation_{kind}"
    return None


def simulate_trial(
    frame: pd.DataFrame,
    variant_key: str,
    entry_idx: int,
    entry_reason: str,
    target_gross_pct: float,
    stop_loss_pct: float,
) -> TrialResult:
    if entry_idx >= len(frame):
        raise ValueError("Entry index outside frame")
    entry_row = frame.loc[entry_idx]
    entry_price = float(entry_row["open"])
    entry_at = entry_row["timestamp"].to_pydatetime()
    target_price = entry_price * (1 + target_gross_pct / 100)
    stop_price = entry_price * (1 - stop_loss_pct / 100)
    target_hit = False
    stop_hit = False
    target_at: datetime | None = None
    stop_at: datetime | None = None
    same_bar_ambiguous = False
    exit_at = frame.iloc[-1]["timestamp"].to_pydatetime()
    exit_price = float(frame.iloc[-1]["close"])
    exit_reason = "market_close"

    path = frame.loc[entry_idx:]
    observed_high = entry_price
    observed_low = entry_price
    for row_idx, row in path.iterrows():
        open_price = float(row["open"])
        row_high = float(row["high"])
        row_low = float(row["low"])
        hit_target = row_high >= target_price
        hit_stop = row_low <= stop_price
        timestamp = row["timestamp"].to_pydatetime()
        if row_idx != entry_idx and open_price <= stop_price:
            observed_high = max(observed_high, open_price)
            observed_low = min(observed_low, open_price)
            stop_hit = True
            stop_at = timestamp
            exit_at = timestamp
            exit_price = open_price
            exit_reason = "stop_gap_through"
            break
        if row_idx != entry_idx and open_price >= target_price:
            observed_high = max(observed_high, open_price)
            observed_low = min(observed_low, open_price)
            target_hit = True
            target_at = timestamp
            exit_at = timestamp
            exit_price = target_price
            exit_reason = "target_gap_open"
            break
        if hit_target and hit_stop:
            same_bar_ambiguous = True
            observed_low = min(observed_low, stop_price)
            stop_hit = True
            stop_at = timestamp
            exit_at = timestamp
            exit_price = stop_price
            exit_reason = "stop_same_bar_conservative"
            break
        if hit_stop:
            observed_low = min(observed_low, stop_price)
            stop_hit = True
            stop_at = timestamp
            exit_at = timestamp
            exit_price = stop_price
            exit_reason = "stop"
            break
        if hit_target:
            # The bar low could have occurred before the target; retain it conservatively.
            observed_low = min(observed_low, row_low)
            observed_high = max(observed_high, target_price)
            target_hit = True
            target_at = timestamp
            exit_at = timestamp
            exit_price = target_price
            exit_reason = "target"
            break
        observed_high = max(observed_high, row_high)
        observed_low = min(observed_low, row_low)

    max_gain_pct = (observed_high / entry_price - 1) * 100
    max_drawdown_pct = (observed_low / entry_price - 1) * 100
    gross_return_pct = (exit_price / entry_price - 1) * 100
    return TrialResult(
        variant_key=variant_key,
        entry_at=entry_at,
        entry_price=entry_price,
        entry_reason=entry_reason,
        target_price=target_price,
        stop_price=stop_price,
        target_hit=target_hit,
        first_target_hit_at=target_at,
        stop_hit=stop_hit,
        first_stop_hit_at=stop_at,
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_pct=gross_return_pct,
        max_gain_pct=max_gain_pct,
        max_drawdown_pct=max_drawdown_pct,
        same_bar_ambiguous=same_bar_ambiguous,
    )


def assign_chronological_splits(dates: Iterable[date], discovery_ratio: float, validation_ratio: float) -> dict[date, str]:
    unique_dates = sorted(set(dates))
    n = len(unique_dates)
    if n == 0:
        return {}
    discovery_end = max(1, int(math.floor(n * discovery_ratio)))
    validation_end = max(discovery_end + 1, int(math.floor(n * (discovery_ratio + validation_ratio)))) if n > 1 else n
    validation_end = min(validation_end, n)
    mapping: dict[date, str] = {}
    for idx, value in enumerate(unique_dates):
        if idx < discovery_end:
            mapping[value] = "discovery"
        elif idx < validation_end:
            mapping[value] = "validation"
        else:
            mapping[value] = "sealed_test"
    return mapping


def block_bootstrap_ci(daily_returns: np.ndarray, iterations: int, seed: int = 562026) -> tuple[float | None, float | None, float | None]:
    """Circular moving-block bootstrap over the ordered daily series."""
    clean = daily_returns[np.isfinite(daily_returns)]
    if len(clean) < 2:
        return None, None, None
    rng = np.random.default_rng(seed)
    n = len(clean)
    block_length = max(1, min(5, int(round(math.sqrt(n)))))
    blocks_needed = int(math.ceil(n / block_length))
    starts = rng.integers(0, n, size=(iterations, blocks_needed))
    offsets = np.arange(block_length)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    sampled = clean[indices].reshape(iterations, -1)[:, :n]
    samples = sampled.mean(axis=1)
    return float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975)), float(np.mean(samples <= 0))


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    if not p_values:
        return []
    arr = np.asarray(p_values, dtype=float)
    order = np.argsort(arr)
    ranked = arr[order]
    n = len(arr)
    adjusted = ranked * n / np.arange(1, n + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    adjusted = np.clip(adjusted, 0, 1)
    out = np.empty(n)
    out[order] = adjusted
    return out.tolist()


def wilson_lower_bound(successes: int, observations: int, z: float = 1.959963984540054) -> float | None:
    if observations <= 0:
        return None
    proportion = successes / observations
    denominator = 1 + (z * z) / observations
    centre = proportion + (z * z) / (2 * observations)
    margin = z * math.sqrt((proportion * (1 - proportion) + (z * z) / (4 * observations)) / observations)
    return (centre - margin) / denominator


def _format_market_minute(value: float | int | None) -> str | None:
    if value is None or not np.isfinite(value):
        return None
    minute = int(round(float(value)))
    minute = max(0, min(23 * 60 + 59, minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def performance_metrics(
    frame: pd.DataFrame,
    cost_bps: int,
    bootstrap_iterations: int,
    net_target_pct: float = 3.0,
) -> dict[str, Any]:
    if frame.empty:
        return {"observations": 0}
    work = frame.copy()
    entry_minutes: pd.Series | None = None
    if "entry_at" in work.columns:
        entry_ts = pd.to_datetime(work["entry_at"], utc=True, errors="coerce").dt.tz_convert(NY)
        entry_minutes = entry_ts.dt.hour * 60 + entry_ts.dt.minute
    work["net_return_pct"] = work["gross_return_pct"] - cost_bps / 100.0
    work["net_target_success"] = work["net_return_pct"] >= float(net_target_pct)
    work["loss_5pct"] = work["net_return_pct"] <= -5.0
    success_count = int(work["net_target_success"].sum())
    success_wilson_low = wilson_lower_bound(success_count, len(work))
    daily = work.groupby("trade_date", sort=True)["net_return_pct"].mean()
    daily_success = work.groupby("trade_date", sort=True)["net_target_success"].mean() * 100.0
    success_ci_low, success_ci_high, _ = block_bootstrap_ci(
        daily_success.to_numpy(dtype=float), bootstrap_iterations, seed=562027
    )
    lower, upper, bootstrap_p = block_bootstrap_ci(daily.to_numpy(dtype=float), bootstrap_iterations)
    t_p = None
    if len(daily) >= 2:
        daily_std = float(daily.std(ddof=1))
        if np.isclose(daily_std, 0.0):
            t_p = 0.0 if float(daily.mean()) > 0 else (1.0 if float(daily.mean()) < 0 else 0.5)
        else:
            statistic = ttest_1samp(daily, popmean=0, alternative="greater", nan_policy="omit")
            t_p = float(statistic.pvalue) if np.isfinite(statistic.pvalue) else None
    positive = work.loc[work["net_return_pct"] > 0, "net_return_pct"].sum()
    negative = abs(work.loc[work["net_return_pct"] < 0, "net_return_pct"].sum())
    profit_factor = float(positive / negative) if negative > 0 else (999.0 if positive > 0 else None)
    compounded = (1 + daily / 100).cumprod()
    drawdown = compounded / compounded.cummax() - 1
    symbol_profit = work.groupby("symbol")["net_return_pct"].sum().sort_values(ascending=False)
    total_positive_profit = float(symbol_profit[symbol_profit > 0].sum())
    best_symbol_share = float(symbol_profit.iloc[0] / total_positive_profit) if total_positive_profit > 0 and len(symbol_profit) else None
    return {
        "observations": int(len(work)),
        "independent_dates": int(work["trade_date"].nunique()),
        "symbols": int(work["symbol"].nunique()),
        "median_entry_time_et": _format_market_minute(float(entry_minutes.median())) if entry_minutes is not None and entry_minutes.notna().any() else None,
        "p25_entry_time_et": _format_market_minute(float(entry_minutes.quantile(0.25))) if entry_minutes is not None and entry_minutes.notna().any() else None,
        "p75_entry_time_et": _format_market_minute(float(entry_minutes.quantile(0.75))) if entry_minutes is not None and entry_minutes.notna().any() else None,
        "target_before_stop_rate_pct": float(work["target_hit"].mean() * 100),
        "stop_before_target_rate_pct": float(work["stop_hit"].mean() * 100),
        "same_bar_ambiguous_rate_pct": float(work["same_bar_ambiguous"].mean() * 100),
        "net_target_pct": float(net_target_pct),
        "net_target_success_rate_pct": float(work["net_target_success"].mean() * 100),
        "net_target_wilson_low_pct": float(success_wilson_low * 100) if success_wilson_low is not None else None,
        "net_target_daily_ci_low_pct": success_ci_low,
        "net_target_daily_ci_high_pct": success_ci_high,
        "loss_5pct_rate_pct": float(work["loss_5pct"].mean() * 100),
        "mean_net_return_pct": float(work["net_return_pct"].mean()),
        "median_net_return_pct": float(work["net_return_pct"].median()),
        "p25_net_return_pct": float(work["net_return_pct"].quantile(0.25)),
        "p75_net_return_pct": float(work["net_return_pct"].quantile(0.75)),
        "win_rate_pct": float((work["net_return_pct"] > 0).mean() * 100),
        "profit_factor": profit_factor,
        "daily_mean_net_return_pct": float(daily.mean()),
        "daily_median_net_return_pct": float(daily.median()),
        "bootstrap_ci_low_pct": lower,
        "bootstrap_ci_high_pct": upper,
        "bootstrap_p_one_sided": bootstrap_p,
        "t_test_p_one_sided": t_p,
        "sequence_max_drawdown_pct": float(drawdown.min() * 100) if len(drawdown) else None,
        "best_symbol_profit_share": best_symbol_share,
    }


def validation_rank_key(record: dict[str, Any]) -> tuple[float, float, float, float, float]:
    metrics = record.get("metrics") or {}
    return (
        float(metrics.get("net_target_success_rate_pct") if metrics.get("net_target_success_rate_pct") is not None else -999),
        float(metrics.get("net_target_daily_ci_low_pct") if metrics.get("net_target_daily_ci_low_pct") is not None else -999),
        float(metrics.get("net_target_wilson_low_pct") if metrics.get("net_target_wilson_low_pct") is not None else -999),
        float(metrics.get("mean_net_return_pct") if metrics.get("mean_net_return_pct") is not None else -999),
        float(metrics.get("median_net_return_pct") if metrics.get("median_net_return_pct") is not None else -999),
    )


def validation_record_tier(record: dict[str, Any]) -> str:
    """Classify a validation record before any cross-variant ranking occurs."""
    metrics = record.get("metrics") or {}
    observations = int(metrics.get("observations") or 0)
    dates = int(metrics.get("independent_dates") or 0)
    mean = float(metrics.get("mean_net_return_pct") if metrics.get("mean_net_return_pct") is not None else -999)
    median = float(metrics.get("median_net_return_pct") if metrics.get("median_net_return_pct") is not None else -999)
    success = float(metrics.get("net_target_success_rate_pct") or 0)
    wilson = float(metrics.get("net_target_wilson_low_pct") or 0)
    daily_low = float(metrics.get("net_target_daily_ci_low_pct") or 0)
    loss5 = float(metrics.get("loss_5pct_rate_pct") if metrics.get("loss_5pct_rate_pct") is not None else 100)
    ci_low = float(metrics.get("bootstrap_ci_low_pct") if metrics.get("bootstrap_ci_low_pct") is not None else -999)
    profit_factor = float(metrics.get("profit_factor") or 0)
    concentration = metrics.get("best_symbol_profit_share")
    q_value = float(record.get("q_value") if record.get("q_value") is not None else 1.0)
    strong = (
        observations >= 100
        and dates >= 20
        and mean >= 0.5
        and median > 0
        and success >= 55.0
        and wilson >= 45.0
        and daily_low >= 40.0
        and loss5 <= 30.0
        and ci_low > 0
        and q_value <= 0.05
        and profit_factor >= 1.2
        and (concentration is None or float(concentration) <= 0.25)
    )
    if strong:
        return "strong"
    promising = (
        observations >= 30
        and dates >= 10
        and mean > 0
        and median > 0
        and success >= 35.0
    )
    return "promising" if promising else "rejected"


def select_validation_record(records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Select only among evidence-qualified records, retaining the raw best for diagnostics."""
    ordered = sorted(records, key=validation_rank_key, reverse=True)
    diagnostic_best = ordered[0] if ordered else None
    for tier in ("strong", "promising"):
        qualified = [record for record in ordered if validation_record_tier(record) == tier]
        if qualified:
            return qualified[0], diagnostic_best, tier
    return None, diagnostic_best, "rejected"


def deterministic_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
