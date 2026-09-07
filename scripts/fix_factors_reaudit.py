#!/usr/bin/env python3
"""因子修复与重评估
=====================================
1. 4 个迁移因子（roe/debt_ratio/ocf_ratio/current_ratio）：
   finance_snapshot 单点截面 → finance_history 多期 ASOF（notice_eff 生效日）
   重算落库后删旧 audit 记录，deep 全审（含 IC + PIT）
2. 无 IC 的因子（audit 早于 IC 检查接入）：deep 重审计补 IC
用法：等 DuckDB 写锁释放后运行（分钟回补任务持锁期间无法执行）
"""
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

MIGRATED = ["roe", "debt_ratio", "ocf_ratio", "current_ratio"]
AUDIT_DIR = Path("data/lake/factor_audit")


def _no_ic_factors() -> list[str]:
    """动态收集 audit JSON 中无 IC 指标的因子"""
    out = []
    for p in sorted(AUDIT_DIR.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            ic = (rec.get("checks") or {}).get("ic") or {}
            if not ic:
                out.append(p.stem)
        except Exception:
            continue
    return out


def main():
    from quantlab.factor.compute import compute_factor
    from quantlab.factor.audit import audit_factor

    # ── 1. 迁移因子重算 + deep 全审 ──────────────────────────────
    for name in MIGRATED:
        print(f"\n=== 重算迁移因子 {name} ===", flush=True)
        df = compute_factor(name)          # 重算落库（口径已升级）
        print(f"  行数 {len(df):,}", flush=True)
        rec_path = AUDIT_DIR / f"{name}.json"
        if rec_path.exists():
            rec_path.unlink()              # 删旧记录强制 deep（版本未变 latest_date 判定会走轻量）
        rec = audit_factor(name, deep=True)
        print(f"  verdict={rec['verdict']} issues={rec['issues']}", flush=True)

    # ── 2. 无 IC 因子 deep 重审计 ────────────────────────────────
    todo = [n for n in _no_ic_factors() if n not in MIGRATED]
    print(f"\n=== 无 IC 因子 deep 重审计：{len(todo)} 个 ===", flush=True)
    for i, name in enumerate(todo, 1):
        rec = audit_factor(name, deep=True)
        ic = rec.get("checks", {}).get("ic") or {}
        print(f"  [{i}/{len(todo)}] {name}: {rec['verdict']} "
              f"ic20={ic.get('ic20')} icir20={ic.get('icir20')}", flush=True)

    # ── 3. 汇总 ──────────────────────────────────────────────────
    print("\n=== 修复后全库状态 ===", flush=True)
    verdict_cnt, grade_cnt = {}, {"达标": 0, "观察": 0, "弱": 0, "未评": 0}
    for p in sorted(AUDIT_DIR.glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        verdict_cnt[rec["verdict"]] = verdict_cnt.get(rec["verdict"], 0) + 1
        ic = (rec.get("checks") or {}).get("ic") or {}
        ic20, icir20 = ic.get("ic20"), ic.get("icir20")
        if ic20 is None or icir20 is None:
            grade_cnt["未评"] += 1
        elif abs(ic20) > 0.03 and abs(icir20) > 0.5:
            grade_cnt["达标"] += 1
        elif abs(ic20) > 0.02 and abs(icir20) > 0.3:
            grade_cnt["观察"] += 1
        else:
            grade_cnt["弱"] += 1
    print(f"审查: {verdict_cnt}", flush=True)
    print(f"评估: {grade_cnt}", flush=True)


if __name__ == "__main__":
    main()
