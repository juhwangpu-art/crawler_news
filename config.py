# -*- coding: utf-8 -*-
"""대시보드 설정: 검색 키워드와 동기화 주기."""

# 네이버 뉴스에서 추적할 키워드
KEYWORDS = [
    "키움증권",
    "키움증권 김익래",
    "김익래",
    "정무위",
    "금융위",
    "금감원",
    "한국거래소",
    "넥스트레이드",
    "금융투자협회",
    "단독증권",
    "증권",
    "단독 금융",
    "금융",
    "자산운용",
]

# 자동 동기화 주기(초). 30분 = 1800초
SYNC_INTERVAL_SEC = 30 * 60

# 키워드당 수집할 최대 기사 수
MAX_PER_KEYWORD = 30

# 요청 사이 대기(초) — 과도한 요청 방지 (봇 차단 회피용, 실제로는 ±지터 적용)
REQUEST_DELAY_SEC = 1.5

# Notion multi-select 옵션 색상. 그룹별로 같은 색을 할당해서
# DB에서 어떤 카테고리인지 시각적으로 구분되도록 한다.
# 유효 색상: default, gray, brown, orange, yellow, green, blue, purple, pink, red
KEYWORD_COLORS = {
    # 자사 관련 — red (최고 강조)
    "키움증권": "red",
    "키움증권 김익래": "red",
    "김익래": "red",
    # 규제·정치 — purple
    "정무위": "purple",
    "금융위": "purple",
    "금감원": "purple",
    # 시장·인프라 — blue
    "한국거래소": "blue",
    "넥스트레이드": "blue",
    "금융투자협회": "blue",
    # 증권 (자본시장) — orange
    "단독증권": "orange",
    "증권": "orange",
    # 금융 전반 — green
    "단독 금융": "green",
    "금융": "green",
    "자산운용": "green",
}
