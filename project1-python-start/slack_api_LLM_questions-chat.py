"""읽는 순서: sync_slack_data → 각 단계의 보조 함수.

파일 읽기 → Slack 조회 → 변경 비교 → 변경이 있을 때만 저장.
ts는 메시지 식별값, thread_ts는 답글이 속한 원글의 식별값입니다.
"""

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import DEBUG
from slack_runtime import FileLock

CHANNEL_ID = "C0BBNNCS4BG"
LIMIT = 100  # API에서 한 페이지에 요청할 메시지 수
DATA_PATH = Path(__file__).resolve().parent / "data/private" / f"slack_{CHANNEL_ID}.json"


def sync_slack_data(data_path=DATA_PATH, read_pages=None, events=None, full_sync=True):
    """직접 수집과 상시 봇이 같은 JSON을 동시에 덮어쓰지 않게 잠급니다."""
    data_path = Path(data_path)
    with FileLock(data_path.with_suffix(".lock")):
        return _sync_slack_data(data_path, read_pages, events, full_sync)


def _sync_slack_data(data_path, read_pages, events, full_sync):
    """동기화의 전체 순서입니다. 실제 실행은 파일 맨 아래에서 시작합니다."""
    data_path = Path(data_path)
    # 평소에는 Slack을 조회합니다. 테스트할 때만 가짜 조회 함수를 전달합니다.
    read_pages = read_pages or read_all_pages
    sync_started = f"{time.time():.6f}"

    # 1. 저장된 기록을 읽고, ts로 메시지를 찾을 수 있는 사전을 만듭니다.
    saved_data = load_saved_data(data_path)
    existing = index_messages(saved_data.get("messages", []))
    print(f"기존 저장 메시지: {len(existing)}개", flush=True)

    # 2. 수정된 답글도 확인할 수 있도록 원글과 답글을 조회합니다.
    if full_sync or not saved_data.get("messages"):
        current, thread_versions = fetch_current_messages(read_pages, sync_started)
    else:
        current, thread_versions = fetch_event_messages(
            read_pages, sync_started, events or [], existing,
        )

    # 삭제 이벤트가 확인된 메시지만 제거합니다. 조회에서 누락된 기록은 보존합니다.
    deleted = {
        event["deleted_ts"] for event in (events or [])
        if event.get("subtype") == "message_deleted" and event.get("deleted_ts")
    }
    for message_ts in deleted:
        current.pop(message_ts, None)

    # 3. 새 메시지와 내용이 달라진 메시지의 수를 셉니다.
    new_count, changed_count = count_changes(existing, current)
    deleted_count = len(deleted.intersection(existing))
    if data_path.exists() and new_count == 0 and changed_count == 0 and not deleted_count:
        print("조회한 범위에 신규·변경 데이터가 없습니다. 기존 대화기록 파일을 사용합니다.")
        return saved_data

    # 4. 같은 ts는 최신 내용으로 교체하고, 새로운 ts는 추가합니다.
    # 조회에서 사라진 기록은 보존합니다. 접근 제한과 삭제를 구별할 수 없기 때문입니다.
    merged = existing.copy()
    merged.update(current)
    for message_ts in deleted:
        merged.pop(message_ts, None)
    versions = saved_data.get("thread_versions", {}).copy()
    versions.update(thread_versions)
    for message_ts in deleted:
        versions.pop(message_ts, None)
    save_data = saved_data.copy()
    save_data.update({
        "channel_id": CHANNEL_ID,
        "last_sync_started_ts": sync_started,
        "thread_versions": versions,
        "messages": sorted(merged.values(), key=lambda message: Decimal(message["ts"])),
    })

    # 5. 모든 조회·비교가 끝난 뒤 파일을 저장합니다.
    save_json(data_path, save_data)
    print(f"대화기록 저장 완료: 신규 {new_count}개 / 변경 {changed_count}개 / 삭제 {deleted_count}개")
    if DEBUG:
        print(f"전체 저장 메시지: {len(merged)}개 / 저장 위치: {data_path}")
    return save_data


# --- 파일 읽기·비교·저장 ---

def load_saved_data(data_path):
    """처음 실행하면 빈 사전을, 기존 파일이 있으면 JSON 내용을 반환합니다."""
    if not data_path.exists():
        return {}

    data = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("channel_id") != CHANNEL_ID:
        raise RuntimeError("저장 파일의 채널 ID가 다릅니다.")
    return data


def index_messages(messages):
    """메시지 목록을 {ts: 메시지} 형태로 바꿉니다. 같은 ts는 하나만 남습니다."""
    if not isinstance(messages, list):
        raise ValueError("메시지 목록의 형식이 잘못됐습니다.")

    indexed = {}
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("ts"), str):
            raise ValueError("메시지의 ts가 없거나 형식이 잘못됐습니다.")

        # Decimal은 소수점 아래 자리까지 정확하게 비교하기 위해 사용합니다.
        timestamp = Decimal(message["ts"])
        if not timestamp.is_finite() or timestamp < 0:
            raise ValueError("메시지의 ts가 유효한 시각이 아닙니다.")
        indexed[message["ts"]] = message
    return indexed


def count_changes(existing, current):
    """ts가 처음 보이면 신규, 같은 ts의 데이터가 다르면 변경으로 셉니다."""
    new_count = 0
    changed_count = 0
    for message_ts, message in current.items():
        if message_ts not in existing:
            new_count += 1
        elif message != existing[message_ts]:
            # 본문뿐 아니라 첨부파일 등 메시지의 다른 정보도 비교합니다.
            changed_count += 1
    return new_count, changed_count


def save_json(data_path, data):
    """임시 파일에 먼저 쓴 뒤 교체하여, 저장 중 오류가 나도 기존 기록을 지킵니다."""
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=data_path.parent,
            prefix=data_path.stem + "_", suffix=".tmp", delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=2)

        # Windows에서는 파일을 닫은 뒤 교체해야 합니다.
        temp_path.replace(data_path)
    finally:
        # 저장에 실패해 남은 임시 파일도 정리합니다.
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


# --- Slack에서 원글과 답글 모으기 ---

def fetch_event_messages(read_pages, sync_started, events, existing):
    """이벤트는 감지에만 쓰고, 해당 메시지·스레드의 최신 본문은 API로 조회합니다."""
    roots, threads, deleted = set(), set(), set()
    for event in events:
        if event.get("subtype") == "message_deleted":
            message_ts = event.get("deleted_ts")
            deleted.add(message_ts)
            previous = event.get("previous_message") or existing.get(message_ts, {})
            parent_ts = previous.get("thread_ts")
            if parent_ts and parent_ts != message_ts:
                threads.add(parent_ts)
            continue
        message = event.get("message") or event
        message_ts = message.get("ts")
        if not message_ts:
            return fetch_current_messages(read_pages, sync_started)
        parent_ts = message.get("thread_ts")
        if parent_ts and parent_ts != message_ts:
            threads.add(parent_ts)
        elif message.get("reply_count", 0) or event.get("subtype") == "message_replied":
            threads.add(message_ts)
        else:
            roots.add(message_ts)

    current, versions = {}, {}
    limited = False
    for message_ts in sorted(roots - threads - deleted, key=Decimal):
        messages, page_limited = read_pages(
            "conversations.history", oldest=message_ts, latest=message_ts, inclusive="true",
        )
        current.update(index_messages(messages))
        limited = limited or page_limited
    for parent_ts in sorted(threads - deleted, key=Decimal):
        messages, page_limited = read_pages(
            "conversations.replies", ts=parent_ts, latest=sync_started,
        )
        current.update(index_messages(messages))
        limited = limited or page_limited
        parent = current.get(parent_ts, {})
        versions[parent_ts] = {
            "reply_count": parent.get("reply_count", 0), "latest_reply": parent.get("latest_reply"),
        }
    if limited:
        print("일부 기록의 접근이 제한되었습니다. 조회 가능한 범위에서만 갱신합니다.")
    return current, versions


def fetch_current_messages(read_pages, sync_started):
    """원글·답글을 조회하고 {ts: 메시지}와 스레드 정보를 반환합니다."""
    print("Slack 원글·답글의 신규 데이터와 수정 내용을 확인합니다.", flush=True)
    channel_messages, limited = read_pages(
        "conversations.history", latest=sync_started,
    )
    current = index_messages(channel_messages)
    thread_ids = find_thread_ids(channel_messages)
    thread_versions = {}

    # 답글 수와 마지막 답글 시각이 같아도 본문은 수정됐을 수 있으므로 모두 확인합니다.
    for index, parent_ts in enumerate(thread_ids, start=1):
        if DEBUG:
            print(f"스레드 {index}/{len(thread_ids)}: 답글 조회", flush=True)

        replies, thread_limited = read_pages(
            "conversations.replies", ts=parent_ts, latest=sync_started,
        )
        limited = limited or thread_limited
        current.update(index_messages(replies))

        # 기존 JSON 형식을 유지하기 위한 스레드 정보입니다. 조회 생략에는 쓰지 않습니다.
        parent = current.get(parent_ts) or {}
        thread_versions[parent_ts] = {
            "reply_count": parent.get("reply_count", 0),
            "latest_reply": parent.get("latest_reply"),
        }

    if limited:
        print("일부 기록의 접근이 제한되었습니다. 조회 가능한 범위에서만 변경을 확인합니다.")
    return current, thread_versions


def find_thread_ids(messages):
    """답글을 조회할 원글 ID를 중복 없이 모아 시간순으로 반환합니다."""
    thread_ids = set()
    for message in messages:
        message_ts = message["ts"]
        parent_ts = message.get("thread_ts")

        if parent_ts and parent_ts != message_ts:
            # 채널에 공유된 답글이라면, 그 답글의 원글을 조회합니다.
            thread_ids.add(parent_ts)
        elif message.get("reply_count", 0) > 0:
            thread_ids.add(message_ts)
    return sorted(thread_ids, key=Decimal)


# --- Slack API 통신: 한 페이지 요청 → 다음 페이지 반복 ---

def read_all_pages(method, **params):
    """cursor(다음 페이지 위치)가 없어질 때까지 메시지를 모읍니다."""
    messages = []
    cursor = ""
    seen_cursors = set()
    limited = False

    while True:
        page_params = {"channel": CHANNEL_ID, "limit": LIMIT, **params}
        if cursor:
            page_params["cursor"] = cursor
        data = call_slack(method, **page_params)

        page_messages = data.get("messages")
        index_messages(page_messages)  # 잘못된 응답을 빈 기록으로 취급하지 않습니다.
        messages.extend(page_messages)
        limited = limited or bool(data.get("is_limited"))
        if DEBUG:
            print(f"{method}: 누적 {len(messages)}개 조회", flush=True)

        metadata = data.get("response_metadata") or {}
        next_cursor = (metadata.get("next_cursor") or "").strip()
        if not next_cursor:
            if data.get("has_more"):
                raise RuntimeError("추가 기록이 있지만 다음 페이지 정보가 없습니다.")
            break
        if next_cursor in seen_cursors:
            raise RuntimeError("같은 페이지 정보가 반복됩니다.")

        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return messages, limited


def call_slack(method, **params):
    """Slack API를 한 번 호출합니다. 호출 제한(429)이면 최대 세 번 재시도합니다."""
    token = os.getenv("SLACK_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SLACK_TOKEN 환경변수를 찾을 수 없습니다.")

    # method는 API 이름, params는 channel·ts 같은 조회 조건입니다.
    # 토큰은 URL에 넣지 않고 인증 헤더로 전달합니다.
    url = f"https://slack.com/api/{method}?{urlencode(params)}"
    request = Request(url, headers={"Authorization": f"Bearer {token}"})

    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
        except HTTPError as error:
            if error.code == 429 and attempt < 3:
                seconds = max(1, int(error.headers.get("Retry-After", "60")))
                error.close()
                print(f"호출 제한: {seconds}초 후 재시도합니다.", flush=True)
                time.sleep(seconds)
                continue

            status = error.code
            error.close()
            raise RuntimeError(f"Slack HTTP 오류: {status}") from None
        except URLError:
            raise RuntimeError("Slack 연결에 실패했습니다.") from None

        if not isinstance(data, dict):
            raise ValueError("Slack 응답이 객체 형식이 아닙니다.")
        if not data.get("ok"):
            raise RuntimeError(f"{method} 조회 실패: {data.get('error')}")
        return data


# 이 파일을 직접 실행할 때만 동기화를 시작합니다.
if __name__ == "__main__":
    try:
        sync_slack_data()
    except (RuntimeError, OSError, ValueError, ArithmeticError) as error:
        raise SystemExit(f"\n동기화 실패: {error}") from None
