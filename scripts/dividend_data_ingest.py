# -*- coding: utf-8 -*-
"""红利策略 Phase 0 数据入湖：红利指数日线 / 全收益 / 当前成分 / 十债收益率 / 分红送配明细

数据源：中证指数官网(csindex) + akshare bond_zh_us_rate + 东财 fhps（东财行情接口不通，仅用 fhps）
输出（Parquet = source of truth，DuckDB 视图待单写者空闲后注册）：
  data/lake/clean/index_kline_ext/part-{index}.parquet   红利指数日线 2013 起
  data/lake/clean/index_members_ext/dividend_cons.parquet 当前成分（快照，含 fetched_at）
  data/lake/clean/bond_yield/cgb.parquet                 国债收益率 2/5/10/30Y
  data/lake/clean/dividend_announce/part-{period}.parquet 分红送配明细（预案公告日/登记日/除权日）
用法：python scripts/dividend_data_ingest.py
"""
import sys, time, warnings
from pathlib import Path
import pandas as pd
import akshare as ak

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
FETCH_AT = pd.Timestamp.now().strftime("%Y-%m-%d")

INDEXES = {
    "000922": "中证红利",
    "H00922": "中证红利全收益",
    "H30269": "红利低波",
    "930955": "红利低波100",
}
REPORT_PERIODS = ["20201231", "20210630", "20211231", "20220630", "20221231",
                  "20230630", "20231231", "20240630", "20241231", "20250630", "20251231"]


def retry(fn, n=3, wait=3):
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            if i == n - 1:
                raise
            print(f"  retry {i+1}: {str(e)[:80]}")
            time.sleep(wait)


def save(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    print(f"saved {path} rows={len(df)}")


def fetch_index_kline():
    out = ROOT / "data/lake/clean/index_kline_ext"
    for code, name in INDEXES.items():
        df = retry(lambda: ak.stock_zh_index_hist_csindex(code, start_date="20130101", end_date="20991231"))
        df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
                                "收盘": "close", "成交量": "volume", "成交金额": "amount",
                                "样本数量": "sample_cnt", "滚动市盈率": "pe_ttm"})
        cols = ["date", "open", "high", "low", "close", "volume", "amount", "sample_cnt", "pe_ttm"]
        df = df[cols].copy()
        df["code"] = code
        df["name"] = name
        df["fetched_at"] = FETCH_AT
        df["date"] = pd.to_datetime(df["date"]).dt.date
        save(df, out / f"part-{code}.parquet")
        time.sleep(1)


def fetch_cons():
    frames = []
    for code in ["000922", "H30269", "930955"]:
        df = retry(lambda c=code: ak.index_stock_cons_csindex(c))
        df = df.rename(columns={"日期": "eff_date", "指数代码": "index_code", "指数名称": "index_name",
                                "成分券代码": "stock_code", "成分券名称": "stock_name", "交易所": "exchange"})
        df["fetched_at"] = FETCH_AT
        frames.append(df[["eff_date", "index_code", "index_name", "stock_code", "stock_name", "exchange", "fetched_at"]])
        time.sleep(1)
    save(pd.concat(frames, ignore_index=True), ROOT / "data/lake/clean/index_members_ext/dividend_cons.parquet")


def fetch_bond():
    df = retry(lambda: ak.bond_zh_us_rate(start_date="20130101"))
    df = df.rename(columns={"日期": "date", "中国国债收益率2年": "cgb_2y", "中国国债收益率5年": "cgb_5y",
                            "中国国债收益率10年": "cgb_10y", "中国国债收益率30年": "cgb_30y"})
    cols = ["date", "cgb_2y", "cgb_5y", "cgb_10y", "cgb_30y"]
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["fetched_at"] = FETCH_AT
    save(df, ROOT / "data/lake/clean/bond_yield/cgb.parquet")


def fetch_fhps():
    out = ROOT / "data/lake/clean/dividend_announce"
    for period in REPORT_PERIODS:
        try:
            df = retry(lambda p=period: ak.stock_fhps_em(date=p))
        except Exception as e:
            print(f"fhps {period} FAIL: {str(e)[:80]}")
            continue
        df = df.rename(columns={"代码": "code", "名称": "name", "现金分红-现金分红比例": "cash_per10",
                                "现金分红-股息率": "div_yield", "送转股份-送转总比例": "songzhuan_per10",
                                "预案公告日": "announce_date", "股权登记日": "record_date",
                                "除权除息日": "ex_date", "方案进度": "progress", "最新公告日期": "latest_date"})
        cols = ["code", "name", "cash_per10", "div_yield", "songzhuan_per10",
                "announce_date", "record_date", "ex_date", "progress", "latest_date"]
        df = df[cols].copy()
        df["report_period"] = period
        df["fetched_at"] = FETCH_AT
        for c in ["announce_date", "record_date", "ex_date", "latest_date"]:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
        save(df, out / f"part-{period}.parquet")
        time.sleep(1.5)


if __name__ == "__main__":
    steps = {"kline": fetch_index_kline, "cons": fetch_cons, "bond": fetch_bond, "fhps": fetch_fhps}
    todo = sys.argv[1:] or list(steps)
    for s in todo:
        print(f"=== {s} ===")
        steps[s]()
    print("ALL DONE")
