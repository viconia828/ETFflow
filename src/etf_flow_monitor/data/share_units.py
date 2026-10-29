"""Explicit listed-share units and evidence-backed historical overrides."""
from pathlib import Path
import pandas as pd
from etf_flow_monitor.data.schemas import ACTUAL_SHARE_UNIT, normalize_etf_share_frame

SZ_MONEY_CODES = {"159001.SZ", "159003.SZ", "159005.SZ"}
VERIFIED_HISTORY_END = pd.Timestamp("2026-10-26")


class ShareDataQualityError(ValueError):
    """Do not downgrade an invalid share basis into an amount-only report."""


def load_share_overrides(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"fund_code", "trade_date", "shares", "source", "share_unit", "evidence"}
    if not required.issubset(frame.columns):
        raise ShareDataQualityError(f"Invalid verified share overrides: {path}")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise")
    if frame.duplicated(["fund_code", "trade_date"]).any() or not frame.share_unit.eq(ACTUAL_SHARE_UNIT).all():
        raise ShareDataQualityError(f"Duplicate or unverified share overrides: {path}")
    if frame.evidence.isna().any() or pd.to_numeric(frame.shares, errors="coerce").isna().any():
        raise ShareDataQualityError(f"Missing evidence or shares: {path}")
    return frame


def normalize_fetched_shares(rows: list[dict], overrides: pd.DataFrame, *, request: dict | None = None) -> pd.DataFrame:
    frame = normalize_etf_share_frame(pd.DataFrame(rows).assign(source="tushare:fund_share", share_unit=ACTUAL_SHARE_UNIT))
    # Known old SZ money rows have BOTH unit and effective-date discrepancies.
    # Never fix them with a blanket multiplier or a guessed date shift.
    historical = frame.fund_code.isin(SZ_MONEY_CODES) & frame.trade_date.le(VERIFIED_HISTORY_END)
    if historical.any():
        replacement = overrides.set_index(["fund_code", "trade_date"]) if not overrides.empty else pd.DataFrame()
        keys = pd.MultiIndex.from_frame(frame.loc[historical, ["fund_code", "trade_date"]])
        if replacement.empty or not keys.isin(replacement.index).all():
            raise ShareDataQualityError("Historical SZ money ETF shares require verified overrides; source units/dates are unreliable")
        chosen = replacement.loc[keys]
        for column in ["shares", "source", "share_unit"]:
            frame.loc[historical, column] = chosen[column].to_numpy()
    # Some historical responses omit these funds entirely. Evidence must also
    # supply absent rows, otherwise a valid override silently becomes zero flow.
    if not overrides.empty:
        verified = overrides.loc[overrides.fund_code.isin(SZ_MONEY_CODES) & overrides.trade_date.le(VERIFIED_HISTORY_END)]
        params = request or {}
        if params.get("ts_code"):
            verified = verified.loc[verified.fund_code.eq(params["ts_code"])]
        if params.get("trade_date"):
            verified = verified.loc[verified.trade_date.eq(pd.Timestamp(params["trade_date"]))]
        elif params.get("start_date") and params.get("end_date"):
            verified = verified.loc[verified.trade_date.between(pd.Timestamp(params["start_date"]), pd.Timestamp(params["end_date"]))]
        else:
            verified = verified.loc[verified.trade_date.isin(frame.trade_date)]
        existing = pd.MultiIndex.from_frame(frame[["fund_code", "trade_date"]])
        missing = ~pd.MultiIndex.from_frame(verified[["fund_code", "trade_date"]]).isin(existing)
        if missing.any():
            frame = normalize_etf_share_frame(pd.concat([frame, verified.loc[missing, frame.columns]], ignore_index=True))
    return frame
