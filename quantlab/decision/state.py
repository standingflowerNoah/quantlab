"""持仓状态持久化（L5 决策层）
=====================================
当前持仓存 portfolio_state/current.json，调仓历史存 state_history.json。
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from ..config import PORTFOLIO_STATE_DIR

CURRENT_FILE = PORTFOLIO_STATE_DIR / "current.json"
HISTORY_FILE = PORTFOLIO_STATE_DIR / "state_history.json"


def load_state() -> dict:
    """读取当前持仓 {code: weight}"""
    if not CURRENT_FILE.exists():
        return {}
    data = json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
    return data.get("holdings", {})


def save_state(holdings: pd.DataFrame, date=None):
    """保存当前持仓（holdings: code/name/weight）"""
    if date is None:
        date = datetime.now().date().isoformat()
    elif hasattr(date, "isoformat"):
        date = date.isoformat()
    else:
        date = str(date)
    h = {r["code"]: float(r["weight"]) for _, r in holdings.iterrows()}
    payload = {"date": date, "holdings": h, "n": len(h)}
    CURRENT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    # 追加历史
    hist = []
    if HISTORY_FILE.exists():
        hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    hist.append(payload)
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return payload
