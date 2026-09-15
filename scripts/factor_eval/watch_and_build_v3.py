#!/usr/bin/env python
"""v3 全库重建看门狗：等主库写锁释放后自动启动批量

启动条件（连续两轮检查通过，间隔 60s）：
  1) 无 daily_pipeline.py 进程存活
  2) 主库 duckdb readonly 连接成功

满足后前台启动 build_all_center_v3.py（输出直通本进程 stdout）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "quant.duckdb"


def pipeline_alive() -> bool:
    try:
        r = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "CommandLine"],
            capture_output=True, text=True, errors="replace", timeout=60)
        return "daily_pipeline.py" in (r.stdout or "")
    except Exception:
        return True  # 查不到时保守视作存活


def db_free() -> bool:
    try:
        con = duckdb.connect(str(DB), read_only=True)
        con.execute("SELECT 1").fetchone()
        con.close()
        return True
    except Exception:
        return False


def main() -> None:
    deadline = time.time() + 8 * 3600  # 最多等 8h 防挂死
    passed = 0
    while time.time() < deadline:
        ok = (not pipeline_alive()) and db_free()
        passed = passed + 1 if ok else 0
        print(f"[watch {datetime.now():%H:%M:%S}] "
              f"pipeline={'无' if not pipeline_alive() else '运行中'} "
              f"db={'空闲' if db_free() else '占用'} → 连续通过 {passed}/2",
              flush=True)
        if passed >= 2:
            break
        time.sleep(60)
    else:
        print("[watch] 等待超时（8h），放弃", flush=True)
        sys.exit(2)

    print(f"[watch] 主库空闲确认，启动批量 {datetime.now():%H:%M:%S}",
          flush=True)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "factor_eval"
                                        / "build_all_center_v3.py"),
                        "--workers", "3", "--end", "2026-09-15"],
                       cwd=str(ROOT))
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
