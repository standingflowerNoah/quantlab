# -*- coding: utf-8 -*-
"""盘前信号快照查询：signal_portfolio_multi 每模型最新快照 + signal_portfolio 生产表最新快照"""
import duckdb, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

con = duckdb.connect("data/quant.duckdb", read_only=True)

MODEL_NOTES = {
    "PROD": "生产主模型 size+amihud_20 等权 top100",
    "EQ3": "候选 等权三因子",
    "PROD_HFA": "PROD 加 fundamental 因子层",
    "PROD_HFA_W3": "PROD_HFA 周三调仓版",
    "EQ3_HFA_ICW": "EQ3 稳健型候选（IC 加权）",
    "PROD_HF": "PROD 加高频因子层",
    "PROD_DUAL": "PROD 双因子变体",
    "PROD_SI": "PROD 稳健型变体",
    "V3_SI": "V3 稳健型变体",
    "DIV10": "红利池卫星仓（中证红利池内 size+amihud top10）",
}
# 缺省定位说明兜底
def note(m):
    return MODEL_NOTES.get(m, "候选/监控模型")

multi = con.execute("""
    WITH latest AS (
        SELECT model, MAX(date) AS d
        FROM signal_portfolio_multi
        GROUP BY model
    )
    SELECT m.model, m.date, m.code, m.name, m.weight
    FROM signal_portfolio_multi m
    JOIN latest l ON m.model = l.model AND m.date = l.d
    ORDER BY m.model, m.weight DESC
""").fetchall()

prod = con.execute("""
    WITH latest AS (SELECT MAX(date) AS d FROM signal_portfolio)
    SELECT p.date, p.code, p.name, p.weight
    FROM signal_portfolio p, latest l
    WHERE p.date = l.d
    ORDER BY p.weight DESC
""").fetchall()

con.close()

# 组装
from collections import defaultdict
by_model = defaultdict(list)
dates = defaultdict(str)
for model, d, code, name, weight in multi:
    by_model[model].append((name, code, weight))
    dates[model] = str(d)

# 主快照日 = 各模型快照日的众数
from collections import Counter
mode_date = Counter(dates.values()).most_common(1)[0][0]

lines = []
lines.append(f"📊 QuantLab 盘前模型信号简报（2026-09-11 · 数据修正版 · 快照日 {mode_date}）")
lines.append("")
lines.append("> 🔁 早间推送因通达信故障用的是 09-09 快照，本条为 09-10 数据补齐后的更正版，以此为准")
lines.append("")
for model in sorted(by_model.keys()):
    rows = by_model[model]
    n = len(rows)
    top5 = " · ".join(f"{name}({code}) {weight*100:.1f}%" for name, code, weight in rows[:5])
    d = dates[model]
    extra = f"（快照 {d}，周度模型非每日更新）" if d != mode_date else ""
    lines.append(f"> **{model}** · {note(model)} · 持仓 {n} 只{extra}")
    lines.append(f"> {top5}")
    lines.append("")

# 生产表快照
if prod:
    pdate = str(prod[0][0])
    ptop = " · ".join(f"{name}({code}) {w*100:.1f}%" for _, code, name, w in prod[:5])
    lines.append(f"> **signal_portfolio（生产账本）** · 快照 {pdate} · 持仓 {len(prod)} 只")
    lines.append(f"> {ptop}")
    lines.append("")

lines.append("⚠️ 前向纸面账本数据，非实盘建议")
brief = "\n".join(lines)

with open("reports/_morning_brief_body.md", "w", encoding="utf-8") as f:
    f.write(brief)

print("MODE_DATE:", mode_date)
print("MODELS:", json.dumps({m: (dates[m], len(by_model[m])) for m in by_model}, ensure_ascii=False))
print("PROD_SNAPSHOT:", prod[0][0] if prod else None, "rows:", len(prod))
print("BRIEF_BYTES:", len(brief.encode("utf-8")))
