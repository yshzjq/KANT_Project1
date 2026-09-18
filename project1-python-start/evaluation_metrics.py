"""로컬·클라우드 호출 통계와 실행 조건을 수집하고 Markdown 평가 보고서를 만듭니다.

미측정 값은 None과 사유로 보관합니다. ps 조회는 질문의 elapsed 구간 밖에서 합니다.
"""

# 검증 순서: MeasuredClient.chat → response_metrics → render_question → render_summary.
# 모델 응답 자체와 성능 통계는 분리해서 보며, 품질 점수는 여기서 계산하지 않습니다.
# 관련 검증: tests/test_evaluation_metrics.py, tests/test_question_evaluation.py.

import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from statistics import mean


EXPERIMENT_KINDS = {"main": "본 실험", "warmup": "워밍업", "retry": "재시도", "extra": "추가 실험"}


def validate_questions(items):
    """잘못된 설정으로 모델을 호출한 뒤 기록 단계에서 실패하지 않도록 먼저 확인합니다."""
    if not isinstance(items, list) or not items:
        raise ValueError("QUESTION_LIST는 항목이 하나 이상 있는 목록이어야 합니다.")
    fields = ("type", "question", "expected_result")
    questions = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"QUESTION_LIST[{number - 1}]: type, question, expected_result를 가진 사전이어야 합니다.")
        for name in fields:
            if not isinstance(item.get(name), str) or not item[name].strip():
                raise ValueError(f"QUESTION_LIST[{number - 1}].{name}: 비어 있지 않은 문자열이 필요합니다.")
        questions.append({name: item[name] for name in fields})
    return questions


def field(obj, name):
    # 실제 SDK 응답 객체와 테스트용 사전을 같은 계산 함수에 넣을 수 있게 합니다.
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def numeric(value, minimum=0, strict=False):
    # Python에서는 True도 int로 취급하므로 따로 제외합니다. NaN·무한대도 평균에 넣지 않습니다.
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and (value > minimum if strict else value >= minimum)
    )


def metric(value=None, reason=None):
    # value=None은 미측정, value=0은 실제 측정된 0입니다. 미측정 이유를 함께 보관합니다.
    return {"value": value, "reason": reason}


def response_metrics(response):
    if field(response, "provider") == "openai_responses":
        return {
            "load_seconds": metric(reason="OpenAI Responses API가 load_duration을 제공하지 않음"),
            "tokens_per_second": metric(reason="OpenAI Responses API가 eval_duration을 제공하지 않음"),
        }
    # Ollama의 duration은 ns(10억분의 1초)입니다. 생성 속도에는 생성 시간만 분모로 사용합니다.
    # 모델 로딩 시간이나 프롬프트 처리 시간을 분모에 섞으면 다른 지표가 됩니다.
    load = field(response, "load_duration")
    count = field(response, "eval_count")
    duration = field(response, "eval_duration")
    loading = metric(load / 1_000_000_000) if numeric(load) else metric(reason="load_duration 누락 또는 유효하지 않은 값")
    if not numeric(count):
        speed = metric(reason="eval_count 누락 또는 유효하지 않은 값")
    elif not numeric(duration, strict=True):
        speed = metric(reason="eval_duration 누락, 0 이하 또는 유효하지 않은 값")
    else:
        speed = metric(count / (duration / 1_000_000_000))
    return {"load_seconds": loading, "tokens_per_second": speed}


def canonical_model(tag):
    if not isinstance(tag, str) or not tag:
        return None
    return tag if ":" in tag.rsplit("/", 1)[-1] else tag + ":latest"


def tokenizer_metadata(client, model):
    """모델명으로 추정하지 않고 /api/show의 tokenizer 메타데이터를 보관합니다."""
    snapshot = {"model": model, "observed_at": datetime.now(timezone.utc).isoformat(), "metadata": {}}
    if getattr(client, "provider", None) == "openai_responses":
        return {**snapshot, "source": "OpenAI Responses API",
                "reason": "API가 실제 tokenizer 메타데이터를 제공하지 않음"}
    snapshot["source"] = "Ollama /api/show → model_info"
    try:
        response = client.show(model)
        # Ollama Python SDK는 modelinfo, HTTP JSON은 model_info라는 이름을 사용합니다.
        info = field(response, "modelinfo")
        if not isinstance(info, Mapping):
            info = field(response, "model_info")
        if not isinstance(info, Mapping):
            return {**snapshot, "reason": "client.show() 응답에 model_info가 없음"}
        # 토큰 사전·merges 배열은 매우 큽니다. tokenizer 종류·전처리·특수 토큰 등 단일 값만 기록합니다.
        snapshot["metadata"] = {
            key: value for key, value in info.items()
            if isinstance(key, str) and key.startswith("tokenizer.ggml.")
            and (isinstance(value, (str, bool)) or numeric(value))
        }
        if not snapshot["metadata"]:
            snapshot["reason"] = "model_info에 tokenizer.ggml 메타데이터가 없음"
    except Exception as error:
        # 메타데이터 조회 실패는 모델 생성 실패와 별개이며 평가를 중단하지 않습니다.
        snapshot["reason"] = f"client.show() 조회 실패 ({type(error).__name__})"
    return snapshot


def running_model(client, model):
    if getattr(client, "provider", None) == "openai_responses":
        # 클라우드 상태를 로컬 Ollama의 ps 결과로 잘못 대체하지 않습니다.
        return client.runtime_snapshot()
    # loaded의 세 상태: True=목록에서 확인, False=목록에 없음, None=조회·판단 불가.
    snapshot = {"observed_at": datetime.now(timezone.utc).isoformat(), "loaded": None}
    try:
        models = field(client.ps(), "models")
        if not isinstance(models, (list, tuple)):
            raise ValueError("models 목록 누락")
        # 여러 모델이 적재돼 있어도 요청한 태그와 일치하는 항목만 선택합니다.
        matches = [item for item in models if canonical_model(model) in (
            canonical_model(field(item, "model")), canonical_model(field(item, "name")),
        )]
        if not matches:
            return {**snapshot, "loaded": False, "reason": "client.ps()에 해당 모델이 없음"}
        if len(matches) != 1:
            raise ValueError("동일 모델 항목이 여러 개여서 구분 불가")
        item = matches[0]
        snapshot.update({
            "loaded": True,
            **{name: field(item, name) for name in ("model", "name", "digest", "context_length", "size", "size_vram")},
            "quantization_level": field(field(item, "details"), "quantization_level"),
        })
    except Exception as error:
        snapshot["reason"] = f"client.ps() 조회 실패 ({type(error).__name__})"
    return snapshot


def vram_metric(snapshot):
    # size_vram은 바이트입니다. 1 MiB=1024*1024바이트이며, GPU 전체 사용량·최댓값은 아닙니다.
    value = snapshot.get("size_vram")
    if numeric(value):
        return metric(value / (1024 * 1024))
    return metric(reason=snapshot.get("reason") or "size_vram 누락 또는 유효하지 않은 값")


def processor_state(snapshot):
    size, vram = snapshot.get("size"), snapshot.get("size_vram")
    if not numeric(vram):
        return "측정 불가: " + (snapshot.get("reason") or "size_vram 누락")
    # Ollama CLI의 PROCESSOR 표기와 같은 size/size_vram 비교입니다. GPU 사용률이 아닙니다.
    if vram == 0:
        return "100% CPU"
    if not numeric(size, strict=True) or vram > size:
        return "측정 불가: size 누락 또는 size/size_vram 불일치"
    if vram == size:
        return "100% GPU"
    gpu = vram / size * 100
    return f"CPU {100 - gpu:.1f}% / GPU {gpu:.1f}% (적재 메모리 비율)"


class MeasuredClient:
    """기존 client.chat 호출을 그대로 전달하면서 반환 통계와 실패를 기록합니다."""

    def __init__(self, client, *, calls=None):
        self.client = client
        # 별도 키워드 연결도 같은 목록을 쓰면 호출 순서와 질문별 성공 수를 보존할 수 있습니다.
        self.calls = [] if calls is None else calls

    def chat(self, **kwargs):
        # 현재 answer_question의 호출 순서에 대응합니다. 호출 단계를 추가하면 이 구분도 함께 검토합니다.
        record = {
            "stage": "검색어 생성" if not self.calls else "최종 답변",
            "model": kwargs.get("model"), "success": False,
            "provider": "ollama",
            "settings": {key: kwargs[key] for key in ("options", "stream", "format", "think", "keep_alive") if key in kwargs},
        }
        if getattr(self.client, "provider", None) == "openai_responses":
            record["provider"] = "openai_responses"
            record["settings"] = self.client.request_settings(**kwargs)
        self.calls.append(record)
        started = time.perf_counter()
        try:
            response = self.client.chat(**kwargs)
        except BaseException as error:
            record["error"] = type(error).__name__
            raise
        else:
            record["success"] = True  # 서버 응답 수신 여부이며, 내용의 정확성·완결성과는 별개입니다.
            record["response"] = {
                name: field(response, name) for name in (
                    "model", "created_at", "done", "done_reason", "total_duration", "load_duration",
                    "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
                )
            }
            record["response"]["content"] = field(field(response, "message"), "content")
            record["response"]["thinking"] = field(field(response, "message"), "thinking")
            if field(response, "provider") == "openai_responses":
                # Responses의 usage·status·refusal을 변환 전 형태로 남깁니다.
                record["response"]["openai_response"] = field(response, "openai_response")
            record.update(response_metrics(response))
            return response
        finally:
            record["elapsed"] = time.perf_counter() - started


def number_text(value):
    # 이미 로드된 모델의 수 ms 로딩 시간도 0.00초로 반올림해 지우지 않습니다.
    if 0 < value < 0.005:
        return f"{value:.9f}".rstrip("0")
    return f"{value:.2f}"


def display(measurement, unit):
    value = measurement["value"]
    if value is None:
        return "측정 불가: " + (measurement["reason"] or "사유 미제공")
    return f"{number_text(value)}{unit}"


def average(values, unit):
    # n은 전체 질문 수가 아니라 이 지표의 유효한 값 개수입니다. 각 지표의 n은 달라도 됩니다.
    values = [value for value in values if numeric(value)]
    return f"{number_text(mean(values))}{unit} (n={len(values)})" if values else "측정 불가 (n=0)"


def fenced(text, language=""):
    # 원문에 ```가 포함돼도 보고서의 코드 블록이 중간에 닫히지 않도록 더 긴 구분자를 씁니다.
    fence = "`" * max(3, 1 + max((len(match[0]) for match in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}\n"


def snapshot_value(snapshot, name):
    value = snapshot.get(name)
    if value is None or value == "":
        return "측정 불가: " + (snapshot.get("reason") or f"{name} 누락")
    if name == "context_length" and not numeric(value, strict=True):
        return "측정 불가: context_length가 0 이하 또는 유효하지 않은 값"
    return str(value)


def initial_state(snapshot):
    return {True: "시작 전 로드됨", False: "시작 전 미적재", None: "시작 전 상태 미확인"}[snapshot["loaded"]]


def table_cell(value):
    # Slack 본문·검색어의 파이프나 줄바꿈으로 Markdown 표가 깨지지 않게 합니다.
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;"
    ).replace("|", "&#124;").replace("`", "&#96;").replace("\r", " ").replace("\n", " ")


def render_model_metadata(model, keyword_model, snapshots):
    """두 단계의 모델과 tokenizer를 연결하고 원본 메타데이터를 한 번씩 남깁니다."""
    lines = ["## 테스트 모델", "", f"- 최종 답변 MODEL: `{model}`",
             f"- 검색어 생성 KEYWORD_MODEL: `{keyword_model}`", "",
             "### 모델별 tokenizer", "",
             "| 역할 | 모델 태그 | tokenizer.ggml.model | tokenizer.ggml.pre |",
             "|---|---|---|---|"]
    for role, tag in (("검색어 생성", keyword_model), ("최종 답변", model)):
        snapshot = snapshots[tag]
        values = []
        for key in ("tokenizer.ggml.model", "tokenizer.ggml.pre"):
            value = snapshot["metadata"].get(key)
            values.append(value if isinstance(value, str) and value.strip() else
                          "확인 불가: " + (snapshot.get("reason") or f"{key} 누락 또는 유효하지 않은 값"))
        lines.append("| " + " | ".join(table_cell(value) for value in (role, tag, *values)) + " |")
    lines += ["", "model은 tokenizer 형식 식별자, pre는 사전 분할 방식 식별자입니다. "
              "예를 들어 gpt2 표기가 답변 모델이 GPT-2라는 뜻은 아닙니다. "
              "위 값은 조회된 메타데이터이며, 외부 tokenizer 패키지명·버전은 추정하지 않습니다.", ""]
    for tag, snapshot in snapshots.items():
        lines += [f"#### {tag} — tokenizer 원본 메타데이터", "",
                  fenced(json.dumps(snapshot, ensure_ascii=False, indent=2), "json")]
    return "\n".join(lines) + "\n"


def render_retrieval(trace):
    """검색 진단은 응답과 분리해 보여 주며, 원문과 실제 전송 내용을 구분합니다."""
    if trace is None:
        return "### 검색 과정\n\n검색 진단 기록 없음 (이전 형식의 기록).\n"
    window = trace.get("date_window")
    date_label = " ~ ".join(window) if window else "날짜 제한 없음" if "date_window" in trace else "미수행"
    lines = [
        "### 검색 과정", "",
        f"- 마지막 처리 단계: {trace['stage']}",
        f"- 질문 기준 시각 (KST): {trace.get('reference_time', '미수행')}",
        f"- 질문 날짜 범위: {date_label}", "",
        "#### 모델이 생성한 검색어", "",
        fenced(json.dumps(trace["raw_keywords"], ensure_ascii=False), "json") if "raw_keywords" in trace else "검색어 해석 전 실패·중단 또는 미수행. 원본 응답은 호출 기록을 확인하세요.",
        "", "#### 실제 검색에 사용한 검색어", "",
        fenced(json.dumps(trace["search_keywords"], ensure_ascii=False), "json") if "search_keywords" in trace else "미수행",
    ]
    if "candidates" in trace:
        candidates = trace["candidates"]
        lines += ["", f"- 검색어가 일치한 후보: {len(candidates)}개 대화 묶음"]
        if "score_cutoff" in trace:
            lines.append(f"- 검색 점수 통과 기준: {trace['score_cutoff']:.4f} (날짜 통과 후보 최고 점수의 65%)")
        lines += ["", "#### 후보 대화와 선택·제외 사유", ""]
        if candidates:
            lines += ["| 검색 순위 | 대화 ID | 점수 | 일치 검색어 | 처리 | 사유 |",
                      "|---:|---|---:|---|---|---|"]
            for candidate in candidates:
                values = [candidate["rank"], candidate["thread_id"], f"{candidate['score']:.4f}",
                          ", ".join(candidate["matched_keywords"]), candidate["decision"], candidate["reason"]]
                lines.append("| " + " | ".join(table_cell(value) for value in values) + " |")
        else:
            lines.append("일치한 후보가 없습니다. 이 결과만으로 실제 일정이나 과제가 없다고 판단하지 않습니다.")
        if window:
            lines += ["", "#### 메시지별 날짜 판정", "",
                      "구체 날짜와 검색 주제의 연결을 확인한 결과입니다. 기간·주제를 통과한 메시지만 참고합니다.", "",
                      "| 대화 ID | 메시지 ID | 게시일 | 판정에 사용한 본문 날짜 | 일치 검색어 | 날짜 판정 | 사유 | 주제·날짜 연결 문장 |",
                      "|---|---|---|---|---|---|---|---|"]
            for candidate in candidates:
                for check in candidate["date_checks"]:
                    values = [candidate["thread_id"], check["ts"], check["posted_on"],
                              ", ".join(check["resolved_dates"]) or "없음", ", ".join(check["matched_keywords"]) or "없음",
                              "통과" if check["matches"] else "제외", check["reason"], check.get("topic_evidence", "—")]
                    lines.append("| " + " | ".join(table_cell(value) for value in values) + " |")
    if trace.get("no_context_reason"):
        lines += ["", f"최종 답변 모델 미호출 사유: {trace['no_context_reason']}"]
    lines += ["", "#### 선택된 Slack 원문 (발췌·날짜 변환 전)", ""]
    for message in trace.get("selected_messages", []):
        lines += [f"- 메시지 ID: {message['ts']} / 대화 ID: {message['thread_id']} / 작성: {message['posted_at']}",
                  f"- 본문 발췌 여부: {'예' if message['excerpted'] else '아니요'}", "",
                  fenced(message["original_text"], "text")]
    if not trace.get("selected_messages"):
        lines.append("최종 원문 선택 기록 없음. 위 처리 단계와 제외 사유를 확인하세요.")
    lines += ["", "#### 모델용 Slack 문맥 (발췌·날짜 변환 후)", ""]
    lines.append(fenced(trace["context"], "text") if trace.get("context") is not None else "작성되지 않았습니다.")
    lines += ["", "#### 최종 답변 모델 요청 메시지", ""]
    if "final_messages" in trace:
        lines += ["호출 직전의 system/user 메시지입니다. 응답 수신 성공 여부는 호출 결과를 확인하세요.", "",
                  fenced(json.dumps(trace["final_messages"], ensure_ascii=False, indent=2), "json")]
    else:
        lines.append("최종 답변 모델을 호출하지 않았습니다.")
    return "\n".join(lines) + "\n"


def render_question(record):
    after = record["after"]
    lines = [
        f"## Q{record['number']:02d}", "", "**질문**", "", record["question"], "",
        "**유형**", "", record["type"], "",
        "**기대 결과**", "", record["expected_result"], "",
        "**답변**", "", record["answer"], "", "**응답시간**", "", f"{record['elapsed']:.2f}초", "",
        f"- 처리 결과: {record['status']}",
        f"- 실행 순서: {record['number']}번째 질문 / {initial_state(record['before'])}",
        f"- VRAM (질문 직후, 최종 답변 모델): {display(vram_metric(after), ' MiB')}",
        "", "### 실행 조건", "",
        f"- 최종 답변 MODEL: `{record['model']}`",
        f"- 검색어 생성 KEYWORD_MODEL: `{record.get('keyword_model', record['model'])}`",
        "- tokenizer: 보고서 상단의 모델별 tokenizer 참조",
        "- 아래 적재 정보는 최종 답변 모델의 관측값입니다.",
        f"- 관측 모델 태그: {snapshot_value(after, 'model')}",
        f"- digest: {snapshot_value(after, 'digest')}",
        f"- details.quantization_level: {snapshot_value(after, 'quantization_level')}",
        f"- 실제 context_length: {snapshot_value(after, 'context_length')}",
        f"- CPU/GPU 적재 상태: {processor_state(after)}",
        f"- 적재 정보 관측 시각 (UTC): {after['observed_at']}",
    ]
    if "keyword_after" in record:
        keyword_after = record["keyword_after"]
        lines += [
            "", "### 검색어 모델 적재 정보 (질문 직후)", "",
            f"- 요청 모델: `{record['keyword_model']}`",
            f"- 질문 시작 전: {initial_state(record['keyword_before'])}",
            f"- VRAM (질문 직후, 검색어 모델): {display(vram_metric(keyword_after), ' MiB')}",
            f"- 관측 모델 태그: {snapshot_value(keyword_after, 'model')}",
            f"- digest: {snapshot_value(keyword_after, 'digest')}",
            f"- details.quantization_level: {snapshot_value(keyword_after, 'quantization_level')}",
            f"- 실제 context_length: {snapshot_value(keyword_after, 'context_length')}",
            f"- CPU/GPU 적재 상태: {processor_state(keyword_after)}",
            f"- 적재 정보 관측 시각 (UTC): {keyword_after['observed_at']}",
        ]
    lines += [
        "", "### 모델 호출별 성능", "",
        "| 단계 | 요청 모델 | 호출 결과 | 호출 전체 시간 | 모델 로딩 시간 | 토큰 생성 속도 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for call in record["calls"]:
        missing = metric(reason="호출 실패로 응답 통계 없음")
        state = "성공" if call["success"] else "실패: " + call["error"]
        lines.append(
            f"| {call['stage']} | {table_cell(call['model'])} | {state} | {call['elapsed']:.2f}초 | "
            f"{display(call.get('load_seconds', missing), '초')} | "
            f"{display(call.get('tokens_per_second', missing), ' tokens/s')} |"
        )
    if len(record["calls"]) < 2:
        lines += ["", "최종 답변 모델 호출은 실행되지 않았습니다. 안내문을 모델 응답으로 집계하지 않습니다."]
    for call in record["calls"]:
        lines += ["", f"#### {call['stage']} — 요청 설정·원본 응답", "",
                  fenced(json.dumps({"model": call["model"], "provider": call.get("provider"),
                                     "settings": call["settings"], "response": call.get("response"),
                                     "error": call.get("error")}, ensure_ascii=False, indent=2), "json")]
    lines += ["", render_retrieval(record.get("retrieval"))]
    return "\n".join(lines) + "\n---\n\n"


def render_summary(records, planned, experiment_kind):
    lines = ["## 집계", "", f"- 실험 구분: {EXPERIMENT_KINDS[experiment_kind]}",
             f"- 기록된 질문 시도: {len(records)} / 예정 {planned}",
             "- 호출 성공은 API 정상 반환입니다. 원본 내용의 정확성·완결성은 별도 평가 대상입니다.",
             "- 평균은 각 항목의 유효한 값만 사용하며, tokens/s는 응답별 속도의 산술평균입니다.", ""]
    # 키워드 모델이 다른 실험의 전체 시간을 같은 답변 모델 통계로 섞지 않습니다.
    groups = defaultdict(list)
    for record in records:
        groups[(record["model"], record.get("keyword_model", record["model"]))].append(record)
    for (model, keyword_model), rows in groups.items():
        calls = [call for row in rows for call in row["calls"]]
        success = [call for call in calls if call["success"]]
        # 질문 전체 시간은 완료된 답변끼리 비교합니다. 실패·중단·근거 없음 안내는 아래에 따로 나열합니다.
        answers = [row for row in rows if row["status"] == "답변 완료"]
        lines += [f"### 최종 답변: {model} / 검색어 생성: {keyword_model}", "",
                  f"- 모델 호출 성공 수 / 전체 시도 수: {len(success)} / {len(calls)} (두 단계 합계)",
                  f"- 최종 모델 답변 완료: {len(answers)} / 질문 시도 {len(rows)}", "",
                  "| 요청 모델 | 호출 성공 수 / 전체 시도 수 |", "|---|---:|"]
        for tag in dict.fromkeys((keyword_model, model)):
            model_calls = [call for call in calls if call["model"] == tag]
            lines.append(f"| {table_cell(tag)} | {sum(call['success'] for call in model_calls)} / {len(model_calls)} |")
        lines += ["",
                  "| 지표 | 평균 |", "|---|---:|",
                  f"| 전체 응답 시간 (최종 답변 완료 질문) | {average([row['elapsed'] for row in answers], '초')} |",
                  f"| VRAM (최종 답변 완료 질문 직후) | {average([vram_metric(row['after'])['value'] for row in answers], ' MiB')} |"]
        for state in ("시작 전 미적재", "시작 전 로드됨", "시작 전 상태 미확인"):
            lines.append(f"| 전체 응답 시간 — {state} | {average([row['elapsed'] for row in answers if initial_state(row['before']) == state], '초')} |")
        for stage in ("검색어 생성", "최종 답변"):
            # 단계별 통계는 API가 반환한 호출 기준입니다. 최종 답변 완료 질문 수와 분모가 다릅니다.
            stage_calls = [call for call in calls if call["stage"] == stage]
            returned = [call for call in stage_calls if call["success"]]
            tag = keyword_model if stage == "검색어 생성" else model
            lines += ["", f"#### {stage}: 호출 성공 {len(returned)} / 시도 {len(stage_calls)}", "",
                      f"- 요청 모델: `{tag}`", "",
                      "| 지표 | 평균 |", "|---|---:|",
                      f"| 호출 전체 시간 (성공) | {average([call['elapsed'] for call in returned], '초')} |",
                      f"| 모델 로딩 시간 | {average([call['load_seconds']['value'] for call in returned], '초')} |",
                      f"| 토큰 생성 속도 | {average([call['tokens_per_second']['value'] for call in returned], ' tokens/s')} |",
                      f"| 호출 실패까지 걸린 시간 | {average([call['elapsed'] for call in stage_calls if not call['success']], '초')} |"]
            if stage == "검색어 생성" and any("keyword_after" in row for row in rows):
                keyword_vram = [vram_metric(row["keyword_after"])["value"] for row in rows
                                if "keyword_after" in row and any(
                                    call["stage"] == stage and call["success"] for call in row["calls"])]
                lines.append(f"| VRAM (검색어 호출 성공 질문 직후, 검색어 모델) | {average(keyword_vram, ' MiB')} |")
            cloud = [call for call in returned if call.get("provider") == "openai_responses"]
            if any(call.get("provider") == "openai_responses" for call in stage_calls):
                usages = [field(call["response"]["openai_response"], "usage") for call in cloud]
                for label, values in (
                    ("입력 토큰", [field(usage, "input_tokens") for usage in usages]),
                    ("출력 토큰 (추론 포함)", [field(usage, "output_tokens") for usage in usages]),
                    ("추론 토큰", [field(field(usage, "output_tokens_details"), "reasoning_tokens") for usage in usages]),
                    ("캐시 입력 토큰", [field(field(usage, "input_tokens_details"), "cached_tokens") for usage in usages]),
                ):
                    lines.append(f"| {label} | {average(values, ' tokens')} |")
        other_rows = [row for row in rows if row["status"] != "답변 완료"]
        if other_rows:
            lines += ["", "성공 답변 평균에서 제외한 질문:", ""]
            lines += [f"- Q{row['number']:02d}: {row['status']} / {row['elapsed']:.2f}초" for row in other_rows]
    return "\n".join(lines) + "\n"
