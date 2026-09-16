"""질문별 문서 ID 없이 검색 주제·문의 경로·날짜 연결을 검증합니다."""
import contextlib
import io
import unittest
from datetime import date, datetime
from unittest.mock import patch

import main_ollama_chat as chat
from search_terms import expand_search_terms
from slack_dates import explain_window, question_window


NOW = datetime(2026, 9, 16, 9, tzinfo=chat.KST)


def message(day, text, **extra):
    return {"ts": str(datetime(2026, 9, day, 9, tzinfo=chat.KST).timestamp()), "text": text, **extra}


class RankingAndDateTests(unittest.TestCase):
    def test_upcoming_schedule_paraphrases_exclude_past_exam(self):
        for question in ("다음 시험 날짜는?", "다음에 치를 시험은 언제인가요?",
                         "다음번 볼 평가는 언제인가요?", "다가오는 과제 일정은?"):
            with self.subTest(question=question):
                window = question_window(question, NOW)
                self.assertEqual(window, (NOW.date(), date.max))
                self.assertFalse(explain_window(message(10, "내일 오후 시험 예정"), window,
                                                keywords=["시험"])["matches"])
                self.assertTrue(explain_window(message(10, "9/18 시험 예정"), window,
                                               keywords=["시험"])["matches"])

    def test_upcoming_modifier_preserves_explicit_period_and_non_schedule_questions(self):
        self.assertEqual(question_window("이번 주에 치를 다음 시험은?", NOW),
                         (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertIsNone(question_window("다음 코드를 보고 시험 점수 계산 방법을 알려주세요", NOW))
        self.assertIsNone(question_window("교재 이해 후 다음 학습으로 넘어가도 되나요?", NOW))

    def test_generic_usage_phrase_does_not_outrank_named_channel(self):
        sources = [message(1, "프로젝트알림방에는 프로젝트 일정과 변경 사항을 올립니다."),
                   message(15, "API는 수업 용도로 사용하고 사적인 용도로 사용하는 것은 금지합니다.")]
        question = "프로젝트알림방은 어떤 용도로 사용하는 채널인가요?"
        terms = expand_search_terms(["프로젝트알림방", "용도", "사용"], question)
        self.assertEqual(chat.find_matching_threads(sources, terms, question)[0]["thread_id"], sources[0]["ts"])

    def test_question_destination_prefers_invitation_over_technical_question(self):
        sources = [message(1, "궁금한 내용은 이 채널에 질문을 올려주세요. 튜터를 태그해 주시면 답변드립니다."),
                   message(15, "수업 질문입니다. GPU 캐시의 원리가 궁금합니다. 답변: 캐시는 메모리를 재사용합니다.")]
        for question in ("수업 질문은 어디에 올리나요?", "모르는 내용은 어느 채널에 문의하면 되나요?"):
            with self.subTest(question=question):
                terms = expand_search_terms(["수업", "질문"], question)
                self.assertEqual(chat.find_matching_threads(sources, terms, question)[0]["thread_id"], sources[0]["ts"])

    def test_tool_names_match_korean_and_english_without_losing_reply(self):
        root = message(1, "주피터 노트북 대신 구글 콜랩 사용해도 되나요?")
        reply = message(2, "네, 가능합니다.", thread_ts=root["ts"])
        other = message(15, "Google Colaboratory 파일 다운로드 안내")
        question = "Jupyter Notebook 대신 Google Colab을 써도 되나요?"
        terms = expand_search_terms(["Jupyter", "Google Colab"], question)
        first = chat.find_matching_threads([root, reply, other], terms, question)[0]
        self.assertEqual(first["thread_id"], root["ts"])
        self.assertIn(reply, first["messages"])

    def test_explicit_deadline_beats_broad_week_for_same_topic(self):
        source = message(9, "다음 주 과제 안내\n9/11까지 과제를 진행하고, 최종 9/14까지 과제를 제출하세요.")
        window = (NOW.date(), NOW.date())
        for keywords in (None, ["과제", "제출"]):
            with self.subTest(keywords=keywords):
                result = explain_window(source, window, keywords=keywords)
                self.assertFalse(result["matches"])
                self.assertIn("구체 날짜", result["reason"])
                self.assertNotIn("2026-09-20", result["resolved_dates"])

    def test_unrelated_date_does_not_make_old_assignment_current(self):
        source = message(16, "9/10 퀘스트 제출 마감입니다.\n오늘 점심 안내입니다.")
        self.assertFalse(explain_window(source, (NOW.date(), NOW.date()), keywords=["퀘스트", "제출"])["matches"])

    def test_unrelated_specific_date_does_not_erase_current_week_assignment(self):
        source = message(16, "이번 주 퀘스트 진행 안내\n9/11 세미나 종료 안내")
        result = explain_window(source, (NOW.date(), NOW.date()), keywords=["퀘스트"])
        self.assertTrue(result["matches"])
        self.assertIn("2026-09-14", result["resolved_dates"])

    def test_date_only_line_inherits_immediate_heading_but_not_another_topic(self):
        for text, expected in (("퀘스트 제출 안내\n9/16까지 제출해주세요.", True),
                               ("퀘스트 제출 안내\n오늘 점심 안내", False),
                               ("퀘스트 제출 안내\n\n9/16까지 제출해주세요.", False)):
            with self.subTest(text=text):
                result = explain_window(message(15, text), (NOW.date(), NOW.date()), keywords=["퀘스트"])
                self.assertEqual(result["matches"], expected)

    def test_current_reply_does_not_drag_expired_parent_into_context(self):
        root = message(10, "오늘 퀘스트 A 제출 마감")
        reply = message(16, "오늘 퀘스트 B 제출 안내", thread_ts=root["ts"])
        trace = {}
        with patch.object(chat, "get_user_names", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            context = chat.prepare_context([root, reply], ["퀘스트"], "오늘 퀘스트는?", NOW, trace=trace)
        self.assertIn("퀘스트 B", context)
        self.assertNotIn("퀘스트 A", context)
        self.assertEqual([item["ts"] for item in trace["selected_messages"]], [reply["ts"]])

    def test_today_generic_confirmation_does_not_pass_submission_topic(self):
        result = explain_window(message(16, "확인 감사합니다"), (NOW.date(), NOW.date()), keywords=["제출"])
        self.assertFalse(result["matches"])


if __name__ == "__main__":
    unittest.main()
