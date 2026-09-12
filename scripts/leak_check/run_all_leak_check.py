r"""全量数值泄露检查 runner（numerical-leak-check skill 首个落地实现）
=====================================================================
对因子湖全部因子做「不引用任何历史测试结论」的全新泄露审计（用户指令
2026-09-12：不看过往因子测试的结果，重新评估所有因子，着重检查数据穿越）。

两类核心测试（与 skill 一致）：
  1. prefix replay（截断重放）：把全部输入源截断到检查点 t0 后重算因子，
     [起点, t0] 历史段必须与全量重算完全一致；任何差异 = 历史输出依赖未来输入。
  2. future mutation（未来扰动）：保持 t0 前输入不变，把 t0 后输入注入极值
     （数值 ×2.718282+1.414214），历史段输出必须不变；暴露缓存/全样本
     fit/rank/normalize 等截断重放不易触及的路径。

环境机制（复用 quantlab.factor.audit 的 PIT 截断规则语义）：
  - 截断规则：_PIT_LE(date<=t0) / _PIT_LT(date<t0 盘后事件) /
    _PIT_NOTICE(notice_eff<=t0) / _PIT_KEEP(事前可知，不截断不扰动)
  - 实现：VIEW 环境截断（零物化内存，逐查询下推过滤），扰动视图逐列 CASE
  - SQL 因子：full 物化临时表 + 逐检查点「测试分支 × FULL OUTER JOIN」
    聚合差分，只回传统计量（不做 pandas 百万行 merge）
  - Python 构造因子（8 个）：SQL 重写代理 Store 接管其全部 store.q /
    store.read_factor 输入；sue_i 通过补丁 quantlab.data.store.Store 接管
  - alpha191（180 个）：宽表引擎批量，共享检查点网格，宽表原生 numpy 比较
  - 湖内无注册实现目录：ML 分数（需重训级检查）与实验期一次性入湖因子
    （无统一实现）→ SKIP 降级，绝不臆测

判定（skill 四态 + 执行层扩展）：
  PASS  当前数值测试未发现未来数据影响历史输出（≠ 证明无泄露）
  WARN  浮点级微差 / as-of 静态缺口 / 重复行等非致命异常
  FAIL  结构性行集差异或超容差值差异（真穿越信号）
  ERROR 计算失败或非确定性（full vs full 复跑不一致；ERROR ≠ 无泄露）
  SKIP  无重放实现 / 数据不足（insufficient-evidence）

产物：reports/leak_check/<factor>.md（每因子一份）+ index.md + summary.csv
断点续跑：results.jsonl 按因子名去重，重跑自动跳过已完成项。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from quantlab import config  # noqa: E402
from quantlab.factor.audit import (  # noqa: E402
    _PIT_KEEP, _PIT_LE, _PIT_LT, _PIT_NOTICE, _PQ_RE, _asof_caveat, _pq_name)

sys.stdout.reconfigure(encoding="utf-8")

LEAK_VERSION = "1.1"
OUT = ROOT / "reports" / "leak_check"
RESULTS = OUT / "results.jsonl"
MUT_K, MUT_B = 2.718281828459045, 1.4142135623730951
ABS_TOL = 1e-6          # |diff| > ABS_TOL * scale 记为坏点
REL_FAIL = 1e-4         # 坏点相对差超过此值 → FAIL，否则 WARN(浮点)
PQ_TIME_COLS = ["date", "infopubldate", "notice_eff"]
PQ_TIME_FUZZY = ("publdate", "publ_date", "pub_date", "publishdate")
NUM_TYPE = re.compile(
    r"^(DOUBLE|FLOAT|REAL|DECIMAL|INT|BIGINT|SMALLINT|TINYINT|HUGEINT|"
    r"UINTEGER|UBIGINT)", re.I)

# SQL 侧坏点条件（vf/vt 为别名）
# SQL 侧坏点条件（j CTE 内别名 vf/vt；pres=测试侧行存在）
# 覆盖三类失配：①一侧有值一侧无值（LEAD/未来依赖的典型形态）
# ②NaN 失配 ③数值超容差
_BAD = (f"pres AND ( ((vf IS NULL) != (vt IS NULL)) "
        "OR (vf IS NOT NULL AND vt IS NOT NULL AND NOT isnan(vf) AND NOT isnan(vt) "
        f"AND abs(vf - vt) > {ABS_TOL} * greatest(1.0, abs(vf), abs(vt))) "
        "OR (vf IS NOT NULL AND vt IS NOT NULL AND ((isnan(vf) AND NOT isnan(vt)) "
        "OR (isnan(vt) AND NOT isnan(vf)))) )")


# ── 检查环境：in-memory 主连接 + 只读 ATTACH 项目库 ──────────────────
class Env:
    def __init__(self):
        self.con = duckdb.connect()
        dbp = str(config.DUCKDB_PATH).replace("\\", "/")
        self.con.execute(f"ATTACH '{dbp}' AS qdb (READ_ONLY)")
        self.con.execute("CREATE SCHEMA IF NOT EXISTS trunc")
        self.con.execute("CREATE SCHEMA IF NOT EXISTS mut")
        self.real = {r[0].lower() for r in self.con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE database_name = 'qdb'").fetchall()}
        self._views: set[str] = set()
        self._cols: dict[str, list[tuple[str, str]]] = {}

    # ---- 列信息 ----
    def cols(self, source: str) -> list[tuple[str, str]]:
        if source not in self._cols:
            rows = self.con.execute(
                f"DESCRIBE SELECT * FROM {source}").fetchall()
            self._cols[source] = [(r[0], r[1]) for r in rows]
        return self._cols[source]

    # ---- PIT 规则（与 audit.py 同语义）----
    def _date_col(self, t: str) -> str | None:
        names = {c.lower() for c, _ in self.cols(f"qdb.{t}")}
        return "date" if "date" in names else None

    def pit_cond(self, t: str, t0: str) -> str | None:
        if t in _PIT_KEEP:
            return None
        if t in _PIT_NOTICE:
            return f"notice_eff <= DATE '{t0}'"
        dc = self._date_col(t)
        if t in _PIT_LE or (t not in _PIT_LT and dc):
            return f"{dc} <= DATE '{t0}'"
        if dc:
            return f"{dc} < DATE '{t0}'"       # 盘后事件表
        return None

    def mut_cond(self, t: str, t0: str) -> str | None:
        if t in _PIT_KEEP:
            return None
        if t in _PIT_NOTICE:
            return f"notice_eff > DATE '{t0}'"
        dc = self._date_col(t)
        if t in _PIT_LE or (t not in _PIT_LT and dc):
            return f"{dc} > DATE '{t0}'" if dc else None
        if dc:
            return f"{dc} >= DATE '{t0}'"      # 盘后事件表：t0 当日行也属未来
        return None

    # ---- 截断/扰动视图 ----
    def trunc_view(self, t: str, t0: str) -> str:
        cond = self.pit_cond(t, t0)
        if cond is None:
            return f"qdb.{t}"
        view = f"trunc.{t}_{t0.replace('-', '')}"
        if view not in self._views:
            self.con.execute(
                f"CREATE VIEW {view} AS SELECT * FROM qdb.{t} WHERE {cond}")
            self._views.add(view)
        return view

    def mut_view(self, t: str, t0: str) -> str:
        cond = self.mut_cond(t, t0)
        if cond is None:
            return f"qdb.{t}"
        view = f"mut.{t}_{t0.replace('-', '')}"
        if view not in self._views:
            sels = []
            for c, typ in self.cols(f"qdb.{t}"):
                if NUM_TYPE.match(typ):
                    sels.append(f"CASE WHEN {cond} THEN {c} * {MUT_K} + {MUT_B} "
                                f"ELSE {c} END AS {c}")
                else:
                    sels.append(c)
            self.con.execute(
                f"CREATE VIEW {view} AS SELECT {', '.join(sels)} FROM qdb.{t}")
            self._views.add(view)
        return view

    # ---- parquet 直读数据集环境 ----
    def pq_view(self, call: str, path: str, t0: str, mode: str) -> str | None:
        name = _pq_name(path)
        cols = self.cols(call)
        low = {c.lower(): c for c, _ in cols}
        tcol = next((low[k] for k in PQ_TIME_COLS if k in low), None)
        if tcol is None:
            tcol = next((c for c, _ in cols
                         if any(f in c.lower() for f in PQ_TIME_FUZZY)), None)
        if tcol is None:
            return None
        view = f"{'trunc' if mode == 'trunc' else 'mut'}.pq_{name}_{t0.replace('-', '')}"
        if view not in self._views:
            if mode == "trunc":
                body = f"SELECT * FROM {call} WHERE {tcol} <= DATE '{t0}'"
            else:
                sels = []
                for c, typ in cols:
                    if c == tcol:
                        sels.append(c)
                    elif NUM_TYPE.match(typ):
                        sels.append(f"CASE WHEN {tcol} > DATE '{t0}' THEN "
                                    f"{c} * {MUT_K} + {MUT_B} ELSE {c} END AS {c}")
                    else:
                        sels.append(c)
                body = f"SELECT {', '.join(sels)} FROM {call} WHERE {tcol} IS NOT NULL"
            self.con.execute(f"CREATE VIEW {view} AS {body}")
            self._views.add(view)
        return view

    # ---- SQL 重写 ----
    def rewrite(self, sql: str, t0: str | None, mode: str) -> tuple[str, dict]:
        """mode: full(全量)/trunc(截断)/mut(扰动)"""
        meta: dict = {"truncated": [], "mutated": [], "pq_unsupported": []}
        cands = {x.lower() for x in
                 re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", sql, re.I)}
        used = sorted(x for x in cands if x in self.real)
        mapping: dict[str, str] = {}
        for t in used:
            if mode == "full":
                mapping[t] = f"qdb.{t}"
            elif mode == "trunc":
                mapping[t] = self.trunc_view(t, t0)
                meta["truncated"].append(t)
            else:
                mapping[t] = self.mut_view(t, t0)
                if not mapping[t].startswith("qdb."):
                    meta["mutated"].append(t)

        def _repl(m):
            name = m.group(2).lower()
            return f"{m.group(1)} {mapping[name]}" if name in mapping else m.group(0)

        sql2 = re.sub(r"(FROM|JOIN)\s+([a-zA-Z_]\w*)", _repl, sql, flags=re.I)
        for m in _PQ_RE.finditer(sql):
            call, path = m.group(0), m.group(1).replace("\\", "/")
            if mode == "full":
                continue                            # 直读保持原样（CWD=根目录）
            qual = self.pq_view(call, path, t0, mode)
            if qual is None:
                meta["pq_unsupported"].append(_pq_name(path))
                continue
            sql2 = sql2.replace(call, qual)
        meta["used_tables"] = used
        return sql2, meta


def run_sql(env: Env, sql: str, params: list | None = None) -> pd.DataFrame:
    return env.con.execute(sql, list(params or [])).fetchdf()


# ── SQL 侧差分（skill: compare_outputs 的 SQL 实现）──────────────────
def sql_diff(env: Env, full_name: str, test_from: str, t0: str,
             test: str) -> dict:
    """full_name: 可查询表/视图名（含 date,code,value）
    test_from: 可查询表名或 (子查询)（含 date,code,value）"""
    test_from = f"(SELECT date, code, value, TRUE AS pres FROM {test_from})"
    q = f"""
    WITH f AS (SELECT date, code, value FROM {full_name}
               WHERE date <= DATE '{t0}'),
         t AS (SELECT date, code, value, pres FROM {test_from}
               WHERE date <= DATE '{t0}'),
         j AS (SELECT f.date AS d, f.code AS c, f.value AS vf,
                      t.value AS vt, t.pres AS pres
               FROM f FULL OUTER JOIN t
                 ON f.date = t.date AND f.code = t.code)
    SELECT
      (SELECT COUNT(*) FROM f) AS n_hist_full,
      COUNT(*) FILTER (WHERE pres) AS n_hist_test,
      COUNT(*) FILTER (WHERE pres AND vf IS NULL AND vt IS NOT NULL) AS only_test,
      COUNT(*) FILTER (WHERE NOT COALESCE(pres, FALSE) AND vf IS NOT NULL) AS only_full,
      COUNT(*) FILTER (WHERE {_BAD}) AS n_val_bad,
      COUNT(*) FILTER (WHERE vf IS NOT NULL AND vt IS NOT NULL
                       AND ((isnan(vf) AND NOT isnan(vt))
                            OR (isnan(vt) AND NOT isnan(vf)))) AS n_nan_mm,
      COALESCE(MAX(CASE WHEN {_BAD} AND vf IS NOT NULL AND vt IS NOT NULL
                        THEN abs(vf - vt) END), 0.0) AS max_abs,
      COALESCE(MAX(CASE WHEN {_BAD} THEN
                  CASE WHEN vf IS NULL OR vt IS NULL THEN 'Infinity'::DOUBLE
                       ELSE abs(vf - vt) / greatest(1.0, abs(vf), abs(vt)) END
                  END), 0.0) AS max_rel,
      COALESCE(arg_max(coalesce(strftime(d, '%Y-%m-%d'), '') || '#'
                       || coalesce(c, ''),
                       CASE WHEN {_BAD} THEN
                       CASE WHEN vf IS NULL OR vt IS NULL THEN 'Infinity'::DOUBLE
                            ELSE abs(vf - vt) / greatest(1.0, abs(vf), abs(vt)) END
                       END), '') AS first_bad_key,
      COALESCE(arg_max(COALESCE(CAST(vf AS VARCHAR), 'NULL'),
                       CASE WHEN {_BAD} THEN
                       CASE WHEN vf IS NULL OR vt IS NULL THEN 'Infinity'::DOUBLE
                            ELSE abs(vf - vt) / greatest(1.0, abs(vf), abs(vt)) END
                       END), '') AS first_bad_vf,
      COALESCE(arg_max(COALESCE(CAST(vt AS VARCHAR), 'NULL'),
                       CASE WHEN {_BAD} THEN
                       CASE WHEN vf IS NULL OR vt IS NULL THEN 'Infinity'::DOUBLE
                            ELSE abs(vf - vt) / greatest(1.0, abs(vf), abs(vt)) END
                       END), '') AS first_bad_vt
    FROM j
    """
    r = env.con.execute(q).fetchone()
    out = {"t0": str(pd.Timestamp(t0).date()), "test": test,
           "n_hist_full": int(r[0]), "n_hist_test": int(r[1]),
           "only_test": int(r[2]), "only_full": int(r[3]),
           "n_val_bad": int(r[4]), "n_nan_mm": int(r[5]),
           "max_abs_diff": float(r[6]), "max_rel": float(r[7])}
    if r[8]:
        d, code = str(r[8]).split("#", 1)
        out["first_bad"] = {"date": d, "code": code,
                            "full": r[9], "test": r[10]}
    else:
        out["first_bad"] = None
    return out


# ── 宽表原生差分（alpha191 专用）─────────────────────────────────────
def wide_compare(fw: pd.DataFrame, ew: pd.DataFrame, t0, test: str) -> dict:
    idx = fw.index[fw.index <= pd.Timestamp(t0)]
    common = ew.index.intersection(idx)
    cols = fw.columns
    a = fw.loc[common].reindex(columns=cols).to_numpy(dtype=float)
    b = ew.loc[common].reindex(index=common, columns=cols).to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        adiff = np.abs(a - b)
        scale = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
        rel = adiff / scale
    nan_a, nan_b = np.isnan(a), np.isnan(b)
    nan_mm = nan_a ^ nan_b
    bad = (~nan_a) & (~nan_b) & (adiff > ABS_TOL * scale)
    out = {"t0": str(pd.Timestamp(t0).date()), "test": test,
           "n_hist_full": int(fw.loc[idx].notna().sum().sum()),
           "n_hist_test": int(ew.notna().sum().sum()),
           "only_full": int((len(idx) - len(common)) * len(cols)),
           "only_test": 0, "n_val_bad": int(bad.sum()),
           "n_nan_mm": int(nan_mm.sum()),
           "max_abs_diff": float(adiff[bad].max()) if bad.any() else 0.0,
           "max_rel": float(rel[bad].max()) if bad.any() else 0.0,
           "first_bad": None}
    if bad.any():
        i, jj = np.unravel_index(int(np.argmax(rel * bad)), a.shape)
        out["first_bad"] = {"date": str(fe_date(common[i])),
                            "code": str(cols[jj]),
                            "full": round(float(a[i, jj]), 8),
                            "test": round(float(b[i, jj]), 8)}
    return out


def fe_date(d):
    return pd.Timestamp(d).date()


def wide_meta(w: pd.DataFrame) -> dict:
    v = w.to_numpy(dtype=float)
    return {"n_rows": int(np.isfinite(v).sum()),
            "n_codes": int(np.isfinite(v).any(axis=0).sum()),
            "date_min": str(fe_date(w.index.min())),
            "date_max": str(fe_date(w.index.max()))}


def aggregate(tests: list[dict], asof: list[str]) -> tuple[str, list[str]]:
    issues: list[str] = []
    worst = "PASS"
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    for r in tests:
        if r["only_full"] > 0 or r["only_test"] > 0:
            issues.append(f"{r['test']}@{r['t0']}: 行集差异 "
                          f"(仅全量 {r['only_full']}, 仅截断 {r['only_test']})")
            worst = "FAIL"
        if r["n_nan_mm"] > 0 or r["n_val_bad"] > 0:
            if r["max_rel"] > REL_FAIL or r["max_rel"] == float("inf"):
                issues.append(f"{r['test']}@{r['t0']}: 值差异 {r['n_val_bad']} 处"
                              f"(NaN 失配 {r['n_nan_mm']})，max_rel={r['max_rel']:.2e}")
                worst = max(worst, "FAIL", key=lambda x: rank[x])
            else:
                issues.append(f"{r['test']}@{r['t0']}: 浮点级微差 "
                              f"{r['n_val_bad']} 处，max_rel={r['max_rel']:.2e}")
                worst = max(worst, "WARN", key=lambda x: rank[x])
    if asof:
        issues.append("as-of 静态缺口：引用快照类表股本/状态列回填历史 "
                      f"{sorted(asof)}（当前快照=未来数据）")
        worst = max(worst, "WARN", key=lambda x: rank[x])
    return worst, issues


def pick_cuts(grid, prefix_props=(0.15, 0.35, 0.55, 0.75, 0.92),
              mut_props=(0.5, 0.9), seed=42) -> tuple[list, list]:
    n = len(grid)
    rng = random.Random(seed)
    rnd = int(n * (0.10 + 0.85 * rng.random()))
    cuts = sorted({pd.Timestamp(grid[min(int(n * p), n - 1)]) for p in prefix_props}
                  | {pd.Timestamp(grid[min(rnd, n - 1)])})
    muts = sorted({pd.Timestamp(grid[min(int(n * p), n - 1)]) for p in mut_props})
    return cuts, muts


# ── 代理 Store（Python 构造因子专用）─────────────────────────────────
class ReplayStore:
    """把 Python 因子的全部 store.q / store.read_factor 输入接管到检查环境"""

    def __init__(self, env: Env, t0: str | None, mode: str):
        self.env, self.t0, self.mode = env, t0, mode

    def q(self, sql: str, params: list | None = None) -> pd.DataFrame:
        sql2, _meta = self.env.rewrite(sql, self.t0, self.mode)
        return run_sql(self.env, sql2, params)

    def read_factor(self, name: str, start=None, end=None) -> pd.DataFrame:
        d = Path(config.FACTOR_DIR) / name
        if not d.exists():
            return pd.DataFrame()
        df = pd.read_parquet(d)
        if "value" not in df.columns and "score" in df.columns:
            df = df.rename(columns={"score": "value"})
        df["date"] = pd.to_datetime(df["date"])
        if self.mode == "trunc" and self.t0:
            df = df[df["date"] <= pd.Timestamp(self.t0)]
        elif self.mode == "mut" and self.t0:
            fut = df["date"] > pd.Timestamp(self.t0)   # 因子值=收盘可知(LE 语义)
            df.loc[fut, "value"] = df.loc[fut, "value"] * MUT_K + MUT_B
        if start is not None:
            df = df[df["date"] >= pd.to_datetime(start)]
        if end is not None:
            df = df[df["date"] <= pd.to_datetime(end)]
        return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["date", "code", "value"])
    d = df[["date", "code", "value"]].copy()
    d["date"] = pd.to_datetime(d["date"])
    d["code"] = d["code"].astype(str)
    d["value"] = pd.to_numeric(d["value"], errors="coerce")
    return d


# ── 报告 ─────────────────────────────────────────────────────────────
def _fmt(x):
    if x is None:
        return "—"
    if isinstance(x, float):
        if x == float("inf"):
            return "inf"
        return f"{x:.6g}"
    return str(x)


def write_report(rec: dict):
    name = rec["factor"]
    lines = [
        f"# 数值泄露检查报告：{name}",
        "",
        f"- **判定**：`{rec['verdict']}` ｜ 检查器 leak-check/{LEAK_VERSION}"
        f" ｜ 用时 {rec.get('elapsed_s', '?')}s",
        f"- 类型：{rec['impl']} ｜ 分类：{rec.get('category', '—')} ｜ "
        f"检查点 {rec.get('n_cuts', 0)} 个（prefix {rec.get('n_prefix', 0)} + "
        f"mutation {rec.get('n_mut', 0)}）",
        f"- 数据：{rec.get('n_rows', 0):,} 行 / {rec.get('n_codes', 0):,} 只 ｜ "
        f"范围 {rec.get('date_min', '—')} ~ {rec.get('date_max', '—')}",
        f"- 说明：{rec.get('description', '')}",
        "",
    ]
    if rec["verdict"] == "SKIP":
        lines += ["## 降级原因", "",
                  f"- {rec['issues'][0] if rec['issues'] else '—'}", ""]
    else:
        lines += ["## 检查结果（只比较 t0 及以前的历史段）", "",
                  "| 测试 | t0 | 历史段行数 全量/测试 | 行集差异 仅全量/仅测试 "
                  "| 值差异 | NaN失配 | max_abs | max_rel | 最重坏点 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in rec["tests"]:
            fb = r.get("first_bad")
            lines.append(
                f"| {r['test']} | {r['t0']} | {r['n_hist_full']:,}/{r['n_hist_test']:,} "
                f"| {r['only_full']}/{r['only_test']} | {r['n_val_bad']} "
                f"| {r['n_nan_mm']} | {_fmt(r['max_abs_diff'])} "
                f"| {_fmt(r['max_rel'])} "
                f"| {fb['date'] + '@' + fb['code'] if fb else '—'} |")
        lines += ["", "## 发现与解释", ""]
        lines += [f"- {i}" for i in rec["issues"]] or ["- 无"]
        lines += ["", "## 纪律声明", "",
                  "- PASS 只表示当前数值测试未发现未来数据影响历史输出，"
                  "**不等于证明无泄露**。",
                  "- 日线级判定是乐观上界：无法区分盘中信息时点与封单强度；"
                  "分钟级前视需分钟数据专项检查。",
                  "- 本次为全新重放，未引用任何历史审计结论；截断/扰动规则与 "
                  "pit_audit 同语义（LE/LT/NOTICE/KEEP）。",
                  ""]
    (OUT / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def save(rec: dict, fh):
    write_report(rec)
    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    fh.flush()
    print(f"[leak] {rec['factor']}: {rec['verdict']} ({rec.get('elapsed_s', '?')}s)"
          f"{' | ' + '; '.join(rec['issues'][:2]) if rec['issues'] else ''}",
          flush=True)


# ── Phase A：registry SQL 因子 ───────────────────────────────────────
def phase_a(env: Env, names: list[str], fh) -> None:
    from quantlab.factor.base import SqlFactor
    from quantlab.factor.registry import all_factors
    fs = all_factors()
    for name in names:
        t_start = time.time()
        factor = fs[name]
        rec = {"factor": name, "impl": "SqlFactor", "harness": LEAK_VERSION,
               "category": factor.category, "description": factor.description,
               "verdict": "ERROR", "tests": [], "issues": []}
        try:
            sql, params = factor._sql(None, None, None)
            params = list(params or [])
            full_sql, meta = env.rewrite(sql, None, "full")
            if params:
                df = normalize(run_sql(env, full_sql, params))
                env.con.register("_full", df)
            else:
                env.con.execute(
                    "CREATE OR REPLACE TEMP TABLE _full AS " + full_sql)
            st = env.con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) "
                "FROM _full").fetchone()
            grid = [r[0] for r in env.con.execute(
                "SELECT DISTINCT date FROM _full ORDER BY date").fetchall()]
            rec.update(n_rows=int(st[0]), n_codes=int(st[1]),
                       date_min=str(st[2]), date_max=str(st[3]))
            if len(grid) < 30 or st[0] < 30:
                rec.update(verdict="SKIP",
                           issues=["截面数不足 30（insufficient-evidence）"])
                rec["elapsed_s"] = round(time.time() - t_start, 1)
                save(rec, fh)
                continue
            rec["asof_caveat"] = sorted(_asof_caveat(
                {x.lower() for x in re.findall(
                    r"(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", sql, re.I)}, sql))
            cuts, muts = pick_cuts(grid)
            tests = []
            for t0 in cuts:
                d0 = str(pd.Timestamp(t0).date())
                s2, _m = env.rewrite(sql, d0, "trunc")
                tests.append(sql_diff(env, "_full", f"({s2})", d0, "prefix"))
            for t0 in muts:
                d0 = str(pd.Timestamp(t0).date())
                s2, _m = env.rewrite(sql, d0, "mut")
                tests.append(sql_diff(env, "_full", f"({s2})", d0, "mutation"))
            rec["tests"] = tests
            rec["n_cuts"], rec["n_prefix"], rec["n_mut"] = len(tests), len(cuts), len(muts)
            verdict, issues = aggregate(tests, rec["asof_caveat"])
            if verdict == "FAIL":              # 确定性甄别：全量 vs 全量
                env.con.execute(
                    "CREATE OR REPLACE TEMP TABLE _full2 AS " + full_sql)
                sc = sql_diff(env, "_full", "_full2", rec["date_max"], "self")
                if sc["n_val_bad"] or sc["only_full"] or sc["only_test"] \
                        or sc["n_nan_mm"]:
                    verdict = "ERROR"
                    issues.append("非确定性：全量两次重放自身不一致（排序/并行聚合"
                                  "不稳定），泄露判定不可达，须修实现后复检")
            rec.update(verdict=verdict, issues=issues)
        except Exception as e:
            rec["issues"] = [f"执行失败: {type(e).__name__}: {str(e)[:200]}"]
        rec["elapsed_s"] = round(time.time() - t_start, 1)
        save(rec, fh)


# ── Phase B：alpha191 宽表批量 ───────────────────────────────────────
A191_PREFIX = (0.25, 0.55, 0.85)
A191_MUT = (0.7,)


def phase_b(env: Env, names: list[str], fh) -> None:
    from quantlab.factor.alpha191 import build_wide
    from quantlab.factor.alpha191_formulas import ALPHAS
    t_start = time.time()
    wide_full = build_wide(ReplayStore(env, None, "full"))
    grid = wide_full["close"].index
    cuts, muts = pick_cuts(grid, A191_PREFIX, A191_MUT)
    envs: list[tuple[str, str, dict]] = []
    for t0 in cuts:
        w = build_wide(ReplayStore(env, str(pd.Timestamp(t0).date()), "trunc"))
        envs.append((pd.Timestamp(t0), "prefix", w))
    for t0 in muts:
        w = build_wide(ReplayStore(env, str(pd.Timestamp(t0).date()), "mut"))
        envs.append((pd.Timestamp(t0), "mutation", w))
    print(f"[leak] a191 宽表环境就绪：full + {len(envs)} 检查点 "
          f"({time.time() - t_start:.0f}s)", flush=True)
    for name in names:
        t0s = time.time()
        rec = {"factor": name, "impl": "alpha191公式引擎", "harness": LEAK_VERSION,
               "category": "alpha191", "verdict": "ERROR", "tests": [], "issues": []}
        try:
            fn, conf = ALPHAS[name]
            fw = fn(wide_full)
            rec.update(wide_meta(fw), description=f"Alpha191（{conf}）")
            if rec["n_rows"] < 30 * 100:
                rec.update(verdict="SKIP",
                           issues=["有效截面不足（insufficient-evidence）"])
            else:
                tests = [wide_compare(fw, fn(w), t0, kind) for t0, kind, w in envs]
                rec["tests"] = tests
                rec["n_cuts"], rec["n_prefix"], rec["n_mut"] = len(tests), len(cuts), len(muts)
                verdict, issues = aggregate(tests, [])
                rec.update(verdict=verdict, issues=issues)
        except Exception as e:
            rec["issues"] = [f"执行失败: {type(e).__name__}: {str(e)[:200]}"]
        rec["elapsed_s"] = round(time.time() - t0s, 1)
        save(rec, fh)


# ── Phase C：Python 构造因子（代理 Store）────────────────────────────
PY_CUTS = {"default": ((0.15, 0.35, 0.55, 0.75, 0.92), (0.5, 0.9)),
           "heavy": ((0.5, 0.85), (0.85,))}      # 复合因子减检查点（compute 分钟级）
HEAVY = {"hf_wf_composite", "a191_wf_composite"}


def phase_c(env: Env, names: list[str], fh) -> None:
    import quantlab.data.store as store_mod
    from quantlab.factor.registry import all_factors
    fs = all_factors()
    orig_store = store_mod.Store
    holder = {"store": None}

    class _Factory:
        def __call__(self, *a, **k):
            return holder["store"]

    store_mod.Store = _Factory()          # sue_i 内部 Store() 补丁
    try:
        for name in names:
            t_start = time.time()
            factor = fs[name]
            rec = {"factor": name, "impl": "Python构造(代理Store)",
                   "harness": LEAK_VERSION, "category": factor.category,
                   "description": factor.description, "verdict": "ERROR",
                   "tests": [], "issues": []}
            try:
                pp, mp = PY_CUTS["heavy" if name in HEAVY else "default"]
                holder["store"] = ReplayStore(env, None, "full")
                full = normalize(factor.compute(holder["store"]))
                env.con.register("_fc", full)
                grid = sorted(full["date"].unique()) if len(full) else []
                rec.update(n_rows=int(len(full)),
                           n_codes=int(full["code"].nunique()) if len(full) else 0,
                           date_min=str(full["date"].min().date()) if len(full) else None,
                           date_max=str(full["date"].max().date()) if len(full) else None)
                if len(grid) < 30:
                    rec.update(verdict="SKIP",
                               issues=["截面数不足 30（insufficient-evidence）"])
                else:
                    cuts, muts = pick_cuts(grid, pp, mp)
                    tests = []
                    for kind, t_list, mode in (("prefix", cuts, "trunc"),
                                               ("mutation", muts, "mut")):
                        for t0 in t_list:
                            d0 = str(pd.Timestamp(t0).date())
                            holder["store"] = ReplayStore(env, d0, mode)
                            te = normalize(factor.compute(holder["store"]))
                            env.con.register("_tc", te)
                            tests.append(sql_diff(env, "_fc",
                                                  "_tc", d0, kind))
                    rec["tests"] = tests
                    rec["n_cuts"], rec["n_prefix"], rec["n_mut"] = \
                        len(tests), len(cuts), len(muts)
                    verdict, issues = aggregate(tests, [])
                    rec.update(verdict=verdict, issues=issues)
            except Exception as e:
                rec["issues"] = [f"执行失败: {type(e).__name__}: {str(e)[:200]}"]
            rec["elapsed_s"] = round(time.time() - t_start, 1)
            save(rec, fh)
    finally:
        store_mod.Store = orig_store


# ── Phase D：湖内无注册实现目录 → SKIP 降级 ──────────────────────────
def phase_d(names: list[str], fh) -> None:
    ml = {"lgbm_all_score", "lgbm_domain_score", "lgbm_rep168_score",
          "lgbm_top64_score", "gru_seq_score"}
    for name in names:
        rec = {"factor": name, "impl": "lake-only(无注册实现)",
               "harness": LEAK_VERSION, "category": "experimental/ml",
               "verdict": "SKIP", "tests": [], "issues": [], "elapsed_s": 0.0,
               "n_cuts": 0}
        if name in ml:
            rec["issues"] = ["ML 模型分数因子：数值重放不适用，泄露检查需重训级"
                             "验证（截断数据重训模型对比），本次未覆盖"]
        else:
            rec["issues"] = ["实验期一次性入湖因子（phase 系列脚本产物，无注册"
                             "实现）：无统一重放入口，逐脚本重放超出本次范围；"
                             "建议纳入 registry 后复检"]
        save(rec, fh)


# ── 汇总索引 ─────────────────────────────────────────────────────────
def build_index():
    recs = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    seen: dict[str, dict] = {}
    for r in recs:
        seen[r["factor"]] = r
    recs = sorted(seen.values(), key=lambda r: r["factor"])
    with (OUT / "summary.csv").open("w", encoding="utf-8-sig") as f:
        f.write("factor,impl,category,verdict,n_cuts,only_full,only_test,"
                "n_val_bad,n_nan_mm,max_rel,first_bad,elapsed_s,issues\n")
        for r in recs:
            worst = (max(r["tests"], key=lambda x: x["n_val_bad"] + x["only_full"]
                         + x["n_nan_mm"]) if r.get("tests") else {})
            fb = worst.get("first_bad") or {}
            f.write(f"{r['factor']},{r['impl']},{r.get('category','')},"
                    f"{r['verdict']},{r.get('n_cuts', 0)},"
                    f"{worst.get('only_full', '')},{worst.get('only_test', '')},"
                    f"{worst.get('n_val_bad', '')},{worst.get('n_nan_mm', '')},"
                    f"{worst.get('max_rel', '')},"
                    f"{fb.get('date', '')}@{fb.get('code', '')},"
                    f"{r.get('elapsed_s', '')},"
                    f"\"{' ; '.join(r['issues'])[:200]}\"\n")
    from collections import Counter
    cnt = Counter(r["verdict"] for r in recs)
    n_impl = Counter(r["impl"] for r in recs)
    L = ["# 全量数值泄露检查汇总（numerical-leak-check skill 首次全量执行）", "",
         "- 执行口径：**全新重放，不引用任何历史测试结论**（用户指令 2026-09-12）"
         f"；检查器 leak-check/{LEAK_VERSION}",
         "- 方法：prefix replay（源截断重放，历史段必须与全量一致）+ "
         "future mutation（未来段极值扰动 ×2.718+1.414，历史段必须不变）"
         "+ FAIL 时全量复跑确定性甄别",
         f"- 覆盖：{len(recs)} 个因子（SQL {n_impl.get('SqlFactor', 0)} + "
         f"Python {n_impl.get('Python构造(代理Store)', 0)} + "
         f"alpha191 {n_impl.get('alpha191公式引擎', 0)} + "
         f"无实现降级 {n_impl.get('lake-only(无注册实现)', 0)}）",
         f"- 判定分布：**{'**，**'.join(f'{k} {v}' for k, v in sorted(cnt.items()))}**",
         "",
         "> PASS ≠ 证明无泄露；日线级判定是乐观上界；截断/扰动规则与 pit_audit "
         "同语义（LE/LT/NOTICE/KEEP）。", "",
         "## 判定明细", "",
         "| 因子 | 实现 | 判定 | 检查点 | 最重证据 | 说明 |",
         "|---|---|---|---|---|---|"]
    for r in recs:
        worst = (max(r["tests"], key=lambda x: x["n_val_bad"] + x["only_full"]
                     + x["n_nan_mm"]) if r.get("tests") else {})
        ev = "—"
        if worst:
            fb = worst.get("first_bad")
            ev = (f"{worst['test']}@{worst['t0']} 仅全量={worst['only_full']} "
                  f"坏点={worst['n_val_bad']} rel={_fmt(worst['max_rel'])}"
                  + (f" @{fb['date']}#{fb['code']}" if fb else ""))
        L.append(f"| [{r['factor']}]({r['factor']}.md) | {r['impl']} "
                 f"| **{r['verdict']}** | {r.get('n_cuts', 0)} | {ev} "
                 f"| {'; '.join(r['issues'])[:120] or '—'} |")
    (OUT / "index.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"[leak] 汇总完成：{len(recs)} 因子，分布 {dict(cnt)}", flush=True)


# ── 主入口 ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all",
                    choices=["all", "A", "B", "C", "D", "index"])
    ap.add_argument("--names", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="忽略断点续跑")
    args = ap.parse_args()

    if args.phase == "index":
        build_index()
        return
    OUT.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if RESULTS.exists() and not args.force:
        for l in RESULTS.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    done.add(json.loads(l)["factor"])
                except Exception:
                    pass
    fh = RESULTS.open("a", encoding="utf-8")
    env = Env()
    from quantlab.factor.base import SqlFactor
    from quantlab.factor.registry import all_factors
    fs = all_factors()
    sql_names = sorted(n for n, f in fs.items() if isinstance(f, SqlFactor))
    py_names = sorted(set(fs) - set(sql_names))
    lake = {p.name for p in (ROOT / "data" / "lake" / "factor").iterdir()
            if p.is_dir()}
    reg_lower = {n.lower() for n in fs}
    extra = sorted(d for d in lake if d.lower() not in reg_lower
                   and not re.fullmatch(r"alpha\d{3}", d))
    a191 = sorted(d for d in lake if re.fullmatch(r"alpha\d{3}", d))

    if args.phase in ("all", "A"):
        todo = [n for n in (args.names or sql_names)
                if args.force or n not in done]
        todo = todo[: args.limit] if args.limit else todo
        print(f"[leak] Phase A：registry SQL 因子 {len(todo)} 个", flush=True)
        phase_a(env, todo, fh)
    if args.phase in ("all", "B"):
        todo = [n for n in (args.names or a191) if args.force or n not in done]
        todo = todo[: args.limit] if args.limit else todo
        print(f"[leak] Phase B：alpha191 {len(todo)} 个", flush=True)
        phase_b(env, todo, fh)
    if args.phase in ("all", "C"):
        todo = [n for n in (args.names or py_names)
                if args.force or n not in done]
        todo = todo[: args.limit] if args.limit else todo
        print(f"[leak] Phase C：Python 构造因子 {len(todo)} 个", flush=True)
        phase_c(env, todo, fh)
    if args.phase in ("all", "D"):
        todo = [n for n in (args.names or extra) if args.force or n not in done]
        todo = todo[: args.limit] if args.limit else todo
        print(f"[leak] Phase D：无实现降级 {len(todo)} 个", flush=True)
        phase_d(todo, fh)
    fh.close()
    build_index()


if __name__ == "__main__":
    main()
