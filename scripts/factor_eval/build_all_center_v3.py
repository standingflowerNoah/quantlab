#!/usr/bin/env python
"""全库 v3 看板批量重建（dashboard-v3 精修版，补齐十分位 + 正交化残差 IC）

与 build_all_center_lake.py（湖模式）的关系：
  湖模式零主库依赖、流水线持锁时可跑；v3 需主库 ATTACH（全部 READ_ONLY），
  本驱动用多进程并行（readonly+readonly 可共存），产物覆盖同名湖模式板。

流程：
  1) N 个 worker 并行跑 build_dashboard.py --no-index（index.json 由本驱动
     最后统一登记，避免并行读改写互踩）
  2) 失败自动重试一轮
  3) 解析各 worker stdout 的 [done] 行，统一登记 index.json

用法：
  python scripts/factor_eval/build_all_center_v3.py               # 全部
  python scripts/factor_eval/build_all_center_v3.py --only a,b    # 指定
  python scripts/factor_eval/build_all_center_v3.py --workers 3 --end 2026-09-15

产出：docs/factor_center_{name}.html × N（template=dashboard-v3）+ index 登记
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
SCRIPT = ROOT / "scripts" / "factor_eval" / "build_dashboard.py"
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
INDEX = ROOT / "reports" / "factor_eval" / "index.json"
DOCS = ROOT / "docs"
FAIL_LOG = ROOT / "logs" / "build_all_center_v3_fails.txt"

DONE_RE = re.compile(
    r"\[done\] (.) (\S+): IC5 ([+-]?[\d.]+) t_adj ([+-]?[\d.]+) q=(\S+)")


def all_factors() -> list[str]:
    return sorted(d.name for d in FACTOR_DIR.iterdir() if d.is_dir())


def run_one(name: str, end: str, timeout: int = 1200) -> tuple:
    """跑单因子 v3 板，返回 (name, status, elapsed, stdout+stderr)"""
    cmd = [PY, str(SCRIPT), "--name", name, "--end", end,
           "--no-index", "--tags", "全库v3重建"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return name, "TIMEOUT", time.time() - t0, ""
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    ok = r.returncode == 0 and (DOCS / f"factor_center_{name}.html").exists()
    return name, ("OK" if ok else "FAIL"), time.time() - t0, out


def run_batch(names: list[str], end: str, workers: int) -> tuple[dict, list]:
    """并行批量，返回 {name: (light, ic, t_adj, q, flags)} 与失败清单"""
    stats, fails = {}, []
    n = len(names)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_one, nm, end): nm for nm in names}
        for i, fut in enumerate(as_completed(futs), 1):
            nm, st, el, out = fut.result()
            if st == "OK":
                m = DONE_RE.search(out)
                if m:
                    stats[nm] = {"light": m.group(1), "ic": m.group(3),
                                 "t_adj": m.group(4), "q": m.group(5),
                                 "flags": re.findall(r"⚠️ (.+)", out)}
                else:
                    stats[nm] = {"light": "?", "ic": "", "t_adj": "",
                                 "q": "", "flags": []}
                print(f"[{i}/{n}] ✅ {nm} ({el:.0f}s, "
                      f"{(time.time()-t0)/60:.0f}min elapsed)", flush=True)
            else:
                fails.append(nm)
                print(f"[{i}/{n}] ❌ {st} {nm} ({el:.0f}s) "
                      f"{out.strip().splitlines()[-1][:100] if out.strip() else ''}",
                      flush=True)
    return stats, fails


def register_index(stats: dict, end: str) -> int:
    """批量完成后统一登记 index.json（单线程读改写，无并发）"""
    idx = (json.loads(INDEX.read_text(encoding="utf-8"))
           if INDEX.exists() else [])
    ts = datetime.now()
    added = 0
    for nm, s in stats.items():
        idx.append({
            "id": f"dashboard:{nm}:{ts:%Y%m%d%H%M}",
            "type": "dashboard", "name": nm,
            "date": f"{ts:%Y-%m-%d}",
            "generated_at": ts.isoformat(timespec="seconds"),
            "window": f"2023-01-01~{end}", "tags": ["全库v3重建"],
            "light": s["light"], "flags": s["flags"],
            "file": f"docs/factor_center_{nm}.html",
            "template": "dashboard-v3",
            "summary": (f"IC5 {s['ic']} t_adj {s['t_adj']} "
                        f"灯 {s['light']} v3 全库重建"),
        })
        added += 1
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return added


def main() -> None:
    p = argparse.ArgumentParser(description="全库 v3 看板批量重建")
    p.add_argument("--workers", type=int, default=3,
                   help="并行进程数（readonly ATTACH 可共存，默认 3）")
    p.add_argument("--end", default="2026-09-15")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--only", default="", help="逗号分隔因子名，只跑指定")
    a = p.parse_args()

    names = ([x.strip() for x in a.only.split(",") if x.strip()]
             if a.only else all_factors())
    n = len(names)
    print(f"[batch] v3 全库重建：{n} 因子 × {a.workers} workers，"
          f"窗口 {a.start}~{a.end}", flush=True)
    t0 = time.time()

    stats, fails = run_batch(names, a.end, a.workers)

    if fails:
        print(f"[retry] 失败 {len(fails)} 个，串行重试一轮…", flush=True)
        for nm in list(fails):
            nm2, st, el, out = run_one(nm, a.end)
            if st == "OK":
                m = DONE_RE.search(out)
                stats[nm] = {"light": m.group(1) if m else "?",
                             "ic": m.group(3) if m else "",
                             "t_adj": m.group(4) if m else "",
                             "q": m.group(5) if m else "",
                             "flags": re.findall(r"⚠️ (.+)", out)}
                fails.remove(nm)
                print(f"[retry] ✅ {nm} ({el:.0f}s)", flush=True)
            else:
                print(f"[retry] ❌ {nm}", flush=True)
        FAIL_LOG.parent.mkdir(exist_ok=True)
        FAIL_LOG.write_text("\n".join(fails), encoding="utf-8")

    added = register_index(stats, a.end)
    ok_n = len(stats)
    print(f"[done] 成功 {ok_n} / 失败 {len(fails)} / 共 {n}"
          f"（{(time.time()-t0)/60:.0f} min）", flush=True)
    if fails:
        print(f"失败清单：{fails}（已写 {FAIL_LOG.name}）", flush=True)
    print(f"[index] +{added} 条（template=dashboard-v3）", flush=True)


if __name__ == "__main__":
    main()
