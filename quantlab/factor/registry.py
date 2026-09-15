"""因子注册表：@register 装饰器收集所有因子"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from .base import Factor

log = get_logger(__name__)

_FACTORS: dict[str, Factor] = {}
# 重名登记簿：name → [实现类全名, ...]，供 /故障排查时定位静默覆盖
_SHADOWED: dict[str, list[str]] = {}


def register(cls):
    """类装饰器：实例化并注册因子

    ⚠️ 同名覆盖防护（2026-09-11）：历史上 `ep`/`bp` 被 `fundamental.Ep`/`Bp`
    覆盖了 `valuation.EP`/`BP`，后者沦为死代码——因子实际口径与预期不符，
    且 audit 记录挂在被覆盖的实现上（陈旧）。改名/挪模块时极易踩。
    此处显式告警并登记，不再静默覆盖。
    """
    obj = cls()
    if not obj.name:
        raise ValueError(f"{cls.__name__} 缺少 name")
    key = obj.name.lower()
    new_impl = f"{cls.__module__.split('.')[-1]}.{cls.__name__}"
    if key in _FACTORS:
        old_impl = f"{type(_FACTORS[key]).__module__.split('.')[-1]}." \
                   f"{type(_FACTORS[key]).__name__}"
        _SHADOWED.setdefault(key, [old_impl]).append(new_impl)
        log.warning(f"[registry] 因子名重复 '{obj.name}'：{old_impl} 被 {new_impl} "
                    f"覆盖（生效的是后者；前者为死代码）")
    _FACTORS[key] = obj
    return cls


def shadowed() -> dict[str, list[str]]:
    """被同名覆盖的因子清单（应为空；非空说明存在死代码/口径错配）"""
    return {k: v for k, v in _SHADOWED.items()}


def get_factor(name: str) -> Factor:
    """按名取因子（大小写不敏感）"""
    key = name.lower()
    if key not in _FACTORS:
        raise KeyError(f"未知因子: {name}（可用: {', '.join(sorted(_FACTORS))}）")
    return _FACTORS[key]


def all_factors() -> dict[str, Factor]:
    return dict(_FACTORS)


def list_factors() -> pd.DataFrame:
    """因子清单：名称/分类/说明/频率/经济机制登记"""
    rows = [{"name": f.name, "category": f.category,
             "freq": f.freq, "description": f.description,
             "economic_rationale": getattr(f, "economic_rationale", "") or "未登记"}
            for f in _FACTORS.values()]
    return pd.DataFrame(rows).sort_values("name").reset_index(drop=True)


# 导入内置因子库，触发注册
from . import library  # noqa: E402,F401
