import hashlib
import hmac
import json
import os
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from tools import TOOL_DEFINITIONS, execute_tool

load_dotenv()

# ── 환경변수 ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# ── 클라이언트 초기화 ─────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# ── 이벤트 중복 처리 방지 캐시 ────────────────────────────────────────────────
# 최근 500개 event_id를 LRU 방식으로 기억
_processed_events: OrderedDict[str, float] = OrderedDict()
_MAX_CACHE = 500
_EVENT_TTL = 300  # 5분


def _is_duplicate_event(event_id: str) -> bool:
    now = time.time()
    # TTL 만료 항목 정리
    stale = [k for k, v in _processed_events.items() if now - v > _EVENT_TTL]
    for k in stale:
        _processed_events.pop(k, None)

    if event_id in _processed_events:
        return True

    _processed_events[event_id] = now
    if len(_processed_events) > _MAX_CACHE:
        _processed_events.popitem(last=False)
    return False


# ── Slack 서명 검증 ───────────────────────────────────────────────────────────

def _verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    if abs(time.time() - float(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{request_body.decode()}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── 채널별 대화 히스토리 ──────────────────────────────────────────────────────
# key: channel_id, value: deque of {"role": ..., "content": ...}
# 채널별 최근 10턴(user+assistant 쌍)을 메모리에 유지
_HISTORY_MAX_TURNS = 10
_channel_history: dict[str, deque] = {}


def _get_history(channel: str) -> list[dict]:
    """채널 히스토리를 리스트로 반환 (없으면 빈 리스트)"""
    return list(_channel_history.get(channel, []))


def _append_history(channel: str, user_msg: str, assistant_msg: str) -> None:
    """user/assistant 한 쌍을 히스토리에 추가, 최대 턴 수 초과 시 오래된 것 제거"""
    if channel not in _channel_history:
        _channel_history[channel] = deque()
    history = _channel_history[channel]
    history.append({"role": "user", "content": user_msg})
    history.append({"role": "assistant", "content": assistant_msg})
    # 최대 턴 수(user+assistant 쌍) 초과 시 앞에서 2개씩 제거
    while len(history) > _HISTORY_MAX_TURNS * 2:
        history.popleft()
        history.popleft()


# ── PO 에이전트 시스템 프롬프트 ───────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 10년 경력의 시니어 프로덕트 오너(PO)입니다.

## 행동 원칙
- 요청을 받으면 반드시 web_search 도구로 시장 현황, 경쟁사, 트렌드를 먼저 조사하세요.
- 리서치 없이 문서를 작성하지 마세요. 모든 주장은 조사 근거에 기반해야 합니다.
- 문서 작성이 완료되면 반드시 notion_write_page 도구로 Notion에 자동 저장하세요.
- 저장 후 Notion 링크를 답변에 포함하세요.
- 답변은 항상 한국어로 작성하세요.

## PRD 작성 형식
PRD 요청 시 아래 순서를 반드시 지키세요:
1. **배경**: 이 기능/제품이 필요한 배경과 맥락
2. **문제 정의**: 해결하려는 핵심 문제와 타겟 사용자
3. **핵심 기능**: 구현할 기능 목록과 상세 설명
4. **성공 지표**: 성공을 측정할 정량적 KPI/OKR
5. **범위 외(Out of Scope)**: 이번 버전에서 다루지 않는 것

## 유저 스토리 작성 형식
유저 스토리 요청 시 아래 형식을 반드시 지키세요:
- 형식: "[사용자 유형]로서, [목적/이유]를 위해, [행동/기능]을 하고 싶다."
- 각 스토리에 인수 조건(Acceptance Criteria)을 Given/When/Then 형식으로 작성하세요.

## 도구 사용 지침
- web_search: 시장 조사, 경쟁사 분석, 트렌드 파악 시 사용 (문서 작성 전 필수)
- notion_write_page: 완성된 문서 저장 시 사용 (작성 완료 후 자동 실행)
- notion_read_page: 기존 Notion 페이지 내용 참조 시 사용
- notion_append_to_page: 기존 페이지에 내용 추가 시 사용"""


# ── Claude agentic loop ──────────────────────────────────────────────────────

async def run_po_agent(user_message: str, channel: str) -> str:
    """채널 히스토리를 포함한 Claude tool use 루프, 최종 응답 반환"""
    # 이전 대화 히스토리 + 현재 메시지
    messages = _get_history(channel) + [{"role": "user", "content": user_message}]

    while True:
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        # 도구 호출이 없으면 종료
        if response.stop_reason == "end_turn":
            text_blocks = [b.text for b in response.content if b.type == "text"]
            answer = "\n".join(text_blocks)
            # 채널 히스토리에 이번 턴 저장 (user 원본 메시지 + assistant 최종 텍스트)
            _append_history(channel, user_message, answer)
            return answer

        # tool_use 블록 처리
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            # 예상치 못한 stop_reason
            text_blocks = [b.text for b in response.content if b.type == "text"]
            answer = "\n".join(text_blocks) or "(응답 없음)"
            _append_history(channel, user_message, answer)
            return answer


# ── FastAPI 앱 ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("PO Agent 서버 시작")
    yield
    print("PO Agent 서버 종료")


app = FastAPI(title="Slack PO Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request):
    body_bytes = await request.body()
    payload = json.loads(body_bytes)

    # URL 검증 챌린지 — 서명 검증 전에 처리해야 함
    # Slack이 Request URL 등록 시 서명 없이 challenge 요청을 보낼 수 있음
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload["challenge"]})

    # 일반 이벤트는 서명 검증 수행
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body_bytes, timestamp, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = payload.get("event", {})
    event_id = payload.get("event_id", "")

    # 중복 이벤트 무시
    if _is_duplicate_event(event_id):
        return JSONResponse({"ok": True})

    event_type = event.get("type", "")

    # app_mention 이벤트만 처리
    if event_type != "app_mention":
        return JSONResponse({"ok": True})

    # 봇 자신의 메시지 무시
    if event.get("bot_id"):
        return JSONResponse({"ok": True})

    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    raw_text: str = event.get("text", "")

    # <@BOTID> 멘션 부분 제거
    user_message = " ".join(
        w for w in raw_text.split() if not w.startswith("<@")
    ).strip()

    if not user_message:
        user_message = "안녕하세요! 무엇을 도와드릴까요?"

    # 처리 중 메시지 전송
    try:
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass_flowing_sand: 요청을 처리 중입니다...",
        )
    except SlackApiError:
        pass

    # Claude 에이전트 실행 (채널 ID를 넘겨 히스토리 맥락 유지)
    try:
        answer = await run_po_agent(user_message, channel)
    except Exception as e:
        answer = f"오류가 발생했습니다: {e}"

    # 결과 전송 (Slack 메시지 최대 3000자 분할)
    try:
        for chunk in _split_message(answer, 3000):
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=chunk,
                mrkdwn=True,
            )
    except SlackApiError as e:
        print(f"Slack 전송 오류: {e}")

    return JSONResponse({"ok": True})


def _split_message(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
