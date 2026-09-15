"""指数历史成分近似重建
=====================================
背景：东财 RPT_INDEX_TS_COMPONENT 仅返回当前成分（is_current，无历史），
中证官网月度权重文件（oss-ch closeweight）只保留最新一期——历史成分无法
直接获取（官方月度收盘权重仅授权信息商）。

本模块按中证规模指数编制规则**近似重建**历史月度成分：
- 样本空间：剔除 ST/北交所/上市不足 1 季度（近似）
- 流动性过滤：日均成交额后 10% 剔除
- 分层：沪深300=总市值前300；中证500=剔300后前500；中证1000=剔300+500后前1000；
  中证2000=再剔后前2000；上证50=沪市市值前50
- 调样频率简化为**每月末重建**（官方每半年调样+缓冲区，月度近似对回测足够）

精度：与当前官方成分对比重合率（模块内自检，写入表注释）。
PIT 口径（2026-09-12 升级）：
- 市值股本用 share_capital_daily ASOF（<= t 逐日真值，原 finance_snapshot
  当前股本回填历史 = as-of 未来数据，已废弃）；
- ST 过滤用 valuation_daily（fsdb 日线 is_st）按月动态判定，原 instruments
  当前 is_st 回填历史（历史上曾 ST 的股票被误留/现 ST 被误剔）已废弃；
- board/list_date 为静态属性（上市日期不变），合法保留。
声明：近似成分（市值排名法），不用于精确归因，
适合池过滤 / 暴露分析 / 分层选股场景。

表：index_members_hist(index_code, period, code, est_mcap_yi, mcap_rank)
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from .store import Store

log = get_logger(__name__)

DDL = """
index_code VARCHAR, period DATE, code VARCHAR,
est_mcap_yi DOUBLE, mcap_rank INTEGER,
PRIMARY KEY(index_code, period, code)
"""

# 指数定义：代码 → (构建规则, 市场)
LAYER = 300, 500, 1000, 2000   # 中证分层


def _period_ends(start: str = "2022-01-01") -> list[str]:
    """月末（近似：每月最后一个自然日，DuckDB 聚合按 <= 自然日即可）"""
    pr = pd.period_range(start, pd.Timestamp.now(), freq="M")
    return [p.end_time.strftime("%Y-%m-%d") for p in pr]


def rebuild_history(start: str = "2022-01-01") -> dict:
    """按月重建 300/500/1000/2000/上证50 历史成分"""
    store = Store()
    store.ensure_table("index_members_hist", DDL)
    store.register_dataset("index_members_hist", "clean", "internal", "monthly",
                           "指数历史成分（市值规则近似重建）")

    months = _period_ends(start)
    month_starts = [(pd.Timestamp(m) - pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d")
                    for m in months]
    lo = month_starts[0]
    hi = months[-1]

    # 月 × 股 的市值/成交额聚合（一次算全）
    # 分层口径：中证用自由流通市值——近似用流通市值（close × float_shares，
    # 无 float_shares 回退总股本）；总市值对高控股国企偏差过大
    # PIT：股本走 share_capital_daily ASOF（逐日真值），禁用 finance_snapshot
    df = store.q(f"""
        WITH base AS (
            SELECT date_trunc('month', k.date) AS m, k.code,
                   k.close, k.amount,
                   COALESCE(NULLIF(f.float_shares, 0), f.total_shares) AS shares
            FROM kline_daily k
            ASOF JOIN share_capital_daily f
              ON f.code = k.code AND k.date >= f.date
            WHERE k.date >= DATE '{lo}' AND k.date <= DATE '{hi}'
              AND k.close > 0
              AND COALESCE(NULLIF(f.float_shares, 0), f.total_shares) > 0
        )
        SELECT m AS month, code,
               AVG(close * shares) / 1e8 AS mcap_yi,
               AVG(amount) / 1e8 AS adv_yi
        FROM base
        GROUP BY m, code
    """)
    if df.empty:
        log.warning("指数重建：无可用数据")
        return {}
    # 剔除北交所/次新（board/list_date 为静态属性，历史适用，PIT 合法）
    # ST 过滤：按月动态判定（valuation_daily fsdb 日线 is_st，月内任一日
    # is_st=true 即剔除该月）——instruments.is_st 是当前状态，回填历史
    # 属 as-of 未来数据，2026-09-12 起禁用
    import os
    _vd = os.environ.get(
        "QUANTLAB_LAKE",
        "C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean")
    st = store.q(f"""
        SELECT code, date_trunc('month', date) AS m,
               MAX(CAST(is_st AS INTEGER)) AS st
        FROM read_parquet('{_vd}/fundamental/valuation_daily/**/*.parquet',
                          union_by_name=true)
        WHERE date >= DATE '{lo}' AND date <= DATE '{hi}'
        GROUP BY code, m
    """)
    st["m"] = pd.to_datetime(st["m"]).dt.date
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df = df.merge(st, on=["code", "month"], how="left")
    df = df[df["st"].fillna(0) == 0].drop(columns=["st"])
    ok = store.q("""
        SELECT code FROM instruments
        WHERE board != 'BJ'
          AND (list_date IS NULL OR
               list_date <= (SELECT MAX(date) - INTERVAL 90 DAY FROM kline_daily))
    """)
    okset = set(ok["code"])
    df = df[df["code"].isin(okset)]

    # 近 6 个月滚动日均（贴近官方半年考察期；前 5 个月用可用窗口）
    df = df.sort_values(["code", "month"])
    g = df.groupby("code")
    df["mcap_yi"] = g["mcap_yi"].transform(
        lambda s: s.rolling(6, min_periods=1).mean())
    df["adv_yi"] = g["adv_yi"].transform(
        lambda s: s.rolling(6, min_periods=1).mean())

    rows = []
    stats = {}
    for month, g in df.groupby("month"):
        # 流动性过滤：日均成交额后 10% 剔除
        adv_cut = g["adv_yi"].quantile(0.10)
        pool = g[g["adv_yi"] > adv_cut].copy()
        pool = pool.sort_values("mcap_yi", ascending=False)
        pool["mcap_rank"] = range(1, len(pool) + 1)
        r = pool["mcap_rank"]
        # 上证50：沪市池内单独排名（全市场前50中深市占比高，不能直接筛前缀）
        sh = pool[pool["code"].str.startswith("60")].copy()
        sh["sh_rank"] = range(1, len(sh) + 1)
        layers = {
            "hs300": pool[r <= 300],                       # 沪深两市前300
            "zz500": pool[(r > 300) & (r <= 800)],
            "zz1000": pool[(r > 800) & (r <= 1800)],
            "zz2000": pool[(r > 1800) & (r <= 3800)],
            "sz50": sh[sh["sh_rank"] <= 50],               # 沪市前50
        }
        pd_date = pd.Timestamp(month).date()
        for idx, seg in layers.items():
            for _, row in seg.iterrows():
                rows.append({"index_code": idx, "period": pd_date,
                             "code": row["code"],
                             "est_mcap_yi": round(row["mcap_yi"], 2),
                             "mcap_rank": int(row["mcap_rank"])})
        stats[str(pd_date)] = {k: len(v) for k, v in layers.items()}

    out = pd.DataFrame(rows)
    store.con.execute("DELETE FROM index_members_hist")   # 全量重建语义（口径升级后避免残留）
    n = store.upsert(out, "index_members_hist", ["index_code", "period", "code"])
    store.set_watermark("index_members_hist", pd.Timestamp.now().date())
    last = out[out["period"] == out["period"].max()]
    log.info(f"指数历史成分重建: {out['period'].nunique()} 个月 × 5 指数 → {n} 行；"
             f"最新月规模: {stats[max(stats)]}")
    return {"rows": n, "months": out["period"].nunique(),
            "latest": stats[max(stats)]}


def get_hist_members(index_code: str, date=None) -> list[str]:
    """取某指数在 date（或最近）时点的近似成分列表"""
    store = Store()
    if date is not None:
        df = store.q(
            "SELECT code FROM index_members_hist "
            "WHERE index_code = ? AND period <= ? "
            "ORDER BY period DESC LIMIT ("
            "  SELECT COUNT(*) FROM index_members_hist"
            "  WHERE index_code = ? AND period = ("
            "    SELECT MAX(period) FROM index_members_hist"
            "    WHERE index_code = ? AND period <= ?))",
            [index_code, pd.Timestamp(date).date(), index_code,
             index_code, pd.Timestamp(date).date()])
    else:
        df = store.q("""
            SELECT code FROM index_members_hist
            WHERE index_code = ? AND period = (SELECT MAX(period) FROM index_members_hist)
        """, [index_code])
    return sorted(df["code"].tolist())


def accuracy_check() -> pd.DataFrame:
    """精度自检：重建的最新月成分 vs 东财当前官方成分的重合率"""
    store = Store()
    out = []
    for idx, official in [("hs300", "hs300"), ("sz50", "sz50"),
                          ("zz500", "zz500"), ("zz1000", "zz1000")]:
        try:
            off = store.q(
                "SELECT code FROM index_members WHERE index_code = ? AND is_current",
                [official])
            est = store.q("""
                SELECT code FROM index_members_hist
                WHERE index_code = ? AND period = (SELECT MAX(period) FROM index_members_hist)
            """, [idx])
            if off.empty or est.empty:
                out.append({"index": idx, "official": len(off),
                            "rebuilt": len(est), "overlap": None, "ratio": None})
                continue
            o, e = set(off["code"]), set(est["code"])
            ov = len(o & e)
            out.append({"index": idx, "official": len(o), "rebuilt": len(e),
                        "overlap": ov, "ratio": round(ov / max(len(o), 1), 3)})
        except Exception as ex:
            out.append({"index": idx, "error": str(ex)[:60]})
    return pd.DataFrame(out)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    res = rebuild_history()
    print(res)
    print(accuracy_check().to_string(index=False))
