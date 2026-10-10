"""
Telegram-бот для управления Discord-авторегом.

Управление через inline-кнопки.  Статистика отправляется картинкой.
Периоды: 1ч · 3ч · сегодня · неделя · месяц · всё.
Логи: ввод номера матча → информация из файла статистики.
Доступ: TELEGRAM_ADMIN_IDS (env) + динамический список.
"""

import asyncio
import io
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s tg_bot: %(message)s",
)
log = logging.getLogger("tg-admin-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
STATS_FILE = os.getenv("STATS_FILE", "/data/registration_stats.json")
COMMAND_PERMISSIONS_FILE = os.getenv(
    "COMMAND_PERMISSIONS_FILE", "/data/command_permissions.json"
)
TG_COMMANDS_FILE = os.getenv("TG_COMMANDS_FILE", "/data/tg_commands.json")
TG_PERMISSIONS_FILE = os.getenv(
    "TG_PERMISSIONS_FILE", "/data/tg_permissions.json"
)
STATS_TIMEZONE = ZoneInfo(os.getenv("STATS_TIMEZONE", "Europe/Moscow"))
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

FONT_CACHE_DIR = "/tmp/tg_bot_fonts"
FONT_REGULAR: Optional[str] = None
FONT_BOLD: Optional[str] = None

# ═══════════════════════════════════════════════════════════════════════════
# Font loader — downloads Noto Sans (Cyrillic support) once
# ═══════════════════════════════════════════════════════════════════════════

_FONT_URLS = {
    "regular": "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf",
    "bold": "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf",
}

_SYSTEM_FONT_PATHS = [
    # Regular
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ],
    # Bold
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ],
]


def _find_system_font(paths: list[str]) -> Optional[str]:
    for p in paths:
        if os.path.exists(p):
            # Verify it supports Cyrillic by trying to load it
            try:
                f = ImageFont.truetype(p, 20)
                # Quick test: render a Cyrillic char
                img = Image.new("RGB", (40, 40))
                d = ImageDraw.Draw(img)
                bbox = d.textbbox((0, 0), "Ш", font=f)
                if bbox[2] - bbox[0] > 2:
                    return p
            except Exception:
                continue
    return None


async def ensure_fonts() -> None:
    global FONT_REGULAR, FONT_BOLD

    if FONT_REGULAR and os.path.exists(FONT_REGULAR):
        return

    # Try system fonts first
    sys_regular = _find_system_font(_SYSTEM_FONT_PATHS[0])
    sys_bold = _find_system_font(_SYSTEM_FONT_PATHS[1])
    if sys_regular:
        FONT_REGULAR = sys_regular
        FONT_BOLD = sys_bold or sys_regular
        log.info("Системный шрифт: %s", FONT_REGULAR)
        return

    # Download Noto Sans
    os.makedirs(FONT_CACHE_DIR, exist_ok=True)
    target = os.path.join(FONT_CACHE_DIR, "NotoSans.ttf")
    if os.path.exists(target):
        FONT_REGULAR = target
        FONT_BOLD = target
        return

    log.info("Скачиваю шрифт Noto Sans для кириллицы...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _FONT_URLS["regular"],
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(target, "wb") as f:
                        f.write(data)
                    FONT_REGULAR = target
                    FONT_BOLD = target
                    log.info("Шрифт скачан: %s (%d байт)", target, len(data))
                else:
                    log.warning("Не удалось скачать шрифт: HTTP %s", resp.status)
    except Exception as exc:
        log.warning("Ошибка скачивания шрифта: %s", exc)

    if not FONT_REGULAR:
        FONT_REGULAR = None
        FONT_BOLD = None


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = (FONT_BOLD if bold else FONT_REGULAR) or FONT_REGULAR
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ═══════════════════════════════════════════════════════════════════════════
# TG commands queue (TG → Discord bot)
# ═══════════════════════════════════════════════════════════════════════════

def load_tg_commands() -> dict:
    try:
        with open(TG_COMMANDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pending": [], "responses": {}}
    except Exception:
        return {"pending": [], "responses": {}}


def save_tg_commands(data: dict) -> None:
    os.makedirs(os.path.dirname(TG_COMMANDS_FILE) or ".", exist_ok=True)
    tmp = TG_COMMANDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TG_COMMANDS_FILE)


def push_tg_command(cmd: str, args: str = "") -> str:
    """Push a command and return its unique id for response tracking."""
    import uuid
    cmd_id = uuid.uuid4().hex[:12]
    data = load_tg_commands()
    data.setdefault("pending", []).append({
        "id": cmd_id,
        "cmd": cmd,
        "args": args,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    save_tg_commands(data)
    return cmd_id


def poll_tg_response(cmd_id: str) -> Optional[dict]:
    """Check if Discord bot wrote a response for this command id."""
    data = load_tg_commands()
    responses = data.get("responses", {})
    return responses.get(cmd_id)


def clear_tg_response(cmd_id: str) -> None:
    """Remove a consumed response."""
    data = load_tg_commands()
    responses = data.get("responses", {})
    if cmd_id in responses:
        del responses[cmd_id]
        data["responses"] = responses
        save_tg_commands(data)


async def send_command_and_wait(
    session: aiohttp.ClientSession,
    chat_id: int,
    message_id: int,
    cmd: str,
    pending_text: str,
) -> None:
    """Send a command to Discord bot and wait for response, updating the message."""
    cmd_id = push_tg_command(cmd)

    # Show pending state
    await edit_message(
        session, chat_id, message_id,
        f"⏳ {pending_text}...",
        reply_markup=back_keyboard(),
    )

    # Poll for response (max ~12 seconds)
    response = None
    for _ in range(24):
        await asyncio.sleep(0.5)
        response = poll_tg_response(cmd_id)
        if response:
            break

    if response:
        clear_tg_response(cmd_id)
        is_active = response.get("is_active", False)
        channels = response.get("channels", 0)
        text = response.get("text", "✅ Выполнено")
        status_line = f"\n\n{'🟢' if is_active else '🔴'} Статус: {'активен' if is_active else 'остановлен'} · Каналов: {channels}"
        await edit_message(
            session, chat_id, message_id,
            f"{text}{status_line}",
            reply_markup=back_keyboard(),
        )
    else:
        await edit_message(
            session, chat_id, message_id,
            f"⚠️ Команда отправлена, но Discord-бот не ответил за 12 сек.\n"
            f"Команда: <code>{cmd}</code>\n\n"
            f"Возможно бот перезапускается или файл очереди недоступен.",
            reply_markup=back_keyboard(),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Discord permissions
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_ids(values: object) -> set[int]:
    result: set[int] = set()
    if not isinstance(values, (list, tuple, set)):
        return result
    for v in values:
        try:
            uid = int(v)
            if uid > 0:
                result.add(uid)
        except (TypeError, ValueError):
            pass
    return result


def load_discord_permissions() -> dict:
    try:
        with open(COMMAND_PERMISSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


def save_discord_permissions(data: dict) -> None:
    os.makedirs(os.path.dirname(COMMAND_PERMISSIONS_FILE) or ".", exist_ok=True)
    tmp = COMMAND_PERMISSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COMMAND_PERMISSIONS_FILE)


def grant_discord_access(user_id: int) -> bool:
    data = load_discord_permissions()
    ids = _normalize_ids(data.get("start_end_user_ids"))
    if user_id in ids:
        return False
    ids.add(user_id)
    data["start_end_user_ids"] = sorted(ids)
    save_discord_permissions(data)
    return True


def revoke_discord_access(user_id: int) -> bool:
    data = load_discord_permissions()
    ids = _normalize_ids(data.get("start_end_user_ids"))
    if user_id not in ids:
        return False
    ids.remove(user_id)
    data["start_end_user_ids"] = sorted(ids)
    save_discord_permissions(data)
    return True


def list_discord_access() -> list[int]:
    data = load_discord_permissions()
    return sorted(_normalize_ids(data.get("start_end_user_ids")))


# ═══════════════════════════════════════════════════════════════════════════
# Telegram permission system
# ═══════════════════════════════════════════════════════════════════════════

def _load_tg_perms() -> dict:
    try:
        with open(TG_PERMISSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


def _save_tg_perms(data: dict) -> None:
    os.makedirs(os.path.dirname(TG_PERMISSIONS_FILE) or ".", exist_ok=True)
    tmp = TG_PERMISSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TG_PERMISSIONS_FILE)


def grant_tg_access(identifier: str, label: str = "") -> bool:
    """identifier can be numeric ID or @username (without @)."""
    data = _load_tg_perms()
    allowed: dict = data.get("allowed_users", {})
    key = str(identifier).lower().lstrip("@")
    if key in allowed:
        return False
    allowed[key] = {"label": label, "granted_at": datetime.now(timezone.utc).isoformat()}
    data["allowed_users"] = allowed
    _save_tg_perms(data)
    return True


def revoke_tg_access(identifier: str) -> bool:
    data = _load_tg_perms()
    allowed: dict = data.get("allowed_users", {})
    key = str(identifier).lower().lstrip("@")
    if key not in allowed:
        return False
    del allowed[key]
    data["allowed_users"] = allowed
    _save_tg_perms(data)
    return True


def list_tg_access() -> dict:
    return _load_tg_perms().get("allowed_users", {})


def is_root_admin(user_id: int) -> bool:
    return user_id in TELEGRAM_ADMIN_IDS


def has_access(user_id: int, username: Optional[str] = None) -> bool:
    if is_root_admin(user_id):
        return True
    allowed = _load_tg_perms().get("allowed_users", {})
    if str(user_id) in allowed:
        return True
    if username and str(username).lower().lstrip("@") in allowed:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Stats data
# ═══════════════════════════════════════════════════════════════════════════

def load_registrations() -> list[dict]:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _parse_moment(r: dict) -> Optional[datetime]:
    for key in ("card_posted_at", "registered_at"):
        try:
            val = datetime.fromisoformat(str(r[key]))
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            return val.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            pass
    return None


PERIOD_CONFIG = {
    "today": {"label": "Сегодня", "short": "24ч"},
    "1h": {"label": "1 час", "short": "1ч"},
    "3h": {"label": "3 часа", "short": "3ч"},
    "week": {"label": "Неделя", "short": "7д"},
    "month": {"label": "Месяц", "short": "30д"},
    "all": {"label": "Всё время", "short": "Всё"},
}


def compute_stats(period: str = "today") -> dict:
    records = [
        r for r in load_registrations()
        if r.get("kind", "registered") == "registered"
    ]
    moments: list[datetime] = []
    for r in records:
        m = _parse_moment(r)
        if m:
            moments.append(m)

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(STATS_TIMEZONE)
    tz_label = "МСК" if str(STATS_TIMEZONE) == "Europe/Moscow" else str(STATS_TIMEZONE)

    # Period boundaries
    if period == "1h":
        period_start = now - timedelta(hours=1)
        period_label = "За последний час"
        bar_count = 12
        bar_delta = timedelta(minutes=5)
        bar_fmt = "%H:%M"
        bar_label_every = 3
    elif period == "3h":
        period_start = now - timedelta(hours=3)
        period_label = "За 3 часа"
        bar_count = 18
        bar_delta = timedelta(minutes=10)
        bar_fmt = "%H:%M"
        bar_label_every = 3
    elif period == "week":
        period_start = now - timedelta(days=7)
        period_label = "За неделю"
        bar_count = 7
        bar_delta = timedelta(days=1)
        bar_fmt = "%d.%m"
        bar_label_every = 1
    elif period == "month":
        period_start = now - timedelta(days=30)
        period_label = "За месяц"
        bar_count = 30
        bar_delta = timedelta(days=1)
        bar_fmt = "%d"
        bar_label_every = 3
    elif period == "all":
        if moments:
            period_start = min(moments) - timedelta(hours=1)
        else:
            period_start = now - timedelta(days=30)
        total_span = (now - period_start).total_seconds()
        if total_span < 86400 * 2:
            bar_count = 24
            bar_delta = timedelta(hours=1)
        elif total_span < 86400 * 60:
            bar_count = min(30, max(7, int(total_span / 86400)))
            bar_delta = timedelta(seconds=total_span / bar_count)
        else:
            bar_count = 30
            bar_delta = timedelta(seconds=total_span / bar_count)
        period_label = "Всё время"
        bar_fmt = "%d.%m" if total_span > 86400 * 3 else "%H:%M"
        bar_label_every = max(1, bar_count // 8)
    else:
        # today: from 00:00 local time
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = today_start.astimezone(timezone.utc)
        period_label = f"Сегодня ({tz_label})"
        # bars = hours from midnight to now
        hours_passed = max(1, int((now - period_start).total_seconds() / 3600) + 1)
        bar_count = min(24, hours_passed)
        bar_delta = timedelta(hours=1)
        bar_fmt = "%H"
        bar_label_every = max(1, bar_count // 8)

    period_moments = [m for m in moments if m >= period_start]
    count = len(period_moments)

    # Build histogram
    bars: list[int] = []
    bar_labels: list[str] = []
    for i in range(bar_count):
        bar_start = period_start + bar_delta * i
        bar_end = bar_start + bar_delta
        bars.append(sum(bar_start <= m < bar_end for m in moments))
        label_time = bar_start.astimezone(STATS_TIMEZONE)
        bar_labels.append(label_time.strftime(bar_fmt))

    # Summary lines
    today_start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    yesterday_start_utc = today_start_utc - timedelta(days=1)

    def since(delta: timedelta) -> int:
        return sum(now - delta <= m <= now for m in moments)

    summary = {
        "total": len(moments),
        "today": sum(today_start_utc <= m <= now for m in moments),
        "yesterday": sum(yesterday_start_utc <= m < today_start_utc for m in moments),
        "h24": since(timedelta(hours=24)),
        "h1": since(timedelta(hours=1)),
        "m30": since(timedelta(minutes=30)),
    }

    return {
        "period": period,
        "period_label": period_label,
        "period_count": count,
        "summary": summary,
        "bars": bars,
        "bar_labels": bar_labels,
        "bar_label_every": bar_label_every,
        "tz": tz_label,
        "generated": local_now.strftime("%d.%m.%Y %H:%M:%S"),
    }


def render_stats_image(stats: dict) -> bytes:
    W, H = 780, 600
    BG = (24, 24, 37)
    HEADER_BG = (30, 30, 46)
    TEXT = (205, 214, 244)
    ACCENT = (137, 180, 250)
    BAR_FG = (166, 227, 161)
    BAR_BG = (49, 50, 68)
    MUTED = (147, 153, 178)
    WHITE = (230, 230, 240)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    fb = get_font(24, bold=True)
    fm = get_font(17)
    fmb = get_font(17, bold=True)
    fs = get_font(13)
    fsb = get_font(13, bold=True)

    # ── Header ──
    draw.rectangle([(0, 0), (W, 54)], fill=HEADER_BG)
    draw.text((24, 13), f"\U0001f4ca  СТАТИСТИКА — {stats['period_label'].upper()}", fill=ACCENT, font=fb)

    # ── Summary rows ──
    y = 70
    s = stats["summary"]
    rows = [
        ("Всего", str(s["total"])),
        (f"Сегодня ({stats['tz']})", str(s["today"])),
        ("Вчера", str(s["yesterday"])),
        ("За 24 часа", str(s["h24"])),
        ("За 1 час", str(s["h1"])),
        ("За 30 минут", str(s["m30"])),
        (f"За период ({stats['period_label']})", str(stats["period_count"])),
    ]
    for label, value in rows:
        draw.text((32, y), label, fill=MUTED, font=fm)
        draw.text((480, y), value, fill=WHITE, font=fmb)
        y += 29

    # ── Histogram ──
    bars = stats.get("bars", [])
    bar_labels = stats.get("bar_labels", [])
    label_every = stats.get("bar_label_every", 1)

    if not bars:
        draw.text((32, y + 20), "Нет данных для графика", fill=MUTED, font=fm)
    else:
        y += 14
        draw.text((32, y), f"Регистрации — {stats['period_label']}", fill=ACCENT, font=fmb)
        y += 26

        chart_left = 32
        chart_right = W - 24
        chart_w = chart_right - chart_left
        bar_max_h = 130
        n = len(bars)
        gap = 2
        bw = max(4, (chart_w - gap * (n - 1)) // n)
        total_bars_w = n * bw + (n - 1) * gap
        x_offset = chart_left + (chart_w - total_bars_w) // 2

        max_val = max(bars) if bars else 1

        for i, val in enumerate(bars):
            x0 = x_offset + i * (bw + gap)
            # Background bar
            draw.rectangle([(x0, y), (x0 + bw, y + bar_max_h)], fill=BAR_BG)
            # Value bar
            if val > 0 and max_val > 0:
                h = max(2, int(val / max_val * bar_max_h))
                draw.rectangle([(x0, y + bar_max_h - h), (x0 + bw, y + bar_max_h)], fill=BAR_FG)
                # Value on top of bar
                if h > 18 and bw >= 14:
                    draw.text((x0 + 2, y + bar_max_h - h + 2), str(val), fill=BG, font=fsb)

        # Labels below
        y2 = y + bar_max_h + 4
        for i, lbl in enumerate(bar_labels):
            if i % label_every == 0:
                x0 = x_offset + i * (bw + gap)
                draw.text((x0, y2), lbl, fill=MUTED, font=fs)

    # ── Footer ──
    draw.text((32, H - 28), f"Обновлено: {stats['generated']}", fill=MUTED, font=fs)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# Game log lookup
# ═══════════════════════════════════════════════════════════════════════════

def find_game_log(match_id: int) -> Optional[dict]:
    records = load_registrations()
    for r in records:
        try:
            if int(r.get("match_id", 0)) == match_id:
                return r
        except (TypeError, ValueError):
            pass
    return None


def format_game_log(match_id: int) -> str:
    record = find_game_log(match_id)
    if not record:
        return f"❌ Матч <b>#{match_id}</b> не найден в статистике."

    lines = [f"🎮 <b>Матч #{match_id}</b>\n"]

    kind = record.get("kind", "registered")
    kind_labels = {
        "registered": "✅ Зарегистрирован",
        "already": "ℹ️ Был зарегистрирован ранее",
        "error": "❌ Ошибка",
    }
    lines.append(f"Статус: {kind_labels.get(kind, kind)}")

    for key, label in [
        ("registered_at", "Время регистрации"),
        ("card_posted_at", "Время карточки"),
    ]:
        val = record.get(key)
        if val:
            try:
                dt = datetime.fromisoformat(str(val))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(STATS_TIMEZONE)
                lines.append(f"{label}: <code>{local.strftime('%d.%m.%Y %H:%M:%S')}</code>")
            except Exception:
                lines.append(f"{label}: <code>{val}</code>")

    # Show any extra fields
    skip_keys = {"match_id", "registered_at", "card_posted_at", "kind"}
    
    # Prioritize error reason if it exists
    error_reason = record.get("error_reason")
    if error_reason:
        lines.append(f"\n<b>Причина:</b>\n<code>{str(error_reason)[:300]}</code>")
        skip_keys.add("error_reason")

    for key, val in record.items():
        if key not in skip_keys and val is not None:
            display_val = str(val)[:200]
            lines.append(f"{key}: <code>{display_val}</code>")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Telegram API helpers
# ═══════════════════════════════════════════════════════════════════════════

async def tg_request(
    session: aiohttp.ClientSession,
    method: str,
    **kwargs,
) -> Optional[dict]:
    url = f"{TG_API}/{method}"
    # Long-polling getUpdates needs a longer HTTP timeout than the poll timeout
    req_timeout = kwargs.get("timeout", 0)
    http_timeout = max(20, int(req_timeout) + 10) if isinstance(req_timeout, (int, float)) else 60
    try:
        async with session.post(url, json=kwargs, timeout=aiohttp.ClientTimeout(total=http_timeout)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning("TG API %s error: %s", method, data)
            return data
    except Exception as exc:
        if method != "getUpdates":
            log.warning("TG API %s exception: %s: %s", method, type(exc).__name__, exc)
        else:
            log.debug("TG API getUpdates: %s: %s", type(exc).__name__, exc)
        return None


async def tg_multipart(
    session: aiohttp.ClientSession,
    method: str,
    data: aiohttp.FormData,
) -> Optional[dict]:
    url = f"{TG_API}/{method}"
    try:
        async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            result = await resp.json()
            if not result.get("ok"):
                log.warning("TG API %s error: %s", method, result)
            return result
    except Exception as exc:
        log.warning("TG API %s exception: %s", method, exc)
        return None


async def send_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> Optional[dict]:
    kwargs: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await tg_request(session, "sendMessage", **kwargs)


async def edit_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> Optional[dict]:
    kwargs: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await tg_request(session, "editMessageText", **kwargs)


async def answer_callback(
    session: aiohttp.ClientSession,
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> None:
    await tg_request(
        session, "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text, show_alert=show_alert,
    )


async def send_photo(
    session: aiohttp.ClientSession,
    chat_id: int,
    photo_bytes: bytes,
    caption: str = "",
    reply_markup: Optional[dict] = None,
) -> Optional[dict]:
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("photo", photo_bytes, filename="stats.png", content_type="image/png")
    if caption:
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
    if reply_markup:
        form.add_field("reply_markup", json.dumps(reply_markup))
    return await tg_multipart(session, "sendPhoto", form)


async def delete_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    message_id: int,
) -> None:
    await tg_request(session, "deleteMessage", chat_id=chat_id, message_id=message_id)


# ═══════════════════════════════════════════════════════════════════════════
# Keyboards
# ═══════════════════════════════════════════════════════════════════════════

def main_menu_keyboard(user_id: int) -> dict:
    buttons = [
        [
            {"text": "▶️ Старт авторег", "callback_data": "cmd:start_all"},
            {"text": "⏹ Стоп авторег", "callback_data": "cmd:stop"},
        ],
        [
            {"text": "📊 Статистика", "callback_data": "stats:today"},
            {"text": "📡 Статус", "callback_data": "cmd:status"},
        ],
        [
            {"text": "📋 Логи матча", "callback_data": "cmd:logs"},
            {"text": "👥 Права Discord", "callback_data": "cmd:dc_rights"},
        ],
    ]
    if is_root_admin(user_id):
        buttons.append([{"text": "🔑 Права TG", "callback_data": "cmd:tg_rights"}])
    return {"inline_keyboard": buttons}


def stats_period_keyboard(current: str) -> dict:
    periods = [
        ("1ч", "1h"),
        ("3ч", "3h"),
        ("24ч", "today"),
        ("7д", "week"),
        ("30д", "month"),
        ("Всё", "all"),
    ]
    row = []
    for label, key in periods:
        display = f"• {label} •" if key == current else label
        row.append({"text": display, "callback_data": f"stats:{key}"})

    return {
        "inline_keyboard": [
            row[:3],
            row[3:],
            [{"text": "🔄 Обновить", "callback_data": f"stats:{current}"}],
            [{"text": "◀️ Меню", "callback_data": "cmd:menu"}],
        ]
    }


def start_mode_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🟢 Все каналы", "callback_data": "start:все"}],
            [
                {"text": "📌 Обычные", "callback_data": "start:обычный"},
                {"text": "⭐ Приоритет", "callback_data": "start:приоритет"},
            ],
            [{"text": "◀️ Назад", "callback_data": "cmd:menu"}],
        ]
    }


def dc_rights_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📋 Список прав", "callback_data": "dc:list"}],
            [
                {"text": "➕ Выдать", "callback_data": "dc:grant_prompt"},
                {"text": "➖ Забрать", "callback_data": "dc:revoke_prompt"},
            ],
            [{"text": "◀️ Назад", "callback_data": "cmd:menu"}],
        ]
    }


def tg_rights_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📋 Список TG-прав", "callback_data": "tg:list"}],
            [
                {"text": "➕ Выдать TG-доступ", "callback_data": "tg:grant_prompt"},
                {"text": "➖ Забрать TG-доступ", "callback_data": "tg:revoke_prompt"},
            ],
            [{"text": "◀️ Назад", "callback_data": "cmd:menu"}],
        ]
    }


def back_keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "◀️ Меню", "callback_data": "cmd:menu"}]]}


# ═══════════════════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════════════════

user_pending_action: dict[int, str] = {}
ACCESS_DENIED_TEXT = "🚫 <b>Нет доступа</b>\n\nУ вас нет прав для управления ботом.\nОбратитесь к администратору."


# ═══════════════════════════════════════════════════════════════════════════
# Callback handler
# ═══════════════════════════════════════════════════════════════════════════

async def handle_callback(session: aiohttp.ClientSession, callback: dict) -> None:
    cb_id = callback["id"]
    user_id = int(callback["from"]["id"])
    username = callback["from"].get("username")
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat_id = msg.get("chat", {}).get("id", 0)
    message_id = msg.get("message_id", 0)

    if not has_access(user_id, username):
        await answer_callback(session, cb_id, "🚫 Нет доступа", show_alert=True)
        return

    # Cancel pending action if user clicks a button
    if data != "cmd:menu":
        user_pending_action.pop(user_id, None)

    # ── Menu ──
    if data == "cmd:menu":
        user_pending_action.pop(user_id, None)
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "🤖 <b>Управление Discord-авторегом</b>\n\nВыберите действие:",
            reply_markup=main_menu_keyboard(user_id),
        )

    # ── Start autoreg ──
    elif data == "cmd:start_all":
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "▶️ <b>Запуск авторега</b>\n\nВыберите режим:",
            reply_markup=start_mode_keyboard(),
        )

    elif data.startswith("start:"):
        mode = data.split(":", 1)[1]
        await answer_callback(session, cb_id, f"⏳ старт {mode}...")
        await send_command_and_wait(
            session, chat_id, message_id,
            f"старт {mode}",
            f"Запускаю авторег ({mode})",
        )

    # ── Stop ──
    elif data == "cmd:stop":
        await answer_callback(session, cb_id, "⏳ Останавливаю...")
        await send_command_and_wait(
            session, chat_id, message_id,
            "енд",
            "Останавливаю авторег",
        )

    # ── Stats with period switching ──
    elif data.startswith("stats:"):
        period = data.split(":", 1)[1]
        await answer_callback(session, cb_id, "📊 Генерирую...")
        stats = compute_stats(period)
        try:
            img = render_stats_image(stats)
            # Delete the old message, send new photo
            try:
                await delete_message(session, chat_id, message_id)
            except Exception:
                pass
            await send_photo(
                session, chat_id, img,
                reply_markup=stats_period_keyboard(period),
            )
        except Exception:
            log.exception("Ошибка рендера статистики")
            await send_message(
                session, chat_id,
                "⚠️ Не удалось отрисовать статистику.",
                reply_markup=back_keyboard(),
            )

    # ── Status ──
    elif data == "cmd:status":
        await answer_callback(session, cb_id, "⏳ Запрашиваю...")
        # Ask Discord bot for live status
        cmd_id = push_tg_command("статус")
        await edit_message(
            session, chat_id, message_id,
            "⏳ Запрашиваю статус у Discord-бота...",
            reply_markup=back_keyboard(),
        )
        response = None
        for _ in range(20):
            await asyncio.sleep(0.5)
            response = poll_tg_response(cmd_id)
            if response:
                break
        granted_dc = list_discord_access()
        granted_tg = list_tg_access()
        if response:
            clear_tg_response(cmd_id)
            is_active = response.get("is_active", False)
            channels = response.get("channels", 0)
            status_emoji = "🟢" if is_active else "🔴"
            status_text = "активен" if is_active else "остановлен"
            text = (
                f"📡 <b>Состояние</b>\n\n"
                f"Авторег: {status_emoji} <b>{status_text}</b>\n"
                f"Каналов: <b>{channels}</b>\n"
                f"Discord-пользователей с правами: <b>{len(granted_dc)}</b>\n"
                f"TG-пользователей с доступом: <b>{len(granted_tg)}</b>\n"
                f"Root-админов (env): <b>{len(TELEGRAM_ADMIN_IDS)}</b>"
            )
        else:
            text = (
                f"📡 <b>Состояние</b>\n\n"
                f"⚠️ Discord-бот не ответил на запрос статуса.\n\n"
                f"Discord-пользователей с правами: <b>{len(granted_dc)}</b>\n"
                f"TG-пользователей с доступом: <b>{len(granted_tg)}</b>"
            )
        await edit_message(session, chat_id, message_id, text, reply_markup=back_keyboard())

    # ── Logs ──
    elif data == "cmd:logs":
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "log_lookup"
        await edit_message(
            session, chat_id, message_id,
            "📋 <b>Логи матча</b>\n\nОтправьте номер матча (число):",
            reply_markup=back_keyboard(),
        )

    # ── Discord rights ──
    elif data == "cmd:dc_rights":
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "👥 <b>Права Discord (старт/стоп)</b>",
            reply_markup=dc_rights_keyboard(),
        )

    elif data == "dc:list":
        await answer_callback(session, cb_id)
        ids = list_discord_access()
        text = (
            "👥 <b>Discord-права:</b>\n\n" + "\n".join(f"• <code>{uid}</code>" for uid in ids)
            if ids
            else "ℹ️ Нет пользователей с правами."
        )
        await edit_message(session, chat_id, message_id, text, reply_markup=dc_rights_keyboard())

    elif data == "dc:grant_prompt":
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "dc_grant"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Discord ID</b> для выдачи прав (17–19 цифр):",
            reply_markup=back_keyboard(),
        )

    elif data == "dc:revoke_prompt":
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "dc_revoke"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Discord ID</b> для снятия прав:",
            reply_markup=back_keyboard(),
        )

    # ── TG rights ──
    elif data == "cmd:tg_rights":
        if not is_root_admin(user_id):
            await answer_callback(session, cb_id, "🚫 Только root-admin", show_alert=True)
            return
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "🔑 <b>Управление TG-доступом</b>",
            reply_markup=tg_rights_keyboard(),
        )

    elif data == "tg:list":
        await answer_callback(session, cb_id)
        allowed = list_tg_access()
        if allowed:
            lines = []
            for uid, info in allowed.items():
                label = info.get("label", "")
                display = f"• <code>{uid}</code>"
                if label:
                    display += f" — {label}"
                lines.append(display)
            text = "🔑 <b>TG-пользователи:</b>\n\n" + "\n".join(lines)
        else:
            text = "ℹ️ Дополнительных TG-пользователей нет."
        await edit_message(session, chat_id, message_id, text, reply_markup=tg_rights_keyboard())

    elif data == "tg:grant_prompt":
        if not is_root_admin(user_id):
            await answer_callback(session, cb_id, "🚫 Только root-admin", show_alert=True)
            return
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "tg_grant"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Telegram ID</b> или перешлите сообщение пользователя.\n"
            "Формат: <code>ID метка</code> (метка опциональна).",
            reply_markup=back_keyboard(),
        )

    elif data == "tg:revoke_prompt":
        if not is_root_admin(user_id):
            await answer_callback(session, cb_id, "🚫 Только root-admin", show_alert=True)
            return
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "tg_revoke"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Telegram ID</b> для снятия доступа:",
            reply_markup=back_keyboard(),
        )

    else:
        await answer_callback(session, cb_id, "❓")


# ═══════════════════════════════════════════════════════════════════════════
# Text input handler
# ═══════════════════════════════════════════════════════════════════════════

async def handle_text_input(
    session: aiohttp.ClientSession,
    chat_id: int,
    user_id: int,
    text: str,
) -> None:
    action = user_pending_action.pop(user_id, None)
    if not action:
        return
    if not has_access(user_id, username):
        await send_message(session, chat_id, ACCESS_DENIED_TEXT)
        return

    if action == "log_lookup":
        match = re.search(r"\d+", text)
        if not match:
            await send_message(session, chat_id, "⚠️ Отправьте номер матча (число).", reply_markup=back_keyboard())
            return
        match_id = int(match.group(0))
        result = format_game_log(match_id)
        await send_message(session, chat_id, result, reply_markup=back_keyboard())

    elif action == "dc_grant":
        match = re.search(r"\d{5,22}", text)
        if not match:
            await send_message(session, chat_id, "⚠️ Не нашёл Discord ID.", reply_markup=back_keyboard())
            return
        target = int(match.group(0))
        ok = grant_discord_access(target)
        msg = f"✅ Права выданы: <code>{target}</code>" if ok else f"ℹ️ Уже есть: <code>{target}</code>"
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    elif action == "dc_revoke":
        match = re.search(r"\d{5,22}", text)
        if not match:
            await send_message(session, chat_id, "⚠️ Не нашёл Discord ID.", reply_markup=back_keyboard())
            return
        target = int(match.group(0))
        ok = revoke_discord_access(target)
        msg = f"✅ Права сняты: <code>{target}</code>" if ok else f"ℹ️ Не было прав: <code>{target}</code>"
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    elif action == "tg_grant":
        if not is_root_admin(user_id):
            await send_message(session, chat_id, "🚫 Только root-admin.", reply_markup=back_keyboard())
            return
        match = re.search(r"\d{4,15}", text)
        if not match:
            await send_message(session, chat_id, "⚠️ Не нашёл TG ID.", reply_markup=back_keyboard())
            return
        target = int(match.group(0))
        label = text.replace(match.group(0), "").strip()[:100]
        ok = grant_tg_access(target, label)
        msg = f"✅ TG-доступ выдан: <code>{target}</code>" if ok else f"ℹ️ Уже есть: <code>{target}</code>"
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    elif action == "tg_revoke":
        if not is_root_admin(user_id):
            await send_message(session, chat_id, "🚫 Только root-admin.", reply_markup=back_keyboard())
            return
        match = re.search(r"\d{4,15}", text)
        if not match:
            await send_message(session, chat_id, "⚠️ Не нашёл TG ID.", reply_markup=back_keyboard())
            return
        target = int(match.group(0))
        ok = revoke_tg_access(target)
        msg = f"✅ TG-доступ убран: <code>{target}</code>" if ok else f"ℹ️ Не было доступа: <code>{target}</code>"
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())


# ═══════════════════════════════════════════════════════════════════════════
# Update dispatcher
# ═══════════════════════════════════════════════════════════════════════════

async def handle_update(session: aiohttp.ClientSession, update: dict) -> None:
    cb = update.get("callback_query")
    if cb:
        await handle_callback(session, cb)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = int(msg.get("from", {}).get("id", 0))
    username = msg.get("from", {}).get("username")
    text = (msg.get("text") or "").strip()

    # Forward → TG grant
    fwd_from = msg.get("forward_from")
    if fwd_from and user_pending_action.get(user_id) == "tg_grant":
        fwd_id = int(fwd_from.get("id", 0))
        fwd_name = ((fwd_from.get("first_name") or "") + " " + (fwd_from.get("last_name") or "")).strip() or str(fwd_id)
        if fwd_id and is_root_admin(user_id):
            user_pending_action.pop(user_id, None)
            ok = grant_tg_access(fwd_id, fwd_name)
            r = f"✅ TG-доступ выдан: <code>{fwd_id}</code> ({fwd_name})" if ok else f"ℹ️ Уже есть: <code>{fwd_id}</code>"
            await send_message(session, chat_id, r, reply_markup=back_keyboard())
            return

    if not text:
        return

    # Pending text input
    if user_id in user_pending_action:
        await handle_text_input(session, chat_id, user_id, text)
        return

    # /start → main menu
    cmd_raw = text.split()[0].lower().lstrip("/").split("@")[0]
    if cmd_raw in ("start", "menu", "команды", "help"):
        if not has_access(user_id, username):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        await send_message(
            session, chat_id,
            "🤖 <b>Управление Discord-авторегом</b>\n\nВыберите действие:",
            reply_markup=main_menu_keyboard(user_id),
        )
    elif cmd_raw in ("стата", "stats"):
        if not has_access(user_id, username):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        stats = compute_stats("today")
        try:
            img = render_stats_image(stats)
            await send_photo(session, chat_id, img, reply_markup=stats_period_keyboard("today"))
        except Exception:
            log.exception("Ошибка рендера")
            await send_message(session, chat_id, "⚠️ Ошибка рендера.", reply_markup=back_keyboard())


# ═══════════════════════════════════════════════════════════════════════════
# Polling loop
# ═══════════════════════════════════════════════════════════════════════════

async def polling_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN не задан — Telegram-бот не запущен.")
        return

    if not TELEGRAM_ADMIN_IDS:
        log.warning("TELEGRAM_ADMIN_IDS не задан — только динамические TG-права работают.")

    await ensure_fonts()

    log.info("Telegram-бот запущен. Root admins: %s, шрифт: %s", TELEGRAM_ADMIN_IDS, FONT_REGULAR or "default")
    offset = 0
    timeout = 30

    async with aiohttp.ClientSession() as session:
        await tg_request(session, "getUpdates", offset=-1, timeout=1)

        while True:
            try:
                resp = await tg_request(
                    session, "getUpdates",
                    offset=offset, timeout=timeout,
                    allowed_updates=["message", "edited_message", "callback_query"],
                )
                if not resp or not resp.get("ok"):
                    await asyncio.sleep(5)
                    continue

                for update in resp.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        await handle_update(session, update)
                    except Exception:
                        log.exception("Ошибка обработки update %s", update.get("update_id"))

            except asyncio.CancelledError:
                log.info("Telegram polling остановлен.")
                return
            except Exception:
                log.exception("Ошибка polling, рестарт через 10 с")
                await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(polling_loop())
