"""财务快照（通达信 finance_info 37字段子集）+ instruments 行业/上市日期回填"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.tdx_source import TdxClient

log = get_logger(__name__)

FIN_DDL = """
code VARCHAR PRIMARY KEY, report_date DATE, report_period DATE, industry VARCHAR, province VARCHAR,
list_date DATE, float_shares DOUBLE, total_shares DOUBLE, total_assets DOUBLE,
current_assets DOUBLE, current_liab DOUBLE, long_term_liab DOUBLE,
net_assets DOUBLE, revenue DOUBLE, main_profit DOUBLE, operating_profit DOUBLE,
total_profit DOUBLE, net_profit_tax DOUBLE, net_profit DOUBLE,
undistributed DOUBLE, bvps DOUBLE, inventory DOUBLE, accounts_recv DOUBLE,
invest_income DOUBLE, operating_cf DOUBLE, total_cf DOUBLE, holder_num DOUBLE,
fetched_at TIMESTAMP
"""


def _report_period(disclosure_date) -> str | None:
    """披露日 → 推断的报告期末（YYYY-MM-DD）

    A 股披露窗口：年报 1-4月（期末上年12-31）、一季报 4月（03-31）、
    中报 7-9月（06-30）、三季报 10-12月（09-30）。
    注意 report_date 存的是通达信 updated_date（披露日），随每周重拉变化；
    report_period 才是稳定的报告期键，用于积累质量因子时序。
    """
    if not disclosure_date:
        return None
    m = int(str(disclosure_date)[5:7])
    y = int(str(disclosure_date)[:4])
    if 7 <= m <= 9:
        return f"{y}-06-30"          # 中报
    if 10 <= m <= 12:
        return f"{y}-09-30"          # 三季报
    if m <= 4:
        return f"{y - 1}-12-31"      # 年报
    return f"{y}-03-31"              # 一季报（5-6月披露滞后情形）


def init_financial(codes: list[str] | None = None):
    """全量拉取财务快照（一次 TCP 调用/只），并回填 instruments 行业/上市日期/股本"""
    store = Store()
    store.ensure_table("finance_snapshot", FIN_DDL)
    store.register_dataset("finance_snapshot", "clean", "tdx", "daily",
                           "财务快照（最新报告期）")
    if codes is None:
        from .instruments import all_codes
        codes = all_codes(st_only=False)
    cli = TdxClient.instance()
    rows = []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            row = cli.finance_info(code)
            if not row:
                continue
            rec = _map_finance(code, row)
            rows.append(rec)
        except Exception as e:
            log.debug(f"finance {code} 失败: {e}")
        if i % 1000 == 0:
            log.info(f"财务快照进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    if not rows:
        log.warning("财务快照为空")
        return 0
    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    store.replace_table("finance_snapshot", df, FIN_DDL)
    # 回填 instruments（上市日期 + 股本；行业文本由 backfill_industry() 负责，
    # 通达信 industry 为粗分数字代码，仅存 finance_snapshot 备查）
    backfill = df[df["list_date"].notna() | df["total_shares"].notna()][
        ["code", "total_shares", "float_shares", "list_date"]].copy()
    store.con.register("_bf", backfill)
    store.con.execute("""
        UPDATE instruments SET
            list_date = COALESCE((SELECT _bf.list_date::DATE FROM _bf WHERE _bf.code = instruments.code), list_date),
            total_shares = COALESCE((SELECT _bf.total_shares FROM _bf WHERE _bf.code = instruments.code), total_shares),
            float_shares = COALESCE((SELECT _bf.float_shares FROM _bf WHERE _bf.code = instruments.code), float_shares)
        WHERE code IN (SELECT code FROM _bf)""")
    store.con.unregister("_bf")
    store.set_watermark("finance_snapshot", pd.Timestamp.now().date())
    log.info(f"财务快照完成: {len(df)} 只, 行业回填 {len(backfill)} 只")
    return len(df)


def _map_finance(code: str, row: dict) -> dict:
    def g(k):
        v = row.get(k)
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    def _yyyymmdd(v):
        """YYYYMMDD 整数/字符串 → 'YYYY-MM-DD'（None 若无效）"""
        if v is None:
            return None
        s = str(int(v)) if not isinstance(v, str) else v
        return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else None

    # 上市日期（通达信 ipo_date 为 YYYYMMDD 整数）
    list_date = _yyyymmdd(g("ipo_date"))
    # 报告期：通达信 finance_info 无真实报告期字段，用 updated_date（数据更新日，
    # YYYYMMDD 整数）。注意不可直接用 pd.to_datetime(整数)——会被当纳秒时间戳
    report_date = _yyyymmdd(g("updated_date"))
    report_period = _report_period(report_date)
    # 注意：字段顺序必须与 FIN_DDL 完全一致（replace_table 按列名插入，
    # 但历史上有过 SELECT * 错位导致 DOUBLE→DATE 崩溃的教训，双保险对齐）
    return {
        "code": code,
        "report_date": report_date,
        "report_period": report_period,
        "industry": g("industry"),
        "province": g("province"),
        "list_date": list_date,
        "float_shares": g("liutongguben"),
        "total_shares": g("zongguben"),
        "total_assets": g("zongzichan"),
        "current_assets": g("liudongzichan"),
        "current_liab": g("liudongfuzhai"),
        "long_term_liab": g("changqifuzhai"),
        "net_assets": g("jingzichan"),
        "revenue": g("zhuyingshouru"),
        "main_profit": g("zhuyinglirun"),
        "operating_profit": g("yingyelirun"),
        "total_profit": g("lirunzonghe"),
        "net_profit_tax": g("shuihoulirun"),
        "net_profit": g("jinglirun"),
        "undistributed": g("weifenpeilirun"),
        "bvps": g("meigujingzichan"),
        "inventory": g("cunhuo"),
        "accounts_recv": g("yingshouzhangkuan"),
        "invest_income": g("touzishouyu"),
        "operating_cf": g("jingyingxianjinliu"),
        "total_cf": g("zongxianjinliu"),
        "holder_num": g("gudongrenshu"),
        "fetched_at": pd.Timestamp.now(),
    }
