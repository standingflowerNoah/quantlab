"""审计模块冒烟测试：
1) 修复版 dragon_net_20 → PIT 应 PASS
2) 故意用旧方向(反)SQL 的临时因子 → PIT 应 FAIL（验证审计能抓穿越）
3) size（引用 finance_snapshot）→ PASS + as-of 警告
"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.factor.base import SqlFactor
from quantlab.factor.registry import get_factor
from quantlab.factor.audit import audit_factor, pit_audit
from quantlab.factor.compute import compute_factor

# 1) 修复版 dragon
r1 = audit_factor("dragon_net_20", deep=True)
print(f"1) dragon_net_20: verdict={r1['verdict']} pit={r1['checks']['pit']['status']}")
print("   issues:", r1["issues"])

# 2) 旧方向 SQL（穿越版）——拷贝修复前的事件贡献区间写法
class DragonBroken(SqlFactor):
    name = "dragon_broken_test"
    description = "旧方向（穿越版）临时因子，仅用于验证 PIT 检验"
    category = "event"

    def _sql(self, start=None, end=None, universe=None):
        from quantlab.factor.base import universe_sql as uq
        usql, uparams = uq(universe)
        sql = f"""
        WITH ev AS (
            SELECT date, code, MAX(net_buy_wan) AS net
            FROM dragon_tiger GROUP BY date, code
        ),
        span AS (
            SELECT date, code, net,
                   CAST(date - INTERVAL 19 DAY AS DATE) AS s   -- ← 方向反（穿越）
            FROM ev
        ),
        hit AS (
            SELECT k.date, s.code, SUM(s.net) AS net20
            FROM span s
            JOIN kline_daily k
              ON k.code = s.code AND k.date BETWEEN s.s AND s.date
            GROUP BY k.date, s.code
        ),
        raw AS (
            SELECT k.date, k.code,
                   COALESCE(h.net20 / 1e4, 0)
                   / NULLIF(f.float_shares * k.close / 1e8, 0) AS value
            FROM kline_daily k
            LEFT JOIN hit h ON h.date = k.date AND h.code = k.code
            JOIN finance_snapshot f ON f.code = k.code
            WHERE k.close > 0 AND f.float_shares > 0 {usql}
        ),
        cal AS (
            SELECT trade_date AS d,
                   LEAD(trade_date) OVER (ORDER BY trade_date) AS next_d
            FROM trade_calendar
        )
        SELECT c.next_d AS date, r.code, r.value
        FROM raw r
        JOIN cal c ON c.d = r.date
        WHERE c.next_d IS NOT NULL
        """
        return sql, uparams

full = compute_factor("dragon_net_20", save=False)
r2 = pit_audit(DragonBroken(), full)
print(f"2) 旧方向(穿越版)SQL: pit={r2['status']}")
for d in r2["detail"]:
    if isinstance(d, dict):
        print(f"   t0={d['t0']} n_diff={d['n_diff']} sample={d['sample']}")

# 3) size（快照 as-of 警告路径）
r3 = audit_factor("size", deep=True)
print(f"3) size: verdict={r3['verdict']} pit={r3['checks']['pit']['status']}")
print("   issues:", r3["issues"])
