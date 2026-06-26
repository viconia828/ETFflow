"""Editable ETF category-map loading and merge helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


CATEGORY_MAP_COLUMNS = [
    "fund_code",
    "category",
    "subcategory",
    "review_note",
]

EXPECTED_CATEGORIES = {
    "宽基",
    "科技",
    "港股",
    "海外",
    "新能源制造",
    "商品",
    "金融地产",
    "货币",
    "债券",
    "红利价值",
    "医药",
    "其他",
    "农林牧渔",
    "基础化工",
    "钢铁",
    "有色金属",
    "电子",
    "汽车",
    "家用电器",
    "食品饮料",
    "纺织服饰",
    "轻工制造",
    "医药生物",
    "公用事业",
    "交通运输",
    "房地产",
    "商贸零售",
    "社会服务",
    "综合",
    "建筑材料",
    "建筑装饰",
    "电力设备",
    "国防军工",
    "计算机",
    "传媒",
    "通信",
    "银行",
    "非银金融",
    "煤炭",
    "石油石化",
    "环保",
    "美容护理",
    "机械设备",
}

FLOW_CATEGORY_COLUMNS = [
    "category",
    "subcategory",
    "review_note",
    "category_source",
]


def load_category_map(path: str | Path | None) -> pd.DataFrame:
    """Load the editable ETF category map, returning an empty normalized frame if missing."""

    if path is None:
        return pd.DataFrame(columns=CATEGORY_MAP_COLUMNS)
    map_path = Path(path)
    if not map_path.exists():
        return pd.DataFrame(columns=CATEGORY_MAP_COLUMNS)
    frame = pd.read_csv(map_path, encoding="utf-8-sig")
    return normalize_category_map(frame)


def normalize_category_map(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CATEGORY_MAP_COLUMNS)
    working = frame.copy()
    for column in CATEGORY_MAP_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    working = working[CATEGORY_MAP_COLUMNS].copy()
    working["fund_code"] = working["fund_code"].fillna("").astype(str).str.strip().str.upper()
    for column in ("category", "subcategory", "review_note"):
        working[column] = working[column].map(_clean_text)
    working = working.loc[working["fund_code"].ne("")]
    return working.drop_duplicates(subset=["fund_code"], keep="last").reset_index(drop=True)


def apply_category_map(flow: pd.DataFrame, category_map: pd.DataFrame | None) -> pd.DataFrame:
    """Merge editable categories into a flow snapshot."""

    if flow is None or flow.empty:
        result = pd.DataFrame() if flow is None else flow.copy()
        for column in FLOW_CATEGORY_COLUMNS:
            if column not in result.columns:
                result[column] = pd.Series(dtype="string")
        return result

    working = flow.copy()
    working["fund_code"] = working["fund_code"].fillna("").astype(str).str.strip().str.upper()
    normalized_map = normalize_category_map(category_map)

    for column in FLOW_CATEGORY_COLUMNS:
        if column in working.columns:
            working = working.drop(columns=[column])

    if normalized_map.empty:
        working["category"] = "其他"
        working["subcategory"] = "待人工确认"
        working["review_note"] = ""
        working["category_source"] = "missing_category_map"
        return working

    merged = working.merge(normalized_map, on="fund_code", how="left")
    matched = merged["category"].map(_clean_text).ne("")
    merged["category"] = merged["category"].map(_clean_text)
    merged["subcategory"] = merged["subcategory"].map(_clean_text)
    merged["review_note"] = merged["review_note"].map(_clean_text)
    merged.loc[~matched, "category"] = "其他"
    merged.loc[~matched, "subcategory"] = "待人工确认"
    merged.loc[~matched, "review_note"] = ""
    merged["category_source"] = "manual_map"
    merged.loc[~matched, "category_source"] = "unmapped"
    return merged


def category_map_stats(flow: pd.DataFrame) -> dict[str, int]:
    if flow is None or flow.empty or "category_source" not in flow.columns:
        return {
            "category_mapped_rows": 0,
            "category_unmapped_rows": 0,
            "category_mapped_funds": 0,
            "category_unmapped_funds": 0,
        }
    source = flow["category_source"].fillna("").astype(str)
    mapped = source.eq("manual_map")
    fund_codes = flow.get("fund_code", pd.Series(dtype="string")).fillna("").astype(str)
    return {
        "category_mapped_rows": int(mapped.sum()),
        "category_unmapped_rows": int((~mapped).sum()),
        "category_mapped_funds": int(fund_codes.loc[mapped].nunique()),
        "category_unmapped_funds": int(fund_codes.loc[~mapped].nunique()),
    }


def category_map_codes(category_map: pd.DataFrame | None) -> list[str]:
    normalized = normalize_category_map(category_map)
    if normalized.empty:
        return []
    return normalized["fund_code"].dropna().astype(str).str.upper().drop_duplicates().tolist()


def _clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "<na>", "none"} else text
