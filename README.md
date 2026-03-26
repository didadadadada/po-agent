# Slack PO Agent

Slack에서 @멘션하면 Claude가 PRD·유저 스토리를 작성하고, 웹 리서치 후 Notion에 저장하는 PO 에이전트입니다.

## 기술 스택

- **Python 3.11+**
- **FastAPI** — Slack 이벤트 수신 서버
- **Anthropic SDK** — Claude claude-opus-4-6 + tool use
- **Tavily API** — 웹 검색
- **Notion SDK** — 페이지 읽기/쓰기
- **Slack SDK** — 메시지 전송

---

## 환경변수 설정

`.env.example` 을 복사해 `.env` 파일을 만드세요.

```bash
cp .env.example .env
```

| 변수 | 설명 | 발급처 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Anthropic API 키 | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `SLACK_BOT_TOKEN` | Slack 봇 토큰 (`xoxb-` 시작) | [api.slack.com/apps](https://api.slack.com/apps) → 앱 → OAuth & Permissions |
| `SLACK_SIGNING_SECRET` | Slack 서명 시크릿 | [api.slack.com/apps](https://api.slack.com/apps) → 앱 → Basic Information → App Credentials |
| `TAVILY_API_KEY` | Tavily 검색 API 키 | [tavily.com](https://tavily.com) → Dashboard |
| `NOTION_TOKEN` | Notion 통합 토큰 (`secret_` 시작) | [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `NOTION_PARENT_PAGE_ID` | 문서를 저장할 Notion 페이지 ID | 페이지 URL 마지막 32자리 |

### Notion 연결 방법

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration** 생성
2. **Internal Integration Token** 복사 → `NOTION_TOKEN`에 입력
3. 문서를 저장할 Notion 페이지 열기 → 우측 상단 `···` → **Connections** → 방금 만든 Integration 연결
4. 해당 페이지 URL의 마지막 32자리 → `NOTION_PARENT_PAGE_ID`에 입력

---

## 로컬 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 서버 실행
uvicorn main:app --reload --port 8000
```

### ngrok으로 외부 노출 (Slack 이벤트 수신용)

```bash
ngrok http 8000
# → https://xxxx.ngrok.io 가 생성됨
```

Slack App 설정 → **Event Subscriptions** → Request URL:
```
https://xxxx.ngrok.io/slack/events
```

---

## Slack App 설정

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. **OAuth & Permissions** → Bot Token Scopes 추가:
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`
   - `groups:history`
3. **Event Subscriptions** 활성화 → Request URL 입력 → **Subscribe to bot events**:
   - `app_mention`
4. **Install to Workspace** → Bot User OAuth Token 복사 → `SLACK_BOT_TOKEN`에 입력

---

## Railway 배포

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. **Variables** 탭에서 `.env` 내용 모두 입력
3. 배포 완료 후 생성된 도메인을 Slack Request URL로 등록:
   ```
   https://your-app.railway.app/slack/events
   ```

---

## 사용 예시

Slack 채널에서 봇을 멘션하세요:

```
@PO에이전트 AI 기반 일정 관리 앱의 PRD를 작성해줘

@PO에이전트 경쟁사 분석 후 유저 스토리 5개 작성하고 Notion에 저장해줘

@PO에이전트 https://notion.so/.../abc123 페이지 내용을 요약해줘
```

---

## 파일 구조

```
po-agent/
├── main.py          # FastAPI 서버, Slack 이벤트 처리, Claude 루프
├── tools.py         # Tavily·Notion 도구 함수 + Claude tool 스키마
├── requirements.txt
├── .env.example     # 환경변수 템플릿
├── Procfile         # Railway 배포 설정
└── README.md
```
