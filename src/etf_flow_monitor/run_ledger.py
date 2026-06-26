"""Run ledger helpers for long-running or scheduled monitor jobs."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Any, Mapping

from etf_flow_monitor.utils.io import write_json


class RunLedger:
    """Small JSON run ledger inspired by the momentum cache update console."""

    def __init__(self, *, log_dir: Path, argv: list[str], config_path: Path | None = None) -> None:
        self.log_dir = log_dir
        self.path = log_dir / "run.json"
        self.payload: dict[str, Any] = {
            "schema_version": "etf_flow_monitor_run_v1",
            "status": "running",
            "pid": os.getpid(),
            "argv": list(argv),
            "config_path": str(config_path or ""),
            "started_at": _now_text(),
            "finished_at": "",
            "exit_code": None,
            "last_progress_at": "",
            "last_stage": "starting",
            "last_message": "",
            "outputs": {},
            "stats": {},
        }
        self._write()

    def progress(self, stage: str, message: str = "", **payload: Any) -> None:
        self.payload.update(
            {
                "last_progress_at": _now_text(),
                "last_stage": str(stage or ""),
                "last_message": str(message or ""),
            }
        )
        if payload:
            self.payload.setdefault("progress_payloads", []).append({"stage": stage, **payload})
        self._write()

    def record_outputs(self, **outputs: str) -> None:
        current = dict(self.payload.get("outputs") or {})
        current.update({key: str(value) for key, value in outputs.items()})
        self.payload["outputs"] = current
        self._write()

    def record_stats(self, stats: Mapping[str, Any]) -> None:
        self.payload["stats"] = dict(stats)
        self._write()

    def finish(self, *, exit_code: int, status: str, error: str = "") -> None:
        self.payload.update({"status": status, "finished_at": _now_text(), "exit_code": int(exit_code)})
        if error:
            self.payload["error"] = error
        self._write()

    def _write(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self.payload)


def make_log_dir(output_dir: Path, prefix: str = "flow_monitor") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / "logs" / f"{prefix}_{stamp}"


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
