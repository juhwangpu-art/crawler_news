# -*- coding: utf-8 -*-
"""단순 키워드 기반 기사 감성 분류.

제목과 요약 텍스트에서 긍정/부정 어휘 매칭 점수를 산정해
'positive' / 'negative' / 'neutral' 중 하나로 분류한다.
LLM 없이 즉시 동작 가능하며, 제목 매칭에 가중치 2배를 부여한다.
"""
import re

# --- 긍정 어휘 (증권/금융/실적/거버넌스 맥락) ---
POSITIVE = [
    "호실적", "신기록", "최고치", "최대", "역대", "최고", "1위",
    "상승", "급등", "강세", "반등", "돌파", "회복", "안정", "상향",
    "성장", "증가", "확대", "개선", "호조", "훈풍", "낙관", "기대",
    "흑자", "흑자전환", "이익", "순익", "순이익", "영업이익", "달성",
    "수혜", "매수", "매력", "유망", "선정", "표창", "포상", "시상",
    "체결", "협약", "제휴", "MOU", "인수", "출시", "신규 상장", "상장",
    "성공", "유치", "확장", "협업", "합작", "동반", "지원", "혁신",
    "브랜드 대상", "1위 차지", "우수", "글로벌 진출", "상승세",
]

# --- 부정 어휘 ---
NEGATIVE = [
    "하락", "급락", "폭락", "약세", "붕괴", "충격", "부진", "저조",
    "미달", "감소", "축소", "감자", "적자", "적자전환", "손실",
    "악화", "위기", "리스크", "부담", "우려", "경고", "충당금",
    "논란", "의혹", "혐의", "고발", "고소", "기소", "구속",
    "제재", "처벌", "행정처분", "과징금", "제소", "소송", "패소",
    "손해배상", "위반", "부실", "결함", "리콜", "취소", "취약",
    "낙마", "탈락", "실패", "지연", "차질", "논쟁",
    "압수수색", "수사", "조사", "감사",
    "먹튀", "폭탄", "주가조작", "시세조종", "불공정거래",
    "대량매도", "블록딜 논란", "책임론", "사퇴 요구",
]

_POS_RE = [re.compile(re.escape(w)) for w in POSITIVE]
_NEG_RE = [re.compile(re.escape(w)) for w in NEGATIVE]

# 감성 판정을 위한 최소 점수 격차 (동점/근소 차이는 중립 처리)
_MIN_MARGIN = 1


def _count(patterns, text):
    return sum(1 for p in patterns if p.search(text))


def classify(title="", summary=""):
    """반환: ('positive'|'negative'|'neutral', score_dict)."""
    title = title or ""
    summary = summary or ""
    # 제목 매칭에 가중치 2배
    pos = _count(_POS_RE, title) * 2 + _count(_POS_RE, summary)
    neg = _count(_NEG_RE, title) * 2 + _count(_NEG_RE, summary)

    if pos - neg >= _MIN_MARGIN:
        label = "positive"
    elif neg - pos >= _MIN_MARGIN:
        label = "negative"
    else:
        label = "neutral"
    return label, {"pos": pos, "neg": neg}


if __name__ == "__main__":
    tests = [
        ("키움증권, 3분기 사상 최대 영업이익 달성", "역대 최고 실적"),
        ("김익래 회장 주가조작 의혹 수사 확대", "압수수색"),
        ("코스피, 5%대 급락 8,000선 붕괴", "충격"),
        ("금융위, 신규 제도 시행", ""),
    ]
    for t, s in tests:
        print(classify(t, s), "|", t)
