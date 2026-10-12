"""Pogodka — прогноз погоды для e-ink рамки (самодельный аналог TRMNL).

Тянет прогноз для Бутово (Москва) из Open-Meteo, рендерит 800x480 PNG
в палитре Spectra 6 и отдаёт его по /api/frame.png с ETag/304 —
рамка опрашивает URL и перерисовывается только когда данные обновились.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

import weather
from frame import frame_etag, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pogodka")

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "20"))
RETRY_SECONDS = 60               # ленивые повторы при пустом кэше — не чаще
WAIT_SECONDS = 5                 # сколько запрос ждёт свежих данных, потом 503

_cache: dict | None = None       # последние удачно полученные данные
_inflight: asyncio.Task | None = None
_last_attempt = 0.0


async def _refresh() -> bool:
    """Обновляет кэш; при сбое сети оставляет старые данные."""
    global _cache
    try:
        data = await weather.fetch()
    except Exception as e:
        log.warning("weather fetch failed: %s", e)
        return False
    _cache = data
    log.info("weather updated: %s, %s°C",
             data["updated"], data["current"]["temp"])
    return True


def _kick_refresh() -> asyncio.Task | None:
    """Одна попытка в полёте и не чаще RETRY_SECONDS: без сети каждый опрос
    рамки иначе делал свой 20-секундный запрос к Open-Meteo."""
    global _inflight, _last_attempt
    if _inflight is not None and not _inflight.done():
        return _inflight
    if time.monotonic() - _last_attempt < RETRY_SECONDS:
        return None
    _last_attempt = time.monotonic()
    _inflight = asyncio.create_task(_refresh())
    return _inflight


async def _refresh_loop():
    while True:
        await asyncio.sleep(REFRESH_MINUTES * 60)
        await _refresh()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # первая загрузка — в фоне: сервис сразу отвечает (healthz при деплое),
    # а не висит до таймаута Open-Meteo без сети
    _kick_refresh()
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="Pogodka", lifespan=lifespan)


async def _data() -> dict:
    if _cache is None:           # старт без сети — ждём фоновую попытку недолго
        task = _kick_refresh()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
    if _cache is None:
        raise HTTPException(503, "weather data not available yet")
    return _cache


@app.get("/api/weather")
async def api_weather():
    return await _data()


@app.get("/api/frame.png")
async def frame_png(request: Request):
    """Карточка для рамки. If-None-Match/304 экономят батарею и циклы E6."""
    data = await _data()
    etag = frame_etag(data)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    png = await asyncio.to_thread(render_frame, data)
    return Response(png, media_type="image/png",
                    headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "cached": _cache is not None,
            "updated": _cache["updated"] if _cache else None}


@app.get("/")
async def index():
    return HTMLResponse(
        '<body style="background:#111;display:grid;place-items:center;height:100vh;margin:0">'
        '<img src="/api/frame.png" style="border:1px solid #444;max-width:96vw">'
        "</body>")
