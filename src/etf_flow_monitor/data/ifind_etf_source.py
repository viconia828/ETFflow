"""Fetch date-labelled ETF listed shares from the iFinD HTTP API."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.schemas import ACTUAL_SHARE_UNIT, normalize_etf_daily_frame, normalize_etf_share_frame


API_ROOT = "https://quantapi.51ifind.com/api/v1/"
BATCH_SIZE = 25
QUOTE_FIELDS = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "preClose": "pre_close", "change": "price_change", "changeRatio": "pct_change",
    "volume": "volume", "amount": "amount",
}


class IFindDateUnavailableError(RuntimeError):
    """The response has share fields, but none for the requested day."""


def _post(endpoint: str, headers: dict, body: dict | None = None) -> dict:
    request = Request(
        API_ROOT + endpoint,
        data=json.dumps(body).encode("utf-8") if body is not None else b"",
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"iFinD {endpoint}: HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f"iFinD {endpoint}: request or JSON decoding failed") from None


class IFindEtfSource:
    def __init__(self, cache: CacheStore) -> None:
        self.cache = cache
        refresh_token = os.getenv("IFIND_REFRESH_TOKEN", "").strip()
        if not refresh_token:
            raise RuntimeError("IFIND_REFRESH_TOKEN is missing")
        payload = _post("get_access_token", {"refresh_token": refresh_token})
        self.access_token = (payload.get("data") or {}).get("access_token")
        if payload.get("errorcode") != 0 or not self.access_token:
            raise RuntimeError(f"iFinD token exchange failed (code {payload.get('errorcode')})")

    def get_listed_shares(self, codes: list[str], trade_date: str) -> dict[str, float]:
        """Return exact-date shares in individual units for one requested batch."""
        endpoint = "smart_stock_picking"
        date_text = f"{trade_date[:4]}年{trade_date[4:6]}月{trade_date[6:]}日"
        body = {
            "searchtype": "fund",
            "searchstring": f"{'、'.join(codes)}，{date_text}场内流通份额（单位：份）",
        }
        payload = self._request_data(endpoint, trade_date, body)
        if payload is None:
            return {}
        return _listed_shares(payload, codes, trade_date)

    def fetch_snapshot(
        self, dataset: str, codes: list[str], trade_date: str, *, progress: Callable[[int], None],
    ) -> pd.DataFrame:
        rows = []
        date = pd.Timestamp(trade_date)
        for start in range(0, len(codes), BATCH_SIZE):
            progress(start)
            batch = codes[start:start + BATCH_SIZE]
            if dataset == "etf_daily":
                rows.extend(self._daily_quotes(batch, trade_date))
                continue
            try:
                shares = self.get_listed_shares(batch, trade_date)
            except IFindDateUnavailableError:
                continue
            rows.extend(
                {"fund_code": code, "trade_date": date, "shares": value / 10000,
                 "source": "ifind:listed_shares", "share_unit": ACTUAL_SHARE_UNIT}
                for code, value in shares.items()
            )
        normalize = normalize_etf_daily_frame if dataset == "etf_daily" else normalize_etf_share_frame
        return normalize(pd.DataFrame(rows))

    def _daily_quotes(self, codes: list[str], trade_date: str) -> list[dict]:
        date = pd.Timestamp(trade_date).date().isoformat()
        body = {
            "codes": ",".join(codes),
            "indicators": ",".join(QUOTE_FIELDS),
            "startdate": date,
            "enddate": date,
            "functionpara": {"Fill": "Omit", "CPS": "1"},
        }
        payload = self._request_data("cmd_history_quotation", trade_date, body)
        return _quote_rows(payload, codes, date) if payload is not None else []

    def _request_data(self, endpoint: str, trade_date: str, body: dict) -> dict | None:
        payload = _post(endpoint, {"access_token": self.access_token, "ifindlang": "cn"}, body)
        self.cache.save_source_response("ifind", endpoint, trade_date, body, [payload])
        if payload.get("errorcode") == -4001:
            return None
        if payload.get("errorcode") != 0:
            raise RuntimeError(f"iFinD {endpoint}: error code {payload.get('errorcode')}")
        return payload


def _quote_rows(payload: dict, codes: list[str], date: str) -> list[dict]:
    rows = []
    requested = set(codes)
    for section in payload["tables"]:
        code = section["thscode"]
        if code not in requested or date not in section.get("time", []):
            continue
        index = section["time"].index(date)
        table = section["table"]
        row = {target: table[field][index] for field, target in QUOTE_FIELDS.items()}
        if row["close"] is None or row["amount"] is None or row["volume"] is None:
            continue
        # Report cache expects Tushare's lots and thousand yuan.
        row["volume"] /= 100
        row["amount"] /= 1000
        rows.append({**row, "fund_code": code, "trade_date": date})
    return rows


def _listed_shares(payload: dict, codes: list[str], trade_date: str) -> dict[str, float]:
    requested = set(codes)
    field = f"基金@场内流通份额[{trade_date}]"
    shares = {}
    returned_fields = set()
    for section in payload.get("tables", []):
        table = section.get("table", {})
        returned_fields.update(name for name in table if name.startswith("基金@场内流通份额["))
        if field not in table:
            continue
        for code, value in zip(table["基金代码"], table[field], strict=True):
            if code not in requested or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                if code in shares and shares[code] != value:
                    raise RuntimeError(f"iFinD returned conflicting shares for {code}")
                shares[code] = value
    if field not in returned_fields:
        if returned_fields:
            raise IFindDateUnavailableError(f"Requested {trade_date}, returned {', '.join(sorted(returned_fields))}")
        raise RuntimeError("iFinD response is missing the listed-share field")
    return shares
