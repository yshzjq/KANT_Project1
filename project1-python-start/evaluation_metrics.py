"""Ollama 호출 통계와 실행 조건을 수집하고 Markdown 평가 보고서를 만듭니다.

미측정 값은 None과 사유로 보관합니다. ps 조회는 질문의 elapsed 구간 밖에서 합니다.
"""

import json
import math
import re
import time
from collections import defaultdict
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
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def numeric(value, minimum=0, strict=False):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and (value > minimum if strict else value >= minimum)
    )


def metric(value=None, reason=None):
    return {"value": value, "reason": reason}


def response_metrics(response):
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


def running_model(client, model):
    snapshot = {"observed_at": datetime.now(timezone.utc).isoformat(), "loaded": None}
    try:
        models = field(client.ps(), "models")
        if not isinstance(models, (list, tuple)):
            raise ValueError("models 목록 누락")
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

    def __init__(self, client):
        self.client = client
        self.calls = []

    def chat(self, **kwargs):
        record = {
            "stage": "검색어 생성" if not self.calls else "최종 답변",
            "model": kwargs.get("model"), "success": False,
            "settings": {key: kwargs[key] for key in ("options", "stream", "format", "think", "keep_alive") if key in kwargs},
        }
        self.calls.append(record)
        started = time.perf_counter()
        try:
            response = self.client.chat(**kwargs)
        except BaseException as error:
            record["error"] = type(error).__name__
            raise
        else:
            record["success"] = True  # API 정상 반환 여부입니다. 내용의 품질 점수가 아닙니다.
            record["response"] = {
                name: field(response, name) for name in (
                    "model", "created_at", "done", "done_reason", "total_duration", "load_duration",
                    "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
                )
            }
            record["response"]["content"] = field(field(response, "message"), "content")
            record["response"]["thinking"] = field(field(response, "message"), "thinking")
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
    values = [value for value in values if numeric(value)]
    return f"{number_text(mean(values))}{unit} (n={len(values)})" if values else "측정 불가 (n=0)"


def fenced(text, language=""):
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


def render_question(record):
    after = record["after"]
    lines = [
        f"## Q{record['number']:02d}", "", "**질문**", "", record["question"], "",
        "**유형**", "", record["type"], "",
        "**기대 결과**", "", record["expected_result"], "",
        "**답변**", "", record["answer"], "", "**응답시간**", "", f"{record['elapsed']:.2f}초", "",
        f"- 처리 결과: {record['status']}",
        f"- 실행 순서: {record['number']}번째 질문 / {initial_state(record['before'])}",
        f"- VRAM (질문 직후): {display(vram_metric(after), ' MiB')}",
        "", "### 실행 조건", "",
        f"- 요청 모델 태그: `{record['model']}`",
        f"- 관측 모델 태그: {snapshot_value(after, 'model')}",
        f"- digest: {snapshot_value(after, 'digest')}",
        f"- details.quantization_level: {snapshot_value(after, 'quantization_level')}",
        f"- 실제 context_length: {snapshot_value(after, 'context_length')}",
        f"- CPU/GPU 적재 상태: {processor_state(after)}",
        f"- 적재 정보 관측 시각 (UTC): {after['observed_at']}",
        "", "### 모델 호출별 성능", "",
        "| 단계 | 호출 결과 | 호출 전체 시간 | 모델 로딩 시간 | 토큰 생성 속도 |",
        "|---|---|---:|---:|---:|",
    ]
    for call in record["calls"]:
        missing = metric(reason="호출 실패로 응답 통계 없음")
        state = "성공" if call["success"] else "실패: " + call["error"]
        lines.append(
            f"| {call['stage']} | {state} | {call['elapsed']:.2f}초 | "
            f"{display(call.get('load_seconds', missing), '초')} | "
            f"{display(call.get('tokens_per_second', missing), ' tokens/s')} |"
        )
    if len(record["calls"]) < 2:
        lines += ["", "최종 답변 모델 호출은 실행되지 않았습니다. 안내문을 모델 응답으로 집계하지 않습니다."]
    for call in record["calls"]:
        lines += ["", f"#### {call['stage']} — 요청 설정·원본 응답", "",
                  fenced(json.dumps({"settings": call["settings"], "response": call.get("response"),
                                     "error": call.get("error")}, ensure_ascii=False, indent=2), "json")]
    return "\n".join(lines) + "\n---\n\n"


def render_summary(records, planned, experiment_kind):
    lines = ["## 집계", "", f"- 실험 구분: {EXPERIMENT_KINDS[experiment_kind]}",
             f"- 기록된 질문 시도: {len(records)} / 예정 {planned}",
             "- 호출 성공은 API 정상 반환입니다. 원본 내용의 정확성·완결성은 별도 평가 대상입니다.",
             "- 평균은 각 항목의 유효한 값만 사용하며, tokens/s는 응답별 속도의 산술평균입니다.", ""]
    groups = defaultdict(list)
    for record in records:
        groups[record["model"]].append(record)
    for model, rows in groups.items():
        calls = [call for row in rows for call in row["calls"]]
        success = [call for call in calls if call["success"]]
        answers = [row for row in rows if row["status"] == "답변 완료"]
        lines += [f"### {model}", "", f"- 모델 호출 성공 수 / 전체 시도 수: {len(success)} / {len(calls)}",
                  f"- 최종 모델 답변 완료: {len(answers)} / 질문 시도 {len(rows)}", "",
                  "| 지표 | 평균 |", "|---|---:|",
                  f"| 전체 응답 시간 (최종 답변 완료 질문) | {average([row['elapsed'] for row in answers], '초')} |",
                  f"| VRAM (최종 답변 완료 질문 직후) | {average([vram_metric(row['after'])['value'] for row in answers], ' MiB')} |"]
        for state in ("시작 전 미적재", "시작 전 로드됨", "시작 전 상태 미확인"):
            lines.append(f"| 전체 응답 시간 — {state} | {average([row['elapsed'] for row in answers if initial_state(row['before']) == state], '초')} |")
        for stage in ("검색어 생성", "최종 답변"):
            stage_calls = [call for call in calls if call["stage"] == stage]
            returned = [call for call in stage_calls if call["success"]]
            lines += ["", f"#### {stage}: 호출 성공 {len(returned)} / 시도 {len(stage_calls)}", "",
                      "| 지표 | 평균 |", "|---|---:|",
                      f"| 호출 전체 시간 (성공) | {average([call['elapsed'] for call in returned], '초')} |",
                      f"| 모델 로딩 시간 | {average([call['load_seconds']['value'] for call in returned], '초')} |",
                      f"| 토큰 생성 속도 | {average([call['tokens_per_second']['value'] for call in returned], ' tokens/s')} |",
                      f"| 호출 실패까지 걸린 시간 | {average([call['elapsed'] for call in stage_calls if not call['success']], '초')} |"]
        other_rows = [row for row in rows if row["status"] != "답변 완료"]
        if other_rows:
            lines += ["", "성공 답변 평균에서 제외한 질문:", ""]
            lines += [f"- Q{row['number']:02d}: {row['status']} / {row['elapsed']:.2f}초" for row in other_rows]
    return "\n".join(lines) + "\n"
