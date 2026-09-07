"""第二批因子落地：A3 隔夜/日内拆分(4) + A4 筹码(3)（计算 → 落库 → 自动审查）"""
import sys, time
sys.path.insert(0, '.')

from quantlab.factor.compute import compute_factor

NAMES = [
    "overnight_mom_20", "intraday_mom_5",
    "overnight_ratio_20", "overnight_vol_ratio_20",
    "chip_age_short_10", "chip_age_mid_60",
    "chip_vwap_bias_250",
]

t0 = time.time()
for name in NAMES:
    print(f"\n===== compute {name} =====", flush=True)
    df = compute_factor(name, save=True)
    print(f"{name}: {len(df)} rows, {df['date'].min()} ~ {df['date'].max()}, "
          f"{df['code'].nunique()} codes", flush=True)
print(f"\ntotal elapsed: {time.time()-t0:.0f}s")
