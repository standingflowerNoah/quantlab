"""因子计算入口：统一计算 + 写入 Parquet 因子库"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from .registry import get_factor
from ..data.store import Store

log = get_logger(__name__)


def _resolve_universe(universe) -> list[str] | None:
    """universe 可为 None(全A) / 股票池名(str) / 代码列表(list)"""
    if universe is None:
        return None
    if isinstance(universe, str):
        from ..data.universe import get_universe
        return get_universe(universe)
    return list(universe)


def compute_factor(name: str, start=None, end=None,
                   universe=None, save: bool = True) -> pd.DataFrame:
    """计算因子并（默认）写入 Parquet 因子库

    参数:
        name: 因子名（见 factor list）
        start/end: 输出日期范围（YYYY-MM-DD 或 Timestamp；窗口计算仍用全历史）
        universe: None=全A / 'ashare_ex' 等池名 / 代码列表
        save: 是否写入 data/lake/factor/<name>/part-<year>.parquet
    返回: date/code/value 长格式
    """
    factor = get_factor(name)
    store = Store()
    codes = _resolve_universe(universe)

    df = factor.compute(store, start=None, end=None, universe=codes)
    if df.empty:
        log.warning(f"因子 {name} 计算结果为空")
        return df

    if start is not None:
        df = df[df["date"] >= pd.to_datetime(start)]
    if end is not None:
        df = df[df["date"] <= pd.to_datetime(end)]
    df = df.sort_values(["date", "code"]).reset_index(drop=True)

    if save:
        n = store.append_factor(factor.name, df)
        log.info(f"因子 {name} 已写入 {n} 行（{df['date'].min().date()} ~ "
                 f"{df['date'].max().date()}，{df['code'].nunique()} 只）")
        # 构建即审查：首次 deep 全审（含 PIT 穿越自检），数据增量 → 轻量复审
        from .audit import maybe_audit
        maybe_audit(factor, df)
    return df


def compute_all(universe=None, save: bool = True) -> dict[str, pd.DataFrame]:
    """批量计算全部注册因子"""
    from .registry import all_factors
    out = {}
    for name in sorted(all_factors()):
        try:
            out[name] = compute_factor(name, universe=universe, save=save)
        except Exception as e:
            log.error(f"因子 {name} 计算失败: {e}")
    return out
