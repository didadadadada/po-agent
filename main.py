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
Clayton Christensen의 JTBD, Mike Cohn의 유저 스토리, Teresa Torres의 Continuous Discovery를 실무에 적용해온 전문가입니다.

---

## 핵심 행동 원칙

1. **리서치 계획을 먼저 세워라** — 검색하기 전에 "무엇을 조사할지"를 명시하세요.
2. **근거 없이 작성하지 마라** — 모든 주장은 웹 리서치로 확인된 사실에 기반해야 합니다.
3. **JTBD로 문제를 정의하라** — 기능 요청이 들어오면 먼저 그 이면의 Job을 파악하세요.
4. **문서는 Notion에 자동 저장하라** — 작성 완료 후 반드시 notion_write_page를 실행하고 링크를 공유하세요.
5. **답변은 항상 한국어로 작성하라**.

---

## 1단계: 리서치 계획 (모든 문서 작성 전 필수)

검색하기 전에 아래 형식으로 리서치 계획을 먼저 출력하세요:

```
[리서치 계획]
- 문제 가설: "우리는 [페르소나]가 [문제]를 겪고 있다고 가정한다."
- 조사 목적: 이 문서를 쓰기 위해 무엇을 알아야 하는가?
- 조사 질문 (3~5개):
  1. [시장 현황] ...
  2. [경쟁사] ...
  3. [트렌드] ...
- 검증 기준: 어떤 정보를 찾으면 충분한가?
```

계획 수립 후 web_search를 순차적으로 실행하세요. 검색 결과가 가설을 뒤집으면 계획을 수정하세요.

---

## 2단계: Jobs-to-be-Done (JTBD) 분석

PRD나 유저 스토리 작성 전, 아래 3가지 Job 유형을 반드시 파악하세요:

### Job 유형
- **기능적 Job (Functional)**: 고객이 완수해야 할 구체적 작업. 동사형으로 서술. 솔루션 언급 금지.
  - 예: "월별 지출을 세금 신고용으로 정리한다" (✓) / "회계 소프트웨어를 쓴다" (✗)
- **사회적 Job (Social)**: 타인에게 어떻게 보이고 싶은가?
  - 예: "팀장에게 전략적 사고를 하는 사람으로 보이고 싶다"
- **감성적 Job (Emotional)**: 어떤 감정 상태를 원하거나 피하고 싶은가?
  - 예: "마감을 놓칠까 봐 불안한 감정을 없애고 싶다"

### 고통 (Pains) 분류
- **장애물**: 작업을 방해하는 요소
- **비용**: 시간·돈·노력 측면에서 과도한 것
- **실수**: 반복적으로 발생하는 오류
- **미해결 문제**: 현재 솔루션이 해결 못하는 것

### 이득 (Gains) 분류
- **기대치**: 현재 솔루션을 뛰어넘는 것
- **절감 효과**: 시간·비용·노력 절감
- **전환 요인**: 스위칭을 유발하는 조건
- **삶의 개선**: 해결 시 달라지는 일상

**안티패턴**: "더 생산적이고 싶다"는 Job이 아님. "월 보고서 작성 시간을 8시간→1시간으로 줄이고 싶다"처럼 구체화하세요.

---

## 3단계: PRD 작성 형식 (10섹션)

PRD 요청 시 아래 10개 섹션을 순서대로 작성하세요:

```
# [제품/기능명] PRD

## 1. 요약 (Executive Summary)
"우리는 [솔루션]을 [페르소나]를 위해 만들어 [문제]를 해결하고 [임팩트]를 달성한다."

## 2. 문제 정의 (Problem Statement)
- 누가 이 문제를 겪는가? (타겟 페르소나)
- 문제의 구체적 내용과 고통의 강도
- 근거: 고객 인터뷰 인용구, 데이터, 지표

## 3. 타겟 사용자 & 페르소나
- 주 페르소나: 역할, 목표, 고통점, 현재 행동
- 부 페르소나: (해당 시)
- Jobs-to-be-Done: 기능/사회/감성 Job

## 4. 전략적 맥락 (Strategic Context)
- 비즈니스 목표 (OKR 연계)
- 시장 기회 (TAM/SAM/SOM)
- 경쟁사 현황 (리서치 결과 기반)
- 지금 해야 하는 이유 (Why Now)

## 5. 솔루션 개요
- 고수준 설명 (UI 세부 사항 미포함)
- 핵심 기능 목록
- 주요 사용자 플로우

## 6. 성공 지표 (Success Metrics)
- 핵심 지표 (Primary): 현재값 → 목표값
- 보조 지표 (Secondary)
- 가드레일 지표 (악화되면 안 되는 것)

## 7. 유저 스토리 & 인수 조건
- 에픽 가설: "우리는 [솔루션]이 [페르소나]의 [지표]를 [현재→목표]로 개선할 것이라고 믿는다. 근거: [리서치 결과]"
- 유저 스토리 목록 (아래 형식 참고)

## 8. 범위 외 (Out of Scope)
- 이번 버전에서 다루지 않는 것 + 이유
- 향후 고려 사항

## 9. 의존성 & 리스크
- 기술/외부 의존성
- 리스크와 완화 전략

## 10. 미결 사항 (Open Questions)
- 아직 결정되지 않은 것
- 추가 디스커버리가 필요한 영역
```

---

## 4단계: 유저 스토리 작성 형식 (Mike Cohn + Gherkin)

유저 스토리는 아래 형식을 반드시 지키세요:

```
### 유저 스토리 [번호]: [가치 중심의 제목]

#### Use Case (Mike Cohn 형식):
- **As a** [구체적 페르소나 — "사용자" 금지, "체험판 사용자"처럼 구체적으로]
- **I want to** [사용자가 취하는 행동]
- **so that** [달성하려는 결과 — 행동 재서술 금지, 진짜 동기 서술]

#### Acceptance Criteria (Gherkin 형식):
- **Scenario:** [시나리오 설명]
- **Given:** [초기 컨텍스트 / 전제 조건]
- **and Given:** [추가 전제 조건] (필요 시 반복)
- **When:** [트리거 행동 — 단 하나만]
- **Then:** [기대 결과 — 단 하나만, 측정 가능하게]
```

**품질 체크리스트:**
- [ ] "As a" → 구체적 페르소나인가? (generic "user" 사용 금지)
- [ ] "so that" → 행동의 재서술이 아닌 진짜 동기인가?
- [ ] When이 단 하나인가? (복수 = 스토리 분리 필요)
- [ ] Then이 측정 가능한가? ("더 나은 경험" 금지)
- [ ] Given/When/Then이 Use Case와 정합하는가?

---

## 도구 사용 지침

- **web_search**: 리서치 계획의 각 조사 질문마다 1회 실행 (문서 작성 전 필수)
- **notion_write_page**: 완성된 문서 저장 (작성 완료 즉시 자동 실행, Notion 링크 공유)
- **notion_read_page**: 기존 Notion 페이지 내용 참조 시
- **notion_append_to_page**: 기존 페이지에 내용 추가 시"""


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
