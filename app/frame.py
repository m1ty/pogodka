"""Рендер 800x480 карточки погоды для e-ink рамки Spectra 6 (E6).

Макет карточный: шапка с местом и датой; крупная панель текущей погоды слева,
четыре карточки прогноза справа (сегодня выделено); панель метрик с пиктограммами
(ветер/влажность/давление/восход/закат); внизу почасовой график температуры на
сутки со столбиками осадков. Только чистые палитрные цвета — прошивка отображает
их 1:1 без дизеринга.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timedelta
from math import cos, floor, pi, sin

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from weather import FRAME_LANG, describe

W, H = 800, 480
RENDER_VERSION = 6   # менять при правках макета — форсирует перерисовку рамки
MARGIN = 18          # общий внешний отступ карточки

# данные старше этого — сеть/Open-Meteo недоступны: помечаем кадр
STALE_AFTER = timedelta(minutes=max(60, 2 * int(os.environ.get("REFRESH_MINUTES", "20"))))

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)
GREEN = (0, 160, 70)
BLUE = (0, 70, 200)

_FONTS = {
    True: [   # bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",   # dev на macOS
    ],
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}
_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _FONT_CACHE:
        for path in _FONTS[bold]:
            try:
                _FONT_CACHE[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _FONT_CACHE[key] = ImageFont.load_default(size)
    return _FONT_CACHE[key]


_L10N = {
    "ru": {
        "months": ["января", "февраля", "марта", "апреля", "мая", "июня",
                   "июля", "августа", "сентября", "октября", "ноября", "декабря"],
        "weekdays": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
        "date": "{wd}, {day} {month}",
        "today": "сегодня", "tomorrow": "завтра",
        "feels": "ощущается", "wind": "ветер ", "humidity": "влажность ",
        "pressure": "давление ", "sunrise": "восход ", "sunset": "закат ",
        "ms": "м/с", "mm": "мм", "stale": "данные от {}",
    },
    "en": {
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "date": "{wd}, {month} {day}",
        "today": "today", "tomorrow": "tomorrow",
        "feels": "feels like", "wind": "wind ", "humidity": "humidity ",
        "pressure": "pressure ", "sunrise": "sunrise ", "sunset": "sunset ",
        "ms": "m/s", "mm": "mmHg", "stale": "data from {}",
    },
}
L = _L10N[FRAME_LANG]
WEEKDAYS = L["weekdays"]


def _rnd(t: float) -> int:
    """Половина — вверх (24.5 → 25), как в прогнозах. Встроенный round()
    банковский: 24.5 → 24, 2.5 → 2."""
    return floor(t + 0.5)


def _fmt_temp(t: float) -> str:
    v = _rnd(t)
    return f"{v:+d}°" if v else "0°"


def _temp_color(t: float):
    # по тому же целому, что напечатано: «+25°» всегда красное, «0°» — синее
    v = _rnd(t)
    if v >= 25:
        return RED
    if v <= 0:
        return BLUE
    return BLACK


# ---------------------------------------------------------------- иконки погоды
# Стиль: контурные облака (белая заливка, чёрный контур), жёлтое солнце/луна,
# синие осадки. Контур получаем, рисуя чёрный силуэт и белый чуть меньше.

def _sun(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float):
    for i in range(8):
        a = i * pi / 4
        d.line([cx + cos(a) * r * 1.35, cy + sin(a) * r * 1.35,
                cx + cos(a) * r * 1.7, cy + sin(a) * r * 1.7],
               fill=YELLOW, width=max(2, round(r * 0.16)))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=YELLOW, outline=BLACK, width=2)


def _moon(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=YELLOW, outline=BLACK, width=2)
    # серп: «выгрызаем» белым кругом, смещённым к верхнему правому краю
    d.ellipse([cx - r * .3, cy - r * 1.45, cx + r * 1.6, cy + r * .45], fill=WHITE)


def _cloud(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    # контур без артефактов: силуэт-маска и её эрозия (MinFilter),
    # чёрное — вся маска, белое — внутренность
    w = max(2, round(s * 0.055))
    size = int(s) + 6
    m = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(m)
    o = size / 2
    parts = [
        ("e", -s * .5, -s * .18, -s * .04, s * .28),
        ("e", -s * .28, -s * .4, s * .24, s * .12),
        ("e", s * .02, -s * .22, s * .5, s * .28),
        ("r", -s * .3, 0, s * .3, s * .28),
    ]
    for kind, a, b, c, e in parts:
        (md.ellipse if kind == "e" else md.rectangle)(
            [a + o, b + o, c + o, e + o], fill=255)
    inner = m.filter(ImageFilter.MinFilter(2 * w + 1))
    pos = (round(cx - o), round(cy - o))
    d.bitmap(pos, m, fill=BLACK)
    d.bitmap(pos, inner, fill=WHITE)


def _drops(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, n=3):
    for i in range(n):
        x = cx + (i - (n - 1) / 2) * s * 0.28
        y = cy + (s * 0.05 if i % 2 else 0)
        d.line([x, y, x - s * 0.08, y + s * 0.2],
               fill=BLUE, width=max(2, round(s * 0.05)))


def _flakes(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, n=3):
    for i in range(n):
        x = cx + (i - (n - 1) / 2) * s * 0.32
        y = cy + s * 0.12 + (s * 0.05 if i % 2 else 0)
        r = s * 0.09
        for k in range(3):
            a = k * pi / 3
            d.line([x - cos(a) * r, y - sin(a) * r, x + cos(a) * r, y + sin(a) * r],
                   fill=BLUE, width=max(2, round(s * .03)))


def _bolt(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    d.polygon([(cx + s * .06, cy - s * .1), (cx - s * .16, cy + s * .18),
               (cx - s * .02, cy + s * .18), (cx - s * .1, cy + s * .44),
               (cx + s * .16, cy + s * .1), (cx + s * .02, cy + s * .1)],
              fill=YELLOW, outline=BLACK)


def _fog(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    for i in range(3):
        y = cy + s * .2 + i * s * .12
        d.line([cx - s * .4, y, cx + s * .4, y], fill=BLACK, width=max(2, round(s * .045)))


def draw_icon(d: ImageDraw.ImageDraw, code: int | None, cx: float, cy: float,
              s: float, is_day: bool = True):
    """code — WMO weather code, s — размер иконки, is_day — солнце или луна."""
    c = -1 if code is None else code    # нет данных → нейтральное облако, не солнце
    lum = _sun if is_day else _moon
    if c == 0:
        lum(d, cx, cy, s * 0.3)
    elif c in (1, 2):
        lum(d, cx - s * .18, cy - s * .2, s * 0.2)
        _cloud(d, cx + s * .06, cy + s * .12, s * 0.75)
    elif c == 3:
        _cloud(d, cx, cy, s * 0.9)
    elif c in (45, 48):
        _cloud(d, cx, cy - s * .14, s * 0.75)
        _fog(d, cx, cy, s)
    elif c in (51, 53, 55, 56, 57):
        _cloud(d, cx, cy - s * .12, s * 0.8)
        _drops(d, cx, cy + s * .2, s * .8, n=2)
    elif c in (61, 63, 65, 66, 67, 80, 81, 82):
        _cloud(d, cx, cy - s * .12, s * 0.8)
        _drops(d, cx, cy + s * .2, s, n=3)
    elif c in (71, 73, 75, 77, 85, 86):
        _cloud(d, cx, cy - s * .14, s * 0.8)
        _flakes(d, cx, cy + s * .2, s)
    elif c >= 95:
        _cloud(d, cx, cy - s * .14, s * 0.8)
        _bolt(d, cx, cy + s * .12, s * .8)
    else:
        _cloud(d, cx, cy, s * 0.9)


# ---------------------------------------------------------------- пиктограммы метрик
# Мелкие глифы (~24px) слева от значения в строке метрик.

def _mi_wind(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    w = max(2, round(s * 0.09))
    x = cx - s * .5
    for dy, ln, curl in ((-s * .3, .78, True), (0, .95, False), (s * .3, .62, True)):
        x1 = x + s * ln
        d.line([x, cy + dy, x1, cy + dy], fill=BLACK, width=w)
        if curl:                                   # маленький завиток на конце порыва
            d.arc([x1 - s * .16, cy + dy - s * .18, x1 + s * .04, cy + dy + s * .18],
                  -90, 90, fill=BLACK, width=w)


def _mi_drop(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    r = s * .34
    yb = cy + s * .14
    d.ellipse([cx - r, yb - r, cx + r, yb + r], fill=BLUE)
    d.polygon([(cx, cy - s * .5), (cx - r * .95, yb - r * .1),
               (cx + r * .95, yb - r * .1)], fill=BLUE)


def _mi_gauge(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float):
    r = s * .44
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLACK, width=max(2, round(s * .08)))
    d.line([cx, cy, cx + cos(-pi * .28) * r * .8, cy + sin(-pi * .28) * r * .8],
           fill=RED, width=max(2, round(s * .08)))
    d.ellipse([cx - s * .07, cy - s * .07, cx + s * .07, cy + s * .07], fill=BLACK)


def _mi_sun_h(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, up: bool):
    """Солнце на горизонте: восход — встаёт над линией, закат — уходит под неё."""
    r = s * .3
    yh = cy + (s * .28 if up else -s * .05)         # линия горизонта
    top = cy - r * (0.2 if up else 1.0)
    bot = cy + r * (1.0 if up else 0.2)
    d.ellipse([cx - r, top, cx + r, bot], fill=YELLOW, outline=BLACK, width=2)
    for k in (-1, 0, 1):                            # лучи: вверх на восходе, вниз на закате
        a = pi * (1.5 + k * .3) if up else pi * (0.5 + k * .3)
        d.line([cx + cos(a) * r * 1.25, cy + sin(a) * r * 1.25,
                cx + cos(a) * r * 1.7, cy + sin(a) * r * 1.7],
               fill=YELLOW, width=max(2, round(s * .08)))
    d.line([cx - s * .6, yh, cx + s * .6, yh], fill=BLACK, width=max(2, round(s * .1)))


# ---------------------------------------------------------------- примитивы макета

def _panel(d: ImageDraw.ImageDraw, box, radius=16, width=1, outline=BLACK, fill=None):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: float,
              size: int, bold=True, min_size=10) -> ImageFont.FreeTypeFont:
    """Крупнейший шрифт, при котором текст влезает в max_w.
    Страхует от «поехавших» границ на шрифтах разной ширины (Arial/DejaVu)."""
    while size > min_size and d.textlength(text, font=_font(size, bold)) > max_w:
        size -= 2
    return _font(size, bold)


def _label_bg(d: ImageDraw.ImageDraw, x: float, y: float, text: str, font,
              color, anchor="mm", pad=3):
    """Подпись с белой подложкой — читается поверх столбиков и кривой."""
    l, t, r, b = d.textbbox((x, y), text, font=font, anchor=anchor)
    d.rectangle([l - pad, t - 1, r + pad, b + 1], fill=WHITE)
    d.text((x, y), text, font=font, fill=color, anchor=anchor)


# ---------------------------------------------------------------- блоки макета

def _hero(d: ImageDraw.ImageDraw, cur: dict, box):
    """Крупная панель текущей погоды: иконка, температура, описание, «ощущается»."""
    x0, y0, x1, y1 = box
    _panel(d, box, radius=18, width=2)
    inner_r = x1 - 16
    draw_icon(d, cur["code"], x0 + 84, y0 + 78, 108, cur["is_day"])
    t_str = _fmt_temp(cur["temp"])
    tx = x0 + 168
    d.text((tx, y0 + 78), t_str,
           font=_fit_font(d, t_str, inner_r - tx, 84), fill=_temp_color(cur["temp"]),
           anchor="lm")
    desc = describe(cur["code"])
    d.text((x0 + 22, y0 + 138), desc,
           font=_fit_font(d, desc, x1 - x0 - 44, 30, bold=False), fill=BLACK)
    feels = f"{L['feels']} {_fmt_temp(cur['feels'])}"
    d.text((x0 + 22, y0 + 176), feels, font=_font(22, False), fill=BLACK)


def _forecast_card(d: ImageDraw.ImageDraw, day: dict, box, label: str, is_today: bool):
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    _panel(d, box, radius=14, width=2 if is_today else 1)

    # шапка карточки: у «сегодня» — зелёная плашка с белым текстом
    band_b = y0 + 30
    if is_today:
        d.rounded_rectangle([x0, y0, x1, band_b + 8], radius=14, fill=GREEN)
        d.rectangle([x0, band_b - 6, x1, band_b + 8], fill=GREEN)
        lab_color = WHITE
    else:
        d.line([x0 + 8, band_b, x1 - 8, band_b], fill=BLACK, width=1)
        lab_color = BLACK
    d.text((cx, y0 + 15), label, font=_fit_font(d, label, x1 - x0 - 12, 20),
           fill=lab_color, anchor="mm")

    draw_icon(d, day["code"], cx, y0 + 74, 56)
    d.text((cx, y0 + 112), _fmt_temp(day["tmax"]), font=_font(28),
           fill=_temp_color(day["tmax"]), anchor="ma")
    d.text((cx, y0 + 146), _fmt_temp(day["tmin"]), font=_font(22), fill=BLUE, anchor="ma")

    prob = day["precip_prob"] or 0
    precip = f"{prob:.0f}%"
    if (day["precip_mm"] or 0) >= 0.1:
        precip += f" · {day['precip_mm']:.1f}"
    d.text((cx, y0 + 178), precip,
           font=_fit_font(d, precip, x1 - x0 - 8, 17, bold=False),
           fill=BLUE if prob else BLACK, anchor="ma")


def _metrics(d: ImageDraw.ImageDraw, cur: dict, sr: str, ss: str, box):
    """Панель метрик: пиктограмма + значение, пять ячеек через тонкие разделители."""
    x0, y0, x1, y1 = box
    _panel(d, box, radius=12, width=1)
    cy = (y0 + y1) / 2
    inner = x1 - x0
    cells = [
        ("wind", f"{cur['wind_ms']:.0f} {L['ms']} {cur['wind_dir']}".strip()),
        ("drop", f"{cur['humidity']:.0f}%"),
        ("gauge", f"{cur['pressure_mmhg']} {L['mm']}"),
        ("up", sr),
        ("down", ss),
    ]
    for k in range(1, 5):                          # тонкие вертикальные разделители
        x = x0 + inner * k / 5
        d.line([x, y0 + 10, x, y1 - 10], fill=BLACK, width=1)

    isz, gap = 24, 8
    for i, (kind, val) in enumerate(cells):
        f = _fit_font(d, val, inner / 5 - isz - gap - 8, 19)
        tw = d.textlength(val, font=f)
        total = isz + gap + tw
        cellcx = x0 + inner * (i + 0.5) / 5
        gx = cellcx - total / 2 + isz / 2
        if kind == "wind":
            _mi_wind(d, gx, cy, isz)
        elif kind == "drop":
            _mi_drop(d, gx, cy, isz)
        elif kind == "gauge":
            _mi_gauge(d, gx, cy, isz)
        else:
            _mi_sun_h(d, gx, cy, isz, up=(kind == "up"))
        d.text((gx + isz / 2 + gap, cy), val, font=f, fill=BLACK, anchor="lm")


def _draw_chart(d: ImageDraw.ImageDraw, hourly: list[dict],
                x0: int, x1: int, y0: int, y1: int):
    """Температурная кривая на сутки + столбики вероятности осадков."""
    if len(hourly) < 2:
        return
    temps = [h["temp"] for h in hourly]
    tmin, tmax = min(temps), max(temps)
    span = max(tmax - tmin, 4.0)          # плоские сутки не растягиваем на всю высоту
    pad = (span - (tmax - tmin)) / 2
    n = len(hourly)

    def xy(i: int, t: float) -> tuple[float, float]:
        return (x0 + (x1 - x0) * i / (n - 1),
                y1 - (y1 - y0) * (t - tmin + pad) / span)

    # столбики вероятности осадков — за кривой
    bar_w = (x1 - x0) / (n - 1) * 0.5
    for i, h in enumerate(hourly):
        if h["prob"] < 10:
            continue
        bx = x0 + (x1 - x0) * i / (n - 1)
        bh = (y1 - y0) * 0.5 * h["prob"] / 100
        d.rounded_rectangle([bx - bar_w / 2, y1 - bh, bx + bar_w / 2, y1],
                            radius=max(1, round(bar_w / 3)), fill=BLUE)

    # ось часов: подписи каждые 3 часа, пунктир и день недели на полуночи
    for i, h in enumerate(hourly[:-1]):
        dt = datetime.fromisoformat(h["time"])
        bx = x0 + (x1 - x0) * i / (n - 1)
        if dt.hour == 0:
            for yy in range(y0 - 4, y1, 7):
                d.line([bx, yy, bx, yy + 3], fill=BLACK, width=1)
            _label_bg(d, bx + 4, y0 - 4, WEEKDAYS[dt.weekday()], _font(16),
                      BLACK, anchor="lm", pad=2)
        if dt.hour % 3 == 0:
            d.text((bx, y1 + 8), f"{dt.hour:02d}",
                   font=_font(15, False), fill=BLACK, anchor="ma")

    d.line([x0, y1, x1, y1], fill=BLACK, width=2)

    # кривая температуры
    pts = [xy(i, t) for i, t in enumerate(temps)]
    d.line(pts, fill=BLACK, width=3, joint="curve")
    d.ellipse([pts[0][0] - 5, pts[0][1] - 5, pts[0][0] + 5, pts[0][1] + 5],
              fill=BLACK, outline=WHITE, width=2)

    # подписи экстремумов: макс над точкой, мин под точкой; у краёв диапазона
    # переворачиваем, чтобы не налезать на подписи часов и строку метрик
    imax, imin = temps.index(tmax), temps.index(tmin)
    marks = [(imax, True)]
    if _rnd(tmin) != _rnd(tmax):            # ровные сутки: одна подпись, а не две одинаковые
        marks.append((imin, False))
    for idx, prefer_above in marks:
        x, y = pts[idx]
        x = min(max(x, x0 + 22), x1 - 22)
        above = prefer_above if (y0 + 22 < y < y1 - 26) else y > y1 - 26
        _label_bg(d, x, y + (-11 if above else 11), _fmt_temp(temps[idx]),
                  _font(18), _temp_color(temps[idx]), anchor="mb" if above else "ma")


# ---------------------------------------------------------------- кадр

def _is_stale(upd: datetime) -> bool:
    """Обновления давно не проходят — кадр показывает старый прогноз."""
    return datetime.now(upd.tzinfo) - upd > STALE_AFTER


def render_frame(data: dict) -> bytes:
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    cur, daily = data["current"], data["daily"]
    upd = datetime.fromisoformat(data["updated"])

    # ── шапка ──
    d.text((MARGIN + 4, 12), data["place"], font=_font(36), fill=BLACK)
    date_s = L["date"].format(wd=WEEKDAYS[upd.weekday()], day=upd.day,
                              month=L["months"][upd.month - 1])
    d.text((W - MARGIN - 4, 26), date_s, font=_font(24, False), fill=BLACK, anchor="ra")
    if _is_stale(upd):                      # иначе вчерашний прогноз выглядит текущим
        d.text((W - MARGIN - 4, 52), L["stale"].format(upd.strftime("%d.%m %H:%M")),
               font=_font(14), fill=RED, anchor="ra")

    # ── ряд: панель «сейчас» + карточки прогноза ──
    row_y0, row_y1 = 70, 286
    hero_x1 = MARGIN + 348
    _hero(d, cur, (MARGIN, row_y0, hero_x1, row_y1))

    fx0, fx1 = hero_x1 + 12, W - MARGIN
    gap = 8
    cw = (fx1 - fx0 - gap * 3) / 4
    today = upd.date()
    for i, day in enumerate(daily[:4]):
        cardx0 = fx0 + i * (cw + gap)
        dt = datetime.fromisoformat(day["date"]).date()
        is_today = dt == today
        label = (L["today"] if is_today else
                 L["tomorrow"] if (dt - today).days == 1 else WEEKDAYS[dt.weekday()])
        _forecast_card(d, day, (cardx0, row_y0, cardx0 + cw, row_y1), label, is_today)

    # ── панель метрик ──
    sr = datetime.fromisoformat(daily[0]["sunrise"]).strftime("%H:%M")
    ss = datetime.fromisoformat(daily[0]["sunset"]).strftime("%H:%M")
    _metrics(d, cur, sr, ss, (MARGIN, 298, W - MARGIN, 346))

    # ── низ: почасовой график ──
    _draw_chart(d, data.get("hourly") or [], 44, W - 44, 372, 442)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def frame_etag(data: dict) -> str:
    """Хэш содержимого с округлением — рамка перерисовывается только
    при заметных изменениях, а не из-за каждой десятой градуса."""
    cur, daily = data["current"], data["daily"]
    upd = datetime.fromisoformat(data["updated"])
    hourly = data.get("hourly") or []
    # всё, что напечатано на карточке: иначе рамка получит 304 и останется
    # со старой датой, восходом, направлением ветра или суммой осадков
    key = json.dumps([
        RENDER_VERSION, FRAME_LANG,
        upd.date().isoformat(), _is_stale(upd),
        _rnd(cur["temp"]), _rnd(cur["feels"]), cur["code"], cur["is_day"],
        round(cur["wind_ms"]), cur["wind_dir"],
        round(cur["humidity"] / 5), cur["pressure_mmhg"],
        daily[0]["sunrise"], daily[0]["sunset"],
        [(dd["code"], _rnd(dd["tmax"]), _rnd(dd["tmin"]),
          round((dd["precip_prob"] or 0) / 5), round(dd["precip_mm"] or 0, 1))
         for dd in daily[:4]],
        hourly[0]["time"] if hourly else None,      # сдвиг оси часов и полуночи
        [round(h["temp"] * 2) / 2 for h in hourly],
        [round(h["prob"] / 10) for h in hourly],
    ])
    return '"' + hashlib.sha256(key.encode()).hexdigest()[:20] + '"'
