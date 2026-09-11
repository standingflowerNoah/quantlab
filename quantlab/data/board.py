"""板块映射（概念 + 申万）逐日快照入湖
================================================================
数据源：free-stockdb `板块*` 表（1,337 个板块，含成分股 symbols）

⚠️ 关键约束（PIT）：fsdb 只提供**最新快照**，没有任何历史版本。直接把当前
成分用于历史回测 = 前视偏差（用"今天的成分"筛"三年前的股票"）。因此本模块
的设计是**逐日快照积累**：每天落一份带日期的全量映射，历史从此不可回补，
只能从启用日起前向累积。

湖结构（Parquet = source of truth）：
  data/lake/clean/board/board_meta/snap=YYYY-MM-DD/part-dYYYYMMDD.parquet
      date, board_code, board_name, source, board_type, group, category,
      n_symbols
  data/lake/clean/board/board_member/snap=YYYY-MM-DD/part-dYYYYMMDD.parquet
      date, board_code, code

查询用法（ASOF 到指定日，取该日或之前最近一份快照）：
    from quantlab.data.board import board_members, board_catalog
    m = board_members(asof="2026-09-11", category="申万一级")
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import config
from ..config import get_logger
from .sources import fsdb_source as fs


def _read_glob(pat: str, columns: list[str] | None = None) -> pd.DataFrame:
    """用 in-memory DuckDB 读 parquet glob（项目惯例；pandas 3.x 不支持 glob）

    hive_partitioning=false：分区目录名（snap=/year=）不注入为列，避免与
    真实列名冲突或误导 schema。
    """
    import duckdb
    sel = ", ".join(columns) if columns else "*"
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT {sel} FROM read_parquet('{pat}', hive_partitioning=false)"
        ).df()
    finally:
        con.close()

log = get_logger(__name__)

BOARD_DIR = config.CLEAN_DIR / "board"
META_DIR = BOARD_DIR / "board_meta"
MEMBER_DIR = BOARD_DIR / "board_member"

# 板块表列（fsdb 返回 code/name/source/type/group/category/symbols）
_RENAME = {"code": "board_code", "name": "board_name", "type": "board_type"}

# 申万级别标记（category 值）
SW_LEVELS = ("申万一级", "申万二级", "申万三级")


def _part(day: pd.Timestamp) -> str:
    return f"part-d{day.strftime('%Y%m%d')}.parquet"


def _write_part(df: pd.DataFrame, base: Path, day: pd.Timestamp,
                keys: list[str]) -> int:
    if df is None or df.empty:
        return 0
    d = base / f"snap={day.strftime('%Y-%m-%d')}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / _part(day)
    df = df.drop_duplicates(subset=keys)
    df.to_parquet(f, index=False, compression="zstd")   # 同名覆盖 = 幂等
    return len(df)


def snapshot(target: pd.Timestamp | str | None = None,
             clamp_to_today: bool = True) -> dict:
    """抓取当前板块映射并落一份带日期的快照（同日重复运行 = 覆盖，幂等）

    ⚠️ PIT 保护：fsdb 只提供**当前**成分。若允许用今天的成分去写"历史某日"
    的快照，就等于伪造历史（前视偏差）。因此 clamp_to_today=True 时，快照
    日期一律取今天；传入更早的 target 会被夹到今天并告警。确实要人工补录
    时才显式传 clamp_to_today=False。
    """
    today = pd.Timestamp.now().normalize()
    day = pd.Timestamp(target).normalize() if target is not None else today
    if clamp_to_today and day != today:
        log.warning(f"板块快照请求日期 {day.date()} ≠ 今天 {today.date()}："
                    f"fsdb 只有当前成分，为避免伪造历史已夹到今天")
        day = today
    raw = fs.boards()
    if raw is None or raw.empty:
        raise RuntimeError("板块表返回为空，跳过快照（不写空文件）")

    meta = raw.rename(columns=_RENAME).copy()
    meta["date"] = day
    meta["n_symbols"] = meta["symbols"].apply(
        lambda s: len(s) if isinstance(s, (list, tuple)) else 0)
    meta = meta[["date", "board_code", "board_name", "source", "board_type",
                 "group", "category", "n_symbols"]]
    n_meta = _write_part(meta, META_DIR, day, ["date", "board_code"])

    rows = []
    for _, r in raw.iterrows():
        syms = r.get("symbols")
        if not isinstance(syms, (list, tuple)):
            continue
        for c in syms:
            rows.append({"date": day, "board_code": r["code"], "code": str(c)})
    members = pd.DataFrame(rows)
    n_mem = _write_part(members, MEMBER_DIR, day, ["date", "board_code", "code"])

    log.info(f"板块快照 {day.date()}: {n_meta} 个板块, {n_mem} 条成分记录 → "
             f"{BOARD_DIR}")
    return {"date": str(day.date()), "boards": n_meta, "members": n_mem}


def _latest_snapshot(base: Path, asof: pd.Timestamp | str | None,
                     col: str = "date") -> pd.Timestamp | None:
    pat = str(base / "snap=*" / "part-*.parquet").replace("\\", "/")
    try:
        df = _read_glob(pat, columns=[col])
    except Exception:                                        # noqa: BLE001
        return None
    if df.empty:
        return None
    d = pd.to_datetime(df[col]).max()
    if asof is not None:
        d = pd.to_datetime(df[pd.to_datetime(df[col])
                              <= pd.Timestamp(asof)][col]).max()
    return None if pd.isna(d) else pd.Timestamp(d)


def board_catalog(asof: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """板块目录（ASOF：取该日或之前最近一份快照）"""
    d = _latest_snapshot(META_DIR, asof)
    if d is None:
        return pd.DataFrame()
    f = str(META_DIR / f"snap={d.strftime('%Y-%m-%d')}" / _part(d))
    return _read_glob(f)


def board_members(asof: pd.Timestamp | str | None = None,
                  category: str | None = None) -> pd.DataFrame:
    """成分股表（ASOF）。category 可传 '申万一级'/'申万二级'/'申万三级'/'概念'

    返回 [date, board_code, board_name, category, code]
    """
    d = _latest_snapshot(MEMBER_DIR, asof)
    if d is None:
        return pd.DataFrame()
    f = str(MEMBER_DIR / f"snap={d.strftime('%Y-%m-%d')}" / _part(d))
    mem = _read_glob(f)
    cat = board_catalog(asof=asof)
    if len(cat):
        mem = mem.merge(cat[["board_code", "board_name", "source", "category"]],
                        on="board_code", how="left")
    if category is not None:
        mem = mem[mem["category"] == category]
    return mem.reset_index(drop=True)


def sw_industry_map(asof: pd.Timestamp | str | None = None,
                    level: str = "申万一级") -> pd.DataFrame:
    """A 股 → 申万行业（取该股在指定级别下的第一个板块，多归属取字典序最小）

    ⚠️ 版本警告（2026-09-11 对账结论）：fsdb 的申万是 **2014 版**
    （一级 28 个，含"化工/商业贸易/采掘/休闲服务/电气设备/纺织服装"），
    而本项目 `instruments.industry` 是 **2021 版**（一级 31 个、二级 128、
    三级 337），且 fsdb 的二/三级（104/227）同样不如本地齐全。
    实测行业名一致率仅 **76.7%** → **不要把本函数当作 industry_map 的
    替代或补充**；它只用于概念板块研究，或做分类版本差异诊断。
    """
    m = board_members(asof=asof, category=level)
    if m.empty:
        return pd.DataFrame(columns=["code", "sw_name", "sw_code", "level"])
    m = m.sort_values(["code", "board_code"])
    out = m.groupby("code", as_index=False).first()[
        ["code", "board_name", "board_code"]]
    return out.rename(columns={"board_name": "sw_name",
                               "board_code": "sw_code"}).assign(level=level)


def coverage(asof: pd.Timestamp | str | None = None) -> dict:
    """快照覆盖概况（板块数/成分数/各 category 计数）"""
    cat = board_catalog(asof=asof)
    if cat.empty:
        return {"snapshots": 0}
    pat = str(MEMBER_DIR / "snap=*" / "part-*.parquet").replace("\\", "/")
    mem = _read_glob(pat)
    return {"asof": str(cat["date"].max().date()),
            "snapshots": int(pd.to_datetime(mem["date"]).nunique()),
            "boards": int(len(cat)),
            "members": int(len(mem)),
            "by_category": cat["category"].value_counts().to_dict()}
