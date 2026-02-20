# YouTube Monitoring Add-on

YouTube 쿠키만으로 시청 기록을 **일별/월별**로 조회하는 Home Assistant Add-on입니다.

## 주요 기능

- 쿠키 기반 시청 기록 조회 (갱신 간격 기본 60초)
- 누적 저장 (`/data/yt_history.json`, `yt_subscriptions.json`). **Shorts 미기록**
- 일별 기록, **월별 통계 달력 뷰** (월 선택 → 일별 시청 개수, 날짜 클릭 시 영상 목록)
- **구독 채널**: 썸네일, 구독자 수, @핸들, **이름순(한글→영어→특수문자) / 구독자 순** 정렬
- 월별 구독 변경 (신규/취소 추적)
- 추천 영상 (갱신 간격 설정 가능, 수동 새로고침 10분 쿨다운)
- 웹 UI (쿠키 상태 표시), Ingress 지원
- 실시간 푸시 (`POST /api/ingest`, Shorts URL 스킵)

## 빠른 시작

1. **설정** → **애드온** → YouTube Monitoring 설치
2. 쿠키 파일 (`youtube_cookies.txt`)을 `/config`에 준비
3. **웹 UI 열기**로 시청 기록 확인

## 설정

| 항목 | 설명 | 기본값 |
|------|------|--------|
| cookies_path | 쿠키 파일 경로 | `/config/youtube_cookies.txt` |
| scan_interval | 시청 기록/구독 갱신(초) | 60 |
| port | API 서버 포트 | 8765 |
| duplicate_minutes | 같은 영상 재감지 무시(분) | 5 |
| fetch_recommended | 추천 영상 조회 | true |
| scan_interval_recommended | 추천 영상 갱신(초) | 1800 |
| timezone | 로그·시청 기록 날짜용 타임존 | Asia/Seoul |

## 문서

- [DOCS.md](DOCS.md) - API, 설정, 사용법 상세
- [CHANGELOG.md](CHANGELOG.md) - 버전별 변경 이력

## 면책 조항

비공식 프로젝트이며 Google/YouTube와 무관합니다. 쿠키 사용 책임은 사용자에게 있으며, 소프트웨어는 "있는 그대로" 제공됩니다.

## 자주 묻는 질문

시청 기록 지연, 쿠키 오류, Shorts 구분 등 자세한 FAQ는 [저장소 루트 README](../README.md#자주-묻는-질문)를 참조하세요.

## 기여

버그 제보, 기능 제안, Pull Request를 환영합니다. [Issues](https://github.com/redchupa/youtube_monitoring_addon/issues)에서 이슈를 생성해 주세요.

## 라이센스

[MIT License](../LICENSE)
