import hashlib
import hmac
import json
import os
import time
from collections import OrderedDict
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


# ── PO 에이전트 시스템 프롬프트 ───────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 경험 많은 PO 에이전트입니다. 사용자의 요청에 따라 PRD, 유저 스토리를 작성하고,
필요하면 웹 리서치를 먼저 진행한 뒤 근거 있는 문서를 작성합니다.
작성한 문서는 Notion에 저장할 수 있습니다.

도구 사용 지침:
- 최신 시장 정보나 경쟁사 분석이 필요하면 web_search를 먼저 사용하세요.
- 문서 작성이 완료되면 사용자가 원할 경우 notion_write_page로 저장하세요.
- Notion 페이지 내용을 참고해야 할 때는 notion_read_page를 사용하세요.
- 응답은 항상 한국어로 작성합니다."""


# ── Claude agentic loop ──────────────────────────────────────────────────────

async def run_po_agent(user_message: str) -> str:
    """Claude tool use 루프를 돌려 최종 응답 반환"""
    messages = [{"role": "user", "content": user_message}]

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
            return "\n".join(text_blocks)

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
            return "\n".join(text_blocks) or "(응답 없음)"


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

    # Claude 에이전트 실행
    try:
        answer = await run_po_agent(user_message)
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
