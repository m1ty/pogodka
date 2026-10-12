"""Офлайн-тесты: нормализация ответа Open-Meteo, округление и цвета,
ETag кадра, пометка устаревших данных, ленивое обновление кэша."""
import asyncio
import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import frame  # noqa: E402
import main  # noqa: E402
import weather  # noqa: E402

TZ = ZoneInfo("Europe/Moscow")


def _raw():
    """Минимальный ответ Open-Meteo: сейчас + 5 дней + почасовка на 2 суток."""
    start = datetime(2026, 7, 29, 0, 0)
    hours = [(start + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(48)]
    days = [(start + timedelta(days=i)).date().isoformat() for i in range(5)]
    return {
        "current": {"time": "2026-07-29T16:15", "temperature_2m": 18.0,
                    "apparent_temperature": 18.0, "relative_humidity_2m": 79,
                    "weather_code": 3, "wind_speed_10m": 2.0, "wind_direction_10m": 270,
                    "surface_pressure": 987.0, "is_day": 1},
        "daily": {"time": days, "weather_code": [61, 95, 2, 2, 1],
                  "temperature_2m_max": [18, 21, 25, 27, 26],
                  "temperature_2m_min": [14, 14, 16, 15, 15],
                  "precipitation_sum": [10.7, 3.5, 0, 0, 0],
                  "precipitation_probability_max": [93, 60, 0, 0, 10],
                  "sunrise": [f"{d}T04:30" for d in days],
                  "sunset": [f"{d}T20:41" for d in days]},
        "hourly": {"time": hours, "temperature_2m": [15 + (i % 7) for i in range(48)],
                   "precipitation_probability": [30] * 48, "precipitation": [0.1] * 48},
    }


def _data(**updated_kw):
    d = weather.normalize(_raw())
    if updated_kw:
        d["updated"] = (datetime.now(TZ) - timedelta(**updated_kw)).isoformat(timespec="minutes")
    return d


# ---------------------------------------------------------------- normalize

def test_normalize_ok():
    d = _data()
    assert d["current"]["wind_dir"] in ("З", "W")
    assert len(d["daily"]) == 5 and len(d["hourly"]) == 25


@pytest.mark.parametrize("where,key,idx", [
    ("current", "apparent_temperature", None),
    ("current", "temperature_2m", None),
    ("daily", "temperature_2m_max", 0),
    ("daily", "sunrise", 0),
])
def test_normalize_rejects_null(where, key, idx):
    """null в обязательном поле — исключение, а не отравленный кэш."""
    raw = _raw()
    if idx is None:
        raw[where][key] = None
    else:
        raw[where][key][idx] = None
    with pytest.raises(ValueError):
        weather.normalize(raw)


def test_normalize_skips_null_hourly_temp():
    raw = _raw()
    raw["hourly"]["temperature_2m"][17] = None      # 17:00, в окне
    temps = [h["temp"] for h in weather.normalize(raw)["hourly"]]
    assert None not in temps


def test_bad_fetch_keeps_previous_cache(monkeypatch):
    good = _data()
    main._cache = good

    async def bad():
        raw = _raw()
        raw["current"]["apparent_temperature"] = None
        return weather.normalize(raw)

    monkeypatch.setattr(weather, "fetch", bad)
    assert asyncio.run(main._refresh()) is False
    assert main._cache is good
    main._cache = None


# ---------------------------------------------------------------- округление и цвет

@pytest.mark.parametrize("t,txt", [(24.5, "+25°"), (2.5, "+3°"), (-2.5, "-2°"),
                                   (0.4, "0°"), (-0.6, "-1°"), (24.4, "+24°")])
def test_fmt_temp_half_up(t, txt):
    assert frame._fmt_temp(t) == txt


def test_color_matches_printed_number():
    assert frame._temp_color(24.6) == frame.RED      # печатается «+25°»
    assert frame._temp_color(24.4) == frame.BLACK    # «+24°»
    assert frame._temp_color(0.4) == frame.BLUE      # «0°»
    assert frame._temp_color(0.6) == frame.BLACK     # «+1°»


def test_describe_none_is_dash():
    assert weather.describe(None) == "—"
    assert weather.describe(0) in ("ясно", "clear")


# ---------------------------------------------------------------- ETag

@pytest.mark.parametrize("mutate", [
    lambda d: d["current"].__setitem__("wind_dir", "Ю"),
    lambda d: d["daily"][0].__setitem__("sunrise", "2026-07-29T04:32"),
    lambda d: d["daily"][1].__setitem__("precip_mm", 7.2),
    lambda d: d.__setitem__("updated", "2026-07-30T00:05+03:00"),
])
def test_etag_tracks_printed_fields(mutate):
    d = _data()
    before = frame.frame_etag(d)
    d2 = copy.deepcopy(d)
    mutate(d2)
    assert frame.frame_etag(d2) != before


def test_etag_stable_for_tiny_changes():
    d = _data()
    d2 = copy.deepcopy(d)
    d2["current"]["temp"] += 0.1
    assert frame.frame_etag(d2) == frame.frame_etag(d)


def test_stale_flag_changes_etag_and_renders():
    fresh, stale = _data(minutes=5), _data(hours=3)
    assert not frame._is_stale(datetime.fromisoformat(fresh["updated"]))
    assert frame._is_stale(datetime.fromisoformat(stale["updated"]))
    assert frame.frame_etag(stale) != frame.frame_etag(fresh)
    assert frame.render_frame(stale)[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- рендер

def test_render_none_code_and_flat_chart(monkeypatch):
    d = _data()
    d["current"]["code"] = None
    for h in d["hourly"]:
        h["temp"] = 5.0
    labels = []
    real = frame._label_bg
    monkeypatch.setattr(frame, "_label_bg",
                        lambda dr, x, y, text, *a, **k: (labels.append(text), real(dr, x, y, text, *a, **k)))
    assert frame.render_frame(d)[:8] == b"\x89PNG\r\n\x1a\n"
    assert labels.count("+5°") == 1, labels      # ровные сутки: одна подпись


# ---------------------------------------------------------------- ленивое обновление

def test_lazy_refresh_single_inflight_and_rate_limited(monkeypatch):
    """Без сети: одна попытка на всех, не чаще RETRY_SECONDS, ответ 503 быстро."""
    calls = []

    async def slow_fail():
        calls.append(1)
        await asyncio.sleep(0.3)
        raise OSError("network down")

    monkeypatch.setattr(weather, "fetch", slow_fail)
    monkeypatch.setattr(main, "WAIT_SECONDS", 1)
    main._cache, main._inflight, main._last_attempt = None, None, 0.0

    async def run():
        res = await asyncio.gather(*(main._data() for _ in range(5)), return_exceptions=True)
        again = await asyncio.gather(main._data(), return_exceptions=True)  # сразу повтор
        return res + again

    res = asyncio.run(run())
    assert all(getattr(r, "status_code", None) == 503 for r in res)
    assert len(calls) == 1, calls
