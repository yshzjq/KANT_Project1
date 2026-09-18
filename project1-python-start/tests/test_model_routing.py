"""서로 다른 모델의 호출·보고서 매핑과 tokenizer 조회 실패를 검증합니다."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from ollama import ShowResponse

import main_ollama_chat as chat
from evaluation_metrics import render_model_metadata, tokenizer_metadata


def response(model, content):
    return SimpleNamespace(model=model, message=SimpleNamespace(content=content), done=True,
                           done_reason="stop", load_duration=500_000_000, eval_count=12,
                           eval_duration=2_000_000_000)


class TokenizerMetadataTests(unittest.TestCase):
    def test_sdk_and_http_metadata_preserve_identifiers_without_large_arrays(self):
        info = {"tokenizer.ggml.model": "gpt2", "tokenizer.ggml.pre": "qwen2",
                "tokenizer.ggml.bos_token_id": 0, "tokenizer.ggml.add_bos_token": False,
                "tokenizer.ggml.tokens": ["large-token-vocabulary"],
                "tokenizer.ggml.merges": ["large-merge-table"],
                "general.architecture": "qwen3"}
        for payload in (ShowResponse(model_info=info), {"model_info": info}):
            with self.subTest(payload=type(payload).__name__):
                client = Mock()
                client.show.return_value = payload
                result = tokenizer_metadata(client, "test:tag")
                self.assertEqual(result["metadata"], {
                    "tokenizer.ggml.model": "gpt2", "tokenizer.ggml.pre": "qwen2",
                    "tokenizer.ggml.bos_token_id": 0, "tokenizer.ggml.add_bos_token": False,
                })
                self.assertEqual(result["model"], "test:tag")
                client.show.assert_called_once_with("test:tag")

    def test_missing_metadata_does_not_guess_from_model_name(self):
        for payload in ({}, {"model_info": {}}, {"model_info": {"tokenizer.ggml.tokens": []}}):
            with self.subTest(payload=payload):
                client = Mock()
                client.show.return_value = payload
                result = tokenizer_metadata(client, "qwen3:test")
                self.assertEqual(result["metadata"], {})
                self.assertIn("없음", result["reason"])

    def test_lookup_error_records_reason_without_exposing_error_body(self):
        client = Mock()
        client.show.side_effect = RuntimeError("server-private-details")
        result = tokenizer_metadata(client, "test:tag")
        self.assertIn("RuntimeError", result["reason"])
        self.assertNotIn("server-private-details", str(result))

    def test_cloud_does_not_query_ollama_or_guess_tokenizer(self):
        client = Mock(provider="openai_responses")
        result = tokenizer_metadata(client, "cloud-model")
        client.show.assert_not_called()
        self.assertEqual(result["metadata"], {})
        self.assertIn("API가 실제 tokenizer", result["reason"])

    def test_partial_metadata_marks_missing_fields_and_escapes_table(self):
        client = Mock()
        client.show.return_value = {"model_info": {"tokenizer.ggml.model": "test|kind\nnext"}}
        snapshot = tokenizer_metadata(client, "test:tag")
        report = render_model_metadata("test:tag", "test:tag", {"test:tag": snapshot})
        self.assertIn("test&#124;kind next", report)
        self.assertIn("tokenizer.ggml.pre 누락", report)
        self.assertEqual(report.count("tokenizer 원본 메타데이터"), 1)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.client = Mock()
        self.client.ps.return_value = {"models": []}
        self.client.show.side_effect = lambda model: ShowResponse(model_info={
            "tokenizer.ggml.model": "test-tokenizer", "tokenizer.ggml.pre": model + "-pre",
        })
        self.client.chat.side_effect = lambda **kwargs: response(
            kwargs["model"], '{"keywords":["공지"]}' if "format" in kwargs else "안내 답변",
        )
        for context in (
            patch.object(chat, "MODEL", "answer:tag"),
            patch.object(chat, "KEYWORD_MODEL", "keywords:tag"),
            patch.object(chat, "BASE_DIR", self.root),
            patch.object(chat, "QUESTION_EVALUATION_DIR", "reports"),
            patch.object(chat, "QUESTION_LIST", [
                {"type": "정상", "question": "공지 내용은?", "expected_result": "안내"},
            ]),
            patch.object(chat, "Client", return_value=self.client),
            patch.object(chat, "prepare_context", return_value="관련 공지"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def report(self):
        return chat.run_question_evaluation([]).read_text(encoding="utf-8")

    def requested_models(self):
        return [item.kwargs["model"] for item in self.client.chat.call_args_list]

    def test_interactive_uses_keyword_model_only_for_keywords(self):
        messages = [{"ts": "100.1", "text": "공지"}]
        with patch.object(chat, "load_latest_slack_data", return_value={"messages": messages}), \
             patch("builtins.input", side_effect=["공지 내용은?", "종료"]):
            chat.run_interactive_chat(messages)
        self.assertEqual(self.requested_models(), ["keywords:tag", "answer:tag"])
        first, final = self.client.chat.call_args_list
        self.assertEqual(first.kwargs["format"], chat.KEYWORD_FORMAT)
        self.assertEqual(first.kwargs["options"]["num_predict"], 256)
        self.assertEqual(final.kwargs["options"]["num_predict"], chat.NUM_PREDICT)
        self.client.show.assert_not_called()  # 기록이 없는 직접 입력 모드에는 메타데이터 조회도 필요 없습니다.

    def test_standalone_keyword_extraction_defaults_to_keyword_model(self):
        chat.extract_search_keywords(self.client, "공지 내용은?")
        self.assertEqual(self.requested_models(), ["keywords:tag"])

    def test_explicit_models_override_each_stage_independently(self):
        trace = {}
        chat.answer_question("공지 내용은?", [], self.client, trace=trace,
                             model="override-answer", keyword_model="override-keywords")
        self.assertEqual(self.requested_models(), ["override-keywords", "override-answer"])
        self.assertEqual(trace["keyword_model"], "override-keywords")

    def test_report_maps_two_models_to_tokenizers_and_separate_statistics(self):
        report = self.report()
        self.assertEqual(self.requested_models(), ["keywords:tag", "answer:tag"])
        self.assertEqual(self.client.show.call_args_list, [call("keywords:tag"), call("answer:tag")])
        self.assertIn("KEYWORD_MODEL: `keywords:tag`", report)
        self.assertIn("MODEL: `answer:tag`", report)
        self.assertIn("| 검색어 생성 | keywords:tag | test-tokenizer | keywords:tag-pre |", report)
        self.assertIn("| 최종 답변 | answer:tag | test-tokenizer | answer:tag-pre |", report)
        self.assertIn("| 검색어 생성 | keywords:tag | 성공 |", report)
        self.assertIn("| 최종 답변 | answer:tag | 성공 |", report)
        summary = report.split("## 집계", 1)[1]
        self.assertIn("| keywords:tag | 1 / 1 |", summary)
        self.assertIn("| answer:tag | 1 / 1 |", summary)
        self.assertIn("요청 모델: `keywords:tag`", summary.split("#### 최종 답변", 1)[0])
        self.assertIn("6.00 tokens/s (n=1)", summary)

    def test_same_model_metadata_is_queried_once_for_entire_run(self):
        with patch.object(chat, "KEYWORD_MODEL", "answer:tag"), \
             patch.object(chat, "QUESTION_LIST", chat.QUESTION_LIST * 2):
            report = self.report()
        self.client.show.assert_called_once_with("answer:tag")
        self.assertEqual(report.count("tokenizer 원본 메타데이터"), 1)
        self.assertIn("| answer:tag | 4 / 4 |", report)

    def test_metadata_failure_keeps_answers_and_explains_unknown_tokenizer(self):
        self.client.show.side_effect = RuntimeError("offline")
        report = self.report()
        self.assertIn("**답변**\n\n안내 답변", report)
        self.assertIn("확인 불가: client.show() 조회 실패 (RuntimeError)", report)
        self.assertIn("최종 모델 답변 완료: 1 / 질문 시도 1", report)

    def test_keyword_failure_is_attributed_to_keyword_model_and_not_retried(self):
        self.client.chat.side_effect = RuntimeError("keyword unavailable")
        report = self.report()
        self.assertEqual(self.requested_models(), ["keywords:tag"])
        self.assertIn("| keywords:tag | 0 / 1 |", report)
        self.assertIn("| answer:tag | 0 / 0 |", report)
        self.assertIn("최종 답변 모델 호출은 실행되지 않았습니다", report)

    def test_final_failure_is_attributed_to_answer_model(self):
        self.client.chat.side_effect = [response("keywords:tag", '{"keywords":["공지"]}'),
                                        RuntimeError("answer unavailable")]
        report = self.report()
        self.assertIn("| keywords:tag | 1 / 1 |", report)
        self.assertIn("| answer:tag | 0 / 1 |", report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)

    def test_show_time_is_excluded_from_question_elapsed(self):
        clock = [0.0]

        def show(model):
            clock[0] += 100
            return {"model_info": {"tokenizer.ggml.model": "gpt2"}}

        def generate(**kwargs):
            clock[0] += 2
            return response(kwargs["model"], '{"keywords":["공지"]}' if "format" in kwargs else "답변")

        self.client.show.side_effect = show
        self.client.chat.side_effect = generate
        with patch.object(chat.time, "perf_counter", side_effect=lambda: clock[0]):
            report = self.report()
        self.assertIn("**응답시간**\n\n4.00초", report)


if __name__ == "__main__":
    unittest.main()
