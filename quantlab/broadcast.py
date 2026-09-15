"""变更广播机制（Change Broadcast）
=====================================
全系统统一的事件广播通道：任何数据、脚本、自动化任务的变更（含失败/降级）
都追加到广播日志，供后续任务在规划前读取，保证跨会话/跨任务的连续性。

存储（三个文件；目录可用环境变量 QUANTLAB_BROADCAST_DIR 覆盖，测试用）:
  <root>/BROADCAST.md                    人读渲染板（工作区根目录，git 入库，最新在前）
  <data>/broadcast/broadcast.jsonl       机读全量日志，append-only，每行一个 JSON
  <data>/broadcast/.read_cursor          未读游标（最后一个"已读"的 seq）

设计约束（不可违背）:
  1. append-only —— 只追加 JSONL，从不改写已有记录；BROADCAST.md 由 JSONL 派生重渲
  2. best-effort —— 广播内部异常一律吞掉（stderr 提示），绝不影响数据/交易主流程
  3. 零依赖 —— 纯标准库；不碰 DuckDB/Parquet，不占用主库写锁
  4. 进程安全 —— O_CREAT|O_EXCL 锁文件 + 陈旧锁回收，多进程并发追加不丢行

用法:
  from quantlab.broadcast import broadcast, unread
  broadcast("data", "kline_daily 增量完成", action="change",
            detail="...", impact="...", source="update.py")
  for r in unread():
      print(r["seq"], r["category"], r["title"])

类别: data / script / automation / model / incident / doc
动作: add / change / remove / fail / warn / info（失败与降级必须广播）
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

__all__ = ["broadcast", "read_records", "unread", "mark_read",
           "render_markdown", "CATEGORIES", "ACTIONS"]

CATEGORIES = ("data", "script", "automation", "model", "incident", "doc")
ACTIONS = ("add", "change", "remove", "fail", "warn", "info")

MAX_MD_ENTRIES = 300               # BROADCAST.md 渲染条数上限（最新 N 条）
ROTATE_BYTES = 10 * 1024 * 1024    # JSONL 超过 10MB 轮转为 .1（保留一份）
LOCK_STALE_SEC = 60                # 锁文件超过此秒数视为陈旧，强制回收
LOCK_RETRY = 40                    # 获取锁重试次数 × 0.1s


# ── 路径 ────────────────────────────────────────────────────────────
def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _dir() -> Path:
    env = os.environ.get("QUANTLAB_BROADCAST_DIR")
    if env:
        return Path(env)
    return _root() / "data" / "broadcast"


def _jsonl() -> Path:
    return _dir() / "broadcast.jsonl"


def _cursor_file() -> Path:
    return _dir() / ".read_cursor"


def _md_path() -> Path:
    # 测试隔离时 MD 与 JSONL 同目录，避免污染真实工作区根目录
    if os.environ.get("QUANTLAB_BROADCAST_DIR"):
        return _dir() / "BROADCAST.md"
    return _root() / "BROADCAST.md"


# ── 进程锁 ──────────────────────────────────────────────────────────
@contextmanager
def _locked():
    """O_CREAT|O_EXCL 锁文件；超时后 yield False（调用方自行决定降级策略）"""
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    lf = d / ".lock"
    got = False
    for _ in range(LOCK_RETRY):
        try:
            fd = os.open(str(lf), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            got = True
            break
        except FileExistsError:
            try:
                if time.time() - lf.stat().st_mtime > LOCK_STALE_SEC:
                    lf.unlink()
            except OSError:
                pass
            time.sleep(0.1)
    try:
        yield got
    finally:
        if got:
            try:
                lf.unlink()
            except OSError:
                pass


# ── 核心 API ────────────────────────────────────────────────────────
def _caller() -> str:
    """默认来源：调用方 文件名:行号"""
    try:
        f = sys._getframe(3)          # _caller <- broadcast <- 真实调用方
        return f"{Path(f.f_code.co_filename).name}:{f.f_lineno}"
    except Exception:
        return ""


def _next_seq() -> int:
    """读 JSONL 尾部取最大 seq +1（只在持锁时调用）"""
    p = _jsonl()
    if not p.exists():
        return 1
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
        mx = 0
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mx = max(mx, int(json.loads(line).get("seq", 0)))
            except Exception:
                continue
        return mx + 1
    except Exception:
        return 1


def _append_jsonl(rec: dict) -> None:
    p = _jsonl()
    # 轮转：超过上限改名为 .1（覆盖旧 .1），保持活跃文件小而快
    try:
        if p.exists() and p.stat().st_size > ROTATE_BYTES:
            p.rename(p.with_suffix(".jsonl.1"))
    except OSError:
        pass
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def broadcast(category: str, title: str, action: str = "info",
              detail: str = "", impact: str = "", source: str | None = None,
              extra: dict | None = None) -> int | None:
    """追加一条广播（best-effort，绝不抛异常）。返回 seq，失败返回 None。

    参数:
      category: data / script / automation / model / incident / doc
      action:   add / change / remove / fail / warn / info
      title:    一句话标题（≤200 字符，自动截断）
      detail:   变更细节（≤1000 字符）
      impact:   对下游的影响（≤500 字符）——读者最关心的字段，尽量写
      source:   来源（默认自动取调用方 文件名:行号）
      extra:    附加结构化字段（可选，值截断 200 字符）
    """
    try:
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "category": str(category or "script").strip().lower()[:20],
            "action": str(action or "info").strip().lower()[:10],
            "title": str(title or "").strip()[:200],
            "detail": str(detail or "")[:1000],
            "impact": str(impact or "")[:500],
            "source": str(source or _caller()).strip()[:120],
        }
        if extra:
            try:
                rec["extra"] = {str(k): str(v)[:200]
                                for k, v in list(extra.items())[:10]}
            except Exception:
                pass
        with _locked() as got:
            if not got:
                # 单用户系统 + 4s 重试，实际到不了这里；真到了就无锁追加并提示
                print("[broadcast] 锁超时，降级为无锁追加", file=sys.stderr)
            rec["seq"] = _next_seq()
            _append_jsonl(rec)
        render_markdown()
        return rec["seq"]
    except Exception as e:                                  # noqa: BLE001
        print(f"[broadcast] 广播写入失败(忽略): {e}", file=sys.stderr)
        return None


# ── 读取 ────────────────────────────────────────────────────────────
def _iter_records():
    """按 .1 → 当前 的顺序产出全部记录（跳过损坏行）"""
    d = _dir()
    paths = [d / "broadcast.jsonl.1", d / "broadcast.jsonl"]
    for p in paths:
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except OSError:
            continue


def read_records(limit: int = 20, category: str | None = None,
                 action: str | None = None, since_seq: int | None = None,
                 oldest_first: bool = False) -> list[dict]:
    """读取广播记录，默认最新在前。可按类别/动作/seq 下限过滤。"""
    recs = list(_iter_records())
    if category:
        recs = [r for r in recs if r.get("category") == category]
    if action:
        recs = [r for r in recs if r.get("action") == action]
    if since_seq is not None:
        recs = [r for r in recs if int(r.get("seq", 0)) > since_seq]
    recs.sort(key=lambda r: int(r.get("seq", 0)), reverse=not oldest_first)
    if limit:
        recs = recs[:limit] if not oldest_first else recs[-limit:]
    return recs


def _read_cursor() -> int:
    try:
        return int(_cursor_file().read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def unread() -> list[dict]:
    """游标之后的未读记录（最新在前）。"""
    return read_records(limit=0, since_seq=_read_cursor())


def mark_read(seq: int | None = None) -> int:
    """推进未读游标（默认推到最新一条）。返回新游标值。"""
    if seq is None:
        recs = read_records(limit=1)
        seq = int(recs[0]["seq"]) if recs else 0
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        _cursor_file().write_text(str(int(seq)), encoding="utf-8")
    except OSError as e:
        print(f"[broadcast] 游标写入失败: {e}", file=sys.stderr)
    return int(seq)


# ── 渲染 ────────────────────────────────────────────────────────────
def _md_cell(s: str, width: int = 120) -> str:
    s = str(s or "")
    if len(s) > width:
        s = s[:width] + "…"
    return s.replace("|", "\\|").replace("\n", "<br>")


def render_markdown() -> None:
    """由 JSONL 重渲 BROADCAST.md（最新在前，最多 MAX_MD_ENTRIES 条）。"""
    recs = read_records(limit=MAX_MD_ENTRIES)
    total = sum(1 for _ in _iter_records())
    cur = _read_cursor()
    n_unread = total - cur if total > cur else 0
    lines = [
        "# QuantLab 变更广播板（BROADCAST）",
        "",
        "> **协议**：每项新任务开始前，先读本板最新条目再规划任务；任何数据 / 脚本 /",
        "> 自动化任务的变更（含失败与降级）完成后，立即追加广播。失败与降级是",
        "> 连续性最关键的事件，**必须**广播。",
        ">",
        "> - 机读全量日志：`data/broadcast/broadcast.jsonl`（append-only，本板由它派生）",
        "> - CLI：`python scripts/broadcast.py read [-n 20] [--mark-read]` · `unread` ·",
        ">   `add --category data --title \"...\" --detail \"...\" --impact \"...\"`",
        "> - 代码：`from quantlab.broadcast import broadcast`",
        "",
        f"累计 **{total}** 条 · 游标 #{cur} · 未读 **{n_unread}** 条 · 本板显示最新 "
        f"{len(recs)} 条",
        "",
        "| seq | 时间 | 类别 | 动作 | 标题 | 详情 | 影响 | 来源 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in recs:
        ts = str(r.get("ts", ""))[:16].replace("T", " ")
        lines.append(
            f"| #{r.get('seq', '?')} | {ts} | {r.get('category', '')} "
            f"| {r.get('action', '')} | {_md_cell(r.get('title'), 60)} "
            f"| {_md_cell(r.get('detail'), 100)} | {_md_cell(r.get('impact'), 80)} "
            f"| {_md_cell(r.get('source'), 30)} |"
        )
    lines.append("")
    p = _md_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, p)          # 原子替换，避免读到半写状态
