"""검색어의 구절·띄어쓰기·조사를 정리합니다. 모델의 토큰 길이 설정과는 별개입니다."""

import re


STOP_WORDS = {
    "어떤", "어떻게", "무엇", "무엇을", "어디", "어디에", "누가", "제가", "저는",
    "하는", "하면", "해야", "하나요", "있나요", "인가요", "되나요", "해주세요",
    "알려주세요", "정리해주세요", "확인해주세요", "좋다고", "좋은", "좋을", "생기면",
    "있을까요", "안내했나요", "사용해도", "넘어가도", "대해", "대한", "것을",
    "오늘", "금일", "내일", "모레", "어제", "이번", "다음", "지난", "금주", "차주", "지금", "현재", "질문", "전에", "정리", "반드시", "the", "is", "a",
}


def compact_text(text):
    """기초 강화/기초강화, 데일리 퀘스트/데일리퀘스트를 같은 표현으로 검색합니다."""
    return re.sub(r"\s+", "", text.casefold())


def split_search_terms(text):
    words = re.findall(r"[가-힣a-zA-Z0-9]+(?:[.+#][a-zA-Z0-9]+)*%?", text.casefold())
    result = []
    for word in words:
        if word in STOP_WORDS:
            continue
        # 모델이 긴 구절을 내놓더라도 명사에 붙은 조사·어미를 한 번 더 정리합니다.
        stem = re.sub(
            r"(?:에서는|으로는|에게는|까지는|에서도|을까요|인가요|하나요|되나요|적으로|적인|하기|하면|에게|에서|으로|까지|부터|은|는|을|를|가|이|와|과)$",
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
    return terms[:32]


def message_search_text(message):
    # 시각·작성자 ID·URL 주소의 우연한 일치를 막고, 본문과 첨부파일 이름을 검색합니다.
    text = re.sub(r"<https?://[^|>]+(?:\|([^>]+))?>", lambda match: match[1] or "", message.get("text") or "")
    names = " ".join(file.get("name", "") for file in message.get("files") or [])
    return compact_text(text + " " + names)
