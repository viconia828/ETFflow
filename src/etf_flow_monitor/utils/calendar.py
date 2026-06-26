"""Trading-calendar utilities for daily monitor jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FALLBACK_CALENDAR_EXCHANGES = {"BIZ"}


def get_shanghai_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def current_shanghai_date() -> date:
    return get_shanghai_now().date()


def normalize_date_input(value: Any, field_name: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be empty")
        fmt = "%Y-%m-%d" if "-" in text else "%Y%m%d"
        return datetime.strptime(text, fmt).date()
    raise TypeError(f"{field_name} only supports date / datetime / YYYY-MM-DD / YYYYMMDD")


def parse_optional_date(value: Any) -> date | None:
    if value in (None, "", "--"):
        return None
    return normalize_date_input(str(value).split(" ", 1)[0], field_name="optional_date")


def format_trade_date(value: Any) -> str:
    return normalize_date_input(value).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class TradeCalendarRow:
    exchange: str
    cal_date: date
    is_open: bool
    pretrade_date: date | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], *, exchange: str = "SSE") -> "TradeCalendarRow":
        current_exchange = str(payload.get("exchange") or exchange).strip().upper() or "SSE"
        cal_date = parse_optional_date(payload.get("cal_date"))
        if cal_date is None:
            raise ValueError("trade calendar row missing cal_date")
        is_open = bool(int(payload.get("is_open") or 0))
        pretrade_date = parse_optional_date(payload.get("pretrade_date"))
        return cls(exchange=current_exchange, cal_date=cal_date, is_open=is_open, pretrade_date=pretrade_date)


def ensure_official_calendar_rows(
    rows: Iterable[TradeCalendarRow],
    *,
    expected_exchange: str | None = None,
) -> tuple[TradeCalendarRow, ...]:
    ordered_rows = tuple(sorted(rows, key=lambda item: item.cal_date))
    if not ordered_rows:
        raise ValueError("TradingCalendar requires at least one calendar row")
    invalid_exchanges = sorted(
        {
            str(row.exchange or "").strip().upper()
            for row in ordered_rows
            if str(row.exchange or "").strip().upper() in FALLBACK_CALENDAR_EXCHANGES
        }
    )
    if invalid_exchanges:
        joined = ", ".join(invalid_exchanges)
        raise ValueError(f"Official trading calendar required; fallback business-day rows are not allowed: {joined}")
    normalized_expected = str(expected_exchange or "").strip().upper()
    if normalized_expected:
        mismatched = sorted(
            {
                str(row.exchange or "").strip().upper()
                for row in ordered_rows
                if str(row.exchange or "").strip().upper()
                and str(row.exchange or "").strip().upper() != normalized_expected
            }
        )
        if mismatched:
            joined = ", ".join(mismatched)
            raise ValueError(f"Trading calendar exchange mismatch: expected={normalized_expected}, actual={joined}")
    open_dates = [row.cal_date for row in ordered_rows if row.is_open]
    if not open_dates:
        raise ValueError("TradingCalendar requires at least one open trading day")
    return ordered_rows


class TradingCalendar:
    """Pure in-memory trading calendar with previous-close semantics."""

    def __init__(self, rows: Iterable[TradeCalendarRow]):
        ordered_rows = ensure_official_calendar_rows(rows)
        self._rows = tuple(ordered_rows)
        self._row_map = {row.cal_date: row for row in self._rows}
        self._open_dates = tuple(row.cal_date for row in self._rows if row.is_open)

    @property
    def rows(self) -> tuple[TradeCalendarRow, ...]:
        return self._rows

    @property
    def open_dates(self) -> tuple[date, ...]:
        return self._open_dates

    def get_trading_days(self, start: date, end: date) -> list[date]:
        start_date = normalize_date_input(start, field_name="start")
        end_date = normalize_date_input(end, field_name="end")
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        return [item for item in self._open_dates if start_date <= item <= end_date]

    def resolve_request_date_market_date(self, calc_date: date) -> date:
        current_date = normalize_date_input(calc_date, field_name="calc_date")
        row = self._row_map.get(current_date)
        if row is not None:
            if row.is_open:
                return current_date
            if row.pretrade_date is not None:
                return row.pretrade_date
        previous_open = [item for item in self._open_dates if item <= current_date]
        if not previous_open:
            raise ValueError(f"Cannot resolve usable market date for {current_date.isoformat()}")
        return previous_open[-1]

    def resolve_market_date(self, calc_date: date) -> date:
        current_date = normalize_date_input(calc_date, field_name="calc_date")
        request_market_date = self.resolve_request_date_market_date(current_date)
        if request_market_date == current_date:
            return self.shift_trade_date(request_market_date, -1)
        return request_market_date

    def shift_trade_date(self, base_date: date, offset: int) -> date:
        base = normalize_date_input(base_date, field_name="base_date")
        if offset == 0:
            return self.resolve_request_date_market_date(base)
        anchor_candidates = [idx for idx, current in enumerate(self._open_dates) if current <= base]
        if not anchor_candidates:
            raise ValueError(f"Cannot compute trade-date shift for base={base.isoformat()}")
        target_index = anchor_candidates[-1] + int(offset)
        if target_index < 0 or target_index >= len(self._open_dates):
            raise ValueError(f"Trade-date shift out of range: base={base.isoformat()} offset={offset}")
        return self._open_dates[target_index]

    def get_previous_trading_day(self, reference_date: date) -> date:
        return self.shift_trade_date(reference_date, -1)


def trading_calendar_from_frame(frame: pd.DataFrame, *, exchange: str = "SSE") -> TradingCalendar:
    """Build an official trading calendar from a normalized calendar DataFrame."""

    if frame is None or frame.empty:
        raise ValueError("Trading calendar frame is empty")
    rows = [
        TradeCalendarRow.from_mapping(
            {
                "exchange": row.get("exchange", exchange),
                "cal_date": row.get("cal_date"),
                "is_open": row.get("is_open"),
                "pretrade_date": row.get("pretrade_date"),
            },
            exchange=exchange,
        )
        for _, row in frame.iterrows()
    ]
    return TradingCalendar(ensure_official_calendar_rows(rows, expected_exchange=exchange))
