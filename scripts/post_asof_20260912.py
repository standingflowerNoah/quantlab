"""op_margin 新源重建 + 股本族 12 因子 audit 1.1 硬闸门重审（一次性收尾脚本）"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FACTORS = ["size", "total_mcap", "turnover", "turnover_std_20", "sp", "ocfp",
           "chip_vwap_bias_250", "dragon_net_20", "lockup_pressure_60",
           "ep", "bp", "op_margin"]

from quantlab.factor.compute import compute_factor
from quantlab.factor.audit import audit_all, AUDIT_VERSION
from quantlab import config

log = config.get_logger("post_asof")

print(f"audit_version = {AUDIT_VERSION}", flush=True)

# ① op_margin 全量重建（finance_q PIT 阶梯新源；rebuild=True 整年重写，
#    清除 finance_snapshot 单点口径的旧行，防口径混合）
print("=== ① op_margin 全量重建（新源） ===", flush=True)
df = compute_factor("op_margin", rebuild=True, save=True)
print(f"op_margin 重建: {len(df):,} 行 {df['date'].min().date()} ~ "
      f"{df['date'].max().date()}，{df['code'].nunique()} 只", flush=True)

# ② 12 因子 1.1 硬闸门重审（df 同步重算 + as-of 引用判 FAIL）
print("=== ② audit 1.1 重审 ===", flush=True)
res = audit_all(names=FACTORS, force=True)
cols = [c for c in ("factor", "verdict", "pit", "ic20", "codes_per_day_median")
        if c in res.columns]
print(res[cols].to_string(index=False), flush=True)
print("\n=== 汇总 ===", flush=True)
print(res["verdict"].value_counts().to_string(), flush=True)
fail = res[res["verdict"] == "FAIL"]
if len(fail):
    print("\n--- FAIL 明细 ---", flush=True)
    print(fail[["factor", "issues"]].to_string(index=False), flush=True)
print("\n[post_asof] DONE", flush=True)
