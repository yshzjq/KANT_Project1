"""외부 연결 없이 상시 봇의 상태·중복 방지·이벤트 기반 갱신을 검사합니다."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import slack_bot as bot
from slack_runtime import BotState, FileLock, is_locked, is_ready


class BotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state_path = self.root / "state.sqlite3"
        self.state = BotState(self.state_path)
        self.data_path = self.root / "slack.json"
        self.lock_path = self.root / "bot.lock"
        self.parent = {"ts": "100.000001", "text": "안내", "reply_count": 1}
        self.reply = {"ts": "101.000001", "thread_ts": "100.000001", "text": "답글"}
        self.data = {"channel_id": bot.collector.CHANNEL_ID, "messages": [self.parent, self.reply]}
        self.expected_data = {"channel_ids": [bot.collector.CHANNEL_ID], "messages": [
            {**message, "channel_id": bot.collector.CHANNEL_ID, "channel_name": "질문잡담방"}
            for message in [self.parent, self.reply]
        ]}
        self.data_path.write_text(json.dumps(self.data), encoding="utf-8")
        for replacement in (
            patch.object(bot, "STATE_PATH", self.state_path),
            patch.object(bot, "LOCK_PATH", self.lock_path),
            patch.object(bot.collector, "DATA_PATH", self.data_path),
            patch.object(bot.collector, "SLACK_CHANNELS", {bot.collector.CHANNEL_ID: "질문잡담방"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            replacement.__enter__()
            self.addCleanup(replacement.__exit__, None, None, None)
        self.state.begin(bot.collector.get_channel_ids())
        self.state.connected()

    def clean(self):
        self.state.finish(self.state.snapshot())

    def payload(self, event_id="Ev1", **event):
        return {"event_id": event_id, "event": {
            "type": "message", "channel": bot.collector.CHANNEL_ID,
            "ts": "102.000001", "text": "새 글", **event,
        }}

    def test_no_changes_means_no_history_api_or_file_write(self):
        self.clean()
        before = self.data_path.stat().st_mtime_ns
        request = self.state.request()
        with patch.object(bot.collector, "sync_slack_data") as sync:
            bot.sync_pending(self.state)
        sync.assert_not_called()
        self.assertEqual(self.data_path.stat().st_mtime_ns, before)
        self.assertTrue(is_ready(self.state.snapshot(), request))

    def test_start_and_reconnection_force_recovery(self):
        with patch.object(bot.collector, "sync_slack_data") as sync:
            bot.sync_pending(self.state)
            self.assertTrue(sync.call_args.kwargs["full_sync"])
            bot.sync_pending(self.state)
            self.assertEqual(sync.call_count, 1)
            self.state.disconnected()
            self.assertFalse(bot.sync_pending(self.state))
            self.state.connected()
            bot.sync_pending(self.state)
            self.assertEqual(sync.call_count, 2)
            self.assertTrue(sync.call_args.kwargs["full_sync"])

    def test_events_filter_channel_deduplicate_and_remain_after_reopen(self):
        self.clean()
        bot.record_event(self.state, self.payload())
        bot.record_event(self.state, self.payload())
        bot.record_event(self.state, self.payload("Ev2", channel="OTHER"))
        bot.record_event(self.state, self.payload("Ev3", type="reaction_added"))
        reopened = BotState(self.state_path)
        self.assertEqual(len(reopened.snapshot()["events"]), 1)
        with patch.object(bot.collector, "sync_slack_data") as sync:
            bot.sync_pending(reopened)
        self.assertFalse(sync.call_args.kwargs["full_sync"])
        bot.record_event(reopened, self.payload())
        self.assertFalse(reopened.snapshot()["events"])

    def test_events_and_reconnect_during_fetch_are_not_marked_complete(self):
        self.clean()
        bot.record_event(self.state, self.payload())

        def incoming(**_):
            bot.record_event(self.state, self.payload("Ev2"))
            self.state.disconnected()
            self.state.connected()

        with patch.object(bot.collector, "sync_slack_data", side_effect=incoming):
            bot.sync_pending(self.state)
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertGreater(snapshot["recovery"], snapshot["recovered"])
        self.assertFalse(is_ready(snapshot, 0))

    def test_failure_keeps_pending_events_for_retry(self):
        self.clean()
        bot.record_event(self.state, self.payload())
        with patch.object(bot.collector, "sync_slack_data", side_effect=RuntimeError("조회 실패")):
            with self.assertRaises(RuntimeError):
                bot.sync_pending(self.state)
        self.assertEqual(len(self.state.snapshot()["events"]), 1)
        with patch.object(bot.collector, "sync_slack_data"):
            bot.sync_pending(self.state)
        self.assertFalse(self.state.snapshot()["events"])

    def test_os_lock_detects_running_process_and_releases_on_exit(self):
        self.assertFalse(is_locked(self.lock_path))
        with FileLock(self.lock_path):
            self.assertTrue(is_locked(self.lock_path))
        self.assertFalse(is_locked(self.lock_path))

    def test_running_bot_is_reused_and_waits_for_request_barrier(self):
        self.clean()
        output = io.StringIO()
        with FileLock(self.lock_path), \
             patch.object(bot, "start_background_bot") as start, \
             patch.object(bot.time, "sleep", side_effect=lambda _: bot.sync_pending(self.state)), \
             patch.object(bot.collector, "sync_slack_data") as sync, contextlib.redirect_stdout(output):
            data = bot.ensure_slack_data(timeout=1, verbose=False)
        self.assertEqual(data, self.expected_data)
        self.assertEqual(output.getvalue(), "")
        start.assert_not_called()
        sync.assert_not_called()

    def test_missing_bot_starts_and_old_error_does_not_abort_new_start(self):
        self.state.fail("이전 실패")
        self.clean()
        self.state.fail("이전 실패")
        output = io.StringIO()
        with patch.object(bot, "is_locked", side_effect=[False, True]), \
             patch.object(bot, "start_background_bot") as start, \
             patch.object(bot.time, "sleep", side_effect=lambda _: bot.sync_pending(self.state)), \
             contextlib.redirect_stdout(output):
            self.assertEqual(bot.ensure_slack_data(timeout=1, verbose=False), self.expected_data)
        start.assert_called_once()
        self.assertEqual(output.getvalue(), "")

    def test_missing_cache_requests_full_recovery(self):
        self.clean()
        self.data_path.unlink()

        def save(**_):
            self.data_path.write_text(json.dumps(self.data), encoding="utf-8")

        with FileLock(self.lock_path), \
             patch.object(bot.time, "sleep", side_effect=lambda _: bot.sync_pending(self.state)), \
             patch.object(bot.collector, "sync_slack_data", side_effect=save) as sync:
            self.assertEqual(bot.ensure_slack_data(timeout=1), self.expected_data)
        self.assertTrue(sync.call_args.kwargs["full_sync"])

    def test_stale_heartbeat_and_disconnected_state_are_not_ready(self):
        self.clean()
        self.state.execute("UPDATE state SET heartbeat=0")
        self.assertFalse(is_ready(self.state.snapshot(), 0))
        self.state.heartbeat()
        self.state.disconnected()
        self.assertFalse(is_ready(self.state.snapshot(), 0))

    def test_two_channels_sync_only_changed_channel_and_merge_without_overwriting(self):
        second = "CTESTNEWS"
        channels = {bot.collector.CHANNEL_ID: "질문잡담방", second: "공지방"}
        notice = {"ts": self.parent["ts"], "text": "공지방 원문"}
        path = bot.collector.data_path_for(second)
        path.write_text(json.dumps({"channel_id": second, "messages": [notice]}), encoding="utf-8")
        self.clean()
        before = self.data_path.read_bytes()
        with patch.object(bot.collector, "SLACK_CHANNELS", channels):
            bot.record_event(self.state, self.payload(channel=second))
            with patch.object(bot.collector, "sync_slack_data") as sync:
                bot.sync_pending(self.state)
            sync.assert_called_once()
            self.assertEqual(sync.call_args.kwargs["channel_id"], second)
            self.assertFalse(sync.call_args.kwargs["full_sync"])
            self.assertEqual(sync.call_args.kwargs["events"][0]["channel"], second)
            merged = bot.collector.load_all_saved_data()
        self.assertEqual(self.data_path.read_bytes(), before)
        self.assertEqual(len(merged["messages"]), 3)
        self.assertEqual({m["text"] for m in merged["messages"] if m["ts"] == self.parent["ts"]},
                         {"안내", "공지방 원문"})
        self.assertEqual({m["channel_id"] for m in merged["messages"]}, set(channels))

    def test_partial_channel_failure_keeps_all_pending_events(self):
        second = "CTESTNEWS"
        with patch.object(bot.collector, "SLACK_CHANNELS", {bot.collector.CHANNEL_ID: "질문", second: "공지"}):
            bot.record_event(self.state, self.payload())
            bot.record_event(self.state, self.payload("Ev2", channel=second))
            with patch.object(bot.collector, "sync_slack_data", side_effect=[None, RuntimeError("not_in_channel")]):
                with self.assertRaisesRegex(RuntimeError, second):
                    bot.sync_pending(self.state)
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertNotEqual(snapshot["recovery"], snapshot["recovered"])

    def test_missing_second_file_recovers_only_that_channel(self):
        self.clean()
        with patch.object(bot.collector, "SLACK_CHANNELS", {bot.collector.CHANNEL_ID: "질문", "CTESTNEWS": "공지"}), \
             patch.object(bot.collector, "sync_slack_data") as sync:
            bot.sync_pending(self.state)
        sync.assert_called_once_with(events=[], full_sync=True, channel_id="CTESTNEWS")

    def test_configuration_change_restarts_bot_after_it_releases_lock(self):
        self.clean()
        self.state.execute("UPDATE bot_config SET channel_ids='[]' WHERE id=1")
        old_lock = FileLock(self.lock_path)
        old_lock.__enter__()
        new_lock = None

        def progress(_):
            nonlocal old_lock
            if old_lock is not None:
                self.assertTrue(self.state.snapshot()["stop"])
                old_lock.__exit__()
                old_lock = None
            else:
                bot.sync_pending(self.state)

        def start():
            nonlocal new_lock
            new_lock = FileLock(self.lock_path)
            new_lock.__enter__()
            self.state.begin(bot.collector.get_channel_ids())
            self.state.connected()

        try:
            with patch.object(bot, "start_background_bot", side_effect=start) as launch, \
                 patch.object(bot.time, "sleep", side_effect=progress), \
                 patch.object(bot.collector, "sync_slack_data"):
                result = bot.ensure_slack_data(timeout=2, verbose=False)
            launch.assert_called_once()
            self.assertEqual(result, self.expected_data)
        finally:
            if old_lock is not None:
                old_lock.__exit__()
            if new_lock is not None:
                new_lock.__exit__()

    def test_changed_reply_fetches_only_affected_thread_using_api_content(self):
        event = self.payload(subtype="message_changed", message=self.reply)["event"]
        updated = {**self.reply, "text": "API 최신 답글"}
        reader = Mock(return_value=([self.parent, updated], False))
        data = bot.collector.sync_slack_data(self.data_path, reader, events=[event], full_sync=False)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args, ("conversations.replies",))
        self.assertEqual(reader.call_args.kwargs["ts"], self.parent["ts"])
        self.assertEqual(data["messages"][1]["text"], "API 최신 답글")

    def test_new_parent_fetches_exact_timestamp_without_scanning_history(self):
        event = self.payload()["event"]
        reader = Mock(return_value=([event], False))
        data = bot.collector.sync_slack_data(self.data_path, reader, events=[event], full_sync=False)
        reader.assert_called_once_with(
            "conversations.history", oldest=event["ts"], latest=event["ts"], inclusive="true",
        )
        self.assertEqual(len(data["messages"]), 3)

    def test_delete_event_removes_only_confirmed_message(self):
        event = self.payload(subtype="message_deleted", deleted_ts=self.reply["ts"], previous_message=self.reply)["event"]
        reader = Mock(return_value=([{**self.parent, "reply_count": 0}], False))
        data = bot.collector.sync_slack_data(self.data_path, reader, events=[event], full_sync=False)
        self.assertEqual([message["ts"] for message in data["messages"]], [self.parent["ts"]])

    def test_two_edits_same_thread_are_queried_once(self):
        event = self.payload(subtype="message_changed", message=self.reply)["event"]
        reader = Mock(return_value=([self.parent, self.reply], False))
        bot.collector.sync_slack_data(self.data_path, reader, events=[event, event], full_sync=False)
        self.assertEqual(reader.call_count, 1)

    def test_socket_loop_persists_then_acknowledges_and_processes_new_event_during_fetch(self):
        # 실제 SDK의 연결 클래스만 교체하여 listen_and_sync의 콜백·수집 루프를 실행합니다.
        client = SimpleNamespace(
            on_message_listeners=[], on_close_listeners=[], on_error_listeners=[],
            socket_mode_request_listeners=[], is_connected=lambda: True, close=Mock(),
        )
        acknowledgements = []

        def ack(response):
            acknowledgements.append(response.envelope_id)
            self.assertGreaterEqual(len(self.state.snapshot()["events"]), 1)

        client.send_socket_mode_response = ack

        def deliver(event_id):
            request = SimpleNamespace(type="events_api", payload=self.payload(event_id), envelope_id=event_id)
            client.socket_mode_request_listeners[0](client, request)

        def connect():
            client.on_message_listeners[0]('{"type":"hello"}')
            deliver("Ev1")

        client.connect = connect
        calls = []

        def fetch(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                deliver("Ev2")
            else:
                self.state.execute("UPDATE state SET stop=1 WHERE id=1")

        # 테スト 실패 시에도 가짜 봇 루프가 영원히 실행되지 않게 합니다.
        timer = threading.Timer(5, lambda: self.state.execute("UPDATE state SET stop=1 WHERE id=1"))
        timer.start()
        try:
            with patch("slack_sdk.socket_mode.SocketModeClient", return_value=client), \
                 patch.object(bot.collector, "sync_slack_data", side_effect=fetch), \
                 patch.dict(bot.os.environ, {"SLACK_APP_TOKEN": "xapp-test", "SLACK_BOT_TOKEN": "xoxb-test"}):
                bot.listen_and_sync(self.state)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual(acknowledgements, ["Ev1", "Ev2"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["full_sync"])
        self.assertFalse(calls[1]["full_sync"])
        self.assertFalse(self.state.snapshot()["events"])
        client.close.assert_called_once()

    def test_background_launcher_detaches_without_tokens_on_command_line(self):
        with patch.object(bot, "validate_environment"), \
             patch.object(bot, "RUNTIME_DIR", self.root), \
             patch.object(bot, "launch_windows_process", return_value=123) as windows_launch, \
             patch.object(bot.subprocess, "Popen") as launch:
            bot.start_background_bot()
        args, kwargs = (windows_launch if bot.os.name == "nt" else launch).call_args
        self.assertEqual(args[0][-1], "--background")
        self.assertFalse(any("xapp-" in arg or "xoxb-" in arg for arg in args[0]))
        if bot.os.name == "nt":
            launch.assert_not_called()
            self.assertEqual(args[1], bot.BASE_DIR)
            self.assertEqual(args[2]["PYTHONIOENCODING"], "utf-8")
        else:
            self.assertEqual(kwargs["stdin"], bot.subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])

    @unittest.skipUnless(bot.os.name == "nt", "Windows 전용 실행")
    def test_windows_launcher_transfers_environment_only_via_stdin(self):
        result = SimpleNamespace(returncode=0, stdout='{"pid": 123}')
        with patch.object(bot.subprocess, "run", return_value=result) as launch:
            self.assertEqual(bot.launch_windows_process(
                ["python.exe", "한글 경로/bot.py"], self.root, {"TOKEN": "test-secret"}
            ), 123)
        args, kwargs = launch.call_args
        self.assertNotIn("test-secret", str(args))
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["environment"], ["TOKEN=test-secret"])
        self.assertEqual(payload["cwd"], str(self.root))
        self.assertTrue(kwargs["creationflags"] & bot.subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(bot.os.name == "nt", "Windows 전용 실행")
    def test_windows_launcher_reports_failures_without_raw_output(self):
        for result in (SimpleNamespace(returncode=1, stdout="test-secret"),
                       SimpleNamespace(returncode=0, stdout='{"pid": 0}'),
                       SimpleNamespace(returncode=0, stdout="test-secret")):
            with self.subTest(result=result), patch.object(bot.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError) as raised:
                    bot.launch_windows_process(["python.exe"], self.root, {})
                self.assertNotIn("test-secret", str(raised.exception))

    def test_background_child_opens_its_own_log(self):
        log_path = self.root / "bot.log"
        with patch.object(bot, "RUNTIME_DIR", self.root), \
             patch.object(bot, "LOG_PATH", log_path), \
             patch.object(bot, "run_bot", side_effect=lambda: print("child output")):
            bot.run_background_bot()
        self.assertIn("child output", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
