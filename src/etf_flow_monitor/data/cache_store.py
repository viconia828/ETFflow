"""Lightweight filesystem-backed cache store for daily monitor datasets."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from etf_flow_monitor.utils.io import read_json, safe_filename, write_json


class CacheStore:
    """CSV/JSON cache store copied from the momentum projects and simplified."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.reference_dir = self.base_dir.parent / "local_reference"
        self.stats: dict[str, int] = {}

    def snapshot_stats(self) -> dict[str, int]:
        return dict(self.stats)

    def load_calendar(self, source_name: str, exchange: str) -> pd.DataFrame | None:
        return self._load_csv(self.base_dir / source_name / "calendar" / f"{exchange.upper()}.csv")

    def save_calendar(self, source_name: str, exchange: str, frame: pd.DataFrame) -> None:
        self._save_csv(self.base_dir / source_name / "calendar" / f"{exchange.upper()}.csv", frame)

    def load_static_frame(self, source_name: str, dataset_name: str, part: str) -> pd.DataFrame | None:
        return self._load_csv(self.base_dir / source_name / "static" / f"{dataset_name}_{part}.csv")

    def save_static_frame(self, source_name: str, dataset_name: str, part: str, frame: pd.DataFrame) -> None:
        self._save_csv(self.base_dir / source_name / "static" / f"{dataset_name}_{part}.csv", frame)

    def load_time_series(
        self,
        source_name: str,
        dataset_name: str,
        code: str,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame | None:
        filename = f"{safe_filename(code)}.csv"
        return self._load_csv(self.base_dir / source_name / "time_series" / dataset_name / filename, columns=columns)

    def save_time_series(self, source_name: str, dataset_name: str, code: str, frame: pd.DataFrame) -> None:
        filename = f"{safe_filename(code)}.csv"
        self._save_csv(self.base_dir / source_name / "time_series" / dataset_name / filename, frame)

    def load_daily_cross_section(
        self,
        source_name: str,
        dataset_name: str,
        trade_date: str,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame | None:
        return self._load_csv(
            self.base_dir / source_name / "daily_cross_section" / dataset_name / f"{safe_filename(trade_date)}.csv",
            columns=columns,
        )

    def save_daily_cross_section(self, source_name: str, dataset_name: str, trade_date: str, frame: pd.DataFrame) -> None:
        self._save_csv(
            self.base_dir / source_name / "daily_cross_section" / dataset_name / f"{safe_filename(trade_date)}.csv",
            frame,
        )

    def load_manifest(self, source_name: str, dataset_name: str, key: str) -> dict:
        path = self.base_dir / source_name / "manifests" / dataset_name / f"{safe_filename(key)}.json"
        self._record("manifest_reads")
        return read_json(path)

    def save_manifest(self, source_name: str, dataset_name: str, key: str, payload: dict) -> None:
        path = self.base_dir / source_name / "manifests" / dataset_name / f"{safe_filename(key)}.json"
        self._record("manifest_writes")
        write_json(path, payload)

    def _load_csv(self, path: Path, columns: Iterable[str] | None = None) -> pd.DataFrame | None:
        self._record("file_reads")
        if not path.exists():
            self._record("cache_misses")
            return None
        self._record("cache_hits")
        if columns is not None:
            requested_columns = [str(column) for column in columns]
            try:
                return pd.read_csv(path, encoding="utf-8-sig", usecols=requested_columns)
            except ValueError:
                requested = set(requested_columns)
                return pd.read_csv(path, encoding="utf-8-sig", usecols=lambda column: column in requested)
        return pd.read_csv(path, encoding="utf-8-sig")

    def _save_csv(self, path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        self._record("file_writes")

    def _record(self, key: str, count: int = 1) -> None:
        self.stats[key] = int(self.stats.get(key, 0)) + int(count)
