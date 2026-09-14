from getpass import getpass

from openai import APIError, APITimeoutError, OpenAI

QUESTION = "프롬프트 엔지니어링이 무엇인지 초보자에게 두 문장으로 설명해 주세요."

# 키는 실행할 때 입력합니다. 화면에 표시되거나 파일에 저장되지 않습니다.
api_key = getpass("OpenAI API 키를 붙여넣고 Enter (화면에 보이지 않음): ").strip()

if not api_key:
    raise SystemExit("키를 입력하지 않아 API를 호출하지 않았습니다.")

client = OpenAI(
    api_key=api_key,
    base_url="https://api.openai.com/v1",
    timeout=60,
    max_retries=0,
)
print("Luna에 질문을 보냈습니다. 답변을 기다려 주세요.")
try:
    response = client.responses.create(
        model="gpt-5.6-luna",
        input=QUESTION,
        reasoning={"effort": "none"},
        max_output_tokens=256,
        tools=[],
        tool_choice="none",
        store=False,
    )
except APITimeoutError:
    raise SystemExit("응답 대기 시간이 초과됐습니다. 재실행 전 사용량을 확인하세요.") from None
except APIError as error:
    status = getattr(error, "status_code", "연결 오류")
    raise SystemExit(f"API 호출 실패: {status}. 가이드의 오류 안내를 확인하세요.") from None

print(f"\n[처리 상태] {response.status}")
print("[Luna 답변]")
print(response.output_text or "출력된 답변이 없습니다.")
if response.status != "completed":
    print("완료된 답변이 아닙니다. 출력이 중간에 끊겼을 수 있습니다.")
