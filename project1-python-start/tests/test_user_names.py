"""이름 조회 중 다른 실행이 저장해도 기존 캐시를 훼손하지 않는지 검사합니다."""

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import slack_api_LLM_questions_UserName as names
from slack_runtime import FileLock


class NameCacheTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "names.json"
        self.initial = {"U1": {"name": "기존 이름", "fetched_at": 0}}
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        for replacement in (patch.dict(names.os.environ, {"SLACK_TOKEN": "test-token"}),
                            contextlib.redirect_stdout(io.StringIO())):
            replacement.__enter__()
            self.addCleanup(replacement.__exit__, None, None, None)

    def test_another_writer_during_lookup_keeps_both_updates(self):
        def fetch(*_):
            names.save_name_updates(self.path, {"U2": {"name": "다른 실행의 이름", "fetched_at": time.time()}})
            return "새 이름"

        with patch.object(names, "fetch_user_name", side_effect=fetch):
            self.assertEqual(names.get_user_names(["U1"], self.path), {"U1": "새 이름"})
        cache = names.load_name_cache(self.path)
        self.assertEqual(cache["U1"]["name"], "새 이름")
        self.assertEqual(cache["U2"]["name"], "다른 실행의 이름")

    def test_busy_cache_does_not_overwrite_or_prevent_current_answer(self):
        with FileLock(self.path.with_suffix(".lock")), \
             patch.object(names, "fetch_user_name", return_value="조회된 이름"):
            self.assertEqual(names.get_user_names(["U1"], self.path), {"U1": "조회된 이름"})
        self.assertEqual(names.load_name_cache(self.path), self.initial)

    def test_failed_replace_preserves_old_file_and_removes_temporary_file(self):
        with patch.object(names, "fetch_user_name", return_value="조회된 이름"), \
             patch.object(Path, "replace", side_effect=OSError("파일 잠김")):
            self.assertEqual(names.get_user_names(["U1"], self.path), {"U1": "조회된 이름"})
        self.assertEqual(names.load_name_cache(self.path), self.initial)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_fresh_cached_name_needs_no_api_call(self):
        names.save_name_updates(self.path, {"U1": {"name": "저장된 이름", "fetched_at": time.time()}})
        with patch.object(names, "fetch_user_name") as fetch:
            self.assertEqual(names.get_user_names(["U1", "U1"], self.path), {"U1": "저장된 이름"})
        fetch.assert_not_called()

    def test_quiet_lookup_keeps_cached_fallback_without_warnings(self):
        output = io.StringIO()
        with patch.object(names, "DEBUG", False), \
             patch.object(names, "fetch_user_name", side_effect=names.UserNameLookupError("권한 없음")), \
             contextlib.redirect_stdout(output):
            result = names.get_user_names(["U1", "U2"], self.path)
        self.assertEqual(result, {"U1": "기존 이름", "U2": "U2"})
        self.assertEqual(output.getvalue(), "")

    def test_quiet_lookup_with_invalid_cache_and_no_token_returns_id(self):
        self.path.write_text("invalid json", encoding="utf-8")
        output = io.StringIO()
        with patch.object(names, "DEBUG", False), patch.dict(names.os.environ, {"SLACK_TOKEN": ""}), \
             contextlib.redirect_stdout(output):
            result = names.get_user_names(["U1"], self.path)
        self.assertEqual(result, {"U1": "U1"})
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
