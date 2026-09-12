"""评估报告检索 CLI（factor-model-evaluator skill 需求 5）
==========================================================
按名称 / 日期 / 标签检索历史评估结果，索引 reports/factor_eval/index.json。

用法：
  python scripts/factor_eval/search_eval.py --name amihud            # 名称模糊
  python scripts/factor_eval/search_eval.py --tag production         # 标签
  python scripts/factor_eval/search_eval.py --since 2026-09          # 日期起
  python scripts/factor_eval/search_eval.py --type model --latest   # 某类最新一条
  python scripts/factor_eval/search_eval.py --id factor:amihud_20:202609121240 --show
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "reports" / "factor_eval" / "index.json"


def main() -> None:
    p = argparse.ArgumentParser(description="检索历史评估报告")
    p.add_argument("--name", default=None, help="对象名称（模糊匹配）")
    p.add_argument("--type", default=None,
                   choices=["factor", "model", "dashboard"])
    p.add_argument("--tag", default=None, help="标签（精确）")
    p.add_argument("--since", default=None, help="日期下限 YYYY / YYYY-MM / YYYY-MM-DD")
    p.add_argument("--until", default=None, help="日期上限")
    p.add_argument("--latest", action="store_true",
                   help="只取每个 (type,name) 的最新一条")
    p.add_argument("--show", action="store_true", help="打印匹配报告全文（单条时）")
    p.add_argument("--id", default=None, help="按评估 ID 精确取")
    a = p.parse_args()

    if not INDEX.exists():
        print("索引不存在（尚无评估记录）")
        sys.exit(1)
    idx = json.loads(INDEX.read_text(encoding="utf-8"))

    hits = idx
    if a.id:
        hits = [e for e in hits if e["id"] == a.id]
    if a.name:
        hits = [e for e in hits if a.name.lower() in e["name"].lower()]
    if a.type:
        hits = [e for e in hits if e["type"] == a.type]
    if a.tag:
        hits = [e for e in hits if a.tag in e.get("tags", [])]
    if a.since:
        hits = [e for e in hits if e["date"] >= a.since]
    if a.until:
        hits = [e for e in hits if e["date"] <= a.until]
    if a.latest:
        best: dict = {}
        for e in hits:
            k = (e["type"], e["name"])
            if k not in best or e["generated_at"] > best[k]["generated_at"]:
                best[k] = e
        hits = sorted(best.values(), key=lambda e: e["generated_at"])

    if not hits:
        print("无匹配记录")
        sys.exit(0)

    print(f"{'日期':10s} {'类型':7s} {'对象':20s} {'灯':2s} 摘要 / 文件")
    for e in hits:
        print(f"{e['date']:10s} {e['type']:7s} {e['name']:20s} {e['light']:2s} "
              f"{e['summary']}  ->  {e['file']}")
        if e.get("flags"):
            for f in e["flags"]:
                print(f"{'':42s}⚠️ {f}")

    if a.show:
        if len(hits) == 1:
            fp = INDEX.parent / hits[0]["file"]
            if not fp.exists():          # dashboard 等根相对路径
                fp = ROOT / hits[0]["file"]
            print("\n" + "=" * 70 + "\n")
            print(fp.read_text(encoding="utf-8"))
        else:
            print(f"\n--show 仅在单条匹配时生效（当前 {len(hits)} 条），"
                  "可先用其他条件收窄。")
    print(f"\n共 {len(hits)} 条 / 索引总 {len(idx)} 条")


if __name__ == "__main__":
    main()
