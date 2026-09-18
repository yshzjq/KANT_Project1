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

    def test_quiet_interactive_main_keeps_only_prompt_and_answer(self):
        for debug in (False, True):
            with self.subTest(debug=debug):
                client = Mock()
                client.chat.side_effect = [model_response('{"keywords":["공지"]}'), model_response("테스트 답변")]
                messages = [{"ts": "100.1", "text": "공지 내용입니다."}]
                questions = iter(["공지 내용은?", "종료"])
                output = io.StringIO()

                def sync(*, verbose):
                    self.assertEqual(verbose, debug)
                    if verbose:
                        print("Slack 변경 확인 로그")
                    return {"messages": messages}

                def ask(prompt):
                    print(prompt, end="")  # 실제 input처럼 프롬프트를 표시합니다.
                    return next(questions)

                with patch.object(chat, "DEBUG", debug), \
                     patch.object(chat, "QUESTION_EVALUATION", False), \
                     patch.object(chat, "ensure_slack_data", side_effect=sync), \
                     patch.object(chat, "Client", return_value=client), \
                     patch("builtins.input", side_effect=ask), contextlib.redirect_stdout(output):
                    chat.main()
                text = output.getvalue()
                if not debug:
                    self.assertEqual(text, "질문을 입력하면 답변합니다. 종료: 종료 / exit / quit / Ctrl+C\n"
                                     "\n질문: 테스트 답변\n\n질문: ")
                else:
                    self.assertIn("Slack 변경 확인 로그", text)
                    self.assertIn("대화기록 1개를 준비", text)
                    self.assertIn("답변을 기다리고 있습니다", text)
                    self.assertIn("테스트 답변", text)
                self.assertEqual(client.chat.call_count, 2)

    def test_quiet_interactive_no_context_still_displays_notice(self):
        client = Mock()
        client.chat.return_value = model_response('{"keywords":["일정"]}')
        output = io.StringIO()
        with patch.object(chat, "DEBUG", False), \
             patch.object(chat, "prepare_context", side_effect=chat.NoRelevantContext("관련 기록을 확인하지 못했습니다.")), \
             contextlib.redirect_stdout(output):
            answer = chat.answer_question("일정은?", [], client)
        self.assertEqual(output.getvalue(), answer + "\n")
        self.assertEqual(client.chat.call_count, 1)

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

    def test_same_timestamp_in_two_channels_stays_separate_in_search_and_trace(self):
        messages = [
            {"channel_id": "CCHAT", "channel_name": "질문잡담방", "ts": "100.1", "text": "학습 안내 질문"},
            {"channel_id": "CCHAT", "channel_name": "질문잡담방", "ts": "101.1", "thread_ts": "100.1", "text": "질문방 답글"},
            {"channel_id": "CNEWS", "channel_name": "공지방", "ts": "100.1", "text": "학습 안내 공지"},
            {"channel_id": "CNEWS", "channel_name": "공지방", "ts": "101.1", "thread_ts": "100.1", "text": "공지방 답글"},
        ]
        threads = chat.find_matching_threads(messages, ["학습 안내"])
        self.assertEqual({thread["thread_id"] for thread in threads}, {"CCHAT:100.1", "CNEWS:100.1"})
        self.assertTrue(all(len({m["channel_id"] for m in thread["messages"]}) == 1 for thread in threads))
        trace = {}
        with patch.object(chat, "get_user_names", return_value={}):
            context = chat.prepare_context(messages, ["학습 안내"], "학습 방식은?", trace=trace)
        self.assertIn("[채널: 질문잡담방 / ID: CCHAT]", context)
        self.assertIn("[채널: 공지방 / ID: CNEWS]", context)
        originals = {(item["channel_id"], item["ts"]): item["original_text"] for item in trace["selected_messages"]}
        self.assertEqual(originals, {(m["channel_id"], m["ts"]): m["text"] for m in messages})

    def test_matching_text_in_one_channel_does_not_select_same_timestamp_elsewhere(self):
        messages = [
            {"channel_id": "CCHAT", "ts": "100.1", "text": "인사"},
            {"channel_id": "CNEWS", "ts": "100.1", "text": "수업 준비물 안내"},
        ]
        threads = chat.find_matching_threads(messages, ["준비물"])
        self.assertEqual([thread["thread_id"] for thread in threads], ["CNEWS:100.1"])

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
