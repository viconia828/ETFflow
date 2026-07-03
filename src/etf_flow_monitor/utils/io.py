"""Filesystem and DataFrame helpers shared by the starter project."""

from __future__ import annotations

import json
import re
from pathlib import Path
from datetime import date, datetime
from typing import Any, Iterable, Mapping

import pandas as pd


USER_CSV_ENCODING = "utf-8-sig"
USER_CSV_FALLBACK_ENCODINGS = ("gb18030",)
_BLANK_TEXT_VALUES = {"", "nan", "nat", "none", "null", "#n/a", "na"}


def read_user_csv(path: Path, *, dtype: object = str) -> pd.DataFrame:
    """Read a user-editable CSV with Excel-friendly defaults."""
    try:
        return pd.read_csv(path, encoding=USER_CSV_ENCODING, dtype=dtype, keep_default_na=False)
    except UnicodeDecodeError:
        for encoding in USER_CSV_FALLBACK_ENCODINGS:
            try:
                return pd.read_csv(path, encoding=encoding, dtype=dtype, keep_default_na=False)
            except UnicodeDecodeError:
                continue
        raise


def write_user_csv(path: Path, frame: pd.DataFrame) -> None:
    """Write a user-editable CSV so Excel recognizes Chinese text correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding=USER_CSV_ENCODING, lineterminator="\n")


def clean_excel_text(value: object) -> str:
    """Normalize text cells that Excel may have saved with quotes/formulas."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in _BLANK_TEXT_VALUES:
        return ""
    if text.startswith("="):
        text = text.lstrip("=").strip()
    if (len(text) >= 2) and ((text[0], text[-1]) in {('"', '"'), ("'", "'")}):
        text = text[1:-1].strip()
    if text.startswith("'"):
        text = text[1:].strip()
    return text


def normalize_date(value: object) -> pd.Timestamp:
    """Normalize date-like input into a midnight pandas timestamp."""
    parsed = parse_excel_friendly_date(value)
    if pd.isna(parsed):
        raise ValueError(f"cannot parse date value: {value!r}")
    return pd.Timestamp(parsed).normalize()


def format_tushare_date(value: object) -> str:
    """Format a date-like value as Tushare's YYYYMMDD string."""
    parsed = parse_excel_friendly_date(value)
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).normalize().strftime("%Y%m%d")


def parse_excel_friendly_date(value: object) -> pd.Timestamp:
    """Parse dates that may have been opened and re-saved by Excel.

    CSV users commonly turn YYYYMMDD strings into numbers, YYYY/M/D text, or
    Excel serial dates. Prefer explicit YYYYMMDD before considering serials.
    """
    if value is None:
        return pd.NaT
    if isinstance(value, pd.Timestamp):
        return value.normalize() if pd.notna(value) else pd.NaT
    try:
        if pd.isna(value):
            return pd.NaT
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return pd.Timestamp(value).normalize()

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if pd.isna(value):
            return pd.NaT
        number = float(value)
        if number.is_integer():
            text = str(int(number))
            parsed = _parse_compact_yyyymmdd(text)
            if pd.notna(parsed):
                return parsed
        if 1 <= number <= 80000:
            return _normalize_parsed_date(pd.to_datetime(number, unit="D", origin="1899-12-30", errors="coerce"))
        return _normalize_parsed_date(pd.to_datetime(value, errors="coerce"))

    text = clean_excel_text(value)
    if not text or text.lower() in _BLANK_TEXT_VALUES:
        return pd.NaT
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    parsed = _parse_compact_yyyymmdd(text)
    if pd.notna(parsed):
        return parsed
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
        return _normalize_parsed_date(pd.to_datetime(text.replace("/", "-"), format="%Y-%m-%d", errors="coerce"))
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        if 1 <= number <= 80000:
            return _normalize_parsed_date(pd.to_datetime(number, unit="D", origin="1899-12-30", errors="coerce"))
    return _normalize_parsed_date(pd.to_datetime(text, errors="coerce"))


def parse_excel_friendly_date_series(values: pd.Series) -> pd.Series:
    return values.map(parse_excel_friendly_date)


def _parse_compact_yyyymmdd(text: str) -> pd.Timestamp:
    if not re.fullmatch(r"\d{8}", str(text or "")):
        return pd.NaT
    return _normalize_parsed_date(pd.to_datetime(str(text), format="%Y%m%d", errors="coerce"))


def _normalize_parsed_date(value: object) -> pd.Timestamp:
    if pd.isna(value):
        return pd.NaT
    return pd.Timestamp(value).normalize()


def ensure_list(values: str | Iterable[str]) -> list[str]:
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values]


def safe_filename(value: str) -> str:
    """Turn a code-like string into a filesystem-safe filename stem."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))


def merge_frames(
    existing: pd.DataFrame | None,
    fresh: pd.DataFrame | None,
    *,
    key_columns: tuple[str, ...],
    sort_columns: tuple[str, ...],
) -> pd.DataFrame:
    """Merge cached and fresh frames, keeping the newest row per key."""
    frames = [frame for frame in (existing, fresh) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if key_columns:
        merged = merged.drop_duplicates(subset=list(key_columns), keep="last")
    if sort_columns:
        merged = merged.sort_values(list(sort_columns), kind="stable")
    return merged.reset_index(drop=True)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}
