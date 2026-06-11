# ChineseNews — Remote Config + Daily News Curator

이 저장소는 ChineseNews 앱(iOS · Android)의 실시간 뉴스 피드를 위한
두 가지를 보관합니다.

1. **매일 뉴스 큐레이터 (Tier 0)** — `generate_daily_news.py` + GitHub Action이
   매일 한 번 중립적인 중국어 기사 ~20건을 모아 `feed` 브랜치의
   `daily-news.json`으로 발행합니다. 앱은 이 파일을 **가장 먼저** 읽고(Tier 0)
   본문이 이미 들어 있으므로 온디바이스 스크래핑을 건너뜁니다.
2. **원격 설정 (Tier 2 폴백)** — `news-config.json`. 아래 "폴백 동작 흐름" 참고.

## 매일 뉴스 큐레이터 (Tier 0)

`generate_daily_news.py`가 매일 `0 21 * * *` UTC(= 06:00 KST)에 실행되어
중립적·간체 중국어 기사 ~20건을 `daily-news.json`으로 발행합니다.

- **소스 (기본 = 재배포 가능한 것만)**:
  - VOA 중문 — **중립 섹션만**(科教/经济), 미국 정부 발행 = **퍼블릭 도메인**
  - 维基新闻(Wikinews) — **CC BY 2.5**
- **중립성 필터** (`is_neutral`): 정치·외교·분쟁·민감 주제 키워드가 들어간
  기사를 모두 제외 → 학습 피드를 테크·경제·과학·문화·건강 중심으로 유지.
- **교차일 중복 제거** (`seen.json`): 최근 `SEEN_RETENTION_DAYS`(10일)간
  내보낸 기사는 다시 내보내지 않음 → "어제 기사가 오늘 또" 방지.
  (이전의 "전날 피드 백필"이 중복의 원인이었기에 제거함.)
- **분량**: 150–1500 한자만, 긴 글은 ~1200자에서 자연 절단.

정치 뉴스가 많은 날은 중립 기사가 적어 14~20건으로 변동합니다. 앱의 "오늘"
탭은 부족분을 최근 "지난 기사"로 채워 항상 20개를 보여주되, 캡션은 실제
"오늘 추가된 기사 N개"만 표기합니다.

### 저작권 있는 테크 소스(선택)

`--tech-rss` 플래그를 주면 IT之家 · 36氪 · 少数派를 추가해 매일 안정적으로
20건을 채울 수 있습니다. **단, 이 세 곳은 상업적 저작권 콘텐츠**이므로 유료
앱에 전문을 재배포하면 저작권·심사 리스크가 있습니다. 그래서 워크플로의 기본은
이 플래그 **없이**(= 안전 소스만) 실행합니다. 활성화하려면
`.github/workflows/daily-news.yml`의 실행 줄에 `--tech-rss`를 추가하세요.

### 수동 실행 / 점검

저장소 Actions 탭 → "Generate daily news" → **Run workflow** (수동 트리거).
로컬에서 시험하려면:

```bash
python generate_daily_news.py --out daily-news.json --target 20 --seen seen.json
# 테크 소스까지(저작권 주의): --tech-rss 추가
```

---

## 원격 설정 (Tier 2 폴백)

VOA 사이트 구조가 바뀌어 앱에 하드코딩된 selectors(URL · CSS 클래스명)가
더 이상 동작하지 않을 때, `news-config.json`의 내용만 수정해주면
**App Store 업데이트 없이** 모든 사용자의 앱이 24시간 이내에 자동 복구됩니다.

## 폴백 동작 흐름

1. **Tier 1** — 앱이 평소엔 Wikinews + VOA에서 직접 기사를 가져옵니다.
2. **Tier 2** — Tier 1 결과가 5건 미만이면 이 저장소의
   `news-config.json`을 가져와 새로운 selectors로 재시도합니다.
3. **Tier 3** — Tier 2도 실패하면 zh.wikipedia.org의 랜덤 article로
   비상 대체합니다. 사용자는 절대 빈 화면을 보지 않습니다.

## 뉴스 피드가 깨졌을 때 복구 절차

1. 본인 아이폰에서 앱의 실시간 뉴스 탭을 확인 — 기사가 거의 없으면
   VOA 사이트 변경이 의심됨.
2. VOA(voachinese.com)에 접속해 새 카테고리 URL과 본문 컨테이너의
   CSS 클래스명을 확인.
3. 이 저장소의 `news-config.json` 우측 위 ✏️ 연필 아이콘 클릭 →
   `voa.categoryURLs` 또는 `voa.containerPatterns`를 새 값으로 수정.
4. `Commit changes` → `main` 브랜치에 직접 커밋.
5. 캐시 만료(최대 24시간) 후 모든 사용자 자동 복구.

## JSON 스키마

| 키 | 타입 | 설명 |
|---|---|---|
| `schemaVersion` | number | 현재 1. 앱이 호환되는 스키마 버전 확인용. |
| `updatedAt` | string | 최근 수정일 (`YYYY-MM-DD`). 본인 추적용. |
| `emergencyFallbackEnabled` | bool | `false`로 두면 Tier 3 (Wikipedia 폴백) 비활성화. 평소엔 `true`. |
| `voa.categoryURLs` | string[] | VOA 카테고리 페이지 URL 목록. 첫 번째는 보통 홈페이지. |
| `voa.linkSubstring` | string | VOA 기사 링크에 반드시 포함돼야 하는 substring. 기본 `/a/`. |
| `voa.containerPatterns` | string[] | 본문 컨테이너 `<div>`를 잡는 정규식 패턴 (HTML 클래스명 기준). |
| `wikinews.categoryTitle` | string | Wikinews에서 발행 기사 카테고리명. 기본 `Category:已发布`. |

자세한 내부 동작은 앱 소스의 `NewsArticleParser.m` 상단 주석을 참고하세요.

## 주의 사항

- 이 저장소는 **반드시 Public** 상태를 유지해야 합니다. Private이면
  앱이 인증 토큰 없이 접근할 수 없어 Tier 2가 항상 실패합니다.
- JSON 문법이 깨지면 앱은 이전 버전을 디스크 캐시에서 그대로 사용하므로
  서비스가 중단되지는 않지만, 잘못된 JSON을 푸시하기 전에
  [jsonlint.com](https://jsonlint.com) 등에서 검증하는 것을 권장합니다.
- `main` 브랜치에 직접 커밋해야 합니다. 다른 브랜치에 커밋하면 앱이
  찾지 못합니다.
