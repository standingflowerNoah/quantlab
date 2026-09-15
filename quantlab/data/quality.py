"""数据质量校验：完整性 / 新鲜度 / 合理性 / 一致性

查询走 store.query() 三层降级（直连 → 只读 → Parquet 镜像），
因此数据更新任务持写锁期间体检照常可跑（报告写入自动跳过）。
"""
from __future__ import annotations

import random
import time

import pandas as pd

from ..config import get_logger
from .store import query

log = get_logger(__name__)


def check_all(sample: int = 30) -> pd.DataFrame:
    """全量体检，返回问题清单并尽力写入 quality_report 表"""
    issues = []

    def add(domain, ctype, status, detail):
        issues.append({"domain": domain, "check_type": ctype,
                       "status": status, "detail": detail})
        try:
            from .store import Store
            Store().add_quality(domain, ctype, status, detail)
        except Exception:
            pass          # 写锁被占时跳过报告落库（结果已返回调用方）

    # ── 1. 完整性：覆盖度足够的最近交易日 + 新鲜度 ──────────────
    try:
        from . import calendar
        from .instruments import all_codes
        total = len(all_codes(st_only=False))
        # 找覆盖度 >=90% 的最近交易日（避免个别补拉股票抬高 MAX(date)）
        r = query(f"""
            SELECT date, COUNT(DISTINCT code) AS n
            FROM kline_daily
            GROUP BY date
            HAVING COUNT(DISTINCT code) >= {int(total * 0.9)}
            ORDER BY date DESC LIMIT 1""")
        if r.empty:
            add("kline_daily", "completeness", "fail",
                "无覆盖度足够的交易日，疑似更新中断")
        else:
            latest = r.iloc[0, 0]
            n = int(r.iloc[0, 1])
            ratio = n / total if total else 0
            last = calendar.last_trading_day()
            lag = (pd.Timestamp(last).normalize()
                   - pd.Timestamp(latest).normalize()).days
            add("kline_daily", "completeness", "pass",
                f"最新交易日({latest.date()})K线覆盖 {n}/{total} ({ratio:.1%})")
            # 新鲜度：落后日历天数（早盘前跑时滞后 1 天属正常）
            if lag > 2:
                add("kline_daily", "freshness", "fail",
                    f"K线最新 {latest.date()} 落后日历 {lag} 天")
    except Exception as e:
        add("kline_daily", "completeness", "fail", f"检查异常: {e}")

    # ── 2. 新鲜度：各域水位 ──────────────────────────────────────
    # 披露终止特判：北向净买入自 2024-08-16 起交易所停止披露（机制性断供），
    # 水位停在该日属正常，不按"日频落后"判 FAIL
    DISCLOSURE_END = {"northbound_daily": pd.Timestamp("2024-08-15")}
    wm = query("""
        SELECT w.domain, w.partition, w.watermark, w.updated_at,
               d.frequency, d.source
        FROM watermarks w LEFT JOIN datasets d USING(domain)
        ORDER BY w.domain""")
    daily_domains = {"kline_daily", "daily_snapshot", "dragon_tiger",
                     "hot_topic", "northbound_daily", "margin_total",
                     "block_trade", "index_kline"}
    for _, w in wm.iterrows():
        d = w["domain"]
        if w["watermark"] is None:
            continue
        days = (pd.Timestamp.now().normalize()
                - pd.Timestamp(w["watermark"]).normalize()).days
        if d in DISCLOSURE_END:
            end = DISCLOSURE_END[d]
            if pd.Timestamp(w["watermark"]).normalize() <= end:
                add(d, "freshness", "pass",
                    f"披露止于 {end.date()}（交易所停止披露，机制性断供）")
                continue
        if d in daily_domains and days > 4:
            add(d, "freshness", "fail",
                f"日频域水位落后 {days} 天（{w['watermark']}）")
        elif d == "finance_snapshot" and days > 14:
            add(d, "freshness", "warn", f"财务快照 {days} 天未更新")
        elif d == "fund_flow_daily" and days > 3:
            add(d, "freshness", "warn",
                f"资金流 {days} 天未更新（push2his 网络问题时为已知状态）")

    # ── 3. 合理性：K线数值边界 ───────────────────────────────────
    try:
        bad = query("""
            SELECT COUNT(*) AS n FROM kline_daily
            WHERE date >= (SELECT MAX(date) - INTERVAL 7 DAY FROM kline_daily)
              AND (close <= 0 OR high < low OR vol < 0)""")
        n_bad = int(bad.iloc[0, 0])
        if n_bad > 0:
            add("kline_daily", "validity", "warn", f"近7日 {n_bad} 行异常K线")
        else:
            add("kline_daily", "validity", "pass", "近7日K线数值正常")
    except Exception as e:
        add("kline_daily", "validity", "fail", f"检查异常: {e}")

    # ── 4. 一致性：K线收盘 vs 腾讯实时快照（抽样，注意非交易时段） ──
    try:
        codes = query("""
            SELECT code FROM kline_daily
            WHERE date = (SELECT MAX(date) FROM kline_daily)
            ORDER BY RANDOM() LIMIT ?""", [sample])["code"].tolist()
        if codes:
            from .sources import tencent_source as tx
            snap = tx.batch_quotes(codes)
            if snap.empty:          # 网络抖动重试一次
                time.sleep(3)
                snap = tx.batch_quotes(codes)
            # 快照不可用（如 SSL 超时导致空表/缺列）只 warn，不判 FAIL
            # —— 外部行情不可达≠数据异常，不应阻塞因子/决策链（2026-09-10 事故）
            if snap.empty or "code" not in snap.columns or "price" not in snap.columns:
                add("kline_daily", "consistency", "warn",
                    "腾讯行情快照不可用（网络超时），一致性检查本轮跳过（非数据异常）")
            else:
                latest = query("""
                    SELECT code, close FROM kline_daily
                    WHERE date = (SELECT MAX(date) FROM kline_daily)""", )
                latest = latest[latest["code"].isin(codes)]
                m = latest.merge(snap[["code", "price"]], on="code")
                m["err"] = ((m["close"] - m["price"]) / m["price"]).abs()
                # 收盘价与最新价在非交易时段应基本一致（容忍当日波动 2%）
                n_big = int((m["err"] > 0.02).sum())
                if n_big <= sample * 0.1:
                    add("kline_daily", "consistency", "pass",
                        f"抽样 {len(m)} 只收盘价 vs 腾讯行情一致（>2% 偏差 {n_big} 只，含盘中波动）")
                else:
                    add("kline_daily", "consistency", "warn",
                        f"抽样 {len(m)} 只中 {n_big} 只收盘价偏差>2%")
    except Exception as e:
        add("kline_daily", "consistency", "fail", f"检查异常: {e}")

    # ── 5. 复权因子抽样校验 ──────────────────────────────────────
    try:
        from .kline import verify_adj_factor
        codes = query("""
            SELECT DISTINCT code FROM dividend_events
            ORDER BY RANDOM() LIMIT 3""")["code"].tolist()
        n_bad = 0
        for c in codes:
            v = verify_adj_factor(c)
            if v["status"] != "ok":
                n_bad += 1
                add("kline_daily", "validity", "warn",
                    f"复权因子校验 {c}: {v.get('issues')}")
        if n_bad == 0 and codes:
            add("kline_daily", "validity", "pass",
                f"复权因子抽样 {len(codes)} 只全部通过")
    except Exception as e:
        add("kline_daily", "validity", "fail", f"复权校验异常: {e}")

    # ── 6. 一致性：因子湖水位 vs K线水位（防 NaN-skip 快照静默降级） ──
    # 2026-09-07 hf 事故：minute_feat 停在 09-04 → 21 个 hf 因子停写，
    # 流水线无任何报警，PROD_HFA 快照降级为 3 因子口径
    try:
        from ..model.composite import factor_coverage
        cov = factor_coverage(
            ["size", "amihud_20", "sue_i", "overnight_mom_20", "hf_amihud_20"])
        bad = cov[~cov["ok"]]
        if bad.empty:
            detail = "; ".join(f"{r.factor}@{r.f_max.date()}({r.n_max}只)"
                               for r in cov.itertuples())
            add("factor_lake", "consistency", "pass", f"模型因子水位一致: {detail}")
        else:
            for r in bad.itertuples():
                dep = ("（hf 系→分钟特征宽表）"
                       if str(r.factor).startswith("hf_") else "")
                add("factor_lake", "consistency", "fail",
                    f"因子 {r.factor} 水位落后 {r.behind_days} 天"
                    f"（最新 {r.f_max}，覆盖 {r.n_max}/{r.n_prev}）——"
                    f"候选快照已被决策闸门阻止，请检查依赖链{dep}")
    except Exception as e:
        add("factor_lake", "consistency", "fail", f"因子水位检查异常: {e}")

    # ── 7. 硬不变量：总市值 >= 流通市值（total_shares >= float_shares）──
    # 2026-09-11 事故：tencent_source 把接口 [44](流通市值) / [45](总市值) 写反，
    # 导致 09-03/04/07/08/11 全市场约 59% 的行「总市值 < 流通市值」——数学不可能。
    # 该错位会污染 universe 的 top 系池（按 float_mcap_yi 排序）与容量估算。
    # 教训：外部接口按**下标**取值必须配不变量防线。
    try:
        r = query("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN mcap_yi < float_mcap_yi * 0.999
                            THEN 1 ELSE 0 END) AS bad
            FROM daily_snapshot
            WHERE date = (SELECT MAX(date) FROM daily_snapshot)
              AND mcap_yi > 0 AND float_mcap_yi > 0""")
        n, bad = int(r.iloc[0, 0]), int(r.iloc[0, 1] or 0)
        if n == 0:
            add("daily_snapshot", "invariant", "warn", "当日快照无有效市值数据")
        elif bad == 0:
            add("daily_snapshot", "invariant", "pass",
                f"最新快照 {n} 只市值口径正常（总市值>=流通市值）")
        else:
            add("daily_snapshot", "invariant", "fail",
                f"最新快照 {bad}/{n} 只「总市值<流通市值」（不可能）——"
                f"疑似市值列错位，请检查 tencent_source 字段索引")
    except Exception as e:
        add("daily_snapshot", "invariant", "fail", f"检查异常: {e}")

    # ── 8. 硬不变量：PIT 股本表 share_capital_daily ────────────────
    # 总股本 >= 流通股本 恒真。判定用 1e-6 相对容差（上游浮点噪声，
    # 如 601166 报 211.628519 亿 vs 211.628552 亿）。北交所（92/83/87/43 开头）
    # 数据源两字段疑语义互换，且不在 ashare_ex/ashare_main 研究池内 → 单列 WARN。
    try:
        r = query("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN total_shares < float_shares * (1 - 1e-6)
                            THEN 1 ELSE 0 END) AS bad,
                   SUM(CASE WHEN total_shares < float_shares * (1 - 1e-6)
                             AND NOT (code LIKE '92%' OR code LIKE '83%'
                                      OR code LIKE '87%' OR code LIKE '43%')
                            THEN 1 ELSE 0 END) AS bad_nonbj,
                   MIN(date) AS first_date, MAX(date) AS last_date
            FROM share_capital_daily""")
        n = int(r.iloc[0, 0] or 0)
        bad = int(r.iloc[0, 1] or 0)
        bad_nonbj = int(r.iloc[0, 2] or 0)
        if n == 0:
            add("share_capital_daily", "invariant", "warn",
                "PIT 股本表为空，请跑 scripts/backfill_share_capital.py")
        elif bad_nonbj > 0:
            add("share_capital_daily", "invariant", "fail",
                f"{bad_nonbj}/{n} 行非北交所 total_shares < float_shares（不可能）"
                f"——跑 scripts/fix_share_capital_conflict.py")
        elif bad > 0:
            add("share_capital_daily", "invariant", "warn",
                f"{bad}/{n} 行为北交所股本冲突（研究池已剔除，不影响因子）")
        else:
            add("share_capital_daily", "invariant", "pass",
                f"{n} 行股本口径正常（{r.iloc[0, 3]} ~ {r.iloc[0, 4]}）")
    except Exception as e:
        add("share_capital_daily", "invariant", "warn", f"检查跳过: {e}")

    return pd.DataFrame(issues)


def summary() -> pd.DataFrame:
    """最近一次体检结果摘要（只读降级）"""
    return query("""
        SELECT domain, check_type, status, detail, checked_at
        FROM quality_report
        WHERE checked_at = (SELECT MAX(checked_at) FROM quality_report)
        ORDER BY status, domain""")
