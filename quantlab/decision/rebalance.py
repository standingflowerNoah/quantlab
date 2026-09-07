"""调仓指令生成（L5 决策层）
=====================================
对比当前持仓与目标持仓，生成买卖指令（含加仓/减仓/清仓/建仓）。
"""
from __future__ import annotations

import pandas as pd


def diff_orders(current, target: pd.DataFrame,
                min_delta: float = 0.005) -> pd.DataFrame:
    """对比当前 vs 目标持仓，生成调仓指令

    参数:
        current: dict{code: weight} 或 DataFrame(code/weight)
        target: DataFrame(code/name/weight)
        min_delta: 权重差异阈值，低于此不触发调仓
    返回: DataFrame(code/name/action/cur_weight/target_weight/delta)
    """
    if isinstance(current, pd.DataFrame):
        cur = dict(zip(current["code"], current["weight"]))
    else:
        cur = dict(current or {})
    tgt = target.set_index("code")["weight"].to_dict()
    names = target.set_index("code")["name"].to_dict()

    all_codes = set(cur) | set(tgt)
    orders = []
    for code in all_codes:
        cw = float(cur.get(code, 0.0))
        tw = float(tgt.get(code, 0.0))
        delta = tw - cw
        if abs(delta) < min_delta:
            continue
        action = "买入" if delta > 0 else "卖出"
        orders.append({
            "code": code,
            "name": names.get(code, ""),
            "action": action,
            "cur_weight": round(cw, 4),
            "target_weight": round(tw, 4),
            "delta": round(delta, 4),
        })
    if not orders:
        return pd.DataFrame(columns=["code", "name", "action",
                                     "cur_weight", "target_weight", "delta"])
    df = pd.DataFrame(orders)
    # 卖出在前，买入在后；同向按 |delta| 降序
    df["_abs"] = df["delta"].abs()
    df = df.sort_values(["action", "_abs"],
                        ascending=[True, False]).drop(columns="_abs")
    return df.reset_index(drop=True)
