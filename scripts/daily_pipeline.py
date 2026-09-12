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

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.config import get_logger
from quantlab.broadcast import broadcast

log = get_logger("pipeline")

STEPS = []

# 广播类别映射（变更广播机制：每步完成/失败/阻塞都广播，失败归 incident）
BC_CATEGORY = {"数据更新": "data", "数据质量": "data", "分钟特征": "data",
               "因子计算": "model", "拥挤度监控": "model", "决策信号": "model",
               "总览报告": "script", "数据看板": "script", "前向监控": "script"}


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


@step("分钟特征")
def run_minute_feat():
    """分钟→日特征宽表增量重建：分钟湖有新日期而 minute_feat 未覆盖时，
    自 since=宽表最新日期 起增量合并（含重叠日重算，幂等）。
    hf 系因子依赖此宽表，不重建则 hf 因子永远滞后一天。"""
    import duckdb
    from quantlab import config
    from quantlab.data.minute_feat import build_minute_feat, feat_max_date

    feat_max = feat_max_date()
    if feat_max is None:
        return "minute_feat 为空，跳过增量（需先手动全量 data minute-feat）"
    # 分钟湖最新日期：只扫最新年份分区，取 MAX(datetime)
    yr_dirs = sorted(config.KLINE_1MIN_DIR.glob("year=*"))
    if not yr_dirs:
        return "分钟湖为空，跳过"
    latest = yr_dirs[-1] / "part-*.parquet"
    con = duckdb.connect()
    try:
        lake_max = con.execute(
            f"SELECT max(CAST(datetime AS DATE)) AS d "
            f"FROM read_parquet('{str(latest).replace(chr(92), '/')}')"
        ).fetchone()[0]
    finally:
        con.close()
    if lake_max is None:
        return "分钟湖无数据，跳过"
    lake_max = pd.Timestamp(lake_max)
    if lake_max <= feat_max:
        return f"minute_feat 已覆盖至 {feat_max.date()}，无需增量"
    n = build_minute_feat(since=str(feat_max.date()))   # 重算 feat_max 当日（防半写）+ 增量日
    return (f"增量重建完成：分钟湖至 {lake_max.date()}，"
            f"{ {k: f'{v:,}行' for k, v in n.items()} }")


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
        # 防降级闸门（2026-09-07 hf 事故：build_composite 对缺日期因子 NaN-skip
        # 静默降级）——任一模型因子水位落后或覆盖骤降时，候选快照整块不记
        from quantlab.model.composite import factor_coverage
        cov = factor_coverage(
            ["size", "amihud_20", "sue_i", "overnight_mom_20", "hf_amihud_20"])
        bad = cov[~cov["ok"]]
        if not bad.empty:
            detail = "; ".join(f"{r.factor}(最新{r.f_max},落后{r.behind_days}天,"
                               f"覆盖{r.n_max}/{r.n_prev})"
                               for r in bad.itertuples())
            raise RuntimeError(f"因子覆盖不足，候选记账跳过（防快照降级）: {detail}")
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
        # ── EQ3_HFA_ICW 稳健仓（2026-09-07 第七轮：同因子 ICIR 加权，
        #    67.9%/2.82/-17.0%，回撤最浅；裁决时与等权二选一）──
        try:
            icw = build_composite(
                ["size", "amihud_20", "sue_i", "hf_amihud_20"],
                universe="ashare_ex", method="ic_weighted")
            record_signal_multi("EQ3_HFA_ICW", generate_target(icw),
                                date=sig_date)
            cand += "/EQ3_HFA_ICW 已记账"
        except Exception as e:
            log.warning(f"EQ3_HFA_ICW 记账失败（不影响生产）: {e}")
        # ── PROD_HFA_W3 周三周度卫星（2026-09-07 第九轮：星期效应研究
        #    周三 7 组合平均最优；快照固定记在"最近一个周三"，已记则跳过，
        #    盯市按周推进）──
        try:
            from quantlab.data.store import Store as _S
            _st = _S()
            last_w3 = _st.q(
                "SELECT max(date) AS d FROM signal_portfolio_multi "
                "WHERE model='PROD_HFA_W3'")["d"][0]
            sig_ts = pd.Timestamp(sig_date)
            wed = sig_ts - pd.Timedelta(days=sig_ts.weekday() - 2)
            if wed > sig_ts:          # 周四~周日：回退到本周前的周三
                wed = wed - pd.Timedelta(days=7)
            if last_w3 is None or pd.Timestamp(wed) > pd.Timestamp(last_w3):
                w3 = build_composite(
                    ["size", "amihud_20", "sue_i", "hf_amihud_20"],
                    universe="ashare_ex")
                d3 = w3[w3["date"] == wed]
                if d3.empty:
                    d3 = w3[w3["date"] == w3["date"].max()]
                    wed = d3["date"].max()
                record_signal_multi("PROD_HFA_W3",
                                    generate_target(d3.copy()), date=wed)
                cand += f"/PROD_HFA_W3 已记账({pd.Timestamp(wed).date()})"
            else:
                cand += "/W3 周三已记"
        except Exception as e:
            log.warning(f"PROD_HFA_W3 记账失败（不影响生产）: {e}")
        # ── DIV10 红利池内精选观察仓（2026-09-07 红利 2C 第二轮：官方规则
        #    池复验过闸门二，与五模型不同暴露的卫星仓；持仓口径见
        #    quantlab/decision/dividend_pool.py）──
        try:
            from quantlab.data.store import Store as _SD
            from quantlab.decision.dividend_pool import div10_holdings
            h10 = div10_holdings(_SD(), sig_date)
            record_signal_multi("DIV10", h10, date=sig_date)
            cand += f"/DIV10 已记账({len(h10)}只)"
        except Exception as e:
            log.warning(f"DIV10 记账失败（不影响生产）: {e}")
    except Exception as e:
        log.warning(f"候选模型记账失败（不影响生产）: {e}")
        cand = "候选模型记账跳过"
    return f"目标持仓 {len(tgt)} 只；{cand}"


def _archive_report(path_str: str):
    """报告生成后按日归档：reports/archive/<name>_YYYYMMDD.html（同日幂等覆盖）"""
    import shutil
    from datetime import date
    from pathlib import Path
    src = Path(path_str)
    if not src.exists():
        return path_str
    adir = src.parent / "archive"
    adir.mkdir(exist_ok=True)
    dst = adir / f"{src.stem}_{date.today():%Y%m%d}{src.suffix}"
    shutil.copy2(src, dst)
    log.info(f"报告归档: {dst}")
    return str(dst)


@step("总览报告")
def run_report():
    from scripts.overview_report import main as overview_main
    overview_main()
    _archive_report("reports/overview_report.html")
    return "reports/overview_report.html"


@step("数据看板")
def run_dashboard():
    from scripts.data_dashboard import main as dash_main
    dash_main()
    _archive_report("reports/data_dashboard.html")
    return "reports/data_dashboard.html"


@step("前向监控")
def run_forward():
    from scripts.forward_monitor import build_report
    out = build_report()
    _archive_report(str(out))
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

    skipped_names = [n for n, v in skip.items() if v]
    broadcast("script", "每日流水线启动", action="info",
              detail=f"共 {len(STEPS)} 步，跳过: {', '.join(skipped_names) or '无'}",
              source="daily_pipeline")
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
            broadcast("incident", f"流水线[{name}] 被阻塞", action="warn",
                      detail="数据质量未通过，本步骤跳过",
                      impact="该步骤今日无产出，下游消费方注意数据截至时间",
                      source="daily_pipeline")
            continue
        t0 = time.time()
        try:
            msg = fn()
            dt = time.time() - t0
            summary.append((name, "ok", msg))
            log.info(f"[{name}] ok ({dt:.0f}s): {msg}")
            broadcast(BC_CATEGORY.get(name, "script"),
                      f"流水线[{name}] 完成", action="change",
                      detail=str(msg)[:400], source="daily_pipeline")
        except Exception as e:
            dt = time.time() - t0
            summary.append((name, "FAIL", str(e)[:80]))
            log.error(f"[{name}] FAIL ({dt:.0f}s): {e}")
            broadcast("incident", f"流水线[{name}] 失败", action="fail",
                      detail=str(e)[:400],
                      impact="下游步骤可能受阻或使用陈旧数据；下轮任务前请先读广播并排查",
                      source="daily_pipeline")
            if name == "数据质量":
                blocked.update(["因子计算", "拥挤度监控", "决策信号"])

    log.info("-" * 56)
    for name, status, msg in summary:
        log.info(f"  {name:10s} {status:8s} {msg}")
    log.info(f"流水线完成，总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    fails = [n for n, s, _ in summary if s == "FAIL"]
    broadcast("script",
              f"每日流水线结束（{(time.time()-t_all)/60:.1f} 分钟）",
              action="fail" if fails else "change",
              detail=" | ".join(f"{n}:{s}" for n, s, _ in summary)[:800],
              impact=(f"失败步骤: {', '.join(fails)}（请读广播排查后重跑）"
                      if fails else "全部完成，报告见 reports/"),
              source="daily_pipeline")


if __name__ == "__main__":
    main()
