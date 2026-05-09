# Changelog

## [1.5.2] - 2026-05-10

### MQTT 디바이스 정보 업데이트
- `manufacturer`: 우*만
- `model`: 토스 1000-1261-7813
- `sw_version`: 커피 한잔은 사랑입니다

## [1.5.1] - 2026-05-02

### MQTT 최근 시청 영상 센서 추가
- `sensor.youtube_recent_watched`
  - state: 가장 최근 시청한 영상 제목
  - attributes: video_id / channel / url / thumbnail / duration / observed_at
  - attributes.recent_videos: 최근 5개 영상 리스트 (자동화·카드 활용)
- 새 영상 시청 감지 시 즉시 MQTT 발행 (state_changed 트리거 가능)
- 시작 시점에도 1회 발행

## [1.5.0] - 2026-05-02

### 쿠키 상태 검출 로직 개선
- YouTube ytcfg의 `LOGGED_IN` 마커를 매 응답마다 직접 검사
- 쿠키 만료 시 즉시 `cookies_valid=False` 반영 (이전엔 한번 True가 되면 영원히 True)
- 만료 감지 시 경고 로그 추가

### MQTT Home Assistant Discovery 추가
- HA에 자동 등록되는 sensor: `youtube_recommended_1/2/3` (제목 + 영상 정보 attributes)
- `binary_sensor.youtube_cookies_valid` (connectivity, LWT 적용)
- `sensor.youtube_recommended_count` (추천 영상 개수, 자동화 트리거용)
- HA Supervisor에서 MQTT 정보 자동 조회 (`services: ["mqtt:want"]`)
- HA에 MQTT 통합이 없으면 자동으로 건너뜀 (선택사항)

## [1.4.18] - 2026-02-20

- CHANGELOG 정리 및 문서 업데이트

## [1.4.17] - 2026-02-20

- 구독 채널 정렬 버튼 탭 전환 버그 수정

## [1.4.16] - 2026-02-20

- 구독 채널 정렬 버튼 UI 개선

## [1.4.15] - 2026-02-20

- 구독 채널 기본 정렬을 구독자 순으로 변경

## [1.4.14] - 2026-02-20

- 구독 채널 영역 왼쪽 정렬

## [1.4.13] - 2026-02-20

- 구독 채널 구독자 순 정렬 옵션 추가

## [1.4.12] - 2026-02-20

- 구독 채널 정렬: 한글 → 영어 → 특수문자 순

## [1.4.11] - 2026-02-20

- 시작 로그 진행률 표시 개선

## [1.4.10] - 2026-02-20

- 구독 채널 정보 확장 (구독자 수, 핸들, 썸네일, 설명)

## [1.4.9] - 2026-02-20

- 로그 메시지 정리

## [1.4.8] - 2026-02-20

- 월별 통계 달력 뷰 추가
- Shorts 미기록 및 통계 제외

## [1.4.7] - 2026-02-20

- 시청 기록 날짜 저장 시 timezone 옵션 적용

## [1.4.6] - 2025-02-18

- 로그 타임스탬프 로컬 타임존 표시
- timezone 옵션 추가

## [1.4.5] - 2025-02-18

- 개발 규칙 추가

## [1.4.4] - 2025-02-18

- 시작 진행률 로그 추가

## [1.4.3] - 2025-02-18

- 추천 영상 수동 새로고침 API 추가 (10분 쿨다운)
- scan_interval_recommended 기본값 1800초

## [1.4.2] - 2025-02-18

- fetch_recommended 기본값 true

## [1.4.1] - 2025-02-18

- scan_interval_recommended 옵션 추가

## [1.4.0] - 2025-02-18

- 월별 구독 변경 추적

## [1.3.2] - 2025-02-18

- 429 rate limit 대응 (scan_interval 기본값 60초, 구독 채널 2분 간격)

## [1.3.1] - 2025-02-18

- fetch_recommended 옵션 추가

## [1.3.0] - 2025-02-18

- 구독 채널 조회
- 추천 영상 조회

## [1.2.0] - 2025-02-18

- 월별 통계 동영상/Shorts 구분

## [1.1.9] - 2025-02

- 버그 수정 및 개선

## [1.1.0] - 2025

- 초기 릴리스
