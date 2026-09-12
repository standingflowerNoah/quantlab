"""gpoa 族（逻辑变更后）WF 复验 + SUE 族（二期再入）有效性验证
=================================================================
背景（2026-09-12）：
  - gpoa_chg / sue_gpoa：按期锚定披露日改造后 rebuild + 重审 PASS，
    但「无穿越 ≠ 有效」，入合成候选前须重走 walk-forward（重算清单第九节开放项）。
  - sue_v2 / sur / roe_chg：v1 审计的 5.50% "弱泄漏" 经双口径复测证伪
    （kept 压制后面板坏行 0/186,696，raw 5.498% 为压制守护面）——
    无因子逻辑变更，无需 WF；本脚本顺带产出其"二期再入"有效性证据。

方法（项目既有 WF 口径，composite_ab_test.py 同款）：
  单因子 score（原值，五因子均为越高越好）→ split_backtest 样本内/外分段回测。
  切分点：2024-01-01 / 2025-01-01 / 2025-06-01（惯例点）。
  另附全窗口参照（2022-07-01 起，alpha191_fusion_test 惯例）。
  回测：top100 / 20 日调仓 / method=equal / 基准中证1000 / 扣费。

判定纪律（不设魔法阈值，报告数字+方向统计）：
  WF 通过 = 多数切分点样本外超额为正且 IR 未塌方；因子层 WF 只解决
  "可入合成候选"，入生产仍须组合层 A/B 终审（既有流程）。

输出：reports/wf_gpoa_sue_family_20260912.json + 控制台表。
只读因子湖；回测价格走主库 store（单进程串行，不并发）。
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb
import pandas as pd

from quantlab.model.walkforward import split_backtest
from quantlab.optimize.backtest import run_optimized_backtest

FACTORS = ["gpoa_chg", "sue_gpoa", "sue_v2", "sur", "roe_chg"]
SPLITS = ["2024-01-01", "2025-01-01", "2025-06-01"]
FULL_START = "2022-07-01"
N, REB = 100, 20
OUT = ROOT / "reports" / "wf_gpoa_sue_family_20260912.json"


def pack(res: dict) -> dict:
    m, e = res["metrics"], res["excess"]
    return {
        "annual_return": round(m["annual_return"] * 100, 2),
        "sharpe": round(m["sharpe"], 3),
        "max_drawdown": round(m["max_drawdown"] * 100, 2),
        "excess_annual": round(e["excess_annual"] * 100, 2),
        "information_ratio": round(e["information_ratio"], 3),
    }


def load_score(name: str) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT date, code, value
        FROM read_parquet('data/lake/factor/{name}/part-*.parquet',
                          union_by_name=true)
        WHERE value IS NOT NULL AND isfinite(value)
    """).fetchdf()
    con.close()
    # 引擎吃长表（date, code, score）
    df = df.rename(columns={"value": "score"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def main() -> None:
    t0 = time.time()
    out: dict = {}
    for name in FACTORS:
        print(f"\n===== {name} =====", flush=True)
        score = load_score(name)
        print(f"  score 行 {len(score):,}，"
              f"{score['date'].min()} ~ {score['date'].max()}", flush=True)
        entry: dict = {"data_start": score["date"].min(),
                       "data_end": score["date"].max()}

        # 全窗口参照
        res = run_optimized_backtest(score.copy(), n_stocks=N, rebalance=REB,
                                     method="equal", start=FULL_START)
        entry["full"] = pack(res)
        print(f"  full    年化 {entry['full']['annual_return']:+.1f}% / "
              f"夏普 {entry['full']['sharpe']} / 超额 "
              f"{entry['full']['excess_annual']:+.1f}%", flush=True)

        # 多切分点 in/out
        entry["walkforward"] = {}
        n_pos = 0
        for s in SPLITS:
            in_r, out_r = split_backtest(score.copy(), split_date=s,
                                         n_stocks=N, rebalance=REB,
                                         method="equal")
            entry["walkforward"][s] = {"in": pack(in_r), "out": pack(out_r)}
            ok = out_r["excess"]["excess_annual"] > 0
            n_pos += ok
            wi, wo = entry["walkforward"][s]["in"], entry["walkforward"][s]["out"]
            print(f"  wf[{s}]  内: 年化{wi['annual_return']:+.1f}%"
                  f"/IR{wi['information_ratio']:+.2f}  ||  "
                  f"外: 年化{wo['annual_return']:+.1f}%"
                  f"/超额{wo['excess_annual']:+.1f}%"
                  f"/IR{wo['information_ratio']:+.2f}"
                  f"  {'✅' if ok else '❌'}", flush=True)
        entry["splits_out_excess_positive"] = f"{n_pos}/{len(SPLITS)}"
        out[name] = entry
        del score

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n完成 {time.time()-t0:.0f}s → {OUT}", flush=True)


if __name__ == "__main__":
    main()
