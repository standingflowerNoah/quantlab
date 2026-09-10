"""QuantLab 只读研究服务（阶段 2 服务化 · 单文件 FastAPI 应用）
=====================================================
定位：给合作者提供浏览器/HTTP 只读入口——查询数据湖、看因子清单、看报告。
设计原则：
- 只读：任何端点都不写库；SQL 端点仅放行 SELECT/WITH，单语句
- 复用 store.query() 的三层降级（本进程连接 → 只读直连 → Parquet 镜像），
  与每日流水线写锁共存，永不阻塞
- 鉴权：Bearer Token（环境变量 QUANTLAB_API_TOKEN，或启动参数 --token）；
  /api/health 免鉴权（探活用）
- 静态报告：/reports/* 直接挂 reports/ 目录

启动：
  $PY scripts/api_server.py --port 8000 --token <TOKEN>
  或设 QUANTLAB_API_TOKEN 后 $PY scripts/api_server.py

测试：
  curl http://127.0.0.1:8000/api/health
  curl -H "Authorization: Bearer <TOKEN>" "http://127.0.0.1:8000/api/universe"
  curl -H "Authorization: Bearer <TOKEN>" -X POST http://127.0.0.1:8000/api/query \
       -H "Content-Type: application/json" -d '{"sql":"SELECT COUNT(*) n FROM kline_daily"}'
  curl -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/api/factors
  浏览器: http://127.0.0.1:8000/reports/overview_report.html
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # quantlab 项目根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from quantlab.data.store import query

app = FastAPI(title="QuantLab Research API", version="0.1.0")
API_TOKEN = ""


def _check_token(authorization: str) -> None:
    """校验 Bearer Token；未配置 token 时拒绝所有请求（fail-closed）。"""
    if not API_TOKEN:
        raise HTTPException(503, "服务未配置 QUANTLAB_API_TOKEN，拒绝访问")
    m = re.match(r"^Bearer\s+(.+)$", authorization or "")
    if not m or m.group(1).strip() != API_TOKEN:
        raise HTTPException(401, "无效 Token")


# ---------- 探活（免鉴权） ----------
@app.get("/api/health")
def health():
    out: dict = {"status": "ok"}
    try:
        df = query("SELECT COUNT(*) n, MAX(date) d FROM kline_daily")
        r = df.iloc[0]
        out["kline_daily"] = {"rows": int(r["n"]), "latest": str(r["d"])}
    except Exception as e:                                   # noqa: BLE001
        out["kline_daily"] = {"error": str(e)[:200]}
    # 因子湖水位（直扫目录，不碰 DuckDB）
    try:
        fdir = ROOT / "data/lake/factor"
        out["factor_domains"] = sum(1 for p in fdir.iterdir() if p.is_dir()) if fdir.is_dir() else 0
    except Exception:                                        # noqa: BLE001
        out["factor_domains"] = None
    return out


# ---------- 股票池 ----------
@app.get("/api/universe")
def universe(authorization: str = Header(default="")):
    _check_token(authorization)
    from quantlab.data.universe import describe_universes
    return JSONResponse({"universes": describe_universes().to_dict(orient="records")})


# ---------- SQL 查询（只读白名单） ----------
class SqlReq(BaseModel):
    sql: str


_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@app.post("/api/query")
def run_query(req: SqlReq, authorization: str = Header(default="")):
    _check_token(authorization)
    sql = req.sql.strip().rstrip(";").strip()
    if not _SELECT_RE.match(sql):
        raise HTTPException(400, "仅允许单条 SELECT/WITH 查询")
    if ";" in sql:
        raise HTTPException(400, "仅允许单条语句")
    if re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ATTACH|COPY|EXPORT|INSTALL|LOAD)\b",
                 sql, re.IGNORECASE):
        raise HTTPException(400, "包含被禁止的关键字")
    try:
        df = query(sql)          # 三层降级：写锁被占时自动落 Parquet 镜像
    except Exception as e:       # noqa: BLE001
        raise HTTPException(400, f"查询失败: {e}") from e
    # pandas Timestamp/np.int64 等类型不能直接 json.dumps，经 to_json 中转
    import json as _json
    records = _json.loads(df.to_json(orient="records", date_format="iso", force_ascii=False))
    return JSONResponse({"rows": len(records), "data": records})


# ---------- 因子清单 ----------
@app.get("/api/factors")
def factors(authorization: str = Header(default="")):
    _check_token(authorization)
    from quantlab.factor.registry import all_factors
    out = []
    for name, f in sorted(all_factors().items()):
        out.append({
            "name": name,
            "category": getattr(f, "category", None),
            "description": getattr(f, "description", None),
        })
    return JSONResponse({"count": len(out), "factors": out})


# ---------- 静态报告（只读挂载） ----------
REPORTS_DIR = ROOT / "reports"
if REPORTS_DIR.is_dir():
    app.mount("/reports", StaticFiles(directory=str(REPORTS_DIR), html=True), name="reports")


def main():
    global API_TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default="", help="API Token；优先取环境变量 QUANTLAB_API_TOKEN")
    args = ap.parse_args()
    API_TOKEN = os.environ.get("QUANTLAB_API_TOKEN") or args.token
    if not API_TOKEN:
        print("[WARN] 未设置 QUANTLAB_API_TOKEN / --token，所有业务端点将返回 503", file=sys.stderr)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
