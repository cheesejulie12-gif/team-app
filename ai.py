"""오픈라우터 AI 배정: 1차, 2차, 3차 모델을 차례로 부름.
모델마다 부른 때부터 응답을 다 받을 때까지 전체 120초를 재서, 넘으면 끊고 다음 모델로 넘어감."""

import json
import re
import threading
import time

import requests

from logic import ROLES

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_TIMEOUT = 120  # 모델 하나에 주는 전체 대기 시간(초)

# 1차, 2차, 3차 모델. 무료 모델 이름은 자주 바뀌므로 https://openrouter.ai/models?q=free 에서 확인해 고침.
# 환경 변수 AI_MODELS="모델1,모델2,모델3" 으로 바꿀 수도 있음.
DEFAULT_MODELS = [
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
]


def _student_line(s):
    subs = ", ".join(s.get("sub_roles", [])) or "없음"
    return f'- 학번 {s["student_no"]}, 이름 {s["name"]}, 주 역할 {s["main_role"]}, 보조 역할 {subs}'


def build_prompt(teams, unassigned):
    lines = [
        "너는 대학 수업의 팀 편성 도우미임. 팀이 없는 학생을 팀에 넣는 배정안을 만들어야 함.",
        "",
        "## 규칙",
        "1. 팀이 없는 학생은 모두 정확히 한 팀에 배정함. 이미 팀에 있는 학생은 옮기지 않음.",
        "2. 배정 뒤 모든 팀은 2~4명이어야 함(빈 팀 없음).",
        "3. 한 팀 안에서 주 역할이 같은 학생이 2명 이상이면 안 됨.",
        "4. 모든 팀에는 기획자를 맡을 학생과 웹 프로그래머를 맡을 학생이 서로 다른 사람으로 1명씩 있어야 함. "
        "주 역할이나 보조 역할 어느 쪽으로든 맡을 수 있음. 한 학생이 둘 다 할 수 있어도 한 역할만 맡음.",
        f"역할 목록: {', '.join(ROLES)}",
        "",
        "## 지금 팀 구성",
    ]
    for t in sorted(teams):
        lines.append(f"팀{t} ({len(teams[t])}명):")
        if teams[t]:
            lines.extend(_student_line(s) for s in teams[t])
        else:
            lines.append("- (팀원 없음)")
    lines += ["", "## 팀이 없는 학생"]
    lines.extend(_student_line(s) for s in unassigned)
    lines += [
        "",
        "## 답 형식",
        "설명 없이 아래 꼴의 JSON 하나만 답함. team 은 팀 번호(정수)임.",
        '{"assignments": [{"student_no": "2026001", "team": 1}, {"student_no": "2026002", "team": 2}]}',
    ]
    return "\n".join(lines)


def parse_reply(text):
    """AI 응답에서 [(student_no, 팀번호), ...] 를 뽑음. 실패하면 ValueError"""
    if not text or not text.strip():
        raise ValueError("빈 응답")
    t = re.sub(r"```(?:json)?", "", text)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("응답에 JSON이 없음")
    data = json.loads(t[start : end + 1])
    items = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("assignments 목록이 없음")
    out = []
    for it in items:
        no = str(it["student_no"]).strip()
        team = it["team"]
        if isinstance(team, str):
            team = int(re.sub(r"\D", "", team))
        out.append((no, int(team)))
    return out


def _stream_call(api_key, model, prompt, deadline, cancel, box):
    """다른 스레드에서 실행. 스트리밍으로 받으며 매 조각마다 전체 마감 시각을 확인함."""
    try:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8000,
            "reasoning": {"effort": "low"},
            "temperature": 0.2,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        parts = []
        with requests.post(
            OPENROUTER_URL, json=body, headers=headers, stream=True, timeout=(10, 30)
        ) as r:
            if r.status_code != 200:
                msg = r.text[:200].replace("\n", " ")
                box["error"] = f"HTTP {r.status_code}: {msg}"
                return
            for raw in r.iter_lines(decode_unicode=False):
                if cancel.is_set() or time.monotonic() > deadline:
                    box["error"] = f"{MODEL_TIMEOUT}초 안에 응답이 끝나지 않음"
                    return
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue  # ": OPENROUTER PROCESSING" 같은 주석 줄
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk:
                    box["error"] = f"오류 응답: {str(chunk['error'])[:200]}"
                    return
                for ch in chunk.get("choices", []):
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
        box["text"] = "".join(parts)
    except requests.exceptions.Timeout:
        box["error"] = "연결 또는 응답 시간 초과"
    except Exception as e:  # 네트워크 오류 등
        box["error"] = f"{type(e).__name__}: {str(e)[:200]}"


def call_model(api_key, model, prompt, on_tick=None):
    """모델 하나를 부름. (text, error, elapsed) 를 돌려줌.
    on_tick(elapsed초) 는 1초마다 불려 화면에 진행 상황을 보여 주는 데 씀."""
    start = time.monotonic()
    deadline = start + MODEL_TIMEOUT
    cancel = threading.Event()
    box = {}
    th = threading.Thread(
        target=_stream_call, args=(api_key, model, prompt, deadline, cancel, box), daemon=True
    )
    th.start()
    while th.is_alive():
        elapsed = time.monotonic() - start
        if elapsed >= MODEL_TIMEOUT:
            cancel.set()  # 스레드는 다음 조각에서 스스로 끝남. 기다리지 않고 넘어감
            return None, f"{MODEL_TIMEOUT}초가 지나 끊음", elapsed
        if on_tick:
            on_tick(elapsed)
        th.join(timeout=1.0)
    elapsed = time.monotonic() - start
    if "error" in box:
        return None, box["error"], elapsed
    text = box.get("text", "")
    if not text.strip():
        return None, "빈 응답", elapsed
    return text, None, elapsed
