"""模型分数因子专项审计
=====================================
lgbm_*/gru_seq_* 落库为 score 列（无 value 列），此前标准审计读不到值
而长期"未审"。store/quality/audit 已做 score→value 兼容后，
本脚本对这类因子强制跑 deep 审计（结构/分布/覆盖/IC；PIT 由训练管道
purge 纪律保证，审计侧按"非 registry 因子"SKIP 并如实记录）。

审计结果写标准格式 data/lake/factor_audit/<name>.json，
data_dashboard 因子审查列即正常显示。

用法:
    python scripts/audit_model_scores.py            # 全部模型分数因子
    python scripts/audit_model_scores.py gru_seq_score   # 指定因子
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.config import get_logger
from quantlab.factor.audit import _save_rec, audit_factor

log = get_logger("audit_model_scores")

MODELS = {
    "lgbm_all_score": "LightGBM 全因子池日度分数（24m 训练/6m 预测滚动 WF，"
                      "purge 21 日，T+1→T+21 标签）",
    "lgbm_top64_score": "LightGBM top64 重要性因子子集日度分数（同上 WF 纪律）",
    "lgbm_domain_score": "LightGBM 分域（风格/价量/事件）日度分数（同上 WF 纪律）",
    "lgbm_rep168_score": "LightGBM rep168 特征集日度分数（同上 WF 纪律）",
    "gru_seq_score": "GRU 时序模型日度分数（top64 因子 20 日 rank 序列，"
                     "24m/6m 滚动 WF，purge 21 日，T+1→T+21 标签）",
}


def main(targets: list[str] | None = None) -> None:
    names = list(targets or MODELS)
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        log.warning(f"未知模型分数因子（仍尝试标准审计）: {unknown}")
    for name in names:
        rec = audit_factor(name, deep=True, force=True)
        if rec.get("category") in (None, "unknown"):
            rec["category"] = "model_score"
            rec["description"] = MODELS.get(name, "ML 模型日度分数（滚动 WF）")
            _save_rec(rec)
        ic = (rec.get("checks") or {}).get("ic") or {}
        log.info(f"→ {name}: {rec.get('verdict')}  "
                 f"ic5={ic.get('ic5')} icir5={ic.get('icir5')} "
                 f"ic20={ic.get('ic20')} icir20={ic.get('icir20')}")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
