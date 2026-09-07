"""分钟→日特征宽表（kline_1min 衍生的高频特征日频层）
=====================================================
一次扫描分钟湖，产出每股每日一行的微观结构特征宽表（minute_feat），
供高频因子（factor/library/highfreq.py）与研究复用。

设计要点（与文献/研报对齐）：
- 分钟收益 r_i = ln(close_i / close_{i-1})，**不跨日**（窗口按日分区，
  首 bar 收益为 NULL）→ 日内矩不含隔夜跳空
- 零成交 bar（价格全 0）剔除：WHERE close > 0
- bv（双幂次变差）= (π/2)·Σ|r_{i-1}||r_i|（Barndorff-Nielsen & Shephard，
  连续波动成分；jump = max(rv-bv, 0) 为跳跃成分）
- 已实现偏度/峰度在因子层由 sum_r3/sum_r4/rv/n_min 组合得出
- 聪明钱（开源金工/方正）：S_t = |r_t| / vol_t^0.25，按 S 降序累计量
  占比前 20% 的分钟为"聪明分钟"，smart_q = VWAP_smart / VWAP_all，
  分钟 VWAP 用 amount/vol（精确成交均价）而非收盘价加权
- 放量时刻（开源金工"待著而救"族）：按 vol 降序累计量占比前 20% 的
  分钟为"放量分钟"，topv_r = 放量分钟收益之和（放量上涨=主力资金
  流入，预期正向），topv_upr = 放量分钟中上涨分钟的量占比（量价
  配合的方向纯度，0.5 中性）
- 时段切分用 strftime('%H:%M') 字符串比较（10:00 / 11:30 / 13:00 / 14:30）

存储：data/lake/clean/minute_feat/part-<YYYY>.parquet（按年整文件覆盖，
幂等）；DuckDB 仅持视图 minute_feat（glob 动态）。
"""
from __future__ import annotations

import time

import pandas as pd

from .. import config
from ..config import get_logger
from .store import Store

log = get_logger(__name__)

FEAT_DIR = config.CLEAN_DIR / "minute_feat"

# 宽表列（显式清单：视图注册与自检共用）
FEAT_COLS = [
    "date", "code", "n_min",
    "rv", "bv", "rs_pos", "rs_neg", "sum_r3", "sum_r4", "abi",
    "v_open30", "v_close30", "v_am", "v_pm", "v_hhi", "v_top1",
    "r_first30", "r_last30", "r_am", "r_pm", "hi_pos", "lo_pos",
    "vwap", "amihud_min", "corr_rv", "day_range", "amt_sum",
    "smart_q", "topv_r", "topv_upr",
]


def _glob_1min() -> str:
    return str(config.KLINE_1MIN_DIR / "year=*" / "part-*.parquet").replace("\\", "/")


def _agg_sql(start: str | None = None, end: str | None = None) -> str:
    """分钟→日聚合 SQL（可选日期过滤，date 为闭区间）"""
    flt = ""
    if start:
        flt += f" AND CAST(datetime AS DATE) >= DATE '{start}'"
    if end:
        flt += f" AND CAST(datetime AS DATE) <= DATE '{end}'"
    return f"""
WITH m AS (
    SELECT code, CAST(datetime AS DATE) AS date,
           strftime(datetime, '%H:%M') AS tstr,
           ROW_NUMBER() OVER w AS bar_no,
           high, low, open, close, vol, amount,
           CASE WHEN close > 0 AND LAG(close) OVER w > 0
                THEN LN(close / LAG(close) OVER w) END AS r
    FROM read_parquet('{_glob_1min()}', hive_partitioning=false)
    WHERE close > 0{flt}
    WINDOW w AS (PARTITION BY code, CAST(datetime AS DATE) ORDER BY datetime)
),
b AS (
    SELECT code, date, tstr, bar_no, high, low, open, close, vol, amount, r,
           ABS(r) AS ar,
           ABS(r) * ABS(LAG(r) OVER wb) AS bv_term
    FROM m
    WINDOW wb AS (PARTITION BY code, date ORDER BY bar_no)
),
sm AS (
    SELECT code, date, vol, amount, ar, bar_no,
           SUM(vol) OVER (PARTITION BY code, date
               ORDER BY ar / POWER(GREATEST(vol, 1.0), 0.25) DESC, bar_no) AS cum_v,
           SUM(vol) OVER (PARTITION BY code, date) AS tot_v
    FROM b
    WHERE vol > 0 AND ar IS NOT NULL
),
smq AS (
    SELECT code, date,
           SUM(CASE WHEN cum_v <= 0.2 * tot_v OR cum_v - vol < 0.2 * tot_v
                    THEN amount END) AS sm_amt,
           SUM(CASE WHEN cum_v <= 0.2 * tot_v OR cum_v - vol < 0.2 * tot_v
                    THEN vol END) AS sm_vol
    FROM sm
    GROUP BY code, date
),
vp AS (   -- 放量时刻：按 vol 降序的累计量（top-20% 量分钟）
    SELECT code, date, r, vol,
           SUM(vol) OVER (PARTITION BY code, date
               ORDER BY vol DESC, bar_no) AS v_cum,
           SUM(vol) OVER (PARTITION BY code, date) AS v_tot
    FROM b
    WHERE vol > 0 AND r IS NOT NULL
),
vpq AS (
    SELECT code, date,
           SUM(CASE WHEN v_cum <= 0.2 * v_tot OR v_cum - vol < 0.2 * v_tot
                    THEN r END)                          AS topv_r,
           SUM(CASE WHEN v_cum <= 0.2 * v_tot OR v_cum - vol < 0.2 * v_tot
                    THEN vol END)                        AS topv_vol,
           SUM(CASE WHEN (v_cum <= 0.2 * v_tot OR v_cum - vol < 0.2 * v_tot)
                         AND r > 0 THEN vol ELSE 0 END)     AS topv_upvol
    FROM vp
    GROUP BY code, date
)
SELECT b.code, b.date,
    COUNT(*)                                        AS n_min,
    SUM(b.r * b.r)                                  AS rv,
    (PI() / 2.0) * SUM(b.bv_term)                   AS bv,
    SUM(CASE WHEN b.r > 0 THEN b.r * b.r END)       AS rs_pos,
    SUM(CASE WHEN b.r < 0 THEN b.r * b.r END)       AS rs_neg,
    SUM(b.r * b.r * b.r)                            AS sum_r3,
    SUM(b.r * b.r * b.r * b.r)                      AS sum_r4,
    AVG(b.ar)                                       AS abi,
    SUM(CASE WHEN b.tstr <= '10:00' THEN b.vol END) / NULLIF(SUM(b.vol), 0) AS v_open30,
    SUM(CASE WHEN b.tstr >= '14:30' THEN b.vol END) / NULLIF(SUM(b.vol), 0) AS v_close30,
    SUM(CASE WHEN b.tstr <= '11:30' THEN b.vol END) AS v_am,
    SUM(CASE WHEN b.tstr >= '13:00' THEN b.vol END) AS v_pm,
    SUM(b.vol * b.vol) / NULLIF(SUM(b.vol) * SUM(b.vol), 0)  AS v_hhi,
    MAX(b.vol) / NULLIF(SUM(b.vol), 0)              AS v_top1,
    arg_max(b.close, CASE WHEN b.tstr <= '10:00' THEN b.bar_no END)
        / NULLIF(arg_min(b.open, b.bar_no), 0) - 1.0           AS r_first30,
    arg_max(b.close, b.bar_no)
        / NULLIF(arg_max(b.close, CASE WHEN b.tstr <= '14:30' THEN b.bar_no END), 0) - 1.0 AS r_last30,
    arg_max(b.close, CASE WHEN b.tstr <= '11:30' THEN b.bar_no END)
        / NULLIF(arg_min(b.open, b.bar_no), 0) - 1.0           AS r_am,
    arg_max(b.close, b.bar_no)
        / NULLIF(arg_max(b.close, CASE WHEN b.tstr <= '13:00' THEN b.bar_no END), 0) - 1.0 AS r_pm,
    arg_max(b.bar_no, b.high) * 1.0 / COUNT(*)      AS hi_pos,
    arg_min(b.bar_no, b.low)  * 1.0 / COUNT(*)      AS lo_pos,
    SUM(b.amount) / NULLIF(SUM(b.vol), 0)           AS vwap,
    AVG(b.ar / NULLIF(b.amount, 0)) * 1e9           AS amihud_min,
    CORR(b.r, b.vol)                                AS corr_rv,
    MAX(b.high) - MIN(b.low)                        AS day_range,
    SUM(b.amount)                                   AS amt_sum,
    (smq.sm_amt / NULLIF(smq.sm_vol, 0))
        / NULLIF(SUM(b.amount) / NULLIF(SUM(b.vol), 0), 0)     AS smart_q,
    vpq.topv_r                                       AS topv_r,
    vpq.topv_upvol / NULLIF(vpq.topv_vol, 0)         AS topv_upr
FROM b
JOIN smq USING (code, date)
JOIN vpq USING (code, date)
GROUP BY b.code, b.date, smq.sm_amt, smq.sm_vol,
         vpq.topv_r, vpq.topv_upvol, vpq.topv_vol
"""


def build_minute_feat(years: list[int] | None = None,
                      replace: bool = True) -> dict:
    """构建分钟日特征宽表（按年分块，整文件覆盖写）

    返回 {year: rows}。years=None 自动发现分钟湖全部年份。
    """
    import duckdb

    if not config.KLINE_1MIN_DIR.exists():
        raise FileNotFoundError("分钟湖不存在，先运行 data minute-init")
    ys = years or sorted(
        int(p.name.split("=")[1]) for p in config.KLINE_1MIN_DIR.glob("year=*"))
    FEAT_DIR.mkdir(parents=True, exist_ok=True)

    out = {}
    con = duckdb.connect()          # 独立连接：不碰 quant.duckdb，无锁冲突
    try:
        for y in ys:
            f = FEAT_DIR / f"part-{y}.parquet"
            if f.exists() and not replace:
                log.info(f"minute_feat {y} 已存在，跳过")
                continue
            t0 = time.time()
            sql = _agg_sql(start=f"{y}-01-01", end=f"{y}-12-31")
            df = con.execute(sql).df()
            if df.empty:
                log.warning(f"minute_feat {y}: 无数据")
                continue
            df = df.sort_values(["date", "code"]).reset_index(drop=True)
            tmp = FEAT_DIR / f".part-{y}.parquet.tmp"
            df.to_parquet(tmp, index=False, compression="zstd")
            tmp.replace(f)
            out[y] = len(df)
            log.info(f"minute_feat {y}: {len(df):,} 行 × {len(df.columns)} 列，"
                     f"{time.time() - t0:.0f}s")
    finally:
        con.close()
    return out


def ensure_view(store: Store | None = None):
    """注册/刷新 minute_feat 视图"""
    store = store or Store()
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    glob_pat = str(FEAT_DIR / "part-*.parquet").replace("\\", "/")
    cols = ", ".join(c for c in FEAT_COLS)
    store.con.execute(f"""
        CREATE OR REPLACE VIEW minute_feat AS
        SELECT {cols}
        FROM read_parquet('{glob_pat}')
    """)
    store.register_dataset(
        "minute_feat", "clean", "kline_1min 衍生", "daily",
        "分钟→日特征宽表（日内波动率矩/量分布/时段收益/流动性/量价相关/"
        "聪明钱 Q；n_min<120 的行慎用）",
        ",".join(FEAT_COLS))
    store.set_watermark("minute_feat",
                        pd.Timestamp(pd.Timestamp.now().date()))


def feat_status() -> pd.DataFrame:
    """宽表覆盖统计"""
    import duckdb
    glob_pat = str(FEAT_DIR / "part-*.parquet").replace("\\", "/")
    try:
        con = duckdb.connect()
        df = con.execute(f"""
            SELECT CAST(regexp_replace(filename, '.*part-(\\d+)\\.parquet$', '\\1') AS INT) AS year,
                   COUNT(*) AS n_rows, COUNT(DISTINCT code) AS n_codes,
                   COUNT(DISTINCT date) AS n_days,
                   MIN(date) AS d_min, MAX(date) AS d_max
            FROM read_parquet('{glob_pat}', filename=true)
            GROUP BY 1 ORDER BY 1
        """).df()
        con.close()
        return df
    except Exception as e:
        return pd.DataFrame({"error": [str(e)]})
