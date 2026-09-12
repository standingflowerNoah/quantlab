#!/usr/bin/env python3
"""变更广播 CLI（agent / 自动化任务 / 人类共用）

用法:
  python scripts/broadcast.py add --category data --action change --title "..."
        [--detail "..."] [--impact "..."] [--source "..."]
  python scripts/broadcast.py read [-n 20] [--category data] [--action fail] [--mark-read]
  python scripts/broadcast.py unread [-n 50] [--mark-read]
  python scripts/broadcast.py last

约定:
  - 每项新任务开始前先 read --mark-read（或 unread）浏览最近广播
  - 任何数据/脚本/自动化任务的变更（含失败与降级）完成后立即 add 广播
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.broadcast import (broadcast, mark_read, read_records,  # noqa: E402
                                unread)

try:  # Windows 控制台中文输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass


def _print_records(recs, cursor: int | None = None) -> None:
    if not recs:
        print("(无广播记录)")
        return
    for r in recs:
        flag = " 🔴未读" if cursor is not None and int(r["seq"]) > cursor else ""
        print(f"#{r['seq']} {r['ts'][:16].replace('T',' ')} "
              f"[{r['category']}/{r['action']}]{flag} {r['title']}")
        if r.get("detail"):
            print(f"    详情: {r['detail']}")
        if r.get("impact"):
            print(f"    影响: {r['impact']}")
        if r.get("source"):
            print(f"    来源: {r['source']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="QuantLab 变更广播")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="追加一条广播")
    p_add.add_argument("--category", required=True,
                       help="data/script/automation/model/incident/doc")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--action", default="change",
                       help="add/change/remove/fail/warn/info")
    p_add.add_argument("--detail", default="")
    p_add.add_argument("--impact", default="")
    p_add.add_argument("--source", default=None)

    p_read = sub.add_parser("read", help="读取最近广播（默认最新 20 条）")
    p_read.add_argument("-n", type=int, default=20)
    p_read.add_argument("--category", default=None)
    p_read.add_argument("--action", default=None)
    p_read.add_argument("--mark-read", action="store_true",
                        help="读完把未读游标推进到最新")

    p_un = sub.add_parser("unread", help="只看未读广播")
    p_un.add_argument("-n", type=int, default=50)
    p_un.add_argument("--mark-read", action="store_true")

    sub.add_parser("last", help="只看最后一条")

    args = ap.parse_args()

    if args.cmd == "add":
        seq = broadcast(args.category, args.title, action=args.action,
                        detail=args.detail, impact=args.impact, source=args.source)
        print(f"已广播 #{seq}" if seq is not None else "广播写入失败（详见 stderr）")
    elif args.cmd == "read":
        recs = read_records(limit=args.n, category=args.category,
                            action=args.action)
        cur = None
        if args.mark_read:
            cur = _cursor_of()
            _new = mark_read()
            print(f"(游标已推进到 #{_new})")
        _print_records(recs, cursor=cur)
    elif args.cmd == "unread":
        from quantlab.broadcast import _read_cursor
        cur = _read_cursor()
        recs = unread()
        if args.mark_read:
            mark_read()
            print("(游标已推进到最新)")
        print(f"未读 {len(recs)} 条（游标 #{cur}）")
        _print_records(recs, cursor=cur)
    elif args.cmd == "last":
        recs = read_records(limit=1)
        _print_records(recs)


def _cursor_of() -> int:
    from quantlab.broadcast import _read_cursor
    return _read_cursor()


def _latest_seq() -> int:
    recs = read_records(limit=1)
    return int(recs[0]["seq"]) if recs else 0


if __name__ == "__main__":
    main()
