"""因子相关性矩阵（L2 因子层研究工具）
=====================================
计算因子之间的截面秩相关（Spearman），用于识别冗余因子：
高相关的因子在等权合成中会重复计入，稀释各自 alpha。

方法：抽样若干交易日，每日算因子间截面 rank 相关，取时间均值。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)


def factor_correlation(names: list[str], sample_dates: int = 60,
                       start=None, end=None) -> pd.DataFrame:
    """因子截面秩相关矩阵（时间平均）

    返回 DataFrame，行/列为因子名，值为平均截面 rank 相关（-1~1）
    """
    store = Store()
    frames = {}
    for name in names:
        fv = store.read_factor(name)
        if fv.empty:
            log.warning(f"因子 {name} 无数据，跳过")
            continue
        fv["date"] = pd.to_datetime(fv["date"])
        fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
        fv = fv[np.isfinite(fv["value"])]
        frames[name] = fv

    if len(frames) < 2:
        return pd.DataFrame()

    # 公共日期（各因子都有的日期），抽样 sample_dates 个
    common = None
    for fv in frames.values():
        d = set(fv["date"].unique())
        common = d if common is None else common & d
    dates = sorted(common)
    if len(dates) > sample_dates:
        idx = np.linspace(0, len(dates) - 1, sample_dates).astype(int)
        dates = [dates[i] for i in idx]

    name_list = list(frames.keys())
    n = len(name_list)
    corr_sum = np.zeros((n, n))
    corr_cnt = np.zeros((n, n))
    valid_names = set()

    for d in dates:
        ts = pd.Timestamp(d)
        # 每因子当日截面
        rank_cols = {}
        for name in name_list:
            g = frames[name][frames[name]["date"] == ts]
            if len(g) < 30:
                continue
            rank_cols[name] = g.set_index("code")["value"].rank()
        if len(rank_cols) < 2:
            continue
        # 交集股票
        common_codes = None
        for s in rank_cols.values():
            c = set(s.index)
            common_codes = c if common_codes is None else common_codes & c
        common_codes = sorted(common_codes)
        if len(common_codes) < 30:
            continue
        mat = np.column_stack([rank_cols[name][common_codes].values
                               for name in rank_cols])
        valid_names.update(rank_cols.keys())
        cm = np.corrcoef(mat.T)
        # 累加（只累加本次参与的名字）
        keys = list(rank_cols.keys())
        for i, ki in enumerate(keys):
            for j, kj in enumerate(keys):
                ii, jj = name_list.index(ki), name_list.index(kj)
                corr_sum[ii, jj] += cm[i, j]
                corr_cnt[ii, jj] += 1

    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(corr_cnt > 0, corr_sum / np.maximum(corr_cnt, 1), np.nan)
    return pd.DataFrame(corr, index=name_list, columns=name_list).round(3)
