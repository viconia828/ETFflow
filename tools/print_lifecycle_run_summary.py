"""Print a concise lifecycle announcement/audit run summary for the BAT window."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.utils.io import read_user_csv  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print concise lifecycle run summary.")
    parser.add_argument("--config", default="config.txt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    output_dir = config.output_dir / "lifecycle_audit"
    ann_summary = _read_latest_json(output_dir, "announcement_update_*.json", prefer_non_skipped=True)
    life_summary = _read_latest_json(output_dir, "etf_lifecycle_share_jump_audit_*.json")
    local_announcement_rows = _csv_row_count(config.announcement_file_path)
    current_pending_rows = _csv_row_count(config.lifecycle_pending_confirmations_path)

    if ann_summary:
        jobs = _as_int(ann_summary.get("jobs") or ann_summary.get("codes"))
        failed = _as_int(ann_summary.get("failed_jobs") or ann_summary.get("errors"))
        completed = _as_int(ann_summary.get("completed_jobs"))
        if completed == 0 and jobs:
            completed = max(jobs - failed, 0)
        label = "未全部完成" if failed else "完成"
        print(
            f"[summary] 公告抓取{label}：任务 {jobs}，完成 {completed}，失败 {failed}，"
            f"本次抓到公告 {_as_int(ann_summary.get('fresh_rows'))} 条，"
            f"本地公告表 {local_announcement_rows} 条，"
            f"当前待重抓/核对 {current_pending_rows} 条。",
            flush=True,
        )
        follow_up_jobs = _as_int(ann_summary.get("liquidation_follow_up_jobs"))
        if follow_up_jobs:
            print(
                f"[summary] 清盘后探测：任务 {follow_up_jobs}，"
                f"抓到公告 {_as_int(ann_summary.get('liquidation_follow_up_rows'))} 条，"
                f"失败 {_as_int(ann_summary.get('liquidation_follow_up_errors'))} 条。",
                flush=True,
            )
    else:
        print(f"[summary] 公告抓取：未找到本轮摘要；本地公告表 {local_announcement_rows} 条。", flush=True)

    if life_summary:
        matched = _as_int(life_summary.get("matched_share_jump_rows")) + _as_int(life_summary.get("manual_confirmed_share_jump_rows"))
        high_pending = _as_int(life_summary.get("request_plan_rows"))
        print(
            f"[summary] 生命周期审计：已审计跳变 {_as_int(life_summary.get('share_jump_rows'))} 条，"
            f"已处理 {matched} 条，高疑似待抓 {high_pending} 条，"
            f"低疑似观察 {_as_int(life_summary.get('observation_plan_rows'))} 条。",
            flush=True,
        )
        if high_pending:
            print("[summary] 提醒：仍有高疑似跳变待抓/待核对，下次运行会继续处理。", flush=True)
    else:
        print("[summary] 生命周期审计：未找到审计摘要。", flush=True)
    return 0


def _read_latest_json(directory: Path, pattern: str, *, prefer_non_skipped: bool = False) -> dict[str, object]:
    paths = [path for path in directory.glob(pattern) if path.is_file()]
    if not paths:
        return {}
    latest_payload: dict[str, object] = {}
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if not latest_payload:
            latest_payload = payload
        if prefer_non_skipped and str(payload.get("skip_reason") or "").strip():
            continue
        return payload
    return latest_payload


def _csv_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(len(read_user_csv(path)))
    except Exception:  # noqa: BLE001
        return 0


def _as_int(value: object) -> int:
    try:
        if pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
