# -*- coding: utf-8 -*-
"""PandaAI 因子中心全量拉取（需登录 token）。

用法（凭据经环境变量注入，不落盘）:
  PB_PHONE=... PB_PWD=... python panda_pull/pull_factors.py

产出:
  reports/pandaaiquant_factor_center/panda_factors_all.json   原始行（4 个回测区间）
  reports/pandaaiquant_factor_center/panda_factors_all.csv    合并宽表（因子 x 区间）
  reports/pandaaiquant_factor_center/panda_factors_ic_mean_ge_003.csv  默认口径(全A/近一年) IC_MEAN>=0.03
"""
import csv
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "https://www.pandaaiquant.com/pandaApi"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "pandaaiquant_factor_center")
PERIODS = ["ONE_YEAR", "HALF_YEAR", "TWO_YEARS", "THREE_YEARS"]
PERIOD_CN = {"HALF_YEAR": "近半年", "ONE_YEAR": "近一年", "TWO_YEARS": "近两年", "THREE_YEARS": "近三年"}
MARKET = "A"          # 全A股（站点默认）
PAGE_SIZE = 200       # UI 用 30；接口未测上限，200 先试，失败自动降
THRESH = 0.03         # IC_MEAN 阈值


def post(path, payload, token=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
        "Origin": "https://www.pandaaiquant.com",
        "Referer": "https://www.pandaaiquant.com/quantfactor-center",
    }
    if token:
        headers["Authorization"] = token
    if "/login/" in path or "/sms/" in path:
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def login(phone, pwd):
    md5pwd = hashlib.md5(pwd.encode("utf-8")).hexdigest()
    last_err = None
    for cc in ("86", "+86"):
        try:
            resp = post("/login/pw", {"countryCode": cc, "password": md5pwd, "phone": phone})
            if resp.get("code") == "200" and resp.get("data"):
                print(f"[login] ok (countryCode={cc})")
                return resp["data"]
            last_err = f"code={resp.get('code')} msg={resp.get('message')}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise SystemExit(f"[login] FAILED: {last_err}")


def pull_period(token, period):
    rows, page = [], 1
    while True:
        payload = {"keyword": "", "stockPool": MARKET, "backtestPeriod": period,
                   "sortField": "IC_MEAN", "sortOrder": "DESC", "category": "",
                   "page": page, "pageSize": PAGE_SIZE}
        resp = post("/factorCenter/getQuantFactorCenterData", payload, token=token)
        if resp.get("code") != "200":
            raise SystemExit(f"[pull {period}] code={resp.get('code')} msg={resp.get('message')}")
        d = resp.get("data") or {}
        lst = d.get("list") or d.get("records") or d.get("rows") or []
        total = d.get("total") or len(rows) + len(lst)
        rows.extend(lst)
        print(f"[pull {period}] page {page}: +{len(lst)} (accum {len(rows)}/{total})")
        if not lst or len(rows) >= total:
            return rows, total
        page += 1
        time.sleep(0.3)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    phone, pwd = os.environ.get("PB_PHONE"), os.environ.get("PB_PWD")
    if not phone or not pwd:
        raise SystemExit("need PB_PHONE / PB_PWD env")
    token = login(phone, pwd)

    os.makedirs(OUT_DIR, exist_ok=True)
    all_data, totals = {}, {}
    for p in PERIODS:
        rows, total = pull_period(token, p)
        all_data[p] = rows
        totals[p] = total
        time.sleep(0.4)

    with open(os.path.join(OUT_DIR, "panda_factors_all.json"), "w", encoding="utf-8") as f:
        json.dump({"market": MARKET, "totals": totals, "data": all_data}, f,
                  ensure_ascii=False)

    # 打印一行样本键名便于核对
    sample = next((r for r in all_data["ONE_YEAR"] if r), None)
    if sample:
        print("[sample keys]", sorted(sample.keys()))
        print("[sample row]", json.dumps(sample, ensure_ascii=False)[:500])

    # 合并宽表：按唯一键（优先 code/factorCode，退回 name）
    def key_of(r):
        return r.get("factorCode") or r.get("code") or r.get("name")

    merged = {}
    for p in PERIODS:
        for r in all_data[p]:
            k = key_of(r)
            if k is None:
                continue
            m = merged.setdefault(k, {"name": r.get("name") or r.get("factorName") or k,
                                      "category": r.get("category") or r.get("categoryName") or ""})
            m[f"ic_mean_{p}"] = num(r.get("icMean"))
            m[f"rank_ic_{p}"] = num(r.get("rankIc"))
            m[f"ic_ir_{p}"] = num(r.get("icIr"))
            m[f"ic_std_{p}"] = num(r.get("icStd"))
            m[f"start_{p}"] = r.get("startDate") or ""
            m[f"update_{p}"] = r.get("dataDate") or r.get("updateDate") or ""

    cols = ["name", "category"]
    for p in PERIODS:
        cols += [f"ic_mean_{p}", f"rank_ic_{p}", f"ic_ir_{p}", f"ic_std_{p}"]
    cols += [f"start_{p}" for p in PERIODS] + [f"update_{p}" for p in PERIODS]

    all_csv = os.path.join(OUT_DIR, "panda_factors_all.csv")
    with open(all_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for k in sorted(merged):
            w.writerow({c: merged[k].get(c, "") for c in cols})
    print(f"[out] all factors: {len(merged)} -> {all_csv}")

    # 筛选：站点默认口径（全A / 近一年）IC_MEAN>=0.03
    hit = [m for m in merged.values()
           if (m.get("ic_mean_ONE_YEAR") or -9) >= THRESH]
    hit.sort(key=lambda m: -(m["ic_mean_ONE_YEAR"] or 0))
    hit_csv = os.path.join(OUT_DIR, "panda_factors_ic_mean_ge_003.csv")
    with open(hit_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for m in hit:
            w.writerow({c: m.get(c, "") for c in cols})
    print(f"[out] IC_MEAN>={THRESH} (全A/近一年): {len(hit)} -> {hit_csv}")

    # 各区间命中数概览
    for p in PERIODS:
        n = sum(1 for m in merged.values() if (m.get(f"ic_mean_{p}") or -9) >= THRESH)
        print(f"[stat] {PERIOD_CN[p]}: IC_MEAN>={THRESH} 的因子 {n} 个")


if __name__ == "__main__":
    main()
