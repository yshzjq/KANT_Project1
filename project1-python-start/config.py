
# False: 진행 로그를 숨깁니다. 직접 질문 모드의 입력 안내·답변·오류 안내는 남깁니다.
# 평가 모드는 터미널 출력을 생략해도 .md 기록을 저장합니다. 봇의 파일 로그도 유지합니다.
DEBUG = True

# True: 아래 목록을 평가해 .md 저장 / False: 직접 입력한 질문에 반복 응답.
# 평가 파일에는 검색어·선택 원문·제외 사유도 저장합니다. DEBUG 설정과는 무관합니다.
QUESTION_EVALUATION = False

# 항목을 추가할 때 type(유형), question(질문), expected_result(기대 결과)를 모두 입력합니다.
# 목록 순서가 Q01, Q02… 번호가 됩니다. 유형·기대 결과는 보고서용이며 모델에는 질문만 전달합니다.
QUESTION_LIST = [
    {
        "type": "정상",
        "question": "질문잡담방은 어떤 용도로 사용하는 채널인가요?",
        "expected_result": "학습 질문, 자료·인사이트 공유, 수강생 소통 공간이라는 내용",
    },
    {
        "type": "정상",
        "question": "수업이 본격적으로 시작하기 전에 무엇을 공부하면 좋다고 안내했나요?",
        "expected_result": "제공된 학습 자료의 개념 중심으로 보완 학습하라는 안내",
    },
    {
        "type": "정상",
        "question": "베이스캠프에서는 어떤 방식으로 학습이 진행되나요?",
        "expected_result": "자가 역량 진단 → 맞춤 커리큘럼/강의/과제 제공 등의 내용",
    },
    {
        "type": "정상",
        "question": "기초 강화 커리큘럼 대상자는 무엇을 제출해야 하나요?",
        "expected_result": "기초 강의 수강, 1일 1퀘스트 제출 등의 핵심 안내",
    },
    {
        "type": "정상",
        "question": "마일스톤 커리큘럼 대상자는 무엇을 제출해야 하나요?",
        "expected_result": "STEP 0~4 마일스톤 진척도 제출 등의 안내",
    },
    {
        "type": "정상",
        "question": "오늘 학습을 마치기 전에 확인해야 하는 제출 항목이 있나요?",
        "expected_result": "퀘스트 또는 진척도 제출 여부 확인",
    },
    {
        "type": "정상",
        "question": "수업을 듣다가 궁금한 점이 생기면 어디에 질문하면 되나요?",
        "expected_result": "질문잡담방, 튜터 태그 등의 안내",
    },
    {
        "type": "정상",
        "question": "코드 오류나 환경 설정 문제가 생겼을 때 어디에 질문하는 것이 좋나요?",
        "expected_result": "설문보다 Slack 또는 튜터에게 직접 질문하라는 내용",
    },
    {
        "type": "정상",
        "question": "VS Code 대신 Cursor를 사용해도 되나요?",
        "expected_result": "해당 Slack 안내를 찾아 사용 가능 여부를 설명",
    },
    {
        "type": "정상",
        "question": "Jupyter Notebook 대신 Google Colab을 사용해도 되나요?",
        "expected_result": "해당 Slack 답변을 근거로 안내",
    },
    {
        "type": "정상",
        "question": "Private LLM 과정에서는 최종적으로 어떤 내용을 배우게 되나요?",
        "expected_result": "RAG, Fine-tuning, Agent, LLMOps 등 파일에서 제공되는 범위로 설명",
    },
    {
        "type": "경계",
        "question": "수업 자료를 100% 이해하지 못해도 다음 학습으로 넘어가도 되나요?",
        "expected_result": "처음부터 모두 이해할 필요는 없고 큰 그림을 보고 모르는 용어를 추후 채우라는 취지",
    },
    {
        "type": "경계",
        "question": "오늘 데일리 퀘스트가 있나요?",
        "expected_result": "과거 Slack 기록과 현재를 구분해야 함. 현재 여부를 파일만으로 단정하지 않아야 함",
    },
    {
        "type": "경계",
        "question": "이번 주까지 제가 반드시 해야 하는 프로젝트를 정리해주세요.",
        "expected_result": "사용자 개인 배정 커리큘럼을 모르면 일반 안내와 개인 정보를 구분해야 함",
    },
    {
        "type": "정보 부족",
        "question": "제가 지금 기초 강화 대상자인가요, 마일스톤 대상자인가요?",
        "expected_result": "파일에 개인 배정 정보가 없다면 확인할 수 없다고 답해야 함",
    },
    {
        "type": "정보 부족",
        "question": "제 퀘스트 제출이 정상적으로 완료됐는지 확인해주세요.",
        "expected_result": "제출 시스템의 개인 상태는 이 Slack 파일만으로 확인 불가",
    },
    {
        "type": "범위 밖",
        "question": "이번 교육을 수료하면 어느 회사에 취업할 수 있나요?",
        "expected_result": "파일에서 특정 취업 회사가 보장되지 않는다면 추측하지 않아야 함",
    },
    {
        "type": "범위 밖",
        "question": "다음 시험 날짜와 시험 범위를 알려주세요.",
        "expected_result": "명확한 정보가 파일에 없다면 없는 정보를 만들어내지 않아야 함",
    },
]

# 평가 결과는 프로젝트 폴더 기준으로 저장합니다.
QUESTION_EVALUATION_DIR = "data/private/evaluations"

# 단위: 초. main이 봇의 기록 준비를 기다리는 한도이며, 봇 수명이나 API 조회 간격이 아닙니다.
SLACK_READY_TIMEOUT = 600

# 수집·감지·검색할 채널입니다. 채널 ID를 키, 보고서에 표시할 이름을 값으로 추가합니다.
# 채널마다 slack_<채널 ID>.json으로 저장하고, 질문할 때 함께 검색합니다.
SLACK_CHANNELS = {
    "C0BBNNCS4BG": "LLM 질문잡담방",
    "C0BBUDU7AEQ": "LLM 공지방",
}
