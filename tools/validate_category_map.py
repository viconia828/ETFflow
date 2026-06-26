"""Validate the editable ETF category map."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import EXPECTED_CATEGORIES, normalize_category_map  # noqa: E402
from etf_flow_monitor.utils.io import write_json  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate ETF category map CSV.")
    parser.add_argument("--config", default="config.example.txt")
    parser.add_argument("--category-map", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when pending manual-review rows exist.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    map_path = Path(args.category_map or config.category_map_path)
    if not map_path.is_absolute():
        map_path = PROJECT_ROOT / map_path
    output_path = _output_path(args.output)

    payload = validate_category_map(map_path)
    payload["config_path"] = str(config_path)
    payload["category_map_path"] = str(map_path)
    payload["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(output_path, payload)

    blocking_count = int(payload["summary"]["blocking_issue_count"])
    pending_count = int(payload["summary"]["pending_review_count"])
    status = "failed" if blocking_count or (args.strict and pending_count) else "success"
    payload["status"] = status
    write_json(output_path, payload)

    print(f"status: {status}")
    print(f"rows: {payload['summary']['rows']}")
    print(f"blocking_issues: {blocking_count}")
    print(f"pending_review: {pending_count}")
    print(f"output: {output_path}")
    return 1 if status == "failed" else 0


def validate_category_map(path: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not path.exists():
        issues.append({"kind": "missing_file", "message": f"category map not found: {path}"})
        return _payload(pd.DataFrame(), issues, warnings)

    raw = pd.read_csv(path, encoding="utf-8-sig")
    required_columns = {"fund_code", "category", "subcategory", "review_note"}
    missing_columns = sorted(required_columns - set(raw.columns))
    for column in missing_columns:
        issues.append({"kind": "missing_column", "column": column})

    raw_codes = raw["fund_code"].fillna("").astype(str).str.strip().str.upper() if "fund_code" in raw.columns else pd.Series(dtype="string")
    duplicated = raw_codes.loc[raw_codes.ne("") & raw_codes.duplicated(keep=False)].drop_duplicates().tolist()
    if duplicated:
        issues.append({"kind": "duplicate_fund_code", "count": len(duplicated), "sample": duplicated[:20]})

    normalized = normalize_category_map(raw)

    empty_category = normalized.loc[normalized["category"].eq(""), "fund_code"].tolist()
    if empty_category:
        issues.append({"kind": "empty_category", "count": len(empty_category), "sample": empty_category[:20]})

    empty_subcategory = normalized.loc[normalized["subcategory"].eq(""), "fund_code"].tolist()
    if empty_subcategory:
        issues.append({"kind": "empty_subcategory", "count": len(empty_subcategory), "sample": empty_subcategory[:20]})

    unexpected = sorted(set(normalized["category"]) - EXPECTED_CATEGORIES - {""})
    if unexpected:
        issues.append({"kind": "unexpected_category", "count": len(unexpected), "values": unexpected})

    bad_code = normalized.loc[~normalized["fund_code"].str.match(r"^\d{6}\.(SH|SZ|BJ)$", na=False), "fund_code"].tolist()
    if bad_code:
        warnings.append({"kind": "unusual_fund_code", "count": len(bad_code), "sample": bad_code[:20]})

    pending = normalized.loc[normalized["category"].eq("其他") | normalized["subcategory"].eq("待人工确认")].copy()
    if not pending.empty:
        warnings.append({"kind": "pending_manual_review", "count": int(len(pending)), "sample": pending["fund_code"].head(30).tolist()})

    return _payload(normalized, issues, warnings)


def _payload(frame: pd.DataFrame, issues: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    categories = {}
    if frame is not None and not frame.empty and "category" in frame.columns:
        categories = {str(key): int(value) for key, value in frame["category"].value_counts(dropna=False).sort_index().items()}
    pending_count = 0
    if frame is not None and not frame.empty:
        pending_count = int((frame["category"].eq("其他") | frame["subcategory"].eq("待人工确认")).sum())
    return {
        "schema_version": "category_map_validation_v1",
        "status": "pending",
        "summary": {
            "rows": int(0 if frame is None else len(frame)),
            "unique_fund_codes": int(0 if frame is None or frame.empty else frame["fund_code"].nunique()),
            "blocking_issue_count": len(issues),
            "warning_count": len(warnings),
            "pending_review_count": pending_count,
            "category_counts": categories,
        },
        "issues": issues,
        "warnings": warnings,
    }


def _output_path(raw_path: str) -> Path:
    if raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else PROJECT_ROOT / path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "outputs" / "category_map_validation" / f"category_map_validation_{stamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
