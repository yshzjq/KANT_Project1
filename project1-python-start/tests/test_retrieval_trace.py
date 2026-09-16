"""검색 진단의 근거·실패 기록을 검증합니다. 모델과 Slack API는 호출하지 않습니다."""

import contextlib
import copy
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main_ollama_chat as chat
from evaluation_metrics import render_retrieval
from slack_dates import explain_window


NOW = datetime(2026, 9, 16, 10, tzinfo=chat.KST)


def message(day, text, **extra):
    return {"ts": str(datetime(2026, 9, day, 9, tzinfo=chat.KST).timestamp()),
            "text": text, "user": "U_TEST", **extra}


def response(text):
    return SimpleNamespace(message=SimpleNamespace(content=text), done=True, done_reason="stop")


class RetrievalTraceTests(unittest.TestCase):
    def setUp(self):
        for replacement in (
            patch.object(chat, "DEBUG", False),
            patch.object(chat, "get_user_names", return_value={"U_TEST": "테스트 작성자"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            replacement.__enter__()
            self.addCleanup(replacement.__exit__, None, None, None)
        self.clock = patch.object(chat, "datetime", wraps=datetime)
        clock = self.clock.start()
        clock.now.return_value = NOW
        self.addCleanup(self.clock.stop)

    def test_trace_keeps_raw_and_expanded_keywords_and_exact_request_without_changing_it(self):
        messages = [message(15, "기초강화 대상자는 퀘스트를 제출합니다.")]
        before = copy.deepcopy(messages)
        requests = []
        trace = {}
        for diagnostic in (None, trace):
            client = Mock()
            client.chat.side_effect = [response('{"keywords":[" 기초 강화 대상자는 ", "제출"]}'), response("답변")]
            answer = chat.answer_question("기초 강화 대상자는?", messages, client, trace=diagnostic)
            self.assertEqual(answer, "답변")
            self.assertEqual(client.chat.call_count, 2)
            requests.append(client.chat.call_args.kwargs)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(trace["raw_keywords"], [" 기초 강화 대상자는 ", "제출"])
        self.assertIn("대상자", trace["search_keywords"])
        self.assertEqual(trace["final_messages"], requests[1]["messages"])
        self.assertIn(trace["context"], requests[1]["messages"][1]["content"])
        self.assertEqual(trace["selected_messages"][0]["original_text"], messages[0]["text"])
        self.assertEqual(messages, before)
        self.assertEqual(trace["candidates"][0]["decision"], "선택")

    def test_date_exclusion_has_message_level_evidence_and_no_false_current_topic(self):
        old = message(10, "오늘 퀘스트 제출")
        unrelated_reply = message(16, "감사합니다", thread_ts=old["ts"])
        current = message(15, "9월 16일 퀘스트 제출")
        trace = {}
        context = chat.prepare_context([old, unrelated_reply, current], ["퀘스트"], "오늘 퀘스트는?", NOW, trace=trace)
        candidates = {item["thread_id"]: item for item in trace["candidates"]}
        excluded = candidates[old["ts"]]
        self.assertEqual(trace["date_window"], ["2026-09-16", "2026-09-16"])
        self.assertEqual(excluded["decision"], "날짜 제외")
        self.assertFalse(excluded["date_checks"][0]["matches"])
        self.assertEqual(excluded["date_checks"][0]["resolved_dates"], ["2026-09-10"])
        self.assertFalse(excluded["date_checks"][1]["matches"])
        self.assertIn("검색 주제가 없음", excluded["date_checks"][1]["reason"])
        self.assertEqual(excluded["date_checks"][1]["matched_keywords"], [])
        self.assertIn("같은 메시지", excluded["reason"])
        self.assertIn(current["ts"], context)
        self.assertNotIn(old["ts"], context)

    def test_date_reasons_distinguish_explicit_dates_intervals_and_posted_date(self):
        window = (NOW.date(), NOW.date())
        cases = [
            (message(16, "어제 퀘스트 마감"), False, "게시일로 대체하지 않음"),
            (message(15, "퀘스트 공지"), False, "게시일 사용: 질문 기간 밖"),
            (message(16, "퀘스트 공지"), True, "게시일 사용: 질문 기간 안"),
            (message(10, "9/1 ~ 9/30 퀘스트 진행"), True, "질문 기간과 겹침"),
        ]
        for source, matches, reason in cases:
            with self.subTest(source=source):
                decision = explain_window(source, window)
                self.assertEqual(decision["matches"], matches)
                self.assertIn(reason, decision["reason"])

    def test_no_context_retains_exclusions_without_calling_final_model(self):
        client = Mock()
        client.chat.return_value = response('{"keywords":["퀘스트"]}')
        trace = {}
        chat.answer_question("오늘 퀘스트는?", [message(10, "오늘 퀘스트 제출")], client, trace=trace)
        self.assertEqual(client.chat.call_count, 1)
        self.assertEqual(trace["candidates"][0]["decision"], "날짜 제외")
        self.assertEqual(trace["selected_messages"], [])
        self.assertNotIn("final_messages", trace)
        self.assertIn("확인하지 못했습니다", trace["no_context_reason"])
        report = render_retrieval(trace)
        self.assertIn("최종 답변 모델 미호출 사유", report)
        self.assertIn("본문 날짜·기간이 질문 기간 밖임", report)

    def test_score_exclusion_is_not_reported_as_a_date_exclusion(self):
        trace = {}
        messages = [message(10, "Ollama 퀘스트 제출"), message(11, "Ollama")]
        chat.prepare_context(messages, ["Ollama", "퀘스트", "제출"], "질문", NOW, trace=trace)
        low = next(item for item in trace["candidates"] if item["thread_id"] == messages[1]["ts"])
        self.assertEqual(low["decision"], "점수 제외")
        self.assertIsNone(low["date_passed"])
        self.assertLess(low["score"], trace["score_cutoff"])

    def test_length_exclusion_after_author_lookup_identifies_the_correct_stage(self):
        sources = [message(16, "Ollama 안내", user="U_LONG"), message(15, "Ollama 안내")]
        trace = {}
        with patch.object(chat, "get_user_names", return_value={"U_LONG": "이름" * 10000}):
            chat.prepare_context(sources, ["ollama"], "질문", NOW, trace=trace)
        too_long = next(item for item in trace["candidates"] if item["thread_id"] == sources[0]["ts"])
        self.assertEqual(too_long["decision"], "길이 제외")
        self.assertIn("작성자 이름 반영 후", too_long["reason"])
        self.assertEqual([item["ts"] for item in trace["selected_messages"]], [sources[1]["ts"]])

    def test_excerpt_preserves_original_and_separately_records_transformed_context(self):
        original = "주변 설명\n" * 300 + "오늘 Ollama 퀘스트 제출\n다만 필수는 아닙니다.\n" + "끝부분 설명\n" * 300
        trace = {}
        chat.prepare_context([message(15, original)], ["ollama"], "질문", NOW, trace=trace)
        self.assertTrue(trace["selected_messages"][0]["excerpted"])
        self.assertEqual(trace["selected_messages"][0]["original_text"], original)
        self.assertIn("2026-09-15 Ollama", trace["context"])
        self.assertIn("다만 필수는 아닙니다", trace["context"])
        self.assertNotIn(original, trace["context"])

    def test_report_retains_trace_when_final_call_fails_or_is_interrupted(self):
        for error in (RuntimeError("연결 실패"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                client = Mock()
                client.ps.return_value = {"models": []}
                client.chat.side_effect = [response('{"keywords":["퀘스트"]}'), error]
                with patch.object(chat, "BASE_DIR", Path(folder)), \
                     patch.object(chat, "QUESTION_EVALUATION_DIR", "reports"), \
                     patch.object(chat, "QUESTION_LIST", [{"type": "정상", "question": "퀘스트는?", "expected_result": "비공개 평가 기준"}]), \
                     patch.object(chat, "Client", return_value=client):
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            chat.run_question_evaluation([message(15, "퀘스트 제출 안내")])
                    else:
                        chat.run_question_evaluation([message(15, "퀘스트 제출 안내")])
                report = next(Path(folder).rglob("*.md")).read_text(encoding="utf-8")
                self.assertIn("선택된 Slack 원문", report)
                self.assertIn("퀘스트 제출 안내", report)
                self.assertIn("최종 답변 호출 중", report)
                self.assertIn('"role": "system"', report)
                self.assertIn("SHA-256", report)
                self.assertIn("## 집계", report)
                self.assertNotIn("비공개 평가 기준", str(client.chat.call_args.kwargs))

    def test_invalid_keywords_leave_partial_trace_and_do_not_invent_empty_search(self):
        client = Mock()
        client.chat.return_value = response("JSON 아님")
        trace = {}
        with self.assertRaises(SystemExit):
            chat.answer_question("질문", [], client, trace=trace)
        self.assertEqual(trace["stage"], "검색어 생성 중")
        self.assertNotIn("candidates", trace)
        self.assertNotIn("search_keywords", trace)
        self.assertIn("해석 전 실패·중단", render_retrieval(trace))


if __name__ == "__main__":
    unittest.main()
