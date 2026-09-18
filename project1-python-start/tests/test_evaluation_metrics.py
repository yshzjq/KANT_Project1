"""통계 단위·미측정 사유·실패 분리·실제 컨텍스트 기록을 외부 호출 없이 검증합니다."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main_ollama_chat as chat
from evaluation_metrics import MeasuredClient, average, display, processor_state, response_metrics, running_model, vram_metric


def response(text, **updates):
    values = dict(model=chat.MODEL, message=SimpleNamespace(content=text), done=True,
                  done_reason="stop", load_duration=500_000_000, eval_count=12, eval_duration=2_000_000_000)
    values.update(updates)
    return SimpleNamespace(**values)


def loaded_model(**updates):
    model = dict(model=chat.MODEL, digest="test-digest", details={"quantization_level": "Q4_K_M"},
                 context_length=4096, size=4 * 1024 * 1024, size_vram=2 * 1024 * 1024)
    model.update(updates)
    return model


class MetricUnitTests(unittest.TestCase):
    def test_nanoseconds_and_generation_rate(self):
        result = response_metrics(response("답변"))
        self.assertEqual(result["load_seconds"]["value"], 0.5)
        self.assertEqual(result["tokens_per_second"]["value"], 6)

    def test_missing_or_nonpositive_duration_does_not_become_zero_speed(self):
        for duration in (None, 0, -1, float("nan"), float("inf"), True, "1000"):
            with self.subTest(duration=duration):
                value = response_metrics(response("답변", eval_duration=duration))["tokens_per_second"]
                self.assertIsNone(value["value"])
                self.assertIn("eval_duration", value["reason"])

    def test_missing_count_and_loading_time_have_individual_reasons(self):
        result = response_metrics(response("답변", eval_count=None, load_duration=None))
        self.assertIsNone(result["tokens_per_second"]["value"])
        self.assertIn("eval_count", result["tokens_per_second"]["reason"])
        self.assertIsNone(result["load_seconds"]["value"])
        self.assertIn("load_duration", result["load_seconds"]["reason"])

    def test_actual_zero_loading_and_count_are_valid(self):
        result = response_metrics(response("", eval_count=0, load_duration=0))
        self.assertEqual(result["tokens_per_second"]["value"], 0)
        self.assertEqual(result["load_seconds"]["value"], 0)

    def test_small_loading_duration_is_not_rounded_to_zero(self):
        loading = response_metrics(response("답변", load_duration=1_351_600))["load_seconds"]
        self.assertEqual(display(loading, "초"), "0.0013516초")
        self.assertEqual(average([loading["value"], None], "초"), "0.0013516초 (n=1)")

    def test_matching_model_uses_exact_tag_and_allows_implicit_latest(self):
        client = Mock()
        client.ps.return_value = {"models": [loaded_model(model="other:latest"), loaded_model(model="target:latest")]}
        snapshot = running_model(client, "target")
        self.assertTrue(snapshot["loaded"])
        self.assertEqual(snapshot["model"], "target:latest")
        self.assertEqual(snapshot["digest"], "test-digest")
        self.assertEqual(snapshot["context_length"], 4096)
        self.assertEqual(snapshot["quantization_level"], "Q4_K_M")
        self.assertFalse(running_model(client, "target:larger")["loaded"])

    def test_ps_failure_is_unknown_and_not_unloaded_or_zero_vram(self):
        client = Mock()
        client.ps.side_effect = RuntimeError("접속 실패")
        snapshot = running_model(client, chat.MODEL)
        self.assertIsNone(snapshot["loaded"])
        self.assertIsNone(vram_metric(snapshot)["value"])
        self.assertIn("client.ps() 조회 실패", vram_metric(snapshot)["reason"])

    def test_mib_conversion_and_processor_states(self):
        self.assertEqual(vram_metric(loaded_model())["value"], 2)
        self.assertEqual(processor_state(loaded_model(size_vram=0)), "100% CPU")
        self.assertEqual(processor_state(loaded_model(size_vram=4 * 1024 * 1024)), "100% GPU")
        self.assertIn("CPU 50.0% / GPU 50.0%", processor_state(loaded_model()))
        self.assertIn("측정 불가", processor_state(loaded_model(size_vram=None)))
        self.assertIn("측정 불가", processor_state(loaded_model(size_vram=8 * 1024 * 1024)))

    def test_exception_is_recorded_and_propagated_without_retry(self):
        raw = Mock()
        raw.chat.side_effect = RuntimeError("연결 오류")
        measured = MeasuredClient(raw)
        with self.assertRaises(RuntimeError):
            measured.chat(model=chat.MODEL)
        self.assertFalse(measured.calls[0]["success"])
        self.assertNotIn("response", measured.calls[0])
        raw.chat.assert_called_once()


class EvaluationReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.client = Mock()
        self.client.ps.return_value = {"models": [loaded_model()]}
        self.client.chat.side_effect = [response('{"keywords":["공지"]}'), response("[ollama 답변]\n정상 답변\n근거 ts: 100.1")]
        for replacement in (
            patch.object(chat, "BASE_DIR", self.root),
            patch.object(chat, "QUESTION_EVALUATION_DIR", "reports"),
            patch.object(chat, "QUESTION_LIST", [{"type": "검증용 유형", "question": "질문", "expected_result": "보고서 전용 기대 결과"}]),
            patch.object(chat, "Client", return_value=self.client),
            patch.object(chat, "prepare_context", return_value="관련 공지"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            replacement.__enter__()
            self.addCleanup(replacement.__exit__, None, None, None)

    def report(self, **kwargs):
        path = chat.run_question_evaluation([], **kwargs)
        return path.read_text(encoding="utf-8")

    def test_debug_controls_console_without_losing_evaluation_records(self):
        for debug in (False, True):
            with self.subTest(debug=debug):
                self.client.chat.side_effect = [response('{"keywords":["공지"]}'), response("평가 답변"),
                                                RuntimeError("검색어 호출 실패")]
                output = io.StringIO()
                with patch.object(chat, "DEBUG", debug), \
                     patch.object(chat, "QUESTION_LIST", chat.QUESTION_LIST * 2), \
                     contextlib.redirect_stdout(output):
                    report = self.report()
                self.assertIn("**답변**\n\n평가 답변", report)
                self.assertIn("평가 실패: 검색어 호출 실패", report)
                self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 3", report)
                self.assertIn("tokenizer", report)
                if debug:
                    self.assertIn("평가 결과 저장 완료", output.getvalue())
                    self.assertIn("평가 답변", output.getvalue())
                else:
                    self.assertEqual(output.getvalue(), "")

    def test_quiet_evaluation_no_context_keeps_notice_only_in_report(self):
        output = io.StringIO()
        with patch.object(chat, "DEBUG", False), \
             patch.object(chat, "prepare_context", side_effect=chat.NoRelevantContext("관련 기록 미확인")), \
             contextlib.redirect_stdout(output):
            report = self.report()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("관련 기록 미확인", report)
        self.assertIn("최종 모델 답변 완료: 0", report)

    def test_report_contains_per_call_metrics_original_response_and_actual_conditions(self):
        report = self.report()
        self.assertIn("0.50초", report)
        self.assertIn("6.00 tokens/s", report)
        self.assertIn("2.00 MiB", report)
        self.assertIn("test-digest", report)
        self.assertIn("Q4_K_M", report)
        self.assertIn("실제 context_length: 4096", report)
        self.assertIn('"num_ctx": 8192', report)
        self.assertIn('"num_predict": 256', report)
        self.assertIn('"num_predict": 512', report)
        self.assertIn('"temperature": 0', report)
        self.assertIn('"stream": false', report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)
        self.assertIn("6.00 tokens/s (n=1)", report)
        self.assertIn("**답변**\n\n정상 답변", report)
        self.assertIn("근거 ts: 100.1", report)  # 원본 응답은 정리하지 않습니다.
        self.assertIn("TTFT는 별도로 측정하지 않았습니다", report)
        self.assertIn("**유형**\n\n검증용 유형", report)
        self.assertIn("**기대 결과**\n\n보고서 전용 기대 결과", report)
        for call in self.client.chat.call_args_list:
            # 평가 기준을 답변 모델에 미리 보여 주면 평가 결과가 오염됩니다.
            sent = str(call.kwargs["messages"])
            self.assertNotIn("보고서 전용 기대 결과", sent)
            self.assertNotIn("검증용 유형", sent)
        self.assertEqual(self.client.chat.call_count, 2)

    def test_ps_time_is_outside_elapsed_and_first_query_is_not_assumed_cold(self):
        clock = [0.0]

        def observe():
            clock[0] += 100
            return {"models": [loaded_model()]}

        results = iter(['{"keywords":["공지"]}', "정상 답변"])

        def generate(**_):
            clock[0] += 2
            return response(next(results))

        self.client.ps.side_effect = observe
        self.client.chat.side_effect = generate
        with patch.object(chat.time, "perf_counter", side_effect=lambda: clock[0]):
            report = self.report()
        self.assertIn("**응답시간**\n\n4.00초", report)
        self.assertIn("1번째 질문 / 시작 전 로드됨", report)
        self.assertIn("전체 응답 시간 — 시작 전 미적재 | 측정 불가 (n=0)", report)
        self.assertIn("전체 응답 시간 — 시작 전 로드됨 | 4.00초 (n=1)", report)

    def test_failure_remains_a_failure_and_next_question_is_not_a_retry(self):
        self.client.chat.side_effect = [response('{"keywords":["공지"]}'), RuntimeError("연결 실패"),
                                        response('{"keywords":["공지"]}'), response("다음 질문 답변")]
        with patch.object(chat, "QUESTION_LIST", [
            {"type": "정상", "question": question, "expected_result": "기대 결과"}
            for question in ("첫 질문", "둘째 질문")
        ]):
            report = self.report()
        self.assertIn("평가 실패: 연결 실패", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 3 / 4", report)
        self.assertIn("최종 모델 답변 완료: 1 / 질문 시도 2", report)
        self.assertIn("Q01: 처리 실패", report)
        self.assertIn("최종 답변: 호출 성공 1 / 시도 2", report)
        self.assertEqual(self.client.chat.call_count, 4)

    def test_missing_stats_and_ps_do_not_prevent_answer_or_enter_averages_as_zero(self):
        self.client.ps.side_effect = RuntimeError("ps 실패")
        self.client.chat.side_effect = [response('{"keywords":["공지"]}'),
                                        response("답변", eval_duration=0, load_duration=None)]
        report = self.report()
        self.assertIn("**답변**\n\n답변", report)
        self.assertIn("client.ps() 조회 실패", report)
        self.assertIn("실제 context_length: 측정 불가", report)
        self.assertIn("토큰 생성 속도 | 측정 불가 (n=0)", report)
        self.assertIn("모델 로딩 시간 | 측정 불가 (n=0)", report)
        self.assertIn("VRAM (최종 답변 완료 질문 직후) | 측정 불가 (n=0)", report)
        self.assertIn("eval_duration 누락, 0 이하", report)

    def test_no_context_notice_is_not_counted_as_final_model_response(self):
        with patch.object(chat, "prepare_context", side_effect=chat.NoRelevantContext("관련 기록 미확인")):
            report = self.report()
        self.assertIn("안내 반환 (최종 모델 미호출)", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 1 / 1", report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)
        self.assertIn("최종 답변: 호출 성공 0 / 시도 0", report)
        self.assertEqual(self.client.chat.call_count, 1)

    def test_incomplete_response_is_preserved_but_not_counted_as_completed_answer(self):
        self.client.chat.side_effect = [response('{"keywords":["공지"]}'), response("일부 답변", done=False)]
        report = self.report()
        self.assertIn("불완전 응답", report)
        self.assertIn("일부 답변", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)

    def test_additional_experiment_has_separate_file_and_label(self):
        report = self.report(experiment_kind="extra")
        self.assertIn("실험 구분: 추가 실험", report)
        self.assertEqual(len(list(self.root.rglob("question_evaluation_extra_*.md"))), 1)

    def test_output_limit_and_cleaned_empty_answer_are_not_completed_answers(self):
        for final in (response("아직 이어질 답변", done_reason="length"),
                      response("[ollama 답변]\n근거 ts: 100.1")):
            with self.subTest(response=final):
                self.client.chat.side_effect = [response('{"keywords":["공지"]}'), final]
                report = self.report()
                self.assertIn("불완전 응답", report)
                self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)
                self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)
                self.assertIn("전체 응답 시간 (최종 답변 완료 질문) | 측정 불가 (n=0)", report)

    def test_interrupt_during_post_response_ps_keeps_answer_and_summary(self):
        self.client.ps.side_effect = [{"models": [loaded_model()]}, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.report()
        report = next(self.root.rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("**답변**\n\n정상 답변", report)
        self.assertIn("**기대 결과**\n\n보고서 전용 기대 결과", report)
        self.assertIn("사용자 중단으로 질문 직후 적재 정보를 조회하지 못함", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 2 / 2", report)
        self.assertIn("최종 모델 답변 완료: 1 / 질문 시도 1", report)

    def test_interrupt_during_generation_skips_post_response_ps(self):
        self.client.chat.side_effect = [response('{"keywords":["공지"]}'), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.report()
        self.client.ps.assert_called_once()
        report = next(self.root.rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("사용자 중단", report)
        self.assertIn("모델 호출 성공 수 / 전체 시도 수: 1 / 2", report)
        self.assertIn("최종 모델 답변 완료: 0 / 질문 시도 1", report)


if __name__ == "__main__":
    unittest.main()
