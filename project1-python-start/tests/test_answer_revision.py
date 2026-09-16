"""날짜 혼동·긴 검색 구절·답변의 내부 표기 노출에 대한 회귀 테스트."""

import contextlib
import io
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main_ollama_chat as chat
from search_terms import expand_search_terms
from slack_dates import applies_to_window, question_window, resolve_relative_dates


class AnswerRevisionTests(unittest.TestCase):
    def test_relative_dates_are_resolved_at_source_date_and_keep_urls(self):
        text = "오늘 과제, 내일 시험. 이번 주 금요일까지. 다음 주 월요일부터. https://example.com/오늘"
        result = resolve_relative_dates(text, date(2026, 9, 10))
        self.assertIn("2026-09-10 과제", result)
        self.assertIn("2026-09-11 시험", result)
        self.assertIn("2026-09-11까지", result)
        self.assertIn("2026-09-14부터", result)
        self.assertIn("https://example.com/오늘", result)

    def test_current_period_excludes_old_daily_notice_and_includes_future_dated_notice(self):
        now = datetime(2026, 9, 15, tzinfo=chat.KST)
        ts = str(datetime(2026, 9, 10, tzinfo=chat.KST).timestamp())
        window = question_window("오늘 퀘스트가 있나요?", now)
        self.assertFalse(applies_to_window({"ts": ts, "text": "오늘 퀘스트 제출"}, window))
        self.assertTrue(applies_to_window({"ts": ts, "text": "9월 15일 퀘스트 제출"}, window))
        self.assertTrue(applies_to_window({"ts": ts, "text": "9/1 ~ 9/30 퀘스트 진행"}, window))
        self.assertFalse(applies_to_window({"ts": ts, "text": "https://example.com/9/15"}, window))

    def test_today_question_does_not_send_old_daily_tasks_to_model(self):
        now = datetime(2026, 9, 15, tzinfo=chat.KST)
        ts = str(datetime(2026, 9, 3, tzinfo=chat.KST).timestamp())
        with patch.object(chat, "get_user_names") as lookup, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(chat.NoRelevantContext) as result:
                chat.prepare_context(
                    [{"ts": ts, "text": "오늘 데일리 퀘스트는 6시 전까지 제출"}],
                    ["데일리", "퀘스트"], "오늘 데일리 퀘스트가 있나요?", now,
                )
        self.assertIn("오늘(2026-09-15)의 데일리 퀘스트 공지", str(result.exception))
        self.assertNotIn("6시", str(result.exception))
        lookup.assert_not_called()

    def test_current_post_with_past_deadline_is_not_a_current_task(self):
        now = datetime(2026, 9, 15, tzinfo=chat.KST)
        ts = str(now.timestamp())
        for question in ("오늘 시험은?", "다음 시험은?"):
            window = question_window(question, now)
            for text in ("어제 시험이 종료됐습니다.", "9월 14일 시험 결과 안내"):
                with self.subTest(question=question, text=text):
                    self.assertFalse(applies_to_window({"ts": ts, "text": text}, window))
        self.assertTrue(applies_to_window({"ts": ts, "text": "시험 공지"}, (now.date(), now.date())))

    def test_tomorrow_and_next_week_are_filtered_across_year_boundary(self):
        now = datetime(2026, 12, 31, tzinfo=chat.KST)
        ts = str(now.timestamp())
        tomorrow = question_window("내일 과제는?", now)
        self.assertEqual(tomorrow, (date(2027, 1, 1), date(2027, 1, 1)))
        self.assertFalse(applies_to_window({"ts": ts, "text": "오늘 과제 제출"}, tomorrow))
        self.assertTrue(applies_to_window({"ts": ts, "text": "내일 과제 제출"}, tomorrow))
        self.assertEqual(question_window("다음 주 시험은?", now), (date(2027, 1, 4), date(2027, 1, 10)))
        self.assertIn("2027-01-01의 과제", chat.missing_context_answer("내일 과제는?", now))

    def test_long_keyword_phrases_split_and_keep_numeric_clues(self):
        terms = expand_search_terms(
            ["기초 강화 대상자는", "시험 날짜와 범위"],
            "수업 자료를 100% 이해하지 못해도 되나요?",
        )
        for term in ("기초", "강화", "대상자", "시험", "범위", "100%"):
            self.assertIn(term, terms)
        self.assertNotIn("되나요", terms)
        self.assertEqual(len(terms), len(set(terms)))

    def test_spacing_differences_and_parent_reply_clues_match(self):
        messages = [
            {"ts": "100.1", "text": "기초강화 대상 안내"},
            {"ts": "101.1", "thread_ts": "100.1", "text": "퀘스트를 제출해주세요."},
            {"ts": "200.1", "text": "기초 강화 대상 안내"},
        ]
        threads = chat.find_matching_threads(messages, ["기초 강화", "퀘스트"])
        self.assertEqual(threads[0]["thread_id"], "100.1")
        self.assertEqual(len(threads[0]["messages"]), 2)
        self.assertGreater(threads[0]["score"], threads[1]["score"])

    def test_rare_term_from_original_question_beats_broad_model_synonyms(self):
        messages = [
            {"ts": "100.1", "text": "본격적인 수업이 시작하기 전에는?"},
            {"ts": "101.1", "thread_ts": "100.1", "text": "개념 중심 보완 학습을 해주세요."},
            {"ts": "200.1", "text": "수업 시작 전 공부와 사전 실습 안내"},
        ]
        threads = chat.find_matching_threads(
            messages, ["본격", "시작", "공부", "사전", "수업"],
            "수업이 본격적으로 시작하기 전에 무엇을 안내했나요?",
        )
        self.assertEqual(threads[0]["thread_id"], "100.1")

    def test_unknown_weekly_assignment_does_not_blame_question_or_claim_no_task(self):
        answer = chat.missing_context_answer(
            "이번 주까지 반드시 해야 하는 과제를 정리해주세요.",
            datetime(2026, 9, 15, tzinfo=chat.KST),
        )
        self.assertIn("이번 주(2026-09-14 ~ 2026-09-20)의 과제와 제출 기한을", answer)
        self.assertIn("확인하지 못했습니다", answer)
        self.assertNotIn("없습니다", answer)

    def test_metadata_and_link_ids_do_not_create_false_search_matches(self):
        messages = [{"ts": "100.1", "user": "U_OLLAMA", "text": "<https://example.com/ollama|참고 링크>"}]
        self.assertEqual(chat.find_matching_threads(messages, ["ollama", "100.1"]), [])

    def test_rare_numeric_clue_beats_many_generic_learning_messages(self):
        messages = [{"ts": "100.1", "text": "100% 이해하려 하지 말고 용어를 체크하세요."}]
        messages += [{"ts": f"{200 + n}.1", "text": "수업 학습 자료 안내"} for n in range(30)]
        threads = chat.find_matching_threads(messages, ["100%", "수업", "학습", "자료"])
        self.assertEqual(threads[0]["thread_id"], "100.1")

    def test_korean_today_and_week_use_runtime_clock_at_utc_date_boundary(self):
        now = datetime(2026, 9, 13, 16, 30, tzinfo=timezone.utc)
        # UTC 일요일 밤은 KST 월요일 새벽입니다.
        prompt = chat.make_prompt("오늘 과제는?", "작성: 2026-09-03\n오늘 과제 제출", now)
        self.assertIn("오늘 = 2026-09-14", prompt)
        self.assertIn("이번 주 = 2026-09-14 ~ 2026-09-20", prompt)
        self.assertIn("작성: 2026-09-03", prompt)
        self.assertIn("각각의 작성일 기준", prompt)

    def test_past_and_future_dated_announcements_remain_available(self):
        now = datetime(2026, 9, 15, tzinfo=chat.KST)
        info = "작성: 2026-09-10\n9월 15일 퀘스트 마감입니다."
        prompt = chat.make_chat_messages("오늘 퀘스트는?", info, now)[1]["content"]
        self.assertIn(info, prompt)
        self.assertIn("오늘 = 2026-09-15", prompt)

    def test_excerpt_keeps_related_paragraph_qualification_and_thread(self):
        messages = [
            {"ts": "100.1", "text": "자료를 100% 이해해야 하나요?"},
            {"ts": "101.1", "thread_ts": "100.1", "text": (
                "주변 설명\n" * 200
                + "지금 100% 이해하려 하지 말고 모르는 용어를 기록하세요.\n"
                + "다만 어렵다고 아예 넘기지는 마세요.\n"
                + "다른 설명\n" * 200
            )},
        ]
        thread = chat.find_matching_threads(messages, ["100%", "용어"])[0]
        excerpt = chat.excerpt_messages(thread, 450)
        self.assertEqual([message["ts"] for message in excerpt], ["100.1", "101.1"])
        self.assertIn("100% 이해하려 하지 말고", excerpt[1]["text"])
        self.assertIn("다만 어렵다고", excerpt[1]["text"])
        self.assertEqual(excerpt[1]["thread_ts"], "100.1")
        self.assertIn("발췌", excerpt[1]["text"])

    def test_cleanup_removes_footer_label_and_keeps_links_dates_and_numbers(self):
        original = (
            "[ollama 답변]\n제공된 자료들에서는 2026-09-03에 100% 이해할 필요가 없다고 안내했습니다.\n\n"
            "[학습 자료](https://example.com/notes?ts=1784510196.819419)\n\n"
            "**근거 ts:** 1784510196.819419, 1786697100.363529\n"
        )
        answer = chat.clean_answer(original)
        self.assertNotIn("근거 ts", answer)
        self.assertNotIn("ollama 답변", answer)
        self.assertNotIn("제공된 자료", answer)
        self.assertEqual(chat.clean_answer("제공된 자료는 참고용입니다."), "확인한 Slack 기록은 참고용입니다.")
        self.assertIn("2026-09-03", answer)
        self.assertIn("100%", answer)
        self.assertIn("https://example.com/notes?ts=1784510196.819419", answer)

    def test_console_and_return_value_use_the_same_clean_answer(self):
        client = Mock()
        client.chat.return_value = SimpleNamespace(message=SimpleNamespace(content="답변입니다.\n근거 ts: 100.123"))
        output = io.StringIO()
        with patch.object(chat, "Client", return_value=client), \
             patch.object(chat, "extract_search_keywords", return_value=["자료"]), \
             patch.object(chat, "prepare_context", return_value="자료 안내"), \
             contextlib.redirect_stdout(output):
            answer = chat.answer_question("질문", [])
        self.assertEqual(answer, "답변입니다.")
        self.assertIn(answer, output.getvalue())
        self.assertNotIn("[Ollama 답변]", output.getvalue())
        self.assertNotIn("근거 ts", output.getvalue())


if __name__ == "__main__":
    unittest.main()
