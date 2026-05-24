# ChineseNews iOS — Remote Config

이 저장소는 ChineseNews iOS 앱의 기사 파서가 **Tier 2 폴백**으로 사용하는
원격 설정 파일을 보관합니다. VOA 사이트 구조가 바뀌어 앱에 하드코딩된
selectors(URL · CSS 클래스명)가 더 이상 동작하지 않을 때, 이 파일의
내용만 수정해주면 **App Store 업데이트 없이** 모든 사용자의 앱이
24시간 이내에 자동 복구됩니다.

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
