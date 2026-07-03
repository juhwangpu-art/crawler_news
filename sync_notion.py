# -*- coding: utf-8 -*-
"""수집된 기사를 Notion 데이터베이스로 동기화.

사용법:
  # 최초 1회 환경변수 세팅 (PowerShell)
  $env:NOTION_TOKEN = "secret_xxxxx"
  $env:NOTION_DB_ID = "REDACTED_NOTION_DB_ID"

  # 실행 (신규 기사만 append)
  python sync_notion.py

  # 전체 백필 (처음 한 번만)
  python sync_notion.py --backfill

주기적 실행을 위해 Windows 작업 스케줄러에 등록하거나
Streamlit 사이드바 버튼에서 호출할 수 있다.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

try:
    from notion_client import Client
except ImportError:
    print("notion-client 미설치. `pip install notion-client` 실행 필요")
    sys.exit(1)

import config

# --- 설정 ---
DB_FILE = Path(__file__).parent / "news.db"
CACHE_FILE = Path(__file__).parent / ".notion_synced.txt"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
# 데이터베이스 ID. 기본값은 juhwani 워크스페이스의 Crawler_News DB.
# 다른 DB로 바꾸려면 환경변수 NOTION_DB_ID 로 덮어쓰기.
NOTION_DB_ID = os.environ.get(
    "NOTION_DB_ID", "REDACTED_NOTION_DB_ID"
)
# 요약 대시보드 페이지 ID (Crawler_News 컨테이너 페이지).
# 코드가 이 페이지의 특정 sentinel heading 아래만 재작성하므로,
# 위쪽에 두는 수동 콘텐츠·인라인 DB 뷰는 건드리지 않는다.
NOTION_SUMMARY_PAGE_ID = os.environ.get(
    "NOTION_SUMMARY_PAGE_ID", "REDACTED_NOTION_PAGE_ID"
)

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


def load_synced_urls():
    if CACHE_FILE.exists():
        return set(CACHE_FILE.read_text(encoding="utf-8").splitlines())
    return set()


def save_synced_urls(urls):
    CACHE_FILE.write_text("\n".join(sorted(urls)), encoding="utf-8")


def build_page(row):
    """SQLite 행 → Notion 페이지 properties."""
    kws = [k for k in (row["keywords"] or "").split(",") if k in ALLOWED_KEYWORDS]
    return {
        "제목": [{"text": {"content": (row["title"] or "")[:2000]}}],
        "감성": {"name": SENTIMENT_MAP.get(row["sentiment"], "· 중립")},
        "매체": [{"text": {"content": (row["press"] or "")[:100]}}],
        "키워드": [{"name": k} for k in kws],
        "발행": [{"text": {"content": (row["pub_text"] or "")[:100]}}],
        "수집시각": {"start": row["first_seen"]},
        "링크": row["url"],
    }


def fetch_articles(since_iso=None):
    """news.db에서 기사 조회. since_iso 이후만."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM articles"
    params = []
    if since_iso:
        q += " WHERE first_seen >= ?"
        params.append(since_iso)
    q += " ORDER BY first_seen DESC"
    return list(conn.execute(q, params))


class NotionTokenMissing(RuntimeError):
    """NOTION_TOKEN 환경변수 미설정."""


def sync(backfill=False):
    """반환: dict(added, failed, total, synced_total, message)."""
    token = os.environ.get("NOTION_TOKEN") or NOTION_TOKEN
    if not token:
        raise NotionTokenMissing(
            "환경변수 NOTION_TOKEN 미설정. "
            "https://www.notion.so/my-integrations 에서 통합 앱을 만들고 "
            "Internal Integration Token(secret_xxxxx)을 발급받은 뒤 등록해 주세요."
        )

    notion = Client(auth=token)
    synced = load_synced_urls()
    articles = fetch_articles()

    print(f"news.db 총 {len(articles)}건 · 이미 동기화: {len(synced)}건")

    to_add = [a for a in articles if a["url"] not in synced]
    if not backfill:
        # 신규만 append 모드에서는 최근 100건 이내로 제한 (rate limit 방어)
        to_add = to_add[:100]

    if not to_add:
        print("추가할 신규 기사 없음")
        return {
            "added": 0,
            "failed": 0,
            "total": 0,
            "synced_total": len(synced),
            "message": "추가할 신규 기사가 없습니다.",
        }

    print(f"→ Notion에 {len(to_add)}건 추가 시작...")
    added, failed = 0, 0
    for i, row in enumerate(to_add, 1):
        try:
            notion.pages.create(
                parent={"database_id": NOTION_DB_ID},
                properties=build_page(row),
            )
            synced.add(row["url"])
            added += 1
            if i % 10 == 0 or i == len(to_add):
                print(f"  [{i}/{len(to_add)}] {row['title'][:40]}")
                save_synced_urls(synced)  # 중간 저장
        except Exception as e:
            failed += 1
            print(f"  실패({row['url'][-30:]}): {e}")
        time.sleep(RATE_DELAY)

    save_synced_urls(synced)
    msg = f"추가 {added}건 · 실패 {failed}건 · 누적 동기화 {len(synced)}건"
    print(f"완료 — {msg}")
    return {
        "added": added,
        "failed": failed,
        "total": len(to_add),
        "synced_total": len(synced),
        "message": msg,
    }


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


def _bullet(text):
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _text(text)},
    }


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
    # 4) 감성 분포
    blocks.append(_h2("😊 감성 자동 분류"))
    for label in ("👍 긍정", "👎 부정", "· 중립"):
        blocks.append(_bullet(
            f"{label}: {stats['sentiment'].get(label, 0):,}건"
        ))
    # 5) 키워드별 (config 순서 유지)
    blocks.append(_h2("🏷️ 키워드별 기사 수"))
    for kw in config.KEYWORDS:
        blocks.append(_bullet(f"{kw}: {stats['keywords'].get(kw, 0):,}건"))
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
    page_id = page_id or NOTION_SUMMARY_PAGE_ID

    notion = Client(auth=token)
    if articles is None:
        ds_id = get_data_source_id(notion, NOTION_DB_ID)
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
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true",
                   help="전체 백필 (기본은 최근 100건만)")
    p.add_argument("--summary-only", action="store_true",
                   help="크롤/push 없이 요약 페이지만 갱신")
    args = p.parse_args()
    try:
        if args.summary_only:
            r = update_summary_page()
            print(f"요약 갱신 완료 — total={r['total']} today={r['today']}")
        else:
            sync(backfill=args.backfill)
    except NotionTokenMissing as e:
        print(str(e))
        sys.exit(1)
