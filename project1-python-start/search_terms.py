"""검색어의 구절·띄어쓰기·조사를 정리합니다. 모델의 토큰 길이 설정과는 별개입니다."""

# 검색은 문자열 포함 여부를 사용합니다. 벡터 검색이나 완전한 한국어 형태소 분석은 아닙니다.
# 잘못된 검색 후보를 점검할 때 split_search_terms의 결과와 원문을 함께 비교합니다.
# 관련 검증: tests/test_answer_revision.py의 검색어 분리·띄어쓰기·드문 단서 테스트.

import re


# 주제를 구분하지 못하는 질문 표현을 제외합니다. 날짜 조건은 slack_dates.py에서 별도로 처리합니다.
STOP_WORDS = {
    "어떤", "어떻게", "무엇", "무엇을", "어디", "어디에", "어느", "누가", "누구", "제가", "저는",
    "언제", "얼마나", "자주", "있으면", "않아도", "되나", "알려야",
    "하는", "하면", "해야", "하나요", "있나요", "인가요", "되나요", "해주세요",
    "알려주세요", "정리해주세요", "확인해주세요", "좋다고", "좋은", "좋을", "생기면",
    "있을까요", "안내했나요", "사용해도", "넘어가도", "대해", "대한", "것을",
    "오늘", "금일", "내일", "모레", "어제", "이번", "다음", "지난", "금주", "차주", "지금", "현재", "전에", "정리", "반드시", "점이", "듣다", "마치기", "확인해야", "the", "is", "a",
}

# 검색에는 남기되 핵심 주제보다 낮게 평가합니다. 드물게 쓰인 일반 표현도 핵심어로 승격하지 않습니다.
GENERAL_TERMS = {
    "학습", "수업", "자료", "안내", "확인", "항목", "사용", "사용하", "용도", "용도로",
    "채널", "질문", "문의", "답변", "궁금", "궁금한", "시작", "진행", "방식", "내용",
    "대신", "대체", "대상자", "커리큘럼", "최종", "최종적", "배우게", "필수", "좋나요",
    "날짜", "기간", "주간", "범위", "결과", "정상", "완료", "과정", "작성",
}


def asks_question_location(question):
    """질문·문의할 곳을 찾는 의도만 식별합니다. 특정 채널이나 답안을 지정하지 않습니다."""
    return bool(re.search(r"질문|문의|물어|궁금", question) and re.search(r"어디|어느.*(?:곳|채널)|방법|경로", question))


def date_subject_terms(keywords):
    terms = [word for word in keywords if compact_text(word) not in GENERAL_TERMS]
    return terms or list(keywords)


def routing_evidence(text):
    """실제 질문 사례보다 '어디에 어떻게 문의하라'는 안내 문장을 우선합니다."""
    score = 0
    for part in re.split(r"\n+|(?<=[.!?])\s+", text.casefold()):
        invitation = re.search(r"질문|문의|궁금|물어|막히|도움", part)
        action = re.search(r"태그|dm|메시지.*(?:주|보내)|남겨|올려|부르|질문.*(?:주세요|주시면)|문의.*(?:주세요|주시면)", part)
        place = re.search(r"채널|슬랙|slack|질문.*방|이\s*방", part)
        if invitation and action:
            score = max(score, 3 if place else 2)
        elif invitation and place:
            score = max(score, 1)
    return score


def compact_text(text):
    """기초 강화/기초강화, 데일리 퀘스트/데일리퀘스트를 같은 표현으로 검색합니다."""
    text = re.sub(r"\s+", "", text.casefold())
    # 제품명의 표기 차이만 통일합니다. 질문별 정답이나 문서 ID를 검색 규칙에 넣지 않습니다.
    for variant, canonical in (("주피터", "jupyter"), ("구글", "google"), ("커서", "cursor"),
                               ("colaboratory", "colab"), ("콜랩", "colab"), ("코랩", "colab"),
                               ("슬랙", "slack"), ("파이썬", "python")):
        text = text.replace(variant, canonical)
    return text


def split_search_terms(text):
    words = re.findall(r"[가-힣a-zA-Z0-9]+(?:[.+#][a-zA-Z0-9]+)*%?", text.casefold())
    result = []
    for word in words:
        if word in STOP_WORDS:
            continue
        # 정규식으로 끝부분을 줄이는 규칙이라, 원래 단어의 일부를 조사로 오인할 수 있습니다.
        # 검색 누락이 생기면 아래 어미 목록과 최소 길이 조건부터 확인합니다.
        stem = re.sub(
            r"(?:에서는|으로는|에게는|까지는|에서도|에도|이나|을까요|인가요|하나요|되나요|적으로|적인|해야|하지|하기|하면|에게|에서|으로|까지|부터|은|는|을|를|가|이|와|과|로)$",
            "", word,
        )
        if len(stem) >= 2:
            word = stem
        if len(word) >= 2 and word not in STOP_WORDS and word not in result:
            result.append(word)
    return result


def expand_search_terms(raw_keywords, question):
    """모델 핵심어 + 잘게 나눈 구절 + 원래 질문의 단서를 중복 없이 결합합니다."""
    terms = []
    for raw in raw_keywords:
        words = split_search_terms(raw)
        # 기술명·복합명사는 전체 구절도 남겨 단어 하나의 우연한 일치보다 우선합니다.
        if len(words) > 1:
            phrase = " ".join(words)
            if phrase not in terms:
                terms.append(phrase)
        for word in words:
            if word not in terms:
                terms.append(word)
    for word in split_search_terms(question):
        if word not in terms:
            terms.append(word)
    if asks_question_location(question):
        for word in ("질문", "문의", "태그", "채널", "슬랙", "slack"):
            if word not in terms:
                terms.append(word)
    # 검색 비용과 지나치게 넓은 일치를 제한합니다. 초과 시 뒤쪽 단서는 검색에서 빠집니다.
    return terms[:32]


def message_search_text(message):
    # ts·작성자 ID는 검색에 넣지 않고, Slack 형식의 <URL|표시명>에서는 표시명만 검색합니다.
    text = re.sub(r"<https?://[^|>]+(?:\|([^>]+))?>", lambda match: match[1] or "", message.get("text") or "")
    names = " ".join(file.get("name", "") for file in message.get("files") or [])
    return compact_text(text + " " + names)
