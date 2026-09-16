"""실제 OpenAI SDK + 가짜 HTTP 응답으로 클라우드 요청·보고서·실패 처리를 검증합니다."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def report(self):
        return shared.run_question_evaluation(self.messages, backend=self.backend).read_text(encoding="utf-8")

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
        with patch.dict(luna.os.environ, {"OPENAI_API_KEY": "test-key-not-real"}), \
             patch.object(luna, "OpenAI") as factory, patch.object(luna, "getpass") as prompt, \
             patch.object(shared, "load_latest_slack_data", return_value={"messages": self.messages}), \
             patch.object(shared, "run_question_evaluation") as evaluate, \
             patch.object(shared, "QUESTION_EVALUATION", True):
            luna.main()
        prompt.assert_not_called()
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(evaluate.call_args.kwargs["backend"].model, luna.MODEL)
        self.assertNotIn("test-key-not-real", self.stdout.getvalue())

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
