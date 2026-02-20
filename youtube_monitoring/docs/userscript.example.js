// ==UserScript==
// @name         YouTube → HA Monitoring Ingest
// @match        https://www.youtube.com/watch*
// @match        https://www.youtube.com/shorts/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

// 설정: 에드온 Ingress URL (HA 주소 + Ingress 경로)
const INGEST_URL = 'https://YOUR_HA_ADDRESS/api/hassio_ingress/YOUR_INGRESS_TOKEN/api/ingest';

function getVideoId() {
  const m = location.pathname.match(/\/watch\?v=([^&]+)|\/shorts\/([^/?]+)/);
  return m ? (m[1] || m[2]) : null;
}

function getTitle() {
  return document.querySelector('h1.ytd-video-primary-info-renderer yt-formatted-string')?.textContent?.trim() || 'N/A';
}

function getChannel() {
  return document.querySelector('#channel-name a')?.textContent?.trim() || 'N/A';
}

let lastSent = null;
const DEBOUNCE_MS = 3000;

function sendToAddon() {
  const videoId = getVideoId();
  if (!videoId || videoId === lastSent) return;
  lastSent = videoId;

  fetch(INGEST_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      video_id: videoId,
      title: getTitle(),
      channel: getChannel(),
      url: location.href,
    }),
  }).catch(() => {});
}

// URL 변경 감지 (SPA)
let prevUrl = location.href;
new MutationObserver(() => {
  if (location.href !== prevUrl) {
    prevUrl = location.href;
    lastSent = null;
    setTimeout(sendToAddon, DEBOUNCE_MS);
  }
}).observe(document.body, { childList: true, subtree: true });

// 초기 로드
setTimeout(sendToAddon, DEBOUNCE_MS);
