"""CNINFO announcement client for ETF lifecycle review."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import pandas as pd


CNINFO_FUND_STOCK_URL = "https://www.cninfo.com.cn/new/data/fund_stock.json"
CNINFO_ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_REFERER = "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
CNINFO_FILE_BASE_URL = "https://static.cninfo.com.cn"


@dataclass(slots=True)
class CninfoAnnouncementClient:
    """Fetch fund announcements from CNINFO's public fund disclosure search."""

    timeout_seconds: int = 20
    page_size: int = 30
    sleep_seconds: float = 1.0
    ignore_proxy: bool = True
    _opener: object = field(init=False, repr=False)
    _fund_stock_by_code: dict[str, dict[str, object]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.timeout_seconds = max(int(self.timeout_seconds), 1)
        self.page_size = max(int(self.page_size), 1)
        self.sleep_seconds = max(float(self.sleep_seconds), 0.0)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.ignore_proxy else None

    def fetch(self, fund_code: str, *, start_date: object, end_date: object) -> list[dict[str, object]]:
        plain_code = _plain_code(fund_code)
        if not plain_code:
            raise ValueError(f"unsupported CNINFO fund code: {fund_code}")
        stock_param = self._stock_param(plain_code)
        rows: list[dict[str, object]] = []
        page_no = 1
        while True:
            payload = {
                "pageNum": str(page_no),
                "pageSize": str(self.page_size),
                "column": "fund",
                "tabName": "fulltext",
                "plate": "",
                "stock": stock_param,
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{_date_hyphen(start_date)}~{_date_hyphen(end_date)}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            request = urllib.request.Request(
                CNINFO_ANNOUNCEMENT_URL,
                data=urllib.parse.urlencode(payload).encode("utf-8"),
                headers=_cninfo_headers(content_type="application/x-www-form-urlencoded; charset=UTF-8"),
                method="POST",
            )
            parsed = json.loads(self._read_text(request).lstrip("\ufeff"))
            data = list(parsed.get("announcements") or [])
            rows.extend(normalize_cninfo_rows(data, fallback_fund_code=fund_code))
            total_records = int(parsed.get("totalRecordNum") or len(data) or 0)
            total_pages = int(parsed.get("totalpages") or 0)
            if not data:
                break
            if total_pages and page_no >= total_pages:
                break
            if not total_pages and page_no * self.page_size >= total_records:
                break
            page_no += 1
            self._sleep_between_pages()
        return rows

    def _stock_param(self, plain_code: str) -> str:
        item = self._load_fund_stock_by_code().get(str(plain_code))
        org_id = str((item or {}).get("orgId") or "").strip()
        return f"{plain_code},{org_id}" if org_id else plain_code

    def _load_fund_stock_by_code(self) -> dict[str, dict[str, object]]:
        if self._fund_stock_by_code is not None:
            return self._fund_stock_by_code
        request = urllib.request.Request(CNINFO_FUND_STOCK_URL, headers=_cninfo_headers(), method="GET")
        parsed = json.loads(self._read_text(request).lstrip("\ufeff"))
        rows = parsed.get("stockList") if isinstance(parsed, dict) else []
        mapping: dict[str, dict[str, object]] = {}
        for row in rows or []:
            code = str(row.get("code") or "").strip()
            if code:
                mapping[code] = dict(row)
        self._fund_stock_by_code = mapping
        return mapping

    def _read_text(self, request: urllib.request.Request) -> str:
        if self._opener is not None:
            response_context = self._opener.open(request, timeout=self.timeout_seconds)
        else:
            response_context = urllib.request.urlopen(request, timeout=self.timeout_seconds)
        with response_context as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    def _sleep_between_pages(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)


def normalize_cninfo_rows(rows: Iterable[dict[str, object]], *, fallback_fund_code: str = "") -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in rows:
        plain_code = str(row.get("secCode") or _plain_code(fallback_fund_code)).strip()
        if not plain_code:
            continue
        title = str(row.get("announcementTitle") or row.get("shortTitle") or "").strip()
        output.append(
            {
                "fund_code": _fund_code_with_suffix(plain_code, fallback_fund_code),
                "announcement_date": _cninfo_date(row.get("announcementTime")),
                "event_date": "",
                "title": title,
                "content": str(row.get("announcementTypeName") or row.get("announcementContent") or "").strip(),
                "source_url": _cninfo_source_url(str(row.get("adjunctUrl") or "")),
            }
        )
    return output


def _cninfo_headers(*, content_type: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://www.cninfo.com.cn",
        "Referer": CNINFO_REFERER,
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _plain_code(fund_code: str) -> str:
    return str(fund_code or "").strip().upper().split(".", 1)[0]


def _fund_code_with_suffix(plain_code: str, fallback_fund_code: str) -> str:
    fallback = str(fallback_fund_code or "").strip().upper()
    if fallback.endswith((".SH", ".SZ")):
        return f"{plain_code}{fallback[-3:]}"
    if str(plain_code).startswith(("15", "16", "18")):
        return f"{plain_code}.SZ"
    return f"{plain_code}.SH"


def _date_hyphen(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y-%m-%d")


def _cninfo_date(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y%m%d")
    if isinstance(value, (int, float)) and float(value) > 10_000_000_000:
        parsed = pd.to_datetime(value, unit="ms", utc=True, errors="coerce")
        if pd.notna(parsed):
            parsed = parsed.tz_convert("Asia/Shanghai")
    else:
        parsed = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y%m%d")


def _cninfo_source_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        text = text[1:]
    return f"{CNINFO_FILE_BASE_URL}/{text}"
