"""ETF / 基金 日线入湖（fsdb 源）
================================================================
覆盖：fsdb `股票代码` 组 1（深市）+ 组 5（沪市），共约 2,053 只基金类证券。
用户裁决（2026-09-11）：**先做 ETF 日线，不做分钟**。

与 kline_daily 同构（不复权，volume 单位=股，amount=元），差异：
- 池来自 fsdb 基金代码组，而非 instruments（股票主表）
- 保留 fsdb 日k 的估值类字段（ETF 多为空，但 LOF/分级基金可能有）
- `is_etf` 由名称是否含 'ETF' 判定（数据驱动，不靠代码前缀猜）

湖结构：
  data/lake/clean/etf_daily/year=YYYY/part-dYYYYMMDD.parquet   每日增量
  data/lake/clean/etf_daily/year=YYYY/part-full.parquet        历史回补（按年）
（按日落文件、同名覆盖=幂等；写入前 anti-join 已有 (code, date)）

⚠️ 历史深度（2026-09-11 实测，scripts/probe_fsdb_etf_history.py）：
  fsdb `日k` 对全部 2,053 只基金代码**都有历史**，合计 2,303,259 行，
  最早 2004-03-22。但字段完整度分两段：
    - OHLCV + amount：全历史 100% 完整
    - pre_close / turnover / total_share / total_mv：**仅 2026-07-01 起**有
      （ETF 历史段是精简 schema；股票 600519 同字段 100% 有）
  → 下游算换手/市值/涨跌幅时必须容忍这段空洞；pre_close 本就应自算
    （LAG(close) + 分红除权，见 MEMORY「fsdb pre_close 不可信」）。
  ⚠️ 另有 43 只标的起始被截断在 2024-01-02（非上市日，如 159903/159915 老
     基金）→ 属数据边界，回补也无法补齐，需在大全/替代源补。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

ETF_DIR = config.CLEAN_DIR / "etf_daily"

KEEP_COLS = ["date", "code", "name", "name_latest", "open", "high", "low",
             "close", "pre_close", "volume", "amount", "turnover", "pct_chg",
             "amplitude", "total_share", "total_mv", "is_etf"]


def _fill_names(df: pd.DataFrame) -> pd.DataFrame:
    """用近期观测到的名称反填全历史（→ name_latest）

    ⚠️ fsdb `日k` 的 ETF 历史段是**精简 schema**：`name` 只有 2026-07-01 起有
    （全表非空率 4.6%）。缺 name 会让 `is_etf` 在历史段恒为 False，
    把 96% 的 ETF 行误判成非 ETF，`load_etf_daily(etf_only=True)` 直接丢数据。

    基金名称是准静态属性（改名极少且不影响"是不是 ETF"），故取每个 code
    **最新已知名称**反填到其全部历史行。
    ⚠️ `name_latest` 不是 PIT 严格字段；原始 `name` 列保持不动，
       需要 PIT 语义时用 `name`，需要全历史标识时用 `name_latest`。
    """
    if "name" not in df.columns:
        df["name_latest"] = None
        return df
    known = (df.loc[df["name"].notna(), ["code", "date", "name"]]
             .sort_values("date")
             .drop_duplicates("code", keep="last")
             .set_index("code")["name"])
    df["name_latest"] = df["code"].map(known)
    log.info(f"  名称反填：{known.size} 只可判定 / "
             f"{df['code'].nunique()} 只（覆盖 {df['name_latest'].notna().mean():.1%} 行）")
    return df


def _fetch_one(code: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        d = fs.day_bars(code, start, end)
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"ETF 日线 {code} 失败: {e}")
        return None
    if d is None or d.empty:
        return None
    if (d["code"] != code).any():        # 防串位
        return None
    return d


def _existing_keys(day: pd.Timestamp) -> pd.DataFrame:
    """该日已有数据（用于 anti-join，避免重复写入）"""
    f = ETF_DIR / f"year={day.year}" / f"part-d{day.strftime('%Y%m%d')}.parquet"
    if not f.exists():
        return pd.DataFrame(columns=["code", "date"])
    try:
        return pd.read_parquet(f, columns=["code", "date"])
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=["code", "date"])


def update_etf_daily(target: pd.Timestamp | str | None = None,
                     codes: list[str] | None = None) -> int:
    """增量更新当日 ETF/基金日线，返回写入行数"""
    day = pd.Timestamp(target) if target is not None else \
        pd.Timestamp.now().normalize()
    day_c = day.strftime("%Y%m%d")
    if codes is None:
        codes = fs.fund_codes()
    log.info(f"ETF 日线更新 {day.date()}：候选 {len(codes)} 只")

    fs.ensure_healthy()
    frames, miss, t0 = [], 0, time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_one, c, day_c, day_c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            d = fut.result()
            if d is None or d.empty:
                miss += 1
            else:
                frames.append(d)
            if i % 800 == 0:
                log.info(f"  进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    if not frames:
        log.warning(f"ETF 日线 {day.date()} 无数据（缺 {miss} 只）")
        return 0

    df = _normalize(frames)
    df = df.rename(columns={"volume": "volume"})

    # anti-join 已有 (code, date)：同一日重复运行不产生重复行
    ex = _existing_keys(day)
    if len(ex):
        ex = ex.assign(date=pd.to_datetime(ex["date"]))
        df = df.merge(ex[["code", "date"]].assign(_dup=1),
                      on=["code", "date"], how="left")
        df = df[df["_dup"].isna()].drop(columns="_dup")

    if df.empty:
        log.info(f"ETF 日线 {day.date()}：全部已存在，跳过写入")
        return 0

    f = ETF_DIR / f"year={day.year}" / f"part-d{day.strftime('%Y%m%d')}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():                       # 同日已有部分数据 → 合并覆盖
        old = pd.read_parquet(f)
        old = old[~old["code"].isin(set(df["code"]))]
        df = pd.concat([old, df], ignore_index=True)
    df.sort_values(["code", "date"]).to_parquet(f, index=False,
                                                compression="zstd")
    n_etf = int(df["is_etf"].sum())
    log.info(f"ETF 日线 {day.date()}: 写入 {len(df)} 行"
             f"（其中 name 含 ETF 的 {n_etf} 行），缺 {miss} 只 → {f}")
    return len(df)


def _normalize(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """合并多只标的的原始返回，统一列与 dtype（含名称反填 + is_etf 判定）"""
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df[df["date"].notna()]
    for c in KEEP_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[KEEP_COLS]
    df = _fill_names(df)
    # is_etf 基于 name_latest（全历史可靠），而非仅有近期值的 name
    df["is_etf"] = (df["name_latest"].fillna("").astype(str)
                    .str.contains("ETF", case=False))
    return df


def backfill_etf_daily(start: str = "19900101", end: str | None = None,
                       codes: list[str] | None = None,
                       workers: int = 4) -> dict:
    """全历史回补 ETF/基金日线 → year=YYYY/part-full.parquet（按年一个文件）

    与每日增量 part-dYYYYMMDD.parquet 共存，互不重叠：
      part-full 覆盖「回补时点及之前」，part-d* 只含回补之后的新增日。
    重复运行幂等（同年 part-full 整体覆盖重写）。

    返回 {"rows": n, "codes": n, "years": n, "miss": n, "elapsed": s}
    """
    if codes is None:
        codes = fs.fund_codes()
    end = end or pd.Timestamp.now().strftime("%Y%m%d")
    log.info(f"ETF 历史回补 {start}~{end}：候选 {len(codes)} 只")

    fs.ensure_healthy()
    frames, miss, t0 = [], 0, time.time()

    def _fetch(code: str) -> pd.DataFrame | None:
        try:
            d = fs.day_bars(code, start, end)
        except Exception as e:                               # noqa: BLE001
            log.debug(f"ETF 回补 {code} 失败: {e}")
            return None
        if d is None or d.empty or (d["code"] != code).any():
            return None
        return d

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            d = fut.result()
            if d is None or d.empty:
                miss += 1
            else:
                frames.append(d)
            if i % 500 == 0:
                log.info(f"  进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")

    if not frames:
        log.warning("ETF 历史回补：无任何数据返回")
        return {"rows": 0, "codes": 0, "years": 0, "miss": miss,
                "elapsed": round(time.time() - t0, 1)}

    df = _normalize(frames)
    # 同日多文件并存时以回补数据为准，去重保护
    df = df.drop_duplicates(subset=["code", "date"], keep="last")

    years = 0
    for y, g in df.groupby(df["date"].dt.year):
        f = ETF_DIR / f"year={int(y)}" / "part-full.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        g = g.sort_values(["code", "date"])
        # 与已有 part-full 合并（部分回补 --codes 时不能抹掉该年其他标的）
        if f.exists():
            try:
                cur = pd.read_parquet(f)
                cur["date"] = pd.to_datetime(cur["date"])
                for c in KEEP_COLS:
                    if c not in cur.columns:
                        cur[c] = None
                g = pd.concat([cur[KEEP_COLS], g], ignore_index=True)
                g = g.drop_duplicates(subset=["code", "date"], keep="last")
                g = g.sort_values(["code", "date"])
            except Exception as e:                           # noqa: BLE001
                log.warning(f"  已存 part-full 合并失败（{y}）: {e}")
        # 合并该年已有的每日增量文件（回补覆盖近端时会与之重叠）
        parts = sorted(f.parent.glob("part-d*.parquet"))
        old_frames = []
        for p in parts:
            try:
                old_frames.append(pd.read_parquet(p))
            except Exception:                                # noqa: BLE001
                continue
        if old_frames:
            old = pd.concat(old_frames, ignore_index=True)
            old["date"] = pd.to_datetime(old["date"])
            for c in KEEP_COLS:
                if c not in old.columns:
                    old[c] = None
            g = pd.concat([old[KEEP_COLS], g], ignore_index=True)
            g = g.drop_duplicates(subset=["code", "date"], keep="last")
            g = g.sort_values(["code", "date"])
        g.to_parquet(f, index=False, compression="zstd")
        years += 1
        # 内容已并入 part-full → 删除被吸收的增量文件，避免同 (code,date) 双份
        gone = 0
        for p in parts:
            try:
                p.unlink()
                gone += 1
            except Exception as e:                           # noqa: BLE001
                log.warning(f"  旧增量文件未能删除 {p.name}: {e}")
        log.info(f"  年 {int(y)}: {len(g)} 行 → {f.name}"
                 + (f"（吸收并清理 {gone} 个增量文件）" if gone else ""))

    log.info(f"ETF 历史回补完成：{len(df)} 行 / {years} 年 / 缺 {miss} 只"
             f" / 耗时 {time.time()-t0:.0f}s")
    return {"rows": int(len(df)), "codes": int(df["code"].nunique()),
            "years": years, "miss": miss, "elapsed": round(time.time() - t0, 1)}


def load_etf_daily(start: str | None = None, end: str | None = None,
                   etf_only: bool = True) -> pd.DataFrame:
    """读取 ETF 日线（glob 直读 parquet，不依赖 DuckDB）"""
    pat = str(ETF_DIR / "year=*" / "part-*.parquet").replace("\\", "/")
    try:
        df = _read_glob(pat)
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=KEEP_COLS)
    if "year" in df.columns:             # hive 分区列回填，避免误导
        df = df.drop(columns=["year"])
    df["date"] = pd.to_datetime(df["date"])
    # part-full 与 part-d* 可能对同一 (code,date) 各留一份 → 去重
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    if etf_only and "is_etf" in df.columns:
        df = df[df["is_etf"]]
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end:
        df = df[df["date"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


def refresh(target: pd.Timestamp | str | None = None) -> int:
    """流水线入口（update.py 调用）"""
    return update_etf_daily(target)
