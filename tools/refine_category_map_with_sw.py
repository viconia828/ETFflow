"""Refine ETF category map using Tushare SW2021 industry index classification."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.cache_store import CacheStore  # noqa: E402
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from tools.build_etf_category_map import infer_category  # noqa: E402


SW_COLUMNS = ["index_code", "industry_name", "level", "industry_code", "is_pub", "parent_code", "src"]
SW_SOURCE = "SW2021"
SW_DATASET = "sw_index_classify"
SW_PART = SW_SOURCE
SW_OUTPUT_COLUMNS = ["sw_index_code", "sw_industry_name", "sw_level", "sw_parent_code", "sw_match_term", "sw_match_rule"]
PURE_COMMODITY_KEYWORDS = ("黄金", "白银", "豆粕", "原油")
SKIP_CATEGORIES = {"债券", "货币", "港股", "海外", "商品", "红利价值"}
BROAD_INDEX_SUBCATEGORY_SKIP = {
    "沪深300",
    "中证A500",
    "中证500",
    "中证1000",
    "中证2000",
    "科创创业50",
    "中证A50",
    "富时中国A50",
    "上证50",
    "创业板50",
    "深证50",
    "上证180",
    "深证成指",
    "上证综指",
    "科创50",
    "创业板",
    "深证100",
    "MSCI",
}
GENERIC_SW_TERMS = {"综合"}
EXCHANGE_NAME_NOISE = ("上海证券交易所", "深圳证券交易所")


@dataclass(frozen=True, slots=True)
class SwMatch:
    category: str
    subcategory: str
    index_code: str
    industry_name: str
    level: str
    parent_code: str
    match_term: str
    match_rule: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refine ETF category map with SW2021 industry index names.")
    parser.add_argument("--config", default="config.example.txt")
    parser.add_argument("--category-map", default="")
    parser.add_argument("--refresh", action="store_true", help="Refresh SW index classification cache from Tushare.")
    parser.add_argument("--overwrite-reviewed", action="store_true", help="Also overwrite rows with non-empty review_note.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    map_path = Path(args.category_map or config.category_map_path)
    if not map_path.is_absolute():
        map_path = PROJECT_ROOT / map_path
    category_map = pd.read_csv(map_path, encoding="utf-8-sig")
    sw_frame = load_sw_index_classification(config.cache_dir, config_path, refresh=args.refresh)
    refined, summary = refine_category_map(category_map, sw_frame, overwrite_reviewed=args.overwrite_reviewed)
    refined.to_csv(map_path, index=False, encoding="utf-8-sig")
    print(f"rows: {len(refined)}")
    print(f"refined_rows: {summary['refined_rows']}")
    print(f"preserved_reviewed_rows: {summary['preserved_reviewed_rows']}")
    print(f"sw_cache_rows: {len(sw_frame)}")
    print(f"category_counts:\n{refined['category'].value_counts().to_string()}")
    print(f"output: {map_path}")
    return 0


def load_sw_index_classification(cache_dir: Path, config_path: Path, *, refresh: bool = False) -> pd.DataFrame:
    cache = CacheStore(cache_dir)
    cached = None if refresh else cache.load_static_frame("tushare", SW_DATASET, SW_PART)
    if cached is not None and not cached.empty:
        return _normalize_sw_frame(cached)
    source = TushareEtfSource.from_runtime(cache_dir=cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
    rows = source.client.query(
        "index_classify",
        params={"src": SW_SOURCE},
        fields=",".join(SW_COLUMNS),
    )
    fresh = _normalize_sw_frame(pd.DataFrame(rows))
    cache.save_static_frame("tushare", SW_DATASET, SW_PART, fresh)
    return fresh


def refine_category_map(category_map: pd.DataFrame, sw_frame: pd.DataFrame, *, overwrite_reviewed: bool = False) -> tuple[pd.DataFrame, dict[str, int]]:
    working = category_map.copy()
    for column in SW_OUTPUT_COLUMNS:
        if column not in working.columns:
            working[column] = ""
        working[column] = working[column].fillna("").astype(str)
    sw_index = _build_sw_index(sw_frame)
    refined_rows = 0
    preserved_reviewed_rows = 0
    for idx, row in working.iterrows():
        if not overwrite_reviewed and _clean_text(row.get("review_note")):
            preserved_reviewed_rows += 1
            continue
        baseline_category, baseline_subcategory, baseline_rule = infer_category(
            fund_type=row.get("fund_type"),
            benchmark=row.get("benchmark"),
            name=row.get("name"),
        )
        working.loc[idx, "category"] = baseline_category
        working.loc[idx, "subcategory"] = baseline_subcategory
        working.loc[idx, "category_rule"] = baseline_rule
        for column in SW_OUTPUT_COLUMNS:
            working.loc[idx, column] = ""
        row = working.loc[idx]
        match = _match_sw_industry(row, sw_index)
        if match is None:
            continue
        working.loc[idx, "category"] = match.category
        working.loc[idx, "subcategory"] = match.subcategory
        working.loc[idx, "category_rule"] = "sw2021:index_classify"
        working.loc[idx, "sw_index_code"] = match.index_code
        working.loc[idx, "sw_industry_name"] = match.industry_name
        working.loc[idx, "sw_level"] = match.level
        working.loc[idx, "sw_parent_code"] = match.parent_code
        working.loc[idx, "sw_match_term"] = match.match_term
        working.loc[idx, "sw_match_rule"] = match.match_rule
        refined_rows += 1
    return working, {"refined_rows": refined_rows, "preserved_reviewed_rows": preserved_reviewed_rows}


def _normalize_sw_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    working = pd.DataFrame() if frame is None else frame.copy()
    for column in SW_COLUMNS:
        if column not in working.columns:
            working[column] = ""
    working = working[SW_COLUMNS].copy()
    for column in SW_COLUMNS:
        working[column] = working[column].fillna("").astype(str).str.strip()
    return working.loc[working["industry_name"].ne("")].drop_duplicates(subset=["index_code", "industry_code"], keep="last").reset_index(drop=True)


def _build_sw_index(sw_frame: pd.DataFrame) -> dict[str, object]:
    code_to_row = {str(row.industry_code): row for row in sw_frame.itertuples(index=False)}
    industry_names = set(sw_frame["industry_name"].astype(str))
    entries = []
    for row in sw_frame.itertuples(index=False):
        industry_name = str(row.industry_name)
        terms = _industry_terms(industry_name)
        for term, rule in terms:
            if len(term) < 2:
                continue
            top_row = _top_level_row(row, code_to_row)
            entries.append(
                {
                    "term": term,
                    "match_rule": rule,
                    "category": str(top_row.industry_name),
                    "subcategory": industry_name,
                    "index_code": str(row.index_code),
                    "industry_name": industry_name,
                    "level": str(row.level),
                    "parent_code": str(row.parent_code),
                }
            )
    for alias, target_name in _alias_targets(industry_names):
        target = sw_frame.loc[sw_frame["industry_name"].eq(target_name)]
        if target.empty:
            continue
        row = target.iloc[0]
        top_row = _top_level_row(row, code_to_row)
        entries.append(
            {
                "term": alias,
                "match_rule": "sw2021:alias",
                "category": str(top_row.industry_name),
                "subcategory": str(row["industry_name"]),
                "index_code": str(row["index_code"]),
                "industry_name": str(row["industry_name"]),
                "level": str(row["level"]),
                "parent_code": str(row["parent_code"]),
            }
        )
    entries = sorted(entries, key=lambda item: len(item["term"]), reverse=True)
    return {"entries": entries}


def _industry_terms(industry_name: str) -> list[tuple[str, str]]:
    simplified = industry_name.replace("Ⅱ", "").replace("Ⅲ", "").replace("II", "").replace("III", "")
    if industry_name in GENERIC_SW_TERMS or simplified in GENERIC_SW_TERMS:
        return []
    terms = [(industry_name, "sw2021:industry_name")]
    if simplified and simplified != industry_name:
        terms.append((simplified, "sw2021:industry_name_simplified"))
    return terms


def _alias_targets(industry_names: set[str]) -> list[tuple[str, str]]:
    raw = [
        ("证券公司", "证券"),
        ("券商", "证券"),
        ("保险", "保险"),
        ("银行", "银行"),
        ("房地产", "房地产开发"),
        ("地产", "房地产开发"),
        ("半导体", "半导体"),
        ("芯片", "半导体"),
        ("消费电子", "消费电子"),
        ("光伏", "光伏设备"),
        ("光伏产业", "光伏设备"),
        ("电池", "电池"),
        ("新能源车", "汽车零部件"),
        ("新能源汽车", "汽车零部件"),
        ("智能车", "汽车零部件"),
        ("机器人", "自动化设备"),
        ("军工", "军工电子"),
        ("白酒", "白酒Ⅱ"),
        ("食品饮料", "食品饮料"),
        ("创新药", "化学制药"),
        ("医疗器械", "医疗器械"),
        ("医疗服务", "医疗服务"),
        ("游戏", "游戏Ⅱ"),
        ("传媒", "传媒"),
        ("家电", "家电行业"),
        ("煤炭", "煤炭开采"),
        ("钢铁", "普钢"),
        ("有色", "工业金属"),
        ("稀土", "小金属"),
    ]
    result = []
    for alias, target in raw:
        resolved = _resolve_target_name(target, industry_names)
        if resolved:
            result.append((alias, resolved))
    return result


def _resolve_target_name(target: str, industry_names: set[str]) -> str:
    if target in industry_names:
        return target
    stripped_target = target.replace("Ⅱ", "").replace("Ⅲ", "")
    for industry_name in industry_names:
        if industry_name.replace("Ⅱ", "").replace("Ⅲ", "") == stripped_target:
            return industry_name
    for industry_name in industry_names:
        if target in industry_name or stripped_target in industry_name.replace("Ⅱ", "").replace("Ⅲ", ""):
            return industry_name
    return ""


def _top_level_row(row: object, code_to_row: dict[str, object]) -> object:
    current = row
    seen = set()
    while str(getattr(current, "level", current["level"] if isinstance(current, pd.Series) else "")) != "L1":
        parent_code = str(getattr(current, "parent_code", current["parent_code"] if isinstance(current, pd.Series) else ""))
        if not parent_code or parent_code == "0" or parent_code in seen or parent_code not in code_to_row:
            return current
        seen.add(parent_code)
        current = code_to_row[parent_code]
    return current


def _match_sw_industry(row: pd.Series, sw_index: dict[str, object]) -> SwMatch | None:
    current_category = _clean_text(row.get("category"))
    if current_category in SKIP_CATEGORIES:
        return None
    if current_category == "宽基" and _clean_text(row.get("subcategory")) in BROAD_INDEX_SUBCATEGORY_SKIP:
        return None
    fund_type = _clean_text(row.get("fund_type"))
    text = _match_text(row)
    if not text:
        return None
    if _contains_any(text, ("港股", "港股通", "沪港深", "恒生", "恒指", "H股", "QDII", "跨境", "海外", "纳斯达克", "纳指", "标普", "日经")):
        return None
    if "商品" in fund_type or _contains_any(text, PURE_COMMODITY_KEYWORDS):
        return None
    for entry in sw_index["entries"]:
        term = str(entry["term"]).upper()
        if term and term in text:
            return SwMatch(
                category=str(entry["category"]),
                subcategory=str(entry["subcategory"]),
                index_code=str(entry["index_code"]),
                industry_name=str(entry["industry_name"]),
                level=str(entry["level"]),
                parent_code=str(entry["parent_code"]),
                match_term=str(entry["term"]),
                match_rule=str(entry["match_rule"]),
            )
    return None


def _match_text(row: pd.Series) -> str:
    parts = [row.get("benchmark"), row.get("name"), row.get("fund_type")]
    return _strip_exchange_name_noise(" ".join(_clean_text(value) for value in parts).upper())


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.upper() in text for keyword in keywords)


def _strip_exchange_name_noise(text: str) -> str:
    result = str(text)
    for phrase in EXCHANGE_NAME_NOISE:
        result = result.replace(phrase.upper(), "")
    return result


def _clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "<na>", "none"} else text


if __name__ == "__main__":
    raise SystemExit(main())
