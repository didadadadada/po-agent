import os
import re
import httpx
from notion_client import Client as NotionClient

# ── Tavily 웹 검색 ────────────────────────────────────────────────────────────

async def tavily_search(query: str, max_results: int = 5) -> str:
    """Tavily API로 웹 검색 후 결과 텍스트 반환"""
    api_key = os.environ["TAVILY_API_KEY"]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    lines = []
    if data.get("answer"):
        lines.append(f"요약 답변: {data['answer']}\n")
    for i, r in enumerate(data.get("results", []), 1):
        lines.append(f"{i}. [{r['title']}]({r['url']})\n{r.get('content', '')[:300]}")
    return "\n\n".join(lines) if lines else "검색 결과 없음"


# ── 마크다운 → Notion 블록 변환 ────────────────────────────────────────────────

def _parse_inline(text: str) -> list[dict]:
    """**bold** 구문을 bold annotation으로 변환, 나머지는 plain text"""
    parts = re.split(r'\*\*(.+?)\*\*', text)
    rich = []
    for i, part in enumerate(parts):
        if not part:
            continue
        rich.append({
            "type": "text",
            "text": {"content": part[:2000]},
            "annotations": {"bold": bool(i % 2)},
        })
    return rich or [{"type": "text", "text": {"content": ""}}]


def markdown_to_notion_blocks(markdown: str) -> list[dict]:
    """마크다운 텍스트를 Notion API 블록 리스트로 변환 (최대 100개)"""
    blocks: list[dict] = []
    for line in markdown.split("\n"):
        s = line.rstrip()

        # divider
        if s in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue

        # heading_3 (### 먼저 확인)
        if s.startswith("### "):
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": _parse_inline(s[4:])},
            })
            continue

        # heading_2
        if s.startswith("## "):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _parse_inline(s[3:])},
            })
            continue

        # heading_1
        if s.startswith("# "):
            blocks.append({
                "object": "block", "type": "heading_1",
                "heading_1": {"rich_text": _parse_inline(s[2:])},
            })
            continue

        # bulleted_list_item  (- 또는 *)
        if re.match(r'^[-*] ', s):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_inline(s[2:])},
            })
            continue

        # numbered_list_item  (1. 2. ...)
        m = re.match(r'^\d+\.\s+(.*)', s)
        if m:
            blocks.append({
                "object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _parse_inline(m.group(1))},
            })
            continue

        # 빈 줄 스킵
        if not s:
            continue

        # paragraph
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _parse_inline(s)},
        })

    return blocks[:100]  # Notion API 한 번에 최대 100개


# ── Notion 헬퍼 ───────────────────────────────────────────────────────────────

def _notion() -> NotionClient:
    return NotionClient(auth=os.environ["NOTION_TOKEN"])


def notion_read_page(page_id: str) -> str:
    """Notion 페이지 블록 내용을 텍스트로 반환"""
    notion = _notion()
    blocks = notion.blocks.children.list(block_id=page_id)
    lines = []
    for block in blocks.get("results", []):
        btype = block.get("type", "")
        rich = block.get(btype, {}).get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich)
        if text:
            lines.append(text)
    return "\n".join(lines) if lines else "(빈 페이지)"


def notion_write_page(title: str, content: str, parent_page_id: str | None = None) -> str:
    """Notion에 새 페이지를 생성하고 URL 반환"""
    notion = _notion()

    # parent 설정
    if parent_page_id:
        parent = {"type": "page_id", "page_id": parent_page_id}
    else:
        # 환경변수로 기본 부모 페이지 지정 가능
        default_parent = os.environ.get("NOTION_PARENT_PAGE_ID")
        if default_parent:
            parent = {"type": "page_id", "page_id": default_parent}
        else:
            raise ValueError(
                "Notion 저장 위치를 알 수 없습니다. "
                "parent_page_id 인자를 넘기거나 NOTION_PARENT_PAGE_ID 환경변수를 설정하세요."
            )

    children = markdown_to_notion_blocks(content)

    page = notion.pages.create(
        parent=parent,
        properties={
            "title": {"title": [{"type": "text", "text": {"content": title}}]}
        },
        children=children,
    )
    url = page.get("url", "")
    return f"Notion 페이지 저장 완료: {url}"


def notion_append_to_page(page_id: str, content: str) -> str:
    """기존 Notion 페이지에 내용 추가"""
    notion = _notion()
    children = markdown_to_notion_blocks(content)
    notion.blocks.children.append(block_id=page_id, children=children)
    return f"페이지 {page_id}에 내용 추가 완료"


# ── Claude tool 스키마 정의 ───────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": (
            "Tavily API를 통해 웹 검색을 수행합니다. "
            "최신 정보, 시장 트렌드, 경쟁사 분석 등이 필요할 때 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색할 쿼리"},
                "max_results": {
                    "type": "integer",
                    "description": "반환할 결과 수 (기본 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "notion_read_page",
        "description": "Notion 페이지의 내용을 읽어옵니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "읽을 Notion 페이지 ID (URL의 마지막 32자리)",
                }
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "notion_write_page",
        "description": (
            "새 Notion 페이지를 생성하고 내용을 저장합니다. "
            "PRD, 유저 스토리 등 문서를 저장할 때 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "페이지 제목"},
                "content": {"type": "string", "description": "저장할 내용 (마크다운 텍스트)"},
                "parent_page_id": {
                    "type": "string",
                    "description": "부모 페이지 ID (없으면 환경변수 NOTION_PARENT_PAGE_ID 사용)",
                },
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "notion_append_to_page",
        "description": "기존 Notion 페이지에 내용을 추가합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "내용을 추가할 페이지 ID"},
                "content": {"type": "string", "description": "추가할 텍스트"},
            },
            "required": ["page_id", "content"],
        },
    },
]


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    """도구 이름과 입력을 받아 실행 후 결과 문자열 반환"""
    try:
        if tool_name == "web_search":
            return await tavily_search(
                query=tool_input["query"],
                max_results=tool_input.get("max_results", 5),
            )
        elif tool_name == "notion_read_page":
            return notion_read_page(tool_input["page_id"])
        elif tool_name == "notion_write_page":
            return notion_write_page(
                title=tool_input["title"],
                content=tool_input["content"],
                parent_page_id=tool_input.get("parent_page_id"),
            )
        elif tool_name == "notion_append_to_page":
            return notion_append_to_page(
                page_id=tool_input["page_id"],
                content=tool_input["content"],
            )
        else:
            return f"알 수 없는 도구: {tool_name}"
    except Exception as e:
        return f"[도구 오류] {tool_name}: {e}"
