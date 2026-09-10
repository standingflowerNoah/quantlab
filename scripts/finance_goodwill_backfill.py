#!/usr/bin/env python3
"""goodwill_snapshot 回填（东财商誉专题 RPT_GOODWILL_STOCKDETAILS，2026-09-10）

实测该报表含多期历史（2019 起每期 ~2600 家有商誉公司，无商誉公司不在表）。
每季度例行重拉即可覆盖新披露期。幂等 upsert（code, report_date 主键）。
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import get_logger
from quantlab.data.sources.eastmoney_source import em_get
from quantlab.data.store import Store

log = get_logger(__name__)
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def main() -> int:
    store = Store()
    store.ensure_table("goodwill_snapshot",
        "code VARCHAR, report_date DATE, notice_date DATE, goodwill DOUBLE, "
        "equity DOUBLE, ratio DOUBLE, fetched_at TIMESTAMP, "
        "PRIMARY KEY(code, report_date)")
    store.register_dataset("goodwill_snapshot", "clean", "eastmoney", "quarterly",
                           "商誉多期（东财商誉专题，有商誉公司）")
    rows, page, t0 = [], 1, time.time()
    while page <= 200:
        r = em_get(URL, params={"reportName": "RPT_GOODWILL_STOCKDETAILS",
                                "columns": "ALL", "pageSize": "500",
                                "pageNumber": str(page),
                                "sortColumns": "SECURITY_CODE", "sortTypes": "1"})
        data = (r.json().get("result") or {})
        chunk = data.get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.25)
    recs = []
    for x in rows:
        try:
            recs.append({"code": x["SECURITY_CODE"],
                         "report_date": pd.to_datetime(x.get("REPORT_DATE")),
                         "notice_date": pd.to_datetime(x.get("NOTICE_DATE")),
                         "goodwill": x.get("GOODWILL"),
                         "equity": x.get("SUMSHEQUITY"),
                         "ratio": x.get("SUMSHEQUITY_RATIO"),
                         "fetched_at": pd.Timestamp.now()})
        except Exception:
            continue
    df = (pd.DataFrame(recs)
          .dropna(subset=["code", "report_date"])
          .drop_duplicates(["code", "report_date"]))
    n = store.upsert(df, "goodwill_snapshot", ["code", "report_date"])
    log.info(f"goodwill_snapshot: {len(df)} 行 → {n} 写入 ({time.time()-t0:.0f}s)")
    return n


if __name__ == "__main__":
    main()
