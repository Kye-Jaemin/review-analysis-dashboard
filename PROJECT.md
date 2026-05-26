# 리뷰 수집·분석 대시보드 — 프로젝트 명세서

> **Claude Code 작업 지시서**
> 이 문서는 Claude Code가 프로젝트를 빌드할 때 참조하는 단일 진실 소스(single source of truth)다. 명세 변경 시 이 파일을 우선 갱신한다.

---

## 1. 프로젝트 개요

여러 채널(Google Play, Apple App Store, Reddit, 임의 리뷰 사이트)에서 사용자 리뷰를 수집하고, 사용자가 정의한 **계층형 카테고리**로 자동 분류하며, 각 리뷰의 **긍정/부정/중립**을 평가해 대시보드로 시각화하는 단일 사용자 웹앱.

- **사용자**: 1명(개인용). 인증·로그인 없음.
- **수집 방식**: 사용자가 UI에서 "수집 시작" 버튼을 누를 때만 트리거 (스케줄러 없음).
- **분석 방식**: 수집 완료 후 사용자가 "분석 실행" 버튼을 눌러 LLM으로 카테고리 분류 + 감성 평가를 일괄 수행.
- **출력**: 대시보드(차트·테이블) + CSV/JSON 추출.

---

## 2. 핵심 기능 요구사항

### F1. 리뷰 수집 소스

| 소스 | 입력 | 라이브러리 |
|---|---|---|
| Google Play | **앱 이름**(검색), 국가, 언어, 최대 개수 → 검색 결과에서 사용자가 선택 → 내부적으로 패키지명 저장 | `google-play-scraper` (`search`, `reviews`) |
| Apple App Store | **앱 이름**(검색), 국가, 최대 개수 → 검색 결과에서 사용자가 선택 → 내부적으로 숫자 ID 저장 | `app-store-scraper` + iTunes Search API |
| Reddit | 서브레딧 이름(자동완성 지원), 검색 키워드(선택), 정렬(`new`/`top`/`hot`), 기간, 최대 개수 | `praw` (공식 OAuth API, **웹 크롤링 금지**) |
| 사용자 지정 URL | URL 목록, CSS 선택자 또는 자동 추출(readability) | `requests` + `beautifulsoup4` + `readability-lxml`; 동적 페이지는 `playwright` |

**소스 추가 UX 원칙**: 사용자는 식별자(패키지명·숫자 ID)를 몰라도 된다. 이름으로 검색 → 아이콘·개발자명이 함께 표시된 후보 목록에서 선택 → 시스템이 ID를 저장. 이후 UI에서는 항상 앱 이름·아이콘으로 표시.

각 소스마다 **수집 작업(CollectionJob)** 단위로 실행 이력을 남기고, 중복은 `(source, external_id)` 유니크로 제거.

### F2. 계층형 카테고리

- 사용자가 **트리 구조**로 카테고리를 정의 (예: `UX > 온보딩`, `UX > 결제`, `성능 > 속도`, `성능 > 크래시`).
- 깊이 제한 없음 (실용상 3단계까지 권장).
- 카테고리 CRUD UI 제공.
- 카테고리에는 **설명 필드**가 있어 LLM이 분류 기준으로 사용한다.

### F3. AI 분류 + 감성 평가

- **단일 LLM 호출**로 한 리뷰에 대해 다음을 한 번에 산출:
  - 가장 적합한 리프(leaf) 카테고리 1개 + 신뢰도(0~1)
  - 감성 5단계: `very_positive` / `positive` / `neutral` / `negative` / `very_negative`
    - `very_positive`: 강한 호평·추천 (예: "최고", "정말 사랑한다", "Best app ever")
    - `positive`: 일반적 만족 (예: "괜찮다", "좋다", "works well")
    - `neutral`: 사실 진술·질문·혼재된 의견·감정 약함
    - `negative`: 일반적 불만 (예: "별로", "아쉽다", "not great")
    - `very_negative`: 강한 비난·분노·삭제 의사 (예: "최악", "쓰레기", "uninstalling", "scam")
  - 감성 점수: `sentiment_score` 정수 `1~5` (1=very_negative, 5=very_positive) — 차트 평균·집계용으로 함께 저장
  - 한 줄 요약(선택)
- **모델 선택**: 분석 실행 화면(`/analyze`)에서 사용자가 매번 모델을 고를 수 있다.
  - 선택지는 `ANTHROPIC_ALLOWED_MODELS` 환경변수(콤마 구분)로 정의.
  - 기본 후보: `claude-haiku-4-5-20251001` (대량·저비용), `claude-sonnet-4-6` (균형), `claude-opus-4-7` (최고 정확도).
  - 드롭다운에 모델명 + 간단한 설명(속도/비용/정확도) 표시.
  - 마지막 선택은 쿠키에 저장해 다음 실행 시 기본값으로.
- 배치 처리: 한 번에 N개씩 묶어 비동기 호출 (`asyncio.gather`, 동시성 제한).
- 실패한 리뷰는 `analysis_status='failed'`로 표시하고 재시도 가능.
- 분석 레코드(`Analysis.model`)에 실제 사용 모델 ID를 저장해 추후 비교 가능.

### F4. 대시보드

- **요약 카드**: 총 리뷰 수, 5단계 감성 분포(각 단계별 건수·비율), **평균 감성 점수(1~5)**, 평균 별점(있는 경우), 최근 수집일.
- **카테고리별 분포**: 막대 차트(카테고리 × 건수, 5단계 감성 색상 스택). 색상 권장: very_positive=진녹, positive=연녹, neutral=회색, negative=연주황, very_negative=진빨강.
- **시간 추이**: 선 차트(주/월 단위). 두 가지 모드 토글:
  - "5단계 분포": 5개 라인 (또는 100% 스택 영역)
  - "평균 점수": 단일 라인 (1~5 스케일)
- **소스별 분포**: 도넛 차트 + 소스별 평균 감성 점수 보조 표시.
- **리뷰 테이블**: 필터(소스/카테고리/**감성 5단계 다중 선택**/기간/키워드) + 페이지네이션 + 원문 보기.

### F5. 데이터 추출

- 현재 필터 상태 그대로 **CSV / JSON / Excel(xlsx)** 다운로드.
- 컬럼: `id, source, external_id, author, posted_at, rating, text, category_path, sentiment, sentiment_score, confidence, summary, collected_at, analyzed_at`.

---

## 3. 기술 스택

| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.11 | |
| 웹 프레임워크 | **FastAPI** | async 친화, OpenAPI 자동 생성 |
| 템플릿 | Jinja2 | 별도 SPA 빌드 없이 SSR |
| 인터랙션 | HTMX + Alpine.js | 가벼운 동적 UI (페이지 새로고침 최소) |
| 차트 | Chart.js (CDN) | |
| ORM | SQLAlchemy 2.0 (async) + Alembic | |
| DB | 로컬: SQLite / 운영(Render): PostgreSQL | `DATABASE_URL` 환경변수로 분기 |
| LLM | Anthropic Python SDK (`anthropic`) | |
| 스크래핑 | `google-play-scraper`, `app-store-scraper`, `praw`, `requests`, `beautifulsoup4`, `readability-lxml`, **`playwright`** (동적 페이지) | |
| i18n | 간단한 사전 기반 + 쿠키 저장 (또는 `Babel`) | UI 언어 전환 (en/ko) |
| 백그라운드 작업 | FastAPI `BackgroundTasks` + 인메모리 작업 레지스트리 | 단일 사용자라 Celery·Redis 불필요 |
| 패키지 관리 | `uv` (또는 `pip` + `requirements.txt`) | Render 호환성 위해 `requirements.txt` 유지 |
| 테스트 | `pytest` + `pytest-asyncio` | |
| 린트/포맷 | `ruff` | |

---

## 4. 시스템 아키텍처

```
[Browser]
   │ HTTP/HTMX
   ▼
[FastAPI 앱]
   ├── routes/        라우터 (pages, api)
   ├── services/      비즈니스 로직
   │     ├── collectors/   (google_play, app_store, reddit, web)
   │     ├── analyzer.py   (Claude API 호출, 카테고리 분류 + 감성)
   │     └── exporter.py   (CSV/JSON/XLSX)
   ├── models/        SQLAlchemy 모델
   ├── jobs.py        백그라운드 작업 레지스트리(in-memory dict)
   └── templates/     Jinja2

[DB] PostgreSQL (운영) / SQLite (로컬)
[External] Anthropic API, Google Play, App Store, Reddit API, 임의 웹사이트
```

수집·분석은 **요청 → 작업 ID 발급 → 백그라운드 실행 → 폴링(HTMX `hx-trigger="every 2s"`)으로 진행률 표시** 패턴.

---

## 5. 데이터 모델

```python
# 의사 SQLAlchemy

class Source(Base):
    id: int (pk)
    type: Enum("google_play", "app_store", "reddit", "web")
    label: str                      # 사용자가 정한 별칭 (예: "Spotify - Android")
    display_name: str | None        # 앱·서브레딧의 실제 이름 (검색 결과에서 자동 채움)
    icon_url: str | None            # 앱 아이콘·서브레딧 아바타 URL (UI 표시용)
    config: JSON                    # 소스별 설정 (resolved_id, country, lang, subreddit 등)
    created_at: datetime

class CollectionJob(Base):
    id: int (pk)
    source_id: FK(Source)
    status: Enum("pending", "running", "succeeded", "failed")
    started_at, finished_at: datetime
    fetched_count, new_count: int
    error: str | None

class Review(Base):
    id: int (pk)
    source_id: FK(Source)
    external_id: str                # 소스 내 고유 ID (중복 방지)
    author: str | None
    posted_at: datetime
    rating: float | None            # 1~5 (별점 있는 소스만)
    text: str
    url: str | None
    raw: JSON                       # 원본 페이로드 보존
    collected_at: datetime
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

class Category(Base):
    id: int (pk)
    parent_id: FK(Category) | None  # 자기참조로 트리
    name: str
    description: str                # LLM 분류 기준 텍스트
    path: str                       # "UX > 결제" 형태 캐시 (자동 갱신)

class Analysis(Base):
    id: int (pk)
    review_id: FK(Review, unique=True)
    category_id: FK(Category) | None
    sentiment: Enum("very_positive", "positive", "neutral", "negative", "very_negative")
    sentiment_score: int            # 1~5 (1=very_negative, 5=very_positive). 집계·차트용
    confidence: float
    summary: str | None
    model: str                      # 사용한 모델 ID
    analyzed_at: datetime
    status: Enum("succeeded", "failed")
    error: str | None

class AnalysisJob(Base):
    id, status, started_at, finished_at, processed_count, failed_count, error
```

---

## 6. 수집 소스별 구현 가이드

### 6.1 Google Play

**검색 단계** (소스 추가 시):
```python
from google_play_scraper import search
candidates = search(query, lang="en", country="us", n_hits=10)
# 반환: [{"appId": "com.spotify.music", "title": "Spotify", "icon": "...", "developer": "Spotify AB", "score": 4.4, ...}, ...]
```
UI는 후보 카드를 보여주고 사용자가 클릭 → `Source.config = {"app_id": "com.spotify.music", "country": "us", "lang": "en"}` 저장. `display_name`, `icon_url`도 함께 캐시.

**수집 단계**:
```python
from google_play_scraper import reviews, Sort
result, _ = reviews(
    app_id=config["app_id"],
    lang=config["lang"], country=config["country"],
    sort=Sort.NEWEST, count=max_count,
)
# external_id = item["reviewId"]
# rating = item["score"], posted_at = item["at"], text = item["content"]
```

### 6.2 Apple App Store

**검색 단계** — iTunes Search API (공식, 키 불필요):
```python
import httpx
resp = httpx.get(
    "https://itunes.apple.com/search",
    params={"term": query, "country": "us", "media": "software", "limit": 10},
).json()
# resp["results"] = [{"trackId": 324684580, "trackName": "Spotify - Music and Podcasts",
#                     "artworkUrl100": "...", "artistName": "Spotify Ltd.",
#                     "averageUserRating": 4.8, ...}, ...]
```
UI는 후보 카드를 보여주고 사용자가 클릭 → `Source.config = {"app_id": 324684580, "app_name": "Spotify", "country": "us"}` 저장. `app_store_scraper`가 `app_name`도 요구하므로 함께 저장.

**수집 단계**:
```python
from app_store_scraper import AppStore
app = AppStore(country=config["country"], app_name=config["app_name"], app_id=config["app_id"])
app.review(how_many=max_count)
# external_id = sha1(item["userName"] + str(item["date"]) + item["title"])  ← 안정적 해시
# rating = item["rating"], posted_at = item["date"], text = item["review"]
```
별점 있음 (`rating`).

### 6.3 Reddit — **공식 API 전용 (PRAW)**

> ⚠️ **웹 크롤링 금지**. Reddit은 `old.reddit.com` 포함 모든 페이지에서 비공식 스크래핑을 차단하며 약관 위반이다. 반드시 OAuth 기반 공식 API(PRAW)로만 접근한다. `requests`로 `reddit.com/r/.../.json` 같은 엔드포인트를 호출하는 코드도 금지.

**라이브러리**: `praw` (Python Reddit API Wrapper, 공식 클라이언트 래퍼).

**Reddit 앱 등록 절차** (최초 1회):
1. https://www.reddit.com/prefs/apps 접속
2. "create another app..." 클릭
3. 타입: **script** 선택 (개인용·읽기 전용에 적합)
4. name: 임의, redirect uri: `http://localhost:8080` (script 타입에선 미사용이지만 필수 입력)
5. 발급되는 `client_id`(앱 이름 아래 문자열)와 `secret`을 `.env`에 저장
6. `user_agent`는 Reddit 규약상 고유 문자열 필수: `"<platform>:<app_name>:v<version> (by /u/<username>)"` 형태 권장

**환경변수**:
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_USER_AGENT` (예: `web:review-collector:v0.1 (by /u/yourname)`)

**클라이언트 초기화** (read-only 모드, 로그인 불필요):
```python
import praw
reddit = praw.Reddit(
    client_id=settings.REDDIT_CLIENT_ID,
    client_secret=settings.REDDIT_CLIENT_SECRET,
    user_agent=settings.REDDIT_USER_AGENT,
)
reddit.read_only = True
```

**서브레딧 검색** (소스 추가 시 자동완성용):
```python
# 이름으로 부분 일치 검색
results = list(reddit.subreddits.search_by_name(query, include_nsfw=False, exact=False))
# 또는 일반 검색 (구독자 수·설명 함께)
results = list(reddit.subreddits.search(query, limit=10))
# 각 항목에서 display_name, subscribers, public_description, community_icon 추출
```
UI는 후보를 보여주고 사용자가 선택 → `Source.config = {"subreddit": "spotify", ...}` 저장. 아이콘은 `community_icon` 또는 `icon_img` 사용.

**수집 대상**:
- 서브레딧의 **submission**(글 제목 + 본문)
- 각 submission의 **comment** (상위 N개, `submission.comments.replace_more(limit=0)` 후 평탄화)
- `external_id`: submission은 `submission.id` (예: `t3_abc123`), comment는 `comment.id` (예: `t1_xyz789`) — 접두사 포함해 충돌 방지
- `rating = None` (Reddit은 별점 없음. score(upvote)는 `raw` JSON에 보존)

**입력 파라미터** (`Source.config`):
```json
{
  "subreddit": "spotify",
  "search_query": "premium",          // 선택. 없으면 서브레딧 전체
  "sort": "new",                       // new | top | hot | relevance
  "time_filter": "month",              // top/relevance일 때: hour|day|week|month|year|all
  "max_submissions": 50,
  "include_comments": true,
  "max_comments_per_submission": 20
}
```

**Rate Limit 처리**:
- OAuth 인증 시 **분당 100 요청, 10분 롤링 윈도우 평균 기준** (즉 10분 동안 총 1,000건까지 가능, 짧은 버스트 허용. 한 번에 윈도우 초과 시 429).
- PRAW가 응답 헤더(`X-Ratelimit-Remaining`, `X-Ratelimit-Reset`)를 읽어 자동으로 throttle하지만, 작업 단위에서도 `submission` 사이에 짧은 sleep(0.5초) 두기.
- 429 응답 시 지수 백오프로 재시도(최대 3회).
- 본 앱은 사용자 트리거 방식 + 한 번에 수십~수백 건 수준이라 한도에 닿을 가능성 낮음.

**상업적 사용 주의**: Reddit API는 개인·연구용은 무료지만 대량 상업 이용은 유료 티어 필요. 본 프로젝트는 개인용이므로 무료 한도 내 사용 명시.

### 6.4 사용자 지정 URL

- `urls: list[str]` 입력. URL별로 다음 옵션:
  - `dynamic`: `false`(기본, 정적 HTML) | `true`(Playwright로 렌더링 후 파싱)
  - `wait_for`: 동적 모드에서 특정 선택자가 나타날 때까지 대기 (예: `.review-item`)
  - `scroll`: 동적 모드에서 무한 스크롤 페이지를 위해 N회 스크롤 (기본 0)
- 처리 흐름:
  1. **정적 모드**: `requests` → `readability-lxml` 본문 추출 (실패 시 BeautifulSoup fallback)
  2. **동적 모드**: `playwright` (Chromium, headless) → `page.goto()` → `wait_for_selector()` → 필요 시 스크롤 → `page.content()`로 HTML 회수 후 BeautifulSoup 파싱
  3. **선택자 모드**: 사용자가 `item_selector`, `text_selector`, `author_selector`, `date_selector`, `rating_selector`를 제공하면 그걸 우선. 미제공이면 휴리스틱으로 반복 블록 탐지.
- `external_id`는 `sha1(url + 본문 첫 200자)`.
- robots.txt 존중: 수집 전 `robotparser`로 확인하고 비허용 시 스킵 + 로그.
- 요청 간격: 같은 도메인에 대해 최소 1초 sleep (예의·차단 방지).
- User-Agent는 환경변수로 설정 가능 (`SCRAPER_USER_AGENT`, 기본은 봇임을 명시하는 문자열).

**Playwright 런타임 요구사항**:
- Render 빌드 시 `playwright install chromium --with-deps` 추가 필요 → 빌드 시간·이미지 용량 증가.
- 메모리 사용량 큼 (Render 무료 플랜 512MB에서 빠듯) → 사용 후 즉시 `browser.close()`.
- 동적 모드는 URL당 5~15초 소요 → 작업 진행률 UI에 반영.

---

## 7. AI 분석 명세

### 7.1 프롬프트 구조

시스템 프롬프트에 카테고리 트리와 5단계 감성 기준을 직렬화해 전달:

```
다음은 사용자가 정의한 카테고리 트리다. 각 리프 카테고리에는 분류 기준이 있다.

- UX
  - 온보딩: 가입·튜토리얼·첫 사용 경험 관련
  - 결제: 구독·결제·환불 관련
- 성능
  - 속도: 로딩·반응 속도 관련
  - 크래시: 앱 종료·오류 관련
- 기타: 위에 해당하지 않는 모든 리뷰

감성은 다음 5단계 중 하나로 분류한다:
- very_positive (5): 강한 호평, 추천, 열렬한 만족
- positive (4): 일반적 만족, 약한 긍정
- neutral (3): 사실 진술, 질문, 의견 혼재, 감정 약함
- negative (2): 일반적 불만, 약한 부정
- very_negative (1): 강한 비난, 분노, 삭제·환불 의사, 사기 주장

각 리뷰에 대해 다음 JSON으로만 응답:
{
  "category_path": "UX > 결제",
  "sentiment": "very_negative",
  "sentiment_score": 1,
  "confidence": 0.87,
  "summary": "환불을 요청했지만 무시당해 매우 화남"
}
```

배치 처리: 한 호출에 리뷰 5~10개를 묶어 JSON 배열로 응답 요구. 토큰 한도 초과 방지 위해 리뷰 텍스트는 1,500자로 절단.

### 7.2 응답 검증

- 카테고리 경로가 존재하는 리프인지 확인. 없으면 `category_id = None`, `status="failed"`로 저장.
- `sentiment ∈ {very_positive, positive, neutral, negative, very_negative}` 검증.
- `sentiment_score ∈ {1,2,3,4,5}` 검증. `sentiment`와 `sentiment_score`가 매핑 표에 맞는지 일관성 체크 (불일치 시 라벨 우선, 점수는 라벨에서 재계산).
- `confidence ∈ [0,1]` 검증.

### 7.3 재실행

"미분석 리뷰만 분석" / "실패만 재시도" / "전체 재분석"(카테고리 트리 변경 후) 3개 모드.

---

## 8. API 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/` | 대시보드 |
| GET | `/sources` | 소스 목록 |
| GET | `/sources/search` | `type` + `query` + `country` → 후보 목록 반환 (Google Play / App Store / Reddit) |
| POST | `/sources` | 소스 추가 (검색 결과에서 선택된 항목 + 별칭) |
| DELETE | `/sources/{id}` | 소스 삭제 |
| POST | `/sources/{id}/collect` | 수집 작업 시작 → `job_id` 반환 |
| GET | `/jobs/{job_id}` | 작업 진행 상태(HTMX 폴링용 부분 HTML 또는 JSON) |
| GET | `/categories` | 카테고리 트리 조회 |
| POST | `/categories` | 카테고리 추가 |
| PATCH | `/categories/{id}` | 수정 |
| DELETE | `/categories/{id}` | 삭제(자식 포함 확인) |
| POST | `/analyze` | 분석 작업 시작 (`mode`: `unanalyzed`/`failed`/`all`) |
| GET | `/reviews` | 리뷰 테이블 (필터 쿼리 파라미터) |
| GET | `/reviews/{id}` | 리뷰 상세 |
| GET | `/export` | `format=csv|json|xlsx` + 동일 필터 |
| GET | `/api/stats` | 대시보드 차트용 집계 JSON |

---

## 9. UI / 화면 명세

**기본 언어는 영어**, 우상단 언어 토글로 한국어 전환. 모든 페이지 공통.

1. **대시보드** (`/`): 요약 카드 + 4개 차트 + 최근 리뷰 미리보기 10개.
2. **소스 관리** (`/sources`): 소스별 카드(아이콘 + 앱·서브레딧 이름 + 별칭 + 마지막 수집 시각·건수), "수집 시작" 버튼. 진행 중이면 진행률 바.
   - **소스 추가 플로우**:
     1. 소스 타입 선택(Google Play / App Store / Reddit / URL)
     2. 앱·서브레딧 타입이면: 검색창에 이름 입력 → HTMX로 디바운스(300ms) 후 `/sources/search` 호출 → 후보 카드 목록 표시(아이콘·이름·개발자/구독자 수·평점)
     3. 사용자가 후보 클릭 → 미리보기에서 별칭·국가·언어·최대 개수 조정 → "추가" 클릭
     4. URL 타입이면: URL 목록·선택자·동적 옵션을 직접 입력하는 폼
3. **카테고리 관리** (`/categories`): 트리 뷰 + 드래그로 부모 이동(선택). 각 노드에 설명 입력.
4. **분석** (`/analyze`): "미분석 N건 / 실패 M건 / 전체 K건" 표시, **모델 선택 드롭다운**, 모드 선택 후 실행. 진행률 표시.
5. **리뷰 탐색** (`/reviews`): 필터 사이드바 + 테이블 + 내보내기 버튼.

레이아웃: 좌측 사이드바 내비 + 메인 콘텐츠. 상단바에 **언어 토글(EN / 한국어)** 배치. Tailwind CDN으로 스타일링.

---

## 9.1 국제화 (i18n)

- **지원 언어**: 영어(`en`, 기본), 한국어(`ko`).
- **저장**: 사용자 선택은 `lang` 쿠키(1년)에 저장. 쿠키 없으면 `Accept-Language` 헤더로 추정, 그것도 없으면 영어.
- **구현**: 간단한 사전 기반 (Python dict, `app/i18n/en.json`, `app/i18n/ko.json`). Jinja2에 `t("key")` 글로벌 함수 등록.
  - 키 네이밍: `dashboard.title`, `sources.add_button`, `reviews.filter.sentiment` 형태.
- **라우팅**: URL에는 언어 코드를 넣지 않음 (쿠키만 사용). 단일 사용자라 SEO 불필요.
- **번역 대상**:
  - UI 라벨, 버튼, 메뉴, 에러 메시지, 차트 축 제목.
  - 카테고리 이름·설명은 사용자 입력값이므로 번역하지 않음 (입력 그대로 표시).
- **데이터 언어와 분리**: 수집된 리뷰 본문은 원문 그대로 저장·표시. UI 언어 변경이 데이터에 영향을 주지 않음.
- **AI 분석 출력 언어**:
  - 분석은 영어 리뷰가 다수이므로 **`summary`는 기본 영어**로 생성.
  - `/analyze` 화면에 `요약 언어: English / 한국어 / Auto(리뷰 언어 따라)` 선택 옵션 제공.
  - 선택값은 분석 프롬프트에 명시적으로 포함.

---

## 10. 디렉터리 구조

```
.
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI 앱 진입점
│   ├── config.py              # Pydantic Settings
│   ├── db.py                  # 엔진·세션
│   ├── models/
│   │   ├── __init__.py
│   │   ├── source.py
│   │   ├── review.py
│   │   ├── category.py
│   │   └── analysis.py
│   ├── routes/
│   │   ├── pages.py           # SSR 페이지
│   │   ├── sources.py
│   │   ├── categories.py
│   │   ├── reviews.py
│   │   ├── analyze.py
│   │   └── export.py
│   ├── services/
│   │   ├── collectors/
│   │   │   ├── base.py
│   │   │   ├── google_play.py
│   │   │   ├── app_store.py
│   │   │   ├── reddit.py
│   │   │   └── web.py
│   │   ├── analyzer.py
│   │   ├── exporter.py
│   │   └── stats.py
│   ├── jobs.py                # 인메모리 작업 레지스트리
│   ├── i18n/
│   │   ├── __init__.py        # t() 함수, 언어 감지
│   │   ├── en.json
│   │   └── ko.json
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── sources.html
│       ├── categories.html
│       ├── reviews.html
│       └── partials/
├── alembic/                   # 마이그레이션
├── tests/
├── static/
├── .env.example
├── .gitignore
├── requirements.txt
├── render.yaml                # Render Blueprint
├── Procfile                   # Render fallback
├── README.md
└── PROJECT.md                 # 이 파일
```

---

## 11. 환경 변수 (`.env.example`)

```
# 데이터베이스
DATABASE_URL=sqlite+aiosqlite:///./local.db
# 운영: postgresql+asyncpg://user:pass@host:5432/db

# Anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
# 사용자가 UI에서 고를 수 있는 모델 후보 (콤마 구분)
ANTHROPIC_ALLOWED_MODELS=claude-haiku-4-5-20251001,claude-sonnet-4-6,claude-opus-4-7

# Reddit (공식 API - PRAW)
# https://www.reddit.com/prefs/apps 에서 'script' 타입 앱 생성
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=web:review-collector:v0.1 (by /u/yourname)

# 웹 스크래퍼
SCRAPER_USER_AGENT=review-collector-bot/0.1 (+contact: you@example.com)
PLAYWRIGHT_ENABLED=true

# UI
DEFAULT_LANGUAGE=en          # en | ko

# 앱
APP_ENV=development
LOG_LEVEL=INFO
ANALYSIS_BATCH_SIZE=8
ANALYSIS_CONCURRENCY=4
```

---

## 12. 배포 — Render.com

### 12.1 `render.yaml` (Blueprint)

```yaml
services:
  - type: web
    name: review-dashboard
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt && playwright install chromium --with-deps && alembic upgrade head
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: review-db
          property: connectionString
      - key: ANTHROPIC_API_KEY
        sync: false
      - key: ANTHROPIC_MODEL
        value: claude-haiku-4-5-20251001
      - key: ANTHROPIC_ALLOWED_MODELS
        value: claude-haiku-4-5-20251001,claude-sonnet-4-6,claude-opus-4-7
      - key: REDDIT_CLIENT_ID
        sync: false
      - key: REDDIT_CLIENT_SECRET
        sync: false
      - key: REDDIT_USER_AGENT
        sync: false
      - key: SCRAPER_USER_AGENT
        value: review-collector-bot/0.1
      - key: PLAYWRIGHT_ENABLED
        value: "true"
      - key: DEFAULT_LANGUAGE
        value: en
      - key: APP_ENV
        value: production

databases:
  - name: review-db
    plan: free
    databaseName: reviews
    user: reviews
```

### 12.2 주의사항

- Render 무료 플랜은 일정 시간 idle이면 슬립 → 첫 요청 지연. 백그라운드 작업이 인메모리이므로 슬립 중 작업은 손실됨 → **수집/분석은 사용자가 직접 트리거**라는 본 명세와 합치.
- 무료 PostgreSQL은 90일 후 삭제됨(Render 정책 확인 필요).
- **Playwright 메모리 압박**: Chromium은 무료 플랜 512MB에서 빠듯하다. 동적 수집 시 OOM 발생 가능 → 한 번에 1개 URL씩 처리, 끝나면 즉시 `browser.close()`. OOM이 잦으면 `PLAYWRIGHT_ENABLED=false`로 끄거나 Starter 플랜($7/mo)으로 업그레이드 권장.
- Playwright 설치로 빌드 시간 5~10분 증가 가능.

---

## 13. 로컬 ↔ GitHub 동기화

### 13.1 원칙

- `main` 브랜치 = 배포 가능한 상태. Render의 자동 배포가 `main`에 연결됨.
- 기능 작업은 `feat/<짧은-이름>` 브랜치에서. PR로 머지(혼자 작업이어도 변경 이력을 위해 PR 권장).
- `main`에 푸시되면 Render가 자동 배포.

### 13.2 `.gitignore` 필수 항목

```
.venv/
__pycache__/
*.pyc
.env
local.db
local.db-journal
.pytest_cache/
.ruff_cache/
node_modules/
.DS_Store
```

### 13.3 로컬 셋업

```bash
git clone <repo>
cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 키 채우기
alembic upgrade head
uvicorn app.main:app --reload
```

### 13.4 권장 워크플로우

1. `git pull origin main`
2. `git checkout -b feat/...`
3. 작업·테스트 (`pytest`, `ruff check .`)
4. 커밋 메시지: Conventional Commits (`feat:`, `fix:`, `chore:`)
5. `git push -u origin feat/...` → GitHub에서 PR → 머지
6. Render가 `main` 변경 감지 → 자동 빌드·배포
7. 마이그레이션이 있으면 `render.yaml`의 `buildCommand`가 `alembic upgrade head` 실행

### 13.5 GitHub Actions (선택)

PR마다 `ruff` + `pytest` 실행하는 `.github/workflows/ci.yml` 추가 권장. 본 명세에서는 후속 작업으로.

---

## 14. 개발 마일스톤 (Claude Code 작업 순서)

각 단계는 **머지 가능한 PR 단위**로 끊는다.

1. **M1 — 스캐폴딩**: 디렉터리 구조, `requirements.txt`, FastAPI 앱 부팅, `/health` 엔드포인트, Alembic 초기화, base.html, **i18n 기본 구조(en/ko 사전 + `t()` 함수 + 상단바 언어 토글)**, README.
2. **M2 — 데이터 모델 + 카테고리 CRUD**: 모델 5개, 첫 마이그레이션, `/categories` 페이지(트리 표시·CRUD).
3. **M3 — 소스 관리 + Google Play 수집**: Source CRUD, **앱 이름 검색 → 후보 선택 UI**, Google Play 수집기, CollectionJob, 진행률 폴링 UI.
4. **M4 — App Store + Reddit 수집기** 추가: App Store는 iTunes Search API로 앱 검색, Reddit은 서브레딧 검색 자동완성 (PRAW 공식 API).
5. **M5 — 사용자 지정 URL 수집기**: 정적 + **동적(Playwright) 모드 동시 지원**, robots.txt 체크, 도메인별 sleep.
6. **M6 — AI 분석 파이프라인**: `analyzer.py`, `/analyze` 페이지, **모델 선택 드롭다운**, 요약 언어 선택, 배치 처리, 재시도.
7. **M7 — 대시보드**: `/api/stats` + Chart.js 4종 차트 + 요약 카드.
8. **M8 — 리뷰 탐색 + 내보내기**: 필터·페이지네이션·CSV/JSON/XLSX.
9. **M9 — Render 배포**: `render.yaml`, Playwright 빌드 검증, 환경변수 정리, 첫 배포.
10. **M10 — 번역 완성도 + CI**: 모든 UI 문자열 i18n 키화 검증, 누락 키 린트, GitHub Actions CI(`ruff` + `pytest`).

각 마일스톤 완료 시 Claude Code는:
- 해당 범위만 변경
- 관련 테스트 추가
- README의 "현재 기능" 섹션 갱신
- 커밋·PR 메시지에 마일스톤 번호 명시

---

## 15. 비기능 요구사항

- **로깅**: `structlog` 또는 표준 `logging`, JSON 포맷 권장. 수집·분석 작업은 작업 ID 포함 로그.
- **에러 처리**: 외부 API 실패는 작업을 실패로 마크하되 앱은 죽지 않게. 사용자에게 친절한 에러 메시지.
- **레이트리밋**: LLM 호출은 동시성·재시도(지수 백오프) 처리. 스크래핑은 소스별로 짧은 sleep.
- **PII**: 리뷰 본문은 그대로 저장. 작성자 이름은 소스가 공개한 값만. 추가 PII 추출·저장 금지.
- **저작권/약관**: 각 소스의 약관 확인은 사용자 책임. 수집기 코드 주석에 약관 링크 참고로 남길 것.

---

## 16. 수용 기준 (Definition of Done)

- [ ] 로컬에서 `uvicorn app.main:app --reload`로 부팅 가능
- [ ] 앱·서브레딧을 **이름으로 검색**해서 후보 목록에서 선택만으로 소스 등록 가능 (사용자가 패키지명·숫자 ID를 직접 입력하지 않아도 됨)
- [ ] 4개 소스 모두에서 최소 1건 이상 수집 성공 (Reddit은 PRAW 공식 API, Web은 정적+동적 둘 다)
- [ ] 카테고리 트리 3단계 이상 생성·편집 가능
- [ ] 분석 화면에서 모델 3종 중 선택해 실행 가능, 결과에 사용 모델 ID 기록
- [ ] UI를 영어·한국어로 토글 시 모든 라벨·메뉴가 즉시 전환
- [ ] 대시보드 4개 차트 정상 렌더
- [ ] CSV·JSON·XLSX 다운로드 동작
- [ ] Render에 배포되어 공개 URL에서 동작, Playwright 동적 수집도 운영 환경에서 1회 검증
- [ ] `main` 브랜치 푸시 → 자동 배포 확인
- [ ] `README.md`에 설치·실행·배포 절차 명시
