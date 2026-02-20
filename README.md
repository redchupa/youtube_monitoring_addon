# YouTube Monitoring Add-on

**독립형** Home Assistant Add-on. YouTube 쿠키만으로 시청 기록을 **일별/월별**로 조회합니다.  

부모의 유튜브 계정으로 유튜브 영상을 많이 보는 아이들이 올바르게 시청하고 있는지 확인하고 싶은 마음에 개발하였습니다.  
유튜브 계정에 로그인 해서 쿠키 정보를 다운로드 해야 하므로, 유튜브 계정의 패스워드를 알고 있어야 해당 에드온을 사용할 수 있습니다.

**행복한 스마트홈 되세요.**

---

## 주요 특징

- **쿠키 기반**: YouTube 시청 기록 페이지 직접 조회 (갱신 간격 기본 60초)
- **누적 저장**: video_id 변경 시 `/data/yt_history.json`에 저장 (5분 내 중복 방지). **Shorts는 기록하지 않음**
- **일별 기록**: 날짜별 그룹핑, 접기/펼치기
- **월별 통계**: **달력 뷰** — 월 선택 후 일별 시청 개수 표시, 날짜 클릭 시 해당일 영상 목록. 통계는 Shorts 제외
- **구독 채널**: 구독 중인 채널 **썸네일, 구독자 수, @핸들**, 이름순(한글→영어→특수문자) / **구독자 순** 정렬 버튼
- **월별 구독 변경**: 신규 구독/구독 취소 채널 월별 추적
- **추천 영상**: YouTube 메인 페이지 추천 영상 최대 3개 (갱신 간격 설정 가능, 수동 새로고침 10분 쿨다운)
- **웹 UI**: 썸네일, 제목, 채널, 시청 시간, 쿠키 상태 표시, 클릭 시 YouTube 이동
- **Ingress**: HA 사이드바/대시보드 패널로 추가 가능
- **실시간 푸시** (선택): `POST /api/ingest`로 브라우저 확장/유저스크립트에서 즉시 기록 (Shorts URL은 스킵)

### 왜 쿠키 방식인가?

YouTube Data API v3는 **실제 시청 기록 접근을 완벽하게 제공하지 않습니다**.

쿠키 방식의 장점:
- YouTube 시청 기록 페이지에 직접 접근
- 가장 최근 본 영상 정보를 정확하게 가져오기
- API 할당량 제한 없음

> **참고**: 기존 컴포넌트는 HA WebSocket으로 실시간 entity를 구독했기 때문에 즉시 반영됐습니다. 에드온은 쿠키로 YouTube history 페이지를 폴링하므로, YouTube 서버 갱신 지연(수 분)이 있을 수 있습니다. **실시간 기록**이 필요하면 `POST /api/ingest` + 유저스크립트를 사용하세요.

---

## 설치 요구사항

- Home Assistant **Supervisor** (에드온 지원 환경)
- YouTube 계정 쿠키 파일

---

## 빠른 시작 가이드

### 단계 1: 쿠키 내보내기

#### Chrome 사용자 (권장)

1. **확장 프로그램 설치**
   - Chrome 웹 스토어에서 [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 설치
   - 확장 프로그램 관리에서 "시크릿 모드에서 허용" 활성화
   - 쿠키를 추출 후 해당 브라우저 닫기

2. **시크릿 모드에서 YouTube 로그인**
   - 시크릿 창을 열고 [YouTube.com](https://youtube.com) 접속
   - YouTube 계정으로 로그인

3. **쿠키 내보내기**
   - 확장 프로그램 아이콘 클릭
   - "Export" 버튼 클릭
   - `youtube.com_cookies.txt` 파일 다운로드

4. **파일 이름 변경**
   ```
   youtube.com_cookies.txt → youtube_cookies.txt
   ```

#### Firefox 사용자

동일한 방식으로 "cookies.txt" 확장 프로그램을 사용하여 내보내기

---

### 단계 2: 쿠키 파일 업로드

Home Assistant의 `/config` 폴더에 `youtube_cookies.txt` 파일을 업로드합니다.

#### 방법 A: File Editor 애드온

1. Home Assistant에서 **File Editor** 애드온 열기
2. 좌측 폴더 아이콘 클릭
3. "Upload file" 선택 후 `youtube_cookies.txt` 업로드

#### 방법 B: Samba/SMB

```
Windows 탐색기에서 \\homeassistant\config 접속 후 파일 복사
```

#### 방법 C: SSH

```bash
scp youtube_cookies.txt root@homeassistant:/config/
```

#### 파일 위치 확인

```bash
ls -la /config/youtube_cookies.txt
```

---

### 단계 3: 저장소 추가 및 에드온 설치

1. **설정** → **애드온** → **애드온 스토어** → 우측 상단 **⋮** → **저장소**
2. 저장소 URL 추가:
   ```
   https://github.com/redchupa/youtube_monitoring_addon
   ```
3. **애드온 스토어**에서 "YouTube Monitoring" 검색 후 설치
4. 에드온 **시작** 클릭

---

### 단계 4: 설정

| 항목 | 설명 | 기본값 |
|------|------|--------|
| cookies_path | 쿠키 파일 경로 | `/config/youtube_cookies.txt` |
| scan_interval | 시청 기록/구독 갱신 간격(초) | 60 |
| port | API 서버 포트 | 8765 |
| duplicate_minutes | 같은 영상 재감지 무시(분) | 5 |
| fetch_recommended | 추천 영상 조회 여부 | true |
| scan_interval_recommended | 추천 영상 갱신 간격(초) | 1800 |
| timezone | 로그 및 시청 기록 날짜용 타임존 | Asia/Seoul |

---

### 완료

**설정** → **애드온** → YouTube Monitoring → **웹 UI 열기**로 시청 기록을 확인할 수 있습니다.

---

## 사이드바 패널 추가

에드온은 Ingress를 지원합니다. 대시보드에 패널로 추가할 수 있습니다.

---

## API 사용

### GET /api/history

날짜별 기록, 월별 통계, 구독 채널, 월별 구독 변경, 추천 영상, 실시간 조회 데이터 반환

### GET /api/stats

월별 통계만 반환 (동영상/Shorts 구분 포함)

### GET /api/health

상태 확인

### POST /api/refresh/recommended

추천 영상 수동 새로고침 (10분 쿨다운). 웹 UI "새로고침" 버튼에서 호출.

### POST /api/ingest (실시간 기록)

브라우저 확장/유저스크립트에서 현재 시청 중인 영상을 **즉시** 기록합니다. (쿠키 폴링보다 빠름)

```json
POST /api/ingest
Content-Type: application/json

{"video_id": "dQw4w9WgXcQ", "title": "영상 제목", "channel": "채널명", "duration": "3:45"}
```

- `video_id` 필수, 나머지 선택
- 5분 내 같은 영상은 중복 저장 안 함

**실시간 기록 (유저스크립트)**: Tampermonkey 등으로 `youtube_monitoring/docs/userscript.example.js`를 설치하고, `INGEST_URL`을 에드온 Ingress 주소로 수정하면 YouTube 시청 시 즉시 기록됩니다.

---

## REST 센서 연동 (선택)

```yaml
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

```powershell
cd youtube_monitoring
$env:COOKIES_PATH = "C:\path\to\youtube_cookies.txt"
python main.py
```

브라우저: http://localhost:8765

---

## 쿠키 보안

### 중요: 쿠키 파일 보호

쿠키는 **민감한 개인 정보**입니다. 다음 사항을 반드시 준수하세요:

#### 파일 권한 설정

```bash
# SSH 접속 후 실행
chmod 600 /config/youtube_cookies.txt
```

#### 보안 체크리스트

- 쿠키 파일을 공개 저장소(GitHub 등)에 업로드하지 않기
- `.gitignore`에 `youtube_cookies.txt` 추가하기
- 정기적으로 쿠키 갱신하기 (2-3개월마다)
- 의심스러운 활동 발견 시 즉시 YouTube 비밀번호 변경

---

## 문제 해결

### 쿠키가 유효하지 않다고 나와요

**가능한 원인**:
- 쿠키 파일 경로 오류
- 쿠키 만료
- YouTube에서 로그아웃됨

**해결 방법**:

1. **쿠키 파일 확인**
   ```bash
   ls -la /config/youtube_cookies.txt
   ```

2. **쿠키 파일 교체**
   - YouTube에 다시 로그인 (시크릿 모드 권장)
   - 확장 프로그램으로 새 쿠키 내보내기
   - 기존 파일 덮어쓰기

3. **에드온 재시작**

---

### 시청 기록이 바로 반영되지 않아요

쿠키 폴링 방식이라 YouTube 서버 갱신 지연(수 분)이 있을 수 있습니다. **실시간 기록**이 필요하면 `POST /api/ingest`와 유저스크립트를 사용하세요.

### 429 에러(서버 문제)가 발생해요

YouTube rate limit입니다. **scan_interval**을 120초 이상으로 늘려보세요. 스마트폰과 동시 접속 시 발생할 수 있습니다.

### 쿠키 만료 시 통계 데이터는?

**유지됩니다.** 시청 기록·구독 변경은 `/data/`에 JSON 파일로 저장되며, 쿠키와 무관합니다. 쿠키를 새로 설정하면 기존 데이터 위에 계속 누적됩니다.

---

## 면책 조항

- 이 에드온은 **비공식** 프로젝트이며, Google/YouTube와 제휴·연관되어 있지 않습니다.
- YouTube 서비스 이용 약관 및 쿠키 사용에 대한 책임은 사용자에게 있습니다.
- 쿠키 파일은 민감한 인증 정보를 포함하므로, 안전하게 보관하고 제3자와 공유하지 마세요.
- 이 소프트웨어는 "있는 그대로" 제공되며, 작동 보증이나 특정 목적에의 적합성을 보장하지 않습니다.

---

## FAQ

**Q: 시청 기록이 바로 반영되지 않아요**

A: 쿠키 폴링 방식이라 YouTube 서버 갱신 지연(수 분)이 있을 수 있습니다. **실시간 기록**이 필요하면 `POST /api/ingest`와 유저스크립트(`youtube_monitoring/docs/userscript.example.js`)를 사용하세요.

**Q: 쿠키 갱신 주기는?**

A: 인터넷 검색 피셜 6개월 사용 가능하다고 합니다. 웹 UI에서 "쿠키 유효하지 않음"이 표시되면 쿠키를 다시 내보내세요.

**Q: 쿠키가 계속 만료되는 이유는?**

A: YouTube 2단계 인증 또는 보안 설정을 확인하세요. 일부 계정은 더 자주 재인증이 필요할 수 있습니다. VPN 사용이나 IP 변경이 원인일 수 있습니다.

**Q: Shorts와 일반 동영상이 구분되나요? / Shorts도 기록되나요?**

A: v1.4.8부터 **Shorts는 기록하지 않으며**, 일별/월별 기록·통계에서도 제외됩니다. API·웹 UI에는 Shorts가 포함되지 않습니다. 기존에 저장된 Shorts 데이터는 파일에는 남아 있으나 표시·통계에서는 필터링됩니다.

**Q: Home Assistant 공식 애드온인가요?**

A: 아니요. 커뮤니티에서 만든 독립형 에드온이며, Home Assistant 공식 저장소에는 포함되어 있지 않습니다.

**Q: 가족 계정으로 여러 명이 공유하는 유튜브 계정을 사용 시에는?**

A: 가장 최근에 유튜브 시청한 기록으로 누적됩니다.

**Q: 429 에러가 나요**

A: scan_interval을 120초 이상으로 늘리세요. 추천 영상이 스마트폰과 충돌하면 fetch_recommended를 false로 설정하세요.

**Q: 쿠키 만료 후 데이터는?**

A: 시청 기록·구독 변경 데이터는 디스크에 저장되어 유지됩니다. 쿠키만 갱신하면 정상 복구됩니다.

---

## 기여

버그 제보, 기능 제안, Pull Request를 환영합니다.

1. [Issues](https://github.com/redchupa/youtube_monitoring_addon/issues)에서 이슈 생성 또는 검색
2. Fork 후 변경 사항 적용
3. Pull Request 생성

---

## 버그 리포트

[이슈 작성하기](https://github.com/redchupa/youtube_monitoring_addon/issues/new)

다음 정보를 포함해주세요:
- Home Assistant 버전
- 에드온 버전
- 오류 로그
- 재현 방법
- 새로운 기능이나 개선 사항을 제안해주세요.

---

## 기술 세부사항

### 시스템 요구사항

- **Home Assistant**: Supervisor 지원 환경
- **아키텍처**: armhf, armv7, aarch64, amd64, i386

### 의존성

- `requests` - HTTP 요청 및 쿠키 처리

---

## 라이선스

[MIT License](LICENSE)

---

## 후원

이 애드온이 유용하셨다면 커피 한 잔 후원 부탁드립니다!

<table>
  <tr>
    <td align="center">
      <b>Toss (토스)</b><br>
      <img src="https://raw.githubusercontent.com/redchupa/youtube_monitoring_addon/main/images/toss-donation.png" width="200" alt="Toss 후원하기">
    </td>
    <td align="center">
      <b>PayPal</b><br>
      <img src="https://raw.githubusercontent.com/redchupa/youtube_monitoring_addon/main/images/paypal-donation.png" width="200" alt="PayPal 후원하기">
    </td>
  </tr>
</table>

---

**즐거운 스마트홈 되세요!**
