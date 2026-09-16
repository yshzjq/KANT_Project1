# KANT_Project1
칸트 첫 프로젝트 과제

## Luna로 Slack 질문·평가 실행

프로젝트 폴더에서 실행합니다.

```powershell
cd project1-python-start
.\.venv\Scripts\python.exe main_luna_chat.py
```

- 모델은 `main_luna_chat.py`의 `MODEL`에서 변경합니다. 기본은 `02_luna_chat.py`와 같은 `gpt-5.6-luna`입니다.
- `OPENAI_API_KEY` 환경변수가 없으면 화면에 보이지 않는 키 입력을 받습니다. 키를 코드나 보고서에 저장하지 않습니다.
- `config.py`에서 `QUESTION_EVALUATION=True`면 기존 질문 목록을 평가하고 `data/private/evaluations`에 모델명별 Markdown 보고서를 저장합니다. `False`면 직접 입력한 질문을 반복 처리합니다.
- Slack 변경 감지·기록 동기화, 검색·날짜 판정, 프롬프트, 검색 진단과 평가 보고서는 로컬 실행과 공유합니다. 질문과 선택된 Slack 기록은 클라우드 API로 전송됩니다.
- 전체 응답 시간·입출력 토큰·실제 요청 설정·원본 응답을 기록합니다. 클라우드가 제공하지 않는 VRAM·로딩 시간·순수 생성 tokens/s·서버 적재 정보는 사유와 함께 측정 불가로 남깁니다.
- 검색어 출력 한도는 256, 최종 답변은 512를 `max_output_tokens`로 전달합니다. `num_ctx`는 클라우드에 전송하지 않으며, temperature는 단일 질문 Luna 예제처럼 미지정입니다.
- 다른 모델로 변경할 때는 Responses API, JSON Schema 출력과 reasoning 설정 지원 여부를 확인합니다. 지원하지 않는 reasoning 설정은 `REASONING_EFFORT=None`으로 생략할 수 있습니다.

연동 참고: [GPT-5.6 Luna 공식 문서](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [구조화 출력 공식 문서](https://developers.openai.com/api/docs/guides/structured-outputs).

검증은 프로젝트 폴더에서 `.\.venv\Scripts\python.exe -m unittest discover -s tests -q`로 실행합니다. 클라우드 테스트는 실제 SDK에 가짜 HTTP 응답을 연결하므로 API 키·과금·외부 전송 없이 실행됩니다.
