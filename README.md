# KANT_Project1 — Slack 기록 기반 질의응답

Slack 공지방·질문잡담방의 기록을 검색해 교육 일정과 과제 관련 질문에 답하는 프로젝트입니다.
키워드 검색 기반 RAG를 사용하며, 로컬 Ollama 모델과 클라우드 Luna 모델의 답변 품질·성능을 비교합니다.

## 비교한 모델

2026-09-17 비교 보고서 기준으로 검색어 추출 모델과 최종 답변 모델을 나누어 평가했습니다.

### 검색어 추출 모델

로컬 모델 6종을 공통 질문 18개로 비교했습니다. 최종 답변 모델은 Qwen3 4B Q4로 고정했습니다.

| 모델 | 실행 태그 | 실행 환경 |
|---|---|---|
| Qwen3 4B Q4 | `qwen3:4b-instruct-2507-q4_K_M` | 로컬 Ollama |
| Gemma 3 4B | `gemma3:4b` | 로컬 Ollama |
| Gemma 3 12B | `gemma3:12b` | 로컬 Ollama |
| EXAONE 3.5 7.8B | `exaone3.5:7.8b` | 로컬 Ollama |
| Phi-4 mini 3.8B | `phi4-mini:3.8b` | 로컬 Ollama |
| Llama 3.1 8B | `llama3.1:8b` | 로컬 Ollama |

검색 근거 전달과 현재 PC의 처리 시간을 고려해 **Qwen3 4B Q4**를 검색어 추출 모델로 선정했습니다.

### 최종 답변 생성 모델

검색어 추출 모델을 Qwen3 4B Q4로 고정하고 아래 4개 설정을 비교했습니다. 18개 질문 중 공통으로 답변 모델을 호출한 14개 문항을 분석했습니다.

| 모델 | 실행 태그 | 실행 환경 |
|---|---|---|
| Qwen3 4B Q4 | `qwen3:4b-instruct-2507-q4_K_M` | 로컬 Ollama |
| Qwen3 4B Q8 | `qwen3:4b-instruct-2507-q8_0` | 로컬 Ollama |
| EXAONE 3.5 7.8B | `exaone3.5:7.8b` | 로컬 Ollama |
| GPT-5.6 Luna | `gpt-5.6-luna` | Cloud API |

Qwen Q4·Q8은 같은 모델의 양자화 설정 비교입니다. 실험별 데이터·생성 설정과 모델 로딩 상태에 차이가 있어 결과를 일반적인 모델 순위로 해석하지 않습니다.

상세 결과: [검색어 모델 선정](project1-python-start/keyword_model_selection_20260917.md) · [로컬 답변 모델 비교](project1-python-start/step6_local_model_measurement_20260917.md) · [로컬·Cloud 비교](project1-python-start/step7_local_cloud_comparison_20260917.md)

## 동작 방식

질문 입력 → LLM 검색어 추출 → 관련 대화 검색·필요시 문단 발췌 → 근거 기반 답변

- `slack_bot.py`가 별도 백그라운드 프로세스로 메시지 추가·수정·삭제를 감지하고 채널별 JSON을 갱신합니다.
- 질문 프로그램을 실행하면 감지 봇을 자동으로 시작하거나 실행 중인 봇을 재사용합니다.
- 직접 질문 모드는 질문마다 최신 저장 기록을 읽고, 평가 모드는 시작 시 읽은 동일한 기록으로 질문 목록을 평가합니다.

## 실행 방법

Python 3.12, uv, 실행 중인 Ollama 서버가 필요합니다. Slack 앱의 Socket Mode·메시지 이벤트 구독과 대상 채널 접근 권한을 설정한 뒤, 프로그램을 실행하는 환경에 아래 변수를 등록합니다.

| 환경변수 | 용도 |
|---|---|
| `SLACK_APP_TOKEN` | Socket Mode 연결용 앱 토큰 |
| `SLACK_BOT_TOKEN` | Slack 봇 연결용 토큰 |
| `SLACK_TOKEN` | 메시지 기록·사용자 이름 조회용 토큰 |
| `OPENAI_API_KEY` | Luna 실행 시 사용. 미설정 시 숨김 입력으로 받음 |

저장소 루트에서 실행합니다. 기본 로컬 모델은 `qwen3:4b-instruct-2507-q4_K_M`입니다.

```powershell
cd project1-python-start
uv sync
ollama pull qwen3:4b-instruct-2507-q4_K_M

# 로컬 모델로 질문·평가 실행
uv run python main_ollama_chat.py

# 로컬 검색어 추출 + Luna 답변 생성
uv run python main_luna_chat.py
```

Luna 실행 시 질문과 선택된 Slack 기록이 클라우드 API로 전송됩니다.

## 설정과 주요 파일

`config.py`에서 실행 모드와 검색 채널을 설정합니다.

- `QUESTION_EVALUATION = False`: 직접 질문 입력 (`종료`, `exit`, `quit`으로 종료)
- `QUESTION_EVALUATION = True`: `QUESTION_LIST`의 질문을 순서대로 평가
- `SLACK_CHANNELS`: 수집·검색할 채널 ID와 이름
- `DEBUG`: 진행 로그 출력 여부

| 파일 | 역할 |
|---|---|
| `main_ollama_chat.py` | 로컬 답변 실행, 검색·문맥 선택, 공통 평가 흐름 |
| `main_luna_chat.py` | Luna 답변 실행 및 검색어 모델 설정 |
| `slack_bot.py` | Slack 변경 감지와 동기화 관리 |
| `slack_api_LLM_questions-chat.py` | 채널 기록 수집·JSON 저장 |
| `search_terms.py` / `slack_dates.py` | 검색어 정리·날짜 조건 처리 |
| `prompts.py` | 검색어 추출·답변 생성 지침 |
| `evaluation_metrics.py` | 호출 성능 측정·Markdown 보고서 생성 |

평가 결과는 `project1-python-start/data/private/evaluations/`에 저장됩니다. 답변·검색 근거·응답 시간·모델 호출 통계를 기록하며, 클라우드에서 확인할 수 없는 VRAM·로딩 시간 등은 측정 불가로 표시합니다.

## 검증과 한계

프로젝트 폴더에서 `uv run python -m unittest discover -s tests -q`로 자동 테스트를 실행합니다.

키워드 검색에서 관련 공지가 누락되거나 모델이 조건·기한을 잘못 해석할 수 있습니다. 개인 제출 상태처럼 기록만으로 확인할 수 없는 정보도 있습니다. API 호출 성공과 답변 정확도는 별도로 평가합니다.
