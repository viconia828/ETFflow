"""Standard schemas and normalization helpers for ETF flow monitoring."""

from __future__ import annotations

from typing import Any

import pandas as pd


ETF_BASIC_COLUMNS = [
    "fund_code",
    "name",
    "market",
    "fund_type",
    "management",
    "custodian",
    "list_date",
    "delist_date",
    "status",
    "benchmark",
]

ETF_DAILY_COLUMNS = [
    "fund_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "price_change",
    "pct_change",
    "volume",
    "amount",
]

ETF_SHARE_COLUMNS = [
    "fund_code",
    "trade_date",
    "shares",
    "source",
]

ETF_FLOW_COLUMNS = [
    "fund_code",
    "trade_date",
    "name",
    "close",
    "flow_price",
    "pct_change",
    "volume",
    "amount",
    "shares",
    "share_change",
    "estimated_net_flow",
    "flow_direction",
    "source",
]


def _ensure_columns(frame: pd.DataFrame | None, columns: list[str]) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.copy()
    for column in columns:
        if column not in working.columns:
            working[column] = pd.NA
    return working[columns].copy()


def _to_date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def normalize_etf_basic_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    working = pd.DataFrame() if frame is None else frame.copy()
    rename_map = {
        "ts_code": "fund_code",
        "management": "management",
        "custodian": "custodian",
        "fund_type": "fund_type",
        "market": "market",
    }
    working = working.rename(columns=rename_map)
    result = _ensure_columns(working, ETF_BASIC_COLUMNS)
    result["fund_code"] = result["fund_code"].astype("string").str.strip().str.upper()
    for column in ("list_date", "delist_date"):
        result[column] = _to_date_series(result[column])
    return result[result["fund_code"].fillna("").ne("")].drop_duplicates(subset=["fund_code"], keep="last").reset_index(drop=True)


def normalize_etf_daily_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    working = pd.DataFrame() if frame is None else frame.copy()
    rename_map = {
        "ts_code": "fund_code",
        "trade_date": "trade_date",
        "change": "price_change",
        "pct_chg": "pct_change",
        "vol": "volume",
    }
    working = working.rename(columns=rename_map)
    result = _ensure_columns(working, ETF_DAILY_COLUMNS)
    result["fund_code"] = result["fund_code"].astype("string").str.strip().str.upper()
    result["trade_date"] = _to_date_series(result["trade_date"])
    for column in ("open", "high", "low", "close", "pre_close", "price_change", "pct_change", "volume", "amount"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return (
        result[result["fund_code"].fillna("").ne("") & result["trade_date"].notna()]
        .drop_duplicates(subset=["fund_code", "trade_date"], keep="last")
        .sort_values(["fund_code", "trade_date"], kind="stable")
        .reset_index(drop=True)
    )


def normalize_etf_share_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    working = pd.DataFrame() if frame is None else frame.copy()
    rename_map = {
        "ts_code": "fund_code",
        "trade_date": "trade_date",
        "fd_share": "shares",
        "fund_share": "shares",
    }
    working = working.rename(columns=rename_map)
    result = _ensure_columns(working, ETF_SHARE_COLUMNS)
    result["fund_code"] = result["fund_code"].astype("string").str.strip().str.upper()
    result["trade_date"] = _to_date_series(result["trade_date"])
    result["shares"] = pd.to_numeric(result["shares"], errors="coerce")
    result["source"] = result["source"].fillna("unknown").astype("string")
    return (
        result[result["fund_code"].fillna("").ne("") & result["trade_date"].notna()]
        .drop_duplicates(subset=["fund_code", "trade_date"], keep="last")
        .sort_values(["fund_code", "trade_date"], kind="stable")
        .reset_index(drop=True)
    )


def normalize_calendar_frame(rows: list[dict[str, Any]] | pd.DataFrame | None) -> pd.DataFrame:
    frame = pd.DataFrame() if rows is None else pd.DataFrame(rows)
    for column in ("exchange", "cal_date", "is_open", "pretrade_date"):
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[["exchange", "cal_date", "is_open", "pretrade_date"]].copy()
    frame["exchange"] = frame["exchange"].fillna("SSE").astype("string").str.upper()
    frame["cal_date"] = pd.to_datetime(frame["cal_date"], errors="coerce")
    frame["pretrade_date"] = pd.to_datetime(frame["pretrade_date"], errors="coerce")
    frame["is_open"] = pd.to_numeric(frame["is_open"], errors="coerce").fillna(0).astype(int)
    return (
        frame[frame["cal_date"].notna()]
        .sort_values(["exchange", "cal_date"], kind="stable")
        .drop_duplicates(subset=["exchange", "cal_date"], keep="last")
        .reset_index(drop=True)
    )
