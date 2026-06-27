"""Probe CNINFO announcement fetch stability at different request intervals."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tools.update_etf_announcements import main as update_announcements_main  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe CNINFO ETF announcement request intervals.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--max-codes", type=int, default=20)
    parser.add_argument("--sleep-seconds", default="0.5,0.2,0.0", help="Comma-separated intervals to test.")
    parser.add_argument("--heartbeat-seconds", type=int, default=5)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-sleep-seconds", type=float, default=0.0)
    parser.add_argument("--output-dir", default="outputs/lifecycle_audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    intervals = _parse_intervals(args.sleep_seconds)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, object]] = []
    for interval in intervals:
        tag = str(interval).replace(".", "p")
        announcement_file = output_dir / f"cninfo_rate_probe_{tag}.csv"
        pending_file = output_dir / f"cninfo_rate_probe_{tag}_pending.csv"
        update_args = [
            "--config",
            args.config,
            "--source",
            "cninfo",
            "--max-codes",
            str(args.max_codes),
            "--heartbeat-seconds",
            str(args.heartbeat_seconds),
            "--exchange-retries",
            str(args.retries),
            "--exchange-retry-sleep-seconds",
            str(args.retry_sleep_seconds),
            "--exchange-sleep-seconds",
            str(interval),
            "--announcement-file",
            str(announcement_file),
            "--pending-confirmations",
            str(pending_file),
        ]
        started = time.perf_counter()
        exit_code = update_announcements_main(update_args)
        elapsed = time.perf_counter() - started
        result = {
            "sleep_seconds": interval,
            "exit_code": exit_code,
            "elapsed_seconds": elapsed,
            "announcement_file": str(announcement_file),
            "pending_file": str(pending_file),
        }
        results.append(result)
        print(
            f"[rate result] sleep={interval:g}s exit={exit_code} elapsed={elapsed:.2f}s "
            f"ann={announcement_file} pending={pending_file}",
            flush=True,
        )

    summary = "; ".join(
        f"{item['sleep_seconds']:g}s: exit={item['exit_code']}, elapsed={float(item['elapsed_seconds']):.2f}s"
        for item in results
    )
    print(f"[rate summary] {summary}", flush=True)
    return 1 if any(int(item["exit_code"]) != 0 for item in results) else 0


def _parse_intervals(value: str) -> list[float]:
    intervals: list[float] = []
    for item in str(value or "").replace("，", ",").split(","):
        text = item.strip()
        if not text:
            continue
        intervals.append(max(float(text), 0.0))
    if not intervals:
        raise ValueError("at least one sleep interval is required")
    return intervals


if __name__ == "__main__":
    raise SystemExit(main())
