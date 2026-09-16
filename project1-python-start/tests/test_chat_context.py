"""검색·자료 선택을 가상 메시지와 가짜 모델 응답으로 확인합니다."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main_ollama_chat as chat


def model_response(content):
    return SimpleNamespace(message=SimpleNamespace(content=content))


class ChatContextTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_keyword_cleanup_preserves_order_and_removes_duplicates(self):
        client = Mock()
        client.chat.return_value = model_response(
            '{"keywords": [" Ollama ", "ollama", "특강", " "]}'
        )
        self.assertEqual(chat.extract_search_keywords(client, "질문"), ["ollama", "특강", "질문"])
        self.assertEqual(client.chat.call_args.kwargs["format"], chat.KEYWORD_FORMAT)

    def test_invalid_keyword_responses_stop(self):
        for content in ("not json", '{"keywords": [1]}', '{"keywords": []}'):
            with self.subTest(content=content):
                client = Mock()
                client.chat.return_value = model_response(content)
                with self.assertRaises(SystemExit):
                    chat.extract_search_keywords(client, "질문")

    def test_single_message_match_score_then_recency_determine_order(self):
        messages = [
            {"ts": "100.1", "text": "Ollama 특강"},
            {"ts": "200.1", "text": "Ollama"},
            {"ts": "201.1", "thread_ts": "200.1", "text": "Ollama"},
            {"ts": "202.1", "thread_ts": "200.1", "text": "특강"},
            {"ts": "300.1", "text": "Ollama 특강"},
        ]
        threads = chat.find_matching_threads(messages, ["ollama", "특강"])
        self.assertEqual([thread["thread_id"] for thread in threads], ["300.1", "100.1", "200.1"])
        self.assertEqual(threads[0]["score"], threads[1]["score"])
        self.assertGreater(threads[1]["score"], threads[2]["score"])
        self.assertEqual(len(threads[2]["messages"]), 3)

    def test_long_thread_is_skipped_and_short_thread_keeps_parent_and_reply(self):
        messages = [
            {"ts": "300.1", "text": "Ollama " + "긴 본문" * 10000},
            {"ts": "100.1", "text": "Ollama 안내", "user": "U1"},
            {"ts": "101.1", "thread_ts": "100.1", "text": "관련 답글", "user": "U2"},
        ]
        threads = chat.find_matching_threads(messages, ["ollama"])
        info, selected, skipped = chat.select_context(threads, "질문")
        self.assertEqual([message["ts"] for message in selected], ["100.1", "101.1"])
        self.assertEqual(skipped, 1)
        self.assertIn("관련 답글", info)
        self.assertNotIn("300.1", info)

    def test_only_selected_authors_are_looked_up(self):
        messages = [
            {"ts": "300.1", "text": "Ollama " + "긴 본문" * 10000, "user": "U_OTHER"},
            {"ts": "100.1", "text": "Ollama 안내", "user": "U1"},
        ]
        with patch.object(chat, "get_user_names", return_value={"U1": "작성자"}) as names:
            info = chat.prepare_context(messages, ["ollama"], "질문")
        self.assertEqual(list(names.call_args.args[0]), ["U1"])
        self.assertIn("작성자 (ID: U1)", info)

    def test_author_names_are_included_in_second_length_check(self):
        messages = [
            {"ts": "200.1", "text": "Ollama 안내", "user": "U1"},
            {"ts": "100.1", "text": "Ollama 자료", "user": "U2"},
        ]
        with patch.object(chat, "get_user_names", return_value={"U1": "이름" * 10000, "U2": "작성자"}):
            info = chat.prepare_context(messages, ["ollama"], "질문")
        self.assertNotIn("200.1", info)
        self.assertIn("100.1", info)
        self.assertIn("작성자 (ID: U2)", info)

    def test_answer_contains_original_links_authors_and_separate_system_rules(self):
        messages = [{
            "ts": "100.1", "user": "U1",
            "text": "Ollama <https://example.com/notes?a=1&b=2|자료>",
            "files": [{"name": "자료.pdf", "permalink": "https://example.com/file"}],
        }]
        client = Mock()
        client.chat.side_effect = [model_response('{"keywords": ["Ollama"]}'), model_response("답변")]
        with patch.object(chat, "Client", return_value=client), \
             patch.object(chat, "get_user_names", return_value={"U1": "작성자"}):
            chat.answer_question("질문", messages)
        self.assertEqual(client.chat.call_count, 2)
        sent = client.chat.call_args.kwargs["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertTrue(sent[0]["content"].startswith(chat.SYSTEM_PROMPT))
        self.assertIn("현재 기준 시각", sent[0]["content"])
        self.assertEqual(sent[1]["role"], "user")
        for text in ("질문", "작성자 (ID: U1)", "ts: 100.1", "https://example.com/notes?a=1&b=2", "https://example.com/file"):
            self.assertIn(text, sent[1]["content"])

    def test_no_matching_thread_answers_uncertainty_without_author_lookup(self):
        client = Mock()
        client.chat.side_effect = [
            model_response('{"keywords": ["unmatched_topic_zz"]}'),
            model_response("관련 안내를 확인하지 못했습니다."),
        ]
        with patch.object(chat, "Client", return_value=client), patch.object(chat, "get_user_names") as names:
            answer = chat.answer_question("질문", [{"ts": "100.1", "text": "Ollama"}])
        names.assert_not_called()
        self.assertEqual(answer, "관련 안내를 검색한 Slack 기록에서 확인하지 못했습니다.")
        self.assertEqual(client.chat.call_count, 1)


if __name__ == "__main__":
    unittest.main()
