"""ETF 持仓 / 申赎清单（PCF）逐日快照入湖（westock 腾讯源）
================================================================
数据源：westock CLI
  westock etf holdings <code[,code...]>  [--date YYYY-MM-DD]

⚠️ 形态（2026-09-12 实测）
- **`--date` 参数不生效**：实测 `--date 2023-06-01` 与 `--date 2026-09-10`
  返回完全相同的持仓 → 属**快照型数据**，历史无法回补，
  只能从接入日起**逐日积累**（与 board 快照同一模式）。
- 每只 ETF 每次返回两张表：
  - `重仓股涨跌`（10 条）：code/name/ratio/rate/change
  - `申赎清单成分股`（PCF，**共 20 条，只显示前 10**）：code/name/ratio
  ⚠️ PCF 被截断为前 10 条（"共 20 条，显示前 10"），**不是完整清单**。
- 批量时按 `**shXXXXXX**` 分段；**单只时无分段头**，需 fallback。

湖结构：
  data/lake/clean/etf_holding/snap=YYYY-MM-DD/part-full.parquet
（`snap=` 分区避开与真实列同名；PIT 语义 = 该日或之前最近一份快照）

字段：snap / etf_code / kind(top|pcf) / stock_code / stock_name /
      ratio / rate / change
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
HOLD_DIR = config.CLEAN_DIR / "etf_holding"

KEEP_COLS = ["snap", "etf_code", "kind", "stock_code", "stock_name",
             "ratio", "rate", "change"]


def _prefix(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith("5"):
        return "sh"
    if c.startswith("1"):
        return "sz"
    return "sh" if c.startswith("6") else "sz"


def parse_holding_md(text: str, fallback_code: str | None = None) -> pd.DataFrame:
    """解析 `**etf**` 分段 +  `重仓股涨跌`/`申赎清单成分股` 两表"""
    rows, cur, kind, header = [], fallback_code, None, None
    for line in text.splitlines():
        s = line.strip()
        m = re.fullmatch(r"\*\*([A-Za-z]{2}\d{6})\*\*", s)
        if m:
            cur = re.sub(r"^(sh|sz|bj)", "", m.group(1))
            kind, header = None, None
            continue
        if s.startswith("**重仓股涨跌**"):
            kind, header = "top", None
            continue
        if s.startswith("**申赎清单成分股**"):
            kind, header = "pcf", None
            continue
        if not s.startswith("|") or cur is None or kind is None:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if header is None:
            header = cells
            continue
        if len(cells) != len(header):
            continue
        rec = dict(zip(header, cells))
        rows.append({
            "etf_code": cur, "kind": kind,
            "stock_code": rec.get("code"),
            "stock_name": rec.get("name"),
            "ratio": rec.get("ratio"),
            "rate": rec.get("rate"),
            "change": rec.get("change"),
        })
    if not rows:
        return pd.DataFrame(columns=KEEP_COLS)
    df = pd.DataFrame(rows)
    for c in ("ratio", "rate", "change"):
        df[c] = pd.to_numeric(df[c].replace("-", None), errors="coerce")
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    return df


def _call(codes: list[str], date: str | None, timeout: float = 300.0) -> pd.DataFrame:
    arg = ",".join(f"{_prefix(c)}{str(c).zfill(6)}" for c in codes)
    cmd = [str(WESTOCK), "etf", "holdings", arg]
    if date:
        cmd += ["--date", date]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.warning(f"westock holdings 超时（{len(codes)} 只）")
        return pd.DataFrame(columns=KEEP_COLS)
    fb = codes[0] if len(codes) == 1 else None
    return parse_holding_md(r.stdout or "", fallback_code=fb)


def snapshot(target: pd.Timestamp | str | None = None,
             codes: list[str] | None = None, batch: int = 10) -> int:
    """取当日 ETF 持仓快照并落盘（幂等：同日覆盖）"""
    from .etf import load_etf_daily
    day = pd.Timestamp(target) if target is not None else \
        pd.Timestamp.now().normalize()
    if codes is None:
        codes = sorted(load_etf_daily()["code"].unique())

    t0, frames = time.time(), []
    groups: dict[str, list[str]] = {}
    for c in codes:
        groups.setdefault(_prefix(c), []).append(c)
    for mk, cs in groups.items():
        for i in range(0, len(cs), batch):
            d = _call(cs[i:i + batch], None)
            if len(d):
                frames.append(d)
    if not frames:
        log.warning(f"ETF 持仓快照 {day.date()} 无数据")
        return 0

    df = pd.concat(frames, ignore_index=True)
    df["snap"] = day.normalize()
    df = df[KEEP_COLS].drop_duplicates(
        subset=["snap", "etf_code", "kind", "stock_code"], keep="last")
    out = HOLD_DIR / f"snap={day:%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    # 同名 part-full.parquet 直接覆盖 = 幂等
    # （不做删除：本机 safe-delete 会拦截批量删除，>50 个文件 fail-closed）
    df.sort_values(["etf_code", "kind", "ratio"],
                   ascending=[True, True, False]).to_parquet(
        out / "part-full.parquet", index=False, compression="zstd")
    log.info(f"ETF 持仓快照 {day.date()}: {len(df):,} 行 / "
             f"{df['etf_code'].nunique()} 只 / {time.time()-t0:.0f}s → {out}")
    return len(df)


def load(snap: str | None = None) -> pd.DataFrame:
    """读取持仓快照（可指定 snap 日期）"""
    import duckdb
    pat = str(HOLD_DIR / "**" / "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        sql = (f"SELECT {', '.join(KEEP_COLS)} FROM "
               f"read_parquet('{pat}', hive_partitioning=false)")
        if snap:
            sql += f" WHERE CAST(snap AS DATE) = DATE '{snap}'"
        return con.execute(sql).df()
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=KEEP_COLS)
    finally:
        con.close()


def coverage() -> dict:
    import duckdb
    pat = str(HOLD_DIR / "**" / "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        r = con.execute(f"""
            SELECT count(*) n, count(DISTINCT etf_code) etfs,
                   count(DISTINCT CAST(snap AS DATE)) n_snaps,
                   min(snap) s0, max(snap) s1
            FROM read_parquet('{pat}', hive_partitioning=false)
        """).df()
        return r.to_dict("records")[0]
    except Exception:                                        # noqa: BLE001
        return {}
    finally:
        con.close()


def refresh(target: pd.Timestamp | str | None = None) -> int:
    """流水线入口"""
    return snapshot(target)
