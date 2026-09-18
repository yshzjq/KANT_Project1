"""외부 API·실제 대화기록을 사용하지 않는 동기화 회귀 테스트."""

import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main_ollama_chat as chat


SCRIPT = Path(__file__).resolve().parents[1] / "slack_api_LLM_questions-chat.py"
spec = importlib.util.spec_from_file_location("slack_sync", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "slack.json"
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        self.parent = {
            "ts": "100.000001", "text": "수업 안내", "user": "U1",
            "reply_count": 1, "latest_reply": "101.000001",
        }
        self.reply = {
            "ts": "101.000001", "thread_ts": "100.000001",
            "text": "수업은 3시입니다.", "user": "U2",
        }

    def save_existing(self, messages=None):
        data = {
            "channel_id": sync.CHANNEL_ID,
            "last_sync_started_ts": "102.000001",
            "thread_versions": {
                self.parent["ts"]: {"reply_count": 1, "latest_reply": self.reply["ts"]},
            },
            "messages": messages if messages is not None else [self.parent, self.reply],
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def reader(self, reply=None, extra=None, limited=False):
        return Mock(side_effect=[
            ([copy.deepcopy(self.parent)] + (extra or []), limited),
            ([copy.deepcopy(self.parent), copy.deepcopy(reply or self.reply)], limited),
        ])

    def test_first_run_saves_history_and_replies_in_order(self):
        data = sync.sync_slack_data(self.path, self.reader())
        self.assertEqual(data["messages"], [self.parent, self.reply])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), data)

    def test_unchanged_does_not_rewrite_file_or_timestamp(self):
        saved = self.save_existing()
        before = self.path.read_bytes()
        modified = self.path.stat().st_mtime_ns
        reader = self.reader()
        data = sync.sync_slack_data(self.path, reader)
        self.assertEqual(data, saved)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)
        self.assertEqual(reader.call_count, 2)
        self.assertIn("기존 대화기록 파일을 사용", self.output.getvalue())

    def test_reply_edit_detected_when_count_and_latest_reply_unchanged(self):
        self.save_existing()
        edited = {**self.reply, "text": "수업은 4시입니다."}
        data = sync.sync_slack_data(self.path, self.reader(reply=edited))
        self.assertEqual(data["messages"][1]["text"], edited["text"])
        self.assertIn("신규 0개 / 변경 1개", self.output.getvalue())

    def test_parent_edit_and_new_message_are_saved_without_duplicates(self):
        self.save_existing()
        self.parent = {**self.parent, "text": "변경된 수업 안내"}
        added = {"ts": "103.000001", "text": "새 공지", "user": "U1"}
        data = sync.sync_slack_data(self.path, self.reader(extra=[added]))
        self.assertEqual(data["messages"], [self.parent, self.reply, added])
        self.assertIn("신규 1개 / 변경 1개", self.output.getvalue())

    def test_failed_reply_fetch_keeps_existing_file(self):
        self.save_existing()
        before = self.path.read_bytes()
        reader = Mock(side_effect=[([self.parent], False), RuntimeError("연결 실패")])
        with self.assertRaises(RuntimeError):
            sync.sync_slack_data(self.path, reader)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_limited_history_preserves_unseen_records_and_reports_limit(self):
        older = {"ts": "90.000001", "text": "과거 기록"}
        self.save_existing([older, self.parent, self.reply])
        edited = {**self.reply, "text": "변경됨"}
        data = sync.sync_slack_data(self.path, self.reader(reply=edited, limited=True))
        self.assertEqual(data["messages"], [older, self.parent, edited])
        self.assertIn("접근이 제한", self.output.getvalue())

    def test_missing_record_is_not_assumed_deleted(self):
        self.save_existing()
        data = sync.sync_slack_data(self.path, Mock(return_value=([], False)))
        self.assertEqual(data["messages"], [self.parent, self.reply])

    def test_invalid_saved_file_stops_before_api(self):
        self.path.write_text("not json", encoding="utf-8")
        reader = Mock()
        with self.assertRaises(ValueError):
            sync.sync_slack_data(self.path, reader)
        reader.assert_not_called()
        self.assertEqual(self.path.read_text(), "not json")

    def test_atomic_replace_failure_preserves_old_file_and_cleans_temp(self):
        self.save_existing()
        before = self.path.read_bytes()
        edited = {**self.reply, "text": "변경됨"}
        with patch.object(Path, "replace", side_effect=OSError("파일 사용 중")):
            with self.assertRaises(OSError):
                sync.sync_slack_data(self.path, self.reader(reply=edited))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_pagination_reads_all_pages_and_propagates_limit(self):
        pages = [
            {"messages": [self.parent], "has_more": True,
             "response_metadata": {"next_cursor": "page2"}},
            {"messages": [self.reply], "is_limited": True},
        ]
        with patch.object(sync, "call_slack", side_effect=pages) as api:
            messages, limited = sync.read_all_pages("conversations.history")
        self.assertEqual(messages, [self.parent, self.reply])
        self.assertTrue(limited)
        self.assertEqual(api.call_args.kwargs["cursor"], "page2")

    def test_incomplete_or_repeating_pagination_raises(self):
        for pages in (
            [{"messages": [], "has_more": True}],
            [{"messages": [], "response_metadata": {"next_cursor": "same"}}] * 2,
        ):
            with self.subTest(pages=pages):
                with patch.object(sync, "call_slack", side_effect=pages):
                    with self.assertRaises(RuntimeError):
                        sync.read_all_pages("conversations.history")

    def test_missing_messages_is_not_treated_as_empty_history(self):
        with patch.object(sync, "call_slack", return_value={"ok": True}):
            with self.assertRaises(ValueError):
                sync.read_all_pages("conversations.history")

    def test_new_channel_uses_its_id_for_history_replies_and_saved_file(self):
        channel_id = "CTESTNEWS"
        with patch.object(sync, "DATA_PATH", self.path), patch.object(sync, "call_slack", side_effect=[
            {"messages": [self.parent]}, {"messages": [self.parent, self.reply]},
        ]) as api:
            data = sync.sync_slack_data(channel_id=channel_id)
        self.assertEqual([call.kwargs["channel"] for call in api.call_args_list], [channel_id, channel_id])
        self.assertEqual([call.args[0] for call in api.call_args_list],
                         ["conversations.history", "conversations.replies"])
        self.assertEqual(data["channel_id"], channel_id)
        self.assertEqual(json.loads((self.path.parent / f"slack_{channel_id}.json").read_text(encoding="utf-8")), data)
        self.assertFalse(self.path.exists())

    def test_wrong_channel_file_and_foreign_delete_cannot_change_existing_data(self):
        saved = self.save_existing()
        before = self.path.read_bytes()
        reader = Mock(return_value=([], False))
        with self.assertRaisesRegex(RuntimeError, "채널 ID"):
            sync.sync_slack_data(self.path, reader, channel_id="CTESTNEWS")
        reader.assert_not_called()
        data = sync.sync_slack_data(self.path, reader, full_sync=False, events=[{
            "channel": "CTESTNEWS", "subtype": "message_deleted", "deleted_ts": self.parent["ts"],
        }])
        self.assertEqual(data, saved)
        self.assertEqual(self.path.read_bytes(), before)
        reader.assert_not_called()


class QuestionFlowTests(unittest.TestCase):
    def setUp(self):
        mode = patch.object(chat, "QUESTION_EVALUATION", False)
        mode.start()
        self.addCleanup(mode.stop)

    def test_sync_then_input_then_answer_with_entered_question(self):
        steps = []
        messages = [{"ts": "100.000001", "text": "수업 공지"}]
        questions = iter(["  변경된 수업 시간은?  ", "종료"])

        def load():
            steps.append("sync")
            return {"messages": messages}

        def ask(prompt):
            steps.append("input")
            return next(questions)

        def answer(question, records):
            steps.append("answer")
            self.assertEqual(question, "변경된 수업 시간은?")
            self.assertEqual(records, messages)

        with patch.object(chat, "load_latest_slack_data", side_effect=load), \
             patch("builtins.input", side_effect=ask), \
             patch.object(chat, "answer_question", side_effect=answer), \
             contextlib.redirect_stdout(io.StringIO()):
            chat.main()
        self.assertEqual(steps, ["sync", "input", "sync", "answer", "input"])

    def test_sync_failure_stops_before_question_or_llm(self):
        with patch.object(chat, "load_latest_slack_data", side_effect=SystemExit("실패")), \
             patch("builtins.input") as ask, patch.object(chat, "answer_question") as answer:
            with self.assertRaises(SystemExit):
                chat.main()
        ask.assert_not_called()
        answer.assert_not_called()

    def test_blank_input_is_skipped_without_default_question(self):
        messages = [{"ts": "100.000001", "text": "수업 공지"}]
        output = io.StringIO()
        with patch.object(chat, "load_latest_slack_data", return_value={"messages": messages}), \
             patch("builtins.input", side_effect=["", "  ", "직접 입력한 질문", "종료"]) as ask, \
             patch.object(chat, "answer_question") as answer, \
             contextlib.redirect_stdout(output):
            chat.main()
        answer.assert_called_once_with("직접 입력한 질문", messages)
        self.assertNotIn("기본 질문", output.getvalue())
        self.assertTrue(all("기본 질문" not in call.args[0] for call in ask.call_args_list))

    def test_multiple_questions_continue_after_answer_errors(self):
        messages = [{"ts": "100.1", "text": "안내"}]
        with patch("builtins.input", side_effect=["첫 질문", "둘째 질문", "셋째 질문", " EXIT "]), \
             patch.object(chat, "load_latest_slack_data", return_value={"messages": messages}), \
             patch.object(chat, "answer_question", side_effect=[RuntimeError("연결 오류"), SystemExit("검색 실패"), "답변"]) as answer, \
             contextlib.redirect_stdout(io.StringIO()):
            chat.run_interactive_chat(messages)
        self.assertEqual([call.args for call in answer.call_args_list], [
            ("첫 질문", messages), ("둘째 질문", messages), ("셋째 질문", messages),
        ])

    def test_exit_commands_and_terminal_interrupt_do_not_call_model(self):
        for command in ("종료", "quit", "EXIT", EOFError(), KeyboardInterrupt()):
            with self.subTest(command=command), \
                 patch("builtins.input", side_effect=[command]), \
                 patch.object(chat, "answer_question") as answer, \
                 contextlib.redirect_stdout(io.StringIO()):
                chat.run_interactive_chat([])
            answer.assert_not_called()

    def test_ctrl_c_during_answer_exits_loop(self):
        with patch("builtins.input", side_effect=["질문"]) as ask, \
             patch.object(chat, "load_latest_slack_data", return_value={"messages": [{"ts": "100.1"}]}), \
             patch.object(chat, "answer_question", side_effect=KeyboardInterrupt()), \
             contextlib.redirect_stdout(io.StringIO()):
            chat.run_interactive_chat([])
        self.assertEqual(ask.call_count, 1)

    def test_each_question_uses_new_snapshot_and_sync_failure_does_not_answer_old_data(self):
        old = [{"ts": "100.1", "text": "이전 안내"}]
        changed = [{"ts": "100.1", "text": "수정된 안내"}]
        latest = [{"ts": "200.1", "text": "삭제 후 새 공지"}]
        with patch("builtins.input", side_effect=["", "첫 질문", "실패 질문", "둘째 질문", "quit"]), \
             patch.object(chat, "load_latest_slack_data", side_effect=[
                 {"messages": changed}, SystemExit("동기화 실패"), {"messages": latest},
             ]) as load, patch.object(chat, "answer_question") as answer, \
             contextlib.redirect_stdout(io.StringIO()):
            chat.run_interactive_chat(old)
        self.assertEqual(load.call_count, 3)
        self.assertEqual([call.args for call in answer.call_args_list], [
            ("첫 질문", changed), ("둘째 질문", latest),
        ])


if __name__ == "__main__":
    unittest.main()
