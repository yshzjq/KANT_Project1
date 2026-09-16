"""실제 모델·Slack 호출 없이 평가 모드의 결과 저장과 분기를 확인합니다."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import config
import main_ollama_chat as chat


def cases(*questions):
    return [{"type": "정상", "question": question, "expected_result": "기대 결과"} for question in questions]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        for replacement in (
            patch.object(chat, "BASE_DIR", self.path),
            patch.object(chat, "QUESTION_EVALUATION_DIR", "reports"),
            patch.object(chat, "Client", return_value=SimpleNamespace(ps=lambda: SimpleNamespace(models=[]))),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            replacement.__enter__()
            self.addCleanup(replacement.__exit__, None, None, None)

    def test_configured_questions_written_in_order_with_model_and_elapsed_time(self):
        count = len(config.QUESTION_LIST)
        messages = [{"ts": "100.1", "text": "안내"}]
        ticks = [tick for index in range(count) for tick in (index * 10, index * 10 + 2.84)]
        with patch.object(chat, "answer_question", return_value="첫 줄\n둘째 줄") as answer, \
             patch.object(chat.time, "perf_counter", side_effect=ticks):
            path = chat.run_question_evaluation(messages)
        report = path.read_text(encoding="utf-8")
        self.assertIn(f"`{chat.MODEL}`", report)
        self.assertEqual(report.count("**응답시간**"), count)
        self.assertEqual(report.split("## 집계")[0].count("2.84초"), count)
        self.assertIn("첫 줄\n둘째 줄", report)
        for number, item in enumerate(config.QUESTION_LIST, 1):
            section = report.split(f"## Q{number:02d}\n", 1)[1].split("\n---\n", 1)[0]
            self.assertIn(f"**질문**\n\n{item['question']}", section)
            self.assertIn(f"**유형**\n\n{item['type']}", section)
            self.assertIn(f"**기대 결과**\n\n{item['expected_result']}", section)
        self.assertEqual([call.args[0] for call in answer.call_args_list], [item["question"] for item in config.QUESTION_LIST])
        self.assertTrue(all(call.args[1] is messages for call in answer.call_args_list))

    def test_failed_question_recorded_and_following_question_still_runs(self):
        with patch.object(chat, "QUESTION_LIST", cases("첫 질문", "둘째 질문", "마지막 질문")), \
             patch.object(chat, "answer_question", side_effect=[SystemExit("검색 실패"), RuntimeError("연결 실패"), "정상 답변"]):
            path = chat.run_question_evaluation([])
        report = path.read_text(encoding="utf-8")
        self.assertIn("평가 실패: 검색 실패", report)
        self.assertIn("평가 실패: 연결 실패", report)
        self.assertIn("정상 답변", report)
        self.assertEqual(report.count("**응답시간**"), 3)

    def test_interrupt_preserves_completed_questions(self):
        with patch.object(chat, "QUESTION_LIST", cases("첫 질문", "둘째 질문")), \
             patch.object(chat, "answer_question", side_effect=["완료", KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                chat.run_question_evaluation([])
        report = next(self.path.rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("완료", report)
        self.assertIn("Q02", report)
        self.assertIn("사용자 중단", report)
        self.assertIn("## 집계", report)

    def test_true_runs_evaluation_without_prompt_false_writes_no_report(self):
        messages = [{"ts": "100.1", "text": "안내"}]
        for enabled in (True, False):
            with self.subTest(enabled=enabled), \
                 patch.object(chat, "QUESTION_EVALUATION", enabled), \
                 patch.object(chat, "load_latest_slack_data", return_value={"messages": messages}), \
                 patch.object(chat, "run_question_evaluation") as evaluate, \
                 patch.object(chat, "answer_question") as answer, \
                 patch("builtins.input", side_effect=["질문", "종료"]) as ask:
                chat.main()
                self.assertEqual(evaluate.call_count, int(enabled))
                self.assertEqual(ask.call_count, 0 if enabled else 2)
                self.assertEqual(answer.call_count, int(not enabled))
        self.assertEqual(list(self.path.rglob("*.md")), [])

    def test_second_run_keeps_previous_report(self):
        with patch.object(chat, "QUESTION_LIST", cases("질문")), \
             patch.object(chat, "answer_question", return_value="답변"):
            first = chat.run_question_evaluation([])
            second = chat.run_question_evaluation([])
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_invalid_case_is_rejected_before_any_model_call_or_report(self):
        invalid_cases = [
            [], ["문자열만 있는 이전 형식"],
            [{"type": "정상", "question": "질문"}],
            [{"type": "정상", "question": "질문", "expected_result": None}],
            [{"type": "정상", "question": "  ", "expected_result": "기대 결과"}],
        ]
        for invalid in invalid_cases:
            with self.subTest(cases=invalid), patch.object(chat, "QUESTION_LIST", invalid), \
                 patch.object(chat, "answer_question") as answer, patch.object(chat, "Client") as client:
                with self.assertRaisesRegex(ValueError, "QUESTION_LIST"):
                    chat.run_question_evaluation([])
                answer.assert_not_called()
                client.assert_not_called()
        self.assertEqual(list(self.path.rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
