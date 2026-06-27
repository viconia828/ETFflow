from __future__ import annotations

from etf_flow_monitor.data.tushare_http import TushareHttpClient, TusharePermissionError, TushareRateLimitError


def test_tushare_response_classifies_permission_error_before_rate_limit() -> None:
    payload = {"code": 40203, "msg": "抱歉，您没有接口(anns_d)访问权限"}

    try:
        TushareHttpClient._normalize_response("anns_d", payload)
    except TusharePermissionError:
        return
    raise AssertionError("expected TusharePermissionError")


def test_tushare_response_still_classifies_frequency_error() -> None:
    payload = {"code": 40203, "msg": "接口调用频率超限"}

    try:
        TushareHttpClient._normalize_response("fund_daily", payload)
    except TushareRateLimitError:
        return
    raise AssertionError("expected TushareRateLimitError")
