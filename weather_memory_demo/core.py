from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOCKET = "/run/bianbu/agent-memoryd.sock"
DEFAULT_MEMORY_FILE = Path(__file__).with_name("weather_memories.json")

#: 设备本机时区（中国无夏令时，固定 +08:00）。与 memory 侧的展示口径一致：
#: 对外只给一个可以直接念给用户的本地时间，不再产出 UTC / 带偏移的机器串。
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


class PromptError(ValueError):
    """The input is not a supported weather query."""


class WeatherServiceError(RuntimeError):
    """The public weather service could not satisfy the request."""


@dataclass(frozen=True)
class WeatherQuery:
    city: str
    day_offset: int
    day_label: str


@dataclass(frozen=True)
class Forecast:
    city: str
    region: str
    country: str
    latitude: float
    longitude: float
    date: str
    day_label: str
    weather_code: int
    condition: str
    temperature_min_c: float
    temperature_max_c: float
    precipitation_probability_percent: int
    precipitation_mm: float
    max_wind_kmh: float
    provider: str
    provider_url: str


@dataclass(frozen=True)
class RecalledMemory:
    memory_id: str
    memory_type: str
    kind: str
    content: str
    summary: str | None
    tags: tuple[str, ...]
    source: str
    file_paths: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    revision: int
    sensitivity: str
    confidence: float | None
    sync_policy: str
    updated_at: int
    updated_at_display: str
    search_score: float


@dataclass(frozen=True)
class AnswerResult:
    prompt: str
    answer: str
    weather: Forecast
    memories: tuple[RecalledMemory, ...]
    memory_applied: tuple[str, ...]
    session_id: str
    identity: dict[str, Any]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CITY_ALIASES = {
    "北京": "Beijing",
    "上海": "Shanghai",
    "天津": "Tianjin",
    "重庆": "Chongqing",
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "珠海": "Zhuhai",
    "佛山": "Foshan",
    "东莞": "Dongguan",
    "杭州": "Hangzhou",
    "南京": "Nanjing",
    "苏州": "Suzhou",
    "成都": "Chengdu",
    "武汉": "Wuhan",
    "西安": "Xi'an",
    "长沙": "Changsha",
    "郑州": "Zhengzhou",
    "青岛": "Qingdao",
    "厦门": "Xiamen",
    "海口": "Haikou",
    "三亚": "Sanya",
    "香港": "Hong Kong",
    "澳门": "Macao",
    "台北": "Taipei",
}

_LEADING_FILLERS = (
    "麻烦帮我",
    "请帮我",
    "能不能帮我",
    "可以帮我",
    "我想知道",
    "麻烦",
    "帮我",
    "请",
    "能不能",
    "可以",
    "给我",
    "查询一下",
    "查询",
    "查一下",
    "查查",
    "看看",
    "告诉我",
    "了解一下",
)

_WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴强冰雹",
}


def parse_weather_prompt(prompt: str) -> WeatherQuery:
    text = prompt.strip()
    if not text:
        raise PromptError("提示词不能为空")

    english = re.search(
        r"\bweather\s+(?:in|for)\s+(.+?)(?:\s+(today|tomorrow|the day after tomorrow))?[?!.。！？]*$",
        text,
        flags=re.IGNORECASE,
    )
    if english:
        city = english.group(1).strip(" ,")
        day_word = (english.group(2) or "today").lower()
        day_offset = {"today": 0, "tomorrow": 1, "the day after tomorrow": 2}[day_word]
        return WeatherQuery(city=city, day_offset=day_offset, day_label=_day_label(day_offset))

    if "天气" not in text:
        raise PromptError("当前演示只支持天气查询，例如：帮我查询珠海明天的天气？")

    day_offset = 2 if "后天" in text else 1 if "明天" in text else 0
    before_weather = text.split("天气", 1)[0]
    candidate = re.sub(r"今天|今日|明天|后天", "", before_weather)
    candidate = candidate.strip(" ，,。？?！!：:的")
    previous = None
    while candidate and candidate != previous:
        previous = candidate
        for filler in _LEADING_FILLERS:
            if candidate.startswith(filler):
                candidate = candidate[len(filler) :].strip()
                break
    candidate = candidate.strip(" ，,。？?！!：:的")
    if candidate.endswith("市") and len(candidate) > 1:
        candidate = candidate[:-1]
    if not candidate or len(candidate) > 80:
        raise PromptError("没有识别到城市，请输入类似“帮我查询珠海明天的天气？”的提示词")
    return WeatherQuery(city=candidate, day_offset=day_offset, day_label=_day_label(day_offset))


def _day_label(day_offset: int) -> str:
    return {0: "今天", 1: "明天", 2: "后天"}.get(day_offset, f"未来第 {day_offset + 1} 天")


class OpenMeteoWeatherClient:
    """Dependency-free Open-Meteo client used by the K3 demo."""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    def forecast(self, query: WeatherQuery) -> Forecast:
        place = self._geocode(query.city)
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
            ),
            "timezone": "Asia/Shanghai",
            "forecast_days": max(3, query.day_offset + 1),
        }
        data, provider_url = self._get_json(self.FORECAST_URL, params)
        daily = data.get("daily")
        if not isinstance(daily, dict):
            raise WeatherServiceError("天气服务没有返回逐日预报")
        index = query.day_offset
        try:
            weather_code = int(daily["weather_code"][index])
            return Forecast(
                city=str(place.get("name") or query.city),
                region=str(place.get("admin1") or ""),
                country=str(place.get("country") or ""),
                latitude=latitude,
                longitude=longitude,
                date=str(daily["time"][index]),
                day_label=query.day_label,
                weather_code=weather_code,
                condition=_WEATHER_CODES.get(weather_code, f"天气代码 {weather_code}"),
                temperature_min_c=round(float(daily["temperature_2m_min"][index]), 1),
                temperature_max_c=round(float(daily["temperature_2m_max"][index]), 1),
                precipitation_probability_percent=int(
                    daily["precipitation_probability_max"][index] or 0
                ),
                precipitation_mm=round(float(daily["precipitation_sum"][index] or 0), 1),
                max_wind_kmh=round(float(daily["wind_speed_10m_max"][index] or 0), 1),
                provider="Open-Meteo",
                provider_url=provider_url,
            )
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise WeatherServiceError("天气服务返回的逐日预报格式不完整") from error

    def _geocode(self, city: str) -> dict[str, Any]:
        normalized = city.removesuffix("市")
        search_name = _CITY_ALIASES.get(normalized, normalized)
        params: dict[str, Any] = {
            "name": search_name,
            "count": 10,
            "language": "zh",
            "format": "json",
        }
        if normalized in _CITY_ALIASES and normalized not in {"香港", "澳门", "台北"}:
            params["countryCode"] = "CN"
        data, _ = self._get_json(self.GEOCODING_URL, params)
        results = data.get("results")
        if not isinstance(results, list) or not results:
            raise WeatherServiceError(f"没有找到城市“{city}”，可尝试输入城市的英文名")
        return self._select_place(results, normalized, search_name)

    @staticmethod
    def _select_place(
        results: list[dict[str, Any]], normalized: str, search_name: str
    ) -> dict[str, Any]:
        expected = {normalized.casefold(), f"{normalized}市".casefold(), search_name.casefold()}
        for place in results:
            if str(place.get("name") or "").casefold() in expected:
                return place
        return results[0]

    def _get_json(self, base_url: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "bianbu-agent-memory-weather-demo/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(2 * 1024 * 1024)
            data = json.loads(payload)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise WeatherServiceError(f"天气服务访问失败：{type(error).__name__}") from error
        if not isinstance(data, dict):
            raise WeatherServiceError("天气服务返回了无效数据")
        return data, url


class WeatherMemoryApplication:
    """Coordinates prompt parsing, bounded memory recall, weather and turn storage."""

    def __init__(
        self,
        memory_client: Any,
        weather_client: OpenMeteoWeatherClient | Any | None = None,
        memory_file: str | Path = DEFAULT_MEMORY_FILE,
    ) -> None:
        self.memory_client = memory_client
        self.weather_client = weather_client or OpenMeteoWeatherClient()
        self.memory_file = Path(memory_file)

    @classmethod
    def connect(
        cls,
        socket_path: str = DEFAULT_SOCKET,
        *,
        timeout: float = 5.0,
        weather_timeout: float = 12.0,
        memory_file: str | Path = DEFAULT_MEMORY_FILE,
    ) -> "WeatherMemoryApplication":
        from bianbu_agent_sdk.memory import MemoryClient

        client = MemoryClient.connect(socket_path, timeout=timeout)
        return cls(
            client,
            OpenMeteoWeatherClient(timeout=weather_timeout),
            memory_file=memory_file,
        )

    def close(self) -> None:
        self.memory_client.close()

    def seed_demo_memories(self) -> list[dict[str, Any]]:
        from bianbu_agent_sdk.memory import EvidenceRef, MemoryKind, SyncPolicy

        raw = self.memory_file.read_bytes()
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"示例记忆文件不是有效 JSON：{self.memory_file}") from error
        entries = document.get("memories") if isinstance(document, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"示例记忆文件缺少 memories：{self.memory_file}")

        source_path = self.memory_file.resolve().as_posix()
        if len(source_path.encode("utf-8")) > 256:
            raise ValueError("示例记忆文件绝对路径超过服务端 evidence_id 上限")
        file_hash = hashlib.sha256(raw).hexdigest()
        results: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("示例记忆条目必须是对象")
            text = str(item.get("text") or "").strip()
            key = str(item.get("key") or "").strip()
            if not text or not key:
                raise ValueError("示例记忆条目必须包含 key 和 text")
            digest = hashlib.sha256(f"{key}\0{text}\0{source_path}".encode()).hexdigest()[:24]
            receipt = self.memory_client.long_term.put(
                kind=MemoryKind(str(item.get("kind") or "preference")),
                text=text,
                summary=str(item["summary"]) if item.get("summary") else None,
                tags=tuple(str(tag) for tag in item.get("tags", ())),
                evidence=(EvidenceRef("file_path", source_path, file_hash),),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                sensitivity="private",
                confidence=float(item.get("confidence", 1.0)),
                idempotency_key=f"weather-demo:seed:v1:{digest}",
            )
            results.append(
                {
                    "key": key,
                    "memory_id": receipt.memory_id,
                    "revision": receipt.revision,
                    "file_path": source_path,
                }
            )
        return results

    def ask(self, prompt: str, *, session_id: str | None = None) -> AnswerResult:
        query = parse_weather_prompt(prompt)
        memories = self.recall(prompt, query)
        forecast = self.weather_client.forecast(query)
        answer, applied = _compose_answer(forecast, memories)
        active_session = session_id or f"weather-demo-{forecast.date}"
        warnings = self._record_turn(active_session, prompt, answer, forecast)
        return AnswerResult(
            prompt=prompt,
            answer=answer,
            weather=forecast,
            memories=tuple(memories),
            memory_applied=tuple(applied),
            session_id=active_session,
            identity=_identity_dict(getattr(self.memory_client, "identity", None)),
            warnings=tuple(warnings),
        )

    def recall(self, prompt: str, query: WeatherQuery, *, top_k: int = 5) -> list[RecalledMemory]:
        candidates = _unique(
            [
                prompt.strip(),
                f"{query.city}天气",
                query.city,
                "天气",
            ]
        )
        headers_by_id: dict[str, Any] = {}
        header_order: dict[str, int] = {}
        for candidate in candidates:
            headers = self.memory_client.long_term.search_headers(candidate, top_k=top_k)
            for header in headers:
                memory_id = str(header.memory_id)
                if memory_id not in headers_by_id:
                    headers_by_id[memory_id] = header
                    header_order[memory_id] = len(header_order)
                elif float(getattr(header, "score", 0.0)) > float(
                    getattr(headers_by_id[memory_id], "score", 0.0)
                ):
                    headers_by_id[memory_id] = header
            if len(headers_by_id) >= top_k * 2:
                break
        if not headers_by_id:
            return []

        ids = list(headers_by_id)
        records = self.memory_client.long_term.get_many(ids)
        result: list[tuple[float, int, RecalledMemory]] = []
        for record in records:
            if str(getattr(record, "sensitivity", "private")) == "secret":
                continue
            memory_id = str(record.memory_id)
            header = headers_by_id.get(memory_id)
            if header is None:
                continue
            view = _memory_view(record, float(getattr(header, "score", 0.0)))
            rank = _memory_rank(view, query.city)
            result.append((rank, header_order.get(memory_id, 9999), view))
        result.sort(key=lambda item: (-item[0], item[1], -item[2].updated_at))
        return [item[2] for item in result[:top_k]]

    def _record_turn(
        self, session_id: str, prompt: str, answer: str, forecast: Forecast
    ) -> list[str]:
        turn_id = str(uuid.uuid4())
        try:
            self.memory_client.sessions.append_event(
                session_id=session_id,
                event_type="user_message",
                payload={"role": "user", "text": prompt, "demo": "weather-memory"},
                idempotency_key=f"weather-demo:{turn_id}:user",
            )
            self.memory_client.sessions.append_event(
                session_id=session_id,
                event_type="assistant_message",
                payload={
                    "role": "assistant",
                    "text": answer,
                    "weather_provider": forecast.provider,
                    "forecast_date": forecast.date,
                    "demo": "weather-memory",
                },
                idempotency_key=f"weather-demo:{turn_id}:assistant",
            )
            return []
        except Exception as error:  # The answer remains useful when optional turn storage degrades.
            return [f"会话记忆写入失败：{type(error).__name__}"]


def _memory_view(record: Any, score: float) -> RecalledMemory:
    evidence = tuple(
        {
            "evidence_type": str(item.evidence_type),
            "evidence_id": str(item.evidence_id),
            "evidence_hash": item.evidence_hash,
        }
        for item in getattr(record, "evidence", ())
    )
    paths = tuple(
        item["evidence_id"]
        for item in evidence
        if item["evidence_type"] in {"file", "file_path", "path", "source_file"}
        or item["evidence_id"].startswith(("/", "./", "../"))
        or re.match(r"^[A-Za-z]:/", item["evidence_id"])
    )
    updated_at = int(getattr(record, "updated_at", 0))
    updated_at_display = (
        datetime.fromtimestamp(updated_at / 1000, LOCAL_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        if updated_at > 0
        else ""
    )
    return RecalledMemory(
        memory_id=str(record.memory_id),
        memory_type="long_term",
        kind=_enum_value(record.kind),
        content=str(record.text)[:2000],
        summary=str(record.summary) if record.summary is not None else None,
        tags=tuple(str(tag) for tag in getattr(record, "tags", ())),
        source=str(getattr(record, "source", "")),
        file_paths=paths,
        evidence=evidence,
        revision=int(record.revision),
        sensitivity=str(record.sensitivity),
        confidence=(
            float(record.confidence) if getattr(record, "confidence", None) is not None else None
        ),
        sync_policy=_enum_value(record.sync_policy),
        updated_at=updated_at,
        updated_at_display=updated_at_display,
        search_score=score,
    )


def _memory_rank(memory: RecalledMemory, city: str) -> float:
    searchable = " ".join(
        [memory.content, memory.summary or "", *memory.tags]
    ).casefold()
    score = memory.search_score
    if "天气" in searchable or "weather" in searchable:
        score += 10
    if city.casefold() in searchable:
        score += 4
    if "雨具" in searchable or "摄氏度" in searchable:
        score += 2
    return score


def _compose_answer(
    forecast: Forecast, memories: Iterable[RecalledMemory]
) -> tuple[str, list[str]]:
    recalled = list(memories)
    location = "，".join(part for part in (forecast.city, forecast.region) if part)
    answer = (
        f"{location} {forecast.date}（{forecast.day_label}）预计{forecast.condition}，"
        f"最低 {forecast.temperature_min_c:.1f}°C，最高 {forecast.temperature_max_c:.1f}°C；"
        f"最高降水概率 {forecast.precipitation_probability_percent}%，"
        f"预计降水量 {forecast.precipitation_mm:.1f} mm，最大风速 {forecast.max_wind_kmh:.1f} km/h。"
    )
    applied: list[str] = []
    combined = "\n".join(
        " ".join([item.content, item.summary or "", *item.tags]) for item in recalled
    )
    if "摄氏度" in combined:
        applied.append("按记忆偏好使用摄氏度和中文回复")
    rain_expected = (
        forecast.precipitation_probability_percent >= 40
        or forecast.precipitation_mm > 0
        or forecast.weather_code in set(range(51, 68)) | set(range(80, 83)) | {95, 96, 99}
    )
    if rain_expected and "雨具" in combined:
        answer += " 根据召回的出行提醒，建议携带雨具。"
        applied.append("有雨时提醒携带雨具")
    if recalled:
        answer += f" 本次同时召回并展示 {len(recalled)} 条相关记忆。"
    else:
        answer += " 本次未召回到相关长期记忆。"
    return answer, applied


def _identity_dict(identity: Any) -> dict[str, Any]:
    if identity is None:
        return {}
    return {
        "account_id": str(identity.account_id),
        "device_id": str(identity.device_id),
        "agent_id": str(identity.agent_id),
        "agent_instance_id": str(identity.agent_instance_id),
        "memory_space_id": str(identity.memory_space_id),
        "capabilities": sorted(str(item) for item in identity.capabilities),
    }


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
