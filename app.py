# -*- coding: utf-8 -*-
"""네이버 뉴스 모니터링 대시보드.

- 설정된 키워드를 네이버 뉴스에서 30분 단위로 자동 수집(백그라운드 스레드)
- 기사 목록 + 원문 링크 제공
- 긍정/부정 기획 기사 저장(일시·매체·기자·제목·한줄요약)

실행:  python -m streamlit run app.py
"""
import datetime
import os
import threading

import pandas as pd
import streamlit as st

import config
import crawler
import db
import sync_notion

# 자사 뉴스 판별에 사용하는 키워드
OWN_KEYWORDS = ["키움증권", "키움증권 김익래", "김익래"]

st.set_page_config(page_title="뉴스 모니터링 대시보드", page_icon="📰", layout="wide")

# 사이드바 지표 폰트 축소 (기본 st.metric 값이 너무 큼)
st.markdown(
    """
    <style>
      [data-testid="stMetricValue"] { font-size: 1.05rem !important; font-weight: 600; line-height: 1.2; }
      [data-testid="stMetricLabel"] { font-size: 0.75rem !important; color: #666; }
      [data-testid="stMetricDelta"] { font-size: 0.75rem !important; }
      .stat-row { display:flex; justify-content:space-between; font-size:0.85rem;
                  padding:2px 0; border-bottom:1px dashed #eee; }
      .stat-row .k { color:#333; }
      .stat-row .v { color:#111; font-weight:600; font-variant-numeric: tabular-nums; }
      .senti-pos { color:#137333; font-weight:600; }
      .senti-neg { color:#c5221f; font-weight:600; }
      .senti-neu { color:#5f6368; }
    </style>
    """,
    unsafe_allow_html=True,
)

db.init_db()


# --------------------------------------------------------------------------
# 동기화 로직
# --------------------------------------------------------------------------
def do_sync():
    """모든 키워드를 수집 → DB 반영 → (토큰 있으면) Notion까지 자동 동기화.

    반환: (수집 총건, 신규 건, notion_added, notion_failed, notion_error_msg).
    notion 관련 값은 실행 안 했으면 (0, 0, None).
    """
    sync_start = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    db.set_meta("last_sync_start", sync_start)
    rows = crawler.scrape_all(
        config.KEYWORDS,
        limit=config.MAX_PER_KEYWORD,
        delay=config.REQUEST_DELAY_SEC,
    )
    new_count = 0
    for a in rows:
        if db.upsert_article(a):
            new_count += 1
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    db.set_meta("last_sync", now)
    db.set_meta("last_sync_total", len(rows))
    db.set_meta("last_sync_new", new_count)

    notion_added, notion_failed, notion_error = 0, 0, None
    if new_count > 0 and os.environ.get("NOTION_TOKEN"):
        try:
            result = sync_notion.sync()  # 신규 최근 100건 상한 (rate limit 방어)
            notion_added = result["added"]
            notion_failed = result["failed"]
            db.set_meta("last_notion_sync", now)
            db.set_meta("last_notion_added", notion_added)
            db.set_meta("last_notion_failed", notion_failed)
        except sync_notion.NotionTokenMissing as e:
            notion_error = str(e)
            print(f"[scheduler] Notion 자동 동기화 스킵: {e}")
        except Exception as e:
            notion_error = str(e)
            print(f"[scheduler] Notion 자동 동기화 실패: {e}")

    # Notion 요약 페이지 갱신 (토큰만 있으면 신규 없어도 갱신)
    if os.environ.get("NOTION_TOKEN"):
        try:
            r = sync_notion.update_summary_page()
            db.set_meta("last_notion_summary", now)
            print(
                f"[scheduler] 요약 페이지 갱신 — total={r['total']} today={r['today']}"
            )
        except Exception as e:
            print(f"[scheduler] 요약 페이지 갱신 실패: {e}")

    return len(rows), new_count, notion_added, notion_failed, notion_error


@st.cache_resource
def start_scheduler():
    """앱 실행 중 30분마다 자동 수집하는 데몬 스레드를 1회만 시작한다."""
    stop_event = threading.Event()

    def loop():
        # 최초 기동 시 수집 이력이 없으면 즉시 1회 수집
        if not db.get_meta("last_sync"):
            try:
                do_sync()
            except Exception as e:
                print(f"[scheduler] 초기 수집 실패: {e}")
        while not stop_event.wait(config.SYNC_INTERVAL_SEC):
            try:
                do_sync()
            except Exception as e:
                print(f"[scheduler] 자동 수집 실패: {e}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return {"thread": t, "stop": stop_event}


start_scheduler()


# --------------------------------------------------------------------------
# 사이드바 — 상태 및 필터
# --------------------------------------------------------------------------
def fmt_dt(iso):
    if not iso:
        return "없음"
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso


def stat_line(label, value):
    st.markdown(
        f"<div class='stat-row'><span class='k'>{label}</span>"
        f"<span class='v'>{value}</span></div>",
        unsafe_allow_html=True,
    )


with st.sidebar:
    st.header("⚙️ 동기화 상태")
    last = db.get_meta("last_sync")
    st.metric("마지막 동기화", fmt_dt(last))
    st.caption(f"자동 동기화 주기: {config.SYNC_INTERVAL_SEC // 60}분")

    if st.button("🔄 지금 동기화", width="stretch"):
        with st.spinner("네이버 뉴스 수집 중..."):
            total, new, n_added, n_failed, n_err = do_sync()
        msg = f"수집 {total}건 · 신규 {new}건"
        if n_added or n_failed:
            msg += f" · Notion 추가 {n_added}건"
            if n_failed:
                msg += f" · 실패 {n_failed}건"
        elif os.environ.get("NOTION_TOKEN") and new == 0:
            msg += " · Notion 스킵(신규 없음)"
        if n_err:
            st.warning(f"Notion 자동 동기화 오류: {n_err}")
        st.success(f"완료 — {msg}")
        st.rerun()

    # Notion 동기화
    _has_notion_token = bool(os.environ.get("NOTION_TOKEN"))
    if st.button(
        "📤 Notion에 동기화",
        width="stretch",
        disabled=not _has_notion_token,
        help=(
            "news.db의 신규 기사(최근 100건)를 Notion DB에 push합니다."
            if _has_notion_token
            else "NOTION_TOKEN 환경변수를 먼저 등록해 주세요."
        ),
    ):
        with st.spinner("Notion에 동기화 중..."):
            try:
                result = sync_notion.sync()
                if result["added"] == 0 and result["failed"] == 0:
                    st.info(result["message"])
                elif result["failed"] == 0:
                    st.success(f"완료 — {result['message']}")
                else:
                    st.warning(f"부분 성공 — {result['message']}")
                db.set_meta(
                    "last_notion_sync",
                    datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                )
            except sync_notion.NotionTokenMissing as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Notion 동기화 실패: {e}")
    if not _has_notion_token:
        st.caption(
            "🔑 Notion 동기화를 쓰려면 터미널에서 "
            "`[Environment]::SetEnvironmentVariable(\"NOTION_TOKEN\",\"secret_xxx\",\"User\")` "
            "실행 후 Streamlit 재시작."
        )
    _last_notion = db.get_meta("last_notion_sync")
    if _last_notion:
        st.caption(f"마지막 Notion 동기화: {fmt_dt(_last_notion)}")

    st.divider()
    st.subheader("📊 수집 현황")
    senti = db.sentiment_counts()
    stat_line("총 수집", db.total_count())
    stat_line("<b>오늘 수집</b>", db.today_count())
    stat_line("이번 회차 수집", db.get_meta("last_sync_total", "0"))
    stat_line("이번 회차 신규", db.get_meta("last_sync_new", "0"))
    _last_added = db.get_meta("last_notion_added")
    if _last_added is not None:
        _last_failed = db.get_meta("last_notion_failed", "0")
        _n_stat = f"{_last_added}"
        if int(_last_failed) > 0:
            _n_stat += f" (실패 {_last_failed})"
        stat_line("↳ Notion 자동 push", _n_stat)
    stat_line("<span class='senti-pos'>👍 긍정 기획(자동)</span>", senti.get("positive", 0))
    stat_line("<span class='senti-neg'>👎 부정 기획(자동)</span>", senti.get("negative", 0))
    stat_line("<span class='senti-neu'>· 중립</span>", senti.get("neutral", 0))

    st.subheader("📌 키워드별 기사 수")
    kcounts = db.keyword_counts(config.KEYWORDS)
    for k in config.KEYWORDS:
        stat_line(k, kcounts.get(k, 0))

    st.divider()
    st.subheader("🔎 필터")

    own_only = st.toggle(
        "🏢 자사 뉴스만 보기",
        value=False,
        help=f"켜면 키워드가 {' · '.join(OWN_KEYWORDS)} 중 하나 이상 포함된 기사만 표시합니다.",
    )

    _scope_labels = {
        "all": "전체",
        "today": "오늘 수집",
        "cycle_all": "이번 회차 수집",
        "cycle_new": "이번 회차 신규",
    }
    scope_key = st.radio(
        "표시 범위",
        list(_scope_labels.keys()),
        format_func=lambda k: _scope_labels[k],
        index=0,
        horizontal=False,
    )
    scope = None if scope_key == "all" else scope_key

    if own_only:
        st.caption(f"키워드 자동 고정: {' · '.join(OWN_KEYWORDS)}")
        kw = "(전체)"
    else:
        kw = st.selectbox("키워드", ["(전체)"] + config.KEYWORDS)

    press_options = db.distinct_press()
    press_sel = st.multiselect(
        "매체 선택",
        press_options,
        default=[],
        help="선택 없으면 모든 매체를 표시합니다.",
    )
    search = st.text_input("제목/요약 검색", "")


# --------------------------------------------------------------------------
# 저장 폼(긍정/부정 공통)
# --------------------------------------------------------------------------
def render_save_form(article):
    """선택된 기사를 편집 후 긍정/부정으로 저장하는 폼."""
    st.markdown(f"##### 선택한 기사")
    st.markdown(f"**[{article['title']}]({article['url']})**")

    default_dt = crawler.rel_to_datetime(article.get("pub_text", ""))
    # 기자명 자동 추출 캐시(세션)
    jkey = f"journalist::{article['url']}"
    if jkey not in st.session_state:
        with st.spinner("기자명 추출 중..."):
            st.session_state[jkey] = crawler.fetch_journalist(article["url"])

    with st.form(key=f"savef_{article['url']}", clear_on_submit=False):
        col1, col2 = st.columns(2)
        pub = col1.text_input("일시", value=default_dt)
        press = col2.text_input("매체", value=article.get("press", ""))
        col3, col4 = st.columns(2)
        journalist = col3.text_input("기자", value=st.session_state[jkey])
        title = col4.text_input("제목", value=article.get("title", ""))
        summary = st.text_area(
            "한줄요약", value=article.get("summary", ""), height=80
        )

        b1, b2 = st.columns(2)
        pos = b1.form_submit_button("👍 긍정 기획 저장", width="stretch")
        neg = b2.form_submit_button("👎 부정 기획 저장", width="stretch")

        if pos or neg:
            db.save_article(
                {
                    "sentiment": "positive" if pos else "negative",
                    "pub_datetime": pub,
                    "press": press,
                    "journalist": journalist,
                    "title": title,
                    "summary": summary,
                    "url": article["url"],
                    "saved_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            st.success(("긍정" if pos else "부정") + " 기획 기사로 저장했습니다.")
            st.rerun()


# --------------------------------------------------------------------------
# 저장 목록 렌더링
# --------------------------------------------------------------------------
def render_saved(sentiment, label):
    rows = db.get_saved(sentiment)
    st.caption(f"저장된 {label} 기획 기사: {len(rows)}건")
    if not rows:
        st.info(f"저장된 {label} 기획 기사가 없습니다.")
        return

    df = pd.DataFrame(rows)[
        ["pub_datetime", "press", "journalist", "title", "summary", "url"]
    ]
    df.columns = ["일시", "매체", "기자", "제목", "한줄요약", "링크"]
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "링크": st.column_config.LinkColumn("원문", display_text="열기"),
            "한줄요약": st.column_config.TextColumn("한줄요약", width="large"),
        },
    )

    # CSV 다운로드
    st.download_button(
        f"⬇️ {label} 목록 CSV 다운로드",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{sentiment}_기획기사.csv",
        mime="text/csv",
    )

    # 삭제
    del_id = st.selectbox(
        "삭제할 기사 선택",
        ["(선택 안 함)"] + [f"{r['id']} · {r['title'][:40]}" for r in rows],
        key=f"del_{sentiment}",
    )
    if del_id != "(선택 안 함)" and st.button("🗑️ 삭제", key=f"delbtn_{sentiment}"):
        db.delete_saved(int(del_id.split(" · ")[0]))
        st.rerun()


# --------------------------------------------------------------------------
# 메인 화면
# --------------------------------------------------------------------------
st.title("📰 네이버 뉴스 모니터링 대시보드")

_SENTI_LABEL = {"positive": "👍 긍정", "negative": "👎 부정", "neutral": "· 중립"}


def _query_kwargs(sentiment=None):
    """사이드바 필터 상태를 db.get_articles 인자로 변환."""
    kw_arg = None if kw == "(전체)" else kw
    return {
        "keyword": None if own_only else kw_arg,
        "keywords": OWN_KEYWORDS if own_only else None,
        "press_list": press_sel or None,
        "search": search or None,
        "scope": scope,
        "sentiment": sentiment,
    }


def render_press_chart(articles):
    """현재 뷰의 매체별 기사 수 상위 20개 막대 그래프."""
    if not articles:
        return
    df = pd.DataFrame(articles)
    if "press" not in df.columns:
        return
    counts = (
        df["press"].fillna("(미상)").replace("", "(미상)").value_counts().head(20)
    )
    if counts.empty:
        return
    st.markdown("##### 📊 매체별 기사 수 (현재 뷰 상위 20)")
    st.bar_chart(counts, height=280)


def render_article_table(articles, key_prefix):
    """감성 자동 분류 컬럼 포함 표 + 매체별 차트 + 선택 기사 저장 폼."""
    if not articles:
        st.info(
            "표시할 기사가 없습니다. 사이드바의 '지금 동기화'를 누르거나 "
            "자동 동기화를 기다려 주세요."
        )
        return

    df = pd.DataFrame(articles)
    df["자동분류"] = df["sentiment"].map(lambda s: _SENTI_LABEL.get(s, "· 중립"))
    df["저장"] = df["url"].map(
        lambda u: {"positive": "👍", "negative": "👎"}.get(db.is_saved(u), "")
    )
    view = df[
        ["자동분류", "저장", "pub_text", "press", "title", "keywords", "summary", "url"]
    ].copy()
    view.columns = ["자동분류", "저장", "일시", "매체", "제목", "키워드", "요약", "링크"]

    event = st.dataframe(
        view,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"tbl_{key_prefix}",
        column_config={
            "링크": st.column_config.LinkColumn("원문", display_text="열기"),
            "요약": st.column_config.TextColumn("요약", width="large"),
            "자동분류": st.column_config.TextColumn("자동분류", width="small"),
            "저장": st.column_config.TextColumn("저장", width="small"),
        },
    )
    render_press_chart(articles)
    st.divider()
    sel = event.selection.rows if event and event.selection else []
    if sel:
        render_save_form(articles[sel[0]])
    else:
        st.info("위 표에서 기사 행을 선택하면 긍정/부정 기획 기사로 저장할 수 있습니다.")


tab_feed, tab_pos, tab_neg, tab_saved = st.tabs(
    ["🗞️ 뉴스 피드", "👍 긍정 기획(자동)", "👎 부정 기획(자동)", "💾 저장된 기획"]
)

_scope_caption = _scope_labels.get(scope_key, "전체")

_kw_caption = "자사(키움증권·김익래·키움증권 김익래)" if own_only else kw
_press_caption = (
    ", ".join(press_sel) if press_sel else "(전체)"
)


with tab_feed:
    articles = db.get_articles(**_query_kwargs())
    st.caption(
        f"표시 중: {len(articles)}건 · 범위: {_scope_caption} · "
        f"키워드: {_kw_caption} · 매체: {_press_caption}"
    )
    render_article_table(articles, key_prefix="feed")

with tab_pos:
    articles = db.get_articles(**_query_kwargs(sentiment="positive"))
    st.caption(
        f"자동 분류된 긍정 기사: {len(articles)}건 · 범위: {_scope_caption} · "
        f"키워드: {_kw_caption} · 매체: {_press_caption}"
    )
    render_article_table(articles, key_prefix="pos")

with tab_neg:
    articles = db.get_articles(**_query_kwargs(sentiment="negative"))
    st.caption(
        f"자동 분류된 부정 기사: {len(articles)}건 · 범위: {_scope_caption} · "
        f"키워드: {_kw_caption} · 매체: {_press_caption}"
    )
    render_article_table(articles, key_prefix="neg")

with tab_saved:
    sub_pos, sub_neg = st.tabs(["👍 저장된 긍정", "👎 저장된 부정"])
    with sub_pos:
        render_saved("positive", "긍정")
    with sub_neg:
        render_saved("negative", "부정")
