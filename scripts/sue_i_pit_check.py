"""sue_i 穿越（PIT 边界）检验：T 日生效 vs T+1 交易日顺延
======================================================================
疑点：sue_i 事件在披露日 T 当天生效（searchsorted ed<=t）。若披露发生
在 T 日盘后，T 日收盘建仓即使用盘后信息 → 最多 1 天边界前视嫌疑
（audit PIT 对 pandas 因子 SKIP，本实验补上）。

方法：全局顺延 = 因子值日期 → 下一交易日（值不变）。对事件驱动因子，
这与"事件日顺延 1 交易日"精确等价。
  A 原版（T 日生效）
  B 顺延版（T+1 交易日起可用）
判读：B 的 IC 保持大部分 → 信号主体非边界穿越；B 崩塌 → 危险信号。
"""
import sys
sys.path.insert(0, '.')

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.data.store import Store
from quantlab.factor.library.earnings_surprise import SueI
from quantlab.factor.quality import factor_ic

store = Store()

# 1) 原版 sue_i
si = SueI()
fv = si.compute(store)
fv["date"] = pd.to_datetime(fv["date"])
fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
fv = fv.dropna(subset=["value"])
print(f"原版 sue_i: {len(fv)} 行，{fv['date'].min().date()} ~ {fv['date'].max().date()}")

# 2) T+1 顺延版：每个因子值的日期 → 下一交易日
all_td = pd.to_datetime(
    store.q("SELECT DISTINCT CAST(date AS DATE) AS d FROM kline_daily "
            "ORDER BY d")["d"]).to_numpy(dtype="datetime64[ns]")
pos = {d: i for i, d in enumerate(all_td)}
d_np = fv["date"].to_numpy(dtype="datetime64[ns]")
idx = np.array([pos.get(d, -1) for d in d_np])
ok = idx >= 0
next_d = np.where(ok & (idx < len(all_td) - 1),
                  all_td[np.clip(idx + 1, 0, len(all_td) - 1)],
                  np.datetime64("NaT"))
fv_shift = fv.copy()
fv_shift["date"] = pd.to_datetime(next_d)
fv_shift = fv_shift.dropna(subset=["date"])
print(f"顺延版 sue_i: {len(fv_shift)} 行")

# 3) 同一截面对比 IC（临时因子注入 registry 方式：直接算秩相关）
fwd = store.q("""
    SELECT date, code, c21/c1 - 1 AS fwd FROM (
        SELECT CAST(date AS DATE) AS date, code,
               LEAD(close*adj_factor, 1) OVER p AS c1,
               LEAD(close*adj_factor, 21) OVER p AS c21
        FROM kline_daily
        WINDOW p AS (PARTITION BY code ORDER BY date))
    WHERE c21 IS NOT NULL AND c1 > 0
""")
fwd["date"] = pd.to_datetime(fwd["date"])


def ic_of(df, tag):
    m = df.merge(fwd, on=["date", "code"], how="inner")
    ics = m.groupby("date").apply(
        lambda g: g["value"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()
    y = pd.DataFrame({"ic": ics})
    y["year"] = pd.DatetimeIndex(y.index).year
    yr = {int(k): round(float(v), 4) for k, v in y.groupby("year")["ic"].mean().items()}
    print(f"{tag:12s} IC20={ics.mean():+.4f} ICIR={ics.mean()/ics.std():+.3f} "
          f"win={(ics>0).mean():.2f} 分年={yr}", flush=True)
    return float(ics.mean())


ic_a = ic_of(fv, "原版(T生效)")
ic_b = ic_of(fv_shift, "顺延(T+1)")
print(f"\n保留率: {ic_b/ic_a:.1%}  "
      f"（>70% 信号主体非边界穿越；<40% 危险）")
