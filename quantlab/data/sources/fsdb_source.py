"""free-stockdb 数据源客户端（本地引擎 + HTTP API）
=====================================
架构：
- tools/free-stockdb/win/stockdb/ 为发行包（stockdb.exe 引擎 + 数据更新.exe 同步器）
- 数据同步：镜像 → 数据更新.exe --sync → ./data + ./data1（LevelDB，增量+断点续传）
- 查询服务：stockdb.exe 监听 127.0.0.1:7899，HTTP + msgpack 协议

访问层选型（2026-09-06 实测定论）：
- 【生产路径】HTTP API（requests + msgpack）：每请求独立连接，18/18 压测
  零失败、行数稳定、身份校验通过
- 【弃用】pyd SDK（pybao/stockdb.pyd）长连接：连续大查询下响应队列串位
  （A 股的查询返回 B 股的数据）+ 静默返回错误串 'Missing required
  parameters' + mget pipeline 对分钟范围查询返回空 —— 不可用于生产
- HTTP 查询格式：
  GET /?cmd=vals&t=分钟k&k1=key:{code}&k2=fwz:{start},{end}   范围取值
  GET /?cmd=get&t={table}:{key1}:{key2}                        点查
  返回 Content-Type: application/x-msgpack

单位约定（实测核验 2026-09-06）：
- fsdb 的 volume 为股；本项目 kline_daily.vol 按源不同为手/股，入湖时换算
- amount 为元

═══ 稳定性护栏（2026-09-11 压测定论，勿绕过）═══
引擎性能极好但**对无界查询零防护，且劣化后不自愈**。三态实测：

| 状态 | 触发            | 日线 p50  | 成功率 |
|------|-----------------|-----------|--------|
| 健康 | 服务刚重启       |    29 ms  |  100%  |
| 劣化 | 发过无界查询     | 318~686ms |  ~100% |
| 塌陷 | 持续无界 ~27min  | 19,056 ms |  16.7% |

- **元凶是无界查询，不是并发**：并发 4 线程实测 100% 成功、p50 39ms；
  而 `cmd=keys&t=*` 单发 12s 超时，并发 4 个则正常查询劣化到 376~686ms
- **劣化不自愈**：静默 70s 仍 318~574ms；`stop_service()+start_service()`
  仅 4.6~6.0s 即完全恢复（回到 13~40ms）
- 引擎 log 的 `long loop time` 在劣化期反而正常 → 瓶颈在查询/响应层

本模块内置四条护栏：
1. `_guard()` 入参校验：拒绝无界 keys 枚举、无界日期范围、无 k1 的通配表扫描
2. `_SEM` 全局并发信号量（≤4）
3. 单请求 15s 超时 + 重试 2 次（退避 1s/3s）
4. `ensure_healthy()` 延迟看门狗：小样本探测 p50 超阈值即重启服务

工程要点：
- 同步前必须退出 stockdb.exe（LevelDB 单写者），同步后重启
- ⚠️ 数据目录出现 `disable` 文件即表示该目录**已被标记停更**（需删除该文件
  才会再次同步）→ sync() 会检测并告警，勿忽略
- updater 为 GUI 程序无 stdout，且 --sync 完成后不退出（实测无增量日
  ~10s 完成工作后静默存活）→ 由 _watch_sync 空闲看门狗回收；镜像落
  data（主库 ~22GB）+ data1（~1.8GB）；完成判定 = 无 .part 残留
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import pandas as pd
import requests

from ... import config
from ...config import get_logger

log = get_logger(__name__)

# ── 护栏参数 ──────────────────────────────────────────────────────
HTTP_TIMEOUT = 15.0                      # 单请求超时（秒）
HTTP_RETRIES = 2                         # 失败重试次数
RETRY_BACKOFF = (1.0, 3.0)               # 重试退避
MAX_CONCURRENCY = 4                      # 全局并发上限
HEALTH_P50_MS = 500.0                    # 健康阈值：p50 超过即判定劣化
HEALTH_MIN_OK = 0.8                      # 健康阈值：成功率下限
HEALTH_RECHECK_S = 300.0                 # 看门狗最短复查间隔（节流）
_UNBOUNDED_LO = {"0", "", "00000000", "0.0"}

_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)
_last_health: float = 0.0


# ── 服务管理 ──────────────────────────────────────────────────────
def service_alive(timeout: float = 1.0) -> bool:
    """探测 stockdb 服务端口"""
    try:
        with socket.create_connection((config.FSDB_HOST, config.FSDB_PORT),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(up: bool = True, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if service_alive() == up:
            return True
        time.sleep(1.0)
    return False


def start_service() -> bool:
    """拉起 stockdb.exe（分离进程，不随本进程退出）"""
    if service_alive():
        return True
    exe = str(config.FSDB_SERVICE)
    if not Path(exe).exists():
        raise FileNotFoundError(f"stockdb.exe 不存在: {exe}（先放置发行包）")
    si = subprocess.STARTUPINFO()
    si.dwFlags = subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    subprocess.Popen([exe], cwd=str(config.FSDB_DIR),
                     startupinfo=si,
                     creationflags=subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)
    ok = _wait_port(up=True, timeout=60)
    if ok:
        log.info("stockdb 服务已启动 (127.0.0.1:7899)")
    else:
        log.error("stockdb 服务启动超时")
    return ok


def stop_service() -> bool:
    """停止 stockdb.exe（同步前必须退出，LevelDB 单写者）"""
    if not service_alive():
        return True
    subprocess.run(["taskkill", "/F", "/IM", "stockdb.exe"],
                   capture_output=True, text=True,
                   encoding="gbk", errors="ignore")
    ok = _wait_port(up=False, timeout=30)
    log.info("stockdb 服务已停止" if ok else "stop_service 未确认退出")
    return ok


def restart_service() -> float:
    """停服 + 起服，返回耗时（秒）。实测 4.6~6.0s。"""
    t0 = time.time()
    stop_service()
    start_service()
    return round(time.time() - t0, 1)


def ensure_service() -> None:
    """确保服务在线（离线则拉起），失败抛异常"""
    if not service_alive() and not start_service():
        raise ConnectionError("stockdb 服务不可用 (127.0.0.1:7899)")


# ── 健康看门狗 ────────────────────────────────────────────────────
def health_check(samples: int = 3, sample_code: str = "600519") -> dict:
    """小样本有界探测，返回 {healthy, ok_rate, p50_ms, n}"""
    lat, ok = [], 0
    for _ in range(max(1, samples)):
        t0 = time.perf_counter()
        try:
            rows = day_bars(sample_code, "20260801", "20260911",
                            timeout=HTTP_TIMEOUT, retries=0)
            if len(rows):
                ok += 1
        except Exception:                                    # noqa: BLE001
            pass
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else 9e9
    rate = ok / max(1, samples)
    healthy = (rate >= HEALTH_MIN_OK) and (p50 <= HEALTH_P50_MS)
    return {"healthy": healthy, "ok_rate": round(rate, 3),
            "p50_ms": round(p50, 1), "n": samples}


def ensure_healthy(force: bool = False, samples: int = 3) -> bool:
    """延迟看门狗：判定引擎劣化则自动重启（MTTR ≈ 5s）。

    节流：距上次检查不足 HEALTH_RECHECK_S 秒则跳过，避免每批都探测。
    返回 True 表示当前健康（含重启后恢复）。
    """
    global _last_health
    now = time.time()
    if not force and now - _last_health < HEALTH_RECHECK_S:
        return True
    _last_health = now
    try:
        h = health_check(samples=samples)
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"fsdb 健康探测失败（{e}），尝试重启")
        restart_service()
        return health_check(samples=samples)["healthy"]
    if h["healthy"]:
        return True
    log.warning(f"fsdb 引擎劣化（p50={h['p50_ms']}ms, ok_rate={h['ok_rate']}）"
                f"→ 自动重启")
    cost = restart_service()
    h2 = health_check(samples=samples)
    log.info(f"fsdb 重启完成（{cost}s），恢复后 p50={h2['p50_ms']}ms "
             f"ok_rate={h2['ok_rate']}")
    return h2["healthy"]


# ── 同步 ──────────────────────────────────────────────────────────
# 同步器会把镜像落到两个 LevelDB 目录：data（主库 ~22GB）+ data1（~1.8GB）
_SYNC_DB_DIRS = ("data", "data1")


def _part_files() -> list[str]:
    out: list[str] = []
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        out += [f"{sub}/{f}" for f in os.listdir(d) if f.endswith(".part")]
    return out


def disabled_dirs() -> list[str]:
    """检测被 `disable` 标记停更的数据目录（存在 = 该目录不再接收增量）"""
    out = []
    for sub in _SYNC_DB_DIRS:
        if (config.FSDB_DIR / sub / "disable").exists():
            out.append(sub)
    return out


def clean_part_files() -> list[str]:
    """清理 .part 残留（未完成传输的临时文件，下次同步会重下）"""
    removed = []
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        for f in os.listdir(d):
            if f.endswith(".part"):
                try:
                    os.remove(d / f)
                    removed.append(f"{sub}/{f}")
                except OSError as e:                         # noqa: BLE001
                    log.warning(f"清理 {sub}/{f} 失败: {e}")
    if removed:
        log.info(f"清理 .part 残留 {len(removed)} 个: {removed}")
    return removed


def _sync_size_mb() -> float:
    total = 0
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total / 1024 / 1024


def _sync_snapshot() -> tuple[int, int, int]:
    """同步进展观察量：两个 DB 目录的 (文件数, 总字节, .part 数)"""
    n = b = parts = 0
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        try:
            for f in os.scandir(d):
                if f.is_file():
                    n += 1
                    b += f.stat().st_size
                    if f.name.endswith(".part"):
                        parts += 1
        except OSError:
            pass
    return n, b, parts


def _watch_sync(proc: subprocess.Popen, timeout: int,
                poll_s: float = 15.0, grace_s: float = 90.0,
                idle_s: float = 240.0, stale_s: float = 600.0) -> None:
    """等待同步器：整体超时 + 空闲看门狗（2026-09-06/11 实测教训）。

    updater 为 GUI 程序，--sync 完成后**不退出**（无增量日实测：~10s
    完成全部工作后静默存活，零 CPU/零网络/零写入，subprocess.run 会
    阻塞到超时）。看门狗：宽限期后，目录快照（文件数/总字节/.part 数）
    持续空闲无变化 → 判定已完成或停滞，回收进程。两种情形均安全：
    ① 已完成：进程纯闲置，杀之无损；② 网络停滞（.part 不再增长）：
    .part 断点续传保证下次运行无损接力。

    2026-09-11 补充：`.part` 存在时放宽到 stale_s 才回收——"完成"与
    "停滞"在外部观测上不可区分，宁可多等，避免误杀仍在重试的连接。
    """
    t0 = time.time()
    last_snap = _sync_snapshot()
    last_move = t0
    while proc.poll() is None:
        time.sleep(poll_s)
        now = time.time()
        if now - t0 > timeout:                       # 整体超时（全量上限）
            log.warning(f"同步超时({timeout}s)，下次运行将继续断点续传")
            break
        snap = _sync_snapshot()
        if snap != last_snap:
            last_snap, last_move = snap, now
        else:
            idle = now - max(last_move, t0 + grace_s)
            has_part = snap[2] > 0
            limit = stale_s if has_part else idle_s
            if idle >= limit:
                log.warning(
                    f"同步器空闲 {idle:.0f}s 无进展（"
                    f"{'存在 .part，判为传输停滞' if has_part else '已完成或停滞'}"
                    f"），回收进程；未完部分由 .part 断点续传")
                break
    try:
        proc.kill()
    except OSError:
        pass


def sync(timeout: int | None = None, clean_parts: bool = True) -> dict:
    """运行镜像同步（增量，断点续传）。流程：
    停服 → 清理 .part 残留 → 检测 disable 标记 → 同步（看门狗）→ 起服

    返回 {"ok","elapsed_s","size_mb","parts_left","parts_cleaned",
          "disabled_dirs","freshness"}
    """
    t0 = time.time()
    timeout = timeout or config.FSDB_SYNC_TIMEOUT
    stop_service()

    cleaned = clean_part_files() if clean_parts else []
    disabled = disabled_dirs()
    if disabled:
        log.warning(f"⚠️ 数据目录 {disabled} 存在 disable 标记 → 该目录不再"
                    f"接收增量；确需继续同步请人工删除 "
                    f"{config.FSDB_DIR}/<dir>/disable")

    exe = str(config.FSDB_UPDATER)
    if not Path(exe).exists():
        raise FileNotFoundError(f"数据更新.exe 不存在: {exe}")
    si = subprocess.STARTUPINFO()
    si.dwFlags = subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    log.info("运行 free-stockdb 镜像同步（增量）...")
    proc = subprocess.Popen([exe, "--sync"], cwd=str(config.FSDB_DIR),
                            startupinfo=si)
    try:
        _watch_sync(proc, timeout)
    finally:
        start_service()

    parts = _part_files()
    out = {"ok": not parts,
           "elapsed_s": round(time.time() - t0, 1),
           "size_mb": round(_sync_size_mb(), 1),
           "parts_left": len(parts),
           "parts_cleaned": cleaned,
           "disabled_dirs": disabled,
           "freshness": data_freshness()}
    if out["ok"]:
        log.info(f"同步完成: {out['elapsed_s']}s, 数据 {out['size_mb']}MB, "
                 f"最新 {out['freshness']}")
    else:
        log.warning(f"同步未完成（残留 {len(parts)} 个 .part），"
                    f"将在下次同步续传")
    return out


# ── 查询护栏 ──────────────────────────────────────────────────────
def _guard(path: str) -> None:
    """拒绝会把引擎拖垮的无界查询（2026-09-11 压测结论）"""
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    cmd = (qs.get("cmd") or [""])[0]
    table = (qs.get("t") or [""])[0].strip()
    k2 = (qs.get("k2") or [""])[0].strip()

    if cmd == "keys" and table in ("*", ""):
        raise ValueError(
            f"禁止无界键枚举（cmd=keys&t={table or '*'}）：会把引擎拖入劣化态"
            f"且不自愈，见模块 docstring 稳定性护栏")
    if table in ("*", ""):
        raise ValueError(f"禁止无界表名查询（t={table or '空'}）")
    if k2.startswith("fwz:"):
        lo = k2[4:].split(",")[0].strip()
        if lo in _UNBOUNDED_LO:
            raise ValueError(
                f"禁止无界日期范围（k2={k2}）：起点必须显式给定，"
                f"如 fwz:19910101,20260911")
    if table.endswith("*") and "k1" not in qs:
        raise ValueError(f"通配表名 {table} 必须带 k1 约束")


# ── HTTP 数据访问（生产路径）──────────────────────────────────────
def http_get(path: str, timeout: float | None = None,
             retries: int | None = None, guard: bool = True):
    """HTTP + msgpack 查询（每请求独立连接，规避 pyd 长连接串位 bug）

    护栏：入参校验 / 全局并发 ≤4 / 15s 超时 / 重试 2 次（退避 1s/3s）
    """
    import msgpack
    if guard:
        _guard(path)
    ensure_service()
    url = f"http://{config.FSDB_HOST}:{config.FSDB_PORT}{path}"
    timeout = HTTP_TIMEOUT if timeout is None else timeout
    retries = HTTP_RETRIES if retries is None else retries
    # 本机引擎必须直连：绕过 HTTP(S)_PROXY 环境变量——代理会让批量
    # 分钟拉取大量 503/拒连（2026-09-07 实测事故，见 research/生产模型重构.md）
    last_exc: Exception | None = None
    with _SEM:
        for attempt in range(retries + 1):
            try:
                z = requests.get(url, timeout=timeout,
                                 proxies={"http": None, "https": None})
                z.raise_for_status()
                return msgpack.unpackb(z.content, raw=False)
            except Exception as e:                            # noqa: BLE001
                last_exc = e
                if attempt < retries:
                    time.sleep(RETRY_BACKOFF[min(attempt,
                                                 len(RETRY_BACKOFF) - 1)])
    raise RuntimeError(f"fsdb 查询失败（{retries + 1} 次尝试）: "
                       f"{type(last_exc).__name__}: {last_exc}")


def _check_rows(code: str, rows, kind: str) -> None:
    if rows and not isinstance(rows[0], dict):
        raise RuntimeError(f"{code} {kind}查询返回非 dict: {str(rows[0])[:60]}")
    if rows:
        bad = [r for r in rows if r.get("code") != code]
        if bad:
            raise RuntimeError(f"{code} {kind}响应身份校验失败"
                               f"（{len(bad)}/{len(rows)} 行 code 不符）")


def minute_bars(code: str, start: str, end: str | None = None,
                timeout: float | None = None,
                retries: int | None = None) -> pd.DataFrame:
    """单股分钟K（不复权）。start/end 为 8 位 YYYYMMDD。

    返回 DataFrame[date, code, open, high, low, close, volume, amount]
    （date 为 14 位 int；volume 单位=股；amount 单位=元）
    """
    end = end or "20991231"
    rows = http_get(f"/?cmd=vals&t=分钟k&k1=key:{code}"
                    f"&k2=fwz:{start}000000,{end}235959",
                    timeout=timeout, retries=retries)
    if not isinstance(rows, list):
        raise RuntimeError(f"{code} 分钟查询返回异常: {str(rows)[:60]}")
    _check_rows(code, rows, "分钟")
    return pd.DataFrame(rows)


def day_bars(code: str, start: str, end: str | None = None,
             timeout: float | None = None,
             retries: int | None = None) -> pd.DataFrame:
    """单股日K（不复权，含估值字段）。start/end 为 8 位 YYYYMMDD。

    21 字段：date/code/name/open/high/low/close/pre_close/volume/amount/
    turnover/pct_chg/amplitude/is_st/vol_ratio/total_share/float_share/
    total_mv/float_mv/pe_ttm/pb
    ⚠️ pre_close 不可信（复权口径混用），前收须自行 LAG(close)
    """
    end = end or "20991231"
    rows = http_get(f"/?cmd=vals&t=日k&k1=key:{code}&k2=fwz:{start},{end}",
                    timeout=timeout, retries=retries)
    if not isinstance(rows, list):
        raise RuntimeError(f"{code} 日k查询返回异常: {str(rows)[:60]}")
    _check_rows(code, rows, "日k")
    return pd.DataFrame(rows)


def adj_factors(code: str) -> pd.DataFrame:
    """单股除权事件表（div 分红/give 送股/trans 转增/mult 单次乘数/cum 累计）

    返回 DataFrame[code, ex_date, div, give, trans, mult, cum]
    """
    krows = http_get(f"/?cmd=keys&t=复权&k1=key:{code}"
                     f"&k2=fwz:19910101,20991231")
    vrows = http_get(f"/?cmd=vals&t=复权&k1=key:{code}"
                     f"&k2=fwz:19910101,20991231")
    keys = krows if isinstance(krows, list) else []
    vals = vrows if isinstance(vrows, list) else []
    if len(keys) != len(vals):
        raise RuntimeError(f"{code} 复权键值不匹配 {len(keys)}/{len(vals)}")
    out = []
    for k, v in zip(keys, vals):
        parts = str(k).split(":")
        if len(parts) < 3 or not isinstance(v, dict):
            continue
        out.append({"code": code, "ex_date": parts[2], **v})
    df = pd.DataFrame(out)
    if len(df):
        df["ex_date"] = pd.to_datetime(df["ex_date"], format="%Y%m%d",
                                       errors="coerce")
    return df


def boards() -> pd.DataFrame:
    """板块全表（1,337 个：概念/申万一~三级 + 成分股 symbols）

    ⚠️ fsdb 只提供**最新快照**，无历史版本 → 历史回测直接用即前视偏差。
    正确用法：逐日快照入湖后按日期 ASOF 取用。
    """
    rows = http_get("/?cmd=vals&t=" + urllib.parse.quote("板块*")
                    + "&k1=key:600519&k2=fwz:19910101,20991231")
    if not isinstance(rows, list):
        raise RuntimeError(f"板块查询返回异常: {str(rows)[:60]}")
    return pd.DataFrame(rows)


def all_codes() -> list[str]:
    """fsdb 全部 A 股代码（含退市，按板块前缀）"""
    d = http_get("/?cmd=get&t=股票代码")
    if not isinstance(d, dict):
        raise RuntimeError(f"股票代码返回异常: {str(d)[:60]}")
    codes: list[str] = []
    for k in ("0", "3", "6"):
        codes.extend(d.get(k, []))
    # 北交所（43/83/87/88/92 开头，分布在不同首段）
    for k, v in d.items():
        if k not in ("0", "3", "6"):
            codes.extend([c for c in v
                          if c[:2] in ("43", "83", "87", "88", "92")])
    return sorted(set(codes))


def fund_codes() -> list[str]:
    """ETF/基金代码（组 1 深市 + 组 5 沪市），约 2,053 只"""
    d = http_get("/?cmd=get&t=股票代码")
    if not isinstance(d, dict):
        raise RuntimeError(f"股票代码返回异常: {str(d)[:60]}")
    out: list[str] = []
    for k in ("1", "5"):
        out.extend(d.get(k, []))
    return sorted(set(out))


def code_groups() -> dict:
    """证券宇宙分组（0/1/3/5/6/9），供对账与池定义"""
    d = http_get("/?cmd=get&t=股票代码")
    if not isinstance(d, dict):
        raise RuntimeError(f"股票代码返回异常: {str(d)[:60]}")
    return d


def data_freshness(sample_code: str = "600519") -> str | None:
    """抽样最新数据日期（YYYYMMDD 字符串）"""
    import datetime as _dt
    try:
        rows = day_bars(sample_code,
                        start=(_dt.date.today()
                               - _dt.timedelta(days=45)).strftime("%Y%m%d"))
        if len(rows):
            return str(int(rows["date"].max()))
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"data_freshness: {e}")
    return None
