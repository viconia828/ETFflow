"""Cross-border ETF calendar-gap filling helpers."""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from etf_flow_monitor.data.category_map import normalize_category_map
from etf_flow_monitor.data.schemas import normalize_etf_daily_frame, normalize_etf_share_frame


CROSS_BORDER_CATEGORIES = {"港股", "海外", "港股海外"}


def fill_cross_border_previous_values(
    daily: pd.DataFrame,
    shares: pd.DataFrame,
    category_map: pd.DataFrame,
    trade_dates: list[object],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Forward-fill cross-border ETF rows across A-share trading dates.

    Hong Kong Stock Connect and QDII ETFs can have source gaps around overseas
    market holidays or disclosure timing differences. For those funds, carry the
    previous available row forward so daily reports keep a stable universe.
    """

    target_codes = _cross_border_codes(category_map)
    daily_filled, daily_stats = _fill_frame(
        normalize_etf_daily_frame(daily),
        target_codes=target_codes,
        trade_dates=trade_dates,
        normalizer=normalize_etf_daily_frame,
    )
    share_filled, share_stats = _fill_frame(
        normalize_etf_share_frame(shares),
        target_codes=target_codes,
        trade_dates=trade_dates,
        normalizer=normalize_etf_share_frame,
        source_suffix=":ffill_previous_trading_day",
    )
    return daily_filled, share_filled, {
        "cross_border_fill_target_funds": len(target_codes),
        "cross_border_daily_ffill_rows": daily_stats["filled_rows"],
        "cross_border_daily_ffill_funds": daily_stats["filled_funds"],
        "cross_border_share_ffill_rows": share_stats["filled_rows"],
        "cross_border_share_ffill_funds": share_stats["filled_funds"],
    }


def _cross_border_codes(category_map: pd.DataFrame) -> list[str]:
    normalized = normalize_category_map(category_map)
    if normalized.empty:
        return []
    mask = normalized["category"].astype(str).isin(CROSS_BORDER_CATEGORIES)
    return normalized.loc[mask, "fund_code"].dropna().astype(str).str.upper().drop_duplicates().tolist()


def _fill_frame(
    frame: pd.DataFrame,
    *,
    target_codes: list[str],
    trade_dates: list[object],
    normalizer: Callable[[pd.DataFrame | None], pd.DataFrame],
    source_suffix: str = "",
) -> tuple[pd.DataFrame, dict[str, int]]:
    normalized = normalizer(frame)
    required_dates = _required_dates(trade_dates)
    if normalized.empty or not target_codes or len(required_dates) == 0:
        return normalized, {"filled_rows": 0, "filled_funds": 0}

    target_set = set(target_codes)
    target_frame = normalized.loc[normalized["fund_code"].astype(str).str.upper().isin(target_set)].copy()
    if target_frame.empty:
        return normalized, {"filled_rows": 0, "filled_funds": 0}

    filled_frames: list[pd.DataFrame] = []
    filled_funds: set[str] = set()
    columns = list(normalized.columns)
    for code, code_frame in target_frame.groupby("fund_code", sort=False):
        current = code_frame.drop_duplicates(subset=["trade_date"], keep="last").sort_values("trade_date", kind="stable")
        original_dates = set(pd.to_datetime(current["trade_date"], errors="coerce").dt.normalize().dropna())
        missing_dates = [trade_date for trade_date in required_dates if trade_date not in original_dates]
        if not missing_dates:
            continue

        reindexed = current.set_index("trade_date").reindex(required_dates).ffill()
        filler = reindexed.loc[missing_dates].dropna(subset=["fund_code"]).copy()
        if filler.empty:
            continue

        filler["trade_date"] = filler.index
        filler["fund_code"] = str(code).upper()
        if source_suffix and "source" in filler.columns:
            source = filler["source"].fillna("unknown").astype(str)
            filler["source"] = source.where(source.str.endswith(source_suffix), source + source_suffix)
        filled_frames.append(filler.reset_index(drop=True)[columns])
        filled_funds.add(str(code).upper())

    if not filled_frames:
        return normalized, {"filled_rows": 0, "filled_funds": 0}

    result = normalizer(pd.concat([normalized, *filled_frames], ignore_index=True))
    filled_rows = sum(len(frame) for frame in filled_frames)
    return result, {"filled_rows": int(filled_rows), "filled_funds": len(filled_funds)}


def _required_dates(trade_dates: list[object]) -> pd.DatetimeIndex:
    if not trade_dates:
        return pd.DatetimeIndex([])
    dates = pd.to_datetime(pd.Series(trade_dates), errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    return pd.DatetimeIndex(dates)
