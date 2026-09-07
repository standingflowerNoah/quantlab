#!/usr/bin/env python3
"""QuantLab CLI — agent 与人类共用的确定性操作入口

用法:
  python3 cli.py data init [--kline-only|--fin-only]   全量首建
  python3 cli.py data update [--domain kline_daily,...] [--full-kline]
  python3 cli.py data quality [--sample 30]            数据体检
  python3 cli.py data status                           数据域水位总览
  python3 cli.py data minute-init [--restart]          分钟K全量回补(free-stockdb)
  python3 cli.py data minute-update                    分钟K增量(同步+入湖)
  python3 cli.py data minute-status                    分钟层覆盖统计
  python3 cli.py data minute-validate [--sample 50]    分钟对账日线
  python3 cli.py query "SELECT ..."                    SQL 查询数据湖
  python3 cli.py universe [name]                       股票池规模一览
  python3 cli.py data test-adj [--code 600519]         复权因子校验
  python3 cli.py factor list                           因子清单
  python3 cli.py factor compute momentum_20 [--universe ashare_ex]
  python3 cli.py factor summary momentum_20 [--horizon 5]
  python3 cli.py model backtest [--factors f1,f2,...]  多因子选股回测
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantlab.config import get_logger  # noqa: E402

log = get_logger("cli")


def cmd_data(args: list[str]):
    from quantlab.data.store import Store
    sub = args[0] if args else "status"
    rest = args[1:]

    if sub == "init":
        import subprocess
        log.info("执行全量首建（scripts/init_data.py）...")
        subprocess.run([sys.executable,
                        str(Path(__file__).parent / "scripts" / "init_data.py")]
                       + rest, check=False)
    elif sub == "update":
        from quantlab.data.update import update_all
        domains = None
        full_kline = "--full-kline" in rest
        for i, a in enumerate(rest):
            if a == "--domain" and i + 1 < len(rest):
                domains = rest[i + 1].split(",")
        t0 = time.time()
        results = update_all(domains=domains, full_kline=full_kline)
        for k, v in results.items():
            print(f"  {k:20s} {v}")
        print(f"总耗时 {time.time()-t0:.0f}s")
    elif sub == "quality":
        from quantlab.data.quality import check_all
        sample = 30
        for i, a in enumerate(rest):
            if a == "--sample" and i + 1 < len(rest):
                sample = int(rest[i + 1])
        df = check_all(sample=sample)
        print(df.to_string(index=False))
    elif sub == "status":
        from quantlab.data.update import update_plan
        print(update_plan().to_string(index=False))
    elif sub == "test-adj":
        from quantlab.data.kline import verify_adj_factor
        code = "600519"
        for i, a in enumerate(rest):
            if a == "--code" and i + 1 < len(rest):
                code = rest[i + 1]
        print(verify_adj_factor(code))
    elif sub == "minute-init":
        from quantlab.data.minute import init_kline_1min
        r = init_kline_1min(resume="--restart" not in rest)
        print(r)
    elif sub == "minute-update":
        from quantlab.data.minute import update_kline_1min
        from quantlab.data.sources.fsdb_source import sync
        print("fsdb 同步:", sync())
        print("入湖:", update_kline_1min())
    elif sub == "minute-status":
        from quantlab.data.minute import minute_status
        print(minute_status().to_string(index=False))
    elif sub == "minute-validate":
        from quantlab.data.minute import validate_minute
        sample = 50
        for i, a in enumerate(rest):
            if a == "--sample" and i + 1 < len(rest):
                sample = int(rest[i + 1])
        print(validate_minute(sample=sample).to_string(index=False))
    elif sub == "minute-feat":
        from quantlab.data.minute_feat import (build_minute_feat, ensure_view,
                                               feat_status)
        print("构建:", build_minute_feat())
        ensure_view()
        print(feat_status().to_string(index=False))
    elif sub == "minute-feat-status":
        from quantlab.data.minute_feat import feat_status
        print(feat_status().to_string(index=False))
    else:
        print(__doc__)


def cmd_query(args: list[str]):
    import pandas as pd
    from quantlab.data.store import query
    if not args:
        print("用法: cli.py query \"SELECT ...\"")
        return
    sql = " ".join(args)
    df = query(sql)
    if df.empty:
        print("(空结果)")
    else:
        with pd.option_context("display.max_rows", 60,
                               "display.width", 200):
            print(df.to_string(index=False))
        print(f"\n({len(df)} 行)")


def cmd_universe(args: list[str]):
    from quantlab.data.universe import describe_universes, get_universe
    if args:
        codes = get_universe(args[0])
        print(f"{args[0]}: {len(codes)} 只")
    else:
        print(describe_universes().to_string(index=False))


def cmd_factor(args: list[str]):
    import pandas as pd
    from quantlab.factor import (list_factors, compute_factor, compute_all,
                                 factor_ic, factor_summary, factor_backtest)
    if not args:
        print("用法: cli.py factor list|compute|compute-all|ic|summary|backtest [因子名] [选项]")
        return
    sub = args[0]
    rest = args[1:]

    def _opt(*names):
        for i, a in enumerate(rest):
            if a in names and i + 1 < len(rest):
                return rest[i + 1]
        return None

    if sub == "list":
        print(list_factors().to_string(index=False))
    elif sub == "compute":
        name = rest[0] if rest else ""
        df = compute_factor(
            name,
            universe=_opt("--universe"),
            start=_opt("--start"), end=_opt("--end"))
        if df.empty:
            print(f"{name}: (空结果)")
        else:
            print(f"{name}: {len(df)} 行, {df['code'].nunique()} 只, "
                  f"{df['date'].min().date()} ~ {df['date'].max().date()}")
    elif sub == "compute-all":
        compute_all(universe=_opt("--universe"))
    elif sub == "backtest":
        name = rest[0] if rest else ""
        h = _opt("--horizon")
        q = _opt("--quantiles")
        res = factor_backtest(
            name,
            horizon=int(h) if h else 5,
            n_quantiles=int(q) if q else 5,
            universe=_opt("--universe"))
        print(f"\n=== {name} 分层回测（horizon={res['horizon']}日, "
              f"{res['n_quantiles']}层, 单调性IC={res['monotonic_ic']}）===")
        print(res["summary"].to_string(index=False))
        print(f"\n多空组合净值: 期末 {res['curve']['nav'].iloc[-1]:.4f} "
              f"（起 {res['curve']['date'].iloc[0].date()} ~ "
              f"止 {res['curve']['date'].iloc[-1].date()}）")
    elif sub in ("ic", "summary"):
        name = rest[0] if rest else ""
        h = _opt("--horizon")
        horizon = int(h) if h else 5
        if sub == "ic":
            print(factor_ic(name, horizon=horizon).to_string(index=False))
        else:
            print(factor_summary(name, horizon=horizon).to_string(index=False))
    elif sub == "corr":
        from quantlab.factor import factor_correlation
        names = rest if rest else [
            "reversal_5", "reversal_10", "momentum_20", "momentum_60",
            "volatility_20", "turnover", "size", "rsi_14",
            "amplitude_20", "max_return_20"]
        print("因子截面秩相关矩阵:")
        print(factor_correlation(names).to_string())
    else:
        print(__doc__)


def cmd_model(args: list[str]):
    from quantlab.model import build_composite, run_backtest
    if not args:
        print("用法: cli.py model backtest [--factors f1,f2,...] [--n 50] [--rebalance 20]")
        return
    sub = args[0]
    rest = args[1:]

    def _opt(*names):
        for i, a in enumerate(rest):
            if a in names and i + 1 < len(rest):
                return rest[i + 1]
        return None

    if sub == "backtest":
        factors = (_opt("--factors") or "reversal_5,size,turnover").split(",")
        n = int(_opt("--n") or 50)
        reb = int(_opt("--rebalance") or 20)
        score = build_composite(factors, universe="ashare_ex")
        res = run_backtest(score, n_stocks=n, rebalance=reb)
        m, bm = res["metrics"], res["metrics_bm"]
        print(f"\n多因子组合: {factors}（top {n} 只，{reb} 日调仓）")
        print(f"  组合: 年化 {m['annual_return']:.1%} | 累计 {m['total_return']:.1%} "
              f"| 夏普 {m['sharpe']} | 最大回撤 {m['max_drawdown']:.1%}")
        print(f"  基准: 年化 {bm['annual_return']:.1%} | 累计 {bm['total_return']:.1%} "
              f"| 夏普 {bm['sharpe']} | 最大回撤 {bm['max_drawdown']:.1%}")
        print(f"  超额: 年化 {m['annual_return'] - bm['annual_return']:.1%} "
              f"| 平均换手 {res['turnover_avg']:.0%} | 单次换手成本 {res['cost_per_turnover']:.2%}")
    elif sub == "walkforward":
        from quantlab.model.walkforward import split_backtest, compare_summary
        factors = (_opt("--factors") or "reversal_5,size,turnover").split(",")
        n = int(_opt("--n") or 100)
        split = _opt("--split") or "2025-01-01"
        score = build_composite(factors, universe="ashare_ex")
        in_r, out_r = split_backtest(score, split_date=split, n_stocks=n)
        print(f"\n样本内外对比（split={split}，{factors}）")
        print(compare_summary(in_r, out_r).to_string(index=False))
    else:
        print(__doc__)


def cmd_optimize(args: list[str]):
    from quantlab.model import build_composite
    from quantlab.optimize import run_optimized_backtest
    if not args:
        print("用法: cli.py optimize backtest [--factors f1,f2] [--method min_var] [--n 50] [--rebalance 20]")
        print("      cli.py optimize compare    # 对比全部权重方案")
        return
    sub = args[0]
    rest = args[1:]

    def _opt(*names):
        for i, a in enumerate(rest):
            if a in names and i + 1 < len(rest):
                return rest[i + 1]
        return None

    factors = (_opt("--factors") or "reversal_5,size,turnover").split(",")
    n = int(_opt("--n") or 50)
    reb = int(_opt("--rebalance") or 20)

    if sub == "compare":
        score = build_composite(factors)
        print(f"\n因子: {factors} | top {n} | {reb}日调仓")
        print(f"{'方案':14s}{'年化':>8s}{'超额':>8s}{'夏普':>7s}{'回撤':>8s}")
        for method in ["equal", "inverse_vol", "min_var", "risk_parity"]:
            r = run_optimized_backtest(score, n_stocks=n, rebalance=reb,
                                       method=method)
            m, bm = r["metrics"], r["metrics_bm"]
            print(f"{method:14s}{m['annual_return']:>7.1%}"
                  f"{m['annual_return']-bm['annual_return']:>8.1%}"
                  f"{m['sharpe']:>7.2f}{m['max_drawdown']:>8.1%}")
    elif sub == "backtest":
        method = _opt("--method") or "min_var"
        lookback = int(_opt("--lookback") or 60)
        score = build_composite(factors)
        r = run_optimized_backtest(score, n_stocks=n, rebalance=reb,
                                   method=method, lookback=lookback)
        m, bm = r["metrics"], r["metrics_bm"]
        print(f"\n权重优化组合: {factors}（top {n}，{reb}日调仓，方案 {method}）")
        print(f"  组合: 年化 {m['annual_return']:.1%} | 夏普 {m['sharpe']} | 最大回撤 {m['max_drawdown']:.1%}")
        print(f"  基准: 年化 {bm['annual_return']:.1%} | 夏普 {bm['sharpe']} | 最大回撤 {bm['max_drawdown']:.1%}")
        print(f"  超额: 年化 {m['annual_return']-bm['annual_return']:.1%}")
    else:
        print(__doc__)


def cmd_decision(args: list[str]):
    import pandas as pd
    from quantlab.model import build_composite
    from quantlab.decision import generate_target, diff_orders, load_state, save_state
    if not args:
        print("用法: cli.py decision target|rebalance|run [--date YYYY-MM-DD] [--n 100] [--method inverse_vol]")
        return
    sub = args[0]
    rest = args[1:]

    def _opt(*names):
        for i, a in enumerate(rest):
            if a in names and i + 1 < len(rest):
                return rest[i + 1]
        return None

    n = int(_opt("--n") or 100)
    method = _opt("--method") or "inverse_vol"
    date = _opt("--date")
    if _opt("--factors"):
        # 自定义因子合成（研究用）
        factors = _opt("--factors").split(",")
        score = build_composite(factors, universe="ashare_ex")
    else:
        # 默认生产模型：池内精选（size+amihud 选 400 → dragon_net_20 精选）
        from quantlab.model.pool_select import build_production_score
        score = build_production_score()

    if sub == "target":
        t = generate_target(score, date=date, n_stocks=n, method=method)
        print(f"\n目标持仓（{t['weight'].sum():.1%} 仓位，{len(t)} 只，"
              f"截至 {score['date'].max().date()}）")
        print(t.to_string(index=False, float_format=lambda x: f"{x:.2%}"))
    elif sub == "rebalance":
        t = generate_target(score, date=date, n_stocks=n, method=method)
        cur = load_state()
        orders = diff_orders(cur, t)
        print(f"\n当前持仓 {len(cur)} 只 → 目标 {len(t)} 只")
        if orders.empty:
            print("无需调仓")
        else:
            print(orders.to_string(index=False,
                                   float_format=lambda x: f"{x:.2%}"))
    elif sub == "run":
        t = generate_target(score, date=date, n_stocks=n, method=method)
        cur = load_state()
        orders = diff_orders(cur, t)
        save_state(t, date=score["date"].max().date())
        print(f"\n[决策完成] 目标持仓 {len(t)} 只，仓位 {t['weight'].sum():.1%}")
        if orders.empty:
            print("无需调仓")
        else:
            print(f"调仓指令 {len(orders)} 条（已写入 portfolio_state/）")
            print(orders.to_string(index=False,
                                   float_format=lambda x: f"{x:.2%}"))
    elif sub == "timing":
        from quantlab.decision.timing import market_timing
        pos = market_timing()
        recent = pos.tail(1)
        if recent.empty:
            print("择时信号为空")
        else:
            p = float(recent.iloc[0]["position"])
            d = recent.iloc[0]["date"].date()
            print(f"当前择时仓位（截至 {d}）: {p:.0%}"
                  f"（{'满仓' if p >= 1 else '降仓'}）")
    else:
        print(__doc__)


def main():
    import pandas as pd  # noqa: F401
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    if cmd == "data":
        cmd_data(args)
    elif cmd == "query":
        cmd_query(args)
    elif cmd == "universe":
        cmd_universe(args)
    elif cmd == "factor":
        cmd_factor(args)
    elif cmd == "model":
        cmd_model(args)
    elif cmd == "optimize":
        cmd_optimize(args)
    elif cmd == "decision":
        cmd_decision(args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
