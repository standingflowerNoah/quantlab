#!/usr/bin/env python3
"""hf 历史回填总编排器（脱离会话运行，nohup 派发）
=====================================================
阶段：
  0. 等待 2022 分钟 K 回填完成（轮询 xd_supervisor.log 的「全部完成」标记）
  1. minute_feat 分块构建 2022/2023/2024（按月切块，显式 memory_limit）
  2. hf_* 因子追加 2022-2024 历史年份
  3. 逐因子 deep 重审计（IC 覆盖到全历史）
  4. 逐因子 run_eval.py 评估（2022-01-01 ~ 2026-09-11）

用法：
    nohup python scripts/run_hf_hist_pipeline.py > logs/hf_hist_pipeline.log 2>&1 &
    tail -f logs/hf_hist_pipeline.log
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = logging.getLogger("hf_pipeline")

SUPERVISOR_LOG = ROOT / "logs" / "xd_supervisor.log"
RESULT_JSON = ROOT / "reports" / "_tmp" / "hf_hist_pipeline_result.json"
YEARS = [2022, 2023, 2024]
START, END, EVAL_END = "2022-01-01", "2024-12-31", "2026-09-11"


def setup_log() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(ROOT / "logs" / "hf_hist_pipeline.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    LOG.setLevel(logging.INFO)


def wait_kline_done(timeout_h: float = 14.0) -> bool:
    """轮询监督器日志直到出现「全部完成」。"""
    t0 = time.time()
    LOG.info("[阶段0] 等待 2022 分钟 K 回填完成（每 5 分钟轮询一次，上限 %.0f h）", timeout_h)
    marker = "全部完成"
    while time.time() - t0 < timeout_h * 3600:
        try:
            txt = SUPERVISOR_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        if marker in txt:
            LOG.info("[阶段0] 监督器已报告完成")
            return True
        try:
            import duckdb
            con = duckdb.connect()
            n, c = con.execute(
                "SELECT count(*), count(DISTINCT code) FROM read_parquet("
                "'data/lake/clean/kline_1min/year=2022/*.parquet')").fetchone()
            con.close()
            LOG.info("[阶段0] 2022 当前 %s 行 / %s 只（%.1f%%）",
                     f"{n:,}", c, n / 272_776_091 * 100)
        except Exception as e:  # noqa: BLE001
            LOG.info("[阶段0] 探测失败: %s", str(e)[:120])
        time.sleep(300)
    LOG.error("[阶段0] 等待超时（%.0f h）", timeout_h)
    return False


def phase_minute_feat() -> dict:
    from scripts.backfill_minute_feat_hist import build_year_chunked
    import duckdb
    from quantlab import config
    out = {}
    tmp = config.CLEAN_DIR / ".duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    # 4GB：机器 33GB，另一会话同时在跑重活（占 8GB+），留足余量防 OOM
    con = duckdb.connect()
    try:
        for y in YEARS:
            LOG.info("[阶段1] minute_feat %s（按月分块）", y)
            out[y] = build_year_chunked(con, y, "4GB", tmp)
    finally:
        con.close()
    return out


def _with_lock_retry(fn, tries: int = 12, wait_s: int = 300):
    """DuckDB 单写者：与每日流水线冲突时等待重试。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "lock" in msg.lower() or "Conflicting" in msg or "正在写入" in msg:
                LOG.warning("DuckDB 写锁被占（第 %d/%d 次），%ds 后重试: %s",
                            i + 1, tries, wait_s, msg[:120])
                time.sleep(wait_s)
                last = e
                continue
            raise
    raise last  # type: ignore[misc]


def phase_factors(names: list[str]) -> list[dict]:
    from quantlab.factor import compute_factor
    res = []
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            df = _with_lock_retry(lambda: compute_factor(name, start=START, end=END, save=True))
            res.append({"factor": name, "phase": "compute", "rows": int(len(df)),
                        "sec": round(time.time() - t0, 1)})
            LOG.info("[阶段2 %d/%d] %s: 追加 %s 行（%ss）",
                     i, len(names), name, f"{len(df):,}", round(time.time() - t0))
        except Exception as e:  # noqa: BLE001
            LOG.error("[阶段2 %d/%d] %s: 失败 %s", i, len(names), name, str(e)[:200])
            res.append({"factor": name, "phase": "compute", "error": str(e)[:300]})
        import gc
        gc.collect()
    return res


def phase_audit(names: list[str]) -> list[dict]:
    from quantlab.factor.audit import audit_factor
    res = []
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            rec = _with_lock_retry(lambda: audit_factor(name, deep=True, force=True))
            st = rec.get("checks", {}).get("structure", {})
            ic = rec.get("checks", {}).get("ic", {})
            res.append({"factor": name, "phase": "audit", "verdict": rec.get("verdict"),
                        "date_min": st.get("date_min"), "date_max": st.get("date_max"),
                        "rank_ic5": ic.get("rank_ic5"), "rank_ic20": ic.get("rank_ic20")})
            LOG.info("[阶段3 %d/%d] %s: %s  %s~%s  rank_ic5=%s (%ss)",
                     i, len(names), name, rec.get("verdict"), st.get("date_min"),
                     st.get("date_max"), ic.get("rank_ic5"), round(time.time() - t0))
        except Exception as e:  # noqa: BLE001
            LOG.error("[阶段3 %d/%d] %s: 审计失败 %s", i, len(names), name, str(e)[:200])
            res.append({"factor": name, "phase": "audit", "error": str(e)[:300]})
    return res


def phase_eval(names: list[str], tags: str) -> list[dict]:
    py = sys.executable
    script = ROOT / "scripts" / "factor_eval" / "run_eval.py"
    res = []
    for i, name in enumerate(names, 1):
        cmd = [py, str(script), "--type", "factor", "--name", name,
               "--start", START, "--end", EVAL_END, "--tags", tags]
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(ROOT))
        # 报告归档路径
        md = ""
        for line in (r.stdout or "").splitlines():
            if ".md" in line and ("报告" in line or "report" in line or "存档" in line
                                  or "/reports/factor_eval/" in line):
                md = line.strip()[-160:]
        LOG.info("[阶段4 %d/%d] %s: rc=%s %ss  %s",
                 i, len(names), name, r.returncode, round(time.time() - t0), md or
                 ((r.stderr or "")[-160:]))
        res.append({"factor": name, "phase": "eval", "rc": r.returncode, "report": md})
    return res


def main() -> None:
    setup_log()
    LOG.info("=" * 70)
    LOG.info("hf 历史回填总编排启动")
    result: dict = {"started": time.strftime("%F %T")}

    if not wait_kline_done():
        result["error"] = "2022 分钟 K 回填未在时限内完成"
        RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        return

    # 发现 hf 因子清单
    names = sorted(p.name for p in (ROOT / "data" / "lake" / "factor").glob("hf_*")
                   if p.is_dir())
    LOG.info("hf 因子 %d 个: %s", len(names), names)
    result["factors"] = names

    LOG.info("=" * 70)
    t0 = time.time()
    result["minute_feat"] = phase_minute_feat()
    LOG.info("[阶段1] minute_feat 完成 %s（%s）", result["minute_feat"], round(time.time() - t0))

    t0 = time.time()
    result["compute"] = phase_factors(names)
    result["audit"] = phase_audit(names)
    result["eval"] = phase_eval(names, "hf,hist-backfill")
    LOG.info("[阶段2-4] 因子回填+审计+评估完成（%ss）", round(time.time() - t0))

    result["finished"] = time.strftime("%F %T")
    RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    LOG.info("=" * 70)
    LOG.info("全部完成，结果写入 %s", RESULT_JSON)


if __name__ == "__main__":
    main()
