# 📰 News Crawler → Notion (Cloud Automation)

GitHub Actions cron으로 30분마다 네이버 뉴스를 수집하고 Notion 데이터베이스와
요약 대시보드 페이지를 자동으로 갱신하는 헤드리스 파이프라인.

- 로컬 SQLite·Streamlit·수동 실행 없음
- 상태는 Notion을 원본으로 사용 (dedup은 매 실행마다 Notion 조회)
- 로컬 PC 꺼져 있어도 계속 동작

로컬에서 Streamlit 대시보드로 열람만 하고 싶다면 별도 폴더에서 관리
(이 리포지토리는 클라우드 전용).

## 아키텍처

```
GitHub Actions cron (30분)
  → run_headless.py
      → Notion 조회 (기존 URL·통계)
      → 네이버 크롤 (14 keywords)
      → 신규만 Notion pages.create
      → sync_notion.update_summary_page() 로 대시보드 갱신
```

## 필요한 환경 (GitHub Actions Secrets)

| 이름 | 값 | 설명 |
|---|---|---|
| `NOTION_TOKEN` | `ntn_...` | Notion Internal Integration secret |
| `NOTION_DB_ID` | UUID | 뉴스 DB ID |
| `NOTION_SUMMARY_PAGE_ID` | UUID | 요약 대시보드가 그려질 페이지 ID |

세 개 다 필수. 미설정 시 워크플로우 실행 즉시 명확한 에러.

## 구성 파일

| 파일 | 역할 |
|---|---|
| `run_headless.py` | 크론 entry point. 크롤 + Notion push + 요약 갱신 |
| `crawler.py` | 네이버 뉴스 검색 스크래핑 (봇 차단 대응 포함) |
| `sentiment.py` | 룰 기반 감성 분류 |
| `sync_notion.py` | Notion API 래퍼 (build_page, fetch_all_articles, aggregate_stats, update_summary_page) |
| `config.py` | 추적 키워드, 그룹 색상, 크롤 주기 설정 |
| `.github/workflows/crawl.yml` | GitHub Actions 크론 (`*/30 * * * *`) |

## 로컬에서 개발·테스트

```powershell
$env:NOTION_TOKEN = "ntn_..."
$env:NOTION_DB_ID = "..."
$env:NOTION_SUMMARY_PAGE_ID = "..."
python run_headless.py            # 전체 파이프라인 로컬 실행
python sync_notion.py             # 요약 페이지만 갱신
```

## 요약 페이지 안전 규칙

`sync_notion.update_summary_page()`는 요약 페이지의 특정 sentinel 두 개 사이만 재생성:
- 시작 sentinel: `📊 자동 갱신 통계 (아래는 스크립트가 관리)`
- 끝 sentinel: `🔒 자동 갱신 영역 끝 (여기까지 스크립트 관리)`

두 sentinel 사이 밖의 블록은 절대 건드리지 않음. `child_database`, `child_page`,
`link_to_page` 등 원본 데이터를 참조하는 타입은 삭제 대상에서 완전 제외.

## Notion 스키마 요구

DB에 아래 property 필수 (정확한 이름·타입):
- `제목` (title), `감성` (select), `매체` (rich_text)
- `키워드` (multi_select), `발행` (rich_text)
- `수집시각` (date), `링크` (url)

## 확장 지점

- 키워드 추가/변경: `config.py`의 `KEYWORDS` 편집
- 크롤 주기 변경: `.github/workflows/crawl.yml`의 cron 표현식
- 그룹 색상: `config.py`의 `KEYWORD_COLORS`
