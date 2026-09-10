"""因子审查模块（构建即审查，防穿越）
=====================================
每个因子在构建/入库时自动审查并记录结果到 data/lake/factor_audit/<name>.json。

审查项：
1. structure  结构：行数/日期范围/股票数/重复(date,code)行/NaN+inf
2. distribution 数值分布：非零率、P1/P50/P99 极值
3. coverage   截面覆盖：每交易日股票数 min/median/max
4. ic         IC 检验：IC5/IC20、ICIR、胜率、方向（alpha191 复用已有 IC 结果）
5. pit        穿越自检（point-in-time，仅 SqlFactor）：
              把底层数据截断到 t0，重算 t0 截面并与全量版对比——
              任何不一致 = 因子使用了 t0 之后的数据（穿越）。
              事件表严格截断 < t0（盘后披露），行情/快照表 <= t0，
              财务快照/股票主表/日历不截断但标记 as-of 警告。

verdict：FAIL（穿越/重复行/inf）> WARN（as-of 缺口/覆盖异常）> PASS

背景：2026-09-05 dragon_net_20 窗口方向写反（t 日值含未来事件，ICIR 虚高
2.3），靠本模块的 pit 检验可在构建时拦截。教训见 reports/dragon_net_20_review.md。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)

AUDIT_DIR = Path("data/lake/factor_audit")
AUDIT_VERSION = "1.0"          # 构造/审查逻辑变更时 bump，旧记录视为失效
IC_CACHE_CSV = Path("reports/alpha191_ic.csv")

# ── PIT 截断规则：表名 → 截断方式 ────────────────────────────────────
# LE：date <= t0（t0 收盘后可得的数据）
_PIT_LE = {"kline_daily", "index_kline", "daily_snapshot"}
# LT：date < t0（盘后披露的事件数据，t0 当日事件只能记到 t0+1）
_PIT_LT = {"dragon_tiger", "block_trade", "lockup", "margin_total",
           "northbound_daily", "hot_topic", "fund_flow_daily"}
# 财务历史：按 notice_eff（法定披露截止保守化的生效日）截断 <= t0
_PIT_NOTICE = {"finance_history"}
# KEEP：不截断（无时点信息 / 事前公告 / 会误清空），但引用即标记 as-of 警告
# lockup：解禁日期事前公告（t 日已知未来 60 日计划），属合法前瞻，全量保留
_PIT_KEEP = {"trade_calendar", "finance_snapshot", "instruments", "index_members",
             "lockup"}
_ASO_TABLES = {"finance_snapshot", "instruments", "index_members"}


# ── 1~3：结构 / 分布 / 覆盖 ─────────────────────────────────────────
def structural_audit(df: pd.DataFrame) -> dict:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    dup = int(d.duplicated(subset=["date", "code"]).sum())
    inf_n = int(np.isinf(pd.to_numeric(d["value"], errors="coerce")).sum())
    return {
        "n_rows": int(len(d)),
        "date_min": str(d["date"].min().date()) if len(d) else None,
        "date_max": str(d["date"].max().date()) if len(d) else None,
        "n_codes": int(d["code"].nunique()),
        "dup_date_code_rows": dup,
        "inf_values": inf_n,
        "nan_values": int(d["value"].isna().sum()),
    }


def distribution_audit(df: pd.DataFrame) -> dict:
    v = pd.to_numeric(df["value"], errors="coerce").abs()
    nonzero = v[v > 1e-12]
    q = v.quantile([0.01, 0.5, 0.99]).round(6).tolist()
    return {
        "nonzero_ratio": round(float(len(nonzero) / len(v)), 4) if len(v) else 0.0,
        "abs_q01_q50_q99": q,
        "abs_max": round(float(v.max()), 6) if len(v) else 0.0,
    }


def coverage_audit(df: pd.DataFrame) -> dict:
    per_day = df.groupby("date")["code"].nunique()
    return {
        "n_days": int(len(per_day)),
        "codes_per_day_min": int(per_day.min()) if len(per_day) else 0,
        "codes_per_day_median": int(per_day.median()) if len(per_day) else 0,
        "codes_per_day_max": int(per_day.max()) if len(per_day) else 0,
    }


# ── 4：IC 审查 ──────────────────────────────────────────────────────
def ic_audit(name: str) -> dict:
    """IC 检验（深审查时全量重算；alpha191 复用批量 IC 结果避免重复计算）"""
    try:
        if re.fullmatch(r"alpha\d{3}", name) and IC_CACHE_CSV.exists():
            cache = pd.read_csv(IC_CACHE_CSV)
            row = cache[cache["factor"] == name]
            if len(row):
                r = row.iloc[0]
                return {"ic5": float(r["ic5"]), "icir5": float(r["icir5"]),
                        "win5": float(r["win5"]), "ic20": float(r["ic20"]),
                        "icir20": float(r["icir20"]), "direction": 1 if r["ic5"] > 0 else -1,
                        "source": "cached_alpha191_ic"}
        from .quality import factor_summary
        s5 = factor_summary(name, horizon=5).iloc[0]
        s20 = factor_summary(name, horizon=20).iloc[0]
        return {"ic5": float(s5["ic_mean"]), "icir5": float(s5["icir"]),
                "win5": float(s5["ic_win_rate"]), "ic20": float(s20["ic_mean"]),
                "icir20": float(s20["icir"]),
                "direction": 1 if s5["ic_mean"] > 0 else -1,
                "source": "computed"}
    except Exception as e:
        return {"error": str(e)[:120]}


# ── 5：PIT 穿越自检 ─────────────────────────────────────────────────
def _real_tables(store: Store) -> set[str]:
    rows = store.q(
        "SELECT DISTINCT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'")
    return set(rows.iloc[:, 0])


def _has_date_col(store: Store, t: str) -> bool:
    rows = store.q(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? AND data_type LIKE '%DATE%'", [t])
    return "date" in set(rows.iloc[:, 0].str.lower())


def _rewrite_sql(sql: str, schema: str, tables: set[str]) -> str:
    """把 SQL 中引用的真实表名重写为 schema.表名（CTE 名不在 tables 中，不受影响）"""
    def repl(m):
        name = m.group(2)
        if name.lower() in tables:
            return f"{m.group(1)} {schema}.{name}"
        return m.group(0)
    return re.sub(r"(FROM|JOIN)\s+([a-zA-Z_]\w*)", repl, sql, flags=re.I)


def _build_cut_env(store: Store, sql: str, t0, pit_summary: dict) -> str | None:
    """在内存 schema 中构建 t0 时点的截断数据环境，返回重写后的 SQL"""
    all_real = _real_tables(store)
    cands = {t.lower() for t in re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", sql, re.I)}
    used = {t for t in cands if t in all_real}
    if not used:
        return None
    try:
        store.con.execute("DETACH audit_mem")
    except Exception:
        pass
    store.con.execute("ATTACH ':memory:' AS audit_mem")
    for t in sorted(used):
        if t in _PIT_KEEP:
            src = t                                  # 全量复制
            conds = ""
        elif t in _PIT_NOTICE:
            # 财务历史：按生效日 notice_eff 截断（法定披露截止保守化）
            src, conds = t, f" WHERE notice_eff <= DATE '{t0}'"
        elif t in _PIT_LE or (t not in _PIT_LT and _has_date_col(store, t)):
            src, conds = t, f" WHERE date <= DATE '{t0}'"   # 行情/快照
        elif _has_date_col(store, t):
            src, conds = t, f" WHERE date < DATE '{t0}'"    # 事件（盘后披露）
        else:
            src, conds = t, ""                       # 无日期列 → 全量
        store.con.execute(
            f"CREATE TABLE audit_mem.{t} AS SELECT * FROM {src}{conds}")
        if conds:
            pit_summary["truncated"][t] = conds.strip()
    return _rewrite_sql(sql, "audit_mem", used)


def pit_audit(factor, df_full: pd.DataFrame, n_t0: int = 3) -> dict:
    """穿越自检：截断底层数据重算 t0 截面 vs 全量版对比

    返回 {status: PASS|FAIL|SKIP, t0s, n_diff_detail, truncated, asof_caveat}
    """
    out: dict = {"status": "SKIP", "t0s": [], "truncated": {},
                 "asof_caveat": [], "detail": []}
    if not isinstance(factor, type) and not hasattr(factor, "_sql"):
        if not hasattr(factor, "_sql"):
            out["detail"].append("非 SqlFactor（宽表/Python 构造），PIT 由构造保证，未执行")
            return out
    store = Store()
    sql, params = factor._sql(None, None, None)
    # 引用快照/主表的因子：声明 as-of 缺口（温和的未来数据，非硬穿越）
    cands = {t.lower() for t in re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", sql, re.I)}
    out["asof_caveat"] = sorted(cands & _ASO_TABLES)

    dates = sorted(pd.to_datetime(df_full["date"]).unique())
    if len(dates) < 30:
        out["detail"].append("截面数不足 30，PIT 跳过")
        return out
    idx = [int(len(dates) * p) for p in (0.50, 0.75, 0.92)][:n_t0]
    t0s = [pd.Timestamp(dates[i]) for i in idx]
    out["t0s"] = [str(t.date()) for t in t0s]

    total_diff = 0
    for t0 in t0s:
        cut_sql = _build_cut_env(store, sql, t0.date(), out)
        if cut_sql is None:
            out["status"] = "SKIP"
            out["detail"].append(f"t0={t0.date()}: SQL 未引用可截断表")
            continue
        df_cut = store.q(cut_sql, params)
        a = (df_full[pd.to_datetime(df_full["date"]) == t0]
             .set_index("code")["value"])
        if df_cut.empty:
            b = pd.Series(dtype=float)
        else:
            b = (df_cut[df_cut["date"] == t0].set_index("code")["value"])
        common = a.index.union(b.index if len(b) else a.index)
        va = a.reindex(common).fillna(0.0)
        vb = b.reindex(common).fillna(0.0) if len(b) else pd.Series(0.0, index=common)
        diff = (va - vb).abs()
        n_diff = int((diff > 1e-9).sum())
        total_diff += n_diff
        worst = diff.idxmax() if n_diff else None
        out["detail"].append({
            "t0": str(t0.date()), "n_codes": int(len(common)),
            "n_diff": n_diff,
            "sample": {"code": str(worst),
                       "full": round(float(va[worst]), 8),
                       "cut": round(float(vb[worst]), 8)} if n_diff else None})
        try:
            store.con.execute("DETACH audit_mem")
        except Exception:
            pass

    out["status"] = "FAIL" if total_diff > 0 else "PASS"
    return out


# ── 汇总 ────────────────────────────────────────────────────────────
def audit_factor(name: str, factor=None, df: pd.DataFrame | None = None,
                 deep: bool = True, force: bool = False) -> dict:
    """审查单个因子并落盘记录（data/lake/factor_audit/<name>.json）"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    from .registry import get_factor
    if factor is None:
        try:
            factor = get_factor(name)
        except Exception:
            factor = None

    if df is None:
        df = _load_factor_values(name)
    if df is None or df.empty:
        rec = {"factor": name, "audit_version": AUDIT_VERSION,
               "audited_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "verdict": "WARN",
               "issues": ["因子库无数据（等待底层数据积累，如财务快照单期限制）"],
               "checks": {}}
        _save_rec(rec)
        return rec

    issues: list[str] = []
    checks = {
        "structure": structural_audit(df),
        "distribution": distribution_audit(df),
        "coverage": coverage_audit(df),
    }

    if checks["structure"]["dup_date_code_rows"] > 0:
        issues.append(f"重复(date,code)行 {checks['structure']['dup_date_code_rows']}")
    if checks["structure"]["inf_values"] > 0:
        issues.append(f"inf 值 {checks['structure']['inf_values']} 个")

    if deep or (re.fullmatch(r"alpha\d{3}", name) and IC_CACHE_CSV.exists()):
        checks["ic"] = ic_audit(name)
        if factor is not None:
            pit = pit_audit(factor, df)
            checks["pit"] = pit
            if pit["status"] == "FAIL":
                issues.append("PIT 穿越自检 FAIL：截断重算与全量不一致（使用了未来数据）")
            elif pit["asof_caveat"]:
                issues.append(f"as-of 缺口：引用快照类表 {pit['asof_caveat']}（当前快照回填历史）")
        else:
            checks["pit"] = {"status": "SKIP", "detail": ["非 registry 因子"]}
    else:
        # quick 模式：IC/PIT 复用旧审查结果（构造未变则历史结论仍有效）。
        # 2026-09-08 事故：增量审计曾把带 IC 的旧记录覆盖成无 IC 记录，
        # 导致看板 75 个因子显示"未评"——根因是实现与注释不符（未复用）。
        old = _read_rec(name)
        carried_ic = carried_pit = False
        if old and old.get("audit_version") == AUDIT_VERSION:
            oc = old.get("checks") or {}
            ic_old = oc.get("ic") or {}
            if ic_old.get("ic20") is not None:
                checks["ic"] = ic_old
                carried_ic = True
            pit_old = oc.get("pit") or {}
            if pit_old.get("status") in ("PASS", "FAIL"):
                checks["pit"] = pit_old
                carried_pit = True
                if pit_old["status"] == "FAIL":
                    issues.append("PIT 穿越自检 FAIL：截断重算与全量不一致（复用旧审查）")
                elif pit_old.get("asof_caveat"):
                    issues.append(f"as-of 缺口：引用快照类表 {pit_old['asof_caveat']}"
                                  "（复用旧审查）")
        if not carried_ic:
            log.warning(f"[audit] {name}: quick 模式且旧记录无 IC，评级将缺失"
                        "（可跑 scripts/reaudit_ic_backfill.py 补填）")
        if not carried_pit:
            checks["pit"] = {"status": "SKIP", "detail": ["quick 模式"]}

    if checks["coverage"]["codes_per_day_median"] < 50:
        issues.append(f"截面覆盖中位数仅 {checks['coverage']['codes_per_day_median']} 只")

    if any("PIT 穿越自检 FAIL" in i for i in issues) or \
       checks["structure"]["dup_date_code_rows"] > 0 or \
       checks["structure"]["inf_values"] > 0:
        verdict = "FAIL"
    elif issues:
        verdict = "WARN"
    else:
        verdict = "PASS"

    rec = {
        "factor": name,
        "category": getattr(factor, "category", "alpha191" if re.fullmatch(
            r"alpha\d{3}", name) else "unknown"),
        "description": getattr(factor, "description", ""),
        "audit_version": AUDIT_VERSION,
        "audited_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "deep": deep,
        "elapsed_s": round(time.time() - t_start, 1),
        "latest_date": checks["structure"]["date_max"],
        "n_rows": checks["structure"]["n_rows"],
        "checks": checks,
        "verdict": verdict,
        "issues": issues,
    }
    _save_rec(rec)
    log.info(f"[audit] {name}: {verdict} ({'; '.join(issues) if issues else 'ok'})"
             f" [{rec['elapsed_s']}s]")
    return rec


def maybe_audit(factor, df: pd.DataFrame) -> dict | None:
    """compute_factor(save=True) 的挂钩：无记录或数据更新时自动审查。

    首次构建 → deep 全审（含 PIT 穿越自检）；数据增量更新 → 轻量审查
    （PIT/IC 复用旧结果，构造未变则历史结论仍有效）。
    """
    rec_path = AUDIT_DIR / f"{factor.name}.json"
    latest = pd.to_datetime(df["date"]).max()
    if rec_path.exists():
        try:
            old = json.loads(rec_path.read_text(encoding="utf-8"))
        except Exception:
            old = None
        if old and old.get("audit_version") == AUDIT_VERSION \
                and old.get("latest_date") == str(latest.date()) \
                and old.get("verdict") != "FAIL":
            return None                    # 数据未变、已有有效审查 → 跳过
        if old and old.get("audit_version") == AUDIT_VERSION:
            deep = False                   # 增量 → 轻量，PIT/IC 复用
        else:
            deep = True                    # 审计逻辑升级或上次 FAIL → 全审
    else:
        deep = True                        # 首次构建 → 必须全审
    return audit_factor(factor.name, factor=factor, df=df, deep=deep)


def _load_factor_values(name: str) -> pd.DataFrame | None:
    d = Path(f"data/lake/factor/{name}")
    if not d.exists():
        return None
    store = Store()
    try:
        # 值列兼容：标准因子为 value；模型分数因子（lgbm_*/gru_seq_*）为 score
        desc = store.q(
            f"DESCRIBE SELECT * FROM read_parquet('{d.as_posix()}/*.parquet')")
        vcol = ("value" if "value" in set(desc["column_name"]) else "score")
        df = store.q(
            f"SELECT date, code, {vcol} AS value "
            f"FROM read_parquet('{d.as_posix()}/*.parquet')")
    except Exception as e:
        log.warning(f"[audit] {name} 读取失败: {e}")
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def _read_rec(name: str) -> dict | None:
    """读取既有审查记录（容错，供 quick 模式复用 IC/PIT）"""
    p = AUDIT_DIR / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_rec(rec: dict):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / f"{rec['factor']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def load_all_records() -> pd.DataFrame:
    rows = []
    for p in sorted(AUDIT_DIR.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            flat = {
                "factor": r["factor"], "category": r.get("category", ""),
                "verdict": r["verdict"], "n_rows": r.get("n_rows"),
                "latest_date": r.get("latest_date"),
                "audited_at": r.get("audited_at"),
                "issues": "; ".join(r.get("issues", [])),
            }
            ck = r.get("checks", {})
            ic = ck.get("ic", {})
            flat.update({"ic5": ic.get("ic5"), "icir20": ic.get("icir20"),
                         "win5": ic.get("win5")})
            pit = ck.get("pit", {})
            flat["pit"] = pit.get("status", "SKIP")
            cov = ck.get("coverage", {})
            flat["codes_per_day_median"] = cov.get("codes_per_day_median")
            rows.append(flat)
        except Exception:
            continue
    return pd.DataFrame(rows)


def _valid_rec(name: str, deep_expected: bool) -> bool:
    """已有同版本审查记录且因子数据未变 → 可跳过"""
    p = AUDIT_DIR / f"{name}.json"
    if not p.exists():
        return False
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if old.get("audit_version") != AUDIT_VERSION:
        return False
    if old.get("verdict") == "FAIL" and not old.get("deep"):
        return False                       # FAIL 记录需 deep 复核
    fv = _load_factor_values(name)
    if fv is None or fv.empty:
        return False
    return old.get("latest_date") == str(pd.to_datetime(fv["date"]).max().date())


def audit_all(names: list[str] | None = None, force: bool = False,
              quick: bool = False, include_alpha191: bool = True) -> pd.DataFrame:
    """批量审查：registry 因子 deep（含 PIT）；alpha191 轻量（结构+IC缓存）"""
    from .registry import all_factors
    targets = list(names) if names else sorted(all_factors())
    out = []
    for name in targets:
        if not force and not quick and _valid_rec(name, deep_expected=True):
            log.info(f"[audit] {name}: 已有有效审查记录，跳过")
            continue
        try:
            r = audit_factor(name, force=force, deep=not quick)
            out.append(r)
        except Exception as e:
            log.error(f"[audit] {name} FAIL: {e}")
    if include_alpha191 and not names:
        a191 = sorted(p.name for p in Path("data/lake/factor").glob("alpha???")
                      if p.is_dir())
        skip = set(targets)
        for name in a191:
            if name in skip:
                continue
            if not force and _valid_rec(name, deep_expected=False):
                log.info(f"[audit] {name}: 已有有效审查记录，跳过")
                continue
            try:
                out.append(audit_factor(name, deep=False, force=force))
            except Exception as e:
                log.error(f"[audit] {name} FAIL: {e}")
    return pd.DataFrame([r for r in out if isinstance(r, dict)])
