from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")

TRIGGER_RECIPES: dict[str, str] = {
    "deep_higher_low": "Deep washout seen recently, then first higher-low green bar",
    "deep_prior_high_break": "Deep washout seen recently, then close above prior bar high",
    "deep_two_green": "Deep washout seen recently, then two rising green closes",
    "capitulation_higher_low": "Volume climax during washout, volume contracts, then higher low",
    "capitulation_prior_high": "Volume climax during washout, volume contracts, then prior-high break",
    "momentum_turn_higher_low": "Downside momentum materially decelerates, then higher low",
    "relative_strength_turn": "Stock recovers relative strength versus SPY after a deep washout",
    "vwap_reclaim_after_deep": "Deep washout followed by a confirmed VWAP reclaim",
    "five_bar_break_after_deep": "Deep washout followed by a close above the previous five-bar high",
}

SEGMENTS = ("all", "price_2_to_5", "price_5_to_20", "price_20_to_50")


def normalise_candidate_availability(value: Any, trade_date: date) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, time):
        return datetime.combine(trade_date, value, tzinfo=NY)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=NY)
    raise TypeError(f"Unsupported candidate availability value: {type(value).__name__}")


def latest_candidate_availability(values: Iterable[Any], trade_date: date) -> datetime | None:
    normalised = [normalise_candidate_availability(value, trade_date) for value in values]
    available = [value for value in normalised if value is not None]
    return max(available) if available else None


def job_marks_historical_calibration(row: dict[str, Any]) -> bool:
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


def scheduled_scanner_cutoff(trade_date: date, scan_type: str) -> datetime:
    london_clock = time(14, 0) if scan_type == "pre_open" else time(17, 0)
    london_cutoff = datetime.combine(trade_date, london_clock, tzinfo=LONDON)
    return london_cutoff.astimezone(NY)


@dataclass(frozen=True)
class TriggerEvent:
    recipe_key: str
    trigger_idx: int
    entry_idx: int
    trigger_at: datetime
    entry_at: datetime
    entry_price: float
    trigger_features: dict[str, Any]


@dataclass(frozen=True)
class TrialResult:
    recipe_key: str
    trigger_at: datetime
    entry_at: datetime
    entry_price: float
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
    trigger_features: dict[str, Any]


def bars_to_frame(
    raw_bars: list[dict[str, Any]],
    session_open: datetime | None = None,
    session_close: datetime | None = None,
) -> pd.DataFrame:
    columns = ["timestamp", "open", "high", "low", "close", "volume", "vwap", "trade_count"]
    if not raw_bars:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(raw_bars).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap", "n": "trade_count"}
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(NY)
    for column in columns[1:]:
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
    return frame.reset_index(drop=True)


def cumulative_vwap(frame: pd.DataFrame) -> pd.Series:
    typical = frame["vwap"].where(frame["vwap"].notna(), (frame["high"] + frame["low"] + frame["close"]) / 3)
    volume = frame["volume"].fillna(0).clip(lower=0)
    denominator = volume.cumsum().replace(0, np.nan)
    return (typical * volume).cumsum() / denominator


def enrich_intraday_features(frame: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build point-in-time features. Every row uses only that bar and earlier rows."""
    work = frame.copy().reset_index(drop=True)
    if work.empty:
        return work
    work["cum_vwap"] = cumulative_vwap(work)
    work["session_high"] = work["high"].cummax()
    work["session_low"] = work["low"].cummin()
    opening = float(work.iloc[0]["open"])
    work["open_return_pct"] = (work["close"] / opening - 1.0) * 100.0
    work["drawdown_from_high_pct"] = (work["close"] / work["session_high"] - 1.0) * 100.0
    work["below_vwap_pct"] = (work["close"] / work["cum_vwap"] - 1.0) * 100.0
    work["dollar_volume"] = (work["close"] * work["volume"]).cumsum()

    previous_close = work["close"].shift(1)
    true_range = pd.concat(
        [
            work["high"] - work["low"],
            (work["high"] - previous_close).abs(),
            (work["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    work["atr20_pct"] = true_range.rolling(20, min_periods=10).mean() / work["close"] * 100.0
    work["drawdown_atr"] = (-work["drawdown_from_high_pct"]) / work["atr20_pct"].replace(0, np.nan)

    work["ret_1m_pct"] = work["close"].pct_change() * 100.0
    work["ret_3m_pct"] = work["close"].pct_change(3) * 100.0
    work["ret_5m_pct"] = work["close"].pct_change(5) * 100.0
    work["prior_3m_pct"] = work["close"].shift(3).pct_change(3) * 100.0
    rolling_std = work["ret_1m_pct"].rolling(30, min_periods=15).std(ddof=1)
    work["ret_5m_z"] = work["ret_5m_pct"] / (rolling_std * math.sqrt(5)).replace(0, np.nan)

    trailing_median_volume = work["volume"].shift(1).rolling(20, min_periods=10).median()
    work["volume_ratio_20"] = work["volume"] / trailing_median_volume.replace(0, np.nan)
    work["green"] = work["close"] > work["open"]
    work["higher_low"] = (work["low"] > work["low"].shift(1)) & work["green"]
    work["prior_high_break"] = work["close"] > work["high"].shift(1)
    work["two_green"] = work["green"] & work["green"].shift(1, fill_value=False) & (work["close"] > work["close"].shift(1))
    work["vwap_reclaim"] = (work["close"].shift(1) <= work["cum_vwap"].shift(1)) & (work["close"] > work["cum_vwap"])
    work["five_bar_break"] = work["close"] > work["high"].shift(1).rolling(5, min_periods=5).max()
    work["momentum_improvement_pct"] = work["ret_3m_pct"] - work["prior_3m_pct"]

    if benchmark is not None and not benchmark.empty:
        bench = benchmark[["timestamp", "close"]].copy().rename(columns={"close": "benchmark_close"})
        work = work.merge(bench, on="timestamp", how="left")
        work["benchmark_close"] = work["benchmark_close"].ffill()
        work["benchmark_ret_3m_pct"] = work["benchmark_close"].pct_change(3) * 100.0
        work["benchmark_ret_5m_pct"] = work["benchmark_close"].pct_change(5) * 100.0
        work["relative_3m_pct"] = work["ret_3m_pct"] - work["benchmark_ret_3m_pct"]
        work["relative_5m_pct"] = work["ret_5m_pct"] - work["benchmark_ret_5m_pct"]
        work["relative_turn"] = (work["relative_3m_pct"] > 0) & (work["relative_5m_pct"].shift(3) < 0)
    else:
        work["benchmark_close"] = np.nan
        work["benchmark_ret_3m_pct"] = np.nan
        work["benchmark_ret_5m_pct"] = np.nan
        work["relative_3m_pct"] = np.nan
        work["relative_5m_pct"] = np.nan
        work["relative_turn"] = False
    return work


def _recipe_condition(recipe: str, row: pd.Series) -> bool:
    deep = bool(row["deep_recent"])
    climax = bool(row["climax_recent"])
    contraction = bool(row["volume_contracted"])
    if recipe == "deep_higher_low":
        return deep and bool(row["higher_low"])
    if recipe == "deep_prior_high_break":
        return deep and bool(row["prior_high_break"])
    if recipe == "deep_two_green":
        return deep and bool(row["two_green"])
    if recipe == "capitulation_higher_low":
        return deep and climax and contraction and bool(row["higher_low"])
    if recipe == "capitulation_prior_high":
        return deep and climax and contraction and bool(row["prior_high_break"])
    if recipe == "momentum_turn_higher_low":
        return deep and bool(row["higher_low"]) and float(row["momentum_improvement_pct"] or 0) >= 1.0
    if recipe == "relative_strength_turn":
        return deep and bool(row["relative_turn"]) and bool(row["prior_high_break"])
    if recipe == "vwap_reclaim_after_deep":
        return deep and bool(row["vwap_reclaim"])
    if recipe == "five_bar_break_after_deep":
        return deep and bool(row["five_bar_break"])
    raise ValueError(f"Unknown trigger recipe: {recipe}")


def find_trigger_event(
    frame: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    recipe: str,
    search_start: datetime,
    search_end: datetime,
    config: dict[str, Any],
) -> TriggerEvent | None:
    """Find the first auditable trigger and enter at the next exact one-minute bar open."""
    if recipe not in TRIGGER_RECIPES:
        raise ValueError(f"Unknown trigger recipe: {recipe}")
    work = enrich_intraday_features(frame, benchmark)
    if work.empty:
        return None
    memory = int(config.get("oversold_memory_minutes", 15))
    min_dd_high = abs(float(config.get("min_drawdown_high_pct", 5.0)))
    min_dd_open = abs(float(config.get("min_drawdown_open_pct", 2.0)))
    min_below_vwap = abs(float(config.get("min_below_vwap_pct", 1.0)))
    min_dollar_volume = float(config.get("min_dollar_volume", 5_000_000))
    min_price = float(config.get("min_price", 2.0))
    max_price = float(config.get("max_price", 50.0))
    volume_climax_ratio = float(config.get("volume_climax_ratio", 2.5))
    min_history_bars = int(config.get("min_history_bars", 30))

    oversold_now = (
        (work["drawdown_from_high_pct"] <= -min_dd_high)
        & (work["open_return_pct"] <= -min_dd_open)
        & (work["below_vwap_pct"] <= -min_below_vwap)
    )
    work["deep_recent"] = oversold_now.rolling(memory, min_periods=1).max().astype(bool)
    work["deepest_drawdown_recent_pct"] = work["drawdown_from_high_pct"].rolling(memory, min_periods=1).min()
    work["deepest_below_vwap_recent_pct"] = work["below_vwap_pct"].rolling(memory, min_periods=1).min()
    climax_now = oversold_now & (work["close"] < work["open"]) & (work["volume_ratio_20"] >= volume_climax_ratio)
    work["climax_recent"] = climax_now.rolling(min(10, memory), min_periods=1).max().astype(bool)
    work["max_volume_ratio_recent"] = work["volume_ratio_20"].rolling(min(10, memory), min_periods=1).max()
    work["volume_contracted"] = work["volume_ratio_20"] <= max(1.25, volume_climax_ratio * 0.55)

    start_ts = pd.Timestamp(search_start).tz_convert(NY)
    end_ts = pd.Timestamp(search_end).tz_convert(NY)
    eligible = work.index[(work["timestamp"] >= start_ts) & (work["timestamp"] <= end_ts)]
    for idx in eligible:
        if idx < min_history_bars or idx + 1 >= len(work):
            continue
        row = work.loc[idx]
        price = float(row["close"])
        if not (min_price <= price <= max_price):
            continue
        if float(row["dollar_volume"]) < min_dollar_volume:
            continue
        if not _recipe_condition(recipe, row):
            continue
        expected_entry_at = row["timestamp"] + pd.Timedelta(minutes=1)
        if work.loc[idx + 1, "timestamp"] != expected_entry_at:
            continue
        feature_names = [
            "close", "open_return_pct", "drawdown_from_high_pct", "below_vwap_pct", "atr20_pct",
            "drawdown_atr", "ret_3m_pct", "ret_5m_pct", "ret_5m_z", "momentum_improvement_pct",
            "volume_ratio_20", "max_volume_ratio_recent", "deepest_drawdown_recent_pct",
            "deepest_below_vwap_recent_pct", "relative_3m_pct", "relative_5m_pct", "dollar_volume",
            "higher_low", "prior_high_break", "two_green", "vwap_reclaim", "five_bar_break",
            "climax_recent", "volume_contracted", "relative_turn",
        ]
        features: dict[str, Any] = {}
        for name in feature_names:
            value = row.get(name)
            if pd.isna(value):
                features[name] = None
            elif isinstance(value, (np.bool_, bool)):
                features[name] = bool(value)
            elif isinstance(value, (np.integer, int)):
                features[name] = int(value)
            else:
                features[name] = float(value)
        features["recipe_description"] = TRIGGER_RECIPES[recipe]
        entry_row = work.loc[idx + 1]
        return TriggerEvent(
            recipe_key=recipe,
            trigger_idx=int(idx),
            entry_idx=int(idx + 1),
            trigger_at=row["timestamp"].to_pydatetime(),
            entry_at=entry_row["timestamp"].to_pydatetime(),
            entry_price=float(entry_row["open"]),
            trigger_features=features,
        )
    return None


def simulate_trial(
    frame: pd.DataFrame,
    event: TriggerEvent,
    target_gross_pct: float,
    stop_loss_pct: float,
) -> TrialResult:
    entry_idx = event.entry_idx
    if entry_idx >= len(frame):
        raise ValueError("Entry index outside frame")
    entry_price = float(frame.loc[entry_idx, "open"])
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
    observed_high = entry_price
    observed_low = entry_price

    for row_idx, row in frame.loc[entry_idx:].iterrows():
        open_price = float(row["open"])
        row_high = float(row["high"])
        row_low = float(row["low"])
        timestamp = row["timestamp"].to_pydatetime()
        if row_idx != entry_idx and open_price <= stop_price:
            observed_low = min(observed_low, open_price)
            observed_high = max(observed_high, open_price)
            stop_hit, stop_at = True, timestamp
            exit_at, exit_price, exit_reason = timestamp, open_price, "stop_gap_through"
            break
        if row_idx != entry_idx and open_price >= target_price:
            observed_low = min(observed_low, open_price)
            observed_high = max(observed_high, open_price)
            target_hit, target_at = True, timestamp
            exit_at, exit_price, exit_reason = timestamp, target_price, "target_gap_open"
            break
        hit_target = row_high >= target_price
        hit_stop = row_low <= stop_price
        if hit_target and hit_stop:
            same_bar_ambiguous = True
            observed_low = min(observed_low, stop_price)
            stop_hit, stop_at = True, timestamp
            exit_at, exit_price, exit_reason = timestamp, stop_price, "stop_same_bar_conservative"
            break
        if hit_stop:
            observed_low = min(observed_low, stop_price)
            stop_hit, stop_at = True, timestamp
            exit_at, exit_price, exit_reason = timestamp, stop_price, "stop"
            break
        if hit_target:
            observed_low = min(observed_low, row_low)
            observed_high = max(observed_high, target_price)
            target_hit, target_at = True, timestamp
            exit_at, exit_price, exit_reason = timestamp, target_price, "target"
            break
        observed_high = max(observed_high, row_high)
        observed_low = min(observed_low, row_low)

    return TrialResult(
        recipe_key=event.recipe_key,
        trigger_at=event.trigger_at,
        entry_at=event.entry_at,
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        target_hit=target_hit,
        first_target_hit_at=target_at,
        stop_hit=stop_hit,
        first_stop_hit_at=stop_at,
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_pct=(exit_price / entry_price - 1) * 100,
        max_gain_pct=(observed_high / entry_price - 1) * 100,
        max_drawdown_pct=(observed_low / entry_price - 1) * 100,
        same_bar_ambiguous=same_bar_ambiguous,
        trigger_features=event.trigger_features,
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
        mapping[value] = "discovery" if idx < discovery_end else ("validation" if idx < validation_end else "sealed_test")
    return mapping


def segment_matches(price: float, segment_key: str) -> bool:
    if segment_key == "all":
        return True
    if segment_key == "price_2_to_5":
        return 2 <= price < 5
    if segment_key == "price_5_to_20":
        return 5 <= price < 20
    if segment_key == "price_20_to_50":
        return 20 <= price <= 50
    raise ValueError(f"Unknown segment: {segment_key}")


def block_bootstrap_ci(values: np.ndarray, iterations: int, seed: int = 562026) -> tuple[float | None, float | None, float | None]:
    clean = values[np.isfinite(values)]
    if len(clean) < 2:
        return None, None, None
    rng = np.random.default_rng(seed)
    n = len(clean)
    block_length = max(1, min(5, int(round(math.sqrt(n)))))
    blocks_needed = int(math.ceil(n / block_length))
    starts = rng.integers(0, n, size=(iterations, blocks_needed))
    indices = (starts[:, :, None] + np.arange(block_length)[None, None, :]) % n
    samples = clean[indices].reshape(iterations, -1)[:, :n].mean(axis=1)
    return float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975)), float(np.mean(samples <= 0))


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    if not p_values:
        return []
    arr = np.asarray(p_values, dtype=float)
    order = np.argsort(arr)
    ranked = arr[order]
    n = len(arr)
    adjusted = np.minimum.accumulate((ranked * n / np.arange(1, n + 1))[::-1])[::-1]
    adjusted = np.clip(adjusted, 0, 1)
    out = np.empty(n)
    out[order] = adjusted
    return out.tolist()


def wilson_lower_bound(successes: int, observations: int, z: float = 1.959963984540054) -> float | None:
    if observations <= 0:
        return None
    p = successes / observations
    denominator = 1 + z * z / observations
    centre = p + z * z / (2 * observations)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * observations)) / observations)
    return (centre - margin) / denominator


def _market_minute(value: float | int | None) -> str | None:
    if value is None or not np.isfinite(value):
        return None
    minute = max(0, min(1439, int(round(float(value)))))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _chronological_fold_means(daily: pd.Series) -> list[float]:
    if daily.empty:
        return []
    parts = np.array_split(daily.sort_index().to_numpy(dtype=float), min(3, len(daily)))
    return [float(np.mean(part)) for part in parts if len(part)]


def performance_metrics(frame: pd.DataFrame, cost_bps: int, bootstrap_iterations: int, net_target_pct: float = 3.0) -> dict[str, Any]:
    if frame.empty:
        return {"observations": 0}
    work = frame.copy()
    work["net_return_pct"] = (work["gross_return_pct"] - cost_bps / 100.0).round(10)
    work["net_target_success"] = work["net_return_pct"] >= float(net_target_pct) - 1e-9
    work["loss_5pct"] = work["net_return_pct"] <= -5.0
    daily = work.groupby("trade_date", sort=True)["net_return_pct"].mean()
    symbols = work.groupby("symbol", sort=True)["net_return_pct"].mean()
    lower, upper, bootstrap_p = block_bootstrap_ci(daily.to_numpy(dtype=float), bootstrap_iterations)
    success_count = int(work["net_target_success"].sum())
    success_wilson = wilson_lower_bound(success_count, len(work))
    positive = float(work.loc[work["net_return_pct"] > 0, "net_return_pct"].sum())
    negative = abs(float(work.loc[work["net_return_pct"] < 0, "net_return_pct"].sum()))
    profit_factor = positive / negative if negative > 0 else (999.0 if positive > 0 else None)
    symbol_profit = work.groupby("symbol")["net_return_pct"].sum().sort_values(ascending=False)
    date_profit = work.groupby("trade_date")["net_return_pct"].sum().sort_values(ascending=False)
    total_positive_symbol = float(symbol_profit[symbol_profit > 0].sum())
    total_positive_date = float(date_profit[date_profit > 0].sum())
    entry_ts = pd.to_datetime(work["entry_at"], utc=True, errors="coerce").dt.tz_convert(NY)
    entry_minutes = entry_ts.dt.hour * 60 + entry_ts.dt.minute
    fold_means = _chronological_fold_means(daily)
    t_p = None
    if len(daily) >= 2:
        if np.isclose(float(daily.std(ddof=1)), 0):
            t_p = 0.0 if float(daily.mean()) > 0 else 1.0
        else:
            t_p = float(ttest_1samp(daily, 0, alternative="greater", nan_policy="omit").pvalue)
    return {
        "observations": int(len(work)),
        "independent_dates": int(work["trade_date"].nunique()),
        "symbols": int(work["symbol"].nunique()),
        "mean_net_return_pct": float(work["net_return_pct"].mean()),
        "median_net_return_pct": float(work["net_return_pct"].median()),
        "win_rate_pct": float((work["net_return_pct"] > 0).mean() * 100),
        "net_target_success_rate_pct": float(work["net_target_success"].mean() * 100),
        "net_target_wilson_low_pct": float(success_wilson * 100) if success_wilson is not None else None,
        "loss_5pct_rate_pct": float(work["loss_5pct"].mean() * 100),
        "target_before_stop_rate_pct": float(work["target_hit"].mean() * 100),
        "stop_before_target_rate_pct": float(work["stop_hit"].mean() * 100),
        "mean_mfe_pct": float(work["max_gain_pct"].mean()),
        "median_mfe_pct": float(work["max_gain_pct"].median()),
        "mean_mae_pct": float(work["max_drawdown_pct"].mean()),
        "median_mae_pct": float(work["max_drawdown_pct"].median()),
        "positive_date_rate_pct": float((daily > 0).mean() * 100),
        "positive_symbol_rate_pct": float((symbols > 0).mean() * 100),
        "fold_means_pct": fold_means,
        "worst_fold_mean_pct": min(fold_means) if fold_means else None,
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "best_symbol_profit_share": float(symbol_profit.iloc[0] / total_positive_symbol) if total_positive_symbol > 0 and len(symbol_profit) else None,
        "best_date_profit_share": float(date_profit.iloc[0] / total_positive_date) if total_positive_date > 0 and len(date_profit) else None,
        "bootstrap_ci_low_pct": lower,
        "bootstrap_ci_high_pct": upper,
        "bootstrap_p_one_sided": bootstrap_p,
        "t_test_p_one_sided": t_p,
        "median_entry_time_et": _market_minute(float(entry_minutes.median())) if entry_minutes.notna().any() else None,
        "p25_entry_time_et": _market_minute(float(entry_minutes.quantile(0.25))) if entry_minutes.notna().any() else None,
        "p75_entry_time_et": _market_minute(float(entry_minutes.quantile(0.75))) if entry_minutes.notna().any() else None,
    }


def materially_consistent(metrics: dict[str, Any], q_value: float, strong: bool = True) -> bool:
    """Deliberately demanding gates: consistency matters more than a high isolated average."""
    if strong:
        return (
            metrics.get("observations", 0) >= 100
            and metrics.get("independent_dates", 0) >= 20
            and metrics.get("symbols", 0) >= 20
            and (metrics.get("mean_net_return_pct") or -999) >= 0.75
            and (metrics.get("median_net_return_pct") or -999) >= 0.25
            and (metrics.get("bootstrap_ci_low_pct") if metrics.get("bootstrap_ci_low_pct") is not None else -999) > 0
            and (metrics.get("net_target_success_rate_pct") or 0) >= 40
            and (metrics.get("net_target_wilson_low_pct") or 0) >= 30
            and (metrics.get("positive_date_rate_pct") or 0) >= 60
            and (metrics.get("positive_symbol_rate_pct") or 0) >= 55
            and (metrics.get("worst_fold_mean_pct") if metrics.get("worst_fold_mean_pct") is not None else -999) > 0
            and (metrics.get("profit_factor") or 0) >= 1.5
            and (metrics.get("loss_5pct_rate_pct") if metrics.get("loss_5pct_rate_pct") is not None else 100) <= 25
            and (metrics.get("best_symbol_profit_share") is None or metrics.get("best_symbol_profit_share") <= 0.20)
            and (metrics.get("best_date_profit_share") is None or metrics.get("best_date_profit_share") <= 0.25)
            and q_value <= 0.05
        )
    return (
        metrics.get("observations", 0) >= 30
        and metrics.get("independent_dates", 0) >= 10
        and metrics.get("symbols", 0) >= 10
        and (metrics.get("mean_net_return_pct") or -999) >= 0.35
        and (metrics.get("median_net_return_pct") or -999) > 0
        and (metrics.get("positive_date_rate_pct") or 0) >= 55
        and (metrics.get("positive_symbol_rate_pct") or 0) >= 50
        and (metrics.get("worst_fold_mean_pct") if metrics.get("worst_fold_mean_pct") is not None else -999) >= -0.10
        and (metrics.get("profit_factor") or 0) >= 1.2
        and (metrics.get("best_symbol_profit_share") is None or metrics.get("best_symbol_profit_share") <= 0.30)
        and (metrics.get("best_date_profit_share") is None or metrics.get("best_date_profit_share") <= 0.35)
        and q_value <= 0.20
    )



def compelling_small_sample(metrics: dict[str, Any], q_value: float) -> bool:
    """Exceptional-but-small evidence worthy of a frozen sealed test, never live approval."""
    return (
        metrics.get("observations", 0) >= 12
        and metrics.get("independent_dates", 0) >= 3
        and metrics.get("symbols", 0) >= 5
        and (metrics.get("mean_net_return_pct") or -999) >= 1.50
        and (metrics.get("median_net_return_pct") or -999) >= 0.75
        and (metrics.get("net_target_success_rate_pct") or 0) >= 50
        and (metrics.get("positive_date_rate_pct") or 0) >= 100
        and (metrics.get("positive_symbol_rate_pct") or 0) >= 70
        and (metrics.get("worst_fold_mean_pct") if metrics.get("worst_fold_mean_pct") is not None else -999) >= 0.50
        and (metrics.get("profit_factor") or 0) >= 2.0
        and (metrics.get("loss_5pct_rate_pct") if metrics.get("loss_5pct_rate_pct") is not None else 100) <= 20
        and (metrics.get("best_symbol_profit_share") is None or metrics.get("best_symbol_profit_share") <= 0.35)
        and (metrics.get("best_date_profit_share") is None or metrics.get("best_date_profit_share") <= 0.45)
        and q_value <= 0.25
    )

def select_validation_record(records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    if not records:
        return None, None, "none"
    diagnostic = max(
        records,
        key=lambda r: (
            float(r["metrics"].get("worst_fold_mean_pct") or -999),
            float(r["metrics"].get("median_net_return_pct") or -999),
            float(r["metrics"].get("mean_net_return_pct") or -999),
            int(r["metrics"].get("independent_dates") or 0),
        ),
    )
    strong = [r for r in records if materially_consistent(r["metrics"], float(r.get("q_value", 1.0)), True)]
    if strong:
        winner = max(strong, key=lambda r: (r["metrics"]["worst_fold_mean_pct"], r["metrics"]["median_net_return_pct"], r["metrics"]["mean_net_return_pct"]))
        return winner, diagnostic, "strong"
    promising = [r for r in records if materially_consistent(r["metrics"], float(r.get("q_value", 1.0)), False)]
    if promising:
        winner = max(promising, key=lambda r: (r["metrics"]["worst_fold_mean_pct"], r["metrics"]["median_net_return_pct"], r["metrics"]["mean_net_return_pct"]))
        return winner, diagnostic, "promising"
    compelling = [r for r in records if compelling_small_sample(r["metrics"], float(r.get("q_value", 1.0)))]
    if compelling:
        winner = max(compelling, key=lambda r: (r["metrics"]["worst_fold_mean_pct"], r["metrics"]["median_net_return_pct"], r["metrics"]["mean_net_return_pct"]))
        return winner, diagnostic, "compelling_small_sample"
    return None, diagnostic, "none"
