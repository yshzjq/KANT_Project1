"""Slack 작성일의 상대 날짜를 풀고, 현재 일정 질문의 검색 범위를 정합니다."""

# 날짜 기준을 두 개로 나눕니다: 질문의 '오늘'은 현재 KST, 원문의 '오늘'은 그 글의 작성일.
# 예: 9월 10일에 쓴 '내일 제출'은 9월 11일이며, 지금 읽는 날의 다음 날로 바꾸지 않습니다.
# 관련 검증: tests/test_answer_revision.py의 과거 공지·연말 경계·현재 시각 테스트.

import re
from datetime import date, datetime, timedelta, timezone

from search_terms import compact_text

KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"


def posted_date(message):
    return datetime.fromtimestamp(float(message["ts"]), KST).date()


def resolve_relative_dates(text, written_on, *, expand_week_ranges=True):
    week_start = written_on - timedelta(days=written_on.weekday())

    def week(match):
        label, weekday = match.groups()
        offset = 7 if label in ("다음 주", "다음주", "차주") else -7 if label in ("지난 주", "지난주") else 0
        start = week_start + timedelta(days=offset)
        if weekday:
            return (start + timedelta(days=WEEKDAYS.index(weekday))).isoformat()
        if not expand_week_ranges:
            return label
        return f"{start.isoformat()} ~ {(start + timedelta(days=6)).isoformat()}"

    def replace(part):
        part = re.sub(r"(이번\s?주|다음\s?주|지난\s?주|금주|차주)(?:\s*([월화수목금토일])요일)?", week, part)
        shifts = {"오늘": 0, "금일": 0, "내일": 1, "모레": 2, "어제": -1}
        return re.sub(
            r"(?<![가-힣A-Za-z])(?:오늘|금일|내일|모레|어제)",
            lambda match: (written_on + timedelta(days=shifts[match[0]])).isoformat(), part,
        )

    # URL은 한 글자도 변경하지 않습니다. 표시명·본문의 날짜만 변환합니다.
    return "".join(part if re.match(r"https?://", part) else replace(part)
                   for part in re.split(r"(https?://[^\s|>]+)", text))


def question_window(question, reference_time):
    # 반환값은 시작일·종료일을 모두 포함하는 범위입니다. None은 이 규칙으로 날짜 제한을 찾지 못했다는 뜻입니다.
    today = reference_time.astimezone(KST).date()
    for label, offset in (("오늘|금일", 0), ("내일", 1), ("모레", 2), ("어제", -1)):
        if re.search(label, question):
            target = today + timedelta(days=offset)
            return target, target
    for label, offset in ((r"이번\s?주|금주", 0), (r"다음\s?주|차주", 7), (r"지난\s?주", -7)):
        if re.search(label, question):
            start = today - timedelta(days=today.weekday()) + timedelta(days=offset)
            return start, start + timedelta(days=6)
    # '다음에 치를 시험'도 미래 일정입니다. 임의의 단어를 건너뛰면 무관한 '다음'까지 연결되므로
    # 일정 앞에 쓰이는 표현만 허용하고, 구체적인 오늘·이번 주 조건을 먼저 적용합니다.
    upcoming = r"(?:다음(?:번|에)?|다가오는)\s*(?:(?:치르는|치를|볼|보는|있을|열리는|열릴|진행되는|진행될|예정된)\s*)?"
    if re.search(upcoming + r"(?:시험|평가|퀘스트|과제|일정)", question):
        return today, date.max
    return None


def applies_to_window(message, window):
    """본문의 대상 날짜를 우선하고, 명시된 날짜가 없을 때만 게시일을 사용합니다."""
    return explain_window(message, window)["matches"]


def parsed_dates(text, written_on):
    """URL을 제외한 본문에서 해석 가능한 날짜와 위치를 반환합니다."""
    dates = []
    for match in re.finditer(r"(?<![\d.])(?:(\d{4})[-년./]\s*)?(\d{1,2})[-월./]\s*(\d{1,2})(?:일)?(?![\d.])", text):
        year, month, day = match.groups()
        # 연도가 없는 날짜는 작성 연도를 사용하되, 12월 글의 1월 일정은 다음 해로 추정합니다.
        inferred_year = written_on.year + int(written_on.month == 12 and int(month) == 1)
        try:
            value = date(int(year) if year else inferred_year, int(month), int(day))
        except ValueError:
            continue
        dates.append((match.start(), match.end(), value))
    return dates


def period_match(text, dates, window):
    start, end = window
    for _, _, value in dates:
        if start <= value <= end:
            return f"본문 날짜 {value}이 질문 기간에 포함됨"
    for previous, following in zip(dates, dates[1:]):
        between = text[previous[1]:following[0]]
        if re.fullmatch(r"\s*(?:~|부터|[-–])\s*", between) and previous[2] <= end and following[2] >= start:
            return f"본문 기간 {previous[2]} ~ {following[2]}이 질문 기간과 겹침"
    return None


def topic_date_clauses(original, written_on, keywords):
    """날짜와 주제가 연결된 문장만 모읍니다. 제목은 바로 다음의 날짜·동작만 있는 줄에 적용합니다."""
    clauses = []
    for paragraph in re.split(r"\n\s*\n", original):
        previous = ""
        for raw in re.split(r"\n+|(?<=[.!?])\s+", paragraph):
            specific = resolve_relative_dates(raw, written_on, expand_week_ranges=False)
            specific_dates = parsed_dates(specific, written_on)
            text = specific if specific_dates else resolve_relative_dates(raw, written_on)
            dates = specific_dates or parsed_dates(text, written_on)
            related = any(compact_text(word) in compact_text(raw) for word in keywords)
            if not related and previous and len(previous) <= 80:
                remainder = text
                for start, end, _ in reversed(dates):
                    remainder = remainder[:start] + remainder[end:]
                # '9/16까지 제출하세요'는 제목을 이어받지만 '9/16 점심 안내'는 별도 주제입니다.
                remainder = re.sub(r"까지|부터|제출|마감|진행|예정|완료|종료|시작|기한|일정|필수|해주세요|하세요|입니다|\W|\d", "", remainder)
                related = bool(dates) and not remainder and any(compact_text(word) in compact_text(previous) for word in keywords)
            if related and dates:
                clauses.append((text, dates, bool(specific_dates)))
            # 이미 날짜가 있는 문장은 다른 일정의 제목으로 재사용하지 않습니다.
            previous = raw if not dates else ""
    return clauses


def explain_window(message, window, *, keywords=None):
    """구체 날짜를 우선하고, 검색 주제를 지정하면 해당 문장·제목과 날짜의 연결도 확인합니다."""
    start, end = window
    written_on = posted_date(message)
    original = re.sub(r"https?://[^\s|>]+", "", message.get("text") or "")
    # '다음 주 프로젝트'가 9/14~9/20 전체의 제출 의무로 바뀌지 않게 합니다.
    # 날짜(9/14, 내일, 다음 주 월요일 등)가 있으면 넓은 주간 표현은 판정에서 제외합니다.
    specific = resolve_relative_dates(original, written_on, expand_week_ranges=False)
    specific_dates = parsed_dates(specific, written_on)
    text = specific if specific_dates else resolve_relative_dates(original, written_on)
    dates = specific_dates or parsed_dates(text, written_on)
    detail = {
        "posted_on": written_on.isoformat(),
        "resolved_dates": [value.isoformat() for _, _, value in dates],
    }
    priority_note = " (구체 날짜 우선, 넓은 주간 표현 제외)" if specific_dates and specific != resolve_relative_dates(original, written_on) else ""
    if keywords is not None and parsed_dates(resolve_relative_dates(original, written_on), written_on):
        scoped = topic_date_clauses(original, written_on, keywords)
        specific_scoped = [clause for clause in scoped if clause[2]]
        # 다른 주제의 구체 날짜 때문에 현재 과제의 주간 일정을 버리지 않습니다.
        considered = specific_scoped or scoped
        if considered:
            detail["resolved_dates"] = [value.isoformat() for _, values, _ in considered for _, _, value in values]
            note = " (같은 주제의 구체 날짜 우선)" if specific_scoped else ""
            for clause, clause_dates, _ in considered:
                reason = period_match(clause, clause_dates, window)
                if reason:
                    return {**detail, "matches": True, "reason": reason + note, "topic_evidence": clause.strip()}
            return {**detail, "matches": False,
                    "reason": "본문 날짜·기간이 질문 기간 밖임 (게시일로 대체하지 않음)" + note}
        return {**detail, "matches": False,
                "reason": "본문에 날짜가 있지만 같은 문장·인접 제목에서 검색 주제 연결을 확인하지 못함"}
    if dates:
        reason = period_match(text, dates, window)
        if not reason:
            return {**detail, "matches": False, "reason": "본문 날짜·기간이 질문 기간 밖임 (게시일로 대체하지 않음)" + priority_note}
        return {**detail, "matches": True, "reason": reason + priority_note}
    matches = start <= written_on <= end
    if matches and keywords is not None and not any(compact_text(word) in compact_text(original) for word in keywords):
        return {**detail, "matches": False, "reason": "게시일은 질문 기간 안이지만 검색 주제가 없음"}
    return {**detail, "matches": matches,
            "reason": "해석된 본문 날짜가 없어 게시일 사용: " + ("질문 기간 안" if matches else "질문 기간 밖")}
