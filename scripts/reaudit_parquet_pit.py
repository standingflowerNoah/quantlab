"""重审 parquet 直读因子（验证 PIT 截断支持是否生效）

背景：105 个 registry 因子里 20 个用 read_parquet 直读湖文件，
旧实现下 PIT 检验整段跳过却被判 PASS（假 PASS）。
audit.py 2026-09-12 加了 parquet 数据集截断支持 + 状态判定修复。
本脚本等主库写锁后逐个重审，输出真实 PIT 结论。

用法：python scripts/reaudit_parquet_pit.py [--names a,b] [--wait-sec 1800]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PQ_FACTORS = ["accruals2", "bp", "cfp_ttm", "ep", "ep_z5", "garp", "gpoa",
              "gpoa_chg", "np_q_yoy", "ocf_to_profit", "op_margin", "rev_q_yoy",
              "roe_chg", "roe_cut", "roe_ttm", "roic", "sp_ttm", "sue_gpoa",
              "sue_v2", "sur"]


def wait_for_lock(max_sec: int, step: int = 15) -> bool:
    import duckdb
    from quantlab import config
    db = str(config.DUCKDB_PATH)
    t0 = time.time()
    n = 0
    while time.time() - t0 < max_sec:
        n += 1
        try:
            con = duckdb.connect(db)
            con.execute("SELECT 1").fetchone()
            con.close()
            print(f"[lock] 第 {n} 次拿到写锁（{time.time()-t0:.0f}s）", flush=True)
            return True
        except Exception:
            print(f"[lock] {n:>3d} 被占用，{step}s 后重试", flush=True)
            time.sleep(step)
    print(f"[lock] 超时 {max_sec}s", flush=True)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default="")
    ap.add_argument("--wait-sec", type=int, default=1800)
    args = ap.parse_args()
    names = [s for s in args.names.split(",") if s] or PQ_FACTORS

    if not wait_for_lock(args.wait_sec):
        sys.exit(2)

    from quantlab.factor.audit import audit_all
    df = audit_all(names=names, force=True)
    print("\n=== 重审汇总 ===", flush=True)
    print(df["verdict"].value_counts().to_string(), flush=True)

    import json
    print("\n=== 逐因子 PIT ===", flush=True)
    for n in names:
        p = Path("data/lake/factor_audit") / f"{n}.json"
        if not p.exists():
            print(f"  {n:16s} (无记录)")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        pit = (d.get("checks") or {}).get("pit") or {}
        print(f"  {n:16s} verdict={d.get('verdict'):5s} "
              f"pit={str(pit.get('status')):5s} "
              f"trunc={sorted((pit.get('truncated') or {}).keys())}")

    if len(df):
        bad = df[df["verdict"] == "FAIL"]
        if len(bad):
            print("\n--- FAIL 明细 ---", flush=True)
            print(bad[["factor", "issues"]].to_string(index=False), flush=True)
    print("\n[reaudit] DONE", flush=True)


if __name__ == "__main__":
    main()
