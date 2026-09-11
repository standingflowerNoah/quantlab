"""ETF / 基金 净值入湖（westock 腾讯源）
================================================================
用户裁决（2026-09-12）：补齐 ETF 净值。
数据源：westock CLI（C:/Users/53497/.local/bin/westock.exe）
  westock etf nav <code[,code...]> --start YYYY-MM-DD --end YYYY-MM-DD

⚠️ 形态（2026-09-12 实测）
- **有完整历史**：510300 可回溯到 **2012-05-28（上市首日）**，
  即「区间查询 → 一次性全史回补」，不是快照。
- 输出为 Markdown：`**sh510300**` 分段 + 每段一个管道表
  （date / nav / closePrice / navChange / navChangePct）
- 支持逗号批量（`sh510300,sz159915` 实测可混市场），本模块仍按市场分组更稳
- 首个交易日 navChange/navChangePct 为 `-`

派生字段（本模块计算）：
- `premium_pct = close_price / nav - 1` —— **折溢价率**（ETF 核心指标，
  湖里此前完全没有）

湖结构：
  data/lake/clean/etf_nav/year=YYYY/part-full.parquet   历史回补（按年）
  data/lake/clean/etf_nav/year=YYYY/part-dYYYYMMDD.parquet  每日增量
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pandas as pd

from .. import config
from ..config import get_logger

log = get_logger(__name__)

WESTOCK = Path(r"C:\Users\53497\.local\bin\westock.exe")
NAV_DIR = config.CLEAN_DIR / "etf_nav"

KEEP_COLS = ["date", "code", "nav", "close_price", "nav_change",
             "nav_change_pct", "premium_pct"]


def _prefix(code: str) -> str:
    """6 位基金代码 → westock 市场前缀（5xxxxx=沪市基金，1xxxxx=深市基金）"""
    c = str(code).zfill(6)
    if c.startswith("5"):
        return "sh"
    if c.startswith("1"):
        return "sz"
    return "sh" if c.startswith("6") else "sz"


def parse_nav_md(text: str, fallback_code: str | None = None) -> pd.DataFrame:
    """解析 `**code**` 分段 + 管道表 → DataFrame（列：code/date/...）

    ⚠️ 实测：**批量查询才有 `**code**` 分段头；单只查询直接给裸表格**。
    单只调用必须传 `fallback_code`，否则解析会静默落空（返回 0 行且不报错）。
    """
    rows = []
    cur: str | None = fallback_code
    header: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        m = re.fullmatch(r"\*\*([A-Za-z]{2}\d{6})\*\*", s)
        if m:
            cur = re.sub(r"^(sh|sz|bj)", "", m.group(1))
            header = None
            continue
        if not s.startswith("|") or cur is None:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        rows.append({"code": cur, **dict(zip(header, cells))})
    if not rows:
        return pd.DataFrame(columns=KEEP_COLS)
    df = pd.DataFrame(rows)
    df = df.rename(columns={"closePrice": "close_price",
                            "navChange": "nav_change",
                            "navChangePct": "nav_change_pct"})
    df = df.replace("-", pd.NA).replace("", pd.NA)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("nav", "close_price", "nav_change", "nav_change_pct"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["date"].notna() & df["nav"].notna()]
    df["premium_pct"] = df["close_price"] / df["nav"] - 1.0
    for c in KEEP_COLS:
        if c not in df.columns:
            df[c] = None
    return df[KEEP_COLS]


def _call(codes: list[str], start: str, end: str,
          timeout: float = 300.0) -> pd.DataFrame:
    """调 westock etf nav（一次一批）"""
    arg = ",".join(f"{_prefix(c)}{str(c).zfill(6)}" for c in codes)
    try:
        r = subprocess.run(
            [str(WESTOCK), "etf", "nav", arg,
             "--start", start, "--end", end],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.warning(f"westock nav 超时（{len(codes)} 只 {start}~{end}）")
        return pd.DataFrame(columns=KEEP_COLS)
    if r.returncode != 0:
        log.debug(f"westock nav 返回 {r.returncode}: {r.stderr[:120]}")
    # 单只时无 **code** 分段头 → 用 codes[0] 兜底
    fb = codes[0] if len(codes) == 1 else None
    return parse_nav_md(r.stdout or "", fallback_code=fb)


# 实测单次返回硬上限 ~1211 行（≈4.8 年），**超限静默截断为最近 N 行**
SEG_LIMIT = 1180
SEG_YEARS = 3          # 3 年 ≈ 730 个交易日，远低于上限 → 不会触顶


def _segments(start: str, end: str, years: int = SEG_YEARS):
    """把 [start, end] 按 years 切段（防单次超限被静默截断）"""
    s, out = pd.Timestamp(start), []
    e = pd.Timestamp(end)
    while s <= e:
        e2 = min(s + pd.DateOffset(years=years) - pd.Timedelta(days=1), e)
        out.append((s, e2))
        s = e2 + pd.Timedelta(days=1)
    return out


def fetch_nav(codes: list[str], start: str | None = None,
              end: str | None = None, batch: int = 20) -> pd.DataFrame:
    """区间取净值（**按时间切段** × 代码分批；段内批量可混市场）"""
    start = (start or "20040101")
    end = (end or pd.Timestamp.now().strftime("%Y%m%d"))
    segs = _segments(start, end)
    groups: dict[str, list[str]] = {}
    for c in codes:
        groups.setdefault(_prefix(c), []).append(c)

    frames, calls = [], 0
    for (s, e) in segs:
        ss, ee = s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")
        for mk, cs in groups.items():
            for i in range(0, len(cs), batch):
                chunk = cs[i:i + batch]
                d = _call(chunk, ss, ee)
                calls += 1
                if not len(d):
                    log.debug(f"  {ss}~{ee} {mk} 批{i//batch+1} 空")
                    continue
                # 触顶防护：若某只行数逼近上限，说明该段可能被截断
                mx = int(d.groupby("code").size().max())
                if mx >= SEG_LIMIT - 5:
                    log.warning(f"  ⚠️ {ss}~{ee} {mk} 批{i//batch+1} "
                                f"最大 {mx} 行逼近上限 {SEG_LIMIT}，可能被截断")
                frames.append(d)
    log.info(f"  净值取数：{calls} 次调用 / {len(segs)} 个时间段")
    if not frames:
        return pd.DataFrame(columns=KEEP_COLS)
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["code", "date"], keep="last")


def backfill(start: str = "20040101", end: str | None = None,
             codes: list[str] | None = None, batch: int = 20) -> dict:
    """全史回补净值（按年落 part-full.parquet）"""
    from .etf import load_etf_daily
    if codes is None:
        codes = sorted(load_etf_daily()["code"].unique())
    t0 = time.time()
    log.info(f"ETF 净值回补 {start}~{end or 'today'}：候选 {len(codes)} 只"
             f"（batch={batch}）")
    df = fetch_nav(codes, start, end, batch)
    if df.empty:
        log.warning("净值回补：无数据返回")
        return {"rows": 0, "codes": 0, "years": 0, "elapsed": 0}

    years = 0
    NAV_DIR.mkdir(parents=True, exist_ok=True)
    for y, g in df.groupby(df["date"].dt.year):
        f = NAV_DIR / f"year={int(y)}" / "part-full.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists():                       # 与已有合并（部分回补不抹数据）
            try:
                old = pd.read_parquet(f)
                old["date"] = pd.to_datetime(old["date"])
                g = pd.concat([old[KEEP_COLS], g], ignore_index=True)
                g = g.drop_duplicates(subset=["code", "date"], keep="last")
            except Exception as e:                            # noqa: BLE001
                log.warning(f"  已有 part-full 合并失败({y}): {e}")
        g.sort_values(["code", "date"]).to_parquet(
            f, index=False, compression="zstd")
        years += 1
    log.info(f"ETF 净值回补完成：{len(df):,} 行 / {df['code'].nunique()} 只 / "
             f"{years} 个年份文件 / {time.time()-t0:.0f}s")
    return {"rows": int(len(df)), "codes": int(df["code"].nunique()),
            "years": years, "elapsed": round(time.time() - t0, 1)}


def update(target: pd.Timestamp | str | None = None,
           codes: list[str] | None = None, batch: int = 20) -> int:
    """每日增量：取最近 10 天净值（覆盖 + 幂等），并入当日分区"""
    from .etf import load_etf_daily
    day = pd.Timestamp(target) if target is not None else \
        pd.Timestamp.now().normalize()
    if codes is None:
        codes = sorted(load_etf_daily()["code"].unique())
    start = day - pd.Timedelta(days=10)
    df = fetch_nav(codes, start, day, batch)
    if df.empty:
        log.warning(f"ETF 净值 {day.date()} 增量无数据")
        return 0
    f = NAV_DIR / f"year={day.year}" / f"part-d{day:%Y%m%d}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values(["code", "date"]).to_parquet(
        f, index=False, compression="zstd")
    log.info(f"ETF 净值 {day.date()}: 写入 {len(df):,} 行 / "
             f"{df['code'].nunique()} 只 → {f.name}")
    return len(df)


def load(start: str | None = None, end: str | None = None,
         codes: list[str] | None = None) -> pd.DataFrame:
    """读取净值（去重合并 part-full 与 part-d*）"""
    pat = str(NAV_DIR / "year=*" / "part-*.parquet").replace("\\", "/")
    try:
        df = _read_glob(pat)
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=KEEP_COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end:
        df = df[df["date"] <= pd.Timestamp(end)]
    if codes:
        df = df[df["code"].isin([str(c).zfill(6) for c in codes])]
    return df.sort_values(["code", "date"]).reset_index(drop=True)


def _read_glob(pat: str) -> pd.DataFrame:
    import duckdb
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT * FROM read_parquet('{pat}', hive_partitioning=false)"
        ).df()
    finally:
        con.close()


def coverage() -> dict:
    import duckdb
    pat = str(NAV_DIR / "year=*" / "part-*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        r = con.execute(f"""
            SELECT count(*) n, count(DISTINCT code) codes,
                   min(date) d0, max(date) d1,
                   avg(premium_pct) avg_prem
            FROM read_parquet('{pat}', hive_partitioning=false)
        """).df()
        return r.to_dict("records")[0]
    finally:
        con.close()


def refresh(target: pd.Timestamp | str | None = None) -> int:
    """流水线入口"""
    return update(target)
