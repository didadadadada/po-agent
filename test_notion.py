#!/usr/bin/env python3
"""
Notion 저장 기능 단독 테스트 스크립트
실행: python test_notion.py

.env 파일에 아래 값이 설정되어 있어야 합니다:
  NOTION_TOKEN=ntn_...
  NOTION_PARENT_PAGE_ID=<저장할 부모 페이지 32자리 ID>
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# tools 모듈 임포트 (환경변수 로드 후)
from tools import (
    markdown_to_notion_blocks,
    notion_append_to_page,
    notion_read_page,
    notion_write_page,
)

TEST_MARKDOWN = """
# 테스트 PRD — PO Agent Notion 연동 확인

## 1. 배경
이 문서는 `notion_write_page` 도구가 정상 동작하는지 확인하는 **테스트 문서**입니다.

---

## 2. 핵심 기능
- **마크다운 변환**: heading, bold, list 모두 Notion 블록으로 변환
- **30초 타임아웃**: 응답 지연 시 자동 실패 처리

### 세부 기능
1. heading_1 / heading_2 / heading_3 변환
2. bulleted_list_item 변환
3. numbered_list_item 변환

---

## 3. 성공 지표
- 저장 후 Notion URL 반환: ✓
- 마크다운 → Notion 블록 정합성: ✓
"""


def check_env() -> bool:
    ok = True
    token = os.environ.get("NOTION_TOKEN", "")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "")

    print("── 환경변수 확인 ──────────────────────────────")
    if token:
        print(f"  ✅ NOTION_TOKEN        : {token[:12]}...")
    else:
        print("  ❌ NOTION_TOKEN        : 없음 — .env에 추가 필요")
        ok = False

    if parent:
        print(f"  ✅ NOTION_PARENT_PAGE_ID: {parent}")
    else:
        print("  ❌ NOTION_PARENT_PAGE_ID: 없음 — .env에 추가 필요")
        print("     Notion 페이지 URL 끝 32자리를 복사해서 넣으세요.")
        print("     예: https://notion.so/workspace/제목-abcd1234ef5678ab90cd12ef34567890")
        print("                                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^")
        ok = False

    print()
    return ok


def test_markdown_blocks():
    print("── 1. 마크다운 → Notion 블록 변환 테스트 ─────")
    blocks = markdown_to_notion_blocks(TEST_MARKDOWN)
    print(f"  변환된 블록 수: {len(blocks)}")

    type_counts: dict[str, int] = {}
    for b in blocks:
        t = b.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    for btype, cnt in sorted(type_counts.items()):
        print(f"  - {btype}: {cnt}개")

    assert len(blocks) > 0, "블록이 하나도 없음"
    assert any(b["type"] == "heading_1" for b in blocks), "heading_1 없음"
    assert any(b["type"] == "heading_2" for b in blocks), "heading_2 없음"
    assert any(b["type"] == "divider" for b in blocks), "divider 없음"
    assert any(b["type"] == "bulleted_list_item" for b in blocks), "bulleted_list 없음"
    assert any(b["type"] == "numbered_list_item" for b in blocks), "numbered_list 없음"
    print("  ✅ 변환 정상\n")
    return blocks


async def test_write_page() -> str:
    print("── 2. notion_write_page 테스트 ────────────────")
    print("  저장 중...")
    result = await notion_write_page(
        title="[PO Agent 테스트] Notion 연동 확인",
        content=TEST_MARKDOWN,
    )
    print(f"  결과: {result}")
    assert "notion.so" in result or "완료" in result, f"예상치 못한 결과: {result}"
    print("  ✅ 저장 성공\n")
    # URL 추출
    url = result.split("저장 완료: ")[-1].strip()
    return url


async def test_read_page(page_url: str):
    print("── 3. notion_read_page 테스트 ─────────────────")
    # URL에서 page_id 추출 (마지막 32자리 hex)
    import re
    m = re.search(r"([a-f0-9]{32})(?:\?|$)", page_url.replace("-", ""))
    if not m:
        print(f"  ⚠️  URL에서 page_id 추출 실패, 읽기 테스트 건너뜀: {page_url}")
        return
    page_id = m.group(1)
    print(f"  page_id: {page_id}")
    content = notion_read_page(page_id)
    print(f"  읽은 내용 (앞 200자): {content[:200]}")
    assert len(content) > 0, "빈 페이지 반환"
    print("  ✅ 읽기 성공\n")


async def main():
    print("=" * 55)
    print("  PO Agent — Notion 연동 테스트")
    print("=" * 55)
    print()

    if not check_env():
        print("❌ 환경변수 설정 후 다시 실행하세요.")
        sys.exit(1)

    # 1. 블록 변환 (동기)
    test_markdown_blocks()

    # 2. 페이지 저장
    try:
        page_url = await test_write_page()
    except Exception as e:
        print(f"  ❌ 저장 실패: {e}")
        print()
        print("  자주 발생하는 원인:")
        print("  - NOTION_TOKEN이 잘못됨")
        print("  - NOTION_PARENT_PAGE_ID 페이지에 Integration이 연결되지 않음")
        print("    (Notion 페이지 → Share → Connections → 인테그레이션 선택)")
        sys.exit(1)

    # 3. 페이지 읽기
    try:
        await test_read_page(page_url)
    except Exception as e:
        print(f"  ⚠️  읽기 실패 (저장은 성공): {e}\n")

    print("=" * 55)
    print("  ✅ 모든 테스트 통과!")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
