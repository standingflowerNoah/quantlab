"""fsdb 数据类别盘点：探测本地镜像实际存在哪些表/字段

只读探测，不写入任何数据。用法：
    python scripts/probe_fsdb_inventory.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.data.sources.fsdb_source import http_get   # noqa: E402

SAMPLE = "600519"


def probe(label: str, path: str, note: str = ""):
    try:
        r = http_get(path, timeout=15)
    except Exception as e:                                   # noqa: BLE001
        print(f"  {label:<24} ERR {type(e).__name__} {str(e)[:60]}")
        return None
    if isinstance(r, list):
        n = len(r)
        head = r[0] if n else None
        print(f"  {label:<24} list[{n}]  {str(head)[:150]}")
        return r
    if isinstance(r, dict):
        keys = list(r.keys())
        sample_k = keys[0] if keys else None
        v = r.get(sample_k) if sample_k else None
        print(f"  {label:<24} dict keys={keys[:8]}  样例[{sample_k}]={str(v)[:90]}")
        return r
    print(f"  {label:<24} {type(r).__name__} {str(r)[:100]}")
    return r


print("=" * 100)
print("A. 行情主表")
print("=" * 100)
probe("日k(单日)", f"/?cmd=vals&t=日k&k1=key:{SAMPLE}&k2=fwz:20260910,20260910")
probe("分钟k(单日)", f"/?cmd=vals&t=分钟k&k1=key:{SAMPLE}&k2=fwz:20260910093000,20260910150000")
probe("复权(因子)", f"/?cmd=vals&t=复权&k1=key:{SAMPLE}&k2=fwz:20260101,20260911")

print()
print("=" * 100)
print("B. 元数据 / 分类")
print("=" * 100)
probe("股票代码", "/?cmd=get&t=股票代码")
probe("交易日历", "/?cmd=vals&t=交易日历&k1=fwz:20260901,20260911")

print()
print("=" * 100)
print("C. 其他粒度（README 宣称的）")
print("=" * 100)
for t in ["周k", "月k", "1分钟k", "5分钟k", "15分钟k", "30分钟k", "60分钟k", "tick", "逐笔"]:
    probe(t, f"/?cmd=vals&t={t}&k1=key:{SAMPLE}&k2=fwz:20260901,20260911")

print()
print("=" * 100)
print("D. 退市 / 特殊证券")
print("=" * 100)
for t in ["退市", "退市股票", "退市股", "退市日k", "ST", "风险警示"]:
    probe(t, f"/?cmd=vals&t={t}&k1=key:400001&k2=fwz:2020*,N")

print()
print("=" * 100)
print("E. 板块 / 行业 / 概念")
print("=" * 100)
for t in ["板块", "概念板块", "申万", "申万一级", "申万二级", "申万三级",
          "行业", "概念", "指数", "指数成分"]:
    probe(t, f"/?cmd=get&t={t}")
    probe(f"  └ {t}(样本查询)", f"/?cmd=get&t={t}:{SAMPLE}")

print()
print("=" * 100)
print("F. ETF / 基金 / 债券")
print("=" * 100)
for t, k in [("日k", "510300"), ("分钟k", "510300"), ("日k", "159915"),
             ("ETF代码", None), ("基金代码", None), ("债券代码", None)]:
    path = (f"/?cmd=vals&t={t}&k1=key:{k}&k2=fwz:20260910,20260910"
            if k else f"/?cmd=get&t={t}")
    probe(f"{t}:{k or ''}", path)

print()
print("=" * 100)
print("G. 基本面 / 资金 / 其他（本地表名猜测）")
print("=" * 100)
for t in ["财务", "财务数据", "估值", "市值", "资金流", "资金流向", "龙虎榜",
          "两融", "融资融券", "解禁", "限售", "股东", "分红", "分红送转",
          "公告", "业绩预告"]:
    probe(t, f"/?cmd=get&t={t}")
