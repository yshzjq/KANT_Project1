"""상시 Slack 이벤트 감지 → 변경된 메시지 수집 → main에 준비된 JSON 제공.

main은 ensure_slack_data부터, 봇은 run_bot부터 읽습니다.
상태 확인: python slack_bot.py --status / 종료: python slack_bot.py --stop
"""

# 두 실행 흐름을 나눠 읽습니다.
# main 쪽: ensure_slack_data → 실행 중인 봇 확인 → 저장 완료 대기 → JSON 반환.
# 봇 쪽: run_bot → listen_and_sync → 이벤트 보관 → sync_pending → 수집기 호출.
# 변경이 없으면 기록 API를 호출하지 않습니다. 최초 시작·재연결·파일 유실 시에는 전체 조회합니다.
# 관련 검증: tests/test_slack_bot.py, tests/test_windows_background.py.

import argparse
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from config import SLACK_READY_TIMEOUT
from slack_runtime import AlreadyRunning, BotState, FileLock, is_locked, is_ready


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "data/private/slack_runtime"
STATE_PATH = RUNTIME_DIR / "state.sqlite3"
LOCK_PATH = RUNTIME_DIR / "bot.lock"
LOG_PATH = RUNTIME_DIR / "bot.log"

# Win32_Process.Create는 터미널의 Job Object를 상속하지 않습니다.
# 실행할 경로와 환경변수는 stdin의 JSON으로 전달하며 명령행에 토큰을 넣지 않습니다.
WINDOWS_LAUNCH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $startup = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly -Property @{
        ShowWindow = [uint16]0
        CreateFlags = [uint32]520
        EnvironmentVariables = [string[]]$payload.environment
    }
    $result = Invoke-CimMethod -Namespace root/cimv2 -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = [string]$payload.command_line
        CurrentDirectory = [string]$payload.cwd
        ProcessStartupInformation = $startup
    }
    if ($result.ReturnValue -ne 0) {
        [Console]::Error.WriteLine('Windows process creation failed: ' + $result.ReturnValue)
        exit 1
    }
    @{pid = [int]$result.ProcessId} | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine('Windows process creation failed (' + $_.Exception.GetType().Name + ')')
    exit 1
}
"""

# 기존 하이픈 파일명을 유지하면서 수집 함수를 재사용합니다.
_spec = importlib.util.spec_from_file_location(
    "slack_collector", BASE_DIR / "slack_api_LLM_questions-chat.py",
)
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def ensure_slack_data(timeout=SLACK_READY_TIMEOUT):
    """봇이 없으면 시작하고, 현재까지 수신한 변경이 저장되면 기록을 반환합니다."""
    state = BotState(STATE_PATH)
    if not is_locked(LOCK_PATH):
        state.fail("")  # 이전 프로세스의 실패를 새 시작의 실패로 오인하지 않습니다.
        start_background_bot()
        print("Slack 감지 봇을 백그라운드로 시작합니다.", flush=True)
    else:
        print("실행 중인 Slack 감지 봇을 사용합니다.", flush=True)

    # 살아 있는 봇도 파일이 지워졌다면 전체 기록을 다시 받아야 합니다.
    # 요청 번호는 SQLite에 남으므로 main과 봇이 서로 다른 Python 프로세스여도 완료를 확인할 수 있습니다.
    saved = collector.load_saved_data(collector.DATA_PATH)
    collector.index_messages(saved.get("messages", []))
    request_id = state.request(full=not saved.get("messages"))
    deadline = time.monotonic() + timeout
    next_notice = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = state.snapshot()
        if is_ready(snapshot, request_id) and is_locked(LOCK_PATH):
            data = collector.load_saved_data(collector.DATA_PATH)
            collector.index_messages(data.get("messages", []))
            print("Slack 변경 확인 완료. 저장된 대화기록을 사용합니다.", flush=True)
            return data
        if snapshot["error"]:
            raise RuntimeError(f"{snapshot['error']} (봇 로그: {LOG_PATH})")
        if time.monotonic() >= next_notice:
            if not is_locked(LOCK_PATH):
                raise RuntimeError(f"Slack 봇 프로세스가 종료됐습니다. 로그를 확인하세요: {LOG_PATH}")
            if not snapshot["connected"]:
                phase = "Slack 연결 대기 중"
            elif snapshot["recovery"] != snapshot["recovered"]:
                phase = "봇 시작·재연결 후 전체 기록 확인 중"
            else:
                phase = "감지된 변경 기록 저장 중"
            print(f"{phase}입니다. 로그: {LOG_PATH}", flush=True)
            next_notice = time.monotonic() + 15
        time.sleep(0.5)
    raise RuntimeError(
        f"{timeout}초 안에 기록 준비가 끝나지 않았습니다. "
        "slack_bot.py --status로 상태를 확인하세요."
    )


def validate_environment():
    # APP_TOKEN은 Socket Mode 연결, BOT_TOKEN은 SDK 클라이언트,
    # SLACK_TOKEN은 수집기·이름 조회의 인증에 쓰입니다. 실제 값은 환경변수에서만 읽습니다.
    if importlib.util.find_spec("slack_sdk") is None:
        raise RuntimeError("Slack 의존성이 없습니다. uv sync를 실행하세요.")
    for name, prefix in (("SLACK_BOT_TOKEN", "xoxb-"), ("SLACK_APP_TOKEN", "xapp-")):
        if not os.getenv(name, "").strip().startswith(prefix):
            raise RuntimeError(f"{name} 환경변수에 {prefix} 토큰이 필요합니다.")
    if not os.getenv("SLACK_TOKEN", "").strip():
        raise RuntimeError("기록 수집용 SLACK_TOKEN 환경변수가 필요합니다.")


def start_background_bot():
    # PID는 시작 결과이고, 이후 실행 여부는 bot.lock으로 확인합니다. PID 번호는 재사용될 수 있습니다.
    validate_environment()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--background"]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":
        return launch_windows_process(command, BASE_DIR, environment)
    process = subprocess.Popen(
        command, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, env=environment, start_new_session=True,
    )
    return process.pid


def launch_windows_process(command, cwd, environment):
    """Windows 관리 서비스에서 생성해 터미널·uv의 자식 정리 대상에서 분리합니다."""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    payload = {
        "command_line": subprocess.list2cmdline(command),
        "cwd": str(cwd),
        "environment": [f"{name}={value}" for name, value in environment.items()],
    }
    try:
        # WMI가 새 프로세스를 만들어 부모 터미널과 수명을 분리합니다.
        # 위 PowerShell의 CreateFlags=520은 DETACHED_PROCESS(8)+CREATE_NEW_PROCESS_GROUP(512)입니다.
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", WINDOWS_LAUNCH_SCRIPT],
            input=json.dumps(payload, ensure_ascii=True), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Windows 백그라운드 실행 요청 실패 ({type(error).__name__}).") from None
    if result.returncode:
        # PowerShell 자체 오류에도 입력받은 환경변수가 포함되지 않도록 원문은 출력하지 않습니다.
        raise RuntimeError("Windows 백그라운드 프로세스를 시작하지 못했습니다. WMI 실행 권한을 확인하세요.")
    try:
        pid = json.loads(result.stdout)["pid"]
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("유효하지 않은 PID")
        return pid
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Windows 백그라운드 실행 결과를 확인할 수 없습니다.") from None


def run_background_bot():
    """분리된 프로세스가 직접 로그를 엽니다. 부모 터미널의 출력 핸들을 사용하지 않습니다."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 백그라운드 봇 시작 / PID {os.getpid()}", flush=True)
            run_bot()


def record_event(state, payload):
    """대상 채널의 메시지 이벤트만 보관합니다. 재전송은 event_id로 제거합니다."""
    if payload.get("type") == "app_rate_limited":
        state.request(full=True)
        return
    event = payload.get("event") or {}
    if event.get("type") != "message" or event.get("channel") != collector.CHANNEL_ID:
        return
    event_id = payload.get("event_id")
    if not event_id:
        # 정상 Events API에는 ID가 있습니다. 불완전한 통지는 전체 조회로 복구합니다.
        state.request(full=True)
        return
    state.enqueue(event_id, event)


def sync_pending(state):
    """스냅샷에 있던 이벤트까지만 완료 처리하여, 수집 중 받은 이벤트를 보존합니다."""
    snapshot = state.snapshot()
    if not snapshot["connected"] or snapshot["stop"]:
        return False
    full_sync = snapshot["recovery"] != snapshot["recovered"] or not collector.DATA_PATH.exists()
    # 이벤트도 복구 필요도 없으면 수집기를 부르지 않습니다. main의 확인 요청만 완료할 수 있습니다.
    if full_sync or snapshot["events"]:
        collector.sync_slack_data(events=snapshot["events"], full_sync=full_sync)
    if full_sync or snapshot["events"] or snapshot["requested"] != snapshot["completed"]:
        # 수집·저장이 예외 없이 끝났을 때만 번호를 전진시켜 실패한 변경이 유실되지 않게 합니다.
        state.finish(snapshot)
    return True


def safe_error(error):
    """SDK 예외의 요청 객체·헤더가 로그에 노출되지 않도록 오류 종류만 기록합니다."""
    if isinstance(error, (RuntimeError, ValueError)):
        return str(error)
    return f"Slack 처리 실패 ({type(error).__name__}). 연결·토큰·파일 권한을 확인하세요."


def run_bot():
    state = BotState(STATE_PATH)
    try:
        with FileLock(LOCK_PATH):
            state.begin()
            try:
                validate_environment()
                listen_and_sync(state)
            except Exception as error:
                message = safe_error(error)
                state.fail(message)
                print(message, flush=True)
            finally:
                state.disconnected()
                state.execute("UPDATE state SET heartbeat=0 WHERE id=1")
    except AlreadyRunning:
        print("이미 실행 중인 Slack 봇이 있습니다.", flush=True)


def listen_and_sync(state):
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    # SDK 통신 로그 대신 이 모듈의 상태·오류 로그만 남깁니다.
    logger = logging.getLogger("slack_bot.transport")
    logger.disabled = True
    client = SocketModeClient(
        app_token=os.environ["SLACK_APP_TOKEN"].strip(),
        web_client=WebClient(token=os.environ["SLACK_BOT_TOKEN"].strip(), logger=logger),
        logger=logger, concurrency=1,
    )
    received_hello = threading.Event()

    def on_raw_message(raw):
        kind = json.loads(raw).get("type")
        if kind == "hello":
            received_hello.set()
            state.connected()
        elif kind == "disconnect":
            state.disconnected()

    def on_request(socket_client, request):
        if request.type == "events_api":
            # ACK는 Slack에 보내는 수신 확인입니다. 이벤트를 먼저 디스크에 보관한 뒤 응답합니다.
            # 느린 수집 API는 아래 반복문에서 처리해 수신 확인을 지연시키지 않습니다.
            record_event(state, request.payload)
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    client.on_message_listeners.append(on_raw_message)
    client.on_close_listeners.append(lambda *_: state.disconnected())
    client.on_error_listeners.append(lambda *_: state.disconnected())
    client.socket_mode_request_listeners.append(on_request)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(1):
            state.heartbeat()
            if not client.is_connected():
                state.disconnected()
            elif received_hello.is_set() and not state.snapshot()["connected"]:
                # SDK가 재연결한 직후 이전 연결의 close 콜백이 늦게 올 수 있습니다.
                state.connected()

    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        client.connect()
        print(f"Slack 이벤트 감지 시작: {collector.CHANNEL_ID} / PID {os.getpid()}", flush=True)
        retry_at = 0
        while not state.snapshot()["stop"]:
            if time.monotonic() >= retry_at:
                try:
                    sync_pending(state)
                except Exception as error:
                    message = safe_error(error)
                    state.fail(message)
                    print(message, flush=True)
                    # 처리 완료로 표시하지 않아 실패한 이벤트는 다음 시도에 다시 조회합니다.
                    retry_at = time.monotonic() + 30
            stopped.wait(1)  # 짧은 간격으로 여러 이벤트를 묶고, 없으면 조회하지 않습니다.
    finally:
        stopped.set()
        monitor.join(timeout=5)
        client.close()
        print("Slack 봇을 종료했습니다.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--run", action="store_true", help="현재 터미널에서 봇 실행")
    actions.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--status", action="store_true", help="봇 실행·연결·대기 상태 확인")
    actions.add_argument("--stop", action="store_true", help="현재 수집 후 봇 종료 요청")
    args = parser.parse_args()
    if args.background:
        run_background_bot()
        return
    if args.run:
        run_bot()
        return
    if not STATE_PATH.exists() or not is_locked(LOCK_PATH):
        print("Slack 봇이 실행 중이 아닙니다.")
        return
    state = BotState(STATE_PATH)
    if args.stop:
        # 강제 종료 대신 종료 요청을 남겨 진행 중인 저장 작업을 마치게 합니다.
        state.execute("UPDATE state SET stop=1 WHERE id=1")
        print("봇 종료를 요청했습니다. 진행 중인 수집을 마치면 종료합니다.")
    else:
        snapshot = state.snapshot()
        print(f"PID: {snapshot['pid']} / Slack 연결: {bool(snapshot['connected'])}")
        print(f"대기 이벤트: {len(snapshot['events'])} / 기록 준비: {bool(is_ready(snapshot, snapshot['requested']))}")
        print(f"최근 오류: {snapshot['error'] or '없음'}\n로그: {LOG_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSlack 봇 실행을 종료했습니다.")
