# Slack 사용자 ID를 표시 이름으로 바꾸는 보조 모듈입니다.
# 읽는 순서: get_user_names → 캐시 확인 → fetch_user_name → save_name_updates.
# 이름 조회 실패 시 저장된 이름이나 ID로 계속 답변합니다. 관련 검증: tests/test_user_names.py.

import json
import os
from pathlib import Path
import sys
import time
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import DEBUG
from slack_runtime import AlreadyRunning, FileLock


CACHE_PATH = Path(__file__).resolve().parent / "data/private/slack_user_names.json"
CACHE_TTL_SECONDS = 24 * 60 * 60


class UserNameLookupError(RuntimeError):
    """토큰 등 민감한 내용을 포함하지 않는 조회 실패 정보입니다."""


def load_name_cache(cache_path):
    if not cache_path.exists():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            raise ValueError("캐시가 객체 형식이 아닙니다.")
        return cache
    except (OSError, ValueError):
        if DEBUG:
            print("작성자 이름 캐시를 읽지 못해 필요한 이름을 다시 조회합니다.")
        return {}


def save_name_updates(cache_path, updates):
    """저장 직전에 최신 캐시를 읽어, 다른 실행이 추가한 이름을 보존합니다."""
    # API 조회 중 다른 실행이 저장했을 수 있어, 잠금을 얻은 뒤 파일을 다시 읽고 이번 변경만 합칩니다.
    with FileLock(cache_path.with_suffix(".lock")):
        cache = load_name_cache(cache_path)
        cache.update(updates)
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache_path.parent,
                prefix=cache_path.stem + "_", suffix=".tmp", delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(cache, temp_file, ensure_ascii=False, indent=2)
            temp_path.replace(cache_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def fetch_user_name(user_id, token):
    """한 작성자의 표시 이름을 조회합니다. 이름이 비어 있으면 실명을 사용합니다."""
    url = "https://slack.com/api/users.info?" + urlencode({"user": user_id})
    request = Request(url, headers={"Authorization": f"Bearer {token}"})

    try:
        with urlopen(request, timeout=15) as response:
            data = json.load(response)
    except HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After", "잠시")
        error.close()
        if status == 429:
            raise UserNameLookupError(
                f"호출 제한으로 이름 조회를 중단합니다. 재시도 대기: {retry_after}초"
            ) from None
        raise UserNameLookupError(f"Slack HTTP 오류: {status}") from None
    except (URLError, OSError):
        raise UserNameLookupError("Slack 사용자 정보에 연결할 수 없습니다.") from None
    except ValueError:
        raise UserNameLookupError("Slack 사용자 정보 응답을 읽을 수 없습니다.") from None

    if not isinstance(data, dict):
        raise UserNameLookupError("Slack 사용자 정보의 응답 형식이 잘못됐습니다.")

    if not data.get("ok"):
        code = data.get("error", "unknown_error")
        if code == "missing_scope":
            raise UserNameLookupError(
                "users:read 권한이 없습니다. 현재 토큰 종류의 OAuth 범위에 "
                "users:read를 추가한 뒤 앱을 재설치/재승인해 주세요."
            )
        if code == "user_not_found":
            return None
        raise UserNameLookupError(f"Slack 사용자 조회 실패: {code}")

    user = data.get("user")
    if not isinstance(user, dict) or user.get("id") != user_id:
        raise UserNameLookupError("요청한 작성자와 Slack 응답의 ID가 다릅니다.")

    profile = user.get("profile") or {}
    if not isinstance(profile, dict):
        profile = {}

    # 표시 이름에 역할 정보가 붙어 있을 수 있어 display_name을 우선하고, 없을 때 실명·계정명을 씁니다.
    for name in (
        profile.get("display_name"),
        profile.get("real_name"),
        user.get("real_name"),
        user.get("name"),
    ):
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def get_user_names(user_ids, cache_path=None):
    """중복 없이 필요한 작성자만 조회하여 {작성자 ID: 이름}을 반환합니다.

    조회 실패 시 마지막으로 저장한 이름 또는 ID를 반환합니다.
    토큰은 환경변수에서만 읽고 파일에는 이름과 조회 시각만 저장합니다.
    """
    user_ids = sorted({
        user_id.strip()
        for user_id in user_ids
        if isinstance(user_id, str) and user_id.strip()
    })
    if not user_ids:
        return {}

    cache_path = Path(cache_path) if cache_path is not None else CACHE_PATH
    cache = load_name_cache(cache_path)

    # 반환값을 ID로 먼저 채워 두어, 조회가 중간에 실패해도 모든 요청 ID를 표시할 수 있게 합니다.
    names = {user_id: user_id for user_id in user_ids}
    pending = []
    now = time.time()

    for user_id in user_ids:
        entry = cache.get(user_id)
        if isinstance(entry, dict):
            name = entry.get("name")
            fetched_at = entry.get("fetched_at")
            if isinstance(name, str) and name.strip() and name != user_id:
                names[user_id] = name.strip()
                # 24시간 안의 이름이면 API를 생략합니다. 오래된 이름은 재조회 실패 시에도 대체값으로 남깁니다.
                if (
                    isinstance(fetched_at, (int, float))
                    and 0 <= now - fetched_at < CACHE_TTL_SECONDS
                ):
                    continue
        pending.append(user_id)

    if DEBUG:
        print(
            f"작성자 {len(user_ids)}명: 이름 캐시 사용 {len(user_ids) - len(pending)}명, "
            f"조회 필요 {len(pending)}명",
            flush=True,
        )
    if not pending:
        return names

    token = os.getenv("SLACK_TOKEN", "").strip()
    if not token:
        if DEBUG:
            print("SLACK_TOKEN이 없어 저장된 작성자 이름 또는 ID를 사용합니다.")
        return names

    updates = {}
    for user_id in pending:
        try:
            name = fetch_user_name(user_id, token)
        except UserNameLookupError as error:
            # 권한·인증·네트워크 오류가 나면 같은 실패를 반복 호출하지 않습니다.
            if DEBUG:
                print(f"작성자 이름 조회: {error}")
                print("나머지 작성자는 저장된 이름 또는 ID로 표시합니다.")
            break

        if name:
            names[user_id] = name
            updates[user_id] = {"name": name, "fetched_at": time.time()}
        else:
            # 실패 결과를 이름으로 저장하지 않아 다음 실행에서 다시 확인합니다.
            if DEBUG:
                print(f"작성자 {user_id}: 이름을 확인하지 못했습니다.")

    if updates:
        try:
            save_name_updates(cache_path, updates)
            if DEBUG:
                print(f"작성자 이름 {len(updates)}명을 조회해 캐시에 저장했습니다.")
        except (OSError, AlreadyRunning):
            if DEBUG:
                print("이름 캐시를 저장하지 못했지만 이번 답변에는 조회한 이름을 사용합니다.")

    return names


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("사용법: python slack_api_LLM_questions_UserName.py 작성자ID [작성자ID ...]")
    print(json.dumps(get_user_names(sys.argv[1:]), ensure_ascii=False, indent=2))
