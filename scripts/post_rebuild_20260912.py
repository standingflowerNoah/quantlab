"""重建后收尾编排：等主库写锁 → 导出镜像 → PIT 审计 → 数据质量复核

背景：2026-09-12 主库与其他工作负载（fundamental_ab_test2 / 分钟回填）并发，
硬抢写锁会失败。故本脚本先轮询等锁，再串行做三件事。

用法：python scripts/post_rebuild_20260912.py [--wait-sec 900]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


FACTORS = ["size", "total_mcap", "turnover", "turnover_std_20", "sp", "ocfp",
           "chip_vwap_bias_250", "dragon_net_20", "lockup_pressure_60",
           "ep", "bp", "op_margin"]


def wait_for_lock(max_sec: int, step: int = 15) -> bool:
    """轮询直到能拿到主库写连接"""
    import duckdb
    from quantlab import config
    db = str(config.DUCKDB_PATH)
    t0 = time.time()
    tries = 0
    while time.time() - t0 < max_sec:
        tries += 1
        try:
            con = duckdb.connect(db)
            con.execute("SELECT 1").fetchone()
            con.close()
            print(f"[lock] 第 {tries} 次尝试拿到写锁（等待 {time.time()-t0:.0f}s）", flush=True)
            return True
        except Exception as e:
            print(f"[lock] {tries:>3d} 被占用（{type(e).__name__}），{step}s 后重试", flush=True)
            time.sleep(step)
    print(f"[lock] 超时 {max_sec}s，放弃", flush=True)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-sec", type=int, default=900)
    args = ap.parse_args()

    if not wait_for_lock(args.wait_sec):
        sys.exit(2)

    # ① 导出 Parquet 镜像（补 share_capital_daily）
    from quantlab.data.store import Store
    s = Store()
    out = s.export_mirror()
    print(f"[mirror] 已导出 {len(out)} 表；含 share_capital_daily="
          f"{'share_capital_daily' in out}", flush=True)
    s.close()

    # ② PIT 审计（深度，强制重算）
    from quantlab.factor.audit import audit_all
    df = audit_all(names=FACTORS, force=True)
    print("\n=== 审计汇总 ===", flush=True)
    print(df["verdict"].value_counts().to_string(), flush=True)
    fail = df[df["verdict"] == "FAIL"]
    if len(fail):
        print("\n--- FAIL 明细 ---", flush=True)
        print(fail[["factor", "issues"]].to_string(index=False), flush=True)
    warn = df[df["verdict"] == "WARN"]
    if len(warn):
        print("\n--- WARN 明细 ---", flush=True)
        print(warn[["factor", "issues"]].to_string(index=False), flush=True)

    # ③ HTML 报告
    sys.path.insert(0, str(Path("scripts").resolve()))
    import importlib.util
    spec = importlib.util.spec_from_file_location("fa", "scripts/factor_audit.py")
    fa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fa)
    fa.generate_report()

    print("\n[post] DONE", flush=True)


if __name__ == "__main__":
    main()
