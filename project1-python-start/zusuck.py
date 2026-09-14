
# JSON 데이터를 읽고 저장하기 위해 사용합니다.
# 이 코드에서는 Slack API 응답과 사용자 이름 캐시 파일을 처리할 때 사용합니다.
import json

# 운영체제 환경변수를 읽기 위해 사용합니다.
# Slack 인증 토큰을 코드에 직접 적지 않고 환경변수에서 가져오기 위해 필요합니다.
import os

# 파일 경로를 문자열보다 안전하고 편리하게 다루기 위해 사용합니다.
from pathlib import Path

# 명령줄에서 전달한 작성자 ID를 읽기 위해 사용합니다.
import sys

# 현재 시각을 초 단위로 구해 캐시가 오래되었는지 확인할 때 사용합니다.
import time

# HTTP 요청 과정에서 발생할 수 있는 오류 종류를 가져옵니다.
from urllib.error import HTTPError, URLError

# {"user": user_id} 같은 Python 데이터를 URL의 쿼리 문자열로 변환합니다.
# 예: {"user": "U123"} -> "user=U123"
from urllib.parse import urlencode

# Slack API에 HTTP 요청을 만들고 실제로 전송하기 위해 사용합니다.
from urllib.request import Request, urlopen


# 이 Python 파일이 있는 폴더를 기준으로 사용자 이름 캐시 파일의 위치를 만듭니다.
# __file__ : 현재 실행 중인 Python 파일 경로
# resolve() : 절대 경로로 변환
# parent : 현재 Python 파일이 들어 있는 폴더
CACHE_PATH = Path(__file__).resolve().parent / "data/private/slack_user_names.json"

# 캐시를 유효하다고 판단할 시간입니다.
# 24시간 × 60분 × 60초 = 86,400초
# 즉, 한 번 조회한 이름은 최대 24시간 동안 Slack API를 다시 호출하지 않고 사용합니다.
CACHE_TTL_SECONDS = 24 * 60 * 60


# 사용자 이름을 조회하는 과정에서 발생한 오류를 구분하기 위한 사용자 정의 예외입니다.
# RuntimeError를 상속하므로 일반 실행 오류처럼 raise/except로 처리할 수 있습니다.
class UserNameLookupError(RuntimeError):
    """토큰 등 민감한 내용을 포함하지 않는 조회 실패 정보입니다."""


# ------------------------------------------------------------
# Slack 사용자 ID 한 개를 받아 실제 사용자 이름을 조회하는 함수
#
# 입력:
#   user_id : Slack 사용자 ID 문자열
#   token   : Slack API 인증 토큰
#
# 출력:
#   이름을 찾으면 문자열(str)
#   사용자를 찾지 못하거나 이름이 비어 있으면 None
# ------------------------------------------------------------
def fetch_user_name(user_id, token):
    """한 작성자의 표시 이름을 조회합니다. 이름이 비어 있으면 실명을 사용합니다."""

    # Slack의 users.info API 주소에 조회하려는 사용자 ID를 붙입니다.
    # urlencode를 사용하면 특수문자가 포함되어도 URL 형식에 맞게 안전하게 변환됩니다.
    url = "https://slack.com/api/users.info?" + urlencode({"user": user_id})

    # HTTP 요청 객체를 만듭니다.
    # Authorization 헤더에 Bearer 토큰을 넣어 Slack에게 인증된 요청임을 알려줍니다.
    # 토큰을 URL이 아니라 Header에 넣는 이유는 민감한 인증 정보를 노출하지 않기 위해서입니다.
    request = Request(url, headers={"Authorization": f"Bearer {token}"})

    try:
        # Slack API에 실제 HTTP 요청을 보냅니다.
        # timeout=15는 최대 15초까지만 응답을 기다린다는 뜻입니다.
        #
        # with를 사용하면 요청 처리가 끝난 뒤 response가 자동으로 닫힙니다.
        with urlopen(request, timeout=15) as response:

            # Slack이 반환한 JSON 응답을 Python 객체로 변환합니다.
            # 일반적인 정상 응답은 dict 형태입니다.
            data = json.load(response)

    # 서버가 4xx, 5xx 같은 HTTP 오류 상태 코드를 반환한 경우입니다.
    except HTTPError as error:
        # HTTP 상태 코드를 가져옵니다.
        status = error.code

        # Slack이 호출 제한을 걸었을 경우 Retry-After 헤더에
        # 몇 초 뒤 다시 요청해야 하는지가 들어 있을 수 있습니다.
        retry_after = error.headers.get("Retry-After", "잠시")

        # 오류 응답 객체를 명시적으로 닫습니다.
        error.close()

        # HTTP 429는 너무 많은 API 요청을 보냈다는 뜻입니다.
        if status == 429:
            raise UserNameLookupError(
                f"호출 제한으로 이름 조회를 중단합니다. 재시도 대기: {retry_after}초"
            ) from None

        # 429 이외의 HTTP 오류는 상태 코드만 포함해 사용자 정의 예외로 바꿉니다.
        # 이렇게 하면 상위 함수에서 HTTPError를 직접 알 필요 없이
        # UserNameLookupError 하나로 조회 오류를 처리할 수 있습니다.
        raise UserNameLookupError(f"Slack HTTP 오류: {status}") from None

    # URLError는 DNS, 연결 실패 등 URL 요청 과정의 오류를 의미하고,
    # OSError는 운영체제 수준의 네트워크 오류 등이 발생했을 때 나올 수 있습니다.
    except (URLError, OSError):
        raise UserNameLookupError("Slack 사용자 정보에 연결할 수 없습니다.") from None

    # JSON 형식이 잘못되어 Slack의 응답을 Python 객체로 변환하지 못한 경우입니다.
    except ValueError:
        raise UserNameLookupError("Slack 사용자 정보 응답을 읽을 수 없습니다.") from None

    # 정상적인 Slack API 응답이라면 최상위 데이터는 dict(JSON Object)여야 합니다.
    # 예상하지 못한 형식이 들어오면 이후 data.get(...) 호출이 잘못될 수 있으므로 미리 검사합니다.
    if not isinstance(data, dict):
        raise UserNameLookupError("Slack 사용자 정보의 응답 형식이 잘못됐습니다.")

    # Slack API는 HTTP 요청 자체는 성공해도
    # {"ok": false, "error": "..."} 형태로 API 수준의 실패를 반환할 수 있습니다.
    if not data.get("ok"):

        # Slack이 알려준 오류 코드를 가져옵니다.
        # error 필드가 없다면 unknown_error를 기본값으로 사용합니다.
        code = data.get("error", "unknown_error")

        # missing_scope는 현재 Slack 토큰에 필요한 OAuth 권한이 없다는 의미입니다.
        if code == "missing_scope":
            raise UserNameLookupError(
                "users:read 권한이 없습니다. 현재 토큰 종류의 OAuth 범위에 "
                "users:read를 추가한 뒤 앱을 재설치/재승인해 주세요."
            )

        # 요청한 사용자 ID 자체가 존재하지 않으면 예외 대신 None을 반환합니다.
        # 호출한 쪽에서 "이름 없음" 상황으로 처리할 수 있게 합니다.
        if code == "user_not_found":
            return None

        # 그 외 Slack API 오류는 오류 코드를 포함해 전달합니다.
        raise UserNameLookupError(f"Slack 사용자 조회 실패: {code}")

    # 정상 응답의 user 필드에는 조회한 Slack 사용자 정보가 dict 형태로 들어 있습니다.
    user = data.get("user")

    # 응답의 user가 dict인지 확인하고,
    # 요청한 user_id와 Slack이 반환한 ID가 같은지도 검증합니다.
    #
    # API 응답을 그대로 믿지 않고 한 번 더 확인하는 방어적 검증입니다.
    if not isinstance(user, dict) or user.get("id") != user_id:
        raise UserNameLookupError("요청한 작성자와 Slack 응답의 ID가 다릅니다.")

    # Slack 사용자 정보 중 profile에는 표시 이름, 실명 등이 들어 있습니다.
    # profile이 없거나 None이면 빈 dict {}를 대신 사용합니다.
    profile = user.get("profile") or {}

    # 혹시 profile이 예상한 dict 형식이 아니라면
    # 이후 .get() 호출에서 오류가 나지 않도록 빈 dict로 바꿉니다.
    if not isinstance(profile, dict):
        profile = {}

    # 표시 이름에는 '이름(담임매니저/LLM/1기)' 같은 정보가 포함될 수 있습니다.

    # 여러 이름 후보를 우선순위대로 확인합니다.
    #
    # 우선순위:
    # 1. profile.display_name : 사용자가 설정한 표시 이름
    # 2. profile.real_name    : 프로필의 실명
    # 3. user.real_name       : 사용자 객체의 실명
    # 4. user.name            : Slack 계정 이름
    #
    # 앞쪽에서 유효한 이름을 찾으면 즉시 return하므로
    # 가능한 한 표시 이름을 우선해서 사용하게 됩니다.
    for name in (
        profile.get("display_name"),
        profile.get("real_name"),
        user.get("real_name"),
        user.get("name"),
    ):

        # 문자열이면서 공백을 제거한 뒤에도 내용이 있는 이름만 사용합니다.
        if isinstance(name, str) and name.strip():

            # strip()으로 이름 앞뒤의 불필요한 공백을 제거해 반환합니다.
            return name.strip()

    # 모든 이름 후보가 비어 있으면 이름을 찾지 못한 것으로 처리합니다.
    return None


# ------------------------------------------------------------
# 여러 Slack 사용자 ID의 이름을 한꺼번에 준비하는 함수
#
# 처리 흐름:
# 1. 입력 사용자 ID 정리
# 2. 기존 캐시 파일 읽기
# 3. 캐시가 아직 유효한 사용자는 그대로 사용
# 4. 오래됐거나 없는 사용자만 Slack API에서 조회
# 5. 새로 조회한 이름을 다시 캐시에 저장
#
# 최종 출력:
# {
#     "Slack 사용자 ID": "사용자 이름",
#     ...
# }
# ------------------------------------------------------------
def get_user_names(user_ids, cache_path=None):
    """중복 없이 필요한 작성자만 조회하여 {작성자 ID: 이름}을 반환합니다.

    조회 실패 시 마지막으로 저장한 이름 또는 ID를 반환합니다.
    토큰은 환경변수에서만 읽고 파일에는 이름과 조회 시각만 저장합니다.
    """

    # 전달받은 사용자 ID들을 정리합니다.
    #
    # 처리 내용:
    # - 문자열(str)인 값만 사용
    # - 앞뒤 공백 제거
    # - 빈 문자열 제거
    # - set(...)으로 중복 제거
    # - sorted(...)로 일정한 순서로 정렬
    #
    # 예:
    # [" U123 ", "U456", "U123", ""] -> ["U123", "U456"]
    user_ids = sorted({
        user_id.strip()
        for user_id in user_ids
        if isinstance(user_id, str) and user_id.strip()
    })

    # 조회할 사용자 ID가 하나도 없다면 API나 파일 작업을 하지 않고 즉시 빈 dict를 반환합니다.
    if not user_ids:
        return {}

    # cache_path를 함수 호출 시 직접 전달했다면 그 경로를 사용하고,
    # 전달하지 않았다면 위에서 정의한 기본 CACHE_PATH를 사용합니다.
    #
    # Path(...)로 변환해 이후 파일 존재 여부 확인, 읽기, 쓰기를 쉽게 처리합니다.
    cache_path = Path(cache_path) if cache_path is not None else CACHE_PATH

    # 캐시 데이터를 담을 빈 dict를 먼저 만듭니다.
    cache = {}

    # 기존 캐시 파일이 있을 때만 읽기를 시도합니다.
    if cache_path.exists():
        try:
            # 캐시 파일의 전체 문자열을 UTF-8로 읽은 뒤
            # json.loads(...)로 Python dict로 변환합니다.
            cache = json.loads(cache_path.read_text(encoding="utf-8"))

            # 캐시 최상위 구조는 사용자 ID를 key로 가지는 dict여야 합니다.
            if not isinstance(cache, dict):
                raise ValueError("캐시가 객체 형식이 아닙니다.")

        # 파일 읽기에 실패하거나 JSON 형식이 잘못된 경우에는
        # 기존 캐시를 포기하고 Slack API에서 필요한 이름을 다시 조회합니다.
        except (OSError, ValueError):
            print("작성자 이름 캐시를 읽지 못해 필요한 이름을 다시 조회합니다.")
            cache = {}

    # 모든 사용자 이름의 기본값을 사용자 ID 자체로 설정합니다.
    #
    # 예:
    # user_ids = ["U1", "U2"]
    #
    # names =
    # {
    #     "U1": "U1",
    #     "U2": "U2"
    # }
    #
    # 이후 이름을 성공적으로 찾은 사용자만 실제 이름으로 덮어씁니다.
    # 이렇게 하면 API 조회가 실패해도 최소한 사용자 ID는 표시할 수 있습니다.
    names = {user_id: user_id for user_id in user_ids}

    # Slack API에서 새로 조회해야 하는 사용자 ID를 저장할 리스트입니다.
    pending = []

    # 현재 Unix timestamp(1970년 1월 1일부터 흐른 초)를 구합니다.
    # 캐시 저장 시각과 비교하여 24시간이 지났는지 판단합니다.
    now = time.time()

    # 요청받은 사용자 ID를 하나씩 확인합니다.
    for user_id in user_ids:

        # 캐시에 해당 사용자 정보가 있는지 확인합니다.
        entry = cache.get(user_id)

        # 정상적인 캐시 항목은 dict 형태입니다.
        #
        # 예:
        # {
        #     "name": "홍길동",
        #     "fetched_at": 1720000000.0
        # }
        if isinstance(entry, dict):

            # 이전에 저장해 둔 사용자 이름입니다.
            name = entry.get("name")

            # Slack에서 이 이름을 마지막으로 조회한 시각입니다.
            fetched_at = entry.get("fetched_at")

            # 저장된 이름이 정상적인 문자열이고
            # 사용자 ID 자체가 아니라 실제 이름이라면 우선 사용합니다.
            if isinstance(name, str) and name.strip() and name != user_id:
                names[user_id] = name.strip()

                # fetched_at이 숫자이고,
                # 현재 시각과 조회 시각의 차이가 CACHE_TTL_SECONDS보다 작으면
                # 아직 캐시가 유효하다고 판단합니다.
                #
                # 즉:
                # now - fetched_at < 24시간
                #
                # 이 경우 Slack API를 다시 호출할 필요가 없습니다.
                if (
                    isinstance(fetched_at, (int, float))
                    and 0 <= now - fetched_at < CACHE_TTL_SECONDS
                ):
                    continue

        # 캐시가 없거나 24시간보다 오래됐다면
        # Slack API에서 새로 조회해야 하므로 pending에 추가합니다.
        pending.append(user_id)

    # 전체 사용자 중 몇 명은 캐시를 사용했고,
    # 몇 명은 Slack API 조회가 필요한지 보여줍니다.
    #
    # flush=True는 출력 버퍼에 쌓아두지 않고 즉시 화면에 표시하도록 합니다.
    print(
        f"작성자 {len(user_ids)}명: 이름 캐시 사용 {len(user_ids) - len(pending)}명, "
        f"조회 필요 {len(pending)}명",
        flush=True,
    )

    # 모든 사용자의 캐시가 아직 유효하다면
    # Slack API를 호출하지 않고 현재 names를 바로 반환합니다.
    if not pending:
        return names

    # Slack API 인증 토큰을 환경변수 SLACK_TOKEN에서 읽습니다.
    #
    # 코드나 JSON 파일 안에 토큰을 직접 저장하지 않는 이유는
    # Git 등에 민감한 인증 정보가 노출되는 것을 방지하기 위해서입니다.
    #
    # 환경변수가 없으면 ""를 기본값으로 사용하고,
    # strip()으로 앞뒤 공백을 제거합니다.
    token = os.getenv("SLACK_TOKEN", "").strip()

    # 토큰이 없다면 API 호출은 할 수 없습니다.
    # 하지만 앞에서 기존 캐시 이름 또는 사용자 ID를 names에 넣어 두었으므로
    # 프로그램 전체를 실패시키지 않고 그 값을 사용합니다.
    if not token:
        print("SLACK_TOKEN이 없어 저장된 작성자 이름 또는 ID를 사용합니다.")
        return names

    # 이번 실행에서 새롭게 조회하여 캐시에 저장한 사용자 수를 셉니다.
    updated = 0

    # 캐시가 없거나 오래된 사용자만 Slack API에서 조회합니다.
    for user_id in pending:
        try:
            # 위에서 만든 fetch_user_name() 함수에
            # 사용자 ID와 Slack 토큰을 전달합니다.
            #
            # 반환값:
            # 이름을 찾은 경우 -> str
            # 찾지 못한 경우   -> None
            name = fetch_user_name(user_id, token)

        # fetch_user_name() 안에서 인증, 권한, 네트워크 등의 문제가 발생하면
        # UserNameLookupError로 변환되어 이곳으로 전달됩니다.
        except UserNameLookupError as error:
            # 권한·인증·네트워크 오류가 나면 같은 실패를 반복 호출하지 않습니다.

            # 현재 실패 원인을 사용자에게 보여줍니다.
            print(f"작성자 이름 조회: {error}")

            # 같은 인증 토큰과 네트워크 환경에서
            # 다음 사용자도 같은 오류가 발생할 가능성이 높으므로
            # 남은 사용자에 대한 API 요청을 중단합니다.
            print("나머지 작성자는 저장된 이름 또는 ID로 표시합니다.")
            break

        # 이름을 정상적으로 찾았다면
        # 최종 결과와 캐시를 모두 갱신합니다.
        if name:
            # 기존 ID 기본값을 실제 사용자 이름으로 교체합니다.
            names[user_id] = name

            # 이름과 현재 조회 시각을 캐시에 저장합니다.
            # 다음 실행에서는 최대 24시간 동안 이 값을 재사용할 수 있습니다.
            cache[user_id] = {"name": name, "fetched_at": time.time()}

            # 이번 실행에서 새롭게 조회한 사용자 수를 1 증가시킵니다.
            updated += 1

        else:
            # 실패 결과를 이름으로 저장하지 않아 다음 실행에서 다시 확인합니다.

            # None이 반환된 경우에는 캐시에 실패 결과를 저장하지 않습니다.
            # 그래야 다음 프로그램 실행 때 다시 Slack API 조회를 시도할 수 있습니다.
            print(f"작성자 {user_id}: 이름을 확인하지 못했습니다.")

    # 한 명 이상 새롭게 조회했다면 변경된 캐시를 파일에 저장합니다.
    if updated:
        try:
            # 캐시 파일을 저장할 상위 폴더가 없다면 생성합니다.
            #
            # parents=True:
            # 중간 폴더까지 모두 생성
            #
            # exist_ok=True:
            # 이미 폴더가 있어도 오류를 발생시키지 않음
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            # 최종 캐시 파일에 바로 덮어쓰지 않고
            # 먼저 임시 파일(.tmp)에 저장합니다.
            #
            # 이렇게 하면 저장 도중 프로그램이 종료되더라도
            # 기존 정상 캐시 파일이 손상될 가능성을 줄일 수 있습니다.
            temp_path = cache_path.with_suffix(".tmp")

            # Python dict인 cache를 JSON 문자열로 변환하여 저장합니다.
            #
            # ensure_ascii=False:
            # 한글을 \uXXXX 형태가 아니라 실제 한글로 저장
            #
            # indent=2:
            # 사람이 읽기 좋게 들여쓰기
            temp_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # 임시 파일 저장이 성공하면 실제 캐시 파일 경로로 교체합니다.
            # 즉, "완전히 저장된 파일"을 마지막에 한 번에 바꾸는 방식입니다.
            temp_path.replace(cache_path)

            print(f"작성자 이름 {updated}명을 조회해 캐시에 저장했습니다.")

        # 캐시 저장에 실패하더라도 API에서 조회한 이름 자체는 names에 들어 있으므로
        # 프로그램 전체를 실패시키지 않고 이번 실행에서는 그대로 사용합니다.
        except OSError:
            print("이름 캐시를 저장하지 못했지만 이번 답변에는 조회한 이름을 사용합니다.")

    # 최종적으로
    # {사용자 ID: 사용자 이름} 형태의 dict를 반환합니다.
    return names


# ------------------------------------------------------------
# 이 파일을 직접 실행했을 때만 아래 코드를 실행합니다.
#
# 예:
# python slack_api_UserName.py U123 U456
#
# 다른 Python 파일에서 import할 때는 실행되지 않습니다.
# ------------------------------------------------------------
if __name__ == "__main__":

    # sys.argv에는 명령줄에서 입력한 값들이 리스트로 들어 있습니다.
    #
    # 예:
    # python slack_api_UserName.py U123 U456
    #
    # sys.argv =
    # [
    #     "slack_api_UserName.py",
    #     "U123",
    #     "U456"
    # ]
    #
    # 따라서 사용자 ID를 하나 이상 받으려면 길이가 최소 2여야 합니다.
    if len(sys.argv) < 2:
        raise SystemExit("사용법: python slack_api_UserName.py 작성자ID [작성자ID ...]")

    # sys.argv[1:]로 Python 파일 이름을 제외한 사용자 ID들만 가져옵니다.
    #
    # get_user_names(...) 결과:
    # {
    #     "U123": "홍길동",
    #     "U456": "김철수"
    # }
    #
    # 이를 JSON 문자열로 변환해 화면에 출력합니다.
    #
    # ensure_ascii=False:
    # 한글을 그대로 출력
    #
    # indent=2:
    # 보기 좋게 들여쓰기
    print(json.dumps(get_user_names(sys.argv[1:]), ensure_ascii=False, indent=2))

