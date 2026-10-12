"""Open-Meteo client: текущая погода + прогноз на 5 дней, без API-ключа."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

LAT = float(os.environ.get("LAT", "55.55"))
LON = float(os.environ.get("LON", "37.55"))          # Бутово, Москва
TZ = os.environ.get("WEATHER_TZ", "Europe/Moscow")
PLACE = os.environ.get("PLACE", "Бутово")

API = "https://api.open-meteo.com/v1/forecast"
PARAMS = {
    "latitude": LAT,
    "longitude": LON,
    "timezone": TZ,
    "wind_speed_unit": "ms",
    "current": ",".join([
        "temperature_2m", "apparent_temperature", "relative_humidity_2m",
        "weather_code", "wind_speed_10m", "wind_direction_10m",
        "surface_pressure", "is_day",
    ]),
    "daily": ",".join([
        "weather_code", "temperature_2m_max", "temperature_2m_min",
        "precipitation_sum", "precipitation_probability_max",
        "sunrise", "sunset",
    ]),
    "hourly": ",".join([
        "temperature_2m", "precipitation_probability", "precipitation",
    ]),
    "forecast_days": 5,
}

HOURLY_WINDOW = 25        # точек на графике: сейчас + 24 часа

FRAME_LANG = os.environ.get("FRAME_LANG", "ru")   # язык кадра: ru | en

# WMO weather code -> короткое описание
DESCRIPTIONS = {
    "ru": {
        0: "ясно", 1: "преим. ясно", 2: "малооблачно", 3: "пасмурно",
        45: "туман", 48: "изморозь",
        51: "морось", 53: "морось", 55: "сильная морось",
        56: "лед. морось", 57: "лед. морось",
        61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
        66: "ледяной дождь", 67: "ледяной дождь",
        71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
        80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
        85: "снегопад", 86: "сильный снегопад",
        95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
    },
    "en": {
        0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "rime fog",
        51: "drizzle", 53: "drizzle", 55: "heavy drizzle",
        56: "icy drizzle", 57: "icy drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
        80: "light shower", 81: "shower", 82: "heavy shower",
        85: "snow shower", 86: "heavy snow shower",
        95: "thunderstorm", 96: "hail storm", 99: "hail storm",
    },
}

_DIRS = {
    "ru": ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"],
    "en": ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
}


def wind_dir(deg: float | None) -> str:
    if deg is None:
        return ""
    return _DIRS[FRAME_LANG][round(deg / 45) % 8]


def describe(code: int | None) -> str:
    # None — нет данных, а не «ясно» (код 0)
    return "—" if code is None else DESCRIPTIONS[FRAME_LANG].get(code, "—")


def _need(value, what: str):
    """Обязательное поле: null от Open-Meteo не должен попасть в кэш —
    иначе кадр падает до следующего обновления, хотя прошлые данные были."""
    if value is None:
        raise ValueError(f"Open-Meteo: нет значения {what}")
    return value


async def fetch() -> dict:
    """Возвращает нормализованный словарь; бросает исключение при сбое сети
    или неполном ответе — тогда в кэше остаются прежние данные."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(API, params=PARAMS)
        r.raise_for_status()
        return normalize(r.json())


def normalize(raw: dict) -> dict:
    cur, day, hour = raw["current"], raw["daily"], raw["hourly"]

    # почасовой срез: от текущего часа на сутки вперёд
    now_h = datetime.fromisoformat(cur["time"]).replace(minute=0)
    hourly = []
    for i, t in enumerate(hour["time"]):
        if datetime.fromisoformat(t) < now_h:
            continue
        if hour["temperature_2m"][i] is None:      # дыра в ряду — точку пропускаем
            continue
        hourly.append({
            "time": t,
            "temp": hour["temperature_2m"][i],
            "prob": hour["precipitation_probability"][i] or 0,
            "mm": hour["precipitation"][i] or 0,
        })
        if len(hourly) >= HOURLY_WINDOW:
            break

    daily = []
    for i, date in enumerate(day["time"]):
        shown = i < 4                              # эти дни рисуются на карточках
        daily.append({
            "date": date,
            "code": day["weather_code"][i],
            "tmax": _need(day["temperature_2m_max"][i], f"tmax {date}") if shown
                    else day["temperature_2m_max"][i],
            "tmin": _need(day["temperature_2m_min"][i], f"tmin {date}") if shown
                    else day["temperature_2m_min"][i],
            "precip_mm": day["precipitation_sum"][i],
            "precip_prob": day["precipitation_probability_max"][i],
            "sunrise": _need(day["sunrise"][i], "sunrise") if i == 0 else day["sunrise"][i],
            "sunset": _need(day["sunset"][i], "sunset") if i == 0 else day["sunset"][i],
        })
    if not daily:
        raise ValueError("Open-Meteo: пустой дневной прогноз")

    return {
        "place": PLACE,
        "updated": datetime.now(ZoneInfo(TZ)).isoformat(timespec="minutes"),
        "current": {
            "temp": _need(cur["temperature_2m"], "temperature_2m"),
            "feels": _need(cur["apparent_temperature"], "apparent_temperature"),
            "humidity": _need(cur["relative_humidity_2m"], "relative_humidity_2m"),
            "code": cur["weather_code"],
            "wind_ms": _need(cur["wind_speed_10m"], "wind_speed_10m"),
            "wind_dir": wind_dir(cur.get("wind_direction_10m")),
            # станционное давление в мм рт. ст. — привычная величина
            "pressure_mmhg": round(_need(cur["surface_pressure"], "surface_pressure")
                                   * 0.750062),
            "is_day": bool(cur.get("is_day", 1)),
        },
        "daily": daily,
        "hourly": hourly,
    }
