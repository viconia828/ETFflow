"""Official exchange announcement clients for ETF lifecycle review."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

import pandas as pd


SSE_ANNOUNCEMENT_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_ANNOUNCEMENT_REFERER = "https://www.sse.com.cn/disclosure/fund/announcement/"
SSE_FILE_BASE_URL = "https://www.sse.com.cn"

SZSE_ANNOUNCEMENT_URL = "https://www.szse.cn/api/disc/announcement/annList"
SZSE_ANNOUNCEMENT_REFERER = "https://www.szse.cn/disclosure/fund/etf/index.html"
SZSE_FILE_BASE_URL = "https://disc.static.szse.cn/download"


@dataclass(slots=True)
class ExchangeAnnouncementClient:
    """Fetch announcement lists from SSE/SZSE with browser-like request headers."""

    timeout_seconds: int = 20
    page_size: int = 50
    sleep_seconds: float = 1.0
    ignore_proxy: bool = True
    _opener: object = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.timeout_seconds = max(int(self.timeout_seconds), 1)
        self.page_size = max(int(self.page_size), 1)
        self.sleep_seconds = max(float(self.sleep_seconds), 0.0)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.ignore_proxy else None

    def fetch(self, fund_code: str, *, start_date: object, end_date: object) -> list[dict[str, object]]:
        code = str(fund_code or "").strip().upper()
        if code.endswith(".SH"):
            return self.fetch_sse(code, start_date=start_date, end_date=end_date)
        if code.endswith(".SZ"):
            return self.fetch_szse(code, start_date=start_date, end_date=end_date)
        raise ValueError(f"unsupported exchange fund code: {fund_code}")

    def fetch_sse(self, fund_code: str, *, start_date: object, end_date: object) -> list[dict[str, object]]:
        plain_code = _plain_code(fund_code)
        rows: list[dict[str, object]] = []
        page_no = 1
        while True:
            payload = {
                "jsonCallBack": "jsonpCallback",
                "isPagination": "true",
                "pageHelp.pageSize": str(self.page_size),
                "pageHelp.pageNo": str(page_no),
                "pageHelp.beginPage": str(page_no),
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": str(page_no),
                "type": "inParams",
                "sqlId": "COMMON_PL_JJXX_JJGG_NEW_L",
                "TITLE": "",
                "SECURITY_CODE": plain_code,
                "BULLETIN_TYPE": "",
                "START_DATE": _date_hyphen(start_date),
                "END_DATE": _date_hyphen(end_date),
                "DATE_DESC": "1",
                "DATE_ASC": "",
                "CODE_DESC": "",
                "CODE_ASC": "",
            }
            url = f"{SSE_ANNOUNCEMENT_URL}?{urllib.parse.urlencode(payload)}"
            request = urllib.request.Request(url, headers=_sse_headers(), method="GET")
            parsed = _parse_json_or_jsonp(self._read_text(request))
            data = list(parsed.get("result") or parsed.get("pageHelp", {}).get("data") or [])
            rows.extend(normalize_sse_rows(data, fallback_fund_code=fund_code))
            page_count = int((parsed.get("pageHelp") or {}).get("pageCount") or 0)
            if page_no >= page_count or not data:
                break
            page_no += 1
            self._sleep_between_pages()
        return rows

    def fetch_szse(self, fund_code: str, *, start_date: object, end_date: object) -> list[dict[str, object]]:
        plain_code = _plain_code(fund_code)
        rows: list[dict[str, object]] = []
        page_no = 1
        while True:
            payload = {
                "seDate": [_date_hyphen(start_date), _date_hyphen(end_date)],
                "stock": [plain_code],
                "channelCode": ["etfNotice_disc"],
                "pageSize": self.page_size,
                "pageNum": page_no,
            }
            request = urllib.request.Request(
                SZSE_ANNOUNCEMENT_URL,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=_szse_headers(),
                method="POST",
            )
            parsed = json.loads(self._read_text(request).lstrip("\ufeff"))
            data = list(parsed.get("data") or [])
            rows.extend(normalize_szse_rows(data, fallback_fund_code=fund_code))
            total = int(parsed.get("announceCount") or parsed.get("recordCount") or len(data) or 0)
            if page_no * self.page_size >= total or not data:
                break
            page_no += 1
            self._sleep_between_pages()
        return rows

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


def normalize_sse_rows(rows: Iterable[dict[str, object]], *, fallback_fund_code: str = "") -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in rows:
        plain_code = str(row.get("SECURITY_CODE") or _plain_code(fallback_fund_code)).strip()
        if not plain_code:
            continue
        url = _absolute_url(str(row.get("URL") or ""), base_url=SSE_FILE_BASE_URL)
        bulletin_type = " ".join(
            value
            for value in (
                str(row.get("BULLETIN_TYPE_DESC") or "").strip(),
                str(row.get("ORG_BULLETIN_TYPE_DESC") or "").strip(),
            )
            if value
        )
        output.append(
            {
                "fund_code": f"{plain_code}.SH",
                "announcement_date": _date_compact(row.get("SSEDATE")),
                "event_date": "",
                "title": str(row.get("TITLE") or "").strip(),
                "content": bulletin_type,
                "source_url": url,
            }
        )
    return output


def normalize_szse_rows(rows: Iterable[dict[str, object]], *, fallback_fund_code: str = "") -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in rows:
        sec_codes = row.get("secCode")
        if isinstance(sec_codes, list):
            plain_code = str(sec_codes[0] if sec_codes else _plain_code(fallback_fund_code)).strip()
        else:
            plain_code = str(sec_codes or _plain_code(fallback_fund_code)).strip()
        if not plain_code:
            continue
        output.append(
            {
                "fund_code": f"{plain_code}.SZ",
                "announcement_date": _date_compact(row.get("publishTime")),
                "event_date": "",
                "title": str(row.get("title") or "").strip(),
                "content": str(row.get("bigCategoryId") or row.get("smallCategoryId") or "").strip(),
                "source_url": _szse_source_url(str(row.get("attachPath") or ""), str(row.get("attachFormat") or "")),
            }
        )
    return output


def _sse_headers() -> dict[str, str]:
    return {
        "Accept": "application/javascript, application/json, text/javascript, */*; q=0.01",
        "Referer": SSE_ANNOUNCEMENT_REFERER,
        "User-Agent": "Mozilla/5.0",
    }


def _szse_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.szse.cn",
        "Referer": SZSE_ANNOUNCEMENT_REFERER,
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }


def _parse_json_or_jsonp(text: str) -> dict:
    raw = str(text or "").strip()
    match = re.match(r"^[\w$]+\((.*)\)\s*;?\s*$", raw, flags=re.S)
    if match:
        raw = match.group(1)
    parsed = json.loads(raw.lstrip("\ufeff"))
    if not isinstance(parsed, dict):
        raise RuntimeError("exchange announcement response format error")
    return parsed


def _plain_code(fund_code: str) -> str:
    return str(fund_code or "").strip().upper().split(".", 1)[0]


def _date_hyphen(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y-%m-%d")


def _date_compact(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y%m%d")


def _absolute_url(value: str, *, base_url: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if not text.startswith("/"):
        text = f"/{text}"
    return f"{base_url}{text}"


def _szse_source_url(attach_path: str, attach_format: str) -> str:
    path = str(attach_path or "").strip()
    if not path:
        return ""
    if str(attach_format or "").strip().lower() == "link" or path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{SZSE_FILE_BASE_URL}{path}"
