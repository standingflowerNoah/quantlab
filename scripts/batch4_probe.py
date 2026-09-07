"""第四批探针：东财 datacenter 股东户数 + 一致预期接口真实覆盖实测"""
import sys
sys.path.insert(0, '.')

from quantlab.data.sources.eastmoney_source import em_get

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def probe(name, params):
    try:
        r = em_get(URL, params=params)
        j = r.json()
        res = j.get("result") or {}
        rows = res.get("data") or []
        print(f"\n### {name}: count={res.get('count')}, rows={len(rows)}")
        if rows:
            print("字段:", ", ".join(list(rows[0].keys())[:25]))
            for row in rows[:2]:
                print({k: v for k, v in list(row.items())[:18]})
        return rows
    except Exception as e:
        print(f"\n### {name}: FAIL {e}")
        return []


# ── B1 股东户数 ──────────────────────────────────────────────
probe("股东户数 全市场某截止日", {
    "reportName": "RPT_HOLDERNUM_DET", "columns": "ALL",
    "filter": "(END_DATE='2024-12-31')",
    "pageSize": "5", "pageNumber": "1",
    "sortColumns": "SECURITY_CODE", "sortTypes": "1",
})
rows = probe("股东户数 单股全历史(000001)", {
    "reportName": "RPT_HOLDERNUM_DET", "columns": "ALL",
    "filter": '(SECURITY_CODE="000001")',
    "pageSize": "500", "pageNumber": "1",
    "sortColumns": "END_DATE", "sortTypes": "0",
})
if rows:
    dates = sorted({str(r.get("END_DATE", ""))[:10] for r in rows})
    print(f"历史深度: {dates[0]} ~ {dates[-1]}, 共 {len(dates)} 期")

# ── B2 一致预期（盈利预测）────────────────────────────────────
probe("盈利预测汇总 单股", {
    "reportName": "RPT_WEB_RESPREDICT", "columns": "ALL",
    "filter": '(SECURITY_CODE="600519")',
    "pageSize": "10", "pageNumber": "1",
})
probe("研报盈利预测明细 单股近期", {
    "reportName": "RPT_EARNINGS_FORECAST", "columns": "ALL",
    "filter": '(SECURITY_CODE="600519")',
    "pageSize": "5", "pageNumber": "1",
})
