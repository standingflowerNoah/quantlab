"""dragon_net_20 前视审计：追查 Test A 异常样本的完整链路"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from quantlab.data.store import query

CODE = "300522"

print("=== 1) %s dragon_tiger 全记录 ===" % CODE)
print(query(
    "SELECT date, reason, net_buy_wan FROM dragon_tiger "
    "WHERE code = ? ORDER BY date", [CODE]).to_string(index=False))

print()
print("=== 2) %s 因子库 2026-06-10 ~ 06-22 ===" % CODE)
print(query(
    "SELECT date, value, COUNT(*) AS n "
    "FROM read_parquet('data/lake/factor/dragon_net_20/*.parquet') "
    "WHERE code = ? AND date BETWEEN DATE '2026-06-10' AND DATE '2026-06-22' "
    "GROUP BY date, value ORDER BY date", [CODE]).to_string(index=False))

print()
print("=== 3) 因子库重复 (date,code) 行（append 污染检查） ===")
print(query(
    "SELECT COUNT(*) AS dup_groups, SUM(c-1) AS dup_rows FROM ("
    "  SELECT date, code, COUNT(*) AS c"
    "  FROM read_parquet('data/lake/factor/dragon_net_20/*.parquet')"
    "  GROUP BY date, code HAVING COUNT(*) > 1)").to_string(index=False))

print()
print("=== 4) 因子库日期范围 ===")
print(query(
    "SELECT MIN(date) AS mn, MAX(date) AS mx, COUNT(*) AS n "
    "FROM read_parquet('data/lake/factor/dragon_net_20/*.parquet')"
).to_string(index=False))

print()
print("=== 5) 异常样本是否在 d0 前后紧邻日也有上榜（窗口重叠解释） ===")
# d-1 非零的另一解释：该股在 d0 之前 20 日内上过榜但 NOT EXISTS 语义漏掉
# （NOT EXISTS 检查的是"任何更早记录"，应已覆盖——除非 date 类型比较问题）
print(query(
    "SELECT date, reason, net_buy_wan FROM dragon_tiger "
    "WHERE code = ? AND date BETWEEN DATE '2026-05-25' AND DATE '2026-06-20' "
    "ORDER BY date", [CODE]).to_string(index=False))
