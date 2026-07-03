# -*- coding: utf-8 -*-
"""헤드리스 크롤러 + Notion push. GitHub Actions cron에서 실행.

로컬 SQLite/캐시 파일을 쓰지 않는다. dedup은 매 실행마다 Notion DB를 조회해
기존 URL 집합을 in-memory로 로드한 뒤, 신규만 push한다.

실행:
  # 로컬 테스트
  $env:NOTION_TOKEN = "..." ; python run_headless.py

  # GitHub Actions (workflow가 자동 실행)
"""
import os
import sys
import time

from notion_client import Client

import config
import crawler
from sync_notion import (
    NOTION_DB_ID,
    RATE_DELAY,
    NotionTokenMissing,
    build_page,
    fetch_all_articles,
    get_data_source_id,
    update_summary_page,
)


def to_db_row(article, keyword):
    """crawler.scrape_all 출력 dict → build_page가 요구하는 SQLite row 형태."""
    return {
        "url": article["url"],
        "title": article["title"],
        "press": article["press"],
        "summary": article["summary"],
        "pub_text": article["pub_text"],
        "keywords": article.get("keyword") or keyword,
        "sentiment": article.get("sentiment"),
        "first_seen": article["crawled_at"],
        "last_seen": article["crawled_at"],
    }


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise NotionTokenMissing("NOTION_TOKEN 환경변수 미설정")

    notion = Client(auth=token)
    ds_id = get_data_source_id(notion, NOTION_DB_ID)

    # 한 번의 순회로 기존 URL 집합 + 통계용 raw article 리스트 확보
    existing_articles = fetch_all_articles(notion, ds_id)
    existing = {a["url"] for a in existing_articles if a["url"]}
    print(f"기존 Notion 페이지 {len(existing_articles)}건 로드 (URL 유효 {len(existing)}건)")

    print(f"→ 네이버 뉴스 크롤 시작 ({len(config.KEYWORDS)} keywords)")
    rows = crawler.scrape_all(
        config.KEYWORDS,
        limit=config.MAX_PER_KEYWORD,
        delay=config.REQUEST_DELAY_SEC,
    )
    print(f"크롤 완료: {len(rows)}건 (중복 URL 병합 전)")

    # URL 단위로 dedup + 키워드 병합
    merged = {}
    for a in rows:
        u = a["url"]
        if u in merged:
            existing_kw = set(merged[u]["keyword"].split(",") if merged[u].get("keyword") else [])
            existing_kw.add(a["keyword"])
            merged[u]["keyword"] = ",".join(sorted(existing_kw))
        else:
            merged[u] = dict(a)
    print(f"URL dedup 후: {len(merged)}건")

    to_push = [a for a in merged.values() if a["url"] not in existing]
    print(f"신규 push 대상: {len(to_push)}건")

    added, failed = 0, 0
    pushed_articles = []
    for i, a in enumerate(to_push, 1):
        row = to_db_row(a, a["keyword"])
        try:
            notion.pages.create(
                parent={"database_id": NOTION_DB_ID},
                properties=build_page(row),
            )
            added += 1
            pushed_articles.append({
                "url": a["url"],
                "sentiment": {"positive": "👍 긍정", "negative": "👎 부정"}.get(
                    a.get("sentiment"), "· 중립"
                ),
                "keywords": [k for k in a["keyword"].split(",") if k],
                "press": a.get("press") or "",
                "first_seen": a.get("crawled_at"),
            })
            if i % 10 == 0 or i == len(to_push):
                print(f"  [{i}/{len(to_push)}] {(a['title'] or '')[:40]}")
        except Exception as e:
            failed += 1
            print(f"  실패 {a['url'][-30:]}: {e}")
        time.sleep(RATE_DELAY)

    print(f"\n완료 — 추가 {added}건 · 실패 {failed}건 · Notion 누적 {len(existing) + added}건")

    # 요약 페이지 갱신 (기존 + 이번에 push된 것 합쳐서 통계)
    try:
        combined = existing_articles + pushed_articles
        r = update_summary_page(articles=combined)
        print(
            f"요약 페이지 갱신 완료 — total={r['total']} today={r['today']} "
            f"(deleted={r['deleted_blocks']}, added={r['added_blocks']})"
        )
    except Exception as e:
        print(f"요약 페이지 갱신 실패: {e}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NotionTokenMissing as e:
        print(str(e))
        sys.exit(1)
