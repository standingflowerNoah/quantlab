"""dragon_net_20 审计第二步：当前表+当前SQL 重算 vs 因子库 现值 对比"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.factor.compute import compute_factor
from quantlab.data.store import query

CODE = "300522"

# 1) 当前口径重算（不落库）
fresh = compute_factor("dragon_net_20", save=False)
import pandas as pd
fresh["date"] = pd.to_datetime(fresh["date"])

print("=== 当前表+当前SQL 重算: %s 2026-06-08 ~ 06-26 ===" % CODE)
f = fresh[(fresh["code"] == CODE)
          & (fresh["date"] >= "2026-06-08") & (fresh["date"] <= "2026-06-26")]
print(f[["date", "value"]].to_string(index=False))

print()
print("=== 因子库现值（对照） ===")
lib = query(
    "SELECT date, value FROM read_parquet('data/lake/factor/dragon_net_20/*.parquet') "
    "WHERE code = ? AND date BETWEEN DATE '2026-06-08' AND DATE '2026-06-26' "
    "ORDER BY date", [CODE])
print(lib.to_string(index=False))

print()
print("=== dragon_tiger 水位与行数（确认表是否在因子计算后变动） ===")
print(query("SELECT * FROM watermarks WHERE domain = 'dragon_tiger'").to_string(index=False))
print(query("SELECT COUNT(*) AS n, MIN(date) AS mn, MAX(date) AS mx FROM dragon_tiger").to_string(index=False))
