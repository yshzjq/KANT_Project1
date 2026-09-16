"""읽는 순서: main → answer_question → prepare_context → 나머지 보조 함수.

Slack 기록 확인 → 질문 입력 → 검색어 추출 → 관련 대화 선택 → 답변 출력.
"""

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
from search_terms import compact_text, expand_search_terms, message_search_text
from slack_dates import applies_to_window, question_window, resolve_relative_dates
from evaluation_metrics import EXPERIMENT_KINDS, MeasuredClient, render_question, render_summary, running_model, validate_questions


# MODEL = "qwen3:4b-instruct-2507-q4_K_M"
MODEL = "exaone3.5:7.8b"

# 모델이 한 번에 처리할 입력·출력 크기와 답변 길이의 설정값입니다.
NUM_CTX = 8192
NUM_PREDICT = 512
OLLAMA_HOST = "http://127.0.0.1:11434"
BASE_DIR = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))


class NoRelevantContext(ValueError):
    """검색된 근거가 없을 때 추측 대신 반환할 안내입니다."""


def main():
    """프로그램 전체 순서입니다. 이 함수부터 읽으면 됩니다."""
    # 1. Slack 변경을 확인하고, 사용할 대화기록을 파일에서 읽습니다.
    saved_data = load_latest_slack_data()
    all_messages = saved_data.get("messages", [])
    if not all_messages:
        raise SystemExit("저장된 Slack 메시지가 없습니다.")


    # 평가 모드
    if QUESTION_EVALUATION:
        run_question_evaluation(all_messages)
        return

    run_interactive_chat(all_messages)


def run_interactive_chat(all_messages):
    """직접 입력 모드에서는 종료할 때까지 질문 → 답변을 반복합니다."""
    print(f"\n대화기록 {len(all_messages)}개를 준비했습니다.")
    print("질문을 입력하면 답변합니다. 종료: 종료 / exit / quit / Ctrl+C")
    while True:
        try:
            question = input("\n질문: ").strip()
            if not question:
                continue
            if question.casefold() in {"종료", "exit", "quit"}:
                print("질문을 종료했습니다.")
                return
            try:
                # 입력을 기다리는 동안 봇이 저장한 신규·수정·삭제도 다음 답변에 반영합니다.
                all_messages = load_latest_slack_data().get("messages", [])
                if not all_messages:
                    raise ValueError("저장된 Slack 메시지가 없습니다.")
                answer_question(question, all_messages)
            except (Exception, SystemExit) as error:
                print(f"답변 처리에 실패했습니다: {error}\n다른 질문을 입력하거나 다시 시도해 주세요.")
        except (KeyboardInterrupt, EOFError):
            print("\n질문을 종료했습니다.")
            return


def answer_question(question, all_messages, client=None):
    """검색어를 만든 뒤 관련 근거가 있을 때 최종 답변을 생성합니다."""
    client = client if client is not None else Client(host=OLLAMA_HOST, timeout=180)
    reference_time = datetime.now(KST)

    keywords = extract_search_keywords(client, question)
    try:
        education_info = prepare_context(all_messages, keywords, question, reference_time)
    except NoRelevantContext as error:
        answer = str(error)
        print(answer)
        return answer

    print("Ollama의 답변을 기다리고 있습니다.", flush=True)
    response = client.chat(
        model=MODEL,
        messages=make_chat_messages(question, education_info, reference_time),
        stream=False,  # 답변을 전부 받은 뒤 한 번에 출력합니다.
        options={"temperature": 0, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
    )
    answer = clean_answer(response.message.content or "")
    print(answer)
    return answer


def clean_answer(answer):
    """터미널과 평가 파일에 같은 정리된 답변을 사용합니다."""
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


def run_question_evaluation(all_messages, *, experiment_kind="main"):
    """동일한 기록으로 질문을 순서대로 평가하고, 매 항목을 즉시 파일에 기록합니다."""
    if experiment_kind not in EXPERIMENT_KINDS:
        raise ValueError("experiment_kind는 main / warmup / retry / extra 중 하나여야 합니다.")
    questions = validate_questions(QUESTION_LIST)
    output_dir = BASE_DIR / QUESTION_EVALUATION_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    model_slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", MODEL)
    output_path = output_dir / f"question_evaluation_{experiment_kind}_{model_slug}_{stamp}_{uuid4().hex[:8]}.md"
    print(f"\n공통 질문 {len(questions)}개 평가를 시작합니다.\n저장 위치: {output_path}")

    client = Client(host=OLLAMA_HOST, timeout=180)
    observer = Client(host=OLLAMA_HOST, timeout=5)
    records = []
    # ps 조회·보고서 기록은 elapsed에 포함하지 않습니다. 모델 로딩은 포함됩니다.
    with output_path.open("x", encoding="utf-8") as report:
        report.write(
            f"# LLM 교육 안내 공통 질문 평가 결과\n\n## 테스트 모델\n\n`{MODEL}`\n\n"
            f"실험 구분: {EXPERIMENT_KINDS[experiment_kind]} / 실행 시각: {datetime.now(KST).isoformat()}\n\n"
            f"Ollama 서버: `{OLLAMA_HOST}`\n\n"
            "응답시간(elapsed)은 검색어 생성·검색·이름 조회·최종 답변까지의 전체 처리 시간입니다. "
            "최초 Slack 동기화, ps 조회, 보고서 저장은 제외합니다. TTFT는 별도로 측정하지 않았습니다.\n\n"
            "모델 로딩 시간 = load_duration / 1,000,000,000초. "
            "토큰 생성 속도 = eval_count / (eval_duration / 1,000,000,000) tokens/s.\n\n"
            "VRAM = 질문 직후 client.ps()의 해당 모델 size_vram / 1,048,576 MiB이며 최대 사용량이 아닙니다. "
            "실제 context_length는 ps 관측값이며 요청한 num_ctx나 모델의 최대 지원 길이로 대체하지 않습니다.\n\n"
            "시작 전 미적재/로드됨은 ps 관측 기준입니다. 첫 질문이라고 미적재로 가정하지 않습니다. "
            "검색어 생성에서 모델이 로드되면 같은 질문의 최종 답변은 그 로드 상태를 재사용할 수 있습니다.\n\n"
            "자동 워밍업·재시도는 하지 않습니다. warmup/retry/extra 실행은 별도 파일로 기록합니다. "
            "tokens/s만으로 품질이나 체감 속도를 판단하지 않습니다. 품질 점수는 자동 부여하지 않으며 원본 응답을 보존합니다.\n\n"
            "각 호출의 출력 한도·생성 설정은 아래 요청 설정에 기록합니다. "
            "미지정 옵션(예: seed/top_p/keep_alive)은 모델·서버 기본값이며 실제 적용값은 미측정입니다.\n\n"
        )
        report.flush()
        try:
            for number, item in enumerate(questions, start=1):
                question = item["question"]
                print(f"\n[Q{number:02d}/{len(questions):02d}] {question}", flush=True)
                measured = MeasuredClient(client)
                before = running_model(observer, MODEL)
                status, answer = "처리 실패", ""
                started = time.perf_counter()
                try:
                    answer = answer_question(question, all_messages, client=measured)
                    status = "답변 완료" if len(measured.calls) == 2 and measured.calls[-1]["success"] else "안내 반환 (최종 모델 미호출)"
                    if status == "답변 완료":
                        original = measured.calls[-1]["response"]
                        if original["done_reason"] == "length":
                            status = "불완전 응답 (출력 한도 도달)"
                        elif original["done"] is False or not (original["content"] or "").strip() or not answer.strip():
                            status = "불완전 응답 (미완료 또는 빈 원문)"
                except (KeyboardInterrupt, EOFError):
                    status, answer = "사용자 중단", "평가를 중단했습니다."
                    raise
                except (Exception, SystemExit) as error:
                    answer = f"평가 실패: {error}"
                    print(answer, flush=True)
                finally:
                    elapsed = time.perf_counter() - started
                    record = {
                        "number": number, "question": question, "answer": answer,
                        "type": item["type"], "expected_result": item["expected_result"],
                        "model": MODEL, "status": status, "elapsed": elapsed,
                        "before": before, "calls": measured.calls,
                        "after": {
                            "loaded": None, "observed_at": datetime.now(timezone.utc).isoformat(),
                            "reason": "사용자 중단으로 질문 직후 적재 정보를 조회하지 못함",
                        },
                    }
                    records.append(record)
                    # 답변을 먼저 보관해 두고, ps 조회 중 Ctrl+C가 와도 반드시 파일에 남깁니다.
                    try:
                        if status != "사용자 중단":
                            record["after"] = running_model(observer, MODEL)
                    finally:
                        report.write(render_question(record))
                        report.flush()
        finally:
            report.write(render_summary(records, len(questions), experiment_kind))
            report.flush()
    print(f"\n평가 결과 저장 완료: {output_path}")
    return output_path


def prepare_context(all_messages, keywords, question, reference_time=None):
    """검색 → 길이에 맞게 선택 → 작성자 이름 추가 순서로 참고 자료를 만듭니다."""
    if DEBUG:
        print(f"자동 검색어: {', '.join(keywords)}", flush=True)
        print(f"전체 검색 대상: {len(all_messages)}개 메시지")

    matched_threads = find_matching_threads(all_messages, keywords, question)
    reference_time = reference_time or datetime.now(KST)
    window = question_window(question, reference_time)
    if window:
        # 원글·답글은 묶어 보존하되, 기간에 해당하는 메시지 자체에도 주제 단서가 있어야 합니다.
        matched_threads = [thread for thread in matched_threads if any(
            applies_to_window(message, window)
            and any(compact_text(word) in message_search_text(message) for word in keywords)
            for message in thread["messages"]
        )]

    if not matched_threads:
        raise NoRelevantContext(missing_context_answer(question, reference_time))

    # 관련성이 낮은 공지가 답변에 섞이지 않도록 상위 점수에 가까운 대화부터 사용합니다.
    cutoff = matched_threads[0]["score"] * 0.65
    matched_threads = [thread for thread in matched_threads if thread["score"] >= cutoff]

    # 1차 선택: 모델 입력에 들어갈 수 있는 대화만 고릅니다.
    _, selected_messages, skipped_threads = select_context(
        matched_threads, question, reference_time=reference_time
    )

    # 선택된 작성자만 이름을 조회합니다. 중복 제거·캐시 처리는 get_user_names가 담당합니다.
    author_ids = [message.get("user") for message in selected_messages]
    user_names = get_user_names(author_ids)

    # 이름을 추가하면 입력이 길어지므로 선택된 대화 안에서 길이를 다시 점검합니다.
    selected_ids = set()
    for message in selected_messages:
        selected_ids.add(message.get("thread_ts") or message["ts"])
    selected_threads = []
    for thread in matched_threads:
        if thread["thread_id"] in selected_ids:
            selected_threads.append(thread)
    education_info, selected_messages, author_skipped = select_context(
        selected_threads, question, user_names=user_names, reference_time=reference_time
    )
    skipped_threads += author_skipped

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
    particle = "을" if (ord(subject[-1]) - ord("가")) % 28 else "를"
    return f"{prefix}{subject}{particle} 검색한 Slack 기록에서 확인하지 못했습니다."



# --- 기록 읽기와 검색어 추출 ---

def load_latest_slack_data():
    """상시 봇을 준비하고, 감지한 변경의 저장이 끝난 대화기록을 읽습니다."""
    try:
        return ensure_slack_data()
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(f"Slack 기록 준비 실패: {error}") from None


# 키워드를 뽑아내는 메소드
def extract_search_keywords(client, question):
    """로컬 Ollama 모델로 질문의 검색어를 자동 생성합니다."""
    if DEBUG:
        print("질문에서 검색어를 추출하고 있습니다.", flush=True)

    keyword_response = client.chat(
        model=MODEL,
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
        # 모델이 반환한 JSON 문자열을 Python 사전으로 바꿉니다.
        data = json.loads(keyword_response.message.content or "")
        raw_keywords = data["keywords"]

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

    return expand_search_terms(keywords, question)


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
    # 판단 지침과 Slack 원문을 서로 다른 역할의 메시지로 전달합니다.
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
    # 여러 공지에 반복되는 '학습'보다 '100%'처럼 드문 핵심 단서의 비중을 높입니다.
    weights = {
        term: math.log(1 + len(all_messages) / (1 + sum(term in text for text in search_texts.values())))
        for term in terms
    }
    query_terms = [compact_text(term) for term in expand_search_terms([], question)]
    frequencies = {
        term: sum(term in text for text in search_texts.values()) for term in query_terms
    }
    positive = [frequency for frequency in frequencies.values() if frequency]
    rarest = min(positive) if positive else 0
    anchors = {term for term, count in frequencies.items() if count == rarest} if 0 < rarest <= max(3, len(all_messages) * 0.01) else set()

    for thread_id, messages in threads.items():
        messages = sorted(messages, key=lambda message: Decimal(message["ts"]))
        texts = [format_message(message) for message in messages]

        hits = [{term for term in terms if term in search_texts[message["ts"]]} for message in messages]
        best_score = max(sum(weights[term] for term in hit) for hit in hits)
        thread_score = sum(weights[term] for term in set().union(*hits))
        # 원글과 답글에 나뉜 단서도 반영하며, 같은 말의 반복은 점수를 올리지 않습니다.
        score = best_score + 0.2 * (thread_score - best_score)
        # 질문 원문의 드문 단서(예: '본격', '100%')가 있는 대화를 우선합니다.
        # 모델이 추가한 넓은 유사어가 정확히 일치하는 대화를 밀어내지 않게 합니다.
        if any(term in search_texts[message["ts"]] for term in anchors for message in messages):
            score *= 2

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


def select_context(matched_threads, question, user_names=None, reference_time=None):
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
            continue

        parts.append(thread_text)
        selected_messages.extend(selected)

    if not selected_messages:
        raise SystemExit(
            "검색 결과는 있지만 대화 묶음이 길이 사전 점검을 초과했습니다. "
            "긴 대화를 나누거나 입력 자료를 정리해야 합니다."
        )

    education_info = "\n\n---\n\n".join(parts)
    return education_info, selected_messages, skipped_threads




# 파일을 직접 실행하면 맨 위의 main()부터 시작합니다.
if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n질문을 종료했습니다.")
