# -*- coding: utf-8 -*-
"""东财公告挂网时刻 vs westock InfoPublDate 批量对账。

问题：InfoPublDate（纯日期）与真实挂网时间的关系。
方法：抽样分层股票 -> 东财公告接口拉财务类公告（notice_date=官方公告日,
display_time=实际挂网时刻, 精确到秒）-> 与 finance_q 的 InfoPublDate 逐期对齐。

判定口径：
- info_gap_days = 实际挂网日 - InfoPublDate（天）。-1=前一晚挂网（保守）, 0=当天, +1=晚于记录日（数据漂移）。
- 真前视候选：挂网日 == InfoPublDate 且挂网时刻 >= 15:00（信息在记账收盘后才公开）。
- 反例关注：挂网日 > InfoPublDate（记录日早于真实可得 = 数据错误）。

只读本地 parquet + 外部 GET，不写湖、不碰主库。
"""
import json
import random
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
FQ_INC = str(ROOT / "data/lake/clean/fundamental/finance_q/income/part-*.parquet")
FQ_CF = str(ROOT / "data/lake/clean/fundamental/finance_q/cashflow/part-*.parquet")
OUT = ROOT / "reports/pit_fundamental"
OUT.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def em_fetch(code: str, pages: int = 2) -> list:
    rows = []
    for pg in range(1, pages + 1):
        url = (
            "https://np-anotice-stock.eastmoney.com/api/security/ann"
            f"?sr=-1&page_size=100&page_index={pg}&ann_type=A&client_source=web"
            f"&stock_list={code}&f_node=1&s_node=0"
        )
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                rows += json.load(r).get("data", {}).get("list", [])
        except Exception:
            break
        time.sleep(0.35)
    return rows


def title_patterns(end_date: str) -> list:
    """同一报告期的标题变体：季报有 一季度/第一季度 两种写法。"""
    y = end_date[:4]
    md = end_date[5:10]
    m = {
        "12-31": [f"{y}年年度报告"],
        "06-30": [f"{y}年半年度报告", f"{y}年中期报告"],
        "03-31": [f"{y}年第一季度报告", f"{y}年一季度报告"],
        "09-30": [f"{y}年第三季度报告", f"{y}年三季度报告"],
    }
    return m.get(md, [])


EXCLUDE = ("摘要", "英文", "提示", "说明会", "取消", "更正", "正文", "意见")


def main() -> None:
    con = duckdb.connect()
    ev = con.execute(
        f"""
        SELECT code, CAST(EndDate AS DATE) AS rp, CAST(InfoPublDate AS DATE) AS pub
        FROM (
            SELECT code, EndDate, InfoPublDate FROM read_parquet('{FQ_INC}', union_by_name=true)
            UNION ALL
            SELECT code, EndDate, InfoPublDate FROM read_parquet('{FQ_CF}', union_by_name=true)
        )
        WHERE InfoPublDate IS NOT NULL AND CAST(InfoPublDate AS DATE) >= '2024-06-01'
        GROUP BY code, CAST(EndDate AS DATE), CAST(InfoPublDate AS DATE)
        """
    ).fetchall()

    # 分层抽样：按代码前缀分桶，每桶均匀抽
    buckets = {}
    for code, rp, pub in ev:
        pre = code[:3]
        buckets.setdefault(pre, set()).add(code)
    random.seed(42)
    sample_codes = set()
    per_bucket = {p: 8 for p in buckets}
    for pre, codes in sorted(buckets.items()):
        cs = sorted(codes)
        random.shuffle(cs)
        sample_codes.update(cs[: per_bucket[pre]])
    # 茅台必含（已人工核验 5 期）
    sample_codes.add("600519")
    print(f"事件总数(2024H2+): {len(ev)}, 抽样股票: {len(sample_codes)}")

    by_code = {}
    for code, rp, pub in ev:
        if code in sample_codes:
            by_code.setdefault(code, []).append((rp, pub))

    recs, skipped = [], 0
    for i, code in enumerate(sorted(sample_codes)):
        anns = em_fetch(code)
        if not anns:
            skipped += 1
            continue
        # 清洗公告表：title -> [(notice_date, display_time)]
        cleaned = []
        for a in anns:
            t = (a.get("title") or "").replace(" ", "").replace(":", "")
            nd = (a.get("notice_date") or "")[:10]
            dt = (a.get("display_time") or "")[:19]
            if nd:
                cleaned.append((t, nd, dt))
        for rp, pub in sorted(set(by_code[code])):
            pats = title_patterns(str(rp))
            if not pats:
                continue
            lo = pub - timedelta(days=7)
            hi = pub + timedelta(days=7)
            cands = [
                (t, nd, dt)
                for t, nd, dt in cleaned
                if any(p in t for p in pats)
                and not any(x in t for x in EXCLUDE)
                and lo <= datetime.strptime(nd, "%Y-%m-%d").date() <= hi
            ]
            if not cands:
                recs.append({"code": code, "rp": str(rp), "pub": str(pub),
                             "matched": False})
                continue
            # 取 notice_date 最接近 pub 的
            t, nd, dt = min(cands, key=lambda x: abs((datetime.strptime(x[1], "%Y-%m-%d").date() - pub).days))
            rec = {"code": code, "rp": str(rp), "pub": str(pub), "matched": True,
                   "em_notice_date": nd, "em_display_time": dt}
            recs.append(rec)
            if dt:
                dtd = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                rec["publ_date"] = dtd.date().isoformat()
                rec["publ_hhmm"] = dtd.strftime("%H:%M")
                rec["info_gap_days"] = (dtd.date() - pub).days
                rec["same_day_after_close"] = (dtd.date() == pub) and (dtd.time() >= datetime.strptime("15:00", "%H:%M").time())
                rec["record_earlier_than_real"] = pub < dtd.date()
        if (i + 1) % 15 == 0:
            print(f"  ... {i + 1}/{len(sample_codes)}")
    print(f"skipped(接口空): {skipped}")

    matched = [r for r in recs if r.get("matched")]
    with_dt = [r for r in matched if r.get("publ_date")]
    gap_cnt = Counter(r.get("info_gap_days") for r in with_dt)
    hh = Counter(r["publ_hhmm"][:2] for r in with_dt)
    nd_equal = sum(1 for r in matched if r["em_notice_date"] == r["pub"])
    real_fv = [r for r in with_dt if r.get("same_day_after_close")]
    record_early = [r for r in with_dt if r.get("record_earlier_than_real")]

    summary = {
        "sample_codes": len(sample_codes),
        "events_total": len(recs),
        "matched": len(matched),
        "matched_with_display_time": len(with_dt),
        "em_notice_date_equals_infopubldate": nd_equal,
        "notice_equal_ratio": round(nd_equal / len(matched), 4) if matched else None,
        "info_gap_days_hist": dict(sorted(gap_cnt.items(), key=lambda x: (x[0] is None, x[0]))),
        "publ_hour_hist": dict(sorted(hh.items())),
        "true_lookahead_candidates_same_day_after_close": len(real_fv),
        "true_lookahead_samples": real_fv[:20],
        "record_earlier_than_real_count": len(record_early),
        "record_earlier_samples": record_early[:10],
    }
    (OUT / "em_timing_check.json").write_text(
        json.dumps({"summary": summary, "records": recs}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
