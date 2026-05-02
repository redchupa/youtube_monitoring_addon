"""
Home Assistant MQTT Discovery 퍼블리셔.

- 추천 영상 3개 → 각 sensor.youtube_recommended_1/2/3
- 쿠키 유효성 → binary_sensor.youtube_cookies_valid (connectivity)
- HA Supervisor에서 MQTT 서비스 정보를 받아 자동 연결.

config.yaml에 services: ["mqtt:want"] 선언 필요.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

try:
    import paho.mqtt.client as mqtt  # type: ignore
    _MQTT_AVAILABLE = True
except ImportError:
    _MQTT_AVAILABLE = False

import requests

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "youtube_monitoring"

_DEVICE = {
    "identifiers": [NODE_ID],
    "name": "YouTube Monitoring",
    "manufacturer": "redchupa",
    "model": "YouTube Monitoring Add-on",
}


def get_supervisor_mqtt() -> dict | None:
    """HA Supervisor에서 MQTT 서비스 정보 조회.

    config.yaml의 `services: ["mqtt:want"]` 가 있어야 응답 받음.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if r.status_code != 200:
            _LOGGER.info("Supervisor MQTT 서비스 응답 %s (HA에 MQTT 통합 필요)", r.status_code)
            return None
        data = r.json().get("data", {})
        host = data.get("host")
        if not host:
            return None
        return {
            "host": host,
            "port": int(data.get("port", 1883)),
            "username": data.get("username") or None,
            "password": data.get("password") or None,
        }
    except (requests.exceptions.RequestException, ValueError) as err:
        _LOGGER.debug("Supervisor MQTT 조회 실패: %s", err)
        return None


class MqttPublisher:
    """HA MQTT Discovery 기반 추천 영상/쿠키 상태 퍼블리셔."""

    def __init__(self, host: str, port: int = 1883,
                 username: str | None = None, password: str | None = None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client: Any = None
        self._connected = False

    def connect(self) -> bool:
        if not _MQTT_AVAILABLE:
            _LOGGER.warning("paho-mqtt 미설치 - MQTT 비활성")
            return False
        try:
            # paho-mqtt 1.x / 2.x 호환
            try:
                self.client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1,  # type: ignore[attr-defined]
                    client_id="youtube_monitoring",
                    clean_session=True,
                )
            except (AttributeError, TypeError):
                self.client = mqtt.Client(client_id="youtube_monitoring", clean_session=True)
            if self.username:
                self.client.username_pw_set(self.username, self.password or "")
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            # LWT: 끊기면 cookies_valid를 OFF로
            self.client.will_set(
                f"{NODE_ID}/cookies_valid/state", "OFF", retain=True
            )
            self.client.connect(self.host, self.port, 60)
            self.client.loop_start()
            return True
        except Exception as err:
            _LOGGER.error("MQTT 연결 실패: %s", err)
            return False

    def _on_connect(self, *args, **kwargs) -> None:
        # paho-mqtt 1.x: (client, userdata, flags, rc)
        # paho-mqtt 2.x: (client, userdata, flags, rc, properties)
        rc = args[3] if len(args) >= 4 else kwargs.get("rc", 0)
        self._connected = (rc == 0)
        _LOGGER.info("MQTT %s (rc=%s, %s:%s)",
                     "연결됨" if self._connected else "연결 실패",
                     rc, self.host, self.port)

    def _on_disconnect(self, *args, **kwargs) -> None:
        self._connected = False
        _LOGGER.warning("MQTT 끊김 (자동 재연결 시도)")

    def is_connected(self) -> bool:
        return self._connected

    def _publish(self, topic: str, payload: Any, retain: bool = True) -> bool:
        if not self._connected or not self.client:
            return False
        try:
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, ensure_ascii=False)
            elif payload is None:
                payload = ""
            self.client.publish(topic, payload, retain=retain, qos=0)
            return True
        except Exception as err:
            _LOGGER.error("MQTT 발행 실패 [%s]: %s", topic, err)
            return False

    def publish_discovery(self) -> None:
        """HA Discovery 설정을 1회 발행 (retain)."""
        # 추천 영상 3개
        for i in range(1, 4):
            cfg = {
                "name": f"YouTube 추천 {i}",
                "unique_id": f"youtube_recommended_{i}",
                "state_topic": f"{NODE_ID}/recommended_{i}/state",
                "json_attributes_topic": f"{NODE_ID}/recommended_{i}/attributes",
                "icon": "mdi:youtube",
                "device": _DEVICE,
            }
            self._publish(
                f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/recommended_{i}/config",
                cfg, retain=True,
            )

        # 쿠키 유효성 binary_sensor
        cfg_cookies = {
            "name": "YouTube 쿠키 유효",
            "unique_id": "youtube_cookies_valid",
            "state_topic": f"{NODE_ID}/cookies_valid/state",
            "device_class": "connectivity",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:cookie-check",
            "device": _DEVICE,
        }
        self._publish(
            f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/cookies_valid/config",
            cfg_cookies, retain=True,
        )

        # 추천 영상 개수 sensor (자동화 트리거용)
        cfg_count = {
            "name": "YouTube 추천 개수",
            "unique_id": "youtube_recommended_count",
            "state_topic": f"{NODE_ID}/recommended_count/state",
            "icon": "mdi:counter",
            "device": _DEVICE,
        }
        self._publish(
            f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/recommended_count/config",
            cfg_count, retain=True,
        )

    def publish_recommended(self, videos: list[dict]) -> None:
        """추천 영상 3개를 sensor 상태/속성으로 발행."""
        videos = videos or []
        for i in range(1, 4):
            v = videos[i - 1] if i <= len(videos) else None
            state_topic = f"{NODE_ID}/recommended_{i}/state"
            attr_topic = f"{NODE_ID}/recommended_{i}/attributes"
            if v:
                title = (v.get("title") or "")[:255]
                self._publish(state_topic, title)
                self._publish(attr_topic, {
                    "video_id": v.get("video_id", ""),
                    "title": v.get("title", ""),
                    "channel": v.get("channel", ""),
                    "url": v.get("url", ""),
                    "thumbnail": v.get("thumbnail", ""),
                    "duration": v.get("duration", ""),
                    "view_count": v.get("view_count", ""),
                    "published": v.get("published", ""),
                })
            else:
                self._publish(state_topic, "")
                self._publish(attr_topic, {})
        self._publish(f"{NODE_ID}/recommended_count/state", str(len(videos)))

    def publish_cookies_valid(self, valid: bool) -> None:
        self._publish(f"{NODE_ID}/cookies_valid/state", "ON" if valid else "OFF")
