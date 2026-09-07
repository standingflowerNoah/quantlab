"""因子注册表：@register 装饰器收集所有因子"""
from __future__ import annotations

import pandas as pd

from .base import Factor

_FACTORS: dict[str, Factor] = {}


def register(cls):
    """类装饰器：实例化并注册因子"""
    obj = cls()
    if not obj.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    _FACTORS[obj.name] = obj
    return cls


def get_factor(name: str) -> Factor:
    """按名取因子（大小写不敏感）"""
    key = name.lower()
    if key not in _FACTORS:
        raise KeyError(f"未知因子: {name}（可用: {', '.join(sorted(_FACTORS))}）")
    return _FACTORS[key]


def all_factors() -> dict[str, Factor]:
    return dict(_FACTORS)


def list_factors() -> pd.DataFrame:
    """因子清单：名称/分类/说明/频率"""
    rows = [{"name": f.name, "category": f.category,
             "freq": f.freq, "description": f.description}
            for f in _FACTORS.values()]
    return pd.DataFrame(rows).sort_values("name").reset_index(drop=True)


# 导入内置因子库，触发注册
from . import library  # noqa: E402,F401
