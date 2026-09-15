"""因子评估指标日频存储（L0/L1/L2 三层）
=====================================
实现方案：research/factor_metric_store_proposal_20260912.md

L0 原子时序  data/lake/factor_metric_daily/{pool}/part-{year}.parquet
             主键 (date, factor)；date=信号日，h 日 IC 列成熟后回填（NULL=未成熟）
L1 滚动缓存  data/lake/factor_metric_rolling/{pool}/part-{year}.parquet
             (date, factor) 键，rank_icir/win/t × h∈{5,20} × w∈{20,60,120}
L2 相关层    data/lake/factor_corr_daily/part-{year}.parquet   (date, factor, core_factor, rho)
             data/lake/factor_corr_weekly/part-{year}.parquet  (week, factor_a, factor_b, rho)

口径（与 run_eval.py / quality.factor_ic_extended 同源）：
  - 价格 close*adj_factor；fwd{h} = LEAD(c,h)/c-1（下 h 个有行情交易日）
  - rank IC = corr(RANK(v), RANK(fwd))；pearson IC = corr(v, fwd)
  - 分组 NTILE(5)（h=1 日频口径 + h=20 调仓口径，均在 fwd 非空截面）
  - 池成员 PIT：period <= 信号日取最新一期（index_members_hist）
  - 写入不删、同名覆盖=幂等（safe-delete 约束）
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from ..config import get_logger

log = get_logger(__name__)

LAKE = Path("data/lake")
FACTOR_DIR = LAKE / "factor"
POOL_DIR = LAKE / "factor_metric_daily"
ROLL_DIR = LAKE / "factor_metric_rolling"
CORR_DIR = LAKE / "factor_corr_daily"
CORR_W_DIR = LAKE / "factor_corr_weekly"
DB = "data/quant.duckdb"

HORIZONS = (1, 5, 20, 60, 120)
ROLL_HORIZONS = (5, 20)
ROLL_WINDOWS = (20, 60, 120)
STYLE_REPS = ("size", "bp", "momentum_20", "volatility_20",
              "turnover", "reversal_5")
CORE_FACTORS = ("size", "amihud_20", "sue_i", "hf_amihud_20")
# L2 每日核心对 = 6 风格代表 + 生产/候选成分，去重后 9 个
CORE_PAIR_TARGETS = tuple(dict.fromkeys(STYLE_REPS + CORE_FACTORS))
IC_COLS = tuple(f"{t}_ic{h}" for h in HORIZONS for t in ("rank", "pearson"))
L0_COLUMNS = (
    ["date", "factor", "universe", "n_codes", "coverage", "degraded",
     "f_std", "f_q01", "f_q50", "f_q99",
     "rank_ac1", "rank_ac5", "rank_ac20",
     "q1_turnover", "q5_turnover"]
    + list(IC_COLS)
    + [f"q{i}_ret" for i in range(1, 6)] + ["ls_ret"]
    + [f"q{i}_ret20" for i in range(1, 6)] + ["ls_ret20"]
    + [f"exp_{s}" for s in STYLE_REPS])

DEFAULT_POOLS = {
    "ashare_ex": {
        "name": "ashare_ex",
        "membership_source": "factor_lake_default",
        "pit_required": True,
        "min_codes": 30,
        "note": "全市场默认池=因子湖写入口径(ashare_ex)，无额外成员过滤",
        "active": True,
    },
    "zz1000": {
        "name": "zz1000",
        "membership_source": "index_members_hist:zz1000",
        "pit_required": True,
        "min_codes": 200,
        "note": "中证1000成分（PROD基准），月度period快照，"
                "join条件 period<=信号日取最新一期",
        "active": True,
    },
    "dividend": {
        "name": "dividend",
        "membership_source": None,
        "pit_required": True,
        "min_codes": 30,
        "note": "红利池：暂无PIT成员历史来源（universe_daily无红利历史、"
                "index_members_hist无红利指数），按红线禁止回填；"
                "补齐PIT成员前本库不产数",
        "active": False,
    },
}


# ─────────────────────────── 池注册表 ───────────────────────────

def pool_registry(refresh: bool = False) -> dict:
    """读取（必要时初始化）池注册表 _pools.json"""
    path = POOL_DIR / "_pools.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_POOLS, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return DEFAULT_POOLS


def active_pools() -> list[str]:
    return [k for k, v in pool_registry().items() if v.get("active")]


def _member_source(reg: dict) -> tuple[str, str] | None:
    """membership_source -> (表名, index_code)；None=无 PIT 来源（禁入）"""
    src = reg.get("membership_source")
    if not src:
        return None
    if src == "factor_lake_default":
        return ("", "")
    if src.startswith("index_members_hist:"):
        return ("maindb.index_members_hist", src.split(":", 1)[1])
    raise ValueError(f"未知成员来源: {src}")


def _pool_ctes(pool: str) -> tuple[str, str, str]:
    """返回 (额外CTE, f定义, 是否带poolsize)。ashare_ex 直接透传。"""
    if pool == "ashare_ex":
        return ("", "SELECT * FROM f_raw", "0")
    reg = pool_registry().get(pool)
    if reg is None:
        raise KeyError(f"池 {pool} 未注册")
    src = _member_source(reg)
    if src is None:
        raise ValueError(
            f"池 {pool} 无 PIT 成员历史（红线），禁止回填——见 _pools.json note")
    _, idx = src
    ctes = f""",
    dmap AS (
        SELECT fr.date AS date, MAX(m.period) AS period
        FROM (SELECT DISTINCT date FROM f_raw) fr
        JOIN maindb.index_members_hist m
          ON m.index_code = '{idx}' AND m.period <= fr.date
        GROUP BY fr.date
    ),
    mem AS (
        SELECT dmap.date AS date, m.code AS code
        FROM dmap JOIN maindb.index_members_hist m
          ON m.index_code = '{idx}' AND m.period = dmap.period
    ),
    poolsize AS (
        SELECT date, COUNT(*) AS pool_n FROM mem GROUP BY date
    )"""
    f_def = ("SELECT fr.* FROM f_raw fr "
             "JOIN mem ON fr.date = mem.date AND fr.code = mem.code")
    return (ctes, f_def, "1")


# ─────────────────────────── 通用小工具 ───────────────────────────

def _factor_paths(name: str) -> list[str]:
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"因子 {name} 无 parquet（{FACTOR_DIR / name}）")
    return [p.as_posix() for p in parts]


def _vcol(con: duckdb.DuckDBPyConnection, path: str) -> str:
    names = set(con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{path}')"
    ).df()["column_name"])
    return "value" if "value" in names else "score"


def _attach(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET enable_progress_bar=false")
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")


# ─────────────────────────── L0 计算 ───────────────────────────

def compute_factor_l0(
    name: str, pool: str = "ashare_ex", start: str | None = None,
    end: str | None = None, min_n: int = 30,
    con: duckdb.DuckDBPyConnection | None = None,
    px_src: str | None = None,
) -> pd.DataFrame:
    """单因子全历史 L0 行（一次大 SQL：IC 双类型×5h + 分组 + 分布 +
    rank自相关 + 换手；暴露/核心相关另走 compute_factor_core_corr）。

    date=信号日；尾部行未成熟 horizon 列为 NULL（LEAD 语义天然实现）。
    px_src：worker 缓存的价格临时表名（含 date/code/c 列），跳过
    kline_daily 扫描；缺省现场查主库。
    """
    plist = _factor_paths(name)
    own = con is None
    if own:
        con = duckdb.connect()
        _attach(con)
    try:
        vcol = _vcol(con, plist[0])
        rng = con.execute(
            f"SELECT min(date), max(date) FROM read_parquet({plist})"
        ).fetchone()
        d0, d1 = start or str(rng[0]), end or str(rng[1])
        pool_ctes, f_def, has_pool = _pool_ctes(pool)

        leads = ",\n            ".join(
            f"LEAD(px.c, {h}) OVER (PARTITION BY f.code ORDER BY f.date)"
            f" / px.c - 1 AS fw{h}" for h in HORIZONS)
        a_ctes = ",\n        ".join(
            f"a{h} AS (SELECT date, COUNT(*) AS n{h}, "
            f"corr(v, fw) AS pic{h}, corr(rv, rf) AS ric{h} "
            f"FROM r{h} GROUP BY date HAVING COUNT(*) >= {min_n})"
            for h in HORIZONS)

        def grp(h: int) -> str:
            q = ", ".join(
                f"AVG(fwd) FILTER (WHERE q = {i}) AS q{i}_ret{'' if h == 1 else h}"
                for i in range(1, 6))
            return (f"g{h} AS (SELECT date, NTILE(5) OVER "
                    f"(PARTITION BY date ORDER BY v) AS q, fw AS fwd FROM r{h}),\n"
                    f"        grp{h} AS (SELECT date, {q} FROM g{h} GROUP BY date)")

        join_cols = (
            [f"ANY_VALUE(d.n_codes) AS n_codes",
             "ANY_VALUE(d.f_std) AS f_std", "ANY_VALUE(d.f_q01) AS f_q01",
             "ANY_VALUE(d.f_q50) AS f_q50", "ANY_VALUE(d.f_q99) AS f_q99"]
            + [f"ANY_VALUE(pic{h}) AS pic{h}, ANY_VALUE(ric{h}) AS ric{h}, "
               f"ANY_VALUE(n{h}) AS n{h}" for h in HORIZONS]
            + ([f"ANY_VALUE(q{i}_ret) AS q{i}_ret" for i in range(1, 6)]
               if has_pool == "0" else
               [f"ANY_VALUE(q{i}_ret) AS q{i}_ret" for i in range(1, 6)])
            + [f"ANY_VALUE(q{i}_ret20) AS q{i}_ret20" for i in range(1, 6)]
            + ["ANY_VALUE(rank_ac1) AS rank_ac1",
               "ANY_VALUE(rank_ac5) AS rank_ac5",
               "ANY_VALUE(rank_ac20) AS rank_ac20",
               "ANY_VALUE(q1_turnover) AS q1_turnover",
               "ANY_VALUE(q5_turnover) AS q5_turnover"]
            + (["ANY_VALUE(pool_n) AS pool_n"] if has_pool == "1" else []))
        joins = ("FROM dist d"
                 + "".join(f" FULL JOIN a{h} USING (date)" for h in HORIZONS)
                 + " FULL JOIN grp1 USING (date) FULL JOIN grp20 USING (date)"
                 " FULL JOIN rac USING (date)"
                 " FULL JOIN tov1 USING (date) FULL JOIN tov5 USING (date)"
                 + (" FULL JOIN poolsize USING (date)" if has_pool == "1" else ""))
        px_def = (f"SELECT date, code, c FROM {px_src} "
                  f"WHERE date >= (SELECT min(date) FROM f)"
                  if px_src else
                  "SELECT date, code, close * adj_factor AS c "
                  "FROM maindb.kline_daily "
                  "WHERE date >= (SELECT min(date) FROM f)"
                  "  AND date <= (SELECT max(date) FROM f) + INTERVAL 200 DAY")
        # 物化 j：DuckDB CTE 默认 inline，j 被 5 个 r{h} 各展开一次
        # （join+LEAD 窗口 ×5 = 全历史因子 158s 的主因），先落临时表。
        _tag = f"j_{abs(hash((name, pool, d0, d1))) % 10**8:x}"
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE {_tag} AS
            WITH f_raw AS (
                SELECT CAST(date AS DATE) AS date, code, {vcol} AS v
                FROM read_parquet({plist})
                WHERE date BETWEEN DATE '{d0}' AND DATE '{d1}'
            ){pool_ctes},
            f AS ({f_def}),
            px AS ({px_def})
            SELECT f.date AS date, f.code AS code, f.v AS v,
            {leads}
            FROM f JOIN px ON f.date = px.date AND f.code = px.code
        """)
        # 共享 rank：rv 只算一次（原 r{h} 各自重复 RANK(v)）
        _rk = f"rk_{_tag}"
        _fw_cols = ", ".join(f"fw{h}" for h in HORIZONS)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE {_rk} AS
            SELECT date, code, v,
                   RANK() OVER (PARTITION BY date ORDER BY v) AS rv,
                   {_fw_cols}
            FROM {_tag} WHERE v IS NOT NULL
        """)
        # 秩约定（2026-09-13 统一为干净口径，与 quality.factor_ic_extended
        # 一致）：v 与 fw 的秩都在"v 非空且 fw 非空"的可评估集内计算
        # （此前 rv 在全覆盖集预排序，NULL-fw 停牌行非均匀移位真实秩，
        # 基本面因子如 sue_i 差 0.148；run_eval 旧版则反向漏过滤 v）
        r_ctes = ",\n        ".join(
            f"r{h} AS (SELECT date, v, fw{h} AS fw, "
            f"RANK() OVER (PARTITION BY date ORDER BY v) AS rv, "
            f"RANK() OVER (PARTITION BY date ORDER BY fw{h}) AS rf "
            f"FROM {_rk} WHERE fw{h} IS NOT NULL)"
            for h in HORIZONS)
        # 池 CTE 不跨语句存活：物化查询里的 poolsize 在最终 SQL 里
        # 用 dts 重建（2026-09-13 zz1000 回填 CatalogException 修复）
        pool_ctes_final = ""
        if has_pool == "1":
            _, idx = _member_source(pool_registry()[pool])
            pool_ctes_final = f""",
        pool_dmap AS (
            SELECT dts.date AS date, MAX(m.period) AS period
            FROM dts JOIN maindb.index_members_hist m
              ON m.index_code = '{idx}' AND m.period <= dts.date
            GROUP BY dts.date
        ),
        poolsize AS (
            SELECT pd.date AS date, COUNT(*) AS pool_n
            FROM pool_dmap pd JOIN maindb.index_members_hist m
              ON m.index_code = '{idx}' AND m.period = pd.period
            GROUP BY pd.date
        )"""
        dts_from = _rk
        sql = f"""
        WITH dist AS (
            SELECT date, COUNT(*) AS n_codes, stddev(v) AS f_std,
                   quantile_cont(v, 0.01) AS f_q01,
                   quantile_cont(v, 0.50) AS f_q50,
                   quantile_cont(v, 0.99) AS f_q99
            FROM {_rk} GROUP BY date
        ),
        {r_ctes},
        {a_ctes},
        {grp(1)},
        {grp(20)},
        rl AS (
            SELECT date, rv,
                   LAG(rv, 1) OVER (PARTITION BY code ORDER BY date) AS rv1,
                   LAG(rv, 5) OVER (PARTITION BY code ORDER BY date) AS rv5,
                   LAG(rv, 20) OVER (PARTITION BY code ORDER BY date) AS rv20
            FROM {_rk}
        ),
        rac AS (
            SELECT date, corr(rv, rv1) AS rank_ac1,
                   corr(rv, rv5) AS rank_ac5,
                   corr(rv, rv20) AS rank_ac20
            FROM rl GROUP BY date HAVING COUNT(rv1) >= {min_n}
        ),
        qs AS (
            SELECT date, code, NTILE(5) OVER (PARTITION BY date ORDER BY v) AS q
            FROM {_rk}
        ),
        q1m AS (SELECT date, code FROM qs WHERE q = 1),
        q5m AS (SELECT date, code FROM qs WHERE q = 5),
        dts AS (SELECT DISTINCT date FROM {dts_from}){pool_ctes_final},
        dmap2 AS (SELECT date, LAG(date) OVER (ORDER BY date) AS pdate FROM dts),
        tov1 AS (
            SELECT a.date AS date,
                   1.0 * (COUNT(*) - COUNT(b.code)) / COUNT(*) AS q1_turnover
            FROM q1m a JOIN dmap2 ON a.date = dmap2.date
            LEFT JOIN q1m b ON b.date = dmap2.pdate AND b.code = a.code
            GROUP BY a.date
        ),
        tov5 AS (
            SELECT a.date AS date,
                   1.0 * (COUNT(*) - COUNT(b.code)) / COUNT(*) AS q5_turnover
            FROM q5m a JOIN dmap2 ON a.date = dmap2.date
            LEFT JOIN q5m b ON b.date = dmap2.pdate AND b.code = a.code
            GROUP BY a.date
        ),
        final AS (
            SELECT d.date,
                   {", ".join(join_cols)}
            {joins}
            GROUP BY d.date
        )
        SELECT * FROM final ORDER BY date
        """
        try:
            df = con.execute(sql).df()
        finally:
            con.execute(f"DROP TABLE IF EXISTS {_tag}")
            con.execute(f"DROP TABLE IF EXISTS {_rk}")
        # 全市场池的覆盖分母：当日 universe_daily(ashare_ex) 行数
        if has_pool == "0":
            uni = con.execute(
                "SELECT date, COUNT(*) AS u_n FROM maindb.universe_daily "
                "WHERE universe_name = 'ashare_ex' GROUP BY date"
            ).df()
            uni["date"] = pd.to_datetime(uni["date"])
            df["date"] = pd.to_datetime(df["date"])
            df = df.merge(uni, on="date", how="left")
            df["coverage"] = df["n_codes"] / df["u_n"]
            df["degraded"] = (df["n_codes"] < min_n) | df["u_n"].isna()
            df = df.drop(columns=["u_n"])
    finally:
        if own:
            con.close()

    if df.empty:
        return df
    # 派生列 + 命名规整
    for h in (1, 20):
        sfx = "" if h == 1 else str(h)
        df[f"ls_ret{sfx}"] = df[f"q5_ret{sfx}"] - df[f"q1_ret{sfx}"]
    df = df.rename(columns={**{f"pic{h}": f"pearson_ic{h}" for h in HORIZONS},
                            **{f"ric{h}": f"rank_ic{h}" for h in HORIZONS}})
    if "pool_n" in df.columns:  # 池内覆盖（分母=当日池成员数）
        df["coverage"] = df["n_codes"] / df["pool_n"]
        df["degraded"] = df["n_codes"] < pool_registry()[
            pool].get("min_codes", 30)
        df = df.drop(columns=["pool_n"])
    df["factor"] = name
    df["universe"] = pool
    keep = [c for c in L0_COLUMNS if c in df.columns]
    df = df[keep].copy()
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ─────────────────── 暴露 + L2 核心对（池感知共用查询） ───────────────────

def compute_factor_core_corr(
    name: str, pool: str = "ashare_ex", start: str | None = None,
    end: str | None = None, min_n: int = 30,
    cores: tuple[str, ...] = CORE_PAIR_TARGETS,
    con: duckdb.DuckDBPyConnection | None = None,
    core_src: str | None = None,
) -> pd.DataFrame:
    """因子 vs 核心因子集的逐日截面 spearman（秩-秩 corr）。

    返回长表 (date, core, rho)。L0 的 exp_* 列与 L2 因子-核心相关
    共用本查询（口径与 build_dashboard.style_exposure 一致）。
    池维度下核心因子同过池成员过滤（池内截面）。
    core_src：worker 缓存的核心 rank 临时表（date, code, core, rs），
    跳过九因子 union+rank（回填的主要成本）；仅全市场池可用。
    """
    plist = _factor_paths(name)
    own = con is None
    if own:
        con = duckdb.connect()
        _attach(con)
    try:
        vcol = _vcol(con, plist[0])
        rng = con.execute(
            f"SELECT min(date), max(date) FROM read_parquet({plist})"
        ).fetchone()
        d0, d1 = start or str(rng[0]), end or str(rng[1])
        cores_avail = [c for c in cores if c != name]
        if not cores_avail:
            return pd.DataFrame(columns=["date", "core", "rho"])
        if pool == "ashare_ex" and core_src:
            # 快路径：核心因子值已在 worker 缓存表；rank 在交集截面
            # 上重算（口径 = build_dashboard.style_exposure）
            in_list = ", ".join(f"'{c}'" for c in cores_avail)
            sql = f"""
            WITH f_raw AS (
                SELECT CAST(date AS DATE) AS date, code, {vcol} AS v
                FROM read_parquet({plist})
                WHERE date BETWEEN DATE '{d0}' AND DATE '{d1}'
            ),
            f AS (SELECT * FROM f_raw),
            u AS (
                SELECT date, code, core, cv
                FROM {core_src}
                WHERE core IN ({in_list})
                  AND date BETWEEN DATE '{d0}' AND DATE '{d1}'
            ),
            j AS (
                SELECT u.date AS date, u.core AS core,
                       RANK() OVER (PARTITION BY u.date, u.core
                                    ORDER BY f.v) AS ra,
                       RANK() OVER (PARTITION BY u.date, u.core
                                    ORDER BY u.cv) AS rs
                FROM u JOIN f ON u.date = f.date AND u.code = f.code
            )
            SELECT date, core, corr(ra, rs) AS rho, COUNT(*) AS n
            FROM j GROUP BY date, core HAVING COUNT(*) >= {min_n}
            ORDER BY date
            """
            return con.execute(sql).df()
        if pool == "ashare_ex":
            pre = ""
            f_def = "SELECT * FROM f_raw"
            unions = "\n            UNION ALL ".join(
                f"(SELECT CAST(date AS DATE) AS date, code, '{c}' AS core, "
                f"value AS cv FROM read_parquet({_factor_paths(c)}) "
                f"WHERE date BETWEEN DATE '{d0}' AND DATE '{d1}')"
                for c in cores_avail)
        else:
            reg = pool_registry().get(pool)
            src = _member_source(reg) if reg else None
            if src is None:
                raise ValueError(f"池 {pool} 无 PIT 成员历史（红线）")
            _, idx = src
            pre = f""",
    dmap AS (
        SELECT fr.date AS date, MAX(m.period) AS period
        FROM (SELECT DISTINCT date FROM f_raw) fr
        JOIN maindb.index_members_hist m
          ON m.index_code = '{idx}' AND m.period <= fr.date
        GROUP BY fr.date
    ),
    mem_all AS (
        SELECT dmap.date AS date, m.code AS code
        FROM dmap JOIN maindb.index_members_hist m
          ON m.index_code = '{idx}' AND m.period = dmap.period
    )"""
            f_def = ("SELECT fr.* FROM f_raw fr "
                     "JOIN mem_all ON fr.date = mem_all.date "
                     "AND fr.code = mem_all.code")
            unions = "\n            UNION ALL ".join(
                f"(SELECT CAST(x.date AS DATE) AS date, x.code, '{c}' AS core, "
                f"x.value AS cv FROM read_parquet({_factor_paths(c)}) x "
                f"JOIN mem_all ON CAST(x.date AS DATE) = mem_all.date "
                f"AND x.code = mem_all.code "
                f"WHERE x.date BETWEEN DATE '{d0}' AND DATE '{d1}')"
                for c in cores_avail)
        sql = f"""
        WITH f_raw AS (
            SELECT CAST(date AS DATE) AS date, code, {vcol} AS v
            FROM read_parquet({plist})
            WHERE date BETWEEN DATE '{d0}' AND DATE '{d1}'
        ){pre},
        f AS ({f_def}),
        u AS ({unions}),
        j AS (
            SELECT u.date AS date, u.core AS core,
                   RANK() OVER (PARTITION BY u.date, u.core ORDER BY f.v) AS ra,
                   RANK() OVER (PARTITION BY u.date, u.core ORDER BY u.cv) AS rs
            FROM u JOIN f ON u.date = f.date AND u.code = f.code
        )
        SELECT date, core, corr(ra, rs) AS rho, COUNT(*) AS n
        FROM j GROUP BY date, core HAVING COUNT(*) >= {min_n}
        ORDER BY date
        """
        df = con.execute(sql).df()
    finally:
        if own:
            con.close()
    return df


def exposure_wide(corr_long: pd.DataFrame) -> dict[str, pd.Series]:
    """核心相关长表 -> L0 的 exp_{style} 列（index=date 的 Series 字典）"""
    if corr_long.empty:
        return {}
    out = {}
    for style in STYLE_REPS:
        sub = corr_long[corr_long["core"] == style]
        if sub.empty:
            continue
        s = sub.set_index("date")["rho"]
        out[f"exp_{style}"] = s
    return out


# ─────────────────────────── 读写 parquet ───────────────────────────

def upsert_parquet(df: pd.DataFrame, out_dir: Path,
                   key: tuple[str, ...] = ("date", "factor")) -> dict:
    """按年分文件 upsert（整文件覆盖写，幂等；不删任何文件）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if df.empty:
        return {}
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    written = {}
    con = duckdb.connect()
    try:
        for year, gy in d.groupby(d["date"].dt.year):
            path = out_dir / f"part-{year}.parquet"
            if path.exists():
                old = con.execute(
                    f"SELECT * FROM read_parquet('{path.as_posix()}')"
                ).df()
                old["date"] = pd.to_datetime(old["date"])
                merged = pd.concat([old, gy], ignore_index=True)
                merged = (merged.sort_values(list(key))
                          .drop_duplicates(subset=list(key), keep="last"))
            else:
                merged = gy
            con.register("m", merged)
            con.execute(
                f"COPY (SELECT * FROM m ORDER BY date) TO "
                f"'{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            con.unregister("m")
            written[int(year)] = len(merged)
    finally:
        con.close()
    return written


def write_l0(df: pd.DataFrame, pool: str) -> dict:
    return upsert_parquet(df, POOL_DIR / pool)


def read_l0(pool: str = "ashare_ex",
             names: list[str] | None = None) -> pd.DataFrame:
    """读 L0（DuckDB glob 直读，规避 pandas glob 坑）"""
    directory = POOL_DIR / pool
    if not directory.exists():
        return pd.DataFrame()
    q = (f"SELECT * FROM read_parquet('{directory.as_posix()}/part-*.parquet'"
         ", hive_partitioning=false)")
    if names:
        lst = ", ".join(f"'{n}'" for n in names)
        q += f" WHERE factor IN ({lst})"
    con = duckdb.connect()
    try:
        return con.execute(q + " ORDER BY date, factor").df()
    finally:
        con.close()


# ─────────────────────────── L1 滚动缓存 ───────────────────────────

def compute_rolling(pool: str = "ashare_ex",
                    start: str | None = None) -> pd.DataFrame:
    """L1：从 L0 日 IC 序列派生滚动 ICIR/胜率/t 值。

    h∈{5,20}（主口径）× w∈{20,60,120}。可随时从 L0 全量重建。
    """
    df = read_l0(pool)
    if df.empty:
        return df
    if start:
        df = df[df["date"] >= pd.to_datetime(start)]
    out = []
    for factor, g in df.groupby("factor"):
        g = g.sort_values("date").set_index("date")
        base = {"factor": factor, "date": g.index}
        for h in ROLL_HORIZONS:
            s = g[f"rank_ic{h}"]
            # 未成熟 NaN 日不得计入胜率分母（mask 保 NaN）
            sp = s.gt(0).astype(float).mask(s.isna())
            for w in ROLL_WINDOWS:
                r = s.rolling(w, min_periods=10)
                m, sd, n = r.mean(), r.std(), r.count()
                base[f"rank_icir{h}_w{w}"] = m
                base[f"rank_win{h}_w{w}"] = sp.rolling(
                    w, min_periods=10).mean()
                base[f"rank_t{h}_w{w}"] = m / (sd / n.pow(0.5))
        out.append(pd.DataFrame(base))
    res = pd.concat(out, ignore_index=True)
    res["universe"] = pool
    return res


def refresh_rolling(pool: str = "ashare_ex") -> dict:
    """全量重建 L1（池级；每年 <1MB，直接覆盖）"""
    df = compute_rolling(pool)
    if df.empty:
        return {"pool": pool, "rows": 0}
    written = upsert_parquet(df, ROLL_DIR / pool)
    return {"pool": pool, "rows": int(len(df)), "files": written}


# ─────────────────────── 回填/增量 worker（进程级） ───────────────────────

_W: dict = {}  # worker 进程全局（con + 缓存表名）


def worker_init(start: str, end: str, mem_limit_gb: int = 10) -> None:
    """Pool initializer：每 worker 进程建一次缓存：

    - px_cache：kline_daily 的 (date, code, close*adj_factor)，
      避免每因子重扫主库
    - core_vals：9 核心因子值长表（date, code, core, cv），省去
      每因子 9 次 parquet 读取（rank 必须每因子在交集截面重算，
      详见 core_vals 注释）

    资源（用户 2026-09-13 指示）：workers=2 × threads=4 +
    memory_limit=10GB（2GB 崩溃、6GB 卡死，10GB 稳）。
    """
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute(f"SET memory_limit='{mem_limit_gb}GB'")
    con.execute("SET enable_progress_bar=false")
    # 有界重试：主库写锁被其他进程（并行会话/自动化）瞬时持有时，
    # 等 60s 而非立即崩（2026-09-14 流水线内曾空转 3h/2005 次崩）
    for _att in range(30):
        try:
            con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
            break
        except duckdb.IOException:
            if _att == 29:
                raise
            import time as _t
            _t.sleep(2)
    # 价格缓存（200 日 buffer 覆盖 h=120 成熟）
    con.execute(f"""
        CREATE TEMP TABLE px_cache AS
        SELECT date, code, close * adj_factor AS c
        FROM maindb.kline_daily
        WHERE date >= DATE '{start}'
          AND date <= DATE '{end}' + INTERVAL 200 DAY
    """)
    # 核心因子值缓存（date, code, core, cv）——rank 不预缓存：
    # 正确口径是"因子∩核心交集截面内 rank"（覆盖差异会改变结果），
    # 必须在每因子 join 后重算（2026-09-13 曾试 core_ranked 预缓存，
    # 全截面 rank ≠ 交集 rank，判为口径 bug 已撤）。
    unions = "\n                UNION ALL ".join(
        f"(SELECT CAST(date AS DATE) AS date, code, '{c}' AS core, value AS cv "
        f"FROM read_parquet({_factor_paths(c)}) "
        f"WHERE date BETWEEN DATE '{start}' AND DATE '{end}')"
        for c in CORE_PAIR_TARGETS)
    con.execute(f"""
        CREATE TEMP TABLE core_vals AS
        SELECT * FROM ({unions})
    """)
    _W["con"] = con


def _worker_finalize() -> None:
    con = _W.pop("con", None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def backfill_worker(job: tuple):
    """worker 任务：单因子 L0+暴露+核心对（复用进程级缓存）。"""
    name, pool, start, end = job
    con = _W.get("con")
    own = con is None
    if own:
        worker_init(start, end)
        con = _W["con"]
    try:
        # px_cache 池无关（全市场价格），任何池都可用；
        # core_vals 只对全市场池（核心因子池内需重过滤，交集 rank 须重算）
        l0 = compute_factor_l0(name, pool, start=start, end=end, con=con,
                               px_src="px_cache")
        cc = compute_factor_core_corr(name, pool, start=start, end=end,
                                      con=con,
                                      core_src="core_vals"
                                      if pool == "ashare_ex" else None)
    finally:
        if own:
            _worker_finalize()
    if not l0.empty:
        # 暴露 exp_* 列并入 L0（按 date 对齐）。
        # 自身即核心因子的列（如 size 的 exp_size）core 侧排除自身，
        # 显式置 NaN 保持列齐全（2026-09-13 zz1000 实录 KeyError）
        l0["date"] = pd.to_datetime(l0["date"])
        for style in STYLE_REPS:
            l0[f"exp_{style}"] = float("nan")
            sub = cc[cc["core"] == style]
            if sub.empty:
                continue
            s = sub.set_index(pd.to_datetime(sub["date"]))["rho"]
            l0[f"exp_{style}"] = l0["date"].map(s)
    if not cc.empty:
        cc = cc.copy()
        cc["factor"] = name
    return name, pool, l0, cc


def lake_factors() -> list[str]:
    """湖内有效因子名单（只收有年份分片 parquet 的目录——
    overnight/ 等遗留杂项目录会让 worker FileNotFoundError 崩批）。"""
    return sorted(d.name for d in FACTOR_DIR.iterdir()
                  if d.is_dir() and list(d.glob("part-*.parquet")))


def recent_signal_start(lookback: int = 130,
                        con: duckdb.DuckDBPyConnection | None = None) -> str:
    """最近 lookback 个交易日的起点（成熟回填窗口，覆盖 h=120）"""
    own = con is None
    if own:
        con = duckdb.connect()
    try:
        r = con.execute(
            f"SELECT min(date) FROM (SELECT DISTINCT date FROM read_parquet("
            f"{_factor_paths('size')}) ORDER BY date DESC LIMIT {lookback})"
        ).fetchone()
        return str(r[0])
    finally:
        if own:
            con.close()


def _lake_max_date() -> str:
    con = duckdb.connect()
    try:
        r = con.execute(
            f"SELECT max(date) FROM read_parquet({_factor_paths('size')})"
        ).fetchone()
        return str(r[0])
    finally:
        con.close()


def update_daily(pools: list[str] | None = None, lookback: int = 130,
                 workers: int = 4) -> str:
    """每日增量（流水线挂钩入口，方案 §4.3）：

    1. 各注册池重算最近 lookback 交易日 L0 行 → upsert
       （整行重算=成熟信号日 T-1/T-5/T-20/T-60/T-120 补格 + 当日
       覆盖/分布/换手/暴露一步到位，无旧行残留）
    2. L1 滚动缓存全量重建（秒级）
    3. L2 核心对追加入库（只全市场）
    周五附带全矩阵相关（周任务）。
    """
    import datetime as _dt
    import multiprocessing as mp

    # 关键：父进程若持有主库 Store 单例（流水线前序步骤留下，写模式），
    # 跨进程写锁会挡住 worker 的 ATTACH READ_ONLY（2026-09-14 实锤：
    # 流水线内该步骤空转 3h/2005 次 IOException）。spawn 前先关闭单例。
    try:
        from ..data.store import Store
        if Store._instance is not None:
            Store._instance.close()
    except Exception:  # noqa: BLE001
        pass

    pools = pools or active_pools()
    start = recent_signal_start(lookback)
    px_end = _lake_max_date()
    names = lake_factors()
    jobs = [(n, p, start, None) for p in pools for n in names]

    n_ok, n_err = 0, 0
    corr_buf = []
    errors: list[str] = []
    ctx = mp.get_context("spawn")
    with ctx.Pool(min(workers, max(1, len(jobs))),
                  initializer=worker_init,
                  initargs=(start, px_end)) as mp_pool:
        for name, pool, l0, cc in mp_pool.imap_unordered(backfill_worker, jobs):
            try:
                if not l0.empty:
                    write_l0(l0, l0["universe"].iloc[0] if len(l0) else
                             "ashare_ex")
                    n_ok += 1
                # corr 表无池维度（设计=只全市场口径）；
                # zz1000 的 cc 只用于 L0 exp_* 列，严禁入库（2026-09-14 实录）
                if not cc.empty and pool == "ashare_ex":
                    corr_buf.append(cc)
            except Exception as e:
                n_err += 1
                errors.append(f"{name}: {e}")

    roll_info = [refresh_rolling(p) for p in pools]
    corr_rows = 0
    if corr_buf:
        allc = pd.concat(corr_buf, ignore_index=True)
        write_corr_daily(allc)
        corr_rows = len(allc)

    weekly = ""
    if _dt.date.today().weekday() == 4:  # 周五：全矩阵周任务
        try:
            fm = compute_full_matrix()
            if not fm.empty:
                write_corr_weekly(fm)
                weekly = f"，周全矩阵 {len(fm):,} 对已入库"
        except Exception as e:
            log.warning(f"周全矩阵失败（不阻塞）: {e}")

    msg = (f"L0 {n_ok} 因子×{len(pools)} 池（{start} 起重算 {lookback} 日窗），"
           f"L1 滚动 {sum(r.get('rows', 0) for r in roll_info):,} 行，"
           f"L2 核心对 {corr_rows:,} 行{weekly}")
    if n_err:
        msg += f"；⚠️ {n_err} 个失败: {'; '.join(errors[:5])}"
    return msg


# ─────────────────────────── L2 相关层 ───────────────────────────

def write_corr_daily(df: pd.DataFrame) -> dict:
    """长表 (date, factor, core_factor, rho) 按年写"""
    if df.empty:
        return {}
    d = df.rename(columns={"core": "core_factor"})[
        ["date", "factor", "core_factor", "rho"]].copy()
    d["date"] = pd.to_datetime(d["date"])
    return upsert_parquet(d, CORR_DIR, key=("date", "factor", "core_factor"))


def compute_full_matrix(end_date: str | None = None,
                        n_days: int = 5,
                        con: duckdb.DuckDBPyConnection | None = None,
) -> pd.DataFrame:
    """全库 N×N 周频截面相关（最近 n_days 交易日平均，秩-秩 spearman）。

    返回长表 (week, factor_a, factor_b, rho)。只算全市场口径。
    """
    own = con is None
    if own:
        con = duckdb.connect()
        _attach(con)
    try:
        if end_date is None:
            end_date = con.execute(
                "SELECT max(date) FROM read_parquet("
                "'data/lake/factor/size/part-*.parquet')").fetchone()[0]
        names = lake_factors()
        dates = [str(r[0]) for r in con.execute(
            f"SELECT DISTINCT date FROM read_parquet("
            f"'data/lake/factor/size/part-*.parquet') "
            f"WHERE date <= DATE '{end_date}' ORDER BY date DESC "
            f"LIMIT {n_days}").fetchall()]
        if not dates:
            return pd.DataFrame()
        start = min(dates)
        # 值列名因因子而异（value/score），逐因子探测（DESCRIBE 元数据，快）
        unions = "\n            UNION ALL ".join(
            f"(SELECT date, code, '{n}' AS factor, "
            f"{_vcol(con, _factor_paths(n)[0])} AS v FROM "
            f"read_parquet({_factor_paths(n)}) "
            f"WHERE date BETWEEN DATE '{start}' AND DATE '{end_date}')"
            for n in names)
        sql = f"""
        WITH u AS ({unions}),
        r AS (SELECT date, code, factor,
                     RANK() OVER (PARTITION BY date, factor ORDER BY v) AS rv
              FROM u WHERE v IS NOT NULL)
        SELECT factor, code, date, rv FROM r
        """
        wide_src = con.execute(sql).df()
    finally:
        if own:
            con.close()
    if wide_src.empty:
        return pd.DataFrame()
    week = pd.to_datetime(dates[-1]).strftime("%G-W%V")
    rows = []
    for dt, gd in wide_src.groupby("date"):
        w = gd.pivot(index="code", columns="factor", values="rv")
        w = w.dropna(axis=1, thresh=30).dropna(axis=0)
        if w.shape[1] < 2:
            continue
        corr = w.corr(method="spearman", min_periods=30)
        corr.index.name = "factor_a"
        corr.columns.name = "factor_b"
        s = corr.stack().dropna().rename("rho").reset_index()
        s = s[s["factor_a"] < s["factor_b"]]
        rows.append(s)
    if not rows:
        return pd.DataFrame()
    allp = pd.concat(rows)
    avg = (allp.groupby(["factor_a", "factor_b"])["rho"]
           .mean().rename("rho").reset_index())
    avg["week"] = week
    return avg[["week", "factor_a", "factor_b", "rho"]]


def write_corr_weekly(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    out_dir = CORR_W_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    week = df["week"].iloc[0]
    year = int(str(week)[:4])
    path = out_dir / f"part-{year}.parquet"
    con = duckdb.connect()
    try:
        if path.exists():
            old = con.execute(
                f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
            merged = (pd.concat([old, df], ignore_index=True)
                      .sort_values(["week", "factor_a", "factor_b"])
                      .drop_duplicates(subset=["week", "factor_a", "factor_b"],
                                       keep="last"))
        else:
            merged = df
        con.register("m", merged)
        con.execute(
            f"COPY (SELECT * FROM m ORDER BY week, factor_a) TO "
            f"'{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        con.unregister("m")
    finally:
        con.close()
    return {year: len(merged)}
