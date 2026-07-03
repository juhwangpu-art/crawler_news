# -*- coding: utf-8 -*-
"""네이버 뉴스 검색 스크래퍼.

검색 결과 페이지(search.naver.com)에서 제목/링크/매체/일시/요약을 추출한다.
기자명은 검색 결과에 없으므로, 저장 시 기사 본문 페이지에서 best-effort로 추출한다.
"""
import datetime
import random
import re
import time

import requests
from bs4 import BeautifulSoup

import sentiment as _sentiment

SEARCH_URL = "https://search.naver.com/search.naver"

# 브라우저처럼 보이기 위한 헤더 세트 + 세션(쿠키) 유지
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


def _build_headers():
    return {
        "User-Agent": random.choice(_UAS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.naver.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
    }


_session = None


def _get_session():
    """네이버 anti-bot 대응 — 세션 쿠키를 유지하고, 최초 1회 메인페이지를 warm-up 방문."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(_build_headers())
        try:
            s.get("https://www.naver.com/", timeout=10)
        except Exception as e:
            print(f"[crawler] warm-up 실패(계속 진행): {e}")
        _session = s
    return _session


def _reset_session():
    """403 등 심한 차단 시 세션·쿠키 리셋."""
    global _session
    _session = None


_TIME_PATTERNS = [
    re.compile(r"\d+\s*분\s*전"),
    re.compile(r"\d+\s*시간\s*전"),
    re.compile(r"\d+\s*일\s*전"),
    re.compile(r"\d+\s*주\s*전"),
    re.compile(r"\d+\s*(개월|달)\s*전"),
    re.compile(r"\d+\s*년\s*전"),
    re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}"),      # 2026.07.01.
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),        # 2026-07-01
    re.compile(r"어제|오늘|방금"),
]

_NON_TIME_HINTS = ("면", "TOP", "PICK", "단독", "구독", "언론사")


def _pick_pub_text(subs):
    """subtext 목록 중 실제 시간 형태를 우선 선택.
    'A8면 1단', '17면 TOP' 같은 지면 마커는 건너뛴다."""
    for s in subs:
        for p in _TIME_PATTERNS:
            if p.search(s):
                return s
    # 시간 패턴을 못 찾았으면, 지면/뱃지 힌트가 있는 항목을 제외한 첫 항목
    for s in subs:
        if not any(h in s for h in _NON_TIME_HINTS):
            return s
    return ""


def _item_root(anchor):
    """제목 앵커에서 위로 올라가며 매체·제목을 모두 포함하는 기사 컨테이너를 찾는다."""
    node = anchor
    for _ in range(12):
        node = node.parent
        if node is None:
            break
        if node.select_one(
            "span.sds-comps-profile-info-title-text"
        ) and node.select_one("span.sds-comps-text-type-headline1"):
            return node
    return None


def _fetch_search(keyword, retries=3):
    """403 등 오류 시 지수 백오프 재시도 + 세션 리셋."""
    params = {"where": "news", "query": keyword, "sort": "1"}
    last_err = None
    for attempt in range(retries):
        s = _get_session()
        try:
            r = s.get(SEARCH_URL, params=params, timeout=12)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                wait = (2 ** attempt) + random.uniform(1.0, 2.5)
                print(
                    f"[crawler] {keyword}: HTTP {r.status_code} → {wait:.1f}s 대기 후 재시도"
                )
                _reset_session()
                time.sleep(wait)
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            last_err = e
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            print(f"[crawler] {keyword}: {e} → {wait:.1f}s 대기 후 재시도")
            _reset_session()
            time.sleep(wait)
    if last_err:
        raise last_err
    raise RuntimeError(f"수집 실패({keyword}): 재시도 초과")


def scrape_keyword(keyword, limit=30):
    r = _fetch_search(keyword)
    soup = BeautifulSoup(r.text, "lxml")
    # 로컬 timezone offset을 포함시켜 Notion이 UTC로 오해하지 않도록
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    out, seen = [], set()
    for anchor in soup.select("a:has(span.sds-comps-text-type-headline1)"):
        root = _item_root(anchor)
        if root is None:
            continue
        href = anchor.get("href")
        if not href or href in seen:
            continue
        seen.add(href)

        title_el = anchor.select_one("span.sds-comps-text-type-headline1")
        title = title_el.get_text(strip=True) if title_el else ""

        press_el = root.select_one("span.sds-comps-profile-info-title-text")
        press = press_el.get_text(strip=True) if press_el else ""

        subs = [
            s.get_text(strip=True)
            for s in root.select("span.sds-comps-profile-info-subtext")
        ]
        # subtext 중 시간 형식만 선별 (지면 마커 'A8면 1단' 등은 제외)
        pub_text = _pick_pub_text(subs)

        body_el = root.select_one(
            "span.sds-comps-text-type-body1, a.sds-comps-text-type-body1"
        )
        summary = body_el.get_text(strip=True) if body_el else ""

        label, scores = _sentiment.classify(title, summary)
        out.append(
            {
                "keyword": keyword,
                "title": title,
                "url": href,
                "press": press,
                "pub_text": pub_text,
                "summary": summary,
                "crawled_at": now,
                "sentiment": label,
                "sentiment_score": f"pos={scores['pos']},neg={scores['neg']}",
            }
        )
        if len(out) >= limit:
            break
    return out


def scrape_all(keywords, limit=30, delay=1.5, on_progress=None):
    """모든 키워드를 순회 수집. on_progress(kw, idx, total) 콜백 지원.

    각 요청 사이에 delay ± 0.5s 지터를 두어 봇 탐지를 회피한다.
    """
    results = []
    total = len(keywords)
    for i, kw in enumerate(keywords):
        try:
            results.extend(scrape_keyword(kw, limit=limit))
        except Exception as e:
            print(f"[crawler] '{kw}' 수집 실패: {e}")
        if on_progress:
            on_progress(kw, i + 1, total)
        time.sleep(delay + random.uniform(-0.5, 0.8))
    return results


def fetch_journalist(url, timeout=8):
    """기사 본문 페이지에서 기자명을 best-effort로 추출."""
    try:
        s = _get_session()
        r = s.get(url, timeout=timeout)
        soup = BeautifulSoup(r.text, "lxml")
        # 1) 메타 태그 우선
        for sel, attr in [
            ("meta[property='dable:author']", "content"),
            ("meta[name='author']", "content"),
            ("meta[property='og:article:author']", "content"),
            ("meta[name='dable:author']", "content"),
        ]:
            el = soup.select_one(sel)
            if el and el.get(attr) and el.get(attr).strip():
                return el.get(attr).strip()
        # 2) 본문에서 'OOO 기자' 패턴
        text = soup.get_text(" ", strip=True)
        m = re.search(r"([가-힣]{2,4})\s*기자", text)
        if m:
            return f"{m.group(1)} 기자"
    except Exception as e:
        print(f"[crawler] 기자명 추출 실패: {e}")
    return ""


def rel_to_datetime(pub_text, base=None):
    """'5분 전', '3시간 전', '2026.07.01.' 등을 절대 시각 문자열로 변환(근사)."""
    base = base or datetime.datetime.now()
    t = (pub_text or "").strip()
    m = re.search(r"(\d+)\s*분", t)
    if m:
        return (base - datetime.timedelta(minutes=int(m.group(1)))).strftime(
            "%Y-%m-%d %H:%M"
        )
    m = re.search(r"(\d+)\s*시간", t)
    if m:
        return (base - datetime.timedelta(hours=int(m.group(1)))).strftime(
            "%Y-%m-%d %H:%M"
        )
    m = re.search(r"(\d+)\s*일", t)
    if m:
        return (base - datetime.timedelta(days=int(m.group(1)))).strftime(
            "%Y-%m-%d %H:%M"
        )
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d} 00:00"
    return base.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    rows = scrape_keyword("키움증권", limit=5)
    for row in rows:
        print(row["press"], "|", row["pub_text"], "|", row["title"])
        print("   ", row["url"])
