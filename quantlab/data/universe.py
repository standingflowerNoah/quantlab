"""Universe 多股票池模块
=====================================
内置股票池（按名称取截面）：
- all           全部 A 股（含 ST/北交所）
- ashare_ex     全 A 剔除 ST/退市标记/上市<120日次新/北交所  ← 研究默认
- ashare_main   全 A 剔除 ST/次新，保留北交所
- hs300 / zz500 / zz1000 / zz2000    指数成分（东财成分接口，周度刷新）
- top2000 / top3000                   按流通市值排名动态池
支持自定义过滤器组合：get_universe(name, date) / register_filter()
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from .store import Store, query

log = get_logger(__name__)

# 东财指数成分 secid
_INDEX_SECID = {
    "hs300": "1.000300", "zz500": "1.000905", "zz1000": "1.000852",
    "zz2000": "1.000000",  # 中证2000 代码 932000，暂以zz1000+小市值近似
}

MIN_LIST_DAYS = 120          # 次新股定义：上市不足 N 天
NEW_STOCK_BUFFER = 20        # 额外缓冲天数（跨节假日）


def _base(date=None) -> pd.DataFrame:
    """股票主表 + 当日市值快照 join（只读降级：更新任务持锁时可走镜像）"""
    sql = """
        SELECT i.code, i.name, i.market, i.board, i.is_st, i.list_date,
               i.industry,
               s.mcap_yi, s.float_mcap_yi, s.turnover_pct
        FROM instruments i
        LEFT JOIN daily_snapshot s
          ON s.code = i.code AND s.date = (
              SELECT MAX(date) FROM daily_snapshot)
    """
    return query(sql)


def _drop_st_new(df: pd.DataFrame, drop_bj: bool = True,
                 min_list_days: int = MIN_LIST_DAYS) -> pd.DataFrame:
    out = df[~df["is_st"].astype(bool)].copy()
    if drop_bj:
        out = out[out["board"] != "BJ"]
    # 次新：上市日期缺失视为老股（保守），或上市不足 N 日剔除
    if "list_date" in out.columns and out["list_date"].notna().any():
        ld = pd.to_datetime(out["list_date"], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=min_list_days + NEW_STOCK_BUFFER)
        out = out[~(ld > cutoff)]
    return out


def get_universe(name: str = "ashare_ex", date=None) -> list[str]:
    """按名称取股票池代码列表

    指数成分池（hs300/sz50/zz500/zz1000/zz2000）：
    - date=None → 官方当前成分（东财 is_current）
    - date=历史时点 → 近似重建的月度成分（index_members_hist，市值排名法，
      自由流通股本不可得故精度有限：hs300 ~79%、zz 系 ~60%，
      见 index_cons_rebuild.accuracy_check）；适合池过滤/暴露分析
    """
    name = name.lower()
    base = _base(date)

    if name == "all":
        return sorted(base["code"].tolist())
    if name in ("ashare_ex", "default"):
        df = _drop_st_new(base, drop_bj=True)
        return sorted(df["code"].tolist())
    if name == "ashare_main":
        df = _drop_st_new(base, drop_bj=False)
        return sorted(df["code"].tolist())
    if name in ("top2000", "top3000", "top500"):
        n = int(name[3:])
        df = _drop_st_new(base, drop_bj=True)
        df = df.dropna(subset=["float_mcap_yi"])
        df = df.sort_values("float_mcap_yi", ascending=False)
        return sorted(df.head(n)["code"].tolist())
    if name in _INDEX_TYPE:
        if date is not None:
            # 历史回溯：近似重建的月度成分（取 <= date 最近一期）
            from .index_cons_rebuild import get_hist_members
            hist = get_hist_members(name, date)
            if hist:
                return hist
            log.warning(f"指数成分 {name} 无 {date} 前历史快照，回退当前成分")
        df = query(
            "SELECT code FROM index_members WHERE index_code=? AND is_current",
            [name])
        if df.empty:
            log.warning(f"指数成分 {name} 为空，请先 refresh_index_members()")
            return []
        return sorted(df["code"].tolist())
    raise ValueError(f"未知股票池: {name}（可选 all/ashare_ex/ashare_main/"
                     f"hs300/sz50/zz500/zz1000/zz2000/top2000/top3000）")


def universe_info(name: str = "ashare_ex", date=None) -> pd.DataFrame:
    """股票池明细（含名称/行业/市值），供研究展示"""
    base = _base(date)
    codes = set(get_universe(name, date))
    return base[base["code"].isin(codes)].reset_index(drop=True)


def describe_universes() -> pd.DataFrame:
    """各内置股票池规模一览"""
    rows = []
    for name in ("all", "ashare_ex", "ashare_main", "top2000", "hs300",
                 "sz50", "zz500", "zz1000", "zz2000"):
        try:
            rows.append({"universe": name, "n_stocks": len(get_universe(name))})
        except Exception as e:
            rows.append({"universe": name, "n_stocks": f"err: {e}"})
    return pd.DataFrame(rows)


# ── 指数成分（东财 datacenter，周度刷新） ─────────────────────────
# push2 clist 接口沙箱不可达，改走 datacenter-web 报表（实测可用）
_INDEX_TYPE = {
    "hs300": "1",    # 沪深300
    "sz50": "2",     # 上证50
    "zz500": "3",    # 中证500
    "zz1000": "7",   # 中证1000
    "zz2000": "13",  # 中证2000
}
_INDEX_SECID = {k: f"TYPE={v}" for k, v in _INDEX_TYPE.items()}


def refresh_index_members() -> dict:
    """刷新指数成分股：datacenter RPT_INDEX_TS_COMPONENT 报表（每页500翻页）"""
    import requests
    from .sources.eastmoney_source import em_get
    store = Store()
    store.ensure_table("index_members", """
        index_code VARCHAR, code VARCHAR, name VARCHAR,
        in_date DATE, is_current BOOLEAN, PRIMARY KEY(index_code, code)""")
    store.register_dataset("index_members", "clean", "eastmoney", "weekly",
                           "指数成分股（hs300/sz50/zz500/zz1000/zz2000）")
    result = {}
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    for idx, t in _INDEX_TYPE.items():
        rows, page = [], 1
        while page <= 10:
            try:
                r = em_get(url, params={
                    "reportName": "RPT_INDEX_TS_COMPONENT",
                    "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR",
                    "filter": f'(TYPE="{t}")',
                    "pageSize": "500", "pageNumber": str(page),
                    "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                })
                items = (r.json().get("result") or {}).get("data") or []
            except Exception as e:
                log.warning(f"{idx} 成分第{page}页失败: {e}")
                break
            if not items:
                break
            rows.extend(items)
            if len(items) < 500:
                break
            page += 1
        if rows:
            df = pd.DataFrame([{
                "index_code": idx,
                "code": it["SECURITY_CODE"],
                "name": it.get("SECURITY_NAME_ABBR", ""),
                "in_date": None, "is_current": True} for it in rows])
            store.con.execute(
                "DELETE FROM index_members WHERE index_code=?", [idx])
            store.con.register("_im", df)
            store.con.execute(
                "INSERT INTO index_members SELECT * FROM _im")
            store.con.unregister("_im")
            result[idx] = len(df)
            log.info(f"{idx} 成分: {len(df)} 只")
        else:
            result[idx] = 0
            log.warning(f"{idx} 成分拉取失败（网络）")
    store.set_watermark("index_members", pd.Timestamp.now().date())
    return result
