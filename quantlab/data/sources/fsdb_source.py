"""free-stockdb 数据源客户端（本地引擎 + HTTP API）
=====================================
架构：
- tools/free-stockdb/win/stockdb/ 为发行包（stockdb.exe 引擎 + 数据更新.exe 同步器）
- 数据同步：镜像 → 数据更新.exe --sync → ./data（LevelDB，增量+断点续传）
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
- fsdb 的 volume 为股；本项目 kline_daily.vol（tdx）为手，入湖时换算
- amount 为元

工程要点：
- 同步前必须退出 stockdb.exe（LevelDB 单写者），同步后重启
- updater 为 GUI 程序无 stdout，且 --sync 完成后不退出（实测无增量日
  ~10s 完成工作后静默存活）→ 由 _watch_sync 空闲看门狗回收；镜像落
  data（主库 ~22GB）+ data1（~1.8GB）两个 LevelDB 目录，完成判定 = 无 .part 残留
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

from ... import config
from ...config import get_logger

log = get_logger(__name__)


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


def ensure_service() -> None:
    """确保服务在线（离线则拉起），失败抛异常"""
    if not service_alive() and not start_service():
        raise ConnectionError("stockdb 服务不可用 (127.0.0.1:7899)")


# ── 同步 ──────────────────────────────────────────────────────────
# 同步器会把镜像落到两个 LevelDB 目录：data（主库 ~22GB）+ data1（~1.8GB）
_SYNC_DB_DIRS = ("data", "data1")


def _part_files() -> list[str]:
    out: list[str] = []
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        out += [f for f in os.listdir(d) if f.endswith(".part")]
    return out


def _data_dir_size_mb() -> float:
    total = 0
    for sub in _SYNC_DB_DIRS:
        d = config.FSDB_DIR / sub
        if not d.exists():
            continue
        for _root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(_root, f))
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
                idle_s: float = 240.0) -> None:
    """等待同步器：整体超时 + 空闲看门狗（2026-09-06 实测教训）。

    updater 为 GUI 程序，--sync 完成后**不退出**（无增量日实测：~10s
    完成全部工作后静默存活，零 CPU/零网络/零写入，subprocess.run 会
    阻塞到超时）。看门狗：宽限期后，目录快照（文件数/总字节/.part 数）
    持续 idle_s 无变化 → 判定已完成或停滞，回收进程。两种情形均安全：
    ① 已完成：进程纯闲置，杀之无损；② 网络停滞（.part 不再增长）：
    .part 断点续传保证下次运行无损接力。
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
        elif now - max(last_move, t0 + grace_s) >= idle_s:
            idle = now - max(last_move, t0 + grace_s)
            log.warning(f"同步器空闲 {idle:.0f}s 无进展（已完成或网络停滞），"
                        f"回收进程；未完部分由 .part 断点续传")
            break
    try:
        proc.kill()
    except OSError:
        pass


def sync(timeout: int | None = None) -> dict:
    """运行镜像同步（增量，断点续传）。流程：停服务 → 同步（看门狗）→ 起服务

    返回 {"ok", "elapsed_s", "size_mb", "parts_left"}
    """
    t0 = time.time()
    timeout = timeout or config.FSDB_SYNC_TIMEOUT
    stop_service()
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
           "size_mb": round(_data_dir_size_mb(), 1),
           "parts_left": len(parts),
           "freshness": data_freshness()}
    if out["ok"]:
        log.info(f"同步完成: {out['elapsed_s']}s, 数据 {out['size_mb']}MB, "
                 f"最新 {out['freshness']}")
    else:
        log.warning(f"同步未完成（残留 {len(parts)} 个 .part），"
                    f"将在下次同步续传")
    return out


# ── HTTP 数据访问（生产路径）──────────────────────────────────────
def http_get(path: str, timeout: float = 120.0):
    """HTTP + msgpack 查询（每请求独立连接，规避 pyd 长连接串位 bug）"""
    import msgpack
    ensure_service()
    url = f"http://{config.FSDB_HOST}:{config.FSDB_PORT}{path}"
    z = requests.get(url, timeout=timeout)
    z.raise_for_status()
    return msgpack.unpackb(z.content, raw=False)


def minute_bars(code: str, start: str, end: str | None = None) -> pd.DataFrame:
    """单股分钟K（不复权）。start/end 为 8 位 YYYYMMDD。

    返回 DataFrame[date, code, open, high, low, close, volume, amount]
    （date 为 14 位 int；volume 单位=股；amount 单位=元）
    含响应身份校验：行内 code 与请求不符即抛异常（防串位数据入库）。
    """
    end = end or "20991231"
    rows = http_get(f"/?cmd=vals&t=分钟k&k1=key:{code}"
                    f"&k2=fwz:{start}000000,{end}235959")
    if not isinstance(rows, list):
        raise RuntimeError(f"{code} 分钟查询返回异常: {str(rows)[:60]}")
    if rows and not isinstance(rows[0], dict):
        raise RuntimeError(f"{code} 分钟查询返回非 dict: {str(rows[0])[:60]}")
    if rows:
        bad = [r for r in rows if r.get("code") != code]
        if bad:
            raise RuntimeError(f"{code} 响应身份校验失败"
                               f"（{len(bad)}/{len(rows)} 行 code 不符）")
    return pd.DataFrame(rows)


def day_bars(code: str, start: str, end: str | None = None) -> pd.DataFrame:
    """单股日K（不复权，交叉验证用）"""
    end = end or "20991231"
    rows = http_get(f"/?cmd=vals&t=日k&k1=key:{code}&k2=fwz:{start},{end}")
    if not isinstance(rows, list):
        raise RuntimeError(f"{code} 日k查询返回异常: {str(rows)[:60]}")
    if rows:
        bad = [r for r in rows if r.get("code") != code]
        if bad:
            raise RuntimeError(f"{code} 日k响应身份校验失败")
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


def data_freshness(sample_code: str = "600519") -> str | None:
    """抽样最新数据日期（YYYYMMDD 字符串）"""
    import datetime as _dt
    try:
        rows = day_bars(sample_code,
                        start=(_dt.date.today()
                               - _dt.timedelta(days=45)).strftime("%Y%m%d"))
        if len(rows):
            return str(int(rows["date"].max()))
    except Exception as e:
        log.debug(f"data_freshness: {e}")
    return None
