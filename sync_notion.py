# -*- coding: utf-8 -*-
"""Notion 데이터베이스·요약 페이지 관리 유틸.

`run_headless.py`에서 크롤 파이프라인의 끝단으로 호출되어
- 기사 push 시 정규 property 포맷을 만드는 `build_page`
- Notion DB 페이지 전체 조회 `fetch_all_articles`
- 통계 집계 `aggregate_stats`
- 요약 대시보드 페이지 갱신 `update_summary_page`
등을 제공한다.

로컬 SQLite와 사용자 대시보드는 이 모듈과 무관하며 별도 폴더
`crawler_news_local/` 에서만 관리한다.

CLI (요약 페이지만 갱신하고 종료):
  python sync_notion.py --summary-only
"""
import argparse
import datetime
import os
import sys
import time
from collections import Counter

try:
    from notion_client import Client
except ImportError:
    print("notion-client 미설치. `pip install notion-client` 실행 필요")
    sys.exit(1)

import config

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
# 필수 환경변수 — 값이 없으면 사용 지점에서 명확한 에러로 알림:
#   NOTION_TOKEN            — Integration secret (ntn_...)
#   NOTION_DB_ID            — 뉴스 DB ID
#   NOTION_SUMMARY_PAGE_ID  — 요약 대시보드 페이지 ID (Crawler_News 컨테이너 페이지).
#                             코드가 이 페이지의 특정 sentinel heading 아래만 재작성.
NOTION_DB_ID = os.environ.get("NOTION_DB_ID")
NOTION_SUMMARY_PAGE_ID = os.environ.get("NOTION_SUMMARY_PAGE_ID")

KST = datetime.timezone(datetime.timedelta(hours=9))

# 요약 페이지 자동 관리 영역을 두 개의 sentinel로 감싸서 안전하게 재생성.
# 시작 sentinel과 끝 sentinel 사이의 블록만 삭제 대상.
# 두 sentinel 다 있어야만 삭제 실행 (한쪽만 있으면 no-op으로 append).
# 추가 방어: PROTECTED_BLOCK_TYPES는 그 안에 있어도 절대 삭제하지 않는다.
SUMMARY_SENTINEL = "📊 자동 갱신 통계 (아래는 스크립트가 관리)"
SUMMARY_SENTINEL_END = "🔒 자동 갱신 영역 끝 (여기까지 스크립트 관리)"

# 삭제 금지 블록 타입 — 원본 데이터(DB·페이지) 트래시 방지
PROTECTED_BLOCK_TYPES = frozenset({
    "child_database",   # 인라인 DB — 삭제 시 원본 DB가 휴지통으로 감
    "child_page",       # 인라인 페이지 — 삭제 시 원본 페이지가 휴지통으로 감
    "link_to_page",     # 페이지 링크 — 원본은 안 사라지지만 사용자 링크
    "synced_block",     # 동기화 블록
    "table",            # 테이블 (사용자가 만들었을 수 있음)
    "column_list", "column",  # 다단 레이아웃
})

SENTIMENT_MAP = {
    "positive": "👍 긍정",
    "negative": "👎 부정",
    "neutral": "· 중립",
}

# Notion 데이터베이스가 인식하는 키워드 목록 (스키마와 정확히 일치)
ALLOWED_KEYWORDS = {
    "키움증권", "키움증권 김익래", "김익래",
    "정무위", "금융위", "금감원",
    "한국거래소", "넥스트레이드", "금융투자협회",
    "단독증권", "증권", "단독 금융", "금융", "자산운용",
}

# API 요청 사이 지연 (Notion rate limit: 초당 약 3건)
RATE_DELAY = 0.35


def build_page(row):
    """crawl 결과 dict → Notion 페이지 properties (정규 Notion API 포맷).

    각 property는 반드시 해당 type key ('title', 'rich_text', 'select', 'multi_select',
    'date', 'url')로 감싸야 한다. shorthand는 새 API에서 400 에러.
    """
    kws = [k for k in (row["keywords"] or "").split(",") if k in ALLOWED_KEYWORDS]
    title_text = (row["title"] or "")[:2000] or " "
    press_text = (row["press"] or "")[:100]
    pub_text = (row["pub_text"] or "")[:100]
    first_seen = row["first_seen"] or None

    props = {
        "제목": {
            "title": [{"type": "text", "text": {"content": title_text}}]
        },
        "감성": {
            "select": {"name": SENTIMENT_MAP.get(row["sentiment"], "· 중립")}
        },
        "매체": {
            "rich_text": [{"type": "text", "text": {"content": press_text}}]
        },
        "키워드": {
            "multi_select": [{"name": k} for k in kws]
        },
        "발행": {
            "rich_text": [{"type": "text", "text": {"content": pub_text}}]
        },
        "링크": {"url": row["url"] or None},
    }
    # 수집시각: 값이 있을 때만 (Notion date property는 None이면 에러)
    if first_seen:
        props["수집시각"] = {"date": {"start": first_seen}}
    return props


class NotionTokenMissing(RuntimeError):
    """필수 Notion 환경변수(NOTION_TOKEN 등) 미설정."""


def add_alert_comment(notion, page_id, user_ids, keywords_matched):
    """페이지에 담당자 mention을 포함한 코멘트를 추가.

    user_ids: 단일 UUID 문자열 또는 여러 UUID의 리스트. 리스트일 때는
              멘션이 나열되며 각 대상자 모두에게 알림이 간다.
    형식: "@User1 @User2 키워드1·키워드2 | 부정 기사 확인 필요"
    """
    if isinstance(user_ids, str):
        user_ids = [user_ids]
    user_ids = [u for u in user_ids if u]  # 빈 값 제거
    if not user_ids:
        raise ValueError("user_ids 비어있음")

    rich_text = []
    for i, uid in enumerate(user_ids):
        if i > 0:
            rich_text.append({"type": "text", "text": {"content": " "}})
        rich_text.append({
            "type": "mention",
            "mention": {"type": "user", "user": {"id": uid}},
        })
    kw_text = "·".join(sorted(keywords_matched))
    rich_text.append({
        "type": "text",
        "text": {"content": f" {kw_text} | 부정 기사 확인 필요"},
    })
    notion.comments.create(parent={"page_id": page_id}, rich_text=rich_text)


# --------------------------------------------------------------------------
# 요약 대시보드 페이지 갱신 (Crawler_News 페이지의 sentinel 아래를 재작성)
# --------------------------------------------------------------------------

def get_data_source_id(notion, database_id):
    """databases.retrieve 로 첫 번째 data source ID 조회.
    Notion 2025-09-03 API 이후 query 엔드포인트가 data source 기반으로 이동."""
    db = notion.databases.retrieve(database_id=database_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(
            f"database {database_id}에 data source 없음. "
            "Notion에서 통합 앱이 DB에 연결됐는지 확인해 주세요."
        )
    return sources[0]["id"]


def _parse_notion_article(pg):
    """Notion DB 페이지 → 통계용 dict."""
    props = pg.get("properties", {})
    url = (props.get("링크") or {}).get("url")
    senti = ((props.get("감성") or {}).get("select") or {}).get("name")
    kws = [k["name"] for k in ((props.get("키워드") or {}).get("multi_select") or [])]
    press_rt = (props.get("매체") or {}).get("rich_text") or []
    press = press_rt[0].get("plain_text", "") if press_rt else ""
    first_seen = ((props.get("수집시각") or {}).get("date") or {}).get("start")
    return {
        "url": url,
        "sentiment": senti,
        "keywords": kws,
        "press": press,
        "first_seen": first_seen,
    }


def fetch_all_articles(notion, ds_id):
    """Notion DB의 모든 페이지를 통계용 dict 리스트로 반환."""
    articles = []
    cursor = None
    while True:
        resp = notion.data_sources.query(
            data_source_id=ds_id, start_cursor=cursor, page_size=100
        )
        for pg in resp["results"]:
            articles.append(_parse_notion_article(pg))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return articles


def aggregate_stats(articles):
    """리스트 → 총계·감성·키워드·매체·오늘 dict."""
    today_kst = datetime.datetime.now(KST).date()
    stats = {
        "total": len(articles),
        "today": 0,
        "sentiment": Counter(),
        "keywords": Counter(),
        "press": Counter(),
    }
    for a in articles:
        s = a.get("sentiment") or "· 중립"
        stats["sentiment"][s] += 1
        for k in a.get("keywords") or []:
            stats["keywords"][k] += 1
        if a.get("press"):
            stats["press"][a["press"]] += 1
        first_seen = a.get("first_seen")
        if first_seen:
            try:
                dt = datetime.datetime.fromisoformat(
                    first_seen.replace("Z", "+00:00")
                )
                if dt.astimezone(KST).date() == today_kst:
                    stats["today"] += 1
            except Exception:
                pass
    return stats


def _text(content):
    return [{"type": "text", "text": {"content": content}}]


def _h2(text):
    return {"type": "heading_2", "heading_2": {"rich_text": _text(text)}}


def _h3(text):
    return {"type": "heading_3", "heading_3": {"rich_text": _text(text)}}


def _bullet(text, color=None):
    """color: Notion 유효 색상. 예: 'red', 'red_background', 'default'.
    None이면 색 미지정."""
    body = {"rich_text": _text(text)}
    if color:
        body["color"] = color
    return {"type": "bulleted_list_item", "bulleted_list_item": body}


# 키워드 그룹 색상을 Notion multi-select 태그와 동일한 톤으로 보이도록
# _background variant 사용. (multi-select 태그도 배경 하이라이트 스타일)
def _kw_bullet_color(kw):
    base = config.KEYWORD_COLORS.get(kw)
    if not base or base == "default":
        return "default"
    return f"{base}_background"


def _para(text):
    return {"type": "paragraph", "paragraph": {"rich_text": _text(text)}}


def _callout(text, emoji="ℹ️"):
    return {
        "type": "callout",
        "callout": {
            "rich_text": _text(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def build_summary_blocks(stats, last_updated_iso):
    """통계 dict → Notion block payload 리스트. 첫 블록은 반드시 sentinel."""
    blocks = []
    # 0) sentinel — 다음 실행 시 이 위치 이후만 재작성
    blocks.append({
        "type": "heading_1",
        "heading_1": {"rich_text": _text(SUMMARY_SENTINEL)},
    })
    # 1) 마지막 갱신 시각
    blocks.append(_callout(f"마지막 갱신: {last_updated_iso} (KST)", "🕐"))
    # 2) 설명
    blocks.append(_para(
        "네이버 뉴스에서 지정 키워드로 수집한 기사 데이터베이스의 요약 지표입니다. "
        "본문 데이터는 위쪽 데이터베이스 뷰에서 확인하세요. "
        "감성은 룰 기반 자동 분류이며 오분류가 있을 수 있습니다."
    ))
    # 3) 수집 현황
    blocks.append(_h2("📥 수집 현황"))
    blocks.append(_bullet(f"총 수집: {stats['total']:,}건"))
    blocks.append(_bullet(f"오늘 수집(KST): {stats['today']:,}건"))
    # 4) 감성 분포 — 라벨별 색상
    blocks.append(_h2("😊 감성 자동 분류"))
    _senti_color = {
        "👍 긍정": "green_background",
        "👎 부정": "red_background",
        "· 중립": "gray_background",
    }
    for label in ("👍 긍정", "👎 부정", "· 중립"):
        blocks.append(_bullet(
            f"{label}: {stats['sentiment'].get(label, 0):,}건",
            color=_senti_color[label],
        ))
    # 5) 키워드별 (config 순서 유지) — 그룹 색상 적용
    blocks.append(_h2("🏷️ 키워드별 기사 수"))
    for kw in config.KEYWORDS:
        blocks.append(_bullet(
            f"{kw}: {stats['keywords'].get(kw, 0):,}건",
            color=_kw_bullet_color(kw),
        ))
    # 6) 매체 상위 20
    blocks.append(_h2("📺 매체별 기사 수 (상위 20)"))
    top20 = sorted(stats["press"].items(), key=lambda x: -x[1])[:20]
    if not top20:
        blocks.append(_para("(집계 데이터 없음)"))
    for name, cnt in top20:
        blocks.append(_bullet(f"{name}: {cnt:,}건"))
    # 마지막: 끝 sentinel — 이 표시 사이만 재작성 영역
    blocks.append({
        "type": "heading_1",
        "heading_1": {"rich_text": _text(SUMMARY_SENTINEL_END)},
    })
    return blocks


def _list_page_children(notion, page_id):
    """페이지의 최상위 children 블록 전체 리스트."""
    out = []
    cursor = None
    while True:
        resp = notion.blocks.children.list(
            block_id=page_id, start_cursor=cursor, page_size=100
        )
        out.extend(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


def _find_heading_index(children, text_target):
    """heading_1 중 텍스트가 text_target과 일치하는 첫 인덱스. 없으면 None."""
    for i, b in enumerate(children):
        if b.get("type") != "heading_1":
            continue
        rt = b["heading_1"].get("rich_text") or []
        text = "".join(r.get("plain_text", "") for r in rt).strip()
        if text == text_target.strip():
            return i
    return None


def _safe_delete_range(children, start_idx, end_idx):
    """start_idx ~ end_idx (양끝 포함) 범위 블록 중 PROTECTED가 아닌 것만 반환."""
    out = []
    for i in range(start_idx, end_idx + 1):
        b = children[i]
        if b.get("type") in PROTECTED_BLOCK_TYPES:
            print(f"  [PROTECT] index={i} type={b['type']} — 삭제 스킵")
            continue
        out.append(b)
    return out


def update_summary_page(page_id=None, articles=None):
    """Crawler_News 페이지의 sentinel 사이 통계 블록을 안전하게 재작성.

    안전 규칙:
    1. 시작·끝 sentinel 두 개가 모두 있어야만 그 사이를 재생성.
    2. sentinel 사이라도 child_database·child_page 같은 PROTECTED 타입은 삭제 안 함.
    3. sentinel 하나만 있거나 없으면 append만 (기존 삭제 없음).

    articles: 미리 조회한 list. None이면 이 함수 안에서 Notion을 조회.
    반환: dict(total, today, added_blocks, deleted_blocks, mode).
    """
    token = os.environ.get("NOTION_TOKEN") or NOTION_TOKEN
    if not token:
        raise NotionTokenMissing("환경변수 NOTION_TOKEN 미설정")
    page_id = page_id or os.environ.get("NOTION_SUMMARY_PAGE_ID") or NOTION_SUMMARY_PAGE_ID
    if not page_id:
        raise NotionTokenMissing("환경변수 NOTION_SUMMARY_PAGE_ID 미설정")
    db_id = os.environ.get("NOTION_DB_ID") or NOTION_DB_ID
    if not db_id:
        raise NotionTokenMissing("환경변수 NOTION_DB_ID 미설정")

    notion = Client(auth=token)
    if articles is None:
        ds_id = get_data_source_id(notion, db_id)
        articles = fetch_all_articles(notion, ds_id)

    stats = aggregate_stats(articles)
    now_iso = datetime.datetime.now(KST).isoformat(timespec="seconds")
    new_blocks = build_summary_blocks(stats, now_iso)

    children = _list_page_children(notion, page_id)
    start_idx = _find_heading_index(children, SUMMARY_SENTINEL)
    end_idx = _find_heading_index(children, SUMMARY_SENTINEL_END)

    to_delete = []
    mode = "append-only"
    if start_idx is not None and end_idx is not None and end_idx >= start_idx:
        to_delete = _safe_delete_range(children, start_idx, end_idx)
        mode = "replace-between-sentinels"
    elif start_idx is not None or end_idx is not None:
        # 한쪽만 있음 — 위험 상황이므로 삭제하지 말고 append만
        print(
            f"[summary] WARNING: sentinel 한쪽만 발견 "
            f"(start={start_idx}, end={end_idx}). 삭제 없이 append만 진행."
        )

    print(
        f"[summary] children={len(children)}, start={start_idx}, end={end_idx}, "
        f"delete={len(to_delete)}, new={len(new_blocks)}, mode={mode}"
    )
    for b in to_delete:
        try:
            notion.blocks.delete(block_id=b["id"])
        except Exception as e:
            print(f"  del skip {b['id'][-6:]}: {e}")
        time.sleep(RATE_DELAY)

    # 100개씩 청크로 append
    for i in range(0, len(new_blocks), 100):
        chunk = new_blocks[i:i + 100]
        notion.blocks.children.append(block_id=page_id, children=chunk)
        time.sleep(RATE_DELAY)

    return {
        "total": stats["total"],
        "today": stats["today"],
        "added_blocks": len(new_blocks),
        "deleted_blocks": len(to_delete),
        "mode": mode,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Notion 요약 대시보드 페이지만 갱신 (크롤·push는 run_headless.py)."
    )
    p.parse_args()
    try:
        r = update_summary_page()
        print(f"요약 갱신 완료 — total={r['total']} today={r['today']}")
    except NotionTokenMissing as e:
        print(str(e))
        sys.exit(1)
