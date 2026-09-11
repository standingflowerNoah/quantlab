"""数据更新调度器：按域编排增量更新，域间独立失败不阻塞"""
from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd

from ..config import get_logger
from ..broadcast import broadcast
from .store import Store

log = get_logger(__name__)


def _days_since(d) -> int:
    if d is None:
        return 999
    return (pd.Timestamp.now().normalize() - pd.Timestamp(d).normalize()).days


def update_all(date=None, domains: list[str] | None = None,
               full_kline: bool = False) -> dict:
    """执行日度增量更新流水线（盘后运行）

    域顺序：日历 → 股票主表/快照 → K线 → 指数 → 特色数据(龙虎榜/两融/解禁/
    大宗/热点/北向) → 财务(周) → 股东户数/业绩预告(周) → 资金流(可用时)
    """
    from . import calendar
    results: dict[str, str] = {}

    def run(domain: str, fn):
        if domains and domain not in domains:
            return
        t0 = time.time()
        try:
            n = fn()
            results[domain] = f"ok ({n}) {time.time()-t0:.0f}s"
        except Exception as e:
            results[domain] = f"FAIL: {str(e)[:80]}"
            log.error(f"[{domain}] {e}")
            broadcast("incident", f"[{domain}] 数据更新失败", action="fail",
                      detail=str(e)[:300],
                      impact=f"{domain} 水位将落后，下游因子/信号使用陈旧数据",
                      source="update.py")

    def run_checked(domain: str, fn, verify, fallback):
        """主源执行后校验是否真正推进；未推进则自动降级到兜底源。

        2026-09-11 起因：通达信服务器池全挂，kline_daily / index_kline 报
        FAIL 后需人工跑 backfill 脚本。此包装把该动作内置到流水线。
        """
        if domains and domain not in domains:
            return
        t0 = time.time()
        err = None
        try:
            n = fn()
            results[domain] = f"ok ({n}) {time.time()-t0:.0f}s"
        except Exception as e:                               # noqa: BLE001
            err = str(e)[:80]
            log.error(f"[{domain}] 主源失败: {e}")
        if err is None and verify():
            return
        reason = err or "主源未推进水位"
        log.warning(f"[{domain}] 未完成（{reason}）→ 自动降级兜底源")
        try:
            r = fallback()
            results[domain] = f"degraded ({r}) {time.time()-t0:.0f}s"
            store.add_quality(domain, "source_fallback", "warn",
                              f"主源未完成：{reason}；已由兜底源补齐")
            broadcast("incident", f"[{domain}] 主源未完成，已降级兜底源",
                      action="warn", detail=f"原因: {reason}；兜底结果: {r}",
                      impact=f"{domain} 本轮由兜底源（fsdb/新浪）补齐，"
                             f"字段口径可能与主源有差异",
                      source="update.py")
        except Exception as e2:                              # noqa: BLE001
            results[domain] = (f"FAIL: {reason} | 兜底亦失败: {str(e2)[:60]}")
            log.error(f"[{domain}] 兜底也失败: {e2}")
            broadcast("incident", f"[{domain}] 主源与兜底源均失败", action="fail",
                      detail=f"主源: {reason}；兜底: {str(e2)[:200]}",
                      impact=f"{domain} 水位停滞，需人工排查后补数",
                      source="update.py")

    store = Store()
    # 交易日判断（重要）：trade_calendar 只含"已完成"的交易日（来自指数K线），
    # 盘前运行（如 07:00 定时任务）时"今天"必然不在日历中，仅用 is_trading_day
    # 判断会导致定时任务永远跳过数据更新。因此补充"待补数据"判断：
    # 日历最大交易日 > K线最大日期 ⇒ 上个交易日收盘数据尚未拉取，需完整更新。
    is_td = calendar.is_trading_day(pd.Timestamp.now())
    cal_max = store.q("SELECT max(trade_date) AS d FROM trade_calendar")["d"][0]
    kline_max = store.q("SELECT max(date) AS d FROM kline_daily")["d"][0]
    pending = (cal_max is not None and
               (kline_max is None
                or pd.Timestamp(cal_max) > pd.Timestamp(kline_max)))
    if (not domains and not is_td and not pending and date is None):
        log.info(f"今日非交易日且无待补数据（日历至 {cal_max}，K线至 {kline_max}），"
                 f"执行轻量检查（日历/水位）")
        run("calendar", lambda: calendar.refresh_calendar())
        # 轻量路径复查：盘后运行时 refresh_calendar 会把"今天"补进日历，
        # 此时 cal_max > kline_max ⇒ 上一交易日数据待补，继续完整更新
        cal_max2 = store.q("SELECT max(trade_date) AS d FROM trade_calendar")["d"][0]
        kline_max2 = store.q("SELECT max(date) AS d FROM kline_daily")["d"][0]
        if (cal_max2 is not None and
                (kline_max2 is None
                 or pd.Timestamp(cal_max2) > pd.Timestamp(kline_max2))):
            log.info(f"日历刷新后出现待补数据（日历至 {cal_max2}，"
                     f"K线至 {kline_max2}），继续完整更新")
        else:
            broadcast("data", "数据更新·非交易日轻量检查", action="info",
                      detail=f"日历至 {cal_max2}，K线至 {kline_max2}，无待补数据",
                      impact="本轮无数据变更", source="update.py")
            return results

    # 目标交易日 = 日历中最后一个已完成交易日（fallback 校验基准）
    def _target_trade_date() -> pd.Timestamp:
        if date is not None:
            return pd.Timestamp(date)
        d = store.q("SELECT max(trade_date) AS d FROM trade_calendar")["d"][0]
        if d is None:
            return pd.Timestamp.now().normalize()
        return pd.Timestamp(d)

    # 1. 日历（顺延刷新未来）
    run("calendar", lambda: calendar.refresh_calendar())
    if domains and "calendar" not in domains:
        # 降级校验依赖"权威目标交易日"，部分域运行时日历也必须是新的，
        # 否则 calendar_max == kline_max 会让校验恒真、兜底永不触发
        try:
            calendar.refresh_calendar()
        except Exception as e:                               # noqa: BLE001
            log.warning(f"日历刷新失败（不影响主流程）: {e}")
    # 2. 股票主表 + 当日估值快照
    from .instruments import refresh_instruments, update_daily_snapshot
    from .sources import fsdb_fallback as fb

    target = _target_trade_date()

    def _advanced(table: str, key: str = "date"):
        """水位是否已推进到目标交易日"""
        def _v() -> bool:
            try:
                mx = store.q(f"SELECT max({key}) AS d FROM {table}")["d"][0]
            except Exception:                                # noqa: BLE001
                return False
            return mx is not None and pd.Timestamp(mx) >= target
        return _v

    # 兜底结果按日缓存：kline_daily 与 daily_snapshot 共用同一次 fsdb 拉取
    _fb_cache: dict = {}

    def _fallback_daily() -> dict:
        if "daily" not in _fb_cache:
            _fb_cache["daily"] = fb.backfill_kline_and_snapshot(store, target)
        r = _fb_cache["daily"]
        return {"kline": r["kline"], "snapshot": r["snapshot"],
                "miss": r["miss"]}

    def _fallback_index() -> dict:
        if "index" not in _fb_cache:
            _fb_cache["index"] = fb.backfill_index_and_calendar(store, target)
        return _fb_cache["index"]

    run("instruments", refresh_instruments)
    run_checked("daily_snapshot",
                lambda: update_daily_snapshot(date),
                _advanced("daily_snapshot"),
                _fallback_daily)
    # 3. K线增量（主源通达信；失败/未推进 → 自动降级 fsdb 日线）
    from .kline import update_kline, update_index_kline, init_kline
    if full_kline:
        run("kline_daily", lambda: init_kline())
    else:
        run_checked("kline_daily",
                    lambda: update_kline(),
                    _advanced("kline_daily"),
                    _fallback_daily)
    run_checked("index_kline",
                update_index_kline,
                _advanced("index_kline"),
                _fallback_index)
    # 3.5 分钟K线（free-stockdb 本地引擎：镜像增量同步 → 入湖）
    #     首次使用需先单独执行 cli.py data minute-init 全量回补
    from .minute import update_kline_1min
    from .sources.fsdb_source import sync as fsdb_sync
    run("fsdb_sync", fsdb_sync)
    run("kline_1min", update_kline_1min)
    # 3.6 ETF / 基金日线（fsdb 源；2026-09-11 接入，先日线后分钟）
    from .etf import update_etf_daily
    run("etf_daily", lambda: update_etf_daily(date))
    # 3.7 板块映射逐日快照（概念 + 申万一~三级）
    #     ⚠️ fsdb 只给最新快照、无历史版本 → 从启用日起前向积累才有 PIT 语义
    from .board import snapshot as board_snapshot
    run("board_map", lambda: board_snapshot(date))
    # 3.8 PIT 逐日股本（as-of 缺口根治，2026-09-11）
    #     源 = fsdb 日线 total_share/float_share（逐日真值，含送转/增发/回购）。
    #     因子侧 ASOF JOIN 取 <= t 最近一条，故每日只需补近 10 日切片承接修正。
    #     首次使用需先跑 scripts/backfill_share_capital.py 全量回填。
    from .share_capital import update as update_share_capital
    run("share_capital", lambda: update_share_capital(date))
    # 3.10 ETF 分钟（fsdb 源，2026-09-12 接入）
    #      边界 2025-01-02 起（与股票分钟同边界），单日 240~242 根**不固定**。
    #      湖 kline_1min_etf/part-{code}.parquet（按 code 平铺），与股票分钟分目录隔离。
    #      首次使用需先跑 scripts/backfill_etf_dataset.py --what minute 全量回补。
    from .etf_minute import update as update_etf_minute
    run("etf_minute", lambda: update_etf_minute(date))
    # 3.11 ETF 净值（westock 腾讯源，2026-09-12 接入）
    #      ⚠️ 单次返回硬上限 ~1211 行且**超限静默截断为最近 N 行** → 必须切段 ≤3 年。
    #      首次使用需先跑 scripts/backfill_etf_dataset.py --what nav 全量回补（可达上市首日）。
    from .etf_nav import update as update_etf_nav
    run("etf_nav", lambda: update_etf_nav(date))
    # 3.12 ETF 持仓/申赎清单快照（westock 源，2026-09-12 接入）
    #      ⚠️ `--date` 参数不生效 = 快照型，历史不可回补 → 只能前向逐日积累。
    from .etf_holding import snapshot as etf_holding_snapshot
    run("etf_holding", lambda: etf_holding_snapshot(date))

    # 3.9 逐日池快照（残留 as-of 根治，2026-09-12）
    #     universe.py 用当前 ST/上市状态/最新市值建池，历史回测带幸存者偏差。
    #     本表每日落一份当日各池成员（严格 PIT），前向积累；
    #     历史段已用 valuation_daily.is_st（逐日真值）重建（source='recon'）。
    #     首次使用需先跑 scripts/backfill_universe_daily.py。
    from .universe_daily import record as record_universe_daily
    run("universe_daily", lambda: record_universe_daily(date))
    for _d, _desc in (("etf_daily", "ETF/基金日线（含 is_etf 标记）"),
                      ("board_map", "板块映射逐日快照（概念+申万一~三级）"),
                      ("share_capital", "PIT 逐日股本（total/float shares）"),
                      ("universe_daily", "逐日股票池快照（PIT 池成员）"),
                      ("etf_minute", "ETF/基金分钟K（fsdb，2025-01-02 起）"),
                      ("etf_nav", "ETF/基金净值（westock，含折溢价）"),
                      ("etf_holding", "ETF 持仓/申赎清单逐日快照")):
        try:
            store.register_dataset(_d, "clean", "free-stockdb", "daily", _desc)
        except Exception as e:                               # noqa: BLE001
            log.debug(f"注册数据集 {_d} 失败: {e}")
    # 水位必须取"实际落库的最新日期"，不能用请求的 target：
    # 例如 fsdb 尚未同步当日数据时 etf_daily 会写 0 行，若按 target 记水位
    # 就会让水位跑到数据前面（下游误判当日已有数据）
    def _etf_actual_max():
        try:
            from .etf import load_etf_daily
            df = load_etf_daily(etf_only=False)
            return df["date"].max() if len(df) else None
        except Exception:                                    # noqa: BLE001
            return None

    def _board_actual_max():
        try:
            from .board import _latest_snapshot, META_DIR
            return _latest_snapshot(META_DIR, None)
        except Exception:                                    # noqa: BLE001
            return None

    def _pq_max(subdir: str, col: str):
        """读 parquet 目录的最大时间值（新域水位用，直扫数据层）"""
        import duckdb
        from ..config import CLEAN_DIR
        pat = str(CLEAN_DIR / subdir / "**" / "*.parquet").replace("\\", "/")
        con = duckdb.connect()
        try:
            v = con.execute(
                f"SELECT max({col}) FROM "
                f"read_parquet('{pat}', hive_partitioning=false)").fetchone()[0]
            return pd.Timestamp(v) if v is not None else None
        except Exception:                                    # noqa: BLE001
            return None
        finally:
            con.close()

    for _d, _actual in (("etf_daily", _etf_actual_max()),
                        ("board_map", _board_actual_max()),
                        ("etf_minute", _pq_max("kline_1min_etf", "datetime")),
                        ("etf_nav", _pq_max("etf_nav", "date")),
                        ("etf_holding", _pq_max("etf_holding", "snap"))):
        if not str(results.get(_d, "")).startswith(("ok", "degraded")):
            continue
        if _actual is None:
            log.warning(f"{_d} 未写入任何数据，跳过水位登记")
            continue
        try:
            store.set_watermark(_d, _actual)
        except Exception as e:                               # noqa: BLE001
            log.debug(f"设置水位 {_d} 失败: {e}")
    # 4. 特色数据
    from .feature.dragon_tiger import update_dragon_tiger
    from .feature.eastmoney_features import (update_margin, update_lockup,
                                             update_block_trade)
    from .feature.ths_features import update_hot_topic, update_northbound
    from .feature.fund_flow import update_fund_flow
    run("dragon_tiger", lambda: update_dragon_tiger(date))
    run("margin_total", lambda: update_margin(date))
    run("lockup", lambda: update_lockup(date))
    run("block_trade", lambda: update_block_trade(date))
    run("hot_topic", lambda: update_hot_topic(date))
    run("northbound_daily", lambda: update_northbound(date))
    # 5. 低频域（按水位间隔触发）
    wm = store.get_watermark("finance_snapshot")
    if _days_since(wm) >= 7:
        from .financial import init_financial
        run("finance_snapshot", init_financial)
    wm = store.get_watermark("finance_history")
    if _days_since(wm) >= 7:
        from .finance_history import update_finance_history
        run("finance_history", update_finance_history)
    wm = store.get_watermark("holder_num")
    if _days_since(wm) >= 7:
        # 2026-09-07 切换至全历史版（RPT_HOLDERNUM_DET，披露日 PIT；
        # 旧"最新一期快照"版已废弃，旧表留档 holder_num_legacy_20260907）
        from .holder_num import update_holder_num
        run("holder_num", update_holder_num)
    wm = store.get_watermark("perf_forecast")
    if _days_since(wm) >= 7:
        from .perf_forecast import update_perf_forecast
        run("perf_forecast", update_perf_forecast)
    wm = store.get_watermark("index_members")
    if _days_since(wm) >= 7:
        from .universe import refresh_index_members
        run("index_members", refresh_index_members)
    wm = store.get_watermark("industry_map")
    if _days_since(wm) >= 7:
        from .instruments import backfill_industry
        run("industry_map", backfill_industry)
    # 6. 资金流（探测可用才跑，抽市值前500）
    from .feature.fund_flow import probe_available
    if probe_available():
        def _ff():
            codes = store.q("""
                SELECT s.code FROM daily_snapshot s
                JOIN instruments i ON i.code = s.code
                WHERE s.date = (SELECT MAX(date) FROM daily_snapshot)
                  AND NOT i.is_st AND i.board != 'BJ'
                ORDER BY s.float_mcap_yi DESC NULLS LAST LIMIT 500""")
            from .feature.fund_flow import update_fund_flow
            return update_fund_flow(codes=codes["code"].tolist())
        run("fund_flow_daily", _ff)
    else:
        results["fund_flow_daily"] = "degraded (push2his 不可达)"

    log.info("更新完成: " + " | ".join(f"{k}:{v.split(' ')[0]}" for k, v in results.items()))

    # 导出 Parquet 镜像（供写锁被占时的无锁查询兜底）
    try:
        exported = store.export_mirror()
        log.info(f"镜像导出 {len(exported)} 表")
    except Exception as e:
        log.warning(f"镜像导出失败（不影响数据更新）: {e}")
    n_fail = sum(1 for v in results.values() if "FAIL" in str(v))
    n_deg = sum(1 for v in results.values() if str(v).startswith("degraded"))
    broadcast("data", "数据日度更新完成", action="change",
              detail=" | ".join(f"{k}:{v.split(' ')[0]}"
                                for k, v in results.items())[:800],
              impact=(f"水位已推进，Parquet 镜像已导出；"
                      f"{n_fail} 域失败 / {n_deg} 域降级（详见上方 incident 广播）"
                      if (n_fail or n_deg) else
                      "水位已推进，Parquet 镜像已导出，下游因子/信号可消费最新数据"),
              source="update.py")
    return results


def update_plan() -> pd.DataFrame:
    """数据域状态总览：水位/新鲜度/行数（只读，可镜像降级）"""
    from .store import query
    wm = query("""
        SELECT w.domain, w.partition, w.watermark, w.updated_at,
               d.frequency, d.source
        FROM watermarks w LEFT JOIN datasets d USING(domain)
        ORDER BY w.domain""")
    import duckdb as _ddb

    def _q(sql):
        try:
            return query(sql)
        except Exception:
            return None

    rows = []
    for _, w in wm.iterrows():
        table = w["domain"]
        cnt_df = _q(f"SELECT COUNT(*) AS n, MAX(date) AS d FROM {table}")
        if cnt_df is None or cnt_df.empty:
            cnt, latest = "-", "-"
        else:
            cnt = int(cnt_df.iloc[0, 0])
            latest = cnt_df.iloc[0, 1]
        rows.append({
            "domain": w["domain"], "source": w.get("source"),
            "frequency": w.get("frequency"),
            "watermark": str(w["watermark"]),
            "latest_row": str(latest)[:10] if latest is not None and str(latest) != "None" else "-",
            "rows": cnt,
            "days_ago": _days_since(w["watermark"]) if w["watermark"] else None,
        })
    return pd.DataFrame(rows)
