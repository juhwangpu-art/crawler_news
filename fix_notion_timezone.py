# -*- coding: utf-8 -*-
"""이미 Notion에 push된 기사들의 `수집시각`을 SQLite의 KST(+09:00) 값으로 갱신.

기사 페이지 자체는 삭제/재작성하지 않고 date property만 pages.update로 덮어쓴다.

사용법:
  # 대상 건수만 확인 (실제 update 안 함)
  python fix_notion_timezone.py --dry-run

  # 실제 실행 (약 6분 소요, 981건 기준)
  python fix_notion_timezone.py
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from notion_client import Client

from sync_notion import NOTION_DB_ID, RATE_DELAY, NotionTokenMissing

DB_FILE = Path(__file__).parent / "news.db"


def fetch_db_first_seen():
    """SQLite에서 url → first_seen(KST-tagged) dict."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return {
        r["url"]: r["first_seen"]
        for r in conn.execute("SELECT url, first_seen FROM articles")
        if r["first_seen"]
    }


def _get_data_source_id(notion, database_id):
    """databases.retrieve로 첫 번째 data source id 조회.

    Notion 2025-09-03 API 이후 query 엔드포인트가 data source 기반으로 이동.
    """
    db = notion.databases.retrieve(database_id=database_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(
            f"database {database_id}에 data source가 없습니다. "
            "Notion에서 DB가 아니라 일반 페이지를 지정했는지 확인해 주세요."
        )
    return sources[0]["id"]


def iter_notion_pages(notion, db_id, page_size=100):
    """Notion DB의 모든 페이지를 (page_id, url, 수집시각_start) 순회."""
    ds_id = _get_data_source_id(notion, db_id)
    cursor = None
    while True:
        resp = notion.data_sources.query(
            data_source_id=ds_id,
            start_cursor=cursor,
            page_size=page_size,
        )
        for pg in resp["results"]:
            props = pg.get("properties", {})
            url = (props.get("링크") or {}).get("url")
            date_obj = (props.get("수집시각") or {}).get("date") or {}
            date_start = date_obj.get("start")
            yield pg["id"], url, date_start
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="update 없이 대상만 계수하고 종료",
    )
    args = p.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise NotionTokenMissing("환경변수 NOTION_TOKEN 미설정")
    db_id = os.environ.get("NOTION_DB_ID") or NOTION_DB_ID
    if not db_id:
        raise NotionTokenMissing("환경변수 NOTION_DB_ID 미설정")

    notion = Client(auth=token)
    db_map = fetch_db_first_seen()
    print(f"SQLite 기사 총 {len(db_map)}건")
    print(f"→ Notion DB({db_id[-12:]}...) 페이지 순회 시작...")

    total = 0
    matched, ok, updated, failed, no_url, no_dbmatch = 0, 0, 0, 0, 0, 0
    for page_id, url, notion_date in iter_notion_pages(notion, db_id):
        total += 1
        if not url:
            no_url += 1
            continue
        target = db_map.get(url)
        if not target:
            no_dbmatch += 1
            continue
        matched += 1
        if notion_date == target:
            ok += 1
            continue
        if args.dry_run:
            updated += 1
            if updated <= 5:
                print(f"  [DRY] {page_id[-6:]} '{notion_date}' -> '{target}'")
            continue
        try:
            notion.pages.update(
                page_id=page_id,
                properties={"수집시각": {"date": {"start": target}}},
            )
            updated += 1
            if updated % 20 == 0 or updated <= 5:
                print(
                    f"  [{updated}] {page_id[-6:]} '{notion_date}' -> '{target}'"
                )
        except Exception as e:
            failed += 1
            print(f"  실패 {page_id[-6:]}: {e}")
        time.sleep(RATE_DELAY)

    tag = " (dry-run)" if args.dry_run else ""
    print(f"\n완료{tag}")
    print(f"  Notion 총 페이지: {total}")
    print(f"  DB 매칭: {matched}")
    print(f"    이미 정확: {ok}")
    print(f"    갱신 대상/완료: {updated}")
    if failed:
        print(f"    실패: {failed}")
    print(f"  URL 속성 없음: {no_url}")
    print(f"  DB에 없음: {no_dbmatch}")


if __name__ == "__main__":
    try:
        main()
    except NotionTokenMissing as e:
        print(str(e))
        sys.exit(1)
