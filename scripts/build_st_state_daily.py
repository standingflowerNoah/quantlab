"""ST 历史 PIT 状态重建（全景规划批次 F-1）。

原料：data/lake/clean/namechange/part-all.parquet（tushare namechange，
8,419 条/1,676 只/2010 起，含 *ST/撤销ST/摘星/终止上市 全时间线）

产出：data/lake/clean/st_state_daily/part-all.parquet
  列：date, code, name, st_state（0=正常 / 1=ST / 2=*ST / 9=退市/终止）
  频率：仅状态变更日输出行（研究侧按 asof 前向填充展开到日历）

规则
----
- 每只股票的名称段 (start_date, end_date, name)：end_date 为空 = 至今生效
- st_state 按生效名称判定：*ST 前缀 → 2；ST 前缀（非 *ST）→ 1；
  名称含「退」或 change_reason=终止上市 → 9；其余 → 0
- 名称缺失日（股票存续但 namechange 无该段覆盖，如早年未改名股票从未入表）
  → 以 instruments/instruments_delisted 的当前名称兜底生成首段
用法：
  python scripts/build_st_state_daily.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake" / "clean"
OUT = LAKE / "st_state_daily" / "part-all.parquet"
LOG_FILE = ROOT / "logs" / "build_st_state_daily.log"


def st_state_of(name: str, reason: str | None = None) -> int:
    n = str(name or "")
    if n.startswith("*ST"):
        return 2
    if n.startswith("ST") or n.startswith("S*ST") or n.startswith("SST"):
        return 1
    if "退" in n or (reason and "终止上市" in str(reason)):
        return 9
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                                  logging.StreamHandler()])
    nc = pd.read_parquet(LAKE / "namechange" / "part-all.parquet")
    # 同名同起点的重复记录（不同 ann_date 的补录）去重；重叠段保留最新 ann
    nc = (nc.sort_values(["ts_code", "start_date", "ann_date"])
            .drop_duplicates(subset=["ts_code", "name", "start_date"], keep="last"))
    nc = nc.sort_values(["ts_code", "start_date"]).reset_index(drop=True)

    # 段有效性：end_date 为空 → 至今；段间空隙用前一段延续（保守）
    rows = []
    for ts, g in nc.groupby("ts_code"):
        g = g.sort_values("start_date").reset_index(drop=True)
        for i, r in g.iterrows():
            seg_end = r["end_date"] if pd.notna(r["end_date"]) else pd.Timestamp("2026-12-31")
            rows.append({
                "ts_code": ts, "code": r["code"],
                "start": r["start_date"], "end": seg_end,
                "name": r["name"], "reason": r["change_reason"],
            })
    seg = pd.DataFrame(rows)

    # 兜底：从未改名股票（namechange 无记录）→ 用清单当前名生成 2010 起的常段
    known = set(seg["code"])
    base = []
    for src, default_state in [("instruments_basic", 0), ("instruments_delisted", None)]:
        f = LAKE / src / "part-all.parquet"
        if not f.exists():
            continue
        d = pd.read_parquet(f)
        for _, r in d.iterrows():
            if r["code"] in known:
                continue
            state = st_state_of(r["name"])
            if default_state is None and state == 0 and "退" not in str(r["name"]):
                pass
            base.append({
                "ts_code": r["ts_code"], "code": r["code"],
                "start": pd.Timestamp("2010-01-01"),
                "end": pd.Timestamp("2026-12-31"),
                "name": r["name"], "reason": None,
            })
    if base:
        seg = pd.concat([seg, pd.DataFrame(base)], ignore_index=True)

    seg["st_state"] = [st_state_of(n, r) for n, r in zip(seg["name"], seg["reason"])]

    # 输出变更日行（start 日生效该状态）
    out = seg.rename(columns={"start": "date"})[
        ["date", "code", "ts_code", "name", "st_state", "reason"]]
    out = out.sort_values(["code", "date"]).reset_index(drop=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    logging.info("st_state_daily: %d 段 / %d 只 / %s ~ %s",
                 len(out), out["code"].nunique(),
                 out["date"].min().date(), out["date"].max().date())
    vc = out["st_state"].value_counts().sort_index()
    logging.info("状态分布: 0正常 %d | 1ST %d | 2*ST %d | 9退市 %d",
                 vc.get(0, 0), vc.get(1, 0), vc.get(2, 0), vc.get(9, 0))

    # 快速自检：抽 3 只已知股票验证
    for code, expect in [("000004", "ST→*ST 多段"), ("600291", "2022 退市"), ("600519", "全程正常")]:
        g = out[out["code"] == code]
        logging.info("自检 %s（%s）: %d 段 %s", code, expect, len(g),
                     g[["date", "name", "st_state"]].head(6).to_dict("records"))


if __name__ == "__main__":
    main()
