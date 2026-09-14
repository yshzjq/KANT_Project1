from time import perf_counter

from ollama import Client

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
QUESTION = "프롬프트 엔지니어링이 무엇인지 초보자에게 두 문장으로 설명해 주세요."
client = Client(host="http://127.0.0.1:11434", timeout=180)

print("Ollama의 답변을 끝까지 받는 데 걸린 시간을 측정합니다.")
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
# 첫 토큰 시간(TTFT)이 아닙니다. 필요하면 모델 로딩 시간도 포함됩니다.
