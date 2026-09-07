#!/usr/bin/env python3
"""每日自动化流水线（早盘前执行）
=====================================
串联 数据更新 → 数据质量闸门 → 因子计算 → 决策信号 → 总览报告。
数据质量不过关时自动阻塞因子/决策，避免脏数据污染信号。

用法:
  python scripts/daily_pipeline.py                 # 完整流程
  python scripts/daily_pipeline.py --skip-data     # 跳过数据更新
  python scripts/daily_pipeline.py --skip-quality  # 跳过数据质量体检
"""
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.config import get_logger

log = get_logger("pipeline")

STEPS = []


def step(name):
    def deco(fn):
        STEPS.append((name, fn))
        return fn
    return deco


@step("数据更新")
def run_data():
    from quantlab.data.update import update_all
    results = update_all()
    ok = sum(1 for v in results.values() if str(v).startswith("ok"))
    return f"{ok}/{len(results)} 域成功"


@step("数据质量")
def run_quality():
    from quantlab.data.quality import check_all
    issues = check_all()
    if issues.empty:
        return "体检完成，无异常"
    n_fail = int((issues["status"] == "fail").sum())
    n_warn = int((issues["status"] == "warn").sum())
    if n_fail > 0:
        raise RuntimeError(f"{n_fail} 项失败, {n_warn} 项警告（数据异常）")
    return f"{n_fail} 失败, {n_warn} 警告（数据健康）"


@step("因子计算")
def run_factor():
    from quantlab.factor import compute_all
    results = compute_all()
    ok = sum(1 for df in results.values()
             if df is not None and not df.empty)
    return f"{ok}/{len(results)} 因子已更新"


@step("拥挤度监控")
def run_crowding():
    from quantlab.factor.crowding import monitor_pool
    _, alerts, gate = monitor_pool()
    w = gate.get("w_current")
    wmsg = f"，门控 w={w:.2f}" if w is not None else ""
    if alerts:
        return f"{len(alerts)} 项拥挤预警(≥80%分位): {', '.join(alerts)}{wmsg}"
    return f"无拥挤预警{wmsg}"


@step("决策信号")
def run_decision():
    from quantlab.model.pool_select import build_production_score
    from quantlab.decision import generate_target, load_state, save_state
    from quantlab.decision.tracker import record_signal, record_signal_multi
    # 生产选股模型（唯一入口，与 cli decision 共用）：
    # 池内精选（size+amihud_20 选 400 → dragon_net_20 精选 100）
    # 分年度验证 4/5 年跑赢纯 size+amihud；全期年化 44.7%/夏普 1.41/IR 2.38。
    score = build_production_score()
    tgt = generate_target(score)
    sig_date = score["date"].max()
    save_state(tgt, date=sig_date)
    record_signal(tgt, date=sig_date)   # 纸面组合台账（前向验证账本）

    # ── 候选模型并行记账（不改变生产，积累 sue_i 前向样本；失败不阻塞）──
    try:
        from quantlab.model import build_composite
        core = build_composite(["size", "amihud_20"], universe="ashare_ex")
        new_si = build_composite(["sue_i", "overnight_mom_20"],
                                 universe="ashare_ex")
        m = core.rename(columns={"score": "s1"}).merge(
            new_si.rename(columns={"score": "s2"}), on=["date", "code"],
            how="inner")
        m["r1"] = m.groupby("date")["s1"].rank(pct=True)
        m["r2"] = m.groupby("date")["s2"].rank(pct=True)
        m["score"] = 0.6 * m["r1"] + 0.4 * m["r2"]
        v3_si = m[["date", "code", "score"]]

        record_signal_multi("PROD", tgt, date=sig_date)
        prod_si = build_composite(
            ["size", "amihud_20", "sue_i", "overnight_mom_20"],
            universe="ashare_ex")
        record_signal_multi("PROD_SI", generate_target(prod_si), date=sig_date)
        record_signal_multi("V3_SI", generate_target(v3_si), date=sig_date)
        cand = "PROD_SI/V3_SI 已记账"
        # ── EQ3 观察仓（2026-09-07 第二轮：overnight_mom 全历史稀释剔除，
        #    主切换候选修正为 核心+sue_i 等权）──
        try:
            eq3 = build_composite(["size", "amihud_20", "sue_i"],
                                  universe="ashare_ex")
            record_signal_multi("EQ3", generate_target(eq3), date=sig_date)
            cand += "/EQ3 已记账"
        except Exception as e:
            log.warning(f"EQ3 记账失败（不影响生产）: {e}")
        # ── PROD_DUAL 两块式观察仓（2026-09-07 生产模型重构立项）──
        try:
            from quantlab.model.pool_select import build_dual_score
            dual = build_dual_score()
            record_signal_multi("PROD_DUAL", generate_target(dual),
                                date=sig_date)
            cand += "/PROD_DUAL 已记账"
        except Exception as e:
            log.warning(f"PROD_DUAL 记账失败（不影响生产）: {e}")
        # ── PROD_HF 观察仓（2026-09-07 第五轮：hf_amihud_20 分钟流动性
        #    精化版，同窗 65.5%/2.49/-21.9% 胜 PROD/EQ3，月配对胜率 68%）──
        try:
            prod_hf = build_composite(
                ["size", "amihud_20", "hf_amihud_20"],
                universe="ashare_ex")
            record_signal_multi("PROD_HF", generate_target(prod_hf),
                                date=sig_date)
            cand += "/PROD_HF 已记账"
        except Exception as e:
            log.warning(f"PROD_HF 记账失败（不影响生产）: {e}")
        # ── PROD_HFA 观察仓（2026-09-07 第六轮：sue_i+hf_amihud_20 增量
        #    合并，71.7%/2.79/-20.7%，月配对 78.9%，当前主切换候选）──
        try:
            prod_hfa = build_composite(
                ["size", "amihud_20", "sue_i", "hf_amihud_20"],
                universe="ashare_ex")
            record_signal_multi("PROD_HFA", generate_target(prod_hfa),
                                date=sig_date)
            cand += "/PROD_HFA 已记账"
        except Exception as e:
            log.warning(f"PROD_HFA 记账失败（不影响生产）: {e}")
    except Exception as e:
        log.warning(f"候选模型记账失败（不影响生产）: {e}")
        cand = "候选模型记账跳过"
    return f"目标持仓 {len(tgt)} 只；{cand}"


@step("总览报告")
def run_report():
    from scripts.overview_report import main as overview_main
    overview_main()
    return "reports/overview_report.html"


@step("数据看板")
def run_dashboard():
    from scripts.data_dashboard import main as dash_main
    dash_main()
    return "reports/data_dashboard.html"


@step("前向监控")
def run_forward():
    from scripts.forward_monitor import build_report
    out = build_report()
    return str(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-data", action="store_true")
    ap.add_argument("--skip-quality", action="store_true")
    ap.add_argument("--skip-factor", action="store_true")
    ap.add_argument("--skip-crowding", action="store_true")
    ap.add_argument("--skip-decision", action="store_true")
    ap.add_argument("--skip-report", action="store_true")
    args = ap.parse_args()

    skip = {
        "数据更新": args.skip_data,
        "数据质量": args.skip_quality,
        "因子计算": args.skip_factor,
        "拥挤度监控": args.skip_crowding,
        "决策信号": args.skip_decision,
        "总览报告": args.skip_report,
    }

    log.info("=" * 56)
    log.info("每日流水线启动")
    t_all = time.time()
    summary = []
    blocked = set()          # 数据质量 FAIL 后阻塞的步骤
    for name, fn in STEPS:
        if skip.get(name):
            summary.append((name, "skipped", "-"))
            continue
        if name in blocked:
            summary.append((name, "blocked", "数据质量未通过，已跳过"))
            log.warning(f"[{name}] blocked（数据质量未通过）")
            continue
        t0 = time.time()
        try:
            msg = fn()
            dt = time.time() - t0
            summary.append((name, "ok", msg))
            log.info(f"[{name}] ok ({dt:.0f}s): {msg}")
        except Exception as e:
            dt = time.time() - t0
            summary.append((name, "FAIL", str(e)[:80]))
            log.error(f"[{name}] FAIL ({dt:.0f}s): {e}")
            if name == "数据质量":
                blocked.update(["因子计算", "拥挤度监控", "决策信号"])

    log.info("-" * 56)
    for name, status, msg in summary:
        log.info(f"  {name:10s} {status:8s} {msg}")
    log.info(f"流水线完成，总耗时 {(time.time()-t_all)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
