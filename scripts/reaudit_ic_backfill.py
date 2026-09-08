#!/usr/bin/env python3
"""批量补填因子审查 IC（轻量恢复，2026-09-08 事故配套）
=====================================
背景：maybe_audit 增量路径（quick 模式）曾把带 IC 的旧审查记录覆盖成
无 IC 记录，导致数据看板 75 个因子显示"未评"。根修已在
quantlab/factor/audit.py（quick 模式复用旧 IC/PIT），本脚本一次性补回存量：

1. 因子库全部因子：无 audit json → 先 quick 建基础记录
2. 已有记录但 checks.ic.ic20 为空 → 只补 ic_audit()，不重算结构/PIT 字段
3. 汇总打印结果

用法：python scripts/reaudit_ic_backfill.py
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

AUDIT_DIR = Path("data/lake/factor_audit")


def main():
    from quantlab.factor.audit import audit_factor, ic_audit

    fac_dirs = sorted(d for d in Path("data/lake/factor").iterdir() if d.is_dir())
    print(f"因子库共 {len(fac_dirs)} 个因子", flush=True)

    todo, skipped = [], 0
    for d in fac_dirs:
        p = AUDIT_DIR / f"{d.name}.json"
        if p.exists():
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                rec = None
            if rec and ((rec.get("checks") or {}).get("ic") or {}).get("ic20") is not None:
                skipped += 1
                continue
        todo.append(d.name)
    print(f"已有完整 IC {skipped} 个，待补 {len(todo)} 个\n", flush=True)

    ok, fail = 0, []
    for i, name in enumerate(todo, 1):
        try:
            p = AUDIT_DIR / f"{name}.json"
            if not p.exists():
                audit_factor(name, deep=False)     # 建基础记录（结构/分布/覆盖）
            rec = json.loads(p.read_text(encoding="utf-8"))
            ic = ic_audit(name)
            if ic.get("ic20") is None:
                fail.append(f"{name}: ic_audit 返回无 IC ({ic.get('error', '?')})")
                print(f"  [{i}/{len(todo)}] {name}: FAIL {ic.get('error', '')[:80]}",
                      flush=True)
                continue
            rec.setdefault("checks", {})["ic"] = ic
            p.write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                         encoding="utf-8")
            ok += 1
            print(f"  [{i}/{len(todo)}] {name}: ic20={ic['ic20']:+.4f} "
                  f"icir20={ic['icir20']:+.2f} ({ic.get('source', 'computed')})",
                  flush=True)
        except Exception as e:
            fail.append(f"{name}: {e}")
            print(f"  [{i}/{len(todo)}] {name}: 异常 {e}", flush=True)

    print(f"\n=== 完成：补填 {ok} / {len(todo)}，失败 {len(fail)} ===", flush=True)
    for f in fail:
        print(f"  - {f}", flush=True)


if __name__ == "__main__":
    main()
