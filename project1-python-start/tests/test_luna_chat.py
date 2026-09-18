"""실제 OpenAI SDK + 가짜 HTTP 응답으로 클라우드 요청·보고서·실패 처리를 검증합니다."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import httpx
from openai import OpenAI

import main_luna_chat as luna
import main_ollama_chat as shared


def response(text, *, status="completed", reason=None, refusal=None, usage=True):
    content = [{"type": "refusal", "refusal": refusal}] if refusal else [
        {"type": "output_text", "text": text, "annotations": []},
    ]
    return {
        "id": "resp_test", "object": "response", "created_at": 1789516800,
        "status": status, "model": "cloud-test-snapshot", "error": None,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                    "status": "completed" if status == "completed" else "incomplete", "content": content}],
        "usage": {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52,
                  "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}} if usage else None,
    }


class LunaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.requests = []
        self.responses = []
        self.messages = [{"ts": "1789516800.1", "text": "테스트 공지는 질문잡담방에 올립니다.", "user": "U_TEST"}]

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.requests.append(json.loads(request.content))
            code, body = self.responses.pop(0)
            return httpx.Response(code, json=body)

        # test-only 키와 MockTransport를 사용합니다. 인터넷·로컬 Ollama 서버 모두 호출하지 않습니다.
        api = OpenAI(api_key="test-key-not-real", base_url="https://api.openai.com/v1", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(api.close)
        self.backend = luna.LunaClient(api, model="cloud-test-model")
        self.stdout = io.StringIO()
        for context in (
            patch.object(shared, "BASE_DIR", self.root),
            patch.object(shared, "QUESTION_EVALUATION_DIR", "reports"),
            patch.object(shared, "QUESTION_LIST", [{"type": "테스트유형", "question": "테스트 공지는 어디에 올리나요?",
                                                   "expected_result": "모델에 보내면 안 되는 평가 기준"}]),
            patch.object(shared, "get_user_names", return_value={"U_TEST": "테스트 담당자"}),
            patch.object(shared, "Client", side_effect=AssertionError("클라우드 경로에서 Ollama 연결 생성")),
            contextlib.redirect_stdout(self.stdout),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def queue(self, *items):
        self.responses.extend((200, item) for item in items)

    def report(self, **kwargs):
        return shared.run_question_evaluation(self.messages, backend=self.backend, **kwargs).read_text(encoding="utf-8")

    def local_keyword_backend(self):
        local, observer = Mock(), Mock()
        with patch.object(shared, "Client", side_effect=[local, observer]) as factory:
            backend = luna.OllamaKeywordClient(model="local-keywords:tag", host="http://local.test:11434")
        self.assertEqual(factory.call_args_list, [call(host="http://local.test:11434", timeout=180),
                                                call(host="http://local.test:11434", timeout=5)])
        local.chat.return_value = SimpleNamespace(
            model=backend.model, message=SimpleNamespace(content='{"keywords":["테스트","공지"]}'),
            done=True, done_reason="stop", load_duration=500_000_000,
            eval_count=12, eval_duration=2_000_000_000,
        )
        observer.show.return_value = {"model_info": {"tokenizer.ggml.model": "gpt2", "tokenizer.ggml.pre": "qwen2"}}
        observer.ps.return_value = {"models": [{
            "model": backend.model, "size": 2 * 1024**3, "size_vram": 1024**3,
            "digest": "local-keyword-digest", "context_length": 4096,
            "details": {"quantization_level": "Q4_K_M"},
        }]}
        return backend

    def keywords(self, **kwargs):
        return response('{"keywords":["테스트","공지"]}', **kwargs)

    def test_sdk_sends_same_prompts_and_schema_with_cloud_settings_and_real_usage(self):
        self.queue(self.keywords(), response("[ollama 답변]\n질문잡담방에 올립니다.\n근거 ts: 100.1"))
        local_model = shared.MODEL
        report = self.report()
        self.assertEqual(shared.MODEL, local_model)
        first, final = self.requests
        self.assertEqual(first["input"][0]["content"], shared.KEYWORD_PROMPT)
        self.assertEqual(first["text"]["format"]["schema"], shared.KEYWORD_FORMAT)
        self.assertTrue(first["text"]["format"]["strict"])
        self.assertTrue(final["input"][0]["content"].startswith(shared.SYSTEM_PROMPT))
        self.assertIn("테스트 공지는 질문잡담방에 올립니다", final["input"][1]["content"])
        self.assertEqual([item["max_output_tokens"] for item in self.requests], [256, 512])
        for request in self.requests:
            self.assertEqual(request["model"], "cloud-test-model")
            self.assertEqual(request["reasoning"], {"effort": "none"})
            self.assertFalse(request["store"])
            self.assertFalse(request["stream"])
            self.assertEqual(request["tools"], [])
            self.assertEqual(request["tool_choice"], "none")
            self.assertNotIn("num_ctx", request)
            self.assertNotIn("temperature", request)
            self.assertNotIn("모델에 보내면 안 되는", str(request))
            self.assertNotIn("테스트유형", str(request))
        self.assertNotIn("Ollama 서버:", report)
        self.assertNotIn("test-key-not-real", report)
        self.assertIn("**답변**\n\n질문잡담방에 올립니다.", report)
        self.assertIn("근거 ts: 100.1", report)
        self.assertIn("cloud-test-snapshot", report)
        self.assertIn('"status": "completed"', report)
        self.assertIn("40.00 tokens (n=1)", report)
        self.assertIn("12.00 tokens (n=1)", report)
        self.assertIn("10.00 tokens (n=1)", report)
        self.assertIn("0.00 tokens (n=1)", report)  # 실제로 받은 추론 토큰 0은 유효합니다.
        self.assertIn("API가 eval_duration을 제공하지 않음", report)
        self.assertIn("토큰 생성 속도 | 측정 불가 (n=0)", report)
        self.assertNotIn("0.00 MiB", report)
        self.assertIn("검색 과정", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)

    def test_truncated_but_valid_keyword_json_does_not_trigger_final_call(self):
        self.queue(self.keywords(status="incomplete", reason="max_output_tokens"))
        report = self.report()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("검색어 응답이 미완료", report)
        self.assertIn('"max_output_tokens"', report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)

    def test_final_incomplete_refusal_and_failed_response_are_not_completed_answers(self):
        for result, expected in (
            (response("중간에 잘린 답", status="incomplete", reason="max_output_tokens"), "출력 한도 도달"),
            (response("", refusal="요청에 답할 수 없습니다."), "응답 거절"),
            (response("", status="failed"), "미완료 또는 빈 원문"),
        ):
            with self.subTest(expected=expected):
                self.queue(self.keywords(), result)
                report = self.report()
                self.assertIn(expected, report)
                self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)
                self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)

    def test_api_failure_is_redacted_and_not_retried_and_next_question_continues(self):
        self.queue(self.keywords())
        self.responses.append((429, {"error": {"message": "test-key-not-real private request",
                                              "type": "rate_limit_error", "code": "rate_limit_exceeded"}}))
        self.queue(self.keywords(), response("정상 답변"))
        with patch.object(shared, "QUESTION_LIST", shared.QUESTION_LIST * 2):
            report = self.report()
        self.assertEqual(len(self.requests), 4)
        self.assertIn("HTTP 429", report)
        self.assertNotIn("test-key-not-real", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 3 / 4", report)
        self.assertIn("최종 모델 답변 완료: 1 / 질문 시도 2", report)

    def test_no_context_notice_uses_only_keyword_call(self):
        self.queue(self.keywords())
        with patch.object(shared, "prepare_context", side_effect=shared.NoRelevantContext("근거 확인 불가")):
            report = self.report()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("안내 반환 (최종 모델 미호출)", report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)

    def test_missing_usage_is_not_recorded_as_zero(self):
        self.queue(self.keywords(usage=False), response("답변", usage=False))
        report = self.report()
        self.assertIn("입력 토큰 | 측정 불가 (n=0)", report)
        self.assertNotIn("0.00 tokens (n=1)", report)

    def test_cloud_entry_uses_environment_key_and_shared_evaluation_switch(self):
        keyword_backend = self.local_keyword_backend()
        with patch.dict(luna.os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), \
             patch.object(luna, "OpenAI") as factory, patch.object(luna, "getpass") as prompt, \
             patch.object(luna, "build_keyword_backend", return_value=keyword_backend), \
             patch.object(shared, "load_latest_slack_data", return_value={"messages": self.messages}), \
             patch.object(shared, "run_question_evaluation") as evaluate, \
             patch.object(shared, "QUESTION_EVALUATION", True):
            luna.main()
        prompt.assert_not_called()
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(evaluate.call_args.kwargs["backend"].model, luna.MODEL)
        self.assertIs(evaluate.call_args.kwargs["keyword_backend"], keyword_backend)
        self.assertNotIn("test-key-not-real", self.stdout.getvalue())

    def test_local_keywords_and_cloud_answer_keep_requests_and_metrics_separate(self):
        keywords = self.local_keyword_backend()
        self.queue(response("질문잡담방에 올립니다."))
        report = self.report(keyword_backend=keywords)

        request = keywords.client.chat.call_args.kwargs
        self.assertEqual(request["model"], keywords.model)
        self.assertEqual(request["format"], shared.KEYWORD_FORMAT)
        self.assertEqual(request["options"], {"temperature": 0, "num_predict": 256, "num_ctx": shared.NUM_CTX})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["model"], self.backend.model)
        self.assertEqual(self.requests[0]["max_output_tokens"], 512)
        self.assertNotIn("text", self.requests[0])  # JSON schema는 로컬 검색어 요청에만 적용됩니다.
        keywords.observer.show.assert_called_once_with(keywords.model)
        self.assertEqual(keywords.observer.ps.call_count, 2)
        self.assertIn("| 검색어 생성 | local-keywords:tag | gpt2 | qwen2 |", report)
        self.assertIn("API가 실제 tokenizer 메타데이터를 제공하지 않음", report)
        self.assertIn('"provider": "ollama"', report)
        self.assertIn('"provider": "openai_responses"', report)
        self.assertIn('"num_predict": 256', report)
        self.assertIn('"max_output_tokens": 512', report)
        self.assertIn("KEYWORD_MODEL: `local-keywords:tag`", report)
        self.assertIn("MODEL: `cloud-test-model`", report)
        self.assertIn("VRAM (질문 직후, 최종 답변 모델): 측정 불가", report)
        self.assertIn("VRAM (질문 직후, 검색어 모델): 1024.00 MiB", report)
        self.assertIn("CPU 50.0% / GPU 50.0%", report)
        self.assertIn("실제 context_length: 4096", report)
        self.assertIn("digest: local-keyword-digest", report)
        summary = report.split("## 집계", 1)[1]
        keyword_summary, answer_summary = summary.split("#### 검색어 생성:", 1)[1].split("#### 최종 답변:", 1)
        self.assertIn("6.00 tokens/s (n=1)", keyword_summary)
        self.assertIn("1024.00 MiB (n=1)", keyword_summary)
        self.assertNotIn("입력 토큰", keyword_summary)
        self.assertIn("토큰 생성 속도 | 측정 불가 (n=0)", answer_summary)
        self.assertIn("입력 토큰 | 40.00 tokens (n=1)", answer_summary)
        self.assertIn("| local-keywords:tag | 1 / 1 |", summary)
        self.assertIn("| cloud-test-model | 1 / 1 |", summary)
        self.assertNotIn("test-key-not-real", report)

    def test_separate_cloud_keyword_model_and_reasoning_are_used_only_for_keywords(self):
        with patch.object(luna, "KEYWORD_PROVIDER", "openai"), \
             patch.object(luna, "KEYWORD_MODEL", "keyword-cloud-model"), \
             patch.object(luna, "KEYWORD_REASONING_EFFORT", "low"):
            keywords = luna.build_keyword_backend(self.backend.client)
        raw_keywords = self.keywords()
        raw_keywords["model"] = "keyword-cloud-snapshot"
        self.queue(raw_keywords, response("정상 답변"))
        report = self.report(keyword_backend=keywords)
        self.assertEqual([r["model"] for r in self.requests], ["keyword-cloud-model", "cloud-test-model"])
        self.assertEqual([r["reasoning"]["effort"] for r in self.requests], ["low", "none"])
        self.assertEqual(keywords.observed_model, "keyword-cloud-snapshot")
        self.assertEqual(self.backend.observed_model, "cloud-test-snapshot")
        self.assertIn("KEYWORD_MODEL: `keyword-cloud-model`", report)
        self.assertIn("VRAM (질문 직후, 검색어 모델): 측정 불가", report)
        self.assertNotIn("Ollama 서버:", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)

    def test_local_keyword_failure_does_not_fall_back_to_cloud_or_retry(self):
        keywords = self.local_keyword_backend()
        keywords.client.chat.side_effect = RuntimeError("Ollama 모델을 찾을 수 없습니다")
        report = self.report(keyword_backend=keywords)
        keywords.client.chat.assert_called_once()
        self.assertEqual(self.requests, [])
        self.assertIn("Ollama 모델을 찾을 수 없습니다", report)
        self.assertIn("| local-keywords:tag | 0 / 1 |", report)
        self.assertIn("| cloud-test-model | 0 / 0 |", report)
        self.assertIn("최종 답변 모델 호출은 실행되지 않았습니다", report)

    def test_invalid_local_keyword_json_stops_before_cloud(self):
        keywords = self.local_keyword_backend()
        keywords.client.chat.return_value.message.content = "JSON이 아닌 응답"
        report = self.report(keyword_backend=keywords)
        self.assertEqual(self.requests, [])
        self.assertIn("처리 실패", report)
        self.assertIn("JSON이 아닌 응답", report)
        self.assertIn("| local-keywords:tag | 1 / 1 |", report)  # API 반환과 내용 해석 실패는 구분합니다.

    def test_hybrid_no_context_skips_cloud_but_records_keyword_metrics(self):
        keywords = self.local_keyword_backend()
        with patch.object(shared, "prepare_context", side_effect=shared.NoRelevantContext("근거 없음")):
            report = self.report(keyword_backend=keywords)
        self.assertEqual(self.requests, [])
        self.assertIn("안내 반환 (최종 모델 미호출)", report)
        self.assertIn("6.00 tokens/s (n=1)", report)

    def test_hybrid_interactive_mode_keeps_two_connections_and_quiet_output(self):
        keywords = self.local_keyword_backend()
        self.queue(response("첫 답변"), response("둘째 답변"))
        with patch.object(shared, "QUESTION_EVALUATION", False), patch.object(shared, "DEBUG", False), \
             patch.object(shared, "load_latest_slack_data", return_value={"messages": self.messages}) as sync, \
             patch("builtins.input", side_effect=["테스트 공지", "테스트 공지는?", "종료"]):
            shared.main(backend=self.backend, keyword_backend=keywords)
        self.assertEqual(keywords.client.chat.call_count, 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(sync.call_count, 3)
        keywords.observer.ps.assert_not_called()
        keywords.observer.show.assert_not_called()
        self.assertIn("첫 답변\n둘째 답변", self.stdout.getvalue())
        self.assertNotIn("기다리고", self.stdout.getvalue())
        self.assertEqual(list(self.root.rglob("*.md")), [])

    def test_hybrid_metadata_failure_is_nonfatal_and_never_becomes_zero_vram(self):
        keywords = self.local_keyword_backend()
        keywords.observer.show.side_effect = RuntimeError("private-observer-error")
        keywords.observer.ps.side_effect = RuntimeError("private-observer-error")
        self.queue(response("정상 답변"))
        report = self.report(keyword_backend=keywords)
        self.assertIn("최종 모델 답변 완료: 1 / 질문 시도 1", report)
        self.assertIn("client.ps() 조회 실패 (RuntimeError)", report)
        self.assertIn("client.show() 조회 실패 (RuntimeError)", report)
        self.assertNotIn("0.00 MiB", report)
        self.assertNotIn("private-observer-error", report)

    def test_hybrid_metadata_time_is_excluded_from_elapsed(self):
        keywords = self.local_keyword_backend()
        clock = [0.0]
        local_response = keywords.client.chat.return_value
        cloud_chat = self.backend.chat

        def local_chat(**kwargs):
            clock[0] += 2
            return local_response

        def cloud_generate(**kwargs):
            clock[0] += 3
            return cloud_chat(**kwargs)

        def observe(*args):
            clock[0] += 100
            return {}

        keywords.client.chat.side_effect = local_chat
        keywords.observer.show.side_effect = observe
        keywords.observer.ps.side_effect = observe
        self.queue(response("답변"))
        with patch.object(shared.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(self.backend, "chat", side_effect=cloud_generate):
            report = self.report(keyword_backend=keywords)
        self.assertIn("**응답시간**\n\n5.00초", report)
        self.assertIn("| 검색어 생성 | local-keywords:tag | 성공 | 2.00초 |", report)
        self.assertIn("| 최종 답변 | cloud-test-model | 성공 | 3.00초 |", report)

    def test_invalid_keyword_config_stops_before_key_prompt_and_slack(self):
        for provider, model in (("typo", "test"), ("ollama", " ")):
            with self.subTest(provider=provider), patch.object(luna, "KEYWORD_PROVIDER", provider), \
                 patch.object(luna, "KEYWORD_MODEL", model), patch.object(luna, "getpass") as prompt, \
                 patch.object(luna, "OpenAI") as api, patch.object(shared, "main") as flow:
                with self.assertRaises(ValueError):
                    luna.main()
                prompt.assert_not_called()
                api.assert_not_called()
                flow.assert_not_called()

    def test_interactive_mode_reuses_cloud_connection_and_refreshes_slack_per_question(self):
        self.queue(self.keywords(), response("첫 답변"), self.keywords(), response("둘째 답변"))
        with patch.object(shared, "QUESTION_EVALUATION", False), \
             patch.object(shared, "load_latest_slack_data", return_value={"messages": self.messages}) as sync, \
             patch("builtins.input", side_effect=["", "테스트 공지", "다른 테스트 공지", "종료"]):
            shared.main(backend=self.backend)
        self.assertEqual(sync.call_count, 3)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(list(self.root.rglob("*.md")), [])

    def test_blank_key_stops_before_slack_or_api_and_hidden_prompt_is_used(self):
        with patch.dict(luna.os.environ, {"OPENAI_API_KEY": ""}), \
             patch.object(luna, "getpass", return_value="  ") as prompt, \
             patch.object(luna, "OpenAI") as api, patch.object(shared, "main") as flow:
            with self.assertRaisesRegex(SystemExit, "키를 입력하지 않아"):
                luna.main()
        prompt.assert_called_once()
        api.assert_not_called()
        flow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
