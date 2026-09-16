"""Luna로 Slack 질문에 답합니다. 실행: python main_luna_chat.py

읽는 순서: main → LunaClient.chat → main_ollama_chat.main.
config.py의 QUESTION_EVALUATION과 QUESTION_LIST, prompts.py의 지침을 그대로 사용합니다.
"""

import os
from datetime import datetime, timezone
from getpass import getpass
from types import SimpleNamespace

from openai import APIError, OpenAI

import main_ollama_chat as shared
from evaluation_metrics import field


# 02_luna_chat.py와 같은 모델입니다. 다른 Responses 모델을 비교할 때 이 값을 변경합니다.
# 모델마다 reasoning 설정 지원 여부가 다를 수 있으므로 지원하지 않는 모델은 None으로 둡니다.
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "none"
API_BASE_URL = "https://api.openai.com/v1"
API_TIMEOUT = 180
CLOUD_RUNTIME_REASON = "OpenAI Responses API는 서버의 VRAM·적재 상태·digest·양자화·실제 context_length를 제공하지 않음"


class LunaClient:
    """공통 코드의 chat 요청을 Responses API로 바꾸는 연결 계층입니다."""

    provider = "openai_responses"

    def __init__(self, client, *, model=MODEL, reasoning_effort=REASONING_EFFORT):
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.observed_model = None

    @property
    def report_header(self):
        return (
            f"OpenAI Responses API: `{API_BASE_URL}`\n\n"
            f"클라이언트 timeout={API_TIMEOUT}초 / max_retries=0 / store=false\n\n"
            f"검색 문맥 선택은 기존 NUM_CTX={shared.NUM_CTX} 기준의 입력 길이 규칙을 재사용합니다. "
            "이 값은 클라우드에 num_ctx로 전송하지 않으며 서버의 실제 context_length가 아닙니다.\n\n"
            "temperature는 02_luna_chat.py처럼 미지정입니다. 로컬 Ollama의 temperature=0과 "
            "동일한 생성 설정이라고 해석하지 마세요. 전송 설정과 API 반환 설정을 각각 보존합니다.\n\n"
        )

    @property
    def measurement_notes(self):
        return (
            "클라우드 호출 시간에는 네트워크와 서버 처리 시간이 포함됩니다. "
            "load_duration·eval_duration을 제공하지 않아 모델 로딩 시간·순수 생성 tokens/s는 측정 불가입니다. "
            "출력 토큰 수를 전체 응답 시간으로 나눠 Ollama의 생성 속도로 대체하지 않습니다.\n\n"
            f"{CLOUD_RUNTIME_REASON}. 미측정 값은 0이 아닌 사유와 함께 남깁니다. "
            "첫 실행의 서버 로딩 여부도 확인할 수 없습니다.\n\n"
            "입력·출력·추론·캐시 토큰 수는 API usage 원본으로 기록합니다. "
            "출력 한도 max_output_tokens는 추론 토큰과 출력 토큰을 포함하며 모델별 토큰화 방식이 다릅니다.\n\n"
        )

    def make_request(self, *, model, messages, options, stream=False, format=None):
        if stream:
            raise ValueError("평가용 Luna 연결은 stream=False만 지원합니다.")
        # num_predict 256/512만 API의 출력 한도로 옮깁니다. num_ctx는 서버 설정인 척 보내지 않습니다.
        request = {
            "model": model, "input": messages, "stream": False,
            "max_output_tokens": options["num_predict"],
            "tools": [], "tool_choice": "none", "store": False,
        }
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        # 02_luna_chat.py처럼 temperature는 미지정입니다. 실제로 보낸 설정만 보고서에 남깁니다.
        if format is not None:
            request["text"] = {"format": {
                "type": "json_schema", "name": "search_keywords", "strict": True, "schema": format,
            }}
        return request

    def request_settings(self, **kwargs):
        # API 키와 입력 원문을 설정 표에 섞지 않습니다. 원문·메시지는 검색 진단에 별도로 남깁니다.
        return {key: value for key, value in self.make_request(**kwargs).items() if key not in {"input", "model"}}

    def runtime_snapshot(self):
        return {
            "observed_at": datetime.now(timezone.utc).isoformat(), "loaded": None,
            "model": self.observed_model, "reason": CLOUD_RUNTIME_REASON,
        }

    def chat(self, **kwargs):
        try:
            response = self.client.responses.create(**self.make_request(**kwargs))
        except APIError as error:
            # SDK 오류 본문에는 요청 정보가 포함될 수 있어, 종류와 HTTP 상태만 표시합니다.
            raise RuntimeError(
                f"OpenAI API 호출 실패: {type(error).__name__} / HTTP {getattr(error, 'status_code', None) or '연결 실패'}"
            ) from None

        raw = response.model_dump(mode="json")
        self.observed_model = response.model
        refusals = [part.get("refusal", "") for item in raw.get("output", [])
                    if item.get("type") == "message" for part in item.get("content", [])
                    if part.get("type") == "refusal"]
        incomplete_reason = field(response.incomplete_details, "reason")
        # 원본 상태를 보존하면서 공통 평가 코드가 출력 한도·거절을 완료로 세지 않게 합니다.
        reason = "refusal" if refusals else "length" if incomplete_reason == "max_output_tokens" else response.status
        return SimpleNamespace(
            provider=self.provider, model=response.model, created_at=response.created_at,
            message=SimpleNamespace(content=response.output_text or "\n".join(refusals), thinking=None),
            done=response.status == "completed" and not refusals, done_reason=reason,
            openai_response=raw,
        )


def main():
    # 환경변수가 없으면 02_luna_chat.py처럼 화면에 보이지 않게 입력받습니다. 키를 파일에 쓰지 않습니다.
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        try:
            api_key = getpass("OpenAI API 키를 붙여넣고 Enter (화면에 보이지 않음): ").strip()
        except (KeyboardInterrupt, EOFError):
            raise SystemExit("API 키 입력을 취소했습니다.") from None
    if not api_key:
        raise SystemExit("키를 입력하지 않아 API를 호출하지 않았습니다.")

    print(f"클라우드 모델: {MODEL} / 질문과 선택된 Slack 기록을 OpenAI API로 전송합니다.")
    with OpenAI(api_key=api_key, base_url=API_BASE_URL, timeout=API_TIMEOUT, max_retries=0) as client:
        shared.main(backend=LunaClient(client, model=MODEL, reasoning_effort=REASONING_EFFORT))


if __name__ == "__main__":
    main()
