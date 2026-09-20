from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner.sessionlog import collect, iter_session_files, parse_session_file


def session_record(session_id: str) -> dict:
    return {"type": "session", "version": 3, "id": session_id, "timestamp": "2026-09-20T13:58:57.714Z"}


def user_record(benchmark_id: str) -> dict:
    return {
        "type": "message",
        "timestamp": "2026-09-20T13:58:57.835Z",
        "message": {
            "role": "user",
            "content": f"你好！\n[message_id: cm-{benchmark_id}]",
            "idempotencyKey": f"{benchmark_id}:user",
        },
    }


def assistant_record(**usage: int) -> dict:
    payload = {"input": 5586, "output": 106, "cacheRead": 10496, "cacheWrite": 0, "totalTokens": 16188}
    payload.update(usage)
    return {
        "type": "message",
        "timestamp": "2026-09-20T13:58:59.029Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "你好"}],
            "model": "deepseek-flash",
            "provider": "custom-66270c13",
            "usage": payload,
            "stopReason": "stop",
        },
    }


class ParseTests(unittest.TestCase):
    def _write(self, name: str, records: list[dict]) -> Path:
        temporary = getattr(self, "_dir", None)
        if temporary is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self._dir = temporary
        path = Path(temporary.name) / name
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_matches_by_idempotency_key_not_time(self) -> None:
        """用户消息带 '<BenchmarkId>:user'，所以这一路是精确匹配。"""
        path = self._write(
            "s1.jsonl",
            [session_record("s1"), user_record("bench-Case02-r1-abc"), assistant_record()],
        )
        found = parse_session_file(path)
        self.assertEqual(1, len(found))
        self.assertEqual("bench-Case02-r1-abc", found[0].benchmark_id)
        self.assertEqual(16188, found[0].total_tokens)
        self.assertEqual(10496, found[0].cache_read_tokens)
        self.assertEqual("deepseek-flash", found[0].model)
        self.assertEqual("idempotency-key", found[0].as_sample().match)

    def test_multi_turn_usage_is_summed_per_run(self) -> None:
        path = self._write(
            "s2.jsonl",
            [
                session_record("s2"),
                user_record("bench-1"),
                assistant_record(),
                assistant_record(input=10, output=5, cacheRead=0, totalTokens=15),
            ],
        )
        found = parse_session_file(path)
        self.assertEqual(1, len(found))
        self.assertEqual(2, found[0].assistant_turns)
        self.assertEqual(16203, found[0].total_tokens)

    def test_two_runs_in_one_session_do_not_merge(self) -> None:
        """我们每轮换 sessionKey，但万一复用了，这里要分开而不是加在一起。"""
        path = self._write(
            "s3.jsonl",
            [
                session_record("s3"),
                user_record("bench-1"),
                assistant_record(),
                user_record("bench-2"),
                assistant_record(input=1, output=1, cacheRead=0, totalTokens=2),
            ],
        )
        found = {item.benchmark_id: item for item in parse_session_file(path)}
        self.assertEqual({"bench-1", "bench-2"}, set(found))
        self.assertEqual(16188, found["bench-1"].total_tokens)
        self.assertEqual(2, found["bench-2"].total_tokens)

    def test_user_message_without_our_key_is_ignored(self) -> None:
        """界面上手动发的消息没有我们的 idempotencyKey，不能算进基准数据。"""
        path = self._write(
            "s4.jsonl",
            [
                session_record("s4"),
                {"type": "message", "message": {"role": "user", "content": "手动点的"}},
                assistant_record(),
            ],
        )
        self.assertEqual([], parse_session_file(path))

    def test_assistant_without_usage_is_not_counted(self) -> None:
        path = self._write(
            "s5.jsonl",
            [
                session_record("s5"),
                user_record("bench-1"),
                {"type": "message", "message": {"role": "assistant", "content": []}},
            ],
        )
        self.assertEqual([], parse_session_file(path))

    def test_malformed_lines_are_skipped(self) -> None:
        path = self._write("s6.jsonl", [session_record("s6"), user_record("bench-1")])
        path.write_text(
            path.read_text(encoding="utf-8") + "这不是 JSON\n"
            + json.dumps(assistant_record(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        found = parse_session_file(path)
        self.assertEqual(16188, found[0].total_tokens)


class DirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)

    def _write(self, name: str, records: list[dict]) -> None:
        (self.dir / name).write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
            encoding="utf-8",
        )

    def test_trajectory_files_are_skipped(self) -> None:
        """轨迹文件 130KB 起步且不含 usage 汇总，跑批时逐个解析纯属浪费。"""
        self._write("a.jsonl", [session_record("a")])
        self._write("a.trajectory.jsonl", [session_record("a")])
        names = [path.name for path in iter_session_files(self.dir)]
        self.assertEqual(["a.jsonl"], names)

    def test_collect_filters_to_the_requested_runs(self) -> None:
        self._write("a.jsonl", [session_record("a"), user_record("bench-1"), assistant_record()])
        self._write("b.jsonl", [session_record("b"), user_record("bench-2"), assistant_record()])
        found = collect({"bench-2"}, sessions_dir=self.dir)
        self.assertEqual(["bench-2"], list(found))


if __name__ == "__main__":
    unittest.main()
