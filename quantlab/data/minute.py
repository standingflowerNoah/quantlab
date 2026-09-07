"""分钟K线层（1 分钟，不复权）
=====================================
- 数据源：free-stockdb 本地引擎（tools/free-stockdb，LevelDB + stockdb.exe）
- 存储：data/lake/clean/kline_1min/year=YYYY/part-*.parquet（source of truth）
- 查询：DuckDB 视图 kline_1min（read_parquet glob，无镜像需求）
- 对齐：MINUTE_START 与 kline_daily 对齐（2022-01-01 起，随日频扩）

Schema（Parquet / 视图）：
    datetime TIMESTAMP    分钟 bar 时间（如 09:30 = 开盘/竞价 bar，09:31 = 09:30-09:31）
    code     VARCHAR      6 位代码
    open/high/low/close DOUBLE  不复权价格（元）
    vol      DOUBLE       成交量（股）——与 kline_daily.vol 同单位
                          （2026-09-06 对账实测：kline_daily.vol 为股，
                           tdx_source 注释"手"有误；fsdb 分钟求和与日线
                           吻合到个位数）
    amount   DOUBLE       成交额（元）

设计要点：
- Parquet 只追加：回补写 part-b{seq}，增量按日写 part-d{YYYYMMDD}（重跑覆盖同名=幂等）
- 回补进度存 _backfill_progress.json（done 代码清单），断点续跑
- 复权不在本层处理：沿用 kline_daily.adj_factor（按日粒度）体系
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from .. import config
from ..config import get_logger
from .store import Store

log = get_logger(__name__)

MIN_COLS = ["datetime", "code", "open", "high", "low", "close", "vol", "amount"]

PROGRESS_FILE = config.KLINE_1MIN_DIR / "_backfill_progress.json"


# ── DuckDB 视图 ────────────────────────────────────────────────────
def ensure_view(store: Store | None = None):
    """注册/刷新 kline_1min 视图（read_parquet glob，随文件动态）"""
    store = store or Store()
    d = config.KLINE_1MIN_DIR
    d.mkdir(parents=True, exist_ok=True)
    glob_pat = str(d / "year=*" / "part-*.parquet").replace("\\", "/")
    store.con.execute(f"""
        CREATE OR REPLACE VIEW kline_1min AS
        SELECT datetime, code, open, high, low, close, vol, amount
        FROM read_parquet('{glob_pat}', hive_partitioning=false)
    """)
    store.register_dataset(
        "kline_1min", "clean", "free-stockdb", "minute",
        "1分钟K线（不复权；vol=股；Parquet 湖，视图查询）",
        "datetime,code,open,high,low,close,vol,amount")
    for col, meaning in [
            ("datetime", "bar 时间（09:30 为开盘/竞价 bar，09:31 表示 09:30-09:31）"),
            ("vol", "成交量（股），与 kline_daily.vol 同单位"),
            ("amount", "成交额（元）"),
            ("open/high/low/close", "不复权价格（元），复权配合 kline_daily.adj_factor")]:
        store.con.execute(
            "INSERT OR REPLACE INTO data_dict VALUES (?,?,?)",
            ["kline_1min", col, meaning])


# ── Parquet 写入 ───────────────────────────────────────────────────
def _write_parts(df: pd.DataFrame, prefix: str) -> int:
    """按年分区写 parquet（文件名 part-{prefix}.parquet，与视图 glob 对齐）"""
    if df.empty:
        return 0
    df = df.sort_values(["code", "datetime"])
    written = 0
    for year in sorted(df["datetime"].dt.year.unique()):
        part = df[df["datetime"].dt.year == year]
        ydir = config.KLINE_1MIN_DIR / f"year={year}"
        ydir.mkdir(parents=True, exist_ok=True)
        f = ydir / f"part-{prefix}.parquet"
        tmp = ydir / f".part-{prefix}.parquet.tmp"
        part.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(f)
        written += len(part)
    return written


def _next_b_seq() -> int:
    """已有 part-b 文件的最大序号 + 1（防止回补批次撞名覆盖旧数据）"""
    mx = 0
    if config.KLINE_1MIN_DIR.exists():
        for ydir in config.KLINE_1MIN_DIR.glob("year=*"):
            for f in ydir.glob("part-b*.parquet"):
                try:
                    mx = max(mx, int(f.stem.split("b")[-1]))
                except ValueError:
                    pass
    return mx + 1


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": []}


def _save_progress(prog: dict):
    config.KLINE_1MIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.KLINE_1MIN_DIR / "._progress.tmp"
    tmp.write_text(json.dumps(prog, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


# ── 数据访问 ───────────────────────────────────────────────────────
def _fetch_minute(code: str, start: str, end: str,
                  retries: int = 3) -> pd.DataFrame:
    """单股分钟拉取（HTTP 路径，含身份校验）。start/end 为 8 位 YYYYMMDD。

    单次失败重试（网络抖动/服务端偶发）；身份校验失败直接抛异常
    （错数据宁可失败，不可入库）。
    """
    from .sources import fsdb_source
    last_err = None
    for attempt in range(retries):
        try:
            raw = fsdb_source.minute_bars(code, start, end)
        except Exception as e:
            last_err = str(e)[:80]
            time.sleep(1.5 + attempt)
            continue
        if raw.empty:
            return pd.DataFrame(columns=MIN_COLS)   # 真·无数据
        ts = raw["date"].astype("int64").astype(str)
        out = pd.DataFrame({
            "datetime": pd.to_datetime(ts, format="%Y%m%d%H%M%S"),
            "code": code,
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "vol": raw["volume"].astype(float),   # 股（与 kline_daily.vol 一致）
            "amount": raw["amount"].astype(float),
        })
        return out[MIN_COLS]
    raise RuntimeError(f"{code} 分钟拉取重试耗尽: {last_err}")


def _target_codes() -> list[str]:
    """对齐目标：kline_daily 出现过的全部代码（含期间退市）。

    只读优先（不阻塞并行研究进程），写锁被占时降级 Parquet 镜像。
    """
    try:
        df = Store(readonly=True, wait_lock=False).q(
            "SELECT DISTINCT code FROM kline_daily")
    except Exception:
        f = config.CLEAN_DIR / "mirror" / "kline_daily.parquet"
        log.info(f"DuckDB 写锁被占，代码清单走镜像 {f.name}")
        df = pd.read_parquet(f, columns=["code"])
    return sorted(df["code"].unique())


def _try_metadata(fn, *a, **kw):
    """元数据写入（视图/水位/run_log）带锁容错：锁被占时告警跳过，
    下次 update 域调度会自愈（ensure_view + _refresh_watermark）"""
    try:
        return fn(*a, **kw)
    except Exception as e:
        if "already open" in str(e) or "IOException" in type(e).__name__:
            log.warning("DuckDB 写锁被占（并行任务运行中），"
                        "分钟层元数据注册延后至下次更新")
            return None
        raise


# ── part-b 存量键（增量防双写）────────────────────────────────────
def _b_glob() -> str:
    return str(config.KLINE_1MIN_DIR / "year=*" /
               "part-b*.parquet").replace("\\", "/")


def _existing_b_keys(start_ts) -> pd.DataFrame:
    """part-b（回补存量）中 start_ts 之后的 (code, datetime) 键。

    增量写入前 anti-join 用：lookback 回看窗口与回补存量重叠时，
    已存在的键不再写 part-d，防止同一根 bar 双份入湖。
    （part-d 文件之间的幂等靠同名覆盖；只有 b/d 重叠需要显式剔除）
    """
    import duckdb
    try:
        con = duckdb.connect()
        return con.execute(
            f"SELECT code, datetime FROM read_parquet('{_b_glob()}', "
            f"hive_partitioning=false) WHERE datetime >= ?",
            [start_ts]).df()
    except Exception:
        return pd.DataFrame(columns=["code", "datetime"])


def _existing_b_codes() -> set:
    """part-b 存量中的全部代码（新股补全防重：progress 丢失场景）"""
    import duckdb
    try:
        con = duckdb.connect()
        df = con.execute(
            f"SELECT DISTINCT code FROM read_parquet('{_b_glob()}', "
            f"hive_partitioning=false)").df()
        return set(df["code"])
    except Exception:
        return set()


def _scan_lake_max_datetime():
    """直扫 parquet 湖取 MAX(datetime)（含 part-b 与 part-d）。

    用途：① 水位兜底（元数据未注册时）；② 水位完整性判定——水位按
    DATE 存储（时间被截断），无法凭其判断最新交易日是否拉到 15:00
    收盘 bar，只能直查数据本身。parquet 行组统计量加速，秒级返回；
    独立 duckdb 连接，不依赖视图/镜像/写锁。
    """
    import duckdb
    try:
        glob_pat = str(config.KLINE_1MIN_DIR / "year=*" /
                       "part-*.parquet").replace("\\", "/")
        mx = duckdb.connect().execute(
            f"SELECT MAX(datetime) FROM read_parquet("
            f"'{glob_pat}', hive_partitioning=false)"
        ).fetchone()[0]
        return pd.Timestamp(mx) if mx is not None else None
    except Exception:
        return None


# ── 全量回补 ──────────────────────────────────────────────────────
def init_kline_1min(resume: bool = True, batch_flush: int = 40,
                    codes: list[str] | None = None):
    """全量回补：MINUTE_START → 今日，断点续跑

    batch_flush: 每积累 N 只股票 flush 一次 parquet（控制内存）
    codes: 指定代码清单（测试/补漏用），默认对齐 kline_daily 全部代码
    设计：拉取+写 Parquet 全程不碰 DuckDB（可与并行研究进程共存），
         元数据（视图/水位/run_log）在结束时注册（锁冲突则延后自愈）。
    """
    from .sources import fsdb_source
    fsdb_source.ensure_service()

    codes = codes or _target_codes()
    prog = _load_progress()
    done = set(prog["done"]) if resume else set()
    todo = [c for c in codes if c not in done]
    log.info(f"分钟回补: 目标 {len(codes)} 只, 待做 {len(todo)} 只, "
             f"范围 {config.MINUTE_START} → 今日")

    run_id = None
    try:
        s = Store()
        ensure_view(s)
        run_id = s.log_run("kline_1min", "init")
    except Exception as e:
        log.info(f"DuckDB 暂不可写（{type(e).__name__}），元数据延后注册")

    start = config.MINUTE_START.replace("-", "")
    end = pd.Timestamp.now().strftime("%Y%m%d")
    t0, ok, empty, fail, rows_total = time.time(), 0, 0, 0, 0
    buf: list[pd.DataFrame] = []

    def flush(seq: int):
        nonlocal rows_total, buf
        if not buf:
            return
        df = pd.concat(buf, ignore_index=True)
        n = _write_parts(df, prefix=f"b{seq:05d}")
        rows_total += n
        buf = []

    seq = _next_b_seq()
    for i, code in enumerate(todo, 1):
        try:
            df = _fetch_minute(code, start, end)
            if df.empty:
                # 不标记 done：服务端半残态可能误报空，下次运行复查
                empty += 1
            else:
                ok += 1
                buf.append(df)
                done.add(code)
            if len(buf) >= batch_flush:
                seq += 1
                flush(seq)
        except Exception as e:
            fail += 1
            log.warning(f"分钟拉取失败 {code}: {e}")
            if fail > 200:
                raise RuntimeError("分钟回补失败过多，中止（进度已保存可续跑）")
        if i % 50 == 0:
            _save_progress({"done": sorted(done)})
            dt = time.time() - t0
            log.info(f"分钟进度 {i}/{len(todo)} ok={ok} empty={empty} "
                     f"fail={fail} rows={rows_total} "
                     f"({dt:.0f}s 均{dt/i:.2f}s/只 剩{(len(todo)-i)*dt/i/60:.0f}分)")
    seq += 1
    flush(seq)
    _save_progress({"done": sorted(done)})

    def _finish():
        s = Store()
        ensure_view(s)
        _refresh_watermark(s)
        if run_id is None:
            run_id2 = s.log_run("kline_1min", "init")
            s.finish_run(run_id2, "ok", rows_total,
                         f"ok={ok} empty={empty} fail={fail}")
        else:
            s.finish_run(run_id, "ok", rows_total,
                         f"ok={ok} empty={empty} fail={fail}")
    _try_metadata(_finish)
    log.info(f"分钟回补完成: ok={ok} 无数据={empty} 失败={fail} "
             f"共 {rows_total} 行, 耗时 {(time.time()-t0)/60:.1f} 分")
    return {"ok": ok, "empty": empty, "fail": fail, "rows": rows_total}


# ── 增量更新 ──────────────────────────────────────────────────────
def update_kline_1min(lookback_days: int = 5, batch_flush: int = 40) -> dict:
    """增量：水位回看 lookback_days 天，按日写 part-d 文件（幂等覆盖）

    防双写：窗口数据写入前 anti-join part-b 存量键（lookback 与回补存量
    重叠是常态而非异常）；新股已入 part-b 存量（progress 缺记录）的跳过。
    无新交易日且当日已拉全（直扫湖 MAX(datetime) 到 15:00）时跳过窗口
    拉取，省全量回看；水位为 DATE 存储，时点完整性以数据为准。
    拉取+写 Parquet 不碰 DuckDB；元数据结束时注册。
    """
    from .sources import fsdb_source
    fsdb_source.ensure_service()

    def _ro(sql, params=None):
        try:
            return Store(readonly=True, wait_lock=False).q(sql, params)
        except Exception:
            from .store import query
            return query(sql, params)   # 镜像降级

    wm_df = _ro("SELECT watermark FROM watermarks WHERE domain='kline_1min'")
    wm = (pd.Timestamp(wm_df.iloc[0, 0]) if not wm_df.empty
          else None)
    if wm is None:
        # 数据侧水位兜底（元数据可能因写锁延后注册）：直扫 parquet 取 max
        wm = _scan_lake_max_datetime()
    if wm is None and not _load_progress()["done"]:
        return {"skipped": "分钟层未初始化，先执行 cli.py data minute-init"}
    start_ts = (wm - pd.Timedelta(days=lookback_days)
                if wm is not None else pd.Timestamp(config.MINUTE_START))
    start = max(start_ts, pd.Timestamp(config.MINUTE_START)
                ).strftime("%Y%m%d")
    end = pd.Timestamp.now().strftime("%Y%m%d")
    if start > end:
        return {"skipped": "水位超前"}

    # 近端活跃代码 + 新增代码（kline_daily 有而分钟层无）
    active = _ro("SELECT DISTINCT code FROM kline_daily WHERE date >= ?",
                 [pd.Timestamp(start)])["code"].tolist()
    prog = _load_progress()
    done = set(prog["done"])
    new_codes = [c for c in _target_codes() if c not in done]

    # 新股防重：已落入 part-b 存量的代码（progress 丢失场景）不再全量拉
    if new_codes:
        b_codes = _existing_b_codes()
        dropped = [c for c in new_codes if c in b_codes]
        if dropped:
            done.update(dropped)
            new_codes = [c for c in new_codes if c not in b_codes]
            log.info(f"新股补全: {len(dropped)} 只已在 part-b 存量"
                     f"（progress 缺记录），补记 done 跳过")

    # 无新交易日且当日已拉全（最后一根 bar 到 15:00）→ 跳过窗口拉取。
    # 注意：水位按 DATE 存储（时间被截断），不能用它判断当日完整性——
    # 必须直扫湖取 MAX(datetime)；盘中跑增量时 last_bar=14:xx < 15:00
    # 仍会重拉窗口，收盘段不会漏（引擎最后一根 bar 为 15:00）。
    last_daily = _ro("SELECT MAX(date) AS d FROM kline_daily")["d"][0]
    last_bar = _scan_lake_max_datetime()
    wm_full = last_bar is not None and (
        last_bar.date() > pd.Timestamp(last_daily).date() or
        (last_bar.date() == pd.Timestamp(last_daily).date()
         and last_bar.hour >= 15))

    t0, rows_total, empty = time.time(), 0, 0
    full_start = config.MINUTE_START.replace("-", "")
    days_written: set[str] = set()
    buf_by_day: dict[str, list[pd.DataFrame]] = {}
    new_buf: list[pd.DataFrame] = []
    new_seq, new_written = _next_b_seq(), 0

    def flush_new():
        nonlocal new_buf, new_seq, new_written, rows_total
        if not new_buf:
            return
        new_seq += 1
        df = pd.concat(new_buf, ignore_index=True)
        n = _write_parts(df, prefix=f"b{new_seq:05d}")
        rows_total += n
        new_written += n
        new_buf = []

    act = sorted(set(active))
    if wm_full:
        log.info(f"分钟增量: 最后一根 bar {last_bar} 已完整覆盖最新交易日 "
                 f"{last_daily}，跳过窗口拉取；新增 {len(new_codes)} 只（全量）")
    else:
        # 防回补重叠双写：窗口内已存在于 part-b 存量的 (code, datetime) 键
        # 写入前剔除；part-d 之间仍靠同名覆盖幂等（新交易日整日重拉覆盖）
        b_keys = _existing_b_keys(pd.Timestamp(start))
        if not b_keys.empty:
            # duckdb TIMESTAMP 可能映射 datetime64[us]，与拉取侧 ns 不一致
            # 会导致 merge 静默不匹配——统一转 ns
            b_keys = b_keys.assign(
                datetime=lambda d: d["datetime"].astype("datetime64[ns]"))
        else:
            log.warning("part-b 存量键查询为空（无回补存量或读取失败），"
                        "本次增量不做防重剔除")
        log.info(f"分钟增量: 活跃 {len(act)} 只（{start}→{end}）+ "
                 f"新增 {len(new_codes)} 只（全量）；窗口存量键 {len(b_keys)} 条")

        for i in range(0, len(act), 500):      # 分块拉取，控制内存
            dfs = []
            for code in act[i:i + 500]:
                try:
                    df = _fetch_minute(code, start, end)
                except Exception as e:
                    log.warning(f"分钟增量失败 {code}: {e}")
                    continue
                if df.empty:
                    empty += 1
                    continue
                done.add(code)
                dfs.append(df)
            if not dfs:
                continue
            win = pd.concat(dfs, ignore_index=True)
            if not b_keys.empty:
                win = (win.merge(b_keys, on=["code", "datetime"],
                                 how="left", indicator=True)
                          .loc[lambda d: d["_merge"] == "left_only"]
                          .drop(columns="_merge"))
                if win.empty:
                    continue
            for day, g in win.groupby(
                    win["datetime"].dt.strftime("%Y%m%d")):
                buf_by_day.setdefault(str(day), []).append(g)

    for code in new_codes:
        try:
            df = _fetch_minute(code, full_start, end)
        except Exception as e:
            log.warning(f"新股全量失败 {code}: {e}")
            continue
        if df.empty:
            empty += 1     # 计数（wm_full 跳过路径下唯一可见的空结果信号）
            continue          # 不标记 done，下次再查（可能停牌/无数据）
        done.add(code)
        new_buf.append(df)
        if len(new_buf) >= batch_flush:
            flush_new()
    flush_new()

    for day, parts in sorted(buf_by_day.items()):
        df = pd.concat(parts, ignore_index=True)
        n = _write_parts(df, prefix=f"d{day}")
        rows_total += n
        days_written.add(day)
    _save_progress({"done": sorted(done)})

    def _finish():
        s = Store()
        ensure_view(s)
        run = s.log_run("kline_1min", "update")
        s.finish_run(run, "ok", rows_total,
                     f"days={sorted(days_written)} new={new_written} "
                     f"empty={empty}")
        _refresh_watermark(s)
    _try_metadata(_finish)
    log.info(f"分钟增量完成: {len(days_written)} 日 + 新股 {new_written} 行 "
             f"共 {rows_total} 行 ({time.time()-t0:.0f}s)")
    return {"days": sorted(days_written), "rows": rows_total,
            "new_rows": new_written, "empty": empty}


def _refresh_watermark(store: Store):
    """视图扫描刷新水位（分钟层行数大，仅在回补/增量后调用）"""
    try:
        wm = store.q("SELECT MAX(datetime) AS d FROM kline_1min")["d"][0]
        if wm is not None:
            store.set_watermark("kline_1min", pd.Timestamp(wm).date())
    except Exception as e:      # 视图为空（尚无 parquet）
        log.debug(f"watermark 刷新跳过: {e}")


# ── 对账校验 ──────────────────────────────────────────────────────
def validate_minute(sample: int = 50, days: int = 30) -> pd.DataFrame:
    """分钟聚合 vs 日线对账（OHLC / 量 / 额），结果写 quality_report

    校验口径（2026-09-06 全量对账实测校准）：
    - **已知口径差异**（数据源机制，单列统计不判 fail）：
      * high/low 单向缺失：m_high < high 或 m_low > low——fsdb 分钟从
        09:30 起，不含 09:25 集合竞价撮合极值（22.6% 股日出现，单向）
      * vol/amount 单向缺失：m_vol < vol——大宗交易/盘后固定价格交易
        tdx 日线计入、fsdb 分钟不含（最大差异 ~11%）
      * close 微差：深市 14:57-15:00 收盘集合竞价（<0.9%）
    - **判错**（fail，说明数据真有问题）：
      * open/close 相对误差 > 0.2%
      * high/low 反向：m_high > high 或 m_low < low 超过 0.2%
        （分钟数据超出日线范围 = 错误）
      * vol/amount 反向（m_vol > vol）或差异 > 5%
    """
    from .store import query
    codes_df = query("""
        SELECT code FROM kline_daily
        WHERE date = (SELECT MAX(date) FROM kline_daily)
        ORDER BY code LIMIT ?""", [sample])
    if codes_df.empty:
        return pd.DataFrame()
    codes = codes_df["code"].tolist()
    d_max = query("SELECT MAX(date) AS d FROM kline_daily")["d"][0]
    d_min = pd.Timestamp(d_max) - pd.Timedelta(days=days * 2)

    agg = query("""
        SELECT code, CAST(datetime AS DATE) AS date,
               first(open ORDER BY datetime) AS m_open,
               max(high) AS m_high, min(low) AS m_low,
               last(close ORDER BY datetime) AS m_close,
               sum(vol) AS m_vol, sum(amount) AS m_amount
        FROM kline_1min
        WHERE code IN ({}) AND datetime >= ?
        GROUP BY code, CAST(datetime AS DATE)""".format(
            ",".join(f"'{c}'" for c in codes)),
        [d_min])
    if agg.empty:
        return pd.DataFrame()
    daily = query("""
        SELECT code, date, open, high, low, close, vol, amount
        FROM kline_daily WHERE code IN ({}) AND date >= ?""".format(
            ",".join(f"'{c}'" for c in codes)),
        [d_min])
    m = agg.merge(daily, on=["code", "date"], suffixes=("", "_d"))

    def relerr(a, b):
        return (a - b).abs() / b.abs().clip(lower=1e-9)

    m["open_err"] = relerr(m["m_open"], m["open"])
    m["close_err"] = relerr(m["m_close"], m["close"])
    m["high_err"] = relerr(m["m_high"], m["high"])
    m["low_err"] = relerr(m["m_low"], m["low"])
    m["vol_err"] = relerr(m["m_vol"], m["vol"])
    m["amount_err"] = relerr(m["m_amount"], m["amount"])

    # 真实错误（fail 判定）
    px_bad = (m["open_err"] > 0.002) | (m["close_err"] > 0.002) | \
             (m["m_high"] > m["high"] * 1.002) | \
             (m["m_low"] < m["low"] * 0.998)          # 反向才判错
    vol_bad = (m["vol_err"] > 0.05) | (m["m_vol"] > m["vol"] * 1.05)
    amt_bad = (m["amount_err"] > 0.05) | (m["m_amount"] > m["amount"] * 1.05)
    bad = m[px_bad | vol_bad | amt_bad]

    # 已知口径差异（单列统计，不判 fail）
    auc_gap = ((m["m_high"] < m["high"]) | (m["m_low"] > m["low"])).sum()
    block_gap = ((m["vol_err"] > 0.02) &
                 (m["m_vol"] < m["vol"])).sum()        # 大宗单向缺失
    n_bad = len(bad)
    status = "pass" if n_bad == 0 else ("warn" if n_bad <= len(m) * 0.01
                                        else "fail")
    detail = (f"样本 {len(m)} 股日: 真实超阈 {n_bad}（价格 "
              f"{int(px_bad.sum())}/量 {int(vol_bad.sum())}/额 "
              f"{int(amt_bad.sum())}）; 口径差: 竞价极值缺失 {auc_gap}, "
              f"大宗量差 {block_gap}")
    _try_metadata(lambda: Store().add_quality(
        "kline_1min", "consistency", status, detail))
    log.info(f"分钟对账: {status} — {detail}")
    if n_bad:
        cols = ["code", "date", "m_open", "open", "m_close", "close",
                "m_high", "high", "m_low", "low",
                "m_vol", "vol", "m_amount", "amount",
                "vol_err", "amount_err"]
        return bad[cols].head(20)
    return m[["code", "date", "open_err", "close_err", "vol_err",
              "amount_err"]].describe().reset_index()


# ── 状态 ──────────────────────────────────────────────────────────
def minute_status() -> pd.DataFrame:
    """分钟层覆盖统计（按年）"""
    from .store import query
    try:
        df = query("""
            SELECT year(datetime) AS 年, count(*) AS 行数,
                   count(DISTINCT code) AS 代码数,
                   min(datetime) AS 起点, max(datetime) AS 终点
            FROM kline_1min GROUP BY 1 ORDER BY 1""")
    except Exception:
        return pd.DataFrame({"说明": ["kline_1min 视图尚无数据（先执行回补）"]})
    return df
