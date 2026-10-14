from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from types import SimpleNamespace

from weather_memory_demo.core import (
    Forecast,
    PromptError,
    WeatherMemoryApplication,
    WeatherQuery,
    parse_weather_prompt,
)
from weather_memory_demo.web import WeatherDemoServer


def enum(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


class FakeLongTerm:
    def __init__(self) -> None:
        self.records = [
            SimpleNamespace(
                memory_id="mem-unit",
                kind=enum("preference"),
                text="用户查询天气时，默认使用中文回复，并使用摄氏度展示温度。",
                summary="天气回复使用中文和摄氏度",
                tags=("weather", "天气", "摄氏度"),
                evidence=(
                    SimpleNamespace(
                        evidence_type="file_path",
                        evidence_id="/home/example/bianbu-agent-memory/weather_memory_demo/weather_memories.json",
                        evidence_hash="abc123",
                    ),
                ),
                source="agent",
                confidence=1.0,
                sensitivity="private",
                revision=1,
                sync_policy=enum("local_only"),
                created_at=1_700_000_000_000,
                updated_at=1_700_000_000_000,
            ),
            SimpleNamespace(
                memory_id="mem-rain",
                kind=enum("preference"),
                text="有雨时提醒用户携带雨具。",
                summary="有雨时提醒携带雨具",
                tags=("天气", "雨具"),
                evidence=(),
                source="agent",
                confidence=1.0,
                sensitivity="private",
                revision=2,
                sync_policy=enum("local_only"),
                created_at=1_700_000_000_000,
                updated_at=1_700_000_100_000,
            ),
        ]

    def search_headers(self, query: str, *, top_k: int = 10):
        if "天气" not in query and "珠海" not in query:
            return []
        return [
            SimpleNamespace(memory_id=record.memory_id, score=float(index))
            for index, record in enumerate(self.records, 1)
        ][:top_k]

    def get_many(self, memory_ids):
        selected = set(memory_ids)
        return [record for record in self.records if record.memory_id in selected]


class FakeSessions:
    def __init__(self) -> None:
        self.events = []

    def append_event(self, **kwargs):
        self.events.append(kwargs)
        return {"event_sequence": len(self.events)}


class FakeMemoryClient:
    def __init__(self) -> None:
        self.long_term = FakeLongTerm()
        self.sessions = FakeSessions()
        self.identity = SimpleNamespace(
            account_id="local:test",
            device_id="k3-test",
            agent_id="weather-demo",
            agent_instance_id="weather-demo-1",
            memory_space_id="weather-demo-default",
            capabilities=frozenset({"memory.long_term.read"}),
        )

    def health(self):
        return {"status": "ok"}

    def close(self):
        return None


class FakeWeatherClient:
    def forecast(self, query: WeatherQuery) -> Forecast:
        return Forecast(
            city="珠海市",
            region="广东",
            country="中国",
            latitude=22.27694,
            longitude=113.56778,
            date="2026-08-01",
            day_label=query.day_label,
            weather_code=61,
            condition="小雨",
            temperature_min_c=27.0,
            temperature_max_c=32.0,
            precipitation_probability_percent=70,
            precipitation_mm=4.2,
            max_wind_kmh=18.5,
            provider="Open-Meteo",
            provider_url="https://example.test/forecast",
        )


class PromptParsingTests(unittest.TestCase):
    def test_chinese_example(self) -> None:
        query = parse_weather_prompt("帮我查询珠海明天的天气？")
        self.assertEqual(query.city, "珠海")
        self.assertEqual(query.day_offset, 1)
        self.assertEqual(query.day_label, "明天")

    def test_rejects_non_weather_prompt(self) -> None:
        with self.assertRaises(PromptError):
            parse_weather_prompt("帮我查看记忆")


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = FakeMemoryClient()
        self.app = WeatherMemoryApplication(self.memory, FakeWeatherClient())

    def test_answer_contains_weather_memory_and_file_path(self) -> None:
        result = self.app.ask("帮我查询珠海明天的天气？")
        self.assertIn("珠海市", result.answer)
        self.assertIn("建议携带雨具", result.answer)
        self.assertEqual(len(result.memories), 2)
        self.assertIn("按记忆偏好使用摄氏度和中文回复", result.memory_applied)
        self.assertIn(
            "/home/example/bianbu-agent-memory/weather_memory_demo/weather_memories.json",
            {path for memory in result.memories for path in memory.file_paths},
        )
        self.assertEqual(len(self.memory.sessions.events), 2)

    def test_web_api_returns_same_structured_result(self) -> None:
        server = WeatherDemoServer(("127.0.0.1", 0), self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            payload = json.dumps(
                {"prompt": "帮我查询珠海明天的天气？"}, ensure_ascii=False
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/ask",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.load(response)
            self.assertIn("建议携带雨具", body["answer"])
            self.assertTrue(
                any(memory["file_paths"] for memory in body["memories"])
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
