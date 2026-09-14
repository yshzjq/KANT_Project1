# Python으로 모델에 첫 질문 보내기

Ollama 설치와 CLI 대화를 마친 뒤, VS Code와 uv로 실행하는 작은 예제입니다.
자세한 환경 준비와 지표 설명은 [두 번째 노션 가이드](https://app.notion.com/p/3d41698118388039be07cca84e1f743c)를 확인하세요.

## 준비

1. 압축을 풀고, VS Code에서 `pyproject.toml`과 Python 파일 3개가 보이는 폴더를 엽니다.
2. **터미널 → 새 터미널**에서 PowerShell을 엽니다. Ollama 앱도 실행합니다.
3. 아래 명령을 한 줄씩 입력합니다. `.venv`를 직접 활성화하지 않아도 됩니다.

```powershell
uv sync
uv run python --version
```

Python `3.12.x`가 나오면 준비됐습니다. 최초 실행에는 Python과 패키지 다운로드가 필요할 수 있습니다.

## 실행

로컬 모델이 없다면 먼저 다운로드합니다. 이미 설치했다면 건너뜁니다.

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
uv run python 01_ollama_chat.py
```

다음 명령은 OpenAI API에 질문을 **1번** 보냅니다. 유효한 API 키와 사용 가능한 잔액·모델 접근 권한이 필요합니다.

```powershell
uv run python 02_luna_chat.py
```

키를 요청하면 터미널에 붙여넣고 Enter를 누릅니다. 입력한 키는 화면에 보이지 않습니다. 키를 코드·채팅·노션에 적지 마세요. 키 없이 Enter를 누르면 호출하지 않고 종료합니다. 키를 받지 않았다면 이 단계는 건너뜁니다.

API 사용 비용은 유료입니다. 실습과 프로젝트를 합쳐 인당 최대 5달러 범위에서 진행하고, [사용량](https://platform.openai.com/usage)을 확인하세요. 이 코드는 자동 재시도하지 않으며, 누적 지출을 자동 차단하지는 않습니다. 오류가 났을 때도 반복 실행 전에 사용량과 오류 안내를 확인합니다.

마지막 명령은 로컬 Ollama에 질문을 1번 보내고 전체 답변을 받는 데 걸린 시간을 출력합니다.

```powershell
uv run python 03_measure_time.py
```

이 값은 첫 토큰 시간(TTFT)이 아닙니다. 모델을 처음 불러오는 시간과 PC 상태, 답변 길이에 영향을 받으므로 한 번의 숫자만으로 모델 우열을 정하지 않습니다.

실행 파일 안의 `QUESTION` 한 줄을 바꾸고 **Ctrl+S**로 저장하면 다른 질문을 보낼 수 있습니다. 이 파일들은 대화 기록을 유지하지 않고, 매번 질문 1개를 새로 보냅니다.
