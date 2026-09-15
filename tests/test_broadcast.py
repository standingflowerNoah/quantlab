#!/usr/bin/env python3
"""变更广播机制单元测试（隔离目录，不污染真实广播日志）
=====================================
用法: python tests/test_broadcast.py
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ql_broadcast_test_")
os.environ["QUANTLAB_BROADCAST_DIR"] = _TMP   # 必须在导入前设置
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.broadcast import (broadcast, mark_read, read_records,  # noqa: E402
                                render_markdown, unread)
_BDIR = Path(_TMP)
_MD = _BDIR / "BROADCAST.md"


class TestBroadcast(unittest.TestCase):
    def setUp(self):
        for p in _BDIR.glob("*"):
            if p.is_file():
                p.unlink()
        mark_read(0)

    def test_add_and_read(self):
        seq = broadcast("data", "测试条目A", action="change",
                        detail="细节", impact="影响", source="test")
        self.assertIsNotNone(seq)
        recs = read_records(limit=5)
        self.assertEqual(recs[0]["title"], "测试条目A")
        self.assertEqual(recs[0]["category"], "data")
        self.assertEqual(recs[0]["action"], "change")
        # JSONL 每行都是合法 JSON（append-only 可解析）
        for line in (_BDIR / "broadcast.jsonl").read_text(
                encoding="utf-8").splitlines():
            json.loads(line)

    def test_md_rendered(self):
        broadcast("script", "渲染测试", action="add", detail="d", impact="i")
        self.assertTrue(_MD.exists())
        text = _MD.read_text(encoding="utf-8")
        self.assertIn("渲染测试", text)
        self.assertIn("变更广播板", text)

    def test_seq_monotonic_and_concurrent(self):
        """8 线程 × 5 条并发追加：seq 严格唯一且连续"""
        def worker(k):
            for i in range(5):
                broadcast("script", f"并发{k}-{i}", action="change",
                          source="test")
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = read_records(limit=0, oldest_first=True)
        self.assertEqual(len(recs), 40)
        seqs = [r["seq"] for r in recs]
        self.assertEqual(len(set(seqs)), 40, "seq 出现重复（锁失效）")
        self.assertEqual(sorted(seqs), list(range(1, 41)))

    def test_unread_and_mark_read(self):
        broadcast("data", "条目1", source="test")
        broadcast("data", "条目2", source="test")
        u = unread()
        self.assertEqual(len(u), 2)
        mark_read()
        self.assertEqual(unread(), [])
        broadcast("data", "条目3", source="test")
        u = unread()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["title"], "条目3")

    def test_filter(self):
        broadcast("data", "数据事件", source="test")
        broadcast("incident", "失败事件", action="fail", source="test")
        only_data = read_records(limit=10, category="data")
        self.assertEqual([r["title"] for r in only_data], ["数据事件"])
        only_fail = read_records(limit=10, action="fail")
        self.assertEqual([r["title"] for r in only_fail], ["失败事件"])

    def test_never_raises(self):
        """best-effort：怪输入也不允许抛异常"""
        try:
            s1 = broadcast(None, "", action="weird!!", detail=None)
            s2 = broadcast("data", "x" * 5000, detail="|" * 3000)
            self.assertTrue(s1 is None or isinstance(s1, int))
            self.assertTrue(s2 is None or isinstance(s2, int))
        except Exception as e:                               # noqa: BLE001
            self.fail(f"broadcast 抛出了异常: {e}")

    def test_md_escape(self):
        broadcast("script", "竖线|标题", action="info",
                  detail="含|竖线和\n换行", source="test")
        text = _MD.read_text(encoding="utf-8")
        self.assertIn("竖线\\|标题", text)


if __name__ == "__main__":
    # 手动重渲一次确保 render_markdown 可独立调用
    render_markdown()
    unittest.main(verbosity=2)
