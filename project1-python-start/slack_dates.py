"""Slack 작성일의 상대 날짜를 풀고, 현재 일정 질문의 검색 범위를 정합니다."""

import re
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"


def posted_date(message):
    return datetime.fromtimestamp(float(message["ts"]), KST).date()


def resolve_relative_dates(text, written_on):
    week_start = written_on - timedelta(days=written_on.weekday())

    def week(match):
        label, weekday = match.groups()
        offset = 7 if label in ("다음 주", "다음주", "차주") else -7 if label in ("지난 주", "지난주") else 0
        start = week_start + timedelta(days=offset)
        if weekday:
            return (start + timedelta(days=WEEKDAYS.index(weekday))).isoformat()
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
    today = reference_time.astimezone(KST).date()
    for label, offset in (("오늘|금일", 0), ("내일", 1), ("모레", 2), ("어제", -1)):
        if re.search(label, question):
            target = today + timedelta(days=offset)
            return target, target
    for label, offset in ((r"이번\s?주|금주", 0), (r"다음\s?주|차주", 7), (r"지난\s?주", -7)):
        if re.search(label, question):
            start = today - timedelta(days=today.weekday()) + timedelta(days=offset)
            return start, start + timedelta(days=6)
    if re.search(r"다음\s*(?:시험|평가|퀘스트|과제|일정)", question):
        return today, date.max
    return None


def applies_to_window(message, window):
    """본문의 대상 날짜를 우선하고, 명시된 날짜가 없을 때만 게시일을 사용합니다."""
    start, end = window
    written_on = posted_date(message)
    text = resolve_relative_dates(message.get("text") or "", written_on)
    text = re.sub(r"https?://[^\s|>]+", "", text)
    dates = []
    for match in re.finditer(r"(?<![\d.])(?:(\d{4})[-년./]\s*)?(\d{1,2})[-월./]\s*(\d{1,2})(?:일)?(?![\d.])", text):
        year, month, day = match.groups()
        inferred_year = written_on.year + int(written_on.month == 12 and int(month) == 1)
        try:
            value = date(int(year) if year else inferred_year, int(month), int(day))
        except ValueError:
            continue
        dates.append((match.start(), match.end(), value))
        if start <= value <= end:
            return True
    # '2026-09-01 ~ 2026-09-30'처럼 요청 기간 전체를 포함하는 명시적 범위도 허용합니다.
    for previous, following in zip(dates, dates[1:]):
        between = text[previous[1]:following[0]]
        if re.fullmatch(r"\s*(?:~|부터|[-–])\s*", between) and previous[2] <= end and following[2] >= start:
            return True
    return not dates and start <= written_on <= end
