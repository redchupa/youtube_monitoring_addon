# YouTube Monitoring Add-on 문서

## 목차

1. [개요](#개요)
2. [설치 및 설정](#설치-및-설정)
3. [API 참조](#api-참조)
4. [실시간 기록 (Ingest)](#실시간-기록-ingest)
5. [REST 센서 연동](#rest-센서-연동)
6. [로컬 테스트](#로컬-테스트)
7. [문제 해결](#문제-해결)

---

## 개요

이 에드온은 YouTube 시청 기록 페이지를 쿠키로 직접 조회하여, Home Assistant에서 일별/월별 시청 기록을 확인할 수 있게 합니다. 기존 HA 통합(컴포넌트)에 의존하지 않는 **독립형** 에드온입니다.

### 데이터 저장

| 파일 | 용도 |
|------|------|
| `/data/yt_history.json` | 시청 기록 (일별) |
| `/data/yt_subscriptions.json` | 구독 스냅샷, 월별 구독 변경 |

- 시청 기록 형식: `{ "YYYY-MM-DD": [ { "video_id", "title", "channel", ... } ] }`
- **Shorts 미기록**: v1.4.8부터 Shorts는 저장하지 않으며, API·웹 UI에서도 제외. 기존 파일에 있는 Shorts는 표시/통계에서 필터링
- **쿠키 만료 시에도 데이터 유지**: 디스크에 저장되므로 쿠키 갱신 후 정상 복구
- **시청 기록 날짜**: 설정한 `timezone` 기준으로 저장 (기본 Asia/Seoul)

---

## 설치 및 설정

### 쿠키 준비

1. Chrome 확장 **Get cookies.txt LOCALLY** 설치
2. 시크릿 모드에서 YouTube 로그인
3. 쿠키 내보내기 → `youtube_cookies.txt`로 저장
4. Home Assistant `/config` 폴더에 업로드

### 설정 옵션

| 옵션 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| cookies_path | str | `/config/youtube_cookies.txt` | 쿠키 파일 경로 |
| scan_interval | int | 60 | YouTube history 페이지 갱신 간격(초), 30~300. **429 에러 시 120 이상 권장** |
| port | int | 8765 | API 서버 포트 |
| duplicate_minutes | int | 5 | 같은 영상 재감지 시 무시 시간(분), 1~60 |
| fetch_recommended | bool | true | 추천 영상 조회 여부. 스마트폰 등과 충돌 시 false로 비활성화 |
| scan_interval_recommended | int | 1800 | 추천 영상 갱신 간격(초), 60~3600. fetch_recommended가 true일 때만 적용 |
| timezone | str | Asia/Seoul | 로그 타임스탬프 및 **시청 기록 저장 시 날짜** 기준 타임존 (예: Europe/Paris, America/New_York) |

### Ingress

에드온은 Ingress를 지원합니다. **설정** → **애드온** → YouTube Monitoring → **웹 UI 열기**로 접속하거나, 대시보드에 패널로 추가할 수 있습니다.

---

## API 참조

기본 URL: `http://[HOST]:8765` (에드온 내부) 또는 Ingress URL

### GET /api/history

날짜별 기록, 월별 통계, 실시간 조회 데이터를 반환합니다.

**응답 예시:**

```json
{
  "cookies_valid": true,
  "by_date": {
    "2025-02-18": [
      {
        "video_id": "xxx",
        "title": "영상 제목",
        "channel": "채널명",
        "thumbnail": "https://...",
        "url": "https://youtube.com/watch?v=xxx",
        "duration": "10:30",
        "timestamp": "2025-02-18T12:00:00"
      }
    ]
  },
  "monthly_stats": { "2025-02": 10, "2025-01": 25 },
  "monthly_breakdown": {
    "2025-02": { "videos": 7, "shorts": 0 },
    "2025-01": { "videos": 20, "shorts": 0 }
  },
  "live": [],
  "subscriptions": {
    "total_count": 42,
    "channels": [
      {
        "channel_name": "채널1",
        "channel_id": "UC...",
        "subscriber_count_text": "구독자 17만명",
        "subscriber_count": 170000,
        "handle": "@handle1",
        "thumbnail": "https://...",
        "channel_url": "https://www.youtube.com/@...",
        "description_snippet": "채널 설명 일부"
      }
    ]
  },
  "monthly_subscription_changes": {
    "2025-02": { "added": ["신규채널"], "removed": ["구독취소채널"] }
  },
  "recommended": [
    {
      "video_id": "xxx",
      "title": "추천 영상 제목",
      "channel": "채널명",
      "thumbnail": "https://...",
      "url": "https://youtube.com/watch?v=xxx",
      "duration": "10:30"
    }
  ],
  "fetch_recommended": true,
  "recommended_refresh_available_at": 1739876543,
  "recommended_refresh_retry_after": 0
}
```

### GET /api/stats

월별 통계만 반환합니다. Shorts는 제외된 기록 기준입니다.

**응답 예시:**

```json
{
  "monthly_stats": { "2025-02": 10, "2025-01": 25 },
  "monthly_breakdown": {
    "2025-02": { "videos": 7, "shorts": 0 }
  }
}
```

### GET /api/health

상태 확인용 엔드포인트입니다.

### POST /api/ingest

브라우저 확장/유저스크립트에서 현재 시청 중인 영상을 **즉시** 기록합니다.

**요청:**

```http
POST /api/ingest
Content-Type: application/json

{
  "video_id": "dQw4w9WgXcQ",
  "title": "영상 제목",
  "channel": "채널명",
  "duration": "3:45",
  "url": "https://youtube.com/watch?v=xxx"
}
```

- `video_id`: **필수**
- `title`, `channel`, `duration`, `url`: 선택
- 5분 내 같은 `video_id`는 중복 저장하지 않음
- **Shorts** URL(`/shorts/`) 또는 `duration: "Shorts"`인 경우 저장하지 않고 `{"status": "skipped", "reason": "shorts"}` 반환

### POST /api/refresh/recommended

추천 영상을 **수동으로 새로고침**합니다. 10분(600초) 쿨다운이 적용됩니다.

**성공 응답 (200):**

```json
{
  "status": "ok",
  "recommended": [ { "video_id": "xxx", "title": "...", "channel": "...", ... } ],
  "next_refresh_at": 1739877143
}
```

**쿨다운 중 (429):**

```json
{
  "error": "cooldown",
  "retry_after": 420,
  "message": "420초 후 다시 시도하세요."
}
```

- 웹 UI 추천 영상 탭의 "새로고침" 버튼으로 호출
- `recommended_refresh_available_at`, `recommended_refresh_retry_after`: GET /api/history 응답에 포함되어 쿨다운 상태 표시용

---

## 실시간 기록 (Ingest)

쿠키 폴링은 YouTube 서버 갱신 지연(수 분)이 있을 수 있습니다. **즉시 기록**이 필요하면 `POST /api/ingest`와 유저스크립트를 사용하세요.

### 유저스크립트 설정

1. Tampermonkey 등 유저스크립트 확장 설치
2. `docs/userscript.example.js` 내용을 새 스크립트로 추가
3. `INGEST_URL`을 에드온 Ingress 주소로 수정:

```javascript
const INGEST_URL = 'https://YOUR_HA_ADDRESS/api/hassio_ingress/YOUR_INGRESS_TOKEN/api/ingest';
```

Ingress 토큰은 **설정** → **애드온** → YouTube Monitoring → **웹 UI 열기** 후 주소창 URL에서 확인할 수 있습니다.

---

## REST 센서 연동

Home Assistant에서 REST 센서로 월별 통계를 가져올 수 있습니다.

```yaml
# configuration.yaml
rest:
  - resource: http://localhost:8765/api/history
    scan_interval: 60
    sensor:
      - name: "YouTube Monitoring 누적"
        unique_id: "youtube_monitoring_accumulated"
        value_template: "{{ value_json.monthly_stats | default({}) | length }}"
```

---

## 로컬 테스트

에드온 없이 로컬에서 실행하여 테스트할 수 있습니다.

```powershell
cd youtube_monitoring
$env:COOKIES_PATH = "C:\path\to\youtube_cookies.txt"
python main.py
```

브라우저에서 http://localhost:8765 접속

---

## 문제 해결

### 429 rate limit

YouTube가 요청을 제한할 때 발생합니다. **scan_interval**을 120초 이상으로 늘리세요. 스마트폰과 동시 접속 시 `fetch_recommended`를 false로 설정할 수 있습니다.

### 쿠키 만료 후 데이터

시청 기록·구독 변경은 JSON 파일에 저장되어 **쿠키와 무관하게 유지**됩니다. 쿠키만 새로 설정하면 기존 데이터 위에 계속 누적됩니다.
