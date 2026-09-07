"""SUE_I 探针：业绩预告 / 业绩快报接口历史覆盖实测"""
import sys
sys.path.insert(0, '.')

from quantlab.data.sources.eastmoney_source import em_get

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def probe(name, params, show_keys=20):
    try:
        r = em_get(URL, params=params)
        j = r.json()
        res = j.get("result") or {}
        rows = res.get("data") or []
        print(f"\n### {name}: count={res.get('count')}, rows={len(rows)}")
        if rows:
            print("字段:", ", ".join(list(rows[0].keys())[:show_keys]))
            for row in rows[:2]:
                print({k: v for k, v in list(row.items())[:show_keys]})
        return rows
    except Exception as e:
        print(f"\n### {name}: FAIL {e}")
        return []


# ── 业绩预告 ─────────────────────────────────────────────────
probe("业绩预告 某报告期全市场", {
    "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
    "filter": "(REPORT_DATE='2025-12-31')",
    "pageSize": "5", "pageNumber": "1",
    "sortColumns": "SECURITY_CODE", "sortTypes": "1",
})
probe("业绩预告 单股全历史(600519)", {
    "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
    "filter": '(SECURITY_CODE="600519")',
    "pageSize": "500", "pageNumber": "1",
    "sortColumns": "NOTICE_DATE", "sortTypes": "0",
})

# ── 业绩快报 ─────────────────────────────────────────────────
probe("业绩快报 某报告期全市场", {
    "reportName": "RPT_PUBLIC_OP_NEWQUICKY", "columns": "ALL",
    "filter": "(REPORT_DATE='2025-12-31')",
    "pageSize": "5", "pageNumber": "1",
})
probe("业绩快报 单股全历史(600519)", {
    "reportName": "RPT_PUBLIC_OP_NEWQUICKY", "columns": "ALL",
    "filter": '(SECURITY_CODE="600519")',
    "pageSize": "500", "pageNumber": "1",
})
