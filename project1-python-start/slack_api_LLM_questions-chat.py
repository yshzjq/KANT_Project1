import os
import json
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import DEBUG


# ① 기본 설정
SLACK_TOKEN = os.getenv("SLACK_TOKEN", "").strip()

if not SLACK_TOKEN:
    raise SystemExit("SLACK_TOKEN 환경변수를 찾을 수 없습니다.")

CHANNEL_ID = "C0BBNNCS4BG"
LIMIT = 100
SYNC_STARTED = f"{time.time():.6f}"

DATA_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "private"
    / f"slack_{CHANNEL_ID}.json"
)


# ② Slack API를 호출합니다.
def call_slack(method, **params):
    url = f"https://slack.com/api/{method}?{urlencode(params)}"

    request = Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
    )

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

        if not data.get("ok"):
            raise RuntimeError(
                f"{method} 조회 실패: {data.get('error')}"
            )

        return data




# ③ 한 API의 모든 페이지를 가져옵니다.
# 채널 조회와 스레드 조회에서 공통으로 사용합니다.
def read_all_pages(method, **params):
    messages = []
    cursor = ""
    seen_cursors = set()
    limited = False

    while True:
        page_params = {
            "channel": CHANNEL_ID,
            "limit": LIMIT,
            **params,
        }

        if cursor:
            page_params["cursor"] = cursor

        data = call_slack(method, **page_params)

        messages.extend(data.get("messages", []))
        limited = limited or bool(data.get("is_limited"))

        if DEBUG : 
            print(f"{method}: 누적 {len(messages)}개 조회", flush=True)

        metadata = data.get("response_metadata") or {}
        next_cursor = (metadata.get("next_cursor") or "").strip()

        if not next_cursor:
            if data.get("has_more"):
                raise RuntimeError(
                    "추가 기록이 있지만 다음 페이지 정보가 없습니다."
                )
            break

        if next_cursor in seen_cursors:
            raise RuntimeError("같은 페이지 정보가 반복됩니다.")

        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return messages, limited




try:
    # ④ 기존 저장 데이터를 읽습니다.
    saved_data = {}

    if DATA_PATH.exists():
        saved_data = json.loads(
            DATA_PATH.read_text(encoding="utf-8")
        )

        if saved_data.get("channel_id") != CHANNEL_ID:
            raise RuntimeError("저장 파일의 채널 ID가 다릅니다.")

    all_messages = {
        message["ts"]: message
        for message in saved_data.get("messages", [])
    }

    existing_ids = set(all_messages)

    # 스레드를 마지막으로 조회했을 때의 답글 정보를 보관합니다.
    # 기존 파일에 이 항목이 없으면 첫 실행에서 스레드를 모두 조회합니다.
    thread_versions = saved_data.get("thread_versions", {})

    print(f"기존 저장 메시지: {len(all_messages)}개")


    # ⑤ 원글 목록을 다시 조회합니다.
    # 오래된 원글의 reply_count와 latest_reply도 확인해야 하므로
    # 여기에는 마지막 메시지 시각인 oldest를 넣지 않습니다.
    channel_messages, limited = read_all_pages(
        "conversations.history",
        latest=SYNC_STARTED,
    )

    current_channel = {
        message["ts"]: message
        for message in channel_messages
    }

    all_messages.update(current_channel)


    # ⑥ 답글이 있는 원글의 ID를 모읍니다.
    thread_ids = set()

    for message in channel_messages:
        message_ts = message["ts"]
        parent_ts = message.get("thread_ts")

        if parent_ts and parent_ts != message_ts:
            # 채널에도 공유된 스레드 답글인 경우입니다.
            thread_ids.add(parent_ts)

        elif message.get("reply_count", 0) > 0:
            thread_ids.add(message_ts)


    # ⑦ 처음 수집하거나 답글 정보가 바뀐 스레드를 조회합니다.
    fetched_threads = 0

    for index, parent_ts in enumerate(
        sorted(thread_ids, key=Decimal),
        start=1,
    ):
        parent = current_channel.get(parent_ts)

        # 원글 정보가 부족하면 생략하지 않고 다시 조회합니다.
        version = None

        if parent and parent.get("latest_reply"):
            version = {
                "reply_count": parent.get("reply_count", 0),
                "latest_reply": parent["latest_reply"],
            }

        if (
            version is not None
            and thread_versions.get(parent_ts) == version
        ):
            if DEBUG : print(f"스레드 {index}/{len(thread_ids)}: 기존 자료 사용")
            continue

        if DEBUG : print(f"스레드 {index}/{len(thread_ids)}: 답글 조회")

        replies, thread_limited = read_all_pages(
            "conversations.replies",
            ts=parent_ts,
        )

        limited = limited or thread_limited

        # 조회 결과에는 원글도 포함될 수 있습니다.
        # ts가 같으면 덮어써서 중복 저장을 방지합니다.
        for message in replies:
            all_messages[message["ts"]] = message

        thread_versions[parent_ts] = version
        fetched_threads += 1


    # ⑧ 원글과 답글을 시간순으로 정렬합니다.
    sorted_messages = sorted(
        all_messages.values(),
        key=lambda message: Decimal(message["ts"]),
    )

    save_data = {
        "channel_id": CHANNEL_ID,
        "last_sync_started_ts": SYNC_STARTED,
        "thread_versions": thread_versions,
        "messages": sorted_messages,
    }


    # ⑨ 모든 조회가 성공한 뒤 파일을 저장합니다.
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    temp_path = DATA_PATH.with_suffix(".tmp")

    temp_path.write_text(
        json.dumps(save_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp_path.replace(DATA_PATH)

    new_count = len(set(all_messages) - existing_ids)


    if DEBUG : 
        print(f"\n새로 추가된 메시지: {new_count}개")
        print(f"이번에 조회한 스레드: {fetched_threads}개")
        print(f"전체 저장 메시지: {len(all_messages)}개")
        print(f"저장 위치: {DATA_PATH}")

    if limited:
        if DEBUG : 
            print("※ 이번 조회에서 일부 이전 기록의 접근이 제한되었습니다.")

except (RuntimeError, OSError, ValueError) as error:
    raise SystemExit(f"\n동기화 실패: {error}") from None