"""第一批新因子落地脚本：sue + earnings_accel（计算 → 落库 → 自动审查）"""
import sys, time
sys.path.insert(0, '.')

from quantlab.factor.compute import compute_factor

t0 = time.time()
for name in ["sue", "earnings_accel"]:
    print(f"\n===== compute {name} =====", flush=True)
    df = compute_factor(name, save=True)
    print(f"{name}: {len(df)} rows, {df['date'].min()} ~ {df['date'].max()}, "
          f"{df['code'].nunique()} codes", flush=True)
print(f"\ntotal elapsed: {time.time()-t0:.0f}s")
