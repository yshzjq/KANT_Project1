"""읽는 순서: main → answer_question → prepare_context → 나머지 보조 함수.

Slack 기록 확인 → 질문 입력 → 검색어 추출 → 관련 대화 선택 → 답변 출력.
"""

# 코드 검증은 main()의 분기부터 따라가면 됩니다.
# 설정: config.py / 답변 지침: prompts.py / 성능 계산·보고서: evaluation_metrics.py
# all_messages는 Slack 메시지 사전의 목록입니다. ts는 식별값 겸 작성 시각,
# thread_ts는 답글을 원글과 묶는 식별값입니다.
# 검증 명령(프로젝트 폴더): .venv\Scripts\python.exe -m unittest discover -s tests -q

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from ollama import Client
from slack_api_LLM_questions_UserName import get_user_names

from config import DEBUG, QUESTION_EVALUATION, QUESTION_EVALUATION_DIR, QUESTION_LIST
from prompts import KEYWORD_FORMAT, KEYWORD_PROMPT, SYSTEM_PROMPT
from slack_bot import ensure_slack_data
from search_terms import (GENERAL_TERMS, asks_question_location, compact_text, date_subject_terms,
                          expand_search_terms, message_search_text, routing_evidence, split_search_terms)
from slack_dates import explain_window, question_window, resolve_relative_dates
from evaluation_metrics import (EXPERIMENT_KINDS, MeasuredClient, field, render_model_metadata,
                                render_question, render_summary, running_model, tokenizer_metadata,
                                validate_questions)


MODEL = "qwen3:4b-instruct-2507-q8_0"  # 최종 답변 생성용

KEYWORD_MODEL = "qwen3:4b-instruct-2507-q4_K_M"  # 검색어 JSON 생성 전용; MODEL과 달라도 됩니다.


# 둘 다 토큰 단위입니다. NUM_CTX는 입력·출력의 전체 한도, NUM_PREDICT는 출력 한도입니다.
# 요청값과 실제 적재된 context_length는 다를 수 있어 평가 파일에는 둘을 따로 기록합니다.
NUM_CTX = 8192
NUM_PREDICT = 512
OLLAMA_HOST = "http://127.0.0.1:11434"
BASE_DIR = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))


class NoRelevantContext(ValueError):
    """검색된 근거가 없을 때 추측 대신 반환할 안내입니다."""


def debug_print(*args, **kwargs):
    """진행 로그만 제어합니다. 질문 입력·답변·오류 안내는 각 호출부에서 출력합니다."""
    if DEBUG:
        print(*args, **kwargs)


def main(*, backend=None, keyword_backend=None):
    """프로그램 전체 순서입니다. 이 함수부터 읽으면 됩니다."""
    # 기록 준비에 실패하면 답변으로 넘어가지 않습니다. 호출 경로: slack_bot.ensure_slack_data().

    # 최신 Slack 기록을 가져오는 코드
    saved_data = load_latest_slack_data()


    all_messages = saved_data.get("messages", [])
    if not all_messages:
        raise SystemExit("저장된 Slack 메시지가 없습니다.")


    # 평가 모드는 같은 메시지 목록으로 모든 질문을 비교합니다.
    # 직접 질문 모드는 입력을 기다리는 동안 바뀐 기록을 질문마다 다시 읽습니다.
    # Luna는 답변·검색어 연결을 따로 넘길 수 있습니다. 검색·날짜 판정·평가는 함께 사용합니다.
    connection = {"backend": backend} if backend is not None else {}
    if keyword_backend is not None:
        connection["keyword_backend"] = keyword_backend
    if QUESTION_EVALUATION:
        run_question_evaluation(all_messages, **connection)
        return

    run_interactive_chat(all_messages, **connection)


def run_interactive_chat(all_messages, *, backend=None, keyword_backend=None):
    """직접 입력 모드에서는 종료할 때까지 질문 → 답변을 반복합니다."""
    debug_print(f"\n대화기록 {len(all_messages)}개를 준비했습니다.")
    print("질문을 입력하면 답변합니다. 종료: 종료 / exit / quit / Ctrl+C")
    while True:
        try:
            question = input("\n질문: ").strip()
            if not question:
                continue
            if question.casefold() in {"종료", "exit", "quit"}:
                debug_print("질문을 종료했습니다.")
                return
            try:
                # 입력을 기다리는 동안 봇이 저장한 신규·수정·삭제도 다음 답변에 반영합니다.
                all_messages = load_latest_slack_data().get("messages", [])
                if not all_messages:
                    raise ValueError("저장된 Slack 메시지가 없습니다.")
                connection = {
                    "client": backend, "model": backend.model, "keyword_model": backend.model,
                } if backend is not None else {}
                if keyword_backend is not None:
                    connection.update(keyword_client=keyword_backend, keyword_model=keyword_backend.model)
                answer_question(question, all_messages, **connection)
            except (Exception, SystemExit) as error:
                print(f"답변 처리에 실패했습니다: {error}\n다른 질문을 입력하거나 다시 시도해 주세요.")
        except (KeyboardInterrupt, EOFError):
            debug_print("\n질문을 종료했습니다.")
            return


def answer_question(question, all_messages, client=None, *, trace=None, model=None, keyword_model=None,
                    keyword_client=None, show_answer=True):
    """검색어를 만든 뒤 관련 근거가 있을 때 최종 답변을 생성합니다."""
    # 평가 모드에서는 MeasuredClient를 주입해 같은 답변 경로의 호출 통계도 수집합니다.
    client = client if client is not None else Client(host=OLLAMA_HOST, timeout=180)
    # 별도 연결이 없으면 기존 Ollama/클라우드처럼 한 연결에 모델 태그만 달리 전달합니다.
    keyword_client = client if keyword_client is None else keyword_client
    model = MODEL if model is None else model
    keyword_model = KEYWORD_MODEL if keyword_model is None else keyword_model
    # 한 질문을 처리하는 도중 날짜가 바뀌어도 검색과 프롬프트의 기준 시각은 같습니다.
    reference_time = datetime.now(KST)
    # trace는 평가 파일에만 기록합니다. 기대 결과나 진단 정보를 모델에 추가하지 않습니다.
    if trace is not None:
        trace.update(reference_time=reference_time.isoformat(), stage="검색어 생성 중",
                     model=model, keyword_model=keyword_model)

    # 검색어 뽑기
    keywords = extract_search_keywords(
        keyword_client,
        question, 
        trace=trace, 
        model=keyword_model
    )
    # "keywords": ["시험", "날짜", "범위"]
    # 측정 대상!!!

    try:
        education_info = prepare_context(
            all_messages, 
            keywords, 
            question, 
            reference_time, 
            trace=trace
        )

    except NoRelevantContext as error:
        # 이 안내는 프로그램이 만든 문장입니다. 최종 모델 호출·답변 성공 수에 넣지 않습니다.
        answer = str(error)
        if trace is not None:
            trace.update(stage="근거 없음 안내 반환", no_context_reason=answer)
        if show_answer:
            print(answer)
        return answer

    debug_print(f"{model}의 답변을 기다리고 있습니다.", flush=True)

    # 진짜 답변 생성  
    # 불필요한 단어 목록들 필터링해서 진짜 답변 생성  
    messages = make_chat_messages(
        question, 
        education_info, 
        reference_time
    )

    if trace is not None:
        # 요청 직전에 저장해야 모델 호출 실패·중단 때도 실제 요청 내용을 확인할 수 있습니다.
        trace.update(stage="최종 답변 호출 중", final_messages=messages)
    # 일반적인 질문은 검색어 생성 1회 + 최종 답변 1회, 총 두 번 모델을 호출합니다.
    response = client.chat(
        model=model,
        messages=messages,
        stream=False,  # 답변을 전부 받은 뒤 한 번에 출력합니다.
        options={"temperature": 0, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
    )

    answer = clean_answer(response.message.content or "") 
    if trace is not None:
        trace["stage"] = "최종 답변 응답 수신"
    if show_answer:
        print(answer)
        if field(response, "done") is False or field(response, "done_reason") == "length":
            print(f"응답이 완료되지 않았습니다. 처리 상태: {field(response, 'done_reason') or '미확인'}")
    return answer


def clean_answer(answer):
    """터미널과 평가 파일에 같은 정리된 답변을 사용합니다."""
    # 화면용 표현만 정리합니다. 평가용 원문은 MeasuredClient가 이 함수 호출 전에 보관합니다.
    answer = re.sub(r"(?im)^\s*\[ollama 답변\]\s*\n?", "", answer)
    answer = re.sub(
        r"(?im)[ \t]*(?:[-*] )?(?:\*\*)?근거\s*ts\s*:(?:\*\*)?[ \t]*"
        r"`?\d+(?:\.\d+)?`?(?:[ \t]*[,、][ \t]*`?\d+(?:\.\d+)?`?)*[ \t]*(?:\*\*)?",
        "", answer,
    )
    for ending, replacement in (("로는", "으로는"), ("로", "으로"), ("는", "은"), ("가", "이"), ("를", "을")):
        answer = re.sub(r"제공된 자료(?:들)?" + ending, "확인한 Slack 기록" + replacement, answer)
    answer = re.sub(r"제공된 자료(?:들)?", "확인한 Slack 기록", answer)
    return re.sub(r"\n{3,}", "\n\n", answer).strip()


def run_question_evaluation(all_messages, *, experiment_kind="main", backend=None, keyword_backend=None):
    """동일한 기록으로 질문을 순서대로 평가하고, 매 항목을 즉시 파일에 기록합니다."""
    if experiment_kind not in EXPERIMENT_KINDS:
        raise ValueError("experiment_kind는 main / warmup / retry / extra 중 하나여야 합니다.")
    questions = validate_questions(QUESTION_LIST)
    model = backend.model if backend is not None else MODEL
    keyword_model = (keyword_backend.model if keyword_backend is not None else
                     backend.model if backend is not None else KEYWORD_MODEL)
    output_dir = BASE_DIR / QUESTION_EVALUATION_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    model_slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", model)
    output_path = output_dir / f"question_evaluation_{experiment_kind}_{model_slug}_{stamp}_{uuid4().hex[:8]}.md"
    debug_print(f"\n공통 질문 {len(questions)}개 평가를 시작합니다.\n저장 위치: {output_path}")

    client = backend if backend is not None else Client(host=OLLAMA_HOST, timeout=180)
    # 메모리 조회 실패가 답변 실패로 이어지지 않도록 별도 클라이언트와 짧은 대기 한도를 씁니다.
    observer = backend if backend is not None else Client(host=OLLAMA_HOST, timeout=5)
    keyword_observer = keyword_backend if keyword_backend is not None else observer
    # show는 생성 호출이 아닙니다. 모델마다 한 번 조회하고 질문 응답시간에서는 제외합니다.
    model_metadata = {keyword_model: tokenizer_metadata(keyword_observer, keyword_model)}
    if model not in model_metadata:
        model_metadata[model] = tokenizer_metadata(observer, model)
    server_description = backend.report_header if backend is not None else f"Ollama 서버: `{OLLAMA_HOST}`\n\n"
    measurement_description = backend.measurement_notes if backend is not None else (
        "모델 로딩 시간 = load_duration / 1,000,000,000초. "
        "토큰 생성 속도 = eval_count / (eval_duration / 1,000,000,000) tokens/s.\n\n"
        "VRAM = 질문 직후 client.ps()의 최종 답변 모델 size_vram / 1,048,576 MiB이며 최대 사용량이 아닙니다. "
        "실제 context_length는 ps 관측값이며 요청한 num_ctx나 모델의 최대 지원 길이로 대체하지 않습니다.\n\n"
        "시작 전 미적재/로드됨은 최종 답변 모델의 ps 관측 기준입니다. 첫 질문이라고 미적재로 가정하지 않습니다. "
        "두 단계가 같은 모델이면 검색어 생성에서 로드한 상태를 재사용할 수 있습니다. "
        "다른 모델이면 전환 중 추가 로딩이 발생할 수 있으며 각 호출의 load_duration으로 확인합니다.\n\n"
    )
    if keyword_backend is not None:
        server_description = (
            f"### 최종 답변 연결\n\n{server_description}"
            f"### 검색어 생성 연결\n\n{keyword_backend.report_header}"
        )
        measurement_description = (
            f"### 최종 답변 측정 안내\n\n{measurement_description}"
            f"### 검색어 생성 측정 안내\n\n{keyword_backend.measurement_notes}"
            "검색어 모델의 별도 적재 정보는 질문 시작 전·질문 직후 관측값입니다. "
            "VRAM은 최대 사용량이나 키워드 호출 직후 측정값이 아닙니다. "
            "이 조회도 응답시간(elapsed)에서 제외합니다.\n\n"
        )
    records = []
    snapshot_hash = hashlib.sha256(json.dumps(
        all_messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    # show·ps 조회·보고서 기록은 elapsed에 포함하지 않습니다. 모델 로딩은 포함됩니다.
    with output_path.open("x", encoding="utf-8") as report:
        report.write(
            "# LLM 교육 안내 공통 질문 평가 결과\n\n"
            f"{render_model_metadata(model, keyword_model, model_metadata)}\n"
            f"실험 구분: {EXPERIMENT_KINDS[experiment_kind]} / 실행 시각: {datetime.now(KST).isoformat()}\n\n"
            f"{server_description}"
            f"검색 대상: {len(all_messages)}개 메시지 / 메시지 목록 SHA-256: `{snapshot_hash}`\n\n"
            "검색 진단은 이 실행에서 수집한 기록입니다. 후보 점수는 정답 확률이 아니며, 날짜 통과는 현재 규칙의 판정입니다. "
            "선택된 원문과 실제 요청 메시지를 함께 보존합니다.\n\n"
            "응답시간(elapsed)은 검색어 생성·검색·진단 수집·이름 조회·최종 답변까지의 전체 처리 시간입니다. "
            "최초 Slack 동기화, show·ps 조회, 보고서 저장은 제외합니다. TTFT는 별도로 측정하지 않았습니다.\n\n"
            f"{measurement_description}"
            "자동 워밍업·재시도는 하지 않습니다. warmup/retry/extra 실행은 별도 파일로 기록합니다. "
            "tokens/s만으로 품질이나 체감 속도를 판단하지 않습니다. 품질 점수는 자동 부여하지 않으며 원본 응답을 보존합니다.\n\n"
            "각 호출의 출력 한도·생성 설정은 아래 요청 설정에 기록합니다. "
            "미지정 옵션(예: seed/top_p/keep_alive)은 모델·서버 기본값이며 실제 적용값은 미측정입니다.\n\n"
        )
        report.flush()
        try:
            for number, item in enumerate(questions, start=1):
                question = item["question"]
                # 유형·기대 결과는 보고서에만 씁니다. 모델에 보내면 기대 답안을 미리 알려 주게 됩니다.
                debug_print(f"\n[Q{number:02d}/{len(questions):02d}] {question}", flush=True)
                measured = MeasuredClient(client)
                # 두 연결의 호출을 한 질문 기록에 순서대로 모아 실패·미호출도 구분합니다.
                measured_keywords = (MeasuredClient(keyword_backend, calls=measured.calls)
                                     if keyword_backend is not None else None)
                trace = {"stage": "시작 전"}
                before = running_model(observer, model)
                keyword_before = (running_model(keyword_observer, keyword_model)
                                  if keyword_backend is not None else None)
                status, answer = "처리 실패", ""
                started = time.perf_counter()
                try:
                    connection = {"model": model, "keyword_model": keyword_model}
                    if measured_keywords is not None:
                        connection["keyword_client"] = measured_keywords

                    # 실제 답변 과정
                    answer = answer_question(
                        question, 
                        all_messages, 
                        client=measured, 
                        trace=trace, 
                        show_answer=DEBUG,  # 조용한 평가도 반환값·원본 응답은 그대로 파일에 저장합니다.
                        **connection
                    )
                    status = "답변 완료" if len(measured.calls) == 2 and measured.calls[-1]["success"] else "안내 반환 (최종 모델 미호출)"
                    if status == "답변 완료":
                        # API가 정상 반환됐어도 내용이 잘리거나 비어 있으면 완료된 답변은 아닙니다.
                        original = measured.calls[-1]["response"]
                        if original["done_reason"] == "length":
                            status = "불완전 응답 (출력 한도 도달)"
                        elif original["done_reason"] == "refusal":
                            status = "응답 거절"
                        elif original["done"] is False or not (original["content"] or "").strip() or not answer.strip():
                            status = "불완전 응답 (미완료 또는 빈 원문)"
                except (KeyboardInterrupt, EOFError):
                    status, answer = "사용자 중단", "평가를 중단했습니다."
                    raise
                except (Exception, SystemExit) as error:
                    # 실패한 질문을 다른 응답으로 대체하지 않고, 실패 기록을 남긴 뒤 다음 질문을 진행합니다.
                    answer = f"평가 실패: {error}"
                    debug_print(answer, flush=True)
                finally:
                    elapsed = time.perf_counter() - started
                    record = {
                        "number": number, "question": question, "answer": answer,
                        "type": item["type"], "expected_result": item["expected_result"],
                        "model": model, "keyword_model": keyword_model, "status": status, "elapsed": elapsed,
                        "before": before, "calls": measured.calls,
                        "retrieval": trace,
                        "after": {
                            "loaded": None, "observed_at": datetime.now(timezone.utc).isoformat(),
                            "reason": "사용자 중단으로 질문 직후 적재 정보를 조회하지 못함",
                        },
                    }
                    if keyword_backend is not None:
                        record["keyword_before"] = keyword_before
                        record["keyword_after"] = dict(record["after"])
                    records.append(record)
                    # 답변을 먼저 보관해 두고, ps 조회 중 Ctrl+C가 와도 반드시 파일에 남깁니다.
                    try:
                        if status != "사용자 중단":
                            record["after"] = running_model(observer, model)
                            if keyword_backend is not None:
                                record["keyword_after"] = running_model(keyword_observer, keyword_model)
                    finally:
                        report.write(render_question(record))
                        report.flush()
        finally:
            report.write(render_summary(records, len(questions), experiment_kind))
            report.flush()
    debug_print(f"\n평가 결과 저장 완료: {output_path}")
    return output_path


def prepare_context(all_messages, keywords, question, reference_time=None, *, trace=None):
    """검색 → 길이에 맞게 선택 → 작성자 이름 추가 순서로 참고 자료를 만듭니다."""
    if DEBUG:
        print(f"자동 검색어: {', '.join(keywords)}", flush=True)
        print(f"전체 검색 대상: {len(all_messages)}개 메시지")

    reference_time = reference_time or datetime.now(KST)

    # window는 검색할 날짜 범위
    window = question_window(question, reference_time)
    if trace is not None:
        trace.update(
            stage="검색 후보 수집 중",
            reference_time=reference_time.isoformat(), search_keywords=list(keywords),
            date_window=[day.isoformat() for day in window] if window else None,
            candidates=[], selected_messages=[], context=None,
        )

    matched_threads = find_matching_threads(
        all_messages, 
        keywords, 
        question
    )

    candidates = {}
    eligible = []
    date_keywords = date_subject_terms(keywords)
    for rank, thread in enumerate(matched_threads, 1):
        checks = []
        if window:
            for message in thread["messages"]:
                hits = [word for word in keywords if compact_text(word) in message_search_text(message)]
                checks.append({"ts": message["ts"], "matched_keywords": hits,
                               **explain_window(message, window, keywords=date_keywords)})
        # 날짜와 핵심 주제가 연결된 메시지만 현재 일정의 근거로 사용합니다.
        date_passed = not window or any(check["matches"] and check["matched_keywords"] for check in checks)
        if trace is not None:
            candidate = {
                "thread_id": thread["thread_id"], "rank": rank, "score": thread["score"],
                "matched_keywords": [word for word in keywords if any(
                    compact_text(word) in message_search_text(message) for message in thread["messages"]
                )],
                "date_checks": checks, "date_passed": bool(date_passed) if window else None,
                "decision": "날짜 통과" if window and date_passed else "날짜 제외" if window else "날짜 제한 없음",
                "reason": "날짜와 검색어가 같은 메시지에서 함께 일치하지 않음" if not date_passed else "",
            }
            candidates[thread["thread_id"]] = candidate
            trace["candidates"].append(candidate)
        if date_passed:
            if window:
                current_ids = {check["ts"] for check in checks if check["matches"] and check["matched_keywords"]}
                # 최신 답글 하나 때문에 과거 원글·다른 일정까지 함께 전달하지 않습니다.
                thread = {**thread, "messages": [message for message in thread["messages"] if message["ts"] in current_ids]}
            eligible.append(thread)
    matched_threads = eligible

    if not matched_threads:
        raise NoRelevantContext(missing_context_answer(question, reference_time))

    # 최고 검색 점수의 65% 이상인 묶음만 유지합니다. 정답 확률이 아닌 검색 후보를 줄이는 기준입니다.
    cutoff = matched_threads[0]["score"] * 0.65
    if trace is not None:
        trace.update(score_cutoff=cutoff, stage="입력 길이에 맞게 선택 중")
        for thread in matched_threads:
            candidate = candidates[thread["thread_id"]]
            candidate.update(
                decision="길이 점검 대기" if thread["score"] >= cutoff else "점수 제외",
                reason="" if thread["score"] >= cutoff else f"검색 점수가 기준 {cutoff:.4f} 미만",
            )
    matched_threads = [thread for thread in matched_threads if thread["score"] >= cutoff]

    _, selected_messages, skipped_threads = select_context(
        matched_threads, question, reference_time=reference_time, decisions=candidates
    )

    # 선택된 작성자만 이름을 조회합니다. 중복 제거·캐시 처리는 get_user_names가 담당합니다.
    author_ids = [message.get("user") for message in selected_messages]
    if trace is not None:
        trace["stage"] = "작성자 이름 조회 중"
    user_names = get_user_names(author_ids)

    # 이름을 추가하면 입력이 길어지므로 선택된 대화 안에서 길이를 다시 점검합니다.
    selected_ids = set()
    for message in selected_messages:
        selected_ids.add(message.get("thread_ts") or message["ts"])
    selected_threads = []
    for thread in matched_threads:
        if thread["thread_id"] in selected_ids:
            selected_threads.append(thread)
    if trace is not None:
        trace["stage"] = "작성자 이름 반영 후 길이 점검 중"
    education_info, selected_messages, author_skipped = select_context(
        selected_threads, question, user_names=user_names, reference_time=reference_time,
        decisions=candidates, selection_stage="작성자 이름 반영 후",
    )
    skipped_threads += author_skipped
    if trace is not None:
        originals = {message["ts"]: message for message in all_messages}
        # 발췌 전 본문도 남겨 선택 과정에서 조건 문장이 빠졌는지 검토할 수 있습니다.
        trace.update(stage="검색 완료", context=education_info, selected_messages=[{
            "ts": message["ts"], "thread_id": message.get("thread_ts") or message["ts"],
            "posted_at": datetime.fromtimestamp(float(message["ts"]), KST).isoformat(),
            "original_text": originals[message["ts"]].get("text") or "",
            "excerpted": (message.get("text") or "") != (originals[message["ts"]].get("text") or ""),
        } for message in selected_messages])

    if DEBUG:
        print(f"검색된 대화 묶음: {len(matched_threads)}개")
        print(f"선택한 대화 묶음: {len(matched_threads) - skipped_threads}개")
        print(f"길이 제한으로 제외한 묶음: {skipped_threads}개")
        if author_skipped:
            print(f"그중 작성자 이름 추가 후 제외한 묶음: {author_skipped}개")
        print(f"이번 질문에 사용하는 메시지: {len(selected_messages)}개")
    return education_info



def missing_context_answer(question, reference_time):
    """근거 없음과 과제 없음은 다릅니다. 일정이 없다고 단정하지 않습니다."""
    subject = "관련 안내"
    for term, label in (("시험", "시험 날짜와 범위"), ("퀘스트", "퀘스트 공지"), ("과제", "과제와 제출 기한"), ("제출", "제출 항목")):
        if term in question:
            subject = label
            break
    if "데일리" in question and "퀘스트" in question:
        subject = "데일리 퀘스트 공지"
    window = question_window(question, reference_time)
    if window:
        start, end = window
        if re.search(r"오늘|금일", question):
            prefix = f"오늘({start})의 "
        elif start == end:
            prefix = f"{start}의 "
        elif re.search(r"이번\s?주|금주", question):
            prefix = f"이번 주({start} ~ {end})의 "
        elif re.search(r"다음\s?주|차주|지난\s?주", question):
            prefix = f"{start} ~ {end}의 "
        else:
            prefix = f"현재({start}) 이후의 "
    else:
        prefix = ""
    # 한글 음절의 마지막 글자에 받침이 있으면 '을', 없으면 '를'을 붙입니다.
    particle = "을" if (ord(subject[-1]) - ord("가")) % 28 else "를"
    return f"{prefix}{subject}{particle} 검색한 Slack 기록에서 확인하지 못했습니다."



# --- 기록 읽기와 검색어 추출 ---

def load_latest_slack_data():
    """상시 봇을 준비하고, 감지한 변경의 저장이 끝난 대화기록을 읽습니다."""
    try:
        return ensure_slack_data(verbose=DEBUG)
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(f"Slack 기록 준비 실패: {error}") from None


def extract_search_keywords(client, question, *, trace=None, model=None):
    """지정한 연결로 검색어를 생성하고 같은 규칙으로 검증합니다."""
    if DEBUG:
        print("질문에서 검색어를 추출하고 있습니다.", flush=True)

    keyword_response = client.chat(
        model=KEYWORD_MODEL if model is None else model,
        messages=[
            {"role": "system", "content": KEYWORD_PROMPT},
            {"role": "user", "content": question},
        ],
        format=KEYWORD_FORMAT,  # prompts.py에 정의한 JSON 형식으로 받습니다.
        stream=False,
        options={
            "temperature": 0,
            "num_predict": 256,
            "num_ctx": NUM_CTX,
        },
    )


    try:
        if field(keyword_response, "done") is False or field(keyword_response, "done_reason") == "length":
            raise ValueError("검색어 응답이 미완료이거나 거절되었습니다.")
        # JSON 형식을 요청했어도 실제 응답이 규칙을 지키는지 다시 검사합니다.
        data = json.loads(keyword_response.message.content or "")
        raw_keywords = data["keywords"]
        if trace is not None:
            trace["raw_keywords"] = raw_keywords

        if not isinstance(raw_keywords, list):
            raise ValueError("검색어가 목록 형식이 아닙니다.")

        if not all(isinstance(word, str) for word in raw_keywords):
            raise ValueError("검색어에 문자열이 아닌 값이 있습니다.")

        # 예: [" Ollama ", "ollama"] → ["ollama"]
        keywords = []
        for word in raw_keywords:
            word = word.strip().casefold()
            if word and word not in keywords:
                keywords.append(word)

        if not 1 <= len(keywords) <= 8:
            raise ValueError("유효한 검색어가 1~8개 필요합니다.")

    except (ValueError, KeyError, TypeError) as error:
        raise SystemExit(
            f"검색어 자동 생성에 실패했습니다: {error}"
        ) from None

    expanded = expand_search_terms(keywords, question)
    if trace is not None:
        trace["search_keywords"] = expanded
    return expanded


# --- Slack 원문을 모델에게 보여 줄 텍스트로 바꾸기 ---

def normalize_slack_links(text):
    """Slack의 <URL|표시명> 표기를 일반 텍스트와 온전한 URL로 바꿉니다."""
    def replace_link(match):
        url, label = match.groups()
        return f"{label}: {url}" if label and label != url else url

    return re.sub(r"<(https?://[^|>\s]+)(?:\|([^>]*))?>", replace_link, text)


def format_message(message, user_names=None):
    """작성자, 메시지 본문, 답글 연결 정보, 첨부파일 링크를 보존합니다."""
    # ts에는 작성 시각도 들어 있습니다. 사람이 읽을 수 있는 한국 시각으로 바꿉니다.
    posted_at = datetime.fromtimestamp(
        float(message["ts"]), tz=KST
    )
    author_id = message.get("user") or message.get("bot_id") or "알 수 없음"
    author_name = (user_names or {}).get(author_id)
    author = author_id
    if author_name and author_name != author_id:
        author = f"{author_name} (ID: {author_id})"
    lines = [
        f"[메시지 ts: {message['ts']} / 작성: {posted_at:%Y-%m-%d %H:%M} KST]",
        f"작성자: {author}",
        normalize_slack_links(resolve_relative_dates(message.get("text") or "", posted_at.date())),
    ]

    parent_ts = message.get("thread_ts")
    if parent_ts and parent_ts != message["ts"]:
        lines.append(f"이 답글의 원글 ts: {parent_ts}")

    for file_info in message.get("files") or []:
        name = file_info.get("name") or "이름 미제공"
        link = file_info.get("permalink") or "링크 미제공"
        lines.append(f"첨부파일: {name} / {link}")

    return "\n".join(lines)


def time_reference(reference_time=None):
    reference_time = (reference_time or datetime.now(KST)).astimezone(KST)
    today = reference_time.date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return (
        f"[현재 기준 시각: {reference_time:%Y-%m-%d %H:%M} KST]\n"
        f"질문에서 오늘 = {today.isoformat()}, 이번 주 = {week_start.isoformat()} ~ {week_end.isoformat()} (월~일).\n"
        "아래 기록의 작성일은 현재 날짜가 아닙니다. 원문의 상대 날짜는 각각의 작성일 기준입니다."
    )


def make_prompt(question, education_info, reference_time=None):
    """선택한 참고 자료와 사용자 질문을 하나의 입력 문자열로 만듭니다."""
    return f"""
{time_reference(reference_time)}

[Slack 참고 자료: 전체 기록에서 선택한 일부]
{education_info}

[사용자 질문]
{question}
"""


def make_chat_messages(question, education_info, reference_time=None):
    # system에는 답변 규칙, user에는 참고 기록과 질문을 넣습니다.
    # 이 역할 구분만으로 원문 속 지시를 완전히 차단하는 것은 아니므로 prompts.py에서도 제한합니다.
    return [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + time_reference(reference_time)},
        {"role": "user", "content": make_prompt(question, education_info, reference_time)},
    ]


# --- 관련 대화 검색과 입력 길이 조절 ---

def find_matching_threads(all_messages, keywords, question=""):
    """전체 메시지를 검색하고, 일치한 메시지의 원글·답글을 함께 모읍니다."""
    threads = {}

    for message in all_messages:
        thread_id = message.get("thread_ts") or message["ts"]
        # 답글은 thread_ts로 원글을 찾고, 원글은 자기 ts를 묶음 ID로 사용합니다.
        if thread_id not in threads:
            threads[thread_id] = []
        threads[thread_id].append(message)

    matched_threads = []
    search_texts = {message["ts"]: message_search_text(message) for message in all_messages}
    terms = list(dict.fromkeys(compact_text(word) for word in keywords if word.strip()))
    # 일반 표현의 희소성이 고유명사·핵심 주제보다 큰 점수를 만들지 않게 합니다.
    weights = {
        term: math.log(1 + len(all_messages) / (1 + sum(term in text for text in search_texts.values())))
        * (0.2 if term in GENERAL_TERMS else 1)
        for term in terms
    }
    query_terms = [compact_text(term) for term in split_search_terms(question)]
    frequencies = {
        term: sum(term in text for text in search_texts.values()) for term in query_terms
    }
    positive = [frequency for term, frequency in frequencies.items() if frequency and term not in GENERAL_TERMS]
    rarest = min(positive) if positive else 0
    # 원래 질문에 있던 드문 단어를 보조 기준으로 삼습니다. 드문 정도의 기준은 최대(3개, 전체의 1%)입니다.
    anchors = {term for term, count in frequencies.items() if count == rarest and term not in GENERAL_TERMS} if 0 < rarest <= max(3, len(all_messages) * 0.01) else set()
    location_query = asks_question_location(question)

    for thread_id, messages in threads.items():
        messages = sorted(messages, key=lambda message: Decimal(message["ts"]))
        texts = [format_message(message) for message in messages]

        hits = [{term for term in terms if term in search_texts[message["ts"]]} for message in messages]
        best_score = max(sum(weights[term] for term in hit) for hit in hits)
        thread_score = sum(weights[term] for term in set().union(*hits))
        # 한 메시지에 모인 단서는 전부, 다른 답글에 흩어진 추가 단서는 20%만 점수에 더합니다.
        score = best_score + 0.2 * (thread_score - best_score)
        # 질문 원문의 드문 단서(예: '본격', '100%')가 있는 대화를 우선합니다.
        # 모델이 추가한 넓은 유사어가 정확히 일치하는 대화를 밀어내지 않게 합니다.
        if any(term in search_texts[message["ts"]] for term in anchors for message in messages):
            score *= 2
        if location_query and score > 0:
            # 문의 경로를 묻는 질문에 기술 문답 자체가 안내문보다 앞서지 않도록 합니다.
            guidance = max(routing_evidence(message.get("text") or "") for message in messages)
            score *= 1 + 3 * guidance

        if score > 0:
            matched_threads.append({
                "thread_id": thread_id,
                "score": score,
                "keywords": keywords,
                "latest_ts": Decimal(messages[-1]["ts"]),
                "messages": messages,
                "text": f"[대화 묶음: {thread_id}]\n" + "\n\n".join(texts),
            })

    # 같은 점수라면 더 최근 메시지가 있는 묶음을 먼저 선택합니다.
    matched_threads.sort(
        key=lambda thread: (thread["score"], thread["latest_ts"]),
        reverse=True,
    )
    return matched_threads


def excerpt_messages(thread, max_bytes):
    """긴 대화는 일치 문단과 이웃 문단을 발췌합니다. 원글·답글 연결은 남깁니다."""
    terms = [compact_text(word) for word in thread.get("keywords", [])]
    messages = thread["messages"]
    ranked = sorted(
        range(len(messages)),
        key=lambda index: sum(term in message_search_text(messages[index]) for term in terms),
        reverse=True,
    )
    # 질문 원글, 가장 관련 있는 메시지, 그 다음 답글을 함께 고려합니다.
    best = ranked[0]
    indexes = sorted({0, best, min(best + 1, len(messages) - 1)})
    result = []
    for index in indexes:
        message = messages[index]
        text = message.get("text") or ""
        if len(text.encode("utf-8")) <= max_bytes:
            result.append(message)
            continue
        paragraphs = [part for part in re.split(r"\n+", text) if part.strip()]
        matches = sorted(
            range(len(paragraphs)),
            key=lambda n: sum(term in compact_text(paragraphs[n]) for term in terms),
            reverse=True,
        )
        chosen = set()
        for n in matches:
            if not any(term in compact_text(paragraphs[n]) for term in terms):
                continue
            # 바로 다음 문단의 단서·조건(예: '다만 ...')도 가능하면 함께 보존합니다.
            for candidate in (n, min(n + 1, len(paragraphs) - 1), max(n - 1, 0)):
                proposed = chosen | {candidate}
                if sum(len(paragraphs[i].encode("utf-8")) + 30 for i in proposed) <= max_bytes:
                    chosen = proposed
        if not chosen:
            return []  # 한 문단조차 들어가지 않으면 문장 중간을 임의로 자르지 않습니다.
        excerpt = "\n[일부 생략]\n".join(paragraphs[i] for i in sorted(chosen))
        result.append({**message, "text": "[관련 문단 발췌]\n" + excerpt})
    return result


def select_context(matched_threads, question, user_names=None, reference_time=None, *,
                   decisions=None, selection_stage="작성자 이름 반영 전"):
    """반환값: (참고 자료 문자열, 선택된 메시지 목록, 제외한 대화 묶음 수)."""
    selected_messages = []
    parts = []
    skipped_threads = 0

    for thread in matched_threads:
        selected = None
        # 전체 대화가 들어가지 않으면 관련 문단을 단계적으로 줄여 다시 점검합니다.
        for max_bytes in (None, 1800, 900, 450):
            candidates = thread["messages"] if max_bytes is None else excerpt_messages(thread, max_bytes)
            if not candidates:
                continue
            thread_text = f"[대화 묶음: {thread['thread_id']}]\n" + "\n\n".join(
                format_message(message, user_names) for message in candidates
            )
            candidate_info = "\n\n---\n\n".join(parts + [thread_text])
            candidate_prompt = make_prompt(question, candidate_info, reference_time)
            # 실제 토크나이저 대신 UTF-8 바이트 수로 보수적으로 길이를 추정합니다.
            # 출력 한도와 형식 여유 512를 예약하므로, 모델이 수용 가능한 기록도 일부 제외될 수 있습니다.
            estimated_size = (
                len((SYSTEM_PROMPT + "\n" + time_reference(reference_time)).encode("utf-8"))
                + len(candidate_prompt.encode("utf-8"))
                + NUM_PREDICT + 512
            )
            if estimated_size <= NUM_CTX:
                selected = candidates
                break
        if selected is None:
            skipped_threads += 1
            if decisions and thread["thread_id"] in decisions:
                decisions[thread["thread_id"]].update(
                    decision="길이 제외", reason=f"{selection_stage}: 전체·발췌 대화가 남은 입력 길이에 들어가지 않음",
                )
            continue

        if decisions and thread["thread_id"] in decisions:
            decisions[thread["thread_id"]].update(
                decision="선택" if selection_stage == "작성자 이름 반영 후" else "1차 선택",
                reason=f"{selection_stage}: " + ("원문 사용" if max_bytes is None else f"메시지당 {max_bytes}바이트 발췌 사용"),
            )

        parts.append(thread_text)
        selected_messages.extend(selected)

    if not selected_messages:
        raise SystemExit(
            "검색 결과는 있지만 대화 묶음이 길이 사전 점검을 초과했습니다. "
            "긴 대화를 나누거나 입력 자료를 정리해야 합니다."
        )

    education_info = "\n\n---\n\n".join(parts)
    return education_info, selected_messages, skipped_threads




if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        debug_print("\n질문을 종료했습니다.")
