# 모델 호출 한 번의 전체 대기 시간을 재는 예제입니다.
# 입력·초기화·출력 시간은 제외하고 client.chat() 직전과 직후만 비교합니다.
# load_duration·tokens/s·VRAM 기록은 main_ollama_chat.py의 평가 모드에서 확인합니다.

from time import perf_counter

from ollama import Client

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
QUESTION = "프롬프트 엔지니어링이 무엇인지 초보자에게 두 문장으로 설명해 주세요."
client = Client(host="http://127.0.0.1:11434", timeout=180)

print("Ollama의 답변을 끝까지 받는 데 걸린 시간을 측정합니다.")
# perf_counter는 시간 간격 측정용입니다. 시스템 날짜 변경의 영향을 받는 현재 시각을 빼지 않습니다.
start = perf_counter()
response = client.chat(
    model=MODEL,
    messages=[{"role": "user", "content": QUESTION}],
    stream=False,
    options={"temperature": 0, "num_predict": 256},
)
elapsed = perf_counter() - start

print("\n[Ollama 답변]")
print(response.message.content)
print(f"\n전체 응답 시간: {elapsed:.2f}초")
# stream=False이므로 마지막 토큰까지 받은 시간입니다. 해당 호출에서 모델을 로드했다면 그 지연도 포함됩니다.
