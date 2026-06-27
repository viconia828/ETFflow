"""Build an editable ETF category map from cached/fetched Tushare fund_basic."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.utils.io import clean_excel_text, read_user_csv, write_user_csv  # noqa: E402


CATEGORY_COLUMNS = [
    "fund_code",
    "name",
    "is_etf_candidate",
    "candidate_rule",
    "category",
    "subcategory",
    "fund_type",
    "benchmark",
    "market",
    "status",
    "list_date",
    "delist_date",
    "management",
    "sw_index_code",
    "sw_industry_name",
    "sw_level",
    "sw_parent_code",
    "sw_match_term",
    "sw_match_rule",
    "category_rule",
    "review_note",
]

EXCHANGE_NAME_NOISE = ("上海证券交易所", "深圳证券交易所")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build editable ETF category map from Tushare fund_basic.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--output", default="data/local_reference/etf_category_map.csv")
    parser.add_argument("--refresh", action="store_true", help="Bypass cached fund_basic and fetch from Tushare.")
    parser.add_argument("--include-inactive", action="store_true", help="Include delisted/inactive exchange-traded funds.")
    parser.add_argument("--include-non-etf-candidates", action="store_true", help="Include LOF/REIT/closed fund rows returned by Tushare market=E.")
    parser.add_argument("--overwrite-edits", action="store_true", help="Do not preserve existing category/subcategory edits.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
    basic = source.get_etf_basic(market=config.etf_market, refresh=args.refresh)
    category_map = build_category_map(basic)
    if not args.include_inactive:
        category_map = category_map.loc[category_map["status"].eq("L")].copy()
    if not args.include_non_etf_candidates:
        category_map = category_map.loc[category_map["is_etf_candidate"].eq("Y")].copy()

    if output_path.exists() and not args.overwrite_edits:
        category_map = preserve_existing_edits(category_map, read_user_csv(output_path))

    write_user_csv(output_path, category_map)
    print(f"rows: {len(category_map)}")
    print(f"output: {output_path}")
    print(f"cache_stats: {source.cache.snapshot_stats() if source.cache is not None else {}}")
    return 0


def build_category_map(basic: pd.DataFrame) -> pd.DataFrame:
    if basic is None or basic.empty:
        return pd.DataFrame(columns=CATEGORY_COLUMNS)
    working = basic.copy()
    for column in ("fund_code", "name", "fund_type", "benchmark", "market", "status", "list_date", "delist_date", "management"):
        if column not in working.columns:
            working[column] = pd.NA
    categories: list[str] = []
    subcategories: list[str] = []
    rules: list[str] = []
    candidate_flags: list[str] = []
    candidate_rules: list[str] = []
    for row in working.itertuples(index=False):
        is_candidate, candidate_rule = infer_etf_candidate(
            fund_code=getattr(row, "fund_code", ""),
            fund_type=getattr(row, "fund_type", ""),
            name=getattr(row, "name", ""),
        )
        category, subcategory, rule = infer_category(
            fund_type=getattr(row, "fund_type", ""),
            benchmark=getattr(row, "benchmark", ""),
            name=getattr(row, "name", ""),
        )
        candidate_flags.append("Y" if is_candidate else "N")
        candidate_rules.append(candidate_rule)
        categories.append(category)
        subcategories.append(subcategory)
        rules.append(rule)
    result = pd.DataFrame(
        {
            "fund_code": working["fund_code"].map(clean_excel_text).str.upper(),
            "name": working["name"].map(clean_excel_text),
            "is_etf_candidate": candidate_flags,
            "candidate_rule": candidate_rules,
            "category": categories,
            "subcategory": subcategories,
            "fund_type": working["fund_type"].map(clean_excel_text),
            "benchmark": working["benchmark"].map(clean_excel_text),
            "market": working["market"].map(clean_excel_text),
            "status": working["status"].map(clean_excel_text),
            "list_date": _date_text(working["list_date"]),
            "delist_date": _date_text(working["delist_date"]),
            "management": working["management"].map(clean_excel_text),
            "sw_index_code": "",
            "sw_industry_name": "",
            "sw_level": "",
            "sw_parent_code": "",
            "sw_match_term": "",
            "sw_match_rule": "",
            "category_rule": rules,
            "review_note": "",
        }
    )
    result = result[result["fund_code"].ne("")]
    return result[CATEGORY_COLUMNS].sort_values(["category", "subcategory", "fund_code"], kind="stable").reset_index(drop=True)


def preserve_existing_edits(fresh: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty or "fund_code" not in existing.columns:
        return fresh
    editable_columns = [
        "category",
        "subcategory",
        "review_note",
        "sw_index_code",
        "sw_industry_name",
        "sw_level",
        "sw_parent_code",
        "sw_match_term",
        "sw_match_rule",
    ]
    keep_columns = ["fund_code", *[column for column in editable_columns if column in existing.columns]]
    previous = existing[keep_columns].copy()
    previous["fund_code"] = previous["fund_code"].map(clean_excel_text).str.upper()
    for column in editable_columns:
        if column in previous.columns:
            previous[column] = previous[column].map(clean_excel_text)
    merged = fresh.merge(previous, on="fund_code", how="left", suffixes=("", "_existing"))
    for column in editable_columns:
        existing_column = f"{column}_existing"
        if existing_column not in merged.columns:
            continue
        mask = merged[existing_column].notna() & merged[existing_column].astype(str).str.strip().ne("")
        merged.loc[mask, column] = merged.loc[mask, existing_column]
        merged = merged.drop(columns=[existing_column])
    return merged[CATEGORY_COLUMNS].sort_values(["category", "subcategory", "fund_code"], kind="stable").reset_index(drop=True)


def infer_category(*, fund_type: object, benchmark: object, name: object) -> tuple[str, str, str]:
    fund_type_text = _clean_text(fund_type)
    benchmark_text = _clean_text(benchmark)
    name_text = _clean_text(name)
    fund_type_and_name = f"{fund_type_text} {name_text}".upper()
    text = _strip_exchange_name_noise(f"{fund_type_text} {benchmark_text} {name_text}".upper())

    if "货币型" in fund_type_text:
        return "货币", "货币现金", "fund_type:money"
    if _contains_any(text, ("自由现金流", "FREECASHFLOW", "FREE CASH FLOW")):
        return "红利价值", "自由现金流", "fund_type/benchmark/name:free_cash_flow"

    primary_rules = [("债券", "债券", "fund_type/name:bond", fund_type_and_name, ("债券型", "债券", "国债", "政金", "信用债", "转债", "短融", "城投", "可转债"))]
    for category, subcategory, rule, target_text, keywords in primary_rules:
        if _contains_any(target_text, keywords):
            return category, subcategory, rule

    pre_broad_rules = [
        ("商品", "商品", "fund_type/benchmark/name:commodity", ("商品", "黄金", "白银", "有色", "能源化工", "豆粕", "煤炭", "钢铁", "石油", "原油", "油气", "铜")),
        ("港股", "港股", "fund_type/benchmark/name:hong_kong", ("港股", "港股通", "沪港深", "恒生", "恒指", "H股", "香港")),
        ("海外", "海外", "fund_type/benchmark/name:overseas", ("QDII", "跨境", "海外", "中概", "纳指", "纳斯达克", "标普", "日经", "德国", "法国", "印度", "沙特", "巴西", "韩国", "新兴亚洲", "东南亚")),
    ]
    for category, subcategory, rule, keywords in pre_broad_rules:
        if _contains_any(text, keywords):
            return category, subcategory, rule

    broad_subcategory = _broad_index_subcategory(text)
    if broad_subcategory:
        return "宽基", broad_subcategory, "fund_type/benchmark/name:broad_index"

    rules = [
        ("红利价值", "红利价值", "fund_type/benchmark/name:dividend_value", ("红利", "价值", "低波", "央企", "股息", "高股息")),
        ("医药", "医药医疗", "fund_type/benchmark/name:healthcare", ("医药", "医疗", "创新药", "生物", "疫苗", "中药")),
        ("金融地产", "金融地产", "fund_type/benchmark/name:financial_real_estate", ("金融", "银行", "证券", "保险", "地产", "房地产")),
        ("科技", "科技成长", "fund_type/benchmark/name:technology", ("科技", "芯片", "半导体", "人工智能", "AI", "软件", "计算机", "通信", "电子", "互联网", "数字")),
        ("新能源制造", "新能源制造", "fund_type/benchmark/name:advanced_manufacturing", ("新能源", "电池", "光伏", "锂", "稀土", "机器人", "智能车", "汽车", "军工", "机械", "装备", "制造")),
    ]
    for category, subcategory, rule, keywords in rules:
        if _contains_any(text, keywords):
            return category, subcategory, rule

    if _contains_any(text, ("指数", "INDEX", "ETF")):
        return "宽基", "指数增强/其他宽基候选", "fund_type/benchmark/name:index_candidate"
    return "其他", "待人工确认", "fallback:manual_review"


def infer_etf_candidate(*, fund_code: object, fund_type: object, name: object) -> tuple[bool, str]:
    code_text = _clean_text(fund_code).upper()
    fund_type_text = _clean_text(fund_type)
    name_text = _clean_text(name).upper()
    if _contains_any(name_text, ("LOF", "REIT", "分级", "封闭", "定开", "联接")):
        return False, "exclude:name_non_etf"
    if "ETF" in name_text or "交易型" in name_text:
        return True, "include:name_etf"
    if "货币型" in fund_type_text and code_text.startswith(("159", "511")):
        return True, "include:money_etf_code"
    return False, "exclude:not_etf_like"


def _broad_index_subcategory(text: str) -> str:
    checks = [
        ("沪深300", ("沪深300", "300ETF", "HS300")),
        ("中证A500", ("中证A500", "A500")),
        ("中证500", ("中证500", "500ETF")),
        ("中证1000", ("中证1000", "1000ETF")),
        ("中证2000", ("中证2000", "2000ETF")),
        ("科创创业50", ("科创创业50",)),
        ("中证A50", ("中证A50",)),
        ("富时中国A50", ("富时中国A50", "FTSECHINAA50", "FTSE CHINA A50", "富时A50")),
        ("上证50", ("上证50",)),
        ("创业板50", ("创业板50",)),
        ("深证50", ("深证50", "深证主板50")),
        ("上证180", ("上证180",)),
        ("深证成指", ("深证成份", "深证成指", "深圳成份", "深成指")),
        ("上证综指", ("上证综合", "上证综指")),
        ("科创50", ("科创50", "科创板50")),
        ("创业板", ("创业板", "创业")),
        ("深证100", ("深证100",)),
        ("MSCI", ("MSCI",)),
    ]
    for subcategory, keywords in checks:
        if _contains_any(text, keywords):
            return subcategory
    return ""


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(str(keyword).upper() in text for keyword in keywords)


def _strip_exchange_name_noise(text: str) -> str:
    result = str(text)
    for phrase in EXCHANGE_NAME_NOISE:
        result = result.replace(phrase.upper(), "")
    return result


def _clean_text(value: object) -> str:
    return clean_excel_text(value)


def _date_text(series: pd.Series) -> pd.Series:
    dates = pd.to_datetime(series, errors="coerce")
    return dates.dt.strftime("%Y-%m-%d").fillna("")


if __name__ == "__main__":
    raise SystemExit(main())
