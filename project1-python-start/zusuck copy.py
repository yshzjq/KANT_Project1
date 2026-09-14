
# 운영체제 환경변수에서 Slack 토큰을 읽기 위해 사용합니다.
import os

# Slack API 응답과 로컬 저장 파일을 JSON 형식으로 읽고 쓰기 위해 사용합니다.
import json

# 현재 시각을 얻거나 API 호출 제한 시 잠시 대기하기 위해 사용합니다.
import time

# Slack timestamp는 "1723456789.123456"처럼 소수점이 포함된 문자열입니다.
# float보다 Decimal을 사용하면 이런 값을 정밀하게 비교하고 정렬할 수 있습니다.
from decimal import Decimal

# 운영체제에 상관없이 파일 경로를 안전하고 편리하게 다루기 위해 사용합니다.
from pathlib import Path

# Slack API 호출에서 발생할 수 있는 HTTP/네트워크 오류를 구분하기 위해 사용합니다.
from urllib.error import HTTPError, URLError

# Python dict를 URL 쿼리 문자열로 변환하기 위해 사용합니다.
# 예: {"channel": "C123", "limit": 100} -> "channel=C123&limit=100"
from urllib.parse import urlencode

# HTTP 요청 객체를 만들고 Slack API에 실제 요청을 보내기 위해 사용합니다.
from urllib.request import Request, urlopen


# ============================================================
# ① 기본 설정
# Slack 인증 정보, 조회할 채널, 페이지 크기, 저장 파일 위치를 준비합니다.
# ============================================================

# SLACK_TOKEN 환경변수에서 Slack API 인증 토큰을 읽습니다.
# 토큰을 코드에 직접 적지 않으면 Git 등에 민감한 정보가 노출될 위험을 줄일 수 있습니다.
SLACK_TOKEN = os.getenv("SLACK_TOKEN", "").strip()

# 토큰이 없다면 Slack API를 호출할 수 없으므로 프로그램을 즉시 종료합니다.
if not SLACK_TOKEN:
    raise SystemExit("SLACK_TOKEN 환경변수를 찾을 수 없습니다.")

# 메시지를 수집할 Slack 채널의 고유 ID입니다.
CHANNEL_ID = "C0BBNNCS4BG"

# 한 번의 Slack API 요청에서 가져올 메시지 수입니다.
# 메시지가 LIMIT보다 많으면 cursor를 이용하여 다음 페이지를 계속 조회합니다.
LIMIT = 100

# 동기화를 시작한 순간의 Unix timestamp를 문자열로 저장합니다.
#
# 예:
# "1789381234.123456"
#
# Slack의 메시지 ts와 비슷한 형식으로 만들기 위해 소수점 아래 6자리까지 저장합니다.
# 이후 conversations.history의 latest 값으로 사용하여
# "동기화를 시작한 이후 새로 생긴 메시지"가 조회 결과에 섞이지 않도록 기준 시각을 고정합니다.
SYNC_STARTED = f"{time.time():.6f}"

# 현재 Python 파일을 기준으로 Slack 메시지를 저장할 JSON 파일 경로를 만듭니다.
#
# 최종 형태:
# 현재폴더/data/private/slack_C0BBNNCS4BG.json
DATA_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "private"
    / f"slack_{CHANNEL_ID}.json"
)


# ============================================================
# ② Slack API 호출 함수
#
# 입력:
#   method : Slack API 메서드 이름
#   params : API에 전달할 추가 파라미터
#
# 출력:
#   Slack이 반환한 JSON 데이터를 Python dict로 반환합니다.
#
# 주요 역할:
#   - 인증
#   - HTTP 요청
#   - 호출 제한(429) 재시도
#   - 오류 처리
# ============================================================
def call_slack(method, **params):

    # method에 따라 Slack API 주소를 동적으로 만듭니다.
    #
    # 예:
    # method = "conversations.history"
    #
    # params =
    # {
    #     "channel": "C123",
    #     "limit": 100
    # }
    #
    # 최종 URL:
    # https://slack.com/api/conversations.history?channel=C123&limit=100
    url = f"https://slack.com/api/{method}?{urlencode(params)}"

    # Slack API에 보낼 HTTP 요청 객체를 만듭니다.
    # Authorization 헤더의 Bearer 토큰으로 Slack에게 인증 정보를 전달합니다.
    request = Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
    )

    # 호출 제한이나 일시적인 문제에 대비하여 최대 4번까지 요청을 시도합니다.
    #
    # attempt 값:
    # 0, 1, 2, 3
    for attempt in range(4):
        try:
            # Slack API로 실제 요청을 보냅니다.
            # timeout=30은 최대 30초까지만 응답을 기다린다는 뜻입니다.
            #
            # with를 사용하면 응답 처리가 끝난 뒤 연결을 자동으로 닫습니다.
            with urlopen(request, timeout=30) as response:

                # Slack의 JSON 응답을 Python 객체(dict)로 변환합니다.
                data = json.load(response)

        # HTTP 상태 코드가 4xx, 5xx인 경우 처리합니다.
        except HTTPError as error:

            # HTTP 429는 API 요청을 너무 많이 보내 Slack이 호출 제한을 건 상황입니다.
            # 아직 마지막 시도가 아니라면 Slack이 알려준 시간만큼 기다린 뒤 다시 요청합니다.
            if error.code == 429 and attempt < 3:

                # Retry-After 헤더에는 몇 초 뒤 재시도해야 하는지가 들어 있습니다.
                # 헤더가 없다면 기본값으로 60초를 사용합니다.
                #
                # max(1, ...)을 사용해 최소 1초 이상 기다리게 합니다.
                seconds = max(1, int(error.headers.get("Retry-After", "60")))

                # 오류 응답 객체를 닫습니다.
                error.close()

                print(f"호출 제한: {seconds}초 후 재시도합니다.", flush=True)

                # 지정된 시간 동안 프로그램 실행을 잠시 멈춥니다.
                time.sleep(seconds)

                # 현재 반복문의 나머지를 건너뛰고 다음 API 요청을 다시 시도합니다.
                continue

            # 429 이외의 HTTP 오류이거나,
            # 429가 계속 발생해 재시도 횟수를 모두 사용한 경우입니다.
            status = error.code
            error.close()

            # 상위 코드에서 처리할 수 있도록 RuntimeError로 변환합니다.
            raise RuntimeError(f"Slack HTTP 오류: {status}") from None

        # URL 접속 자체가 실패한 경우입니다.
        # 예: 네트워크 장애, DNS 문제 등
        except URLError:
            raise RuntimeError("Slack 연결에 실패했습니다.") from None

        # HTTP 요청이 성공했더라도 Slack API 자체에서
        # {"ok": false, "error": "..."}를 반환할 수 있습니다.
        if not data.get("ok"):
            raise RuntimeError(
                f"{method} 조회 실패: {data.get('error')}"
            )

        # 정상 응답이면 Python dict를 호출한 쪽으로 반환합니다.
        return data


# ============================================================
# ③ 한 Slack API의 모든 페이지를 가져오는 함수
#
# Slack API는 메시지가 많으면 한 번에 전부 주지 않고
# 여러 페이지로 나누어 반환합니다.
#
# 이 함수는 next_cursor가 없어질 때까지 반복하여
# 모든 메시지를 하나의 리스트로 합칩니다.
#
# 반환값:
#   messages : 모든 페이지에서 모은 메시지 리스트
#   limited  : Slack이 일부 과거 기록 접근을 제한했는지 여부
# ============================================================

# ③ 한 API의 모든 페이지를 가져옵니다.
# 채널 조회와 스레드 조회에서 공통으로 사용합니다.
def read_all_pages(method, **params):

    # 각 페이지에서 가져온 Slack 메시지를 계속 추가할 리스트입니다.
    messages = []

    # Slack pagination(페이지 이동)에 사용하는 cursor입니다.
    # 첫 요청에는 cursor가 없으므로 빈 문자열로 시작합니다.
    cursor = ""

    # 같은 cursor가 반복되는 비정상 상황을 감지하기 위한 set입니다.
    seen_cursors = set()

    # Slack이 일부 이전 메시지를 제한했는지 기록합니다.
    limited = False

    # 다음 cursor가 없어질 때까지 계속 페이지를 조회합니다.
    while True:

        # 모든 API 요청에 공통으로 전달할 기본 파라미터를 만듭니다.
        #
        # **params는 함수 호출 때 전달된 추가 파라미터를 펼쳐 넣는 문법입니다.
        #
        # 예:
        # params = {"latest": "..."}
        #
        # 결과:
        # {
        #     "channel": CHANNEL_ID,
        #     "limit": 100,
        #     "latest": "..."
        # }
        page_params = {
            "channel": CHANNEL_ID,
            "limit": LIMIT,
            **params,
        }

        # 두 번째 페이지부터는 이전 응답에서 받은 cursor를 전달합니다.
        if cursor:
            page_params["cursor"] = cursor

        # 실제 Slack API 호출은 call_slack() 함수에 맡깁니다.
        data = call_slack(method, **page_params)

        # 이번 페이지의 메시지를 기존 messages 리스트 뒤에 추가합니다.
        #
        # append는 리스트 자체를 하나의 요소로 추가하지만,
        # extend는 리스트 안의 요소들을 각각 추가합니다.
        messages.extend(data.get("messages", []))

        # 현재까지 한 번이라도 is_limited=True가 있었다면
        # limited 값을 계속 True로 유지합니다.
        limited = limited or bool(data.get("is_limited"))

        print(f"{method}: 누적 {len(messages)}개 조회", flush=True)

        # Slack pagination 정보는 response_metadata 안에 들어 있습니다.
        # response_metadata가 None이면 빈 dict를 사용합니다.
        metadata = data.get("response_metadata") or {}

        # 다음 페이지를 조회할 때 사용할 cursor를 가져옵니다.
        # 값이 없거나 None이면 빈 문자열로 처리합니다.
        next_cursor = (metadata.get("next_cursor") or "").strip()

        # next_cursor가 없다는 것은 일반적으로 마지막 페이지라는 의미입니다.
        if not next_cursor:

            # 그런데 has_more=True라면
            # 추가 데이터가 있다고 Slack이 알려주면서 cursor는 주지 않은 모순된 상태입니다.
            # 이런 상황에서 조용히 종료하면 메시지가 누락될 수 있으므로 오류로 처리합니다.
            if data.get("has_more"):
                raise RuntimeError(
                    "추가 기록이 있지만 다음 페이지 정보가 없습니다."
                )

            # 모든 페이지를 조회했으므로 while 반복을 종료합니다.
            break

        # 이미 사용했던 cursor가 또 등장하면
        # 같은 페이지를 무한 반복할 가능성이 있으므로 중단합니다.
        if next_cursor in seen_cursors:
            raise RuntimeError("같은 페이지 정보가 반복됩니다.")

        # 새 cursor를 기록하여 이후 중복 여부를 확인할 수 있게 합니다.
        seen_cursors.add(next_cursor)

        # 다음 반복에서 사용할 cursor로 교체합니다.
        cursor = next_cursor

    # 모든 메시지와 접근 제한 여부를 함께 반환합니다.
    return messages, limited


# ============================================================
# 아래부터 실제 Slack 동기화 과정입니다.
#
# 전체 흐름:
#
# 기존 JSON 읽기
#     ↓
# Slack 채널 원글 조회
#     ↓
# 답글이 있는 스레드 확인
#     ↓
# 변경된 스레드만 다시 조회
#     ↓
# 기존 데이터 + 새 데이터 합치기
#     ↓
# 시간순 정렬
#     ↓
# JSON 파일 저장
# ============================================================

# 파일 처리, JSON 변환, Slack API 처리 과정에서 발생하는 오류를
# 마지막에 한 번에 사용자 친화적인 메시지로 바꾸기 위해 try로 감쌉니다.
try:
    # ========================================================
    # ④ 기존 저장 데이터를 읽습니다.
    # 이전 동기화 결과가 있다면 불러와 새 데이터와 합치기 위한 단계입니다.
    # ========================================================

    # 저장 파일이 아직 없는 첫 실행을 대비하여 빈 dict로 시작합니다.
    saved_data = {}

    # 이전에 저장한 JSON 파일이 존재하면 읽습니다.
    if DATA_PATH.exists():

        # 파일 전체 내용을 문자열로 읽은 뒤 JSON을 Python 객체로 변환합니다.
        saved_data = json.loads(
            DATA_PATH.read_text(encoding="utf-8")
        )

        # 잘못된 채널 데이터가 같은 파일에 섞이는 것을 방지합니다.
        if saved_data.get("channel_id") != CHANNEL_ID:
            raise RuntimeError("저장 파일의 채널 ID가 다릅니다.")

    # 이전에 저장된 메시지를
    #
    # {
    #     "Slack ts": 메시지 dict
    # }
    #
    # 형태의 dict로 바꿉니다.
    #
    # ts는 Slack 메시지의 고유한 timestamp 역할을 하므로
    # dict의 key로 사용하면 같은 메시지를 쉽게 덮어써 중복을 방지할 수 있습니다.
    all_messages = {
        message["ts"]: message
        for message in saved_data.get("messages", [])
    }

    # 동기화 전부터 존재하던 메시지 ID(ts)들을 보관합니다.
    # 나중에 새롭게 추가된 메시지가 몇 개인지 계산할 때 사용합니다.
    existing_ids = set(all_messages)

    # 스레드를 마지막으로 조회했을 때의 답글 정보를 보관합니다.
    # 기존 파일에 이 항목이 없으면 첫 실행에서 스레드를 모두 조회합니다.
    #
    # 예상 형태:
    # {
    #     "원글 ts": {
    #         "reply_count": 5,
    #         "latest_reply": "..."
    #     }
    # }
    #
    # 답글 개수와 마지막 답글 시각이 이전과 같다면
    # 그 스레드는 다시 API로 조회하지 않아도 됩니다.
    thread_versions = saved_data.get("thread_versions", {})

    print(f"기존 저장 메시지: {len(all_messages)}개")


    # ========================================================
    # ⑤ 원글 목록을 다시 조회합니다.
    #
    # 채널 전체 원글 정보를 다시 확인하여
    # 기존 스레드에 새로운 답글이 생겼는지도 찾아냅니다.
    # ========================================================

    # ⑤ 원글 목록을 다시 조회합니다.
    # 오래된 원글의 reply_count와 latest_reply도 확인해야 하므로
    # 여기에는 마지막 메시지 시각인 oldest를 넣지 않습니다.
    #
    # latest=SYNC_STARTED:
    # 동기화를 시작한 시각 이전의 메시지만 조회합니다.
    #
    # 반환:
    # channel_messages = Slack 메시지 리스트
    # limited = 일부 이전 기록 접근 제한 여부
    channel_messages, limited = read_all_pages(
        "conversations.history",
        latest=SYNC_STARTED,
    )

    # 이번 Slack 조회에서 얻은 채널 메시지를 ts 기준 dict로 변환합니다.
    #
    # 이렇게 하면 특정 parent_ts의 원글을
    # current_channel.get(parent_ts)로 빠르게 찾을 수 있습니다.
    current_channel = {
        message["ts"]: message
        for message in channel_messages
    }

    # 기존에 저장된 메시지에 이번에 조회한 최신 채널 메시지를 합칩니다.
    #
    # 같은 ts가 있다면 새 데이터가 기존 데이터를 덮어씁니다.
    # 따라서 reply_count, latest_reply 같은 최신 정보도 갱신됩니다.
    all_messages.update(current_channel)


    # ========================================================
    # ⑥ 답글이 있는 원글의 ID(ts)를 모읍니다.
    #
    # 이후 conversations.replies API를 호출할 대상을 찾는 단계입니다.
    # ========================================================

    # 스레드 원글의 ts를 중복 없이 저장하기 위해 set을 사용합니다.
    thread_ids = set()

    # 이번에 조회한 채널 메시지를 하나씩 검사합니다.
    for message in channel_messages:

        # 현재 메시지 자신의 timestamp입니다.
        message_ts = message["ts"]

        # 이 메시지가 스레드 답글이면
        # thread_ts에는 부모 원글의 timestamp가 들어 있습니다.
        parent_ts = message.get("thread_ts")

        # thread_ts가 존재하고 자신의 ts와 다르면
        # 현재 메시지는 원글이 아니라 스레드의 답글입니다.
        if parent_ts and parent_ts != message_ts:
            # 채널에도 공유된 스레드 답글인 경우입니다.

            # 답글 자체가 아니라 원글의 ts를 조회 대상으로 저장합니다.
            thread_ids.add(parent_ts)

        # 현재 메시지가 원글이고 reply_count가 1개 이상이라면
        # 이 원글 역시 스레드 조회 대상입니다.
        elif message.get("reply_count", 0) > 0:
            thread_ids.add(message_ts)


    # ========================================================
    # ⑦ 처음 수집하거나 답글 정보가 바뀐 스레드만 조회합니다.
    #
    # 모든 스레드를 매번 다시 요청하지 않고
    # reply_count / latest_reply가 달라진 경우에만 API를 호출하여
    # 불필요한 Slack API 요청을 줄입니다.
    # ========================================================

    # 이번 실행에서 실제로 API 조회한 스레드 개수를 셉니다.
    fetched_threads = 0

    # Slack ts는 문자열이지만 시간순으로 정렬해야 하므로
    # Decimal로 변환하여 정확하게 정렬합니다.
    #
    # enumerate(..., start=1):
    # index를 1부터 시작하게 하여 진행률 출력에 사용합니다.
    for index, parent_ts in enumerate(
        sorted(thread_ids, key=Decimal),
        start=1,
    ):

        # 현재 채널 조회 결과에서 원글 정보를 찾습니다.
        parent = current_channel.get(parent_ts)

        # 원글 정보가 부족하면 생략하지 않고 다시 조회합니다.
        #
        # version은 현재 Slack에서 확인된 스레드 상태를 나타냅니다.
        version = None

        # latest_reply가 존재한다면
        # 답글 개수와 마지막 답글 시각을 버전 정보로 사용합니다.
        if parent and parent.get("latest_reply"):
            version = {
                "reply_count": parent.get("reply_count", 0),
                "latest_reply": parent["latest_reply"],
            }

        # 이전에 저장한 스레드 버전과 현재 버전이 완전히 같다면
        # 새로운 답글이 생기지 않았다고 판단합니다.
        if (
            version is not None
            and thread_versions.get(parent_ts) == version
        ):
            print(f"스레드 {index}/{len(thread_ids)}: 기존 자료 사용")

            # Slack API 재조회 없이 다음 스레드로 이동합니다.
            continue

        print(f"스레드 {index}/{len(thread_ids)}: 답글 조회")

        # conversations.replies API로
        # 해당 원글과 그 아래의 모든 답글을 페이지 끝까지 조회합니다.
        replies, thread_limited = read_all_pages(
            "conversations.replies",
            ts=parent_ts,
        )

        # 채널 조회 또는 스레드 조회 중 하나라도 제한이 있었다면
        # 최종 limited 값을 True로 유지합니다.
        limited = limited or thread_limited

        # 조회 결과에는 원글도 포함될 수 있습니다.
        # ts가 같으면 덮어써서 중복 저장을 방지합니다.
        for message in replies:
            all_messages[message["ts"]] = message

        # 현재 스레드 상태를 저장합니다.
        # 다음 실행에서는 이 값과 비교하여 변경 여부를 확인합니다.
        thread_versions[parent_ts] = version

        # 실제로 새로 API 조회한 스레드 수를 증가시킵니다.
        fetched_threads += 1


    # ========================================================
    # ⑧ 원글과 답글을 시간순으로 정렬합니다.
    #
    # all_messages는 중복 제거에 편한 dict였지만,
    # JSON에 저장할 때는 시간순 리스트가 읽기 좋으므로 다시 정렬합니다.
    # ========================================================

    # all_messages.values()로 메시지 dict들만 가져온 뒤
    # Slack의 ts를 Decimal로 변환하여 오래된 메시지 → 최신 메시지 순으로 정렬합니다.
    sorted_messages = sorted(
        all_messages.values(),
        key=lambda message: Decimal(message["ts"]),
    )

    # JSON 파일에 최종 저장할 전체 구조입니다.
    #
    # channel_id:
    # 어떤 Slack 채널 데이터인지 확인
    #
    # last_sync_started_ts:
    # 이번 동기화가 시작된 시각
    #
    # thread_versions:
    # 다음 실행에서 스레드 변경 여부 판단
    #
    # messages:
    # 실제 원글 + 답글 전체 데이터
    save_data = {
        "channel_id": CHANNEL_ID,
        "last_sync_started_ts": SYNC_STARTED,
        "thread_versions": thread_versions,
        "messages": sorted_messages,
    }


    # ========================================================
    # ⑨ 모든 조회가 성공한 뒤 JSON 파일을 저장합니다.
    #
    # API 조회 중간에 바로 저장하지 않고
    # 모든 조회가 성공한 다음 한 번에 저장하여
    # 부분적으로 수집된 데이터가 정상 파일을 덮어쓰는 것을 방지합니다.
    # ========================================================

    # 저장할 상위 디렉터리가 없다면 생성합니다.
    #
    # parents=True:
    # data/private처럼 중간 폴더도 함께 생성
    #
    # exist_ok=True:
    # 이미 폴더가 존재해도 오류를 발생시키지 않음
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 최종 JSON 파일에 바로 쓰지 않고 .tmp 임시 파일을 먼저 만듭니다.
    #
    # 예:
    # slack_C0BBNNCS4BG.json
    #        ↓
    # slack_C0BBNNCS4BG.tmp
    temp_path = DATA_PATH.with_suffix(".tmp")

    # Python dict를 JSON 문자열로 변환하여 임시 파일에 저장합니다.
    #
    # ensure_ascii=False:
    # 한글을 \uXXXX가 아니라 실제 한글로 저장
    #
    # indent=2:
    # 사람이 읽기 좋게 들여쓰기
    temp_path.write_text(
        json.dumps(save_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 임시 파일이 정상적으로 저장된 뒤
    # 실제 데이터 파일로 교체합니다.
    #
    # 중간에 프로그램이 종료되더라도
    # 기존 JSON 파일이 깨질 위험을 줄이는 방식입니다.
    temp_path.replace(DATA_PATH)

    # 현재 전체 메시지 ID 중
    # 동기화 시작 전에 존재하지 않았던 ID만 구해 새 메시지 수를 계산합니다.
    #
    # set 차집합:
    # 현재 메시지 - 기존 메시지
    new_count = len(set(all_messages) - existing_ids)

    # 동기화 결과를 화면에 출력합니다.
    print(f"\n새로 추가된 메시지: {new_count}개")
    print(f"이번에 조회한 스레드: {fetched_threads}개")
    print(f"전체 저장 메시지: {len(all_messages)}개")
    print(f"저장 위치: {DATA_PATH}")

    # Slack에서 일부 과거 데이터 접근이 제한되었다면 경고를 보여줍니다.
    if limited:
        print("※ 이번 조회에서 일부 이전 기록의 접근이 제한되었습니다.")

# Slack API 처리 오류, 파일 입출력 오류, JSON 변환 오류 등을
# 프로그램 마지막에서 한 번에 처리합니다.
except (RuntimeError, OSError, ValueError) as error:

    # traceback 전체를 보여주는 대신
    # 사용자가 이해하기 쉬운 동기화 실패 메시지로 프로그램을 종료합니다.
    #
    # from None은 예외가 연달아 출력되는 traceback 정보를 숨겨
    # 최종 오류 메시지를 깔끔하게 보여주기 위해 사용합니다.
    raise SystemExit(f"\n동기화 실패: {error}") from None
