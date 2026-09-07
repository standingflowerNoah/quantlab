"""第三批因子落地：A5 高/低位放量事件簇（等锁 → 计算 → 落库 → 自动审查）"""
import sys, time
sys.path.insert(0, '.')

import duckdb

from quantlab.config import DUCKDB_PATH
from quantlab.factor.compute import compute_factor

NAMES = ["ev_high_vol_20", "ev_low_vol_20"]

SMOKE = "--smoke" in sys.argv


def wait_for_lock(poll=90, max_wait=7200):
    """轮询等待 DuckDB 写锁释放（read_only 能连上即视为空闲）"""
    t0 = time.time()
    while True:
        try:
            con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
            con.close()
            return
        except Exception:
            if time.time() - t0 > max_wait:
                raise RuntimeError(f"DuckDB 写锁等待超时 {max_wait}s")
            print(f"[wait] DuckDB 被占用，{poll}s 后重试（已等 {time.time()-t0:.0f}s）", flush=True)
            time.sleep(poll)


wait_for_lock()

t0 = time.time()
for name in NAMES:
    print(f"\n===== compute {name} =====", flush=True)
    if SMOKE:
        df = compute_factor(name, save=False, end="2023-06-30")
        print(f"[smoke] {name}: {len(df)} rows, {df['date'].min()} ~ {df['date'].max()}, "
              f"{df['code'].nunique()} codes, value nulls={df['value'].isna().sum()}")
        print(df[df['value'].notna()].describe().to_string())
    else:
        df = compute_factor(name, save=True)
        print(f"{name}: {len(df)} rows, {df['date'].min()} ~ {df['date'].max()}, "
              f"{df['code'].nunique()} codes", flush=True)
print(f"\ntotal elapsed: {time.time()-t0:.0f}s")
