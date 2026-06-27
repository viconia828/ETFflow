"""Profile lifecycle share-jump audit rows to tune the review scope."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.utils.io import read_user_csv, write_user_csv  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile lifecycle share-jump audit rows and candidate narrowing rules.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--audit", default="", help="Audit CSV. Blank uses latest outputs/lifecycle_audit audit CSV.")
    parser.add_argument("--output-dir", default="outputs/lifecycle_audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config).resolve())
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    audit_path = Path(args.audit) if args.audit else _latest_audit_path(output_dir)
    if not audit_path.is_absolute():
        audit_path = PROJECT_ROOT / audit_path
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = build_profile(audit_path=audit_path, category_map_path=config.category_map_path)
    detail_path = output_dir / "lifecycle_jump_scope_profile_20260627.csv"
    summary_path = output_dir / "lifecycle_jump_scope_summary_20260627.md"
    write_user_csv(detail_path, profile.frame)
    summary_path.write_text(profile.markdown, encoding="utf-8")

    print(profile.console, flush=True)
    print(f"profile: {detail_path}", flush=True)
    print(f"summary: {summary_path}", flush=True)
    return 0


class JumpScopeProfile:
    def __init__(self, *, frame: pd.DataFrame, markdown: str, console: str) -> None:
        self.frame = frame
        self.markdown = markdown
        self.console = console


def build_profile(*, audit_path: Path, category_map_path: Path) -> JumpScopeProfile:
    jumps = read_user_csv(audit_path)
    category_map = read_user_csv(category_map_path) if category_map_path.exists() else pd.DataFrame()
    frame = _prepare_frame(jumps, category_map)
    scope_table = _scope_comparison(frame)
    abs_bins = _abs_pct_bins(frame)
    list_bins = _days_since_list_bins(frame)
    category_counts = _value_counts(frame, "category")
    fund_type_counts = _value_counts(frame, "fund_type")
    top_funds = (
        frame.groupby(["fund_code", "name", "category", "fund_type"], dropna=False)
        .size()
        .sort_values(ascending=False)
        .head(25)
        .reset_index(name="jump_rows")
    )
    matched = frame.loc[frame["matched"]].copy()

    markdown = "\n".join(
        [
            "# 生命周期跳变审计范围画像",
            "",
            f"- 审计文件：`{audit_path}`",
            f"- 跳变行数：{len(frame)}",
            f"- 已匹配生命周期事件：{int(frame['matched'].sum())}",
            f"- 未匹配：{int((~frame['matched']).sum())}",
            "",
            "## 候选范围对比",
            _markdown_table(scope_table),
            "",
            "## 绝对变化比例分布",
            _markdown_table(abs_bins),
            "",
            "## 上市时间分布",
            _markdown_table(list_bins),
            "",
            "## 分类分布 Top 20",
            _markdown_table(category_counts.head(20)),
            "",
            "## 基金类型分布",
            _markdown_table(fund_type_counts.head(20)),
            "",
            "## 同一基金跳变次数 Top 25",
            _markdown_table(top_funds),
            "",
            "## 已匹配样本",
            _markdown_table(
                matched[
                    [
                        "fund_code",
                        "name",
                        "trade_date",
                        "share_change_pct",
                        "category",
                        "days_since_list",
                        "share_ratio",
                        "integer_ratio_error",
                        "matched_event_type",
                    ]
                ]
                if not matched.empty
                else pd.DataFrame()
            ),
            "",
        ]
    )
    console = "\n".join(
        [
            f"[scope] audit={audit_path}",
            f"[scope] rows={len(frame)} matched={int(frame['matched'].sum())} unmatched={int((~frame['matched']).sum())}",
            "[scope] candidate rules:",
            scope_table.to_string(index=False),
            "",
            "[scope] abs pct bins:",
            abs_bins.to_string(index=False),
            "",
            "[scope] days since list bins:",
            list_bins.to_string(index=False),
            "",
            "[scope] top funds:",
            top_funds.head(15).to_string(index=False),
        ]
    )
    return JumpScopeProfile(frame=frame.sort_values("abs_pct", ascending=False), markdown=markdown, console=console)


def _prepare_frame(jumps: pd.DataFrame, category_map: pd.DataFrame) -> pd.DataFrame:
    frame = jumps.copy()
    for column in ("share_change_pct", "share_change", "prev_shares", "shares", "abs_share_change"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("trade_date", "prev_trade_date"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], format="%Y%m%d", errors="coerce")

    map_columns = [column for column in ("fund_code", "category", "subcategory", "fund_type", "list_date", "status") if column in category_map.columns]
    if map_columns:
        metadata = category_map[map_columns].drop_duplicates("fund_code")
        frame = frame.merge(metadata, on="fund_code", how="left")
    for column in ("category", "subcategory", "fund_type", "list_date"):
        if column not in frame.columns:
            frame[column] = ""
        frame[column] = frame[column].fillna("").astype(str).str.strip()

    frame["list_date_parsed"] = pd.to_datetime(frame["list_date"], errors="coerce")
    frame["days_since_list"] = (frame["trade_date"] - frame["list_date_parsed"]).dt.days
    frame["abs_pct"] = frame["share_change_pct"].abs()
    frame["direction"] = frame["share_change_pct"].map(lambda value: "increase" if pd.notna(value) and value > 0 else "decrease")
    frame["matched"] = frame.get("match_status", "").fillna("").astype(str).eq("matched_lifecycle_event")
    frame["share_ratio"] = frame["shares"] / frame["prev_shares"]
    frame["nearest_integer_ratio"] = frame["share_ratio"].round()
    frame["integer_ratio_error"] = (frame["share_ratio"] - frame["nearest_integer_ratio"]).abs()
    frame["integer_like"] = frame["nearest_integer_ratio"].between(2, 20) & frame["integer_ratio_error"].le(0.03)
    frame["early_60d"] = frame["days_since_list"].notna() & frame["days_since_list"].lt(60)
    frame["money_like"] = frame["category"].eq("货币") | frame["fund_type"].str.contains("货币", na=False)
    return frame


def _scope_comparison(frame: pd.DataFrame) -> pd.DataFrame:
    scopes = {
        "current_abs_ge_50pct": pd.Series(True, index=frame.index),
        "abs_ge_100pct": frame["abs_pct"].ge(1.0),
        "abs_ge_100pct_ex_money": frame["abs_pct"].ge(1.0) & ~frame["money_like"],
        "abs_ge_100pct_ex_money_after_60d": frame["abs_pct"].ge(1.0) & ~frame["money_like"] & ~frame["early_60d"],
        "integer_like_positive_or_drop_ge_50pct_ex_money": (
            (frame["integer_like"] & frame["share_change_pct"].gt(0)) | frame["share_change_pct"].le(-0.5)
        )
        & ~frame["money_like"],
        "integer_like_positive_or_drop_ge_80pct_ex_money": (
            (frame["integer_like"] & frame["share_change_pct"].gt(0)) | frame["share_change_pct"].le(-0.8)
        )
        & ~frame["money_like"],
        "integer_like_positive_or_drop_ge_50pct_ex_money_after_60d": (
            (frame["integer_like"] & frame["share_change_pct"].gt(0)) | frame["share_change_pct"].le(-0.5)
        )
        & ~frame["money_like"]
        & ~frame["early_60d"],
        "configured_high_suspicion": (
            (frame["integer_like"] & frame["share_change_pct"].gt(0))
            | frame["share_change_pct"].ge(2.0)
            | frame["share_change_pct"].le(-0.5)
        )
        & ~frame["money_like"]
        & ~frame["early_60d"],
    }
    rows: list[dict[str, object]] = []
    matched_total = int(frame["matched"].sum())
    for name, mask in scopes.items():
        rows.append(
            {
                "scope": name,
                "rows": int(mask.sum()),
                "funds": int(frame.loc[mask, "fund_code"].nunique()),
                "matched_kept": int((mask & frame["matched"]).sum()),
                "matched_total": matched_total,
                "money_rows": int((mask & frame["money_like"]).sum()),
                "early_60d_rows": int((mask & frame["early_60d"]).sum()),
            }
        )
    return pd.DataFrame(rows)


def _abs_pct_bins(frame: pd.DataFrame) -> pd.DataFrame:
    bins = [0.5, 1, 2, 3, 5, 10, 20, 999]
    labels = ["50-100%", "100-200%", "200-300%", "300-500%", "500-1000%", "1000-2000%", ">=2000%"]
    counts = pd.cut(frame["abs_pct"], bins=bins, labels=labels, right=False).value_counts().sort_index()
    return counts.reset_index(name="rows").rename(columns={"abs_pct": "abs_pct_bin"})


def _days_since_list_bins(frame: pd.DataFrame) -> pd.DataFrame:
    bins = [-999999, 0, 30, 60, 90, 180, 365, 730, 999999]
    labels = ["unknown/before", "0-30d", "30-60d", "60-90d", "90-180d", "180-365d", "1-2y", ">2y"]
    values = frame["days_since_list"].fillna(-999999)
    counts = pd.cut(values, bins=bins, labels=labels, right=False).value_counts().sort_index()
    return counts.reset_index(name="rows").rename(columns={"days_since_list": "days_since_list_bin"})


def _value_counts(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    values = frame[column].replace("", "未知").fillna("未知")
    return values.value_counts().reset_index(name="rows").rename(columns={column: "value"})


def _latest_audit_path(output_dir: Path) -> Path:
    paths = sorted(output_dir.glob("etf_lifecycle_share_jump_audit_*.csv"))
    if not paths:
        raise FileNotFoundError(f"no lifecycle audit CSV found under {output_dir}")
    return paths[-1]


def _markdown_table(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return "无。"
    columns = [str(column) for column in frame.columns]
    rows = [[_format_cell(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    separator = ["---"] * len(columns)
    return "\n".join(
        [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join(separator) + " |",
            *["| " + " | ".join(row) + " |" for row in rows],
        ]
    )


def _format_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("|", "/")


if __name__ == "__main__":
    raise SystemExit(main())
