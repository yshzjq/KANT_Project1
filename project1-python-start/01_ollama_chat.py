# 로컬 Ollama에 고정 질문 한 개를 보내는 최소 예제입니다. Slack 검색·평가 기록은 사용하지 않습니다.
# 확인할 값: MODEL과 QUESTION. 여러 질문을 직접 입력하려면 main_ollama_chat.py를 실행합니다.

from ollama import Client

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
QUESTION = "프롬프트 엔지니어링이 무엇인지 초보자에게 두 문장으로 설명해 주세요."

# Ollama 서버와 모델이 미리 준비돼 있어야 합니다. 이 코드는 서버를 시작하거나 모델을 내려받지 않습니다.
client = Client(host="http://127.0.0.1:11434", timeout=180)
print("Ollama에 질문을 보냈습니다. 답변을 기다려 주세요.")
response = client.chat(
    model=MODEL,
    messages=[{"role": "user", "content": QUESTION}],
    stream=False,  # 전체 응답을 받은 뒤 아래에서 한 번에 출력합니다.
    options={"temperature": 0, "num_predict": 256},
)

print("\n[Ollama 답변]")
print(response.message.content)
