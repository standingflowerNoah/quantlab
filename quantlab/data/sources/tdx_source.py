"""通达信 pytdx 直连数据源（服务器池 + failover + 断线重连）
=====================================
教训记录（2026-09-02）：
- mootdx 的 bestip 会选中"能连接但无数据"的半死节点 → 弃用 mootdx 封装
- 腾讯 ifzq.gtimg.cn K线域名被 WAF 封 IP；qt.gtimg.cn 快照域名正常
- pytdx 直连指定服务器稳定：218.75.126.9 / 115.238.56.198 / 115.238.90.165

能力：日/周/月K(个股+指数)、除权除息、财务快照37字段、全品种列表、批量实时行情
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from ... import config
from ...config import get_logger

log = get_logger(__name__)

# ── 服务器池（已验证可用，按优先级排列） ──────────────────────────
TDX_SERVERS = [
    ("218.75.126.9", 7709),      # 主力（2026-09-02 验证）
    ("115.238.56.198", 7709),    # 杭州备用
    ("115.238.90.165", 7709),    # 杭州备用
]

# 通达信 K 线 category（协议原生编号）
CATEGORY = {"day": 4, "week": 5, "month": 6,
            "min1": 8, "min5": 0, "min15": 1, "min30": 2, "min60": 3}
# 注：pytdx 协议 0=5分钟 1=15分钟 2=30分钟 3=1小时 4=日 5=周 6=月 7=1分钟 8=1分钟
CATEGORY["min5"] = 0
CATEGORY["min1"] = 8

# A 股代码前缀
_A_SH = ("600", "601", "603", "605", "688", "689")
_A_SZ = ("000", "001", "002", "003", "300", "301")
_A_BJ = ("43", "83", "87", "88", "92")


def is_a_share(code: str) -> bool:
    return code.startswith(_A_SH + _A_SZ + _A_BJ)


def market_of(code: str) -> int:
    """0=深圳 1=上海 2=北交所
    注：北交所 2025 年起统一改码 920 段（通达信 market=2）；
    43/83/87 老段部分已改码 920、部分退市，在通达信 market=0 可拉到存量数据"""
    if code.startswith("92"):
        return 2
    return 1 if code.startswith(("6", "9")) else 0


def _bars_to_df(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["datetime"] = df["datetime"].astype(str)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["datetime"].str[:10]),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "vol": df["vol"].astype(float),          # 股（2026-09-06 对账实测：
                                                # amount/均价 = vol，非手；
                                                # 与 fsdb 分钟求和吻合）
        "amount": df["amount"].astype(float),    # 元
    })
    return out.sort_values("date").drop_duplicates(
        subset=["date"], keep="last").reset_index(drop=True)


class TdxClient:
    """pytdx 连接管理：服务器池轮询 + 空结果自动重连重试（线程安全）"""

    _inst = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> "TdxClient":
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    def __init__(self):
        from pytdx.hq import TdxHq_API
        self._api_cls = TdxHq_API
        self._api = None
        self._server_idx = 0
        self._last_ok = 0.0
        self._connect()

    # ── 连接管理 ──────────────────────────────────────────────────
    def _connect(self):
        """按服务器池顺序尝试连接（健康检查：拉 10 根日K）"""
        from pytdx.hq import TdxHq_API
        last_err = None
        for i in range(len(TDX_SERVERS)):
            idx = (self._server_idx + i) % len(TDX_SERVERS)
            host, port = TDX_SERVERS[idx]
            api = self._api_cls()
            try:
                if api.connect(host, port, time_out=8):
                    probe = api.get_security_bars(4, 1, "600519", 0, 5)
                    if probe:
                        self._api = api
                        self._server_idx = idx
                        self._last_ok = time.time()
                        log.debug(f"tdx 连接 {host}:{port}")
                        return True
                api.disconnect()
            except Exception as e:
                last_err = e
                try:
                    api.disconnect()
                except Exception:
                    pass
        raise ConnectionError(f"通达信服务器池全部不可用: {last_err}")

    def _reconnect(self):
        self._server_idx = (self._server_idx + 1) % len(TDX_SERVERS)
        try:
            if self._api:
                self._api.disconnect()
        except Exception:
            pass
        self._api = None
        self._connect()

    def _call(self, method: str, *args, retries: int | None = None):
        """带重连的调用：None/空结果 → 换服务器重试"""
        from ...config import TDX_RETRIES
        if retries is None:
            retries = TDX_RETRIES
        last = None
        for attempt in range(retries):
            try:
                if self._api is None:
                    self._connect()
                fn = getattr(self._api, method)
                result = fn(*args)
                if result:                      # 非空即成功
                    self._last_ok = time.time()
                    return result
                # 空结果：先做连接健康检查，连接坏了才重连重试。
                # 注意：财务/除权接口对本就没有数据的股票也会返回空，
                # 但此前对该类接口豁免重连，导致长任务中连接静默失效后
                # 全批返回空（财务快照整批为 0 行的根因）。
                if self._healthy():             # 连接正常 → 确实无数据
                    return result
                self._reconnect()
                continue
            except Exception as e:
                last = e
                log.debug(f"tdx {method} 异常(第{attempt+1}次): {e}")
                try:
                    self._reconnect()
                except ConnectionError:
                    time.sleep(1)
        if method in ("get_xdxr_info", "get_finance_info", "get_security_list"):
            return []                           # 这些接口空也可能正常（无数据）
        raise ConnectionError(f"tdx {method} 重试{retries}次失败: {last}")

    def _healthy(self) -> bool:
        """连接健康检查（用已知有数据的股票探测，判断是否静默断连）"""
        try:
            if self._api is None:
                return False
            return bool(self._api.get_security_bars(4, 1, "600519", 0, 5))
        except Exception:
            return False

    # ── 对外接口 ──────────────────────────────────────────────────
    def bars(self, code: str, freq: str = "day", start: int = 0,
             count: int = 800) -> pd.DataFrame:
        rows = self._call("get_security_bars", CATEGORY[freq],
                          market_of(code), str(code), int(start), int(count))
        return _bars_to_df(rows)

    def index_bars(self, code: str, market: int, freq: str = "day",
                   start: int = 0, count: int = 800) -> pd.DataFrame:
        rows = self._call("get_index_bars", CATEGORY[freq], market,
                          str(code), int(start), int(count))
        return _bars_to_df(rows)

    def bars_history(self, code: str, freq: str = "day",
                     start_date: str | None = None,
                     max_rows: int = 12000) -> pd.DataFrame:
        """分页拉全量历史（start 为从最新往回的偏移）"""
        chunks, cursor = [], 0
        while cursor < max_rows:
            df = self.bars(code, freq, start=cursor, count=800)
            if df.empty:
                break
            chunks.append(df)
            if start_date and df["date"].iloc[0] <= pd.to_datetime(start_date):
                break
            if len(df) < 800:
                break
            cursor += len(df)
        if not chunks:
            return pd.DataFrame()
        out = pd.concat(chunks, ignore_index=True)
        out = (out.drop_duplicates(subset=["date"], keep="last")
               .sort_values("date").reset_index(drop=True))
        if start_date:
            out = out[out["date"] >= pd.to_datetime(start_date)]
        return out

    def index_bars_history(self, code: str, market: int, freq: str = "day",
                           start_date: str | None = None) -> pd.DataFrame:
        chunks, cursor = [], 0
        while cursor < 40000:
            df = self.index_bars(code, market, freq, start=cursor, count=800)
            if df.empty:
                break
            chunks.append(df)
            if start_date and df["date"].iloc[0] <= pd.to_datetime(start_date):
                break
            if len(df) < 800:
                break
            cursor += len(df)
        if not chunks:
            return pd.DataFrame()
        out = pd.concat(chunks, ignore_index=True)
        out = (out.drop_duplicates(subset=["date"], keep="last")
               .sort_values("date").reset_index(drop=True))
        if start_date:
            out = out[out["date"] >= pd.to_datetime(start_date)]
        return out

    def xdxr_events(self, code: str) -> pd.DataFrame:
        """除权除息事件（category=1），每股口径"""
        rows = self._call("get_xdxr_info", market_of(code), str(code))
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df[df["category"] == 1]
        if df.empty:
            return pd.DataFrame()
        out = pd.DataFrame({
            "date": pd.to_datetime(dict(year=df["year"], month=df["month"],
                                        day=df["day"])),
            "fenhong": pd.to_numeric(df["fenhong"], errors="coerce").fillna(0) / 10,
            "songzhuangu": pd.to_numeric(df["songzhuangu"], errors="coerce").fillna(0) / 10,
            "peigu": pd.to_numeric(df["peigu"], errors="coerce").fillna(0) / 10,
            "peigujia": pd.to_numeric(df["peigujia"], errors="coerce").fillna(0),
        })
        return out.sort_values("date").reset_index(drop=True)

    def finance_info(self, code: str) -> dict:
        rows = self._call("get_finance_info", market_of(code), str(code))
        if not rows:
            return {}
        # pytdx 返回单个 OrderedDict（部分版本返回列表），两种都兼容
        return dict(rows[0] if isinstance(rows, (list, tuple)) else rows)

    def stock_list(self) -> pd.DataFrame:
        """全品种列表（沪深两市）"""
        frames = []
        for mkt in (0, 1):
            cnt = self._call("get_security_count", mkt) or 0
            for start in range(0, int(cnt) + 1000, 1000):
                rows = self._call("get_security_list", mkt, start)
                if not rows:
                    break
                frames.append(pd.DataFrame(rows)[["code", "name"]])
                if len(rows) < 1000:
                    break
        if not frames:
            return pd.DataFrame(columns=["code", "name"])
        return (pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset=["code"]))

    def quotes(self, codes: list[str]) -> pd.DataFrame:
        """批量实时五档行情（≤50/批，备用源）"""
        pairs = [(market_of(c), c) for c in codes]
        rows = []
        for i in range(0, len(pairs), 50):
            batch = pairs[i:i + 50]
            r = self._call("get_security_quotes", batch)
            if r:
                rows.extend(r)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    def close(self):
        try:
            if self._api:
                self._api.disconnect()
        except Exception:
            pass
        TdxClient._inst = None
