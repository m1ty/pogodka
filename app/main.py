"""Pogodka — прогноз погоды для e-ink рамки (самодельный аналог TRMNL).

Тянет прогноз для Бутово (Москва) из Open-Meteo, рендерит 800x480 PNG
в палитре Spectra 6 и отдаёт его по /api/frame.png с ETag/304 —
рамка опрашивает URL и перерисовывается только когда данные обновились.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

import weather
from frame import frame_etag, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pogodka")

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "20"))

_cache: dict | None = None       # последние удачно полученные данные
_lock = asyncio.Lock()


async def _refresh() -> bool:
    """Обновляет кэш; при сбое сети оставляет старые данные."""
    global _cache
    try:
        data = await weather.fetch()
    except Exception as e:
        log.warning("weather fetch failed: %s", e)
        return False
    async with _lock:
        _cache = data
    log.info("weather updated: %s, %s°C",
             data["updated"], data["current"]["temp"])
    return True


async def _refresh_loop():
    while True:
        await asyncio.sleep(REFRESH_MINUTES * 60)
        await _refresh()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _refresh()             # первая загрузка сразу на старте
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="Pogodka", lifespan=lifespan)


async def _data() -> dict:
    if _cache is None:           # старт без сети — пробуем ещё раз лениво
        await _refresh()
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
