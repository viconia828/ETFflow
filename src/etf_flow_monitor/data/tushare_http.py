"""Minimal Tushare Pro HTTP client with local-secret loading helpers."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request


DEFAULT_TUSHARE_API_URL = "http://api.tushare.pro"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRY_COUNT = 4
DEFAULT_RETRY_SLEEP_SECONDS = 1.0
DEFAULT_MIN_INTERVAL_SECONDS = 0.12
DEFAULT_RATE_LIMIT_SLEEP_SECONDS = 15.0
DEFAULT_IGNORE_PROXY = True
LOCAL_SECRET_FILENAMES = (".local_secrets.local.json", ".local_secrets.json")
_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}


def _read_local_secret_file(path: str) -> dict | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def load_local_secret_map(search_dirs=None) -> dict:
    ordered_dirs = []
    for raw_dir in search_dirs or ():
        if not raw_dir:
            continue
        abs_dir = os.path.abspath(raw_dir)
        if abs_dir not in ordered_dirs:
            ordered_dirs.append(abs_dir)
    for directory in ordered_dirs:
        for filename in LOCAL_SECRET_FILENAMES:
            payload = _read_local_secret_file(os.path.join(directory, filename))
            if payload is not None:
                return payload
    return {}


def _parse_bool(value, default: bool) -> bool:
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(default)


def _parse_float(value, default: float) -> float:
    if value is None:
        return float(default)
    text = str(value).strip()
    if not text:
        return float(default)
    try:
        return float(text)
    except (TypeError, ValueError):
        return float(default)


def load_tushare_runtime_config(search_dirs=None) -> dict:
    """Return token/url config from env first, then local ignored JSON files."""
    secret_map = load_local_secret_map(search_dirs=search_dirs)
    token = (
        os.environ.get("TUSHARE_TOKEN")
        or os.environ.get("TUSHARE_PRO_TOKEN")
        or secret_map.get("tushare_token")
        or secret_map.get("TUSHARE_TOKEN")
        or ""
    )
    api_url = os.environ.get("TUSHARE_API_URL") or secret_map.get("tushare_api_url") or DEFAULT_TUSHARE_API_URL
    timeout_seconds = os.environ.get("TUSHARE_TIMEOUT_SECONDS") or secret_map.get("tushare_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    retry_count = os.environ.get("TUSHARE_RETRY_COUNT") or secret_map.get("tushare_retry_count") or DEFAULT_RETRY_COUNT
    retry_sleep_seconds = (
        os.environ.get("TUSHARE_RETRY_SLEEP_SECONDS")
        or secret_map.get("tushare_retry_sleep_seconds")
        or DEFAULT_RETRY_SLEEP_SECONDS
    )
    min_interval_seconds = (
        os.environ.get("TUSHARE_MIN_INTERVAL_SECONDS")
        or secret_map.get("tushare_min_interval_seconds")
        or DEFAULT_MIN_INTERVAL_SECONDS
    )
    rate_limit_sleep_seconds = (
        os.environ.get("TUSHARE_RATE_LIMIT_SLEEP_SECONDS")
        or secret_map.get("tushare_rate_limit_sleep_seconds")
        or DEFAULT_RATE_LIMIT_SLEEP_SECONDS
    )
    ignore_proxy = (
        os.environ.get("TUSHARE_IGNORE_PROXY")
        or os.environ.get("DATA_SOURCE_IGNORE_PROXY")
        or secret_map.get("tushare_ignore_proxy")
        or secret_map.get("data_source_ignore_proxy")
    )
    return {
        "token": str(token).strip(),
        "api_url": str(api_url).strip() or DEFAULT_TUSHARE_API_URL,
        "timeout_seconds": max(int(timeout_seconds), 1),
        "retry_count": max(int(retry_count), 1),
        "retry_sleep_seconds": max(float(retry_sleep_seconds), 0.0),
        "min_interval_seconds": max(_parse_float(min_interval_seconds, DEFAULT_MIN_INTERVAL_SECONDS), 0.0),
        "rate_limit_sleep_seconds": max(_parse_float(rate_limit_sleep_seconds, DEFAULT_RATE_LIMIT_SLEEP_SECONDS), 0.0),
        "ignore_proxy": _parse_bool(ignore_proxy, DEFAULT_IGNORE_PROXY),
    }


class TushareRateLimitError(RuntimeError):
    """Raised when Tushare reports API frequency throttling."""


class TushareHttpClient:
    """Small HTTP wrapper around the official Tushare Pro POST API."""

    supports_parallel_requests = False
    _rate_limit_lock = threading.Lock()
    _last_request_at_by_key: dict[tuple[str, str], float] = {}

    def __init__(
        self,
        token,
        api_url=DEFAULT_TUSHARE_API_URL,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        retry_count=DEFAULT_RETRY_COUNT,
        retry_sleep_seconds=DEFAULT_RETRY_SLEEP_SECONDS,
        min_interval_seconds=0.0,
        rate_limit_sleep_seconds=DEFAULT_RATE_LIMIT_SLEEP_SECONDS,
        ignore_proxy=DEFAULT_IGNORE_PROXY,
    ):
        self.token = str(token or "").strip()
        self.api_url = str(api_url or "").strip() or DEFAULT_TUSHARE_API_URL
        self.timeout_seconds = int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        self.retry_count = max(int(retry_count or DEFAULT_RETRY_COUNT), 1)
        self.retry_sleep_seconds = max(float(DEFAULT_RETRY_SLEEP_SECONDS if retry_sleep_seconds is None else retry_sleep_seconds), 0.0)
        self.min_interval_seconds = max(float(0.0 if min_interval_seconds is None else min_interval_seconds), 0.0)
        self.rate_limit_sleep_seconds = max(float(DEFAULT_RATE_LIMIT_SLEEP_SECONDS if rate_limit_sleep_seconds is None else rate_limit_sleep_seconds), 0.0)
        self.ignore_proxy = bool(ignore_proxy)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.ignore_proxy else None
        if not self.token:
            raise ValueError("Tushare token cannot be empty.")

    def query(self, api_name, params=None, fields=None) -> list[dict]:
        if not api_name:
            raise ValueError("api_name cannot be empty.")
        payload = {"api_name": str(api_name).strip(), "token": self.token, "params": params or {}}
        if fields:
            payload["fields"] = ",".join(str(item).strip() for item in fields) if isinstance(fields, (list, tuple)) else str(fields).strip()
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

        last_error = None
        for attempt in range(1, self.retry_count + 1):
            try:
                request = urllib.request.Request(self.api_url, data=encoded_payload, headers=headers, method="POST")
                self._wait_for_rate_limit_turn()
                with self._open(request) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    raw_text = response.read().decode(charset, errors="replace")
                parsed = json.loads(raw_text.lstrip("\ufeff"))
                return self._normalize_response(payload["api_name"], parsed)
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_count:
                    break
                if isinstance(exc, TushareRateLimitError):
                    time.sleep(max(self.rate_limit_sleep_seconds, self.retry_sleep_seconds * attempt))
                else:
                    time.sleep(min(5.0, self.retry_sleep_seconds * attempt))
        raise RuntimeError(f"Tushare request failed: {api_name}: {last_error}") from last_error

    def _wait_for_rate_limit_turn(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        key = (self.api_url, self.token)
        with self._rate_limit_lock:
            now = time.monotonic()
            last = self._last_request_at_by_key.get(key)
            if last is not None:
                wait_seconds = self.min_interval_seconds - (now - last)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                    now = time.monotonic()
            self._last_request_at_by_key[key] = now

    def _open(self, request):
        if self._opener is not None:
            return self._opener.open(request, timeout=self.timeout_seconds)
        return urllib.request.urlopen(request, timeout=self.timeout_seconds)

    @staticmethod
    def _normalize_response(api_name, parsed) -> list[dict]:
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Tushare response format error: {api_name}")
        code = parsed.get("code", 0)
        if code not in (0, None):
            message = str(parsed.get("msg") or "").strip() or "unknown error"
            if str(code) == "40203" or "frequency" in message.lower() or "频率" in message or "超限" in message:
                raise TushareRateLimitError(f"Tushare rate limit: {api_name} code={code} msg={message}")
            raise RuntimeError(f"Tushare API error: {api_name} code={code} msg={message}")
        data = parsed.get("data") or {}
        fields = list(data.get("fields") or [])
        items = list(data.get("items") or [])
        if not fields:
            return []
        rows = []
        for item in items:
            rows.append({str(field): item[idx] if idx < len(item) else None for idx, field in enumerate(fields)})
        return rows
