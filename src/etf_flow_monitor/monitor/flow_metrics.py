"""ETF flow proxy metrics.

The starter treats share outstanding changes as the primary flow signal when
available, and keeps turnover/amount rankings as secondary market-activity
signals. Replace the formula once a more authoritative source is selected.
"""

from __future__ import annotations

import pandas as pd

from etf_flow_monitor.data.schemas import ETF_FLOW_COLUMNS, normalize_etf_daily_frame, normalize_etf_share_frame

TUSHARE_FD_SHARE_UNIT_MULTIPLIER = 10_000.0
TUSHARE_DAILY_AMOUNT_UNIT_MULTIPLIER = 1_000.0
MONEY_FUND_QUOTE_DIVISOR = 100.0


def build_flow_snapshot(
    daily: pd.DataFrame,
    shares: pd.DataFrame | None = None,
    basic: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build per-ETF flow rows from daily trading data and optional share data."""
    daily_frame = normalize_etf_daily_frame(daily)
    share_frame = normalize_etf_share_frame(shares)
    if daily_frame.empty:
        return pd.DataFrame(columns=ETF_FLOW_COLUMNS)

    working = daily_frame.copy()
    working["amount"] = pd.to_numeric(working["amount"], errors="coerce") * TUSHARE_DAILY_AMOUNT_UNIT_MULTIPLIER
    if basic is not None and not basic.empty and "fund_code" in basic.columns:
        basic_columns = [column for column in ("fund_code", "name", "fund_type") if column in basic.columns]
        names = basic[basic_columns].drop_duplicates(subset=["fund_code"], keep="last")
        working = working.merge(names, on="fund_code", how="left")
    else:
        working["name"] = ""
    if "fund_type" not in working.columns:
        working["fund_type"] = ""

    if not share_frame.empty:
        working = working.merge(share_frame[["fund_code", "trade_date", "shares", "source"]], on=["fund_code", "trade_date"], how="left")
        working = working.sort_values(["fund_code", "trade_date"], kind="stable")
        working["share_change"] = working.groupby("fund_code")["shares"].diff()
    else:
        working["shares"] = pd.NA
        working["share_change"] = pd.NA
        working["source"] = "amount_only"

    close = pd.to_numeric(working["close"], errors="coerce")
    working["flow_price"] = close
    money_mask = working["fund_type"].fillna("").astype(str).str.contains("货币", regex=False) & close.gt(10)
    working.loc[money_mask, "flow_price"] = close.loc[money_mask] / MONEY_FUND_QUOTE_DIVISOR

    working["estimated_net_flow"] = (
        pd.to_numeric(working["share_change"], errors="coerce")
        * TUSHARE_FD_SHARE_UNIT_MULTIPLIER
        * pd.to_numeric(working["flow_price"], errors="coerce")
    )
    working.loc[working["estimated_net_flow"].isna(), "estimated_net_flow"] = 0.0
    working["flow_direction"] = "flat"
    working.loc[working["estimated_net_flow"].gt(0), "flow_direction"] = "inflow"
    working.loc[working["estimated_net_flow"].lt(0), "flow_direction"] = "outflow"

    result = working.rename(columns={"pct_change": "pct_change"})[
        [
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
    ].copy()
    return result.sort_values(["trade_date", "estimated_net_flow"], ascending=[True, False], kind="stable").reset_index(drop=True)


def build_market_summary(flow: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily whole-market flow and activity."""
    if flow.empty:
        return pd.DataFrame(columns=["trade_date", "fund_count", "total_amount", "estimated_net_flow", "inflow_count", "outflow_count"])
    grouped = flow.groupby("trade_date", as_index=False).agg(
        fund_count=("fund_code", "nunique"),
        total_amount=("amount", "sum"),
        estimated_net_flow=("estimated_net_flow", "sum"),
        inflow_count=("flow_direction", lambda item: int((item == "inflow").sum())),
        outflow_count=("flow_direction", lambda item: int((item == "outflow").sum())),
    )
    return grouped.sort_values("trade_date", kind="stable").reset_index(drop=True)


def select_alert_rows(
    flow: pd.DataFrame,
    *,
    min_amount: float,
    min_abs_flow: float,
    max_rows: int,
) -> pd.DataFrame:
    """Pick largest noteworthy ETF rows for the daily report."""
    if flow.empty:
        return flow.copy()
    working = flow.copy()
    amount = pd.to_numeric(working["amount"], errors="coerce").fillna(0)
    abs_flow = pd.to_numeric(working["estimated_net_flow"], errors="coerce").fillna(0).abs()
    selected = working.loc[amount.ge(float(min_amount)) | abs_flow.ge(float(min_abs_flow))].copy()
    if selected.empty:
        selected = working.copy()
    selected["abs_flow"] = pd.to_numeric(selected["estimated_net_flow"], errors="coerce").fillna(0).abs()
    return selected.sort_values(["trade_date", "abs_flow", "amount"], ascending=[True, False, False], kind="stable").head(max(int(max_rows), 0))
