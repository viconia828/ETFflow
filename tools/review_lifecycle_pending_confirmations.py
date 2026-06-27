"""Review no-announcement lifecycle jump confirmations before refetching."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.lifecycle import (  # noqa: E402
    MANUAL_CONFIRMATION_COLUMNS,
    empty_manual_confirmations,
    empty_pending_confirmations,
    normalize_manual_confirmations,
    normalize_pending_confirmations,
    prepare_for_csv,
)
from etf_flow_monitor.utils.io import format_tushare_date, read_user_csv, write_user_csv  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask whether previous no-announcement jumps were manually confirmed.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--pending-confirmations", default="", help="Blank uses config lifecycle_pending_confirmations_path.")
    parser.add_argument("--manual-confirmations", default="", help="Blank uses config lifecycle_manual_confirmations_path.")
    parser.add_argument("--yes", action="store_true", help="Confirm all pending rows without prompting.")
    parser.add_argument("--max-display", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config).resolve())
    pending_path = Path(args.pending_confirmations or config.lifecycle_pending_confirmations_path)
    manual_path = Path(args.manual_confirmations or config.lifecycle_manual_confirmations_path)

    pending = normalize_pending_confirmations(_read_csv_if_exists(pending_path, empty_pending_confirmations()))
    if pending.empty:
        print("[manual] 没有待扩大窗口重抓的公告缺失跳变。", flush=True)
        return 0

    print(f"[manual] 上次有 {len(pending)} 条跳变在公告窗口内未抓到公告。", flush=True)
    print(f"[manual] 待重抓清单：{pending_path}", flush=True)
    print(f"[manual] 人工确认白名单：{manual_path}", flush=True)
    _print_pending_preview(pending, max_display=max(int(args.max_display), 1))

    if not args.yes:
        print("[manual] 本轮不需要人工输入；这些跳变会自动扩大公告窗口后继续重抓。", flush=True)
        return 0

    selected = pending

    manual = normalize_manual_confirmations(_read_csv_if_exists(manual_path, empty_manual_confirmations()))
    confirmed = _pending_to_manual_confirmations(selected)
    combined = normalize_manual_confirmations(pd.concat([manual, confirmed], ignore_index=True))
    write_user_csv(manual_path, prepare_for_csv(combined))

    remaining = _drop_selected_pending(pending, selected)
    write_user_csv(pending_path, prepare_for_csv(remaining))
    print(f"[manual] 已手动确认 {len(selected)} 条；剩余待重抓 {len(remaining)} 条。", flush=True)
    return 0


def _read_csv_if_exists(path: Path, fallback: pd.DataFrame) -> pd.DataFrame:
    if not path.exists():
        return fallback.copy()
    return read_user_csv(path)


def _pending_to_manual_confirmations(pending: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(columns=MANUAL_CONFIRMATION_COLUMNS)
    for column in ("fund_code", "name", "trade_date", "prev_trade_date", "share_change", "share_change_pct"):
        result[column] = pending[column] if column in pending.columns else pd.NA
    result["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
    result["confirm_note"] = "bat_manual_confirmed_no_announcement"
    return result[MANUAL_CONFIRMATION_COLUMNS]


def _drop_selected_pending(pending: pd.DataFrame, selected: pd.DataFrame) -> pd.DataFrame:
    selected_keys = set(_pending_keys(selected))
    keep_mask = ~_pending_keys(pending).isin(selected_keys)
    return normalize_pending_confirmations(pending.loc[keep_mask].copy())


def _pending_keys(frame: pd.DataFrame) -> pd.Series:
    fund_code = frame["fund_code"].fillna("").astype(str).str.upper()
    trade_date = frame["trade_date"].map(format_tushare_date)
    prev_trade_date = frame["prev_trade_date"].map(format_tushare_date)
    return fund_code + "|" + trade_date + "|" + prev_trade_date


def _select_rows(pending: pd.DataFrame, reply: str) -> pd.DataFrame:
    row_numbers: set[int] = set()
    for item in reply.replace("，", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            row_numbers.add(int(text))
        except ValueError:
            continue
    valid_positions = [idx - 1 for idx in sorted(row_numbers) if 1 <= idx <= len(pending)]
    if not valid_positions:
        return empty_pending_confirmations()
    return pending.iloc[valid_positions].copy().reset_index(drop=True)


def _print_pending_preview(pending: pd.DataFrame, *, max_display: int) -> None:
    display = pending.head(max_display)
    for idx, row in display.iterrows():
        pct = row.get("share_change_pct")
        pct_text = f"{float(pct):.2%}" if pd.notna(pct) else ""
        print(
            "[manual] "
            f"{idx + 1}. {row.get('fund_code', '')} {row.get('name', '')} "
            f"跳变日={format_tushare_date(row.get('trade_date'))} "
            f"窗口={format_tushare_date(row.get('request_start_date'))}-{format_tushare_date(row.get('request_end_date'))} "
            f"变化={pct_text}",
            flush=True,
        )
    hidden = len(pending) - len(display)
    if hidden > 0:
        print(f"[manual] 还有 {hidden} 条未展示，请打开 CSV 查看完整清单。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
