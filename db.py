# -*- coding: utf-8 -*-
"""SQLite 저장소.

테이블
- articles : 크롤링한 뉴스(URL 기준 중복 제거, 키워드는 병합 저장)
- saved    : 사용자가 긍정/부정으로 저장한 기획 기사
- meta     : 마지막 동기화 시각 등 상태값
"""
import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "news.db")
_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, get_conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS articles(
                url        TEXT PRIMARY KEY,
                title      TEXT,
                press      TEXT,
                summary    TEXT,
                pub_text   TEXT,
                keywords   TEXT,
                first_seen TEXT,
                last_seen  TEXT,
                sentiment  TEXT,
                sentiment_score TEXT
            )
            """
        )
        # 기존 DB 대비 컬럼 자동 추가
        existing = {r[1] for r in c.execute("PRAGMA table_info(articles)")}
        if "sentiment" not in existing:
            c.execute("ALTER TABLE articles ADD COLUMN sentiment TEXT")
        if "sentiment_score" not in existing:
            c.execute("ALTER TABLE articles ADD COLUMN sentiment_score TEXT")
        # naive ISO 문자열(예 '2026-07-03T18:02:15')에 KST offset 부여.
        # Notion date property가 timezone 없는 값을 UTC로 해석해 9시간 밀리는 것을 방지.
        for col in ("first_seen", "last_seen"):
            c.execute(
                f"UPDATE articles SET {col} = {col} || '+09:00' "
                f"WHERE {col} IS NOT NULL AND LENGTH({col}) = 19 "
                f"AND {col} NOT LIKE '%+%' AND {col} NOT LIKE '%Z'"
            )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS saved(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sentiment    TEXT,   -- 'positive' | 'negative'
                pub_datetime TEXT,   -- 일시
                press        TEXT,   -- 매체
                journalist   TEXT,   -- 기자
                title        TEXT,   -- 제목
                summary      TEXT,   -- 한줄요약
                url          TEXT,
                saved_at     TEXT
            )
            """
        )
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")


# ---------- articles ----------
def upsert_article(a):
    """반환값: 신규 기사면 True, 기존 갱신이면 False."""
    now = a["crawled_at"]
    with _lock, get_conn() as c:
        row = c.execute(
            "SELECT keywords FROM articles WHERE url=?", (a["url"],)
        ).fetchone()
        if row:
            kws = set(filter(None, (row["keywords"] or "").split(",")))
            kws.add(a["keyword"])
            c.execute(
                """UPDATE articles
                   SET last_seen=?, keywords=?, title=?, press=?, summary=?, pub_text=?,
                       sentiment=?, sentiment_score=?
                   WHERE url=?""",
                (
                    now,
                    ",".join(sorted(kws)),
                    a["title"],
                    a["press"],
                    a["summary"],
                    a["pub_text"],
                    a.get("sentiment"),
                    a.get("sentiment_score"),
                    a["url"],
                ),
            )
            return False
        c.execute(
            """INSERT INTO articles(url,title,press,summary,pub_text,keywords,first_seen,last_seen,sentiment,sentiment_score)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                a["url"],
                a["title"],
                a["press"],
                a["summary"],
                a["pub_text"],
                a["keyword"],
                now,
                now,
                a.get("sentiment"),
                a.get("sentiment_score"),
            ),
        )
        return True


def get_articles(
    keyword=None,
    keywords=None,
    search=None,
    sentiment=None,
    scope=None,
    press_list=None,
    limit=500,
):
    """scope: None(전체) | 'today' | 'cycle_all' | 'cycle_new'.

    keyword    : 단일 키워드 필터(하위호환).
    keywords   : 여러 키워드 OR 필터. 하나라도 포함되면 매칭.
    press_list : 매체명 화이트리스트. 리스트에 있는 매체만 매칭.
    """
    q = "SELECT * FROM articles"
    conds, params = [], []
    if keyword:
        conds.append("(',' || keywords || ',') LIKE ?")
        params.append(f"%,{keyword},%")
    if keywords:
        sub = []
        for k in keywords:
            sub.append("(',' || keywords || ',') LIKE ?")
            params.append(f"%,{k},%")
        conds.append("(" + " OR ".join(sub) + ")")
    if press_list:
        placeholders = ",".join("?" for _ in press_list)
        conds.append(f"press IN ({placeholders})")
        params.extend(press_list)
    if search:
        conds.append("(title LIKE ? OR summary LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if sentiment:
        conds.append("sentiment=?")
        params.append(sentiment)
    if scope == "today":
        conds.append("date(first_seen) = date('now','localtime')")
    elif scope in ("cycle_all", "cycle_new"):
        sync_start = get_meta("last_sync_start")
        if sync_start:
            col = "last_seen" if scope == "cycle_all" else "first_seen"
            conds.append(f"{col} >= ?")
            params.append(sync_start)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)
    with get_conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def sentiment_counts():
    """감성 라벨별 기사 수 dict (positive/negative/neutral)."""
    out = {"positive": 0, "negative": 0, "neutral": 0}
    with get_conn() as c:
        for r in c.execute(
            "SELECT COALESCE(sentiment,'neutral') AS s, COUNT(*) AS n "
            "FROM articles GROUP BY s"
        ):
            out[r["s"]] = r["n"]
    return out


def distinct_press():
    """DB에 존재하는 매체명 목록(빈 값 제외, 알파벳/가나다 정렬)."""
    with get_conn() as c:
        return [
            r[0]
            for r in c.execute(
                "SELECT press FROM articles "
                "WHERE press IS NOT NULL AND press!='' "
                "GROUP BY press ORDER BY press COLLATE NOCASE"
            )
        ]


def keyword_counts(keywords):
    """키워드별 기사 수 dict."""
    counts = {}
    with get_conn() as c:
        for kw in keywords:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM articles "
                "WHERE (',' || keywords || ',') LIKE ?",
                (f"%,{kw},%",),
            ).fetchone()
            counts[kw] = row["n"]
    return counts


def total_count():
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def today_count():
    """오늘 처음 수집된 기사 수 (first_seen 기준, 로컬 시간)."""
    with get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM articles "
            "WHERE date(first_seen) = date('now','localtime')"
        ).fetchone()[0]


# ---------- saved ----------
def save_article(rec):
    with _lock, get_conn() as c:
        c.execute(
            """INSERT INTO saved(sentiment,pub_datetime,press,journalist,title,summary,url,saved_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                rec["sentiment"],
                rec["pub_datetime"],
                rec["press"],
                rec["journalist"],
                rec["title"],
                rec["summary"],
                rec["url"],
                rec["saved_at"],
            ),
        )


def get_saved(sentiment):
    with get_conn() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM saved WHERE sentiment=? ORDER BY saved_at DESC",
                (sentiment,),
            ).fetchall()
        ]


def delete_saved(saved_id):
    with _lock, get_conn() as c:
        c.execute("DELETE FROM saved WHERE id=?", (saved_id,))


def is_saved(url):
    with get_conn() as c:
        row = c.execute(
            "SELECT sentiment FROM saved WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        return row["sentiment"] if row else None


# ---------- meta ----------
def set_meta(k, v):
    with _lock, get_conn() as c:
        c.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, str(v)),
        )


def get_meta(k, default=None):
    with get_conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default
