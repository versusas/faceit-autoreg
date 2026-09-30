import asyncio
import base64
import contextlib
import html
import io
import json
import logging
import math
import os
import re
import time
import traceback
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from itertools import permutations
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

BOT_VERSION = "v65-recover-zeroed-rows-2026-09-15"

# Railway environment variables
DISCORD_USER_TOKEN = os.environ["DISCORD_USER_TOKEN"]
_key_candidates: list[str] = []
for key_number in range(1, 11):
    value = os.getenv(f"GEMINI_API_KEY_{key_number}", "").strip()
    if value:
        _key_candidates.append(value)

_key_candidates.extend(
    key.strip()
    for key in os.getenv("GEMINI_API_KEYS", "").split(",")
    if key.strip()
)
_single_gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
if _single_gemini_key:
    _key_candidates.append(_single_gemini_key)

GEMINI_API_KEYS = list(dict.fromkeys(_key_candidates))
# Reserve keys: one Railway variable, keys separated by commas, spaces or new
# lines. They are used only after every main key has failed; at most 10.
GEMINI_RESERVE_KEYS_LIMIT = 10
_reserve_candidates = [
    key
    for key in dict.fromkeys(
        re.split(r"[\s,;]+", os.getenv("GEMINI_RESERVE_API_KEYS", ""))
    )
    if key and key not in GEMINI_API_KEYS
]
GEMINI_RESERVE_API_KEYS = _reserve_candidates[:GEMINI_RESERVE_KEYS_LIMIT]
GEMINI_RESERVE_KEYS_IGNORED = len(_reserve_candidates) - len(GEMINI_RESERVE_API_KEYS)
if not GEMINI_API_KEYS and GEMINI_RESERVE_API_KEYS:
    # Nothing to reserve for: the reserve list becomes the main one.
    GEMINI_API_KEYS, GEMINI_RESERVE_API_KEYS = GEMINI_RESERVE_API_KEYS, []
if not GEMINI_API_KEYS:
    raise RuntimeError(
        "Укажите GEMINI_API_KEY_1, GEMINI_API_KEY_2, GEMINI_API_KEY или "
        "GEMINI_RESERVE_API_KEYS в Railway."
    )

AI_API_STYLE = os.getenv("AI_API_STYLE", "gemini").strip().lower()
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL",
    os.getenv("GOOGLE_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
).rstrip("/")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash")
GEMINI_MODELS = [
    model.strip()
    for model in os.getenv(
        "GEMINI_MODELS",
        f"{GEMINI_MODEL},{GEMINI_FALLBACK_MODEL}",
    ).split(",")
    if model.strip()
]
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
PROCESS_CONCURRENCY = max(1, int(os.getenv("PROCESS_CONCURRENCY", "2")))

def parse_channel_ids(variable_name: str) -> set[int]:
    return {
        int(x.strip())
        for x in os.getenv(variable_name, "").split(",")
        if x.strip()
    }


_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
        "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
        "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def normalize_nickname(value: object) -> str:
    """Normalize tags, punctuation and Cyrillic/Latin spelling for matching."""
    text = strip_leading_clan_tags(str(value))
    # Scoreboards often contain a harmless creator/platform prefix while the
    # Discord roster contains the same nick with trailing digits.  For
    # example, `yt: Shamin` must match `Shamin336` instead of being registered
    # as an absent 0/0/13 player.
    text = re.sub(
        r"^\s*(?:yt|youtube|ttv|twitch|vk)\s*(?:[:|._\-–—]+\s*|\s+)",
        "",
        text,
        flags=re.I,
    )
    # One player is known on the league as the unique nickname `!`, while the
    # scoreboard may render it as a short variant like `!m`. Treat `!` and
    # `!<one letter/digit>` as the same canonical nickname.
    if re.fullmatch(r"\s*![A-Za-zА-Яа-яЁё0-9]?\s*", text):
        return "bangnick"
    text = unicodedata.normalize("NFKD", text).casefold()
    text = text.translate(_CYRILLIC_TO_LATIN)
    return "".join(character for character in text if character.isalnum())


def strip_leading_clan_tags(value: str) -> str:
    """Drop clan/league prefixes before the real nickname on every code path."""
    text = str(value).strip()
    previous = None
    while previous != text:
        previous = text
        # OLD is a league/clan tag, never part of the game nickname. Remove it
        # globally in all common renderings, regardless of case or decoration:
        # `OLD | Nick`, `old Nick`, `[OLD] Nick`, `🔴 OLD — Nick`.
        text = re.sub(
            r"^\s*[^\w\[({]*[\[({]?\s*OLD\s*[\])}]?"
            r"(?=\s|[|:·•\-–—])\s*(?:[|:·•\-–—]+\s*)?",
            "",
            text,
            flags=re.I,
        )
        # A visible separator is authoritative for arbitrary clan/league tags,
        # including mixed-case forms which cannot safely be recognized by
        # capitalization alone: `MCRW|ReNiTe`, `noob | Yaksty`,
        # `sley｜Future`, and chained `OLD | MCRW | Shkiper`.
        pipe_tag = re.match(
            r"^\s*([^|｜¦]{1,24}?)\s*[|｜¦]\s*(\S.*)$",
            text,
        )
        if pipe_tag:
            prefix = pipe_tag.group(1).strip(" [](){}<>@#`*_.,:;·•-–—")
            nickname = pipe_tag.group(2).strip()
            if prefix and nickname:
                text = nickname
                continue
        text = re.sub(r"^\s*[\[({][^\])}]{1,20}[\])}]\s*", "", text)
        # Discord may render a role/clan prefix without brackets, for example
        # `CLION 1331` or `CLION | 1331`. Only an all-uppercase/digit prefix is
        # removed; the actual nickname is everything after it.
        text = re.sub(
            r"^\s*[A-ZА-ЯЁ0-9]{2,16}(?:\s*[|:·•\-–—]\s*|\s+)(?=\S)",
            "",
            text,
        )
    return text.strip()


def nickname_similarity(first: object, second: object) -> float:
    """Match names such as versus/версус/versustop/111versus."""
    left = normalize_nickname(first)
    right = normalize_nickname(second)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # Numeric nicknames are valid in STANDOFF 2 (for example `51`).  They are
    # safe only as an exact match; fuzzy numeric matching would confuse them
    # with player IDs and scoreboard values.
    if left.isdigit() or right.isdigit():
        return 0.0

    left_without_edge_digits = re.sub(r"^\d+|\d+$", "", left)
    right_without_edge_digits = re.sub(r"^\d+|\d+$", "", right)
    if (
        left_without_edge_digits
        and right_without_edge_digits
        and left_without_edge_digits == right_without_edge_digits
    ):
        return 0.99

    shorter, longer = sorted((left, right), key=len)
    # Short roster names can have decorative text appended on the scoreboard:
    # `McL` -> `[xtng] McL Bo$$`. Three characters are accepted only as the
    # beginning of the cleaned visible name; longer names may occur anywhere.
    if len(shorter) == 3 and longer.startswith(shorter):
        return 0.93 + 0.07 * len(shorter) / len(longer)
    if len(shorter) >= 4 and shorter in longer:
        return 0.94 + 0.06 * len(shorter) / len(longer)
    return SequenceMatcher(None, left, right).ratio()


def nicknames_match(first: object, second: object) -> bool:
    return nickname_similarity(first, second) >= 0.72


def allow_full_live_tab(audit: dict) -> bool:
    """An opened TAB is acceptable only when all ten rows are visible."""
    if not audit.get("has_live_gameplay_hud"):
        return False
    try:
        left_players = list(audit.get("left_players") or [])
        right_players = list(audit.get("right_players") or [])
    except TypeError:
        return False
    return len(left_players) == 5 and len(right_players) == 5


NORMAL_CHANNEL_IDS = parse_channel_ids("NORMAL_CHANNEL_IDS")
PRIORITY_CHANNEL_IDS = parse_channel_ids("PRIORITY_CHANNEL_IDS")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
WARN_CHANNEL_ID = int(os.getenv("WARN_CHANNEL_ID", "0"))
PRO_LEAGUE_USER_IDS: set[int] = {
    # Вставляйте Discord user ID игроков Про Лиги сюда, по одному на строку:
    # 111111111111111111,
    # 222222222222222222,
}
# Эмодзи и дополнительные значки в названии роли не мешают проверке:
# роль вида `🔴 Pro League ⓘ` также распознаётся по фрагменту `Pro League`.
PRO_LEAGUE_ROLE_NAMES = {
    "Pro League",
    "🔴 Pro League",
}
# Автоварны предназначены только для участников лиг выше Default League.
# Pro League по-прежнему полностью освобождена от автоварнов.
WARNING_ELIGIBLE_ROLE_FRAGMENTS = {
    "prospect",
    "проспект",
    "division",
    "дивизион",
}
MY_ACCOUNT_ID = int(os.getenv("MY_ACCOUNT_ID", "0"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.82"))
BACKFILL_LIMIT = int(os.getenv("BACKFILL_LIMIT", "500"))
SEND_DELAY = float(os.getenv("SEND_DELAY", "0.25"))
DELETE_AFTER_REGISTRATION = os.getenv("DELETE_AFTER_REGISTRATION", "true").lower() in {
    "1", "true", "yes", "on"
}
DELETE_DELAY = float(os.getenv("DELETE_DELAY", "3.0"))
DELETE_SOURCE_AFTER_REGISTRATION = os.getenv(
    "DELETE_SOURCE_AFTER_REGISTRATION", "true"
).lower() in {"1", "true", "yes", "on"}
SOURCE_DELETE_DELAY = float(os.getenv("SOURCE_DELETE_DELAY", "1.0"))
REGISTRATION_CONFIRM_TIMEOUT = float(
    os.getenv("REGISTRATION_CONFIRM_TIMEOUT", "25.0")
)
PLAYER_MODAL_TIMEOUT = float(os.getenv("PLAYER_MODAL_TIMEOUT", "12.0"))
STATS_FILE = os.getenv("STATS_FILE", "/data/registration_stats.json")
STATS_TIMEZONE = ZoneInfo(os.getenv("STATS_TIMEZONE", "Europe/Moscow"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("faceit-reg-self")

client = discord.Client()
BOT_STARTED_AT = datetime.now(timezone.utc)
is_active = False
active_channel_ids: set[int] = set()
processed_message_ids: set[int] = set()
gemini_assignment_index = 0
processing_semaphore = asyncio.Semaphore(PROCESS_CONCURRENCY)
stats_lock = asyncio.Lock()
player_modal_lock = asyncio.Lock()
processing_match_lock = asyncio.Lock()
processing_match_ids: set[int] = set()


def load_registration_records() -> list[dict]:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("Не удалось прочитать файл статистики %s", STATS_FILE)
        return []


async def registration_exists(match_id: int) -> bool:
    """Return whether this match is already present in persistent history."""
    async with stats_lock:
        return any(
            str(item.get("match_id")) == str(match_id)
            for item in load_registration_records()
        )


async def record_registration(
    match_id: int,
    posted_at: Optional[datetime] = None,
    kind: str = "registered",
) -> bool:
    """Save a match; return False when it is already in history.

    `posted_at` is when the game card came into the channel: statistics count
    games by that time, so a backlog registered at once does not look like
    many games in the last minutes. kind="already" marks a match the
    registration bot reported as entered earlier; it is not counted.
    """
    async with stats_lock:
        records = load_registration_records()
        if any(str(item.get("match_id")) == str(match_id) for item in records):
            return False

        record = {
            "match_id": int(match_id),
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(posted_at, datetime):
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            record["card_posted_at"] = posted_at.astimezone(timezone.utc).isoformat()
        if kind != "registered":
            record["kind"] = kind
        records.append(record)
        try:
            directory = os.path.dirname(STATS_FILE)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temporary_file = f"{STATS_FILE}.tmp"
            with open(temporary_file, "w", encoding="utf-8") as file:
                json.dump(records, file, ensure_ascii=False, indent=2)
            os.replace(temporary_file, STATS_FILE)
        except Exception:
            log.exception("Не удалось сохранить статистику в %s", STATS_FILE)
        return True


async def forget_registration(match_id: int) -> None:
    """Remove a failed reservation so the source match can be retried."""
    async with stats_lock:
        records = load_registration_records()
        remaining = [
            item for item in records
            if str(item.get("match_id")) != str(match_id)
        ]
        if len(remaining) == len(records):
            return
        directory = os.path.dirname(STATS_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary_file = f"{STATS_FILE}.tmp"
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(remaining, file, ensure_ascii=False, indent=2)
        os.replace(temporary_file, STATS_FILE)


def registration_moment(item: dict) -> Optional[datetime]:
    """When the game card came in; older records only know the registration."""
    for key in ("card_posted_at", "registered_at"):
        try:
            value = datetime.fromisoformat(str(item[key]))
        except (KeyError, TypeError, ValueError):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def counted_registrations() -> list[tuple[datetime, dict]]:
    """Games this bot registered itself, with their card time, oldest first."""
    rows: list[tuple[datetime, dict]] = []
    for item in load_registration_records():
        if item.get("kind", "registered") != "registered":
            continue
        moment = registration_moment(item)
        if moment is not None:
            rows.append((moment, item))
    rows.sort(key=lambda row: row[0])
    return rows


def stats_timezone_label() -> str:
    name = str(STATS_TIMEZONE)
    return "МСК" if name == "Europe/Moscow" else name


def registration_stats_text(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    rows = counted_registrations()
    moments = [moment for moment, _ in rows]
    local_now = now.astimezone(STATS_TIMEZONE)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    def since(delta: timedelta) -> int:
        return sum(now - delta <= moment <= now for moment in moments)

    today_count = sum(today_start <= moment <= now for moment in moments)
    yesterday_count = sum(yesterday_start <= moment < today_start for moment in moments)
    legacy = sum("card_posted_at" not in item for _, item in rows)
    lines = [
        "📊 **Статистика регистраций**",
        f"Всего: **{len(moments)}**",
        f"Сегодня: **{today_count}**",
        f"Вчера: **{yesterday_count}**",
        f"За 24 часа: **{since(timedelta(hours=24))}**",
        f"За 10 часов: **{since(timedelta(hours=10))}**",
        f"За 1 час: **{since(timedelta(hours=1))}**",
        f"За 30 минут: **{since(timedelta(minutes=30))}**",
        f"Время игры — когда её карточка пришла в канал ({stats_timezone_label()}), "
        "а не когда бот её зарегистрировал.",
    ]
    if legacy:
        lines.append(
            f"У {legacy} старых записей время карточки не сохранялось — "
            "они учтены по времени регистрации."
        )
    return "\n".join(lines)


def registration_status_counts() -> dict[str, int]:
    """Return compact registration counters for the status report."""
    now = datetime.now(timezone.utc)
    moments = [moment for moment, _ in counted_registrations()]
    local_now = now.astimezone(STATS_TIMEZONE)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "total": len(moments),
        "today": sum(today_start <= moment <= now for moment in moments),
        "hour": sum(now - timedelta(hours=1) <= moment <= now for moment in moments),
    }


STATS_DAYS = 14
STATS_HOURS = 24
STATS_MODE_EMOJI = {"days": "📅", "hours": "🕐"}
# Stats message ID -> shown mode, so 📅/🕐 reactions can switch its chart.
stats_messages: dict[int, str] = {}


def registration_buckets(
    mode: str,
    now: Optional[datetime] = None,
) -> list[tuple[str, int]]:
    """(label, games) oldest first: the last 14 local days or 24 local hours."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(STATS_TIMEZONE)
    moments = [
        moment.astimezone(STATS_TIMEZONE)
        for moment, _ in counted_registrations()
        if moment <= now
    ]
    if mode == "hours":
        current = local_now.replace(minute=0, second=0, microsecond=0)
        starts = [current - timedelta(hours=offset) for offset in range(STATS_HOURS - 1, -1, -1)]
        step, label_format = timedelta(hours=1), "%H:00"
    else:
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [today - timedelta(days=offset) for offset in range(STATS_DAYS - 1, -1, -1)]
        step, label_format = timedelta(days=1), "%d.%m"
    return [
        (
            start.strftime(label_format),
            sum(start <= moment < start + step for moment in moments),
        )
        for start in starts
    ]


def nice_tick_step(peak: int) -> int:
    """Clean y-axis step giving at most five gridlines above zero."""
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if peak <= step * 5:
            return step
    return 1000 * math.ceil(peak / 5000)


def render_registration_chart(buckets: list[tuple[str, int]], mode: str) -> bytes:
    """One-series bar chart PNG on a dark surface for Discord.

    Text in the image is digits only (the built-in font has no Cyrillic);
    the title and the exact numbers go into the message text.
    """
    from PIL import ImageDraw, ImageFont

    surface, gridline, baseline, muted, ink, bar_color = (
        "#1a1a19", "#2c2c2a", "#383835", "#898781", "#ffffff", "#3987e5",
    )
    width, height = 1000, 420
    left, right, top, bottom = 56, 24, 40, 48
    plot_width = width - left - right
    plot_height = height - top - bottom
    image = Image.new("RGB", (width, height), surface)
    draw = ImageDraw.Draw(image)

    def font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    tick_font, value_font = font(15), font(17)

    def text_size(text: str, used_font) -> tuple[int, int]:
        box = draw.textbbox((0, 0), text, font=used_font)
        return box[2] - box[0], box[3] - box[1]

    values = [count for _, count in buckets]
    peak = max(values, default=0)
    step = nice_tick_step(peak)
    top_value = max(step, math.ceil(peak / step) * step)
    base_y = top + plot_height

    def y_of(value: float) -> int:
        return round(base_y - plot_height * value / top_value)

    for tick in range(step, top_value + 1, step):
        y = y_of(tick)
        draw.line([(left, y), (width - right, y)], fill=gridline, width=1)
    for tick in range(0, top_value + 1, step):
        label = str(tick)
        label_width, label_height = text_size(label, tick_font)
        draw.text(
            (left - 10 - label_width, y_of(tick) - label_height // 2 - 3),
            label,
            fill=muted,
            font=tick_font,
        )

    slot = plot_width / max(1, len(buckets))
    bar_width = int(min(24, max(6, slot * 0.55)))
    label_every = 1 if mode == "days" else 3
    last_index = len(buckets) - 1
    labelled = {last_index}
    if peak > 0:
        labelled.add(values.index(peak))
    for index, (label, value) in enumerate(buckets):
        center = left + slot * index + slot / 2
        x0 = round(center - bar_width / 2)
        x1 = x0 + bar_width
        if value > 0:
            y0 = y_of(value)
            radius = max(0, min(4, (base_y - y0) // 2))
            draw.rounded_rectangle([x0, y0, x1, base_y], radius=radius, fill=bar_color)
            if base_y - y0 > radius:
                # Square at the baseline, rounded only at the data end.
                draw.rectangle([x0, base_y - radius, x1, base_y], fill=bar_color)
        if index in labelled:
            text = str(value)
            text_width, text_height = text_size(text, value_font)
            draw.text(
                (center - text_width / 2, y_of(value) - text_height - 10),
                text,
                fill=ink,
                font=value_font,
            )
        if index % label_every == 0 or index == last_index:
            label_width, _ = text_size(label, tick_font)
            draw.text(
                (center - label_width / 2, base_y + 12),
                label,
                fill=muted,
                font=tick_font,
            )
    draw.line([(left, base_y), (width - right, base_y)], fill=baseline, width=1)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def registration_stats_message(mode: str) -> tuple[str, Optional[bytes]]:
    """Stats text with the chosen period's table, and its chart PNG."""
    buckets = registration_buckets(mode)
    if mode == "hours":
        title = f"🕐 По часам — последние {STATS_HOURS} часа"
        per_row = 6
    else:
        title = f"📅 По дням — последние {STATS_DAYS} дней"
        per_row = 7
    rows = [buckets[index:index + per_row] for index in range(0, len(buckets), per_row)]
    table = "\n".join(
        "  ".join(f"{label} {count:>2}" for label, count in row) for row in rows
    )
    text = (
        f"{registration_stats_text()}\n\n**{title}**\n```text\n{table}\n```"
        "Переключить график: 📅 дни · 🕐 часы"
    )[:2000]
    try:
        chart = render_registration_chart(buckets, mode)
    except Exception:
        log.exception("Не удалось построить график статистики")
        chart = None
    return text, chart


async def send_registration_stats(channel, mode: str = "days") -> None:
    """Post stats with a chart and 📅/🕐 reactions that switch the period."""
    text, chart = registration_stats_message(mode)
    if chart is None:
        await channel.send(text)
        return
    sent = await channel.send(
        text,
        file=discord.File(io.BytesIO(chart), filename=f"registrations-{mode}.png"),
    )
    stats_messages[int(sent.id)] = mode
    while len(stats_messages) > 50:
        stats_messages.pop(next(iter(stats_messages)))
    for emoji in STATS_MODE_EMOJI.values():
        with contextlib.suppress(Exception):
            await sent.add_reaction(emoji)


def format_uptime(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours or days:
        parts.append(f"{hours} ч")
    if minutes or hours or days:
        parts.append(f"{minutes} мин")
    parts.append(f"{secs} сек")
    return " ".join(parts)


def build_status_html(status: dict[str, object]) -> str:
    """Build a self-contained, secret-free HTML readiness report."""
    esc = lambda value: html.escape(str(value), quote=True)
    channel_items = "".join(
        f"<li><span>{esc(name)}</span></li>"
        for name in status.get("channels", [])
    ) or "<li><span>Каналы не выбраны</span></li>"
    active_class = "ok" if status.get("active") else "idle"
    active_text = "Авторег запущен" if status.get("active") else "Авторег ожидает команду"
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FACEIT AutoReg — статус</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#f9f8f7; --surface:#fff; --soft:#f0efed; --text:#2c2c2b; --muted:#7d7a75; --border:#e6e5e3; --blue:#2783de; --green:#46a171; --orange:#d5803b; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; padding:32px 20px; background:var(--bg); color:var(--text); font:16px/1.5 Arial,system-ui,sans-serif; }}
    main {{ width:min(960px,100%); margin:0 auto; }}
    header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:24px; }}
    h1 {{ margin:0 0 6px; font-size:32px; line-height:1.15; letter-spacing:-.02em; }}
    .sub {{ margin:0; color:var(--muted); }}
    .badge {{ display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border:1px solid color-mix(in srgb,var(--green) 35%,var(--border)); border-radius:999px; background:color-mix(in srgb,var(--green) 10%,var(--surface)); color:var(--green); font-weight:700; white-space:nowrap; }}
    .dot {{ width:9px; height:9px; border-radius:50%; background:currentColor; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
    .card {{ padding:20px; border:1px solid var(--border); border-radius:12px; background:var(--surface); box-shadow:0 1px 2px rgba(0,0,0,.04); }}
    .card.wide {{ grid-column:1/-1; }}
    h2 {{ margin:0 0 16px; font-size:18px; }}
    dl {{ display:grid; grid-template-columns:minmax(130px,.8fr) minmax(0,1.2fr); gap:10px 16px; margin:0; }}
    dt {{ color:var(--muted); }} dd {{ margin:0; font-weight:700; overflow-wrap:anywhere; }}
    .state {{ color:var(--green); }} .state.idle {{ color:var(--orange); }}
    ul {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:0; padding:0; list-style:none; }}
    li {{ padding:10px 12px; border-radius:8px; background:var(--soft); overflow-wrap:anywhere; }}
    footer {{ margin-top:20px; color:var(--muted); font-size:14px; }}
    code {{ font-family:Consolas,Menlo,monospace; font-size:.92em; }}
    @media (max-width:700px) {{ body {{ padding:20px 16px; }} header {{ display:block; }} .badge {{ margin-top:16px; }} .grid {{ grid-template-columns:1fr; }} .card.wide {{ grid-column:auto; }} ul {{ grid-template-columns:1fr; }} dl {{ grid-template-columns:1fr; gap:3px; }} dd {{ margin-bottom:8px; }} }}
    @media (prefers-color-scheme:dark) {{ :root {{ --bg:#191919; --surface:#202020; --soft:#2b2b2a; --text:#fff; --muted:rgba(255,255,255,.65); --border:rgba(255,255,255,.20); --blue:#5e9fe8; --green:#72bc8f; --orange:#de9255; }} .card {{ box-shadow:none; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>FACEIT AutoReg</h1><p class="sub">Отчёт готовности без токенов и API-ключей</p></div>
    <div class="badge"><span class="dot"></span>Бот на связи</div>
  </header>
  <section class="grid">
    <article class="card"><h2>Состояние</h2><dl>
      <dt>Режим</dt><dd class="state {active_class}">{active_text}</dd>
      <dt>Версия</dt><dd><code>{esc(status['version'])}</code></dd>
      <dt>Аптайм</dt><dd>{esc(status['uptime'])}</dd>
      <dt>Задержка Discord</dt><dd>{esc(status['latency'])}</dd>
      <dt>Сейчас обрабатывается</dt><dd>{esc(status['processing'])}</dd>
    </dl></article>
    <article class="card"><h2>Пользователь из Variables</h2><dl>
      <dt>Ник в Discord</dt><dd>{esc(status['configured_name'])}</dd>
      <dt>MY_ACCOUNT_ID</dt><dd><code>{esc(status['configured_id'])}</code></dd>
      <dt>Текущая сессия</dt><dd>{esc(status['session_user'])}</dd>
      <dt>Команды</dt><dd>Доступны всем пользователям</dd>
    </dl></article>
    <article class="card"><h2>Регистрации</h2><dl>
      <dt>Всего</dt><dd>{esc(status['registrations_total'])}</dd>
      <dt>Сегодня</dt><dd>{esc(status['registrations_today'])}</dd>
      <dt>За последний час</dt><dd>{esc(status['registrations_hour'])}</dd>
      <dt>Минимум игроков</dt><dd>4 совпадения</dd>
      <dt>Автоварны</dt><dd>{esc(status['warning_status'])}</dd>
      <dt>Pro League ID</dt><dd>{esc(status['pro_exemptions'])}</dd>
    </dl></article>
    <article class="card"><h2>Распознавание</h2><dl>
      <dt>API-режим</dt><dd>{esc(status['api_style'])}</dd>
      <dt>Модели</dt><dd>{esc(status['models'])}</dd>
      <dt>Ключей настроено</dt><dd>{esc(status['key_count'])}</dd>
      <dt>Параллельность</dt><dd>{esc(status['concurrency'])}</dd>
      <dt>Мин. уверенность</dt><dd>{esc(status['confidence'])}</dd>
    </dl></article>
    <article class="card wide"><h2>Каналы текущего режима</h2><ul>{channel_items}</ul></article>
  </section>
  <footer>Сформировано: {esc(status['generated_at'])} · Часовой пояс: {esc(status['timezone'])}</footer>
</main>
</body>
</html>"""


def next_gemini_assignment() -> tuple[str, str, int]:
    """Assign exactly one model and one API key to each consecutive game."""
    global gemini_assignment_index
    index = gemini_assignment_index
    gemini_assignment_index += 1
    model = GEMINI_MODELS[index % len(GEMINI_MODELS)]
    key_index = index % len(GEMINI_API_KEYS)
    api_key = GEMINI_API_KEYS[key_index]
    return model, api_key, key_index + 1


# Main keys rotate between games; reserve keys (GEMINI_RESERVE_API_KEYS) are
# used in their order only after every main key failed. A key that answered
# with a spent limit or balance rests for AI_KEY_REST_MINUTES, then is retried.
AI_KEY_REST_SECONDS = max(1.0, float(os.getenv("AI_KEY_REST_MINUTES", "30"))) * 60
AI_MAIN_KEY_SLOTS = [
    {"key": key, "reserve": False, "number": number, "label": f"основной ключ #{number}"}
    for number, key in enumerate(GEMINI_API_KEYS, 1)
]
AI_RESERVE_KEY_SLOTS = [
    {"key": key, "reserve": True, "number": number, "label": f"резервный ключ #{number}"}
    for number, key in enumerate(GEMINI_RESERVE_API_KEYS, 1)
]
ai_key_rest_until: dict[str, float] = {}
ai_key_state = {"on_reserve": False, "all_exhausted": False}


def ai_key_resting(slot: dict) -> bool:
    return ai_key_rest_until.get(slot["key"], 0.0) > time.monotonic()


def ai_key_order(assigned_main_index: int) -> list[dict]:
    """Keys to try for one game, best first.

    Working main keys starting from the one assigned to this game, then
    working reserve keys in their order, then resting keys: their limit may
    have been raised in the meantime.
    """
    start = assigned_main_index % len(AI_MAIN_KEY_SLOTS) if AI_MAIN_KEY_SLOTS else 0
    everything = AI_MAIN_KEY_SLOTS[start:] + AI_MAIN_KEY_SLOTS[:start] + AI_RESERVE_KEY_SLOTS
    return (
        [slot for slot in everything if not ai_key_resting(slot)]
        + [slot for slot in everything if ai_key_resting(slot)]
    )


def is_ai_key_exhausted(status: int, body: str) -> bool:
    """A spent limit or balance, or a rejected key — not a short overload."""
    if status == 402:
        return True
    if re.search(r"per\s*minute|rate[\s_-]*limit|overload|try again", body, re.I):
        return False
    if status in (401, 403):
        return True
    return bool(
        re.search(
            r"insufficient|balance|quota|limit_exceeded|allowance|exhausted|billing",
            body,
            re.I,
        )
    )


def short_ai_error(error: str) -> str:
    found = re.search(r'"message"\s*:\s*"([^"]{1,160})', error)
    return (found.group(1) if found else error)[:160]


def notify_ai_keys(text: str) -> None:
    log.warning("API-ключи ИИ: %s", text.replace("\n", " "))
    run_in_background(notify_log_channel(text))


def working_reserve_keys() -> int:
    return sum(not ai_key_resting(slot) for slot in AI_RESERVE_KEY_SLOTS)


def ai_keys_summary() -> str:
    """Key counts and state for `бот ты тут?`; never the keys themselves."""
    resting = sum(ai_key_resting(slot) for slot in AI_MAIN_KEY_SLOTS + AI_RESERVE_KEY_SLOTS)
    text = f"основных {len(AI_MAIN_KEY_SLOTS)}, резервных {len(AI_RESERVE_KEY_SLOTS)}"
    if ai_key_state["on_reserve"]:
        text += ", сейчас работают резервные"
    if resting:
        text += f", отдыхают после лимита: {resting}"
    if GEMINI_RESERVE_KEYS_IGNORED:
        text += (
            f", лишних резервных не используется: {GEMINI_RESERVE_KEYS_IGNORED} "
            f"(максимум {GEMINI_RESERVE_KEYS_LIMIT})"
        )
    return text



async def check_ai_key_status() -> list[dict]:
    """Query AI STAR / OpenAI-compatible API for each key's token balance."""
    results: list[dict] = []
    all_slots = AI_MAIN_KEY_SLOTS + AI_RESERVE_KEY_SLOTS
    if not all_slots:
        return results

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for slot in all_slots:
            key = slot["key"]
            masked = key[:8] + "..." + key[-4:] if len(key) > 16 else key[:4] + "..."
            info: dict = {
                "label": slot["label"],
                "masked_key": masked,
                "reserve": slot["reserve"],
                "resting": ai_key_resting(slot),
                "status": "unknown",
                "limit": None,
                "used": None,
                "remaining": None,
                "details": "",
            }

            rest_until = ai_key_rest_until.get(key, 0.0)
            if rest_until > time.monotonic():
                remaining_rest = int(rest_until - time.monotonic())
                info["rest_minutes"] = remaining_rest // 60
                info["rest_seconds"] = remaining_rest % 60

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }

            base_v1 = GEMINI_BASE_URL.rstrip("/")
            if not base_v1.endswith("/v1"):
                base_v1 += "/v1"
            base_root = base_v1[:-3] if base_v1.endswith("/v1") else base_v1

            balance_found = False
            # Enhanced endpoint list for common OpenAI proxies
            endpoints = [
                base_root + "/api/user/info",
                base_root + "/api/v1/user/info",
                base_v1 + "/key",
                base_v1 + "/dashboard/billing/subscription",
                base_v1 + "/dashboard/billing/usage",
                base_v1 + "/balance",
                base_v1 + "/me",
            ]
            
            for url in endpoints:
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            d = data.get("data") if isinstance(data.get("data"), dict) else data
                            
                            # Quota in some systems is represented as a large integer (total tokens)
                            # In One API, 1 unit = 0.002$ or similar, but often mapped to tokens
                            rem = d.get("remaining_tokens") or d.get("limit_remaining") or d.get("balance") or d.get("remaining") or d.get("left") or d.get("quota")
                            lim = d.get("total_tokens") or d.get("limit") or d.get("total") or d.get("hard_limit_usd")
                            usd = d.get("used_tokens") or d.get("usage") or d.get("used")
                            
                            if rem is not None:
                                info["remaining"] = rem
                                info["limit"] = lim
                                info["used"] = usd
                                info["status"] = "active"
                                balance_found = True
                                break
                        elif resp.status in (401, 403):
                            info["status"] = "invalid_key"
                            info["details"] = "Ключ не принят API"
                            break
                except Exception:
                    continue

            if not balance_found and info["status"] not in ("invalid_key", "forbidden"):
                try:
                    test_url = base_v1 + "/chat/completions"
                    test_payload = {
                        "model": GEMINI_MODELS[0] if GEMINI_MODELS else "test",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    }
                    async with session.post(test_url, json=test_payload, headers=headers) as resp:
                        status_code = resp.status
                        body = await resp.text()
                        if status_code == 200:
                            info["status"] = "active"
                            info["details"] = "Ключ работает (баланс через API недоступен)"
                            for h in ["x-ratelimit-remaining-tokens", "x-ratelimit-remaining", "x-ratelimit-limit-tokens"]:
                                val = resp.headers.get(h)
                                if val:
                                    if "remaining" in h:
                                        info["remaining"] = val
                                    else:
                                        info["limit"] = val
                        elif status_code in (401, 403):
                            info["status"] = "invalid_key"
                            info["details"] = "Ключ не принят API"
                        elif status_code == 402:
                            info["status"] = "exhausted"
                            info["details"] = "Баланс исчерпан"
                            info["remaining"] = 0
                        elif status_code == 429:
                            info["status"] = "rate_limited"
                            info["details"] = "Превышен лимит запросов (ключ рабочий)"
                        else:
                            if is_ai_key_exhausted(status_code, body):
                                info["status"] = "exhausted"
                                info["details"] = short_ai_error(body)
                                info["remaining"] = 0
                            else:
                                info["status"] = "error"
                                info["details"] = f"HTTP {status_code}"
                except Exception as exc:
                    info["status"] = "error"
                    info["details"] = str(exc)[:100]

            results.append(info)
    return results

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for slot in all_slots:
            key = slot["key"]
            masked = key[:8] + "..." + key[-4:] if len(key) > 16 else key[:4] + "..."
            info: dict = {
                "label": slot["label"],
                "masked_key": masked,
                "reserve": slot["reserve"],
                "resting": ai_key_resting(slot),
                "status": "unknown",
                "limit": None,
                "used": None,
                "remaining": None,
                "details": "",
            }
            rest_until = ai_key_rest_until.get(key, 0.0)
            if rest_until > time.monotonic():
                remaining_rest = int(rest_until - time.monotonic())
                info["rest_minutes"] = remaining_rest // 60
                info["rest_seconds"] = remaining_rest % 60
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            base = GEMINI_BASE_URL.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            balance_found = False
            for endpoint in ["/v1/key", "/v1/balance", "/v1/me", "/v1/account"]:
                try:
                    url = base + endpoint
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "limit_remaining" in data:
                                info["remaining"] = data.get("limit_remaining")
                                info["limit"] = data.get("limit")
                                info["used"] = data.get("usage")
                                info["status"] = "active"
                                balance_found = True
                            elif "balance" in data or "remaining" in data or "quota" in data:
                                info["remaining"] = data.get("remaining") or data.get("balance") or data.get("left")
                                info["limit"] = data.get("limit") or data.get("quota") or data.get("total")
                                info["used"] = data.get("used") or data.get("usage")
                                info["status"] = "active"
                                balance_found = True
                            elif isinstance(data.get("data"), dict):
                                d = data["data"]
                                info["remaining"] = d.get("remaining") or d.get("balance") or d.get("left")
                                info["limit"] = d.get("limit") or d.get("quota") or d.get("total")
                                info["used"] = d.get("used") or d.get("usage")
                                info["status"] = "active"
                                balance_found = True
                            if balance_found:
                                info["endpoint"] = endpoint
                                break
                        elif resp.status == 401:
                            info["status"] = "invalid_key"
                            info["details"] = "Ключ не принят API"
                        elif resp.status == 403:
                            info["status"] = "forbidden"
                            info["details"] = "Доступ запрещён"
                except Exception:
                    pass
            if not balance_found and info["status"] not in ("invalid_key", "forbidden"):
                try:
                    test_url = GEMINI_BASE_URL.rstrip("/")
                    if not test_url.endswith("/v1"):
                        test_url += "/v1"
                    test_url += "/chat/completions"
                    test_payload = {"model": GEMINI_MODELS[0] if GEMINI_MODELS else "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
                    async with session.post(test_url, json=test_payload, headers=headers) as resp:
                        status_code = resp.status
                        body = await resp.text()
                        if status_code == 200:
                            info["status"] = "active"
                            info["details"] = "Ключ работает (баланс через API недоступен)"
                            for h in ["x-ratelimit-remaining", "x-ratelimit-limit"]:
                                val = resp.headers.get(h)
                                if val:
                                    if "remaining" in h:
                                        info["remaining"] = val
                                    else:
                                        info["limit"] = val
                        elif status_code in (401, 403):
                            info["status"] = "invalid_key"
                            info["details"] = "Ключ не принят API"
                        elif status_code == 402:
                            info["status"] = "exhausted"
                            info["details"] = "Баланс исчерпан"
                            info["remaining"] = 0
                        elif status_code == 429:
                            info["status"] = "rate_limited"
                            info["details"] = "Превышен лимит запросов (ключ рабочий)"
                        else:
                            if is_ai_key_exhausted(status_code, body):
                                info["status"] = "exhausted"
                                info["details"] = short_ai_error(body)
                                info["remaining"] = 0
                            else:
                                info["status"] = "error"
                                info["details"] = f"HTTP {status_code}"
                except Exception as exc:
                    info["status"] = "error"
                    info["details"] = str(exc)[:100]
            results.append(info)
    return results


def format_ai_status_message(results: list[dict]) -> str:
    """Format the API key status results for Discord."""
    if not results:
        return "❌ Нет настроенных API-ключей."
    lines: list[str] = ["📊 **Состояние API-ключей**\n"]
    status_emoji = {"active": "🟢", "exhausted": "🔴", "invalid_key": "⛔", "forbidden": "⛔", "rate_limited": "🟡", "error": "🟠", "unknown": "⚪"}
    for info in results:
        emoji = status_emoji.get(info["status"], "⚪")
        label = info["label"].capitalize()
        line = f"{emoji} **{label}** (`{info['masked_key']}`)"
        status_text = {"active": "активен", "exhausted": "исчерпан", "invalid_key": "невалидный", "forbidden": "заблокирован", "rate_limited": "лимит запросов", "error": "ошибка", "unknown": "неизвестно"}
        line += f" — {status_text.get(info['status'], info['status'])}"
        parts: list[str] = []
        if info.get("remaining") is not None:
            parts.append(f"осталось: **{info['remaining']} токенов**")
        if info.get("used") is not None:
            parts.append(f"использовано: **{info['used']}**")
        if info.get("limit") is not None:
            parts.append(f"лимит: **{info['limit']}**")
        if parts:
            line += "\n  " + " · ".join(parts)
        if info.get("resting"):
            rm = info.get("rest_minutes", 0)
            rs = info.get("rest_seconds", 0)
            line += f"\n  ⏸️ Отдыхает: ещё {rm} мин {rs} сек"
        if info.get("details") and info["status"] not in ("active",):
            line += f"\n  ℹ️ {info['details']}"
        lines.append(line)
    total = len(results)
    active = sum(1 for r in results if r["status"] == "active")
    exhausted = sum(1 for r in results if r["status"] == "exhausted")
    resting = sum(1 for r in results if r.get("resting"))
    lines.append(f"\n**Итого:** {total} ключей · {active} активных · {exhausted} исчерпанных · {resting} на отдыхе")
    return "\n".join(lines)


def note_ai_key_exhausted(slot: dict, error: str) -> None:
    """Rest a spent key; a spent reserve key is reported with what is left."""
    newly_spent = not ai_key_resting(slot)
    ai_key_rest_until[slot["key"]] = time.monotonic() + AI_KEY_REST_SECONDS
    log.warning("ИИ: %s не работает — %s", slot["label"], short_ai_error(error))
    if not newly_spent or not slot["reserve"]:
        return
    left, total = working_reserve_keys(), len(AI_RESERVE_KEY_SLOTS)
    text = (
        f"⚠️ Резервный API-ключ ИИ #{slot['number']} закончился "
        f"({short_ai_error(error)}). Рабочих резервных ключей осталось: "
        f"**{left} из {total}**."
    )
    if left <= 1:
        text += (
            "\nРезервные ключи заканчиваются — пополните лимит или добавьте "
            "новые ключи в `GEMINI_RESERVE_API_KEYS` в Railway."
        )
    notify_ai_keys(text)


def note_ai_key_success(slot: dict) -> None:
    """Report the switch to reserve keys and the return to the main ones."""
    ai_key_rest_until.pop(slot["key"], None)
    if ai_key_state["all_exhausted"]:
        ai_key_state["all_exhausted"] = False
        notify_ai_keys(f"✅ ИИ снова отвечает ({slot['label']}).")
    if slot["reserve"]:
        if (
            not ai_key_state["on_reserve"]
            and AI_MAIN_KEY_SLOTS
            and all(ai_key_resting(main) for main in AI_MAIN_KEY_SLOTS)
        ):
            ai_key_state["on_reserve"] = True
            notify_ai_keys(
                "🔄 Основные API-ключи ИИ закончились — перешёл на резервные "
                f"(сейчас резервный ключ #{slot['number']}). Рабочих резервных "
                f"ключей: **{working_reserve_keys()} из {len(AI_RESERVE_KEY_SLOTS)}**."
            )
    elif ai_key_state["on_reserve"]:
        ai_key_state["on_reserve"] = False
        notify_ai_keys(
            f"✅ Основной API-ключ ИИ #{slot['number']} снова работает — "
            "вернулся с резервных ключей на основные."
        )


def note_ai_all_failed() -> None:
    """Report once when every configured key is spent."""
    slots = AI_MAIN_KEY_SLOTS + AI_RESERVE_KEY_SLOTS
    if not slots or not all(ai_key_resting(slot) for slot in slots):
        return
    if ai_key_state["all_exhausted"]:
        return
    ai_key_state["all_exhausted"] = True
    if AI_RESERVE_KEY_SLOTS:
        hint = (
            "Пополните лимит ключей или добавьте новые в `GEMINI_RESERVE_API_KEYS` "
            "в Railway (до 10 через запятую)."
        )
    else:
        hint = (
            "Добавьте резервные ключи в `GEMINI_RESERVE_API_KEYS` в Railway "
            "(до 10 через запятую) или пополните лимит основных."
        )
    notify_ai_keys(
        "🛑 Закончились все API-ключи ИИ: основных "
        f"{len(AI_MAIN_KEY_SLOTS)}, резервных {len(AI_RESERVE_KEY_SLOTS)}. "
        "Игры не регистрируются, карточки остаются в каналах.\n"
        f"{hint} После этого напишите `старт все`."
    )


def allowed_for_parsing(message: discord.Message) -> bool:
    """Return True if this message may be parsed automatically."""
    if client.user and message.author.id == client.user.id:
        return False
    if not active_channel_ids or message.channel.id not in active_channel_ids:
        return False
    return True


def message_parts(message: discord.Message) -> list[object]:
    parts: list[object] = [message]
    for snapshot in getattr(message, "message_snapshots", None) or []:
        parts.append(getattr(snapshot, "message", snapshot))
    return parts


def is_forwarded_message(message: discord.Message) -> bool:
    """Detect a Discord forwarded message without changing card parsing."""
    if getattr(message, "message_snapshots", None):
        return True
    flags = getattr(message, "flags", None)
    return bool(getattr(flags, "forwarded", False))


def is_test_result_card(message: discord.Message) -> bool:
    """Recognize a manually posted test card even if forward metadata is lost.

    Some Discord/discord.py-self combinations render the visible `Переслано`
    card but expose neither message_snapshots nor the forwarded flag. A result
    card outside the active registration channels is therefore a safe test,
    except in the configured log/warning channels where the bot writes copies.
    """
    channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
    if channel_id in active_channel_ids or channel_id in {
        LOG_CHANNEL_ID,
        WARN_CHANNEL_ID,
    }:
        return False
    if not image_urls(message):
        return False
    # Cards posted by the tournament bot itself in other channels (history, a
    # registration channel missing from the mode) are not tests: previewing
    # them would post =g into that channel or fail there without access.
    if getattr(getattr(message, "author", None), "bot", False):
        return False
    return bool(
        re.search(
            r"Результат\s+матча\s*#\s*\d+",
            plain_message_text(message),
            re.I,
        )
    )


# Channels already reported as receiving review cards outside the mode.
_unconfigured_card_channels: set[int] = set()


def is_unconfigured_result_card(message: discord.Message) -> bool:
    """A tournament-bot review card in a same-server channel outside the mode."""
    if not is_active:
        return False
    channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
    if (
        not channel_id
        or channel_id in active_channel_ids
        or channel_id in {LOG_CHANNEL_ID, WARN_CHANNEL_ID}
        or channel_id in _unconfigured_card_channels
    ):
        return False
    if not getattr(getattr(message, "author", None), "bot", False):
        return False
    if not image_urls(message) or not is_review_result_card(plain_message_text(message)):
        return False
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    active_guild_ids = {
        getattr(getattr(client.get_channel(active_id), "guild", None), "id", None)
        for active_id in active_channel_ids
    }
    return guild_id is not None and guild_id in active_guild_ids


async def notify_unconfigured_card_channel(message: discord.Message) -> None:
    """Tell #логи the ID of a channel that receives cards but is not in the mode."""
    channel_id = int(message.channel.id)
    found = re.search(r"Результат\s+матча\s*#\s*(\d+)", plain_message_text(message), re.I)

    def ids(values: set[int]) -> str:
        return ", ".join(str(value) for value in sorted(values)) or "не указаны"

    await notify_log_channel(
        f"ℹ️ Карточка матча #{found.group(1) if found else '?'} пришла в "
        f"<#{channel_id}> (ID `{channel_id}`), но этот канал не входит в "
        "запущенный режим — бот её не регистрирует.\n"
        f"Если это канал регистрации, добавьте `{channel_id}` в "
        "`NORMAL_CHANNEL_IDS` или `PRIORITY_CHANNEL_IDS` в Railway и напишите "
        "`старт все`.\n"
        f"Сейчас в Railway: обычные `{ids(NORMAL_CHANNEL_IDS)}`, "
        f"приоритет `{ids(PRIORITY_CHANNEL_IDS)}`."
    )


# (source message ID, Discord user ID) -> display name picked for that card.
# Warnings read the same mentions again after registration; reuse resolved
# names instead of repeating fetch_member in every shared guild.
_mention_display_name_cache: dict[tuple[int, int], str] = {}


def registration_id_from_display_name(name: str) -> Optional[int]:
    """Read the short registration number from `#124 | OLD | Shkiper`."""
    found = re.search(r"(?<!\d)#\s*(\d{1,5})(?!\d)", name)
    if not found:
        found = re.search(r"(?:^|\D)(\d{1,5})\s*\|", name)
    return int(found.group(1)) if found else None


def source_guild_ids(message: discord.Message) -> list[int]:
    """Servers whose member nicknames may give registration IDs for a card.

    A card posted in a league server uses that server. A forwarded card uses
    the server it was forwarded from (the message reference); without one, the
    servers of the registration channels. Every other shared server has
    unrelated `#number | nick` names, such as another league's #49370.
    """
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    if not is_forwarded_message(message):
        return [int(guild_id)] if guild_id else []
    ids: list[int] = []
    for part in message_parts(message):
        for reference in (
            getattr(part, "reference", None),
            getattr(part, "message_reference", None),
        ):
            reference_guild_id = getattr(reference, "guild_id", None)
            if reference_guild_id:
                ids.append(int(reference_guild_id))
    if not ids:
        for channel_id in sorted(NORMAL_CHANNEL_IDS | PRIORITY_CHANNEL_IDS):
            channel_guild_id = getattr(
                getattr(client.get_channel(channel_id), "guild", None), "id", None
            )
            if channel_guild_id:
                ids.append(int(channel_guild_id))
    if not ids and guild_id:
        ids.append(int(guild_id))
    return list(dict.fromkeys(ids))


def guild_by_id(guild_id: int, message: discord.Message):
    for guild in (getattr(message, "guild", None), *(getattr(client, "guilds", None) or [])):
        if guild is not None and getattr(guild, "id", None) == guild_id:
            return guild
    get_guild = getattr(client, "get_guild", None)
    return get_guild(guild_id) if callable(get_guild) else None


# (server ID, Discord user ID) -> server display name, for uncached members.
_guild_member_name_cache: dict[tuple[int, int], str] = {}


async def guild_member_display_name(guild, member_id: int) -> str:
    """Display name of a member in one server: cache, HTTP, then gateway query."""
    guild_id = int(getattr(guild, "id", 0) or 0)
    cached_name = _guild_member_name_cache.get((guild_id, member_id))
    if cached_name:
        return cached_name
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except Exception:
            member = None
    if member is None and callable(getattr(guild, "query_members", None)):
        try:
            for candidate in await guild.query_members(
                user_ids=[member_id], limit=1, cache=True
            ) or []:
                if getattr(candidate, "id", None) == member_id:
                    member = candidate
        except Exception:
            log.debug(
                "Не удалось запросить участника %s на сервере %s",
                member_id,
                guild_id,
                exc_info=True,
            )
    name = str(getattr(member, "display_name", "") or "").strip()
    if name and guild_id:
        _guild_member_name_cache[(guild_id, member_id)] = name
        while len(_guild_member_name_cache) > 5000:
            _guild_member_name_cache.pop(next(iter(_guild_member_name_cache)))
    return name


def without_registration_number(name: str) -> str:
    """`#49370 | Nick` from a server that is not the card's source -> `Nick`."""
    text = re.sub(r"(?<!\d)#\s*\d{1,5}(?!\d)\s*\|?\s*", "", str(name))
    text = re.sub(r"^\s*\d{1,5}\s*\|\s*", "", text)
    return text.strip(" |")


async def mention_display_names(
    text: str,
    message: discord.Message,
) -> dict[int, str]:
    """Pick the best display name for every `<@id>` mention in the text.

    Registration numbers come only from the card's source server (see
    source_guild_ids): for a forwarded test card that is the league server it
    was forwarded from, never the destination or another shared server.
    Names from anywhere else keep just the nickname, without a `#number`.
    A mention without a number stays a nickname; its ID then comes from
    «Получить игроков» on the original card. An empty name means that the
    mention must stay unresolved.
    """
    mention_ids = list(dict.fromkeys(
        int(value) for value in re.findall(r"<@!?(\d{15,22})>", text)
    ))
    forwarded = is_forwarded_message(message)
    known = {int(member.id): member for member in (message.mentions or [])}
    message_id = int(getattr(message, "id", 0) or 0)
    source_guilds = [
        guild
        for guild in (
            guild_by_id(guild_id, message) for guild_id in source_guild_ids(message)
        )
        if guild is not None
    ]
    resolved: dict[int, str] = {}
    for member_id in mention_ids:
        cached_name = _mention_display_name_cache.get((message_id, member_id))
        if cached_name:
            resolved[member_id] = cached_name
            continue

        known_name = str(
            getattr(known.get(member_id), "display_name", "") or ""
        ).strip()
        source_names: list[str] = []
        if known_name and not forwarded:
            # message.mentions of a card posted in its own server are members
            # of that server and have priority over every lookup.
            source_names.append(known_name)
        if not (source_names and registration_id_from_display_name(source_names[0]) is not None):
            for guild in source_guilds:
                name = await guild_member_display_name(guild, member_id)
                if name and name not in source_names:
                    source_names.append(name)

        id_names = [
            (registration_id_from_display_name(name), name)
            for name in source_names
            if registration_id_from_display_name(name) is not None
        ]
        unique_ids = {item[0] for item in id_names}
        if len(unique_ids) == 1:
            display_name = id_names[0][1]
        elif len(unique_ids) > 1:
            # The same account can have different numbers in two league
            # servers. Leaving the mention unresolved forces the safe
            # «Получить игроков» lookup instead of guessing an ID.
            log.warning(
                "Неоднозначный регистрационный ID для Discord %s: %s",
                member_id,
                sorted(unique_ids),
            )
            display_name = ""
        else:
            display_name = next(
                (
                    without_registration_number(name)
                    for name in (*source_names, known_name)
                    if name and without_registration_number(name)
                ),
                "",
            )
        resolved[member_id] = display_name
        if display_name and message_id:
            _mention_display_name_cache[(message_id, member_id)] = display_name
            while len(_mention_display_name_cache) > 2000:
                _mention_display_name_cache.pop(next(iter(_mention_display_name_cache)))
    return resolved


async def resolve_member_mentions(text: str, message: discord.Message) -> str:
    """Replace mentions with the best display name from every shared guild."""
    for member_id, display_name in (await mention_display_names(text, message)).items():
        if display_name:
            text = re.sub(rf"<@!?{member_id}>", f"@{display_name}", text)
    return text


async def message_context(message: discord.Message) -> str:
    chunks: list[str] = []
    for part in message_parts(message):
        content = getattr(part, "content", "")
        if content:
            chunks.append(str(content))
        for embed in getattr(part, "embeds", None) or []:
            if embed.title:
                chunks.append(embed.title)
            if embed.description:
                chunks.append(embed.description)
            for field in embed.fields:
                chunks.append(f"{field.name}\n{field.value}")
            if embed.footer and embed.footer.text:
                chunks.append(embed.footer.text)
    return await resolve_member_mentions("\n".join(chunks), message)


def find_get_players_button(message: discord.Message):
    """Find the existing `Получить игроков` component on a result card."""
    stack: list[object] = []
    for part in message_parts(message):
        stack.extend(getattr(part, "components", None) or [])

    visited: set[int] = set()
    while stack:
        component = stack.pop(0)
        identity = id(component)
        if identity in visited:
            continue
        visited.add(identity)

        label = str(getattr(component, "label", "") or "").strip().lower()
        if "получить игроков" in label and callable(getattr(component, "click", None)):
            return component

        stack.extend(getattr(component, "children", None) or [])
        stack.extend(getattr(component, "components", None) or [])
    return None


async def find_original_match_card(
    match_id: int,
    forwarded_message: discord.Message,
) -> Optional[discord.Message]:
    """Find the live original card whose forwarded snapshot lost its button."""
    def is_original_candidate(candidate: object) -> bool:
        if candidate is None or getattr(candidate, "id", None) == forwarded_message.id:
            return False
        text = plain_message_text(candidate)
        found = re.search(r"(?:матч|матча)\s*#\s*(\d+)", text, re.I)
        return bool(
            found
            and int(found.group(1)) == int(match_id)
            and find_get_players_button(candidate) is not None
        )

    # Discord forwards normally preserve a message reference. Fetch that
    # exact source before falling back to channel-history scanning.
    references: list[object] = []
    for part in message_parts(forwarded_message):
        for reference in (
            getattr(part, "reference", None),
            getattr(part, "message_reference", None),
        ):
            if reference is not None and reference not in references:
                references.append(reference)

    for reference in references:
        for candidate in (
            getattr(reference, "resolved", None),
            getattr(reference, "cached_message", None),
        ):
            if is_original_candidate(candidate):
                return candidate

        channel_id = getattr(reference, "channel_id", None)
        message_id = getattr(reference, "message_id", None)
        if not channel_id or not message_id:
            continue
        try:
            channel = client.get_channel(int(channel_id))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id))
            candidate = await channel.fetch_message(int(message_id))
            if is_original_candidate(candidate):
                log.info(
                    "Матч #%s: оригинальная карточка %s получена по ссылке forward",
                    match_id,
                    message_id,
                )
                return candidate
        except Exception:
            log.warning(
                "Матч #%s: не удалось получить оригинал forward %s/%s",
                match_id,
                channel_id,
                message_id,
                exc_info=True,
            )

    preferred_ids = {
        *NORMAL_CHANNEL_IDS,
        *PRIORITY_CHANNEL_IDS,
        *active_channel_ids,
    }
    channels: list[object] = []
    for channel_id in preferred_ids:
        channel = client.get_channel(channel_id)
        if channel is not None and channel not in channels:
            channels.append(channel)

    # Also inspect likely registration channels on shared guilds. This is
    # needed when a test card is forwarded into another server and its source
    # channel is not part of the currently selected autorun mode.
    for guild in getattr(client, "guilds", None) or []:
        for channel in getattr(guild, "text_channels", None) or []:
            name = str(getattr(channel, "name", "") or "").casefold()
            if (
                getattr(channel, "id", None) in preferred_ids
                or "основ" in name
                or "приоритет" in name
                or "result" in name
                or "результ" in name
            ) and channel not in channels:
                channels.append(channel)

    # Last-resort search: the original channel may have an arbitrary name.
    # Check a short recent window in every other visible text channel rather
    # than failing and trying to use incomplete long Discord IDs.
    for guild in getattr(client, "guilds", None) or []:
        for channel in getattr(guild, "text_channels", None) or []:
            if channel not in channels:
                channels.append(channel)

    for channel in channels:
        try:
            channel_id = getattr(channel, "id", None)
            channel_name = str(getattr(channel, "name", "") or "").casefold()
            is_likely_source = (
                channel_id in preferred_ids
                or "основ" in channel_name
                or "приоритет" in channel_name
                or "result" in channel_name
                or "результ" in channel_name
            )
            history_limit = min(BACKFILL_LIMIT, 500) if is_likely_source else 100
            async for candidate in channel.history(limit=history_limit):
                if candidate.id == forwarded_message.id:
                    continue
                if is_original_candidate(candidate):
                    log.info(
                        "Матч #%s: найдена оригинальная карточка %s в канале %s",
                        match_id,
                        candidate.id,
                        getattr(channel, "id", "?"),
                    )
                    return candidate
        except Exception:
            log.warning(
                "Матч #%s: не удалось проверить канал %s для поиска оригинала",
                match_id,
                getattr(channel, "id", "?"),
                exc_info=True,
            )
    return None


def extract_player_modal_text(root: object) -> Optional[str]:
    """Read the prefilled text area from a modal returned by discord.py-self."""
    queue: list[tuple[object, int]] = [(root, 0)]
    visited: set[int] = set()
    candidates: list[str] = []

    while queue:
        value, depth = queue.pop(0)
        if value is None or depth > 12:
            continue
        if isinstance(value, str):
            upper = value.upper()
            has_match = bool(re.search(r"=G\s+\d+", upper))
            has_ct = bool(re.search(r"(?m)^\s*#?\s*CT\s*$", upper))
            has_t = bool(re.search(r"(?m)^\s*#?\s*T\s*$", upper))
            numeric_rows = len(
                re.findall(r"(?m)^\s*\d{1,5}\s+\d+\s+\d+\s+\d+\s*$", value)
            )
            if has_match and has_ct and has_t and numeric_rows >= 8:
                candidates.append(value)
            continue
        if isinstance(value, (bytes, bytearray, int, float, bool)):
            continue

        identity = id(value)
        if identity in visited:
            continue
        visited.add(identity)

        if isinstance(value, dict):
            queue.extend((item, depth + 1) for item in value.values())
            continue
        if isinstance(value, (list, tuple, set)):
            queue.extend((item, depth + 1) for item in value)
            continue

        # Some discord.py-self interaction wrappers keep the response only in
        # private instance fields. Inspect their bounded __dict__ values too.
        with contextlib.suppress(Exception):
            object_values = list(vars(value).values())[:80]
            queue.extend((item, depth + 1) for item in object_values)

        # discord.py-self versions expose modal fields through slightly
        # different wrappers. Only inspect the known, bounded attributes.
        for attribute in (
            "value", "default", "text", "content", "data", "modal", "payload",
            "raw_data", "values", "embeds", "embed", "fields", "description",
            "interaction", "message", "response", "response_message",
            "original_response", "followup", "successful", "result", "messages",
            "components", "children", "items",
        ):
            with contextlib.suppress(Exception):
                child = getattr(value, attribute)
                if child is not value:
                    queue.append((child, depth + 1))

    return max(candidates, key=len) if candidates else None


def extract_interaction_image_urls(root: object) -> list[str]:
    """Collect image attachments from a private interaction response."""
    queue: list[tuple[object, int]] = [(root, 0)]
    visited: set[int] = set()
    found: list[str] = []
    while queue:
        value, depth = queue.pop(0)
        if value is None or depth > 7:
            continue
        if isinstance(value, str):
            lowered = value.lower().split("?", 1)[0]
            if value.startswith("http") and lowered.endswith((".png", ".jpg", ".jpeg", ".webp")):
                found.append(value)
            continue
        if isinstance(value, (bytes, bytearray, int, float, bool)):
            continue
        identity = id(value)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(value, dict):
            queue.extend((item, depth + 1) for item in value.values())
            continue
        if isinstance(value, (list, tuple, set)):
            queue.extend((item, depth + 1) for item in value)
            continue
        for attribute in (
            "url", "proxy_url", "attachments", "embeds", "image", "thumbnail",
            "data", "message", "response", "response_message", "successful",
            "result", "messages", "components", "children", "items",
        ):
            with contextlib.suppress(Exception):
                child = getattr(value, attribute)
                if child is not value:
                    queue.append((child, depth + 1))
    return list(dict.fromkeys(found))


# Why «Получить игроков» gave no player list, per match, for Discord logs.
players_helper_errors: dict[int, str] = {}


def players_helper_error_text(match_id: Optional[int]) -> str:
    reason = players_helper_errors.get(match_id or 0)
    return f"«ПОЛУЧИТЬ ИГРОКОВ»: {reason}\n\n" if reason else ""


async def get_players_response(message: discord.Message) -> tuple[Optional[str], list[str]]:
    """Click `Получить игроков` and capture its private helper message."""
    button = find_get_players_button(message)
    if button is None:
        return None, []

    async with player_modal_lock:
        custom_id = str(getattr(button, "custom_id", "") or "")
        source_match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", await message_context(message), re.I)
        expected_match_id = int(source_match.group(1)) if source_match else None
        players_helper_errors.pop(expected_match_id or 0, None)

        def modal_matches_expected(text: Optional[str]) -> bool:
            if not text:
                return False
            if expected_match_id is None:
                return True
            return bool(re.search(rf"=g\s+{expected_match_id}\b", text, re.I))

        def interaction_check(interaction: object) -> bool:
            interaction_custom_id = str(
                getattr(interaction, "custom_id", "")
                or (getattr(interaction, "data", {}) or {}).get("custom_id", "")
            )
            if custom_id and interaction_custom_id and interaction_custom_id != custom_id:
                return False
            interaction_text = extract_player_modal_text(interaction)
            return not interaction_text or modal_matches_expected(interaction_text)

        # discord.py-self dispatches `interaction_finish` after the private
        # component response has been finalized and Interaction.successful
        # has been populated. `interaction` is retained as a compatibility
        # fallback for builds that only expose the first event.
        def helper_message_check(candidate: object) -> bool:
            text = extract_player_modal_text(candidate)
            # Ephemeral responses may expose no channel or a synthetic
            # channel. The exact match number is a safer binding.
            return modal_matches_expected(text)

        waiters = [
            asyncio.create_task(client.wait_for("message", check=helper_message_check)),
            asyncio.create_task(client.wait_for("interaction_finish", check=interaction_check)),
            asyncio.create_task(client.wait_for("interaction", check=interaction_check)),
        ]
        try:
            try:
                click_result = await button.click()
            except Exception as exc:
                # Snapshot components without message state, Discord HTTP
                # errors and rejected interactions are recoverable helper
                # failures, not a reason to crash the whole card.
                reason = f"кнопка не нажалась — {type(exc).__name__}: {exc}"
                players_helper_errors[expected_match_id or 0] = reason[:500]
                log.warning(
                    "Матч #%s: «Получить игроков»: %s",
                    expected_match_id or "?",
                    reason,
                )
                return None, []
            observed_roots: list[object] = [button, message]
            if click_result is not None:
                observed_roots.append(click_result)
            deadline = asyncio.get_running_loop().time() + PLAYER_MODAL_TIMEOUT
            pending = set(waiters)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    players_helper_errors[expected_match_id or 0] = (
                        "турнирный бот не прислал список игроков за "
                        f"{PLAYER_MODAL_TIMEOUT:g} с"
                    )
                    log.error(
                        "Матч #%s: тайм-аут захвата ephemeral-ответа «Получить игроков»; observed=%s",
                        expected_match_id or "?",
                        [type(item).__name__ for item in observed_roots],
                    )
                    return None, []

                # Poll already returned interaction objects because their
                # response fields can be populated after button.click exits.
                cached = list(getattr(client, "cached_messages", None) or [])[-25:]
                for root in [*observed_roots, *cached]:
                    response_text = extract_player_modal_text(root)
                    if modal_matches_expected(response_text):
                        log.info(
                            "Матч #%s: ephemeral-ответ «Получить игроков» захвачен из %s",
                            expected_match_id or "?",
                            type(root).__name__,
                        )
                        return response_text, extract_interaction_image_urls(root)

                if pending:
                    done, still_pending = await asyncio.wait(
                        pending,
                        timeout=min(0.35, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    pending = set(still_pending)
                    for completed in done:
                        try:
                            event_result = completed.result()
                        except Exception:
                            log.exception(
                                "Матч #%s: ошибка получения interaction event",
                                expected_match_id or "?",
                            )
                            continue
                        if event_result is not None:
                            observed_roots.append(event_result)
                else:
                    await asyncio.sleep(min(0.2, remaining))
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            for waiter in waiters:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiter


async def get_players_modal_text(message: discord.Message) -> Optional[str]:
    """Backward-compatible wrapper used by older tests/integrations."""
    text, _ = await get_players_response(message)
    return text


def parse_players_modal(modal_text: str) -> Optional[dict[str, list[dict]]]:
    """Parse authoritative starting-side groups from the player modal."""
    sides: dict[str, list[dict]] = {"CT": [], "T": []}
    current_side: Optional[str] = None
    named_pattern = re.compile(r"^\s*(\d{1,5})\s+(.+?)\s*=\s*(\d+)\s+(\d+)\s+(\d+)\s*$")
    numeric_pattern = re.compile(r"^\s*(\d{1,5})\s+(\d+)\s+(\d+)\s+(\d+)\s*$")

    for raw_line in modal_text.splitlines():
        line = raw_line.strip()
        header = re.fullmatch(r"#?\s*(CT|T)\s*", line, re.I)
        if header:
            current_side = header.group(1).upper()
            continue
        if current_side is None:
            continue
        named = named_pattern.fullmatch(line)
        numeric = numeric_pattern.fullmatch(line)
        if not named and not numeric:
            continue
        if named:
            player_id = int(named.group(1))
            nickname = strip_leading_clan_tags(named.group(2).strip())
            kills, assists, deaths = map(int, named.group(3, 4, 5))
        else:
            player_id, nickname = int(numeric.group(1)), ""
            kills, assists, deaths = map(int, numeric.group(2, 3, 4))
        sides[current_side].append({
            "id": player_id,
            "nickname": nickname,
            "kills": kills,
            "assists": assists,
            "deaths": deaths,
        })

    players = [*sides["CT"], *sides["T"]]
    player_ids = [player["id"] for player in players]
    if (
        len(sides["CT"]) != 5
        or len(sides["T"]) != 5
        or any(player_id <= 0 for player_id in player_ids)
        or len(set(player_ids)) != 10
    ):
        return None
    return sides


def result_from_players_modal(
    message_text: str,
    modal_text: str,
    score_override: Optional[tuple[int, int]] = None,
    visual_verified: bool = False,
) -> Optional[dict]:
    """Build a registration only after checking the modal against the card."""
    parsed = parse_players_modal(modal_text)
    if parsed is None:
        return None

    match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", message_text, re.I)
    score = re.search(r"(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)", message_text)
    if not match or (score is None and score_override is None):
        return None

    if score_override is not None:
        score_a, score_b = score_override
    else:
        score_a, score_b = int(score.group(1)), int(score.group(2))
    if not (0 <= score_a <= 99 and 0 <= score_b <= 99):
        return None

    # The visible result card must contain the same ten K/A/D rows. We compare
    # a multiset because the modal uses starting CT/T while the card may show
    # the teams after a side swap.
    card_slots = parse_card_roster_slots(message_text)
    if card_slots is None and not visual_verified:
        return None
    card_kad = Counter(
        (player["kills"], player["assists"], player["deaths"])
        for team in (card_slots or {}).values() for player in team
    )
    modal_kad = Counter(
        (player["kills"], player["assists"], player["deaths"])
        for player in [*parsed["CT"], *parsed["T"]]
    )
    # A missing player is displayed as 0/0/0 in the modal but registered as
    # 0/0/13. Normalize the card the same way for comparison.
    if card_slots is not None:
        normalized_card_kad = Counter()
        for kad, count in card_kad.items():
            normalized_card_kad[(0, 0, 13) if kad == (0, 0, 0) else kad] += count
        if normalized_card_kad != modal_kad:
            return None

    return {
        "is_match_result": True,
        "match_id": int(match.group(1)),
        "score_a": score_a,
        "score_b": score_b,
        # Modal groups are authoritative starting sides. Store them directly
        # as A=CT and B=T so format_registration cannot invert them.
        "ct_team": "A",
        "team_a": parsed["CT"],
        "team_b": parsed["T"],
        "overall_confidence": 1.0,
        "notes": "ID и стартовые стороны взяты из окна «Получить игроков»; результат сверен с карточкой.",
    }


def result_from_nonzero_modal_and_final_score(
    message_text: str,
    modal_text: str,
    audit: dict,
) -> Optional[dict]:
    """Safe fallback when names are weak but helper K/A/D is complete.

    The helper groups players by their starting CT/T side. A completed match
    has already crossed halftime, so the final scoreboard displays those
    groups on the opposite side. We use the final screenshot only for the
    score and the helper for all ten IDs and non-zero K/A/D rows.
    """
    parsed = parse_players_modal(modal_text)
    match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", message_text, re.I)
    if (
        parsed is None
        or match is None
        or not audit.get("is_scoreboard")
        or not audit.get("is_final_result")
        or audit.get("has_live_gameplay_hud")
    ):
        return None
    try:
        score_left = int(audit["score_left"])
        score_right = int(audit["score_right"])
    except (KeyError, TypeError, ValueError):
        return None
    side_left = str(audit.get("side_left") or "").upper()
    side_right = str(audit.get("side_right") or "").upper()
    if {side_left, side_right} != {"CT", "T"}:
        return None

    players = [*parsed["CT"], *parsed["T"]]
    # A 0/0/0 helper row is not authoritative and must still be recovered by
    # nickname from the screenshot. Never use this fallback when any exists.
    if any(
        (
            int(player.get("kills", 0) or 0),
            int(player.get("assists", 0) or 0),
            int(player.get("deaths", 0) or 0),
        ) == (0, 0, 0)
        for player in players
    ):
        return None

    # Starting CT is displayed on final T after the mandatory halftime swap.
    if side_left == "T":
        score_ct, score_t = score_left, score_right
    else:
        score_ct, score_t = score_right, score_left
    return {
        "is_match_result": True,
        "match_id": int(match.group(1)),
        "score_a": score_ct,
        "score_b": score_t,
        "ct_team": "A",
        "team_a": parsed["CT"],
        "team_b": parsed["T"],
        "overall_confidence": min(
            1.0,
            float(audit.get("overall_confidence", 0) or 0),
        ),
        "notes": (
            "Резерв: все ID/K/A/D и стартовые CT/T взяты из «Получить "
            "игроков»; финальный счёт взят со скриншота с учётом смены сторон."
        ),
    }


def result_from_visual_audit(
    message_text: str,
    modal_text: str,
    audit: dict,
) -> Optional[dict]:
    """Accept a review card only when its modal exactly matches the screenshot."""
    parsed = parse_players_modal(modal_text)
    match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", message_text, re.I)
    if (
        parsed is None
        or match is None
        or not audit.get("is_scoreboard")
        or not audit.get("is_final_result")
        or audit.get("has_live_gameplay_hud")
    ):
        return None

    try:
        confidence = float(audit.get("overall_confidence", 0) or 0)
        score_left = int(audit["score_left"])
        score_right = int(audit["score_right"])
        left_players = audit["left_players"]
        right_players = audit["right_players"]
    except (KeyError, TypeError, ValueError):
        return None

    if (
        confidence < 0.90
        or not (0 <= score_left <= 99 and 0 <= score_right <= 99)
        or len(left_players) != 5
        or len(right_players) != 5
    ):
        return None

    def nickname_key(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value)).casefold()
        return "".join(character for character in text if character.isalnum())

    def nickname_score(first: object, second: object) -> float:
        left = nickname_key(first)
        right = nickname_key(second)
        if not left or not right or left.isdigit() or right.isdigit():
            return 0.0
        if left == right:
            return 1.0
        if min(len(left), len(right)) >= 3 and (left in right or right in left):
            return 0.92 + 0.08 * min(len(left), len(right)) / max(len(left), len(right))
        return SequenceMatcher(None, left, right).ratio()

    def kad(player: dict) -> Optional[tuple[int, int, int]]:
        try:
            values = (
                int(player["kills"]),
                int(player["assists"]),
                int(player["deaths"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if any(value < 0 or value > 100 for value in values):
            return None
        return (0, 0, 13) if values == (0, 0, 0) else values

    if any(kad(player) is None for player in [*left_players, *right_players]):
        return None

    def best_alignment(
        modal_players: list[dict], visual_players: list[dict]
    ) -> tuple[tuple[int, ...], float, float]:
        best_order: tuple[int, ...] = tuple(range(5))
        best_average = -1.0
        best_minimum = -1.0
        for order in permutations(range(5)):
            scores: list[float] = []
            for modal_player, visual_index in zip(modal_players, order):
                visual_player = visual_players[visual_index]
                name_match = nickname_similarity(
                    modal_player.get("nickname", ""),
                    visual_player.get("nickname", ""),
                )
                # Exact K/A/D is useful for mentions or decorated nicknames,
                # but a name match remains authoritative when modal stats are
                # one round stale.
                stat_match = 0.88 if kad(modal_player) == kad(visual_player) else 0.0
                scores.append(max(name_match, stat_match))
            average = sum(scores) / 5
            minimum = min(scores)
            if (average, minimum) > (best_average, best_minimum):
                best_order = tuple(order)
                best_average = average
                best_minimum = minimum
        return best_order, best_average, best_minimum

    # Current review cards contain only `#ID nickname` roster lines, while
    # «Получить игроков» contains the same IDs split into the authoritative
    # starting CT/T groups and zeroed statistics.  Enrich those IDs with card
    # nicknames, then take score and K/A/D exclusively from the screenshot.
    card_rosters = parse_card_roster_identities(message_text)
    if card_rosters is not None:
        card_a = card_rosters["team_a"]
        card_b = card_rosters["team_b"]
        ids_a = {int(player["id"]) for player in card_a}
        ids_b = {int(player["id"]) for player in card_b}
        ids_ct = {int(player["id"]) for player in parsed["CT"]}
        ids_t = {int(player["id"]) for player in parsed["T"]}

        if ids_a == ids_ct and ids_b == ids_t:
            ct_team = "A"
        elif ids_a == ids_t and ids_b == ids_ct:
            ct_team = "B"
        else:
            direct_ids = len(ids_a & ids_ct) + len(ids_b & ids_t)
            swapped_ids = len(ids_a & ids_t) + len(ids_b & ids_ct)
            if max(direct_ids, swapped_ids) < 8 or abs(direct_ids - swapped_ids) < 2:
                log.error(
                    "Матч #%s: ID карточки неоднозначно сопоставлены с CT/T "
                    "(direct=%s swapped=%s).",
                    match.group(1), direct_ids, swapped_ids,
                )
                return None
            ct_team = "A" if direct_ids > swapped_ids else "B"

        direct_a = best_alignment(card_a, left_players)
        direct_b = best_alignment(card_b, right_players)
        swapped_a = best_alignment(card_a, right_players)
        swapped_b = best_alignment(card_b, left_players)
        direct_names = direct_a[1] + direct_b[1]
        swapped_names = swapped_a[1] + swapped_b[1]

        if direct_names >= swapped_names:
            chosen_names, other_names = direct_names, swapped_names
            alignment_a, alignment_b = direct_a, direct_b
            visual_a, visual_b = left_players, right_players
            score_a, score_b = score_left, score_right
            visual_side_a = str(audit.get("side_left") or "").upper()
            visual_side_b = str(audit.get("side_right") or "").upper()
        else:
            chosen_names, other_names = swapped_names, direct_names
            alignment_a, alignment_b = swapped_a, swapped_b
            visual_a, visual_b = right_players, left_players
            score_a, score_b = score_right, score_left
            visual_side_a = str(audit.get("side_right") or "").upper()
            visual_side_b = str(audit.get("side_left") or "").upper()

        # «Получить игроков» contains the starting CT/T groups, while the final
        # scoreboard normally shows the sides after the halftime switch. The
        # visible CT/T labels must therefore never invalidate an otherwise
        # reliable roster/ID/statistics mapping.

        if (
            chosen_names / 2 < 0.78
            or min(alignment_a[2], alignment_b[2]) < 0.55
            or chosen_names - other_names < 0.08
        ):
            log.error(
                "Матч #%s: ники карточки неоднозначно сопоставлены со "
                "скриншотом (direct=%.3f swapped=%.3f).",
                match.group(1), direct_names, swapped_names,
            )
            return None

        def merge_card_team(
            card_players: list[dict],
            visual_players: list[dict],
            order: tuple[int, ...],
        ) -> list[dict]:
            merged: list[dict] = []
            for card_player, visual_index in zip(card_players, order):
                visual_player = visual_players[visual_index]
                kills = int(visual_player["kills"])
                assists = int(visual_player["assists"])
                deaths = int(visual_player["deaths"])
                if kills == 0 and assists == 0 and deaths == 0:
                    deaths = 13
                merged.append(
                    {
                        "id": int(card_player["id"]),
                        "nickname": str(
                            visual_player.get("nickname")
                            or card_player.get("nickname", "")
                        ),
                        "card_nickname": str(card_player.get("nickname", "")),
                        "kills": kills,
                        "assists": assists,
                        "deaths": deaths,
                    }
                )
            return merged

        return {
            "is_match_result": True,
            "match_id": int(match.group(1)),
            "score_a": score_a,
            "score_b": score_b,
            "ct_team": ct_team,
            "team_a": merge_card_team(card_a, visual_a, alignment_a[0]),
            "team_b": merge_card_team(card_b, visual_b, alignment_b[0]),
            "overall_confidence": confidence,
            "notes": (
                "ID и стороны взяты из «Получить игроков»; ники — из "
                "карточки; счёт и K/A/D — только из исходного скриншота."
            ),
        }

    direct_ct = best_alignment(parsed["CT"], left_players)
    direct_t = best_alignment(parsed["T"], right_players)
    swapped_ct = best_alignment(parsed["CT"], right_players)
    swapped_t = best_alignment(parsed["T"], left_players)
    direct_score = direct_ct[1] + direct_t[1]
    swapped_score = swapped_ct[1] + swapped_t[1]

    if direct_score >= swapped_score:
        chosen_score, other_score = direct_score, swapped_score
        ct_alignment, t_alignment = direct_ct, direct_t
        visual_ct, visual_t = left_players, right_players
        score_a, score_b = score_left, score_right
    else:
        chosen_score, other_score = swapped_score, direct_score
        ct_alignment, t_alignment = swapped_ct, swapped_t
        visual_ct, visual_t = right_players, left_players
        score_a, score_b = score_right, score_left

    if (
        chosen_score / 2 < 0.78
        or min(ct_alignment[2], t_alignment[2]) < 0.55
        or chosen_score - other_score < 0.08
    ):
        log.error(
            "Матч #%s: не удалось однозначно сопоставить игроков скриншота с окном игроков (direct=%.3f swapped=%.3f).",
            match.group(1),
            direct_score,
            swapped_score,
        )
        return None

    def merge_audited_stats(
        modal_players: list[dict],
        visual_players: list[dict],
        order: tuple[int, ...],
    ) -> list[dict]:
        merged: list[dict] = []
        for modal_player, visual_index in zip(modal_players, order):
            visual_player = visual_players[visual_index]
            merged.append(
                {
                    "id": int(modal_player["id"]),
                    "nickname": str(visual_player.get("nickname") or modal_player["nickname"]),
                    "card_nickname": str(modal_player["nickname"]),
                    "kills": int(visual_player["kills"]),
                    "assists": int(visual_player["assists"]),
                    "deaths": int(visual_player["deaths"]),
                }
            )
        return merged

    audited_ct = merge_audited_stats(parsed["CT"], visual_ct, ct_alignment[0])
    audited_t = merge_audited_stats(parsed["T"], visual_t, t_alignment[0])

    return {
        "is_match_result": True,
        "match_id": int(match.group(1)),
        "score_a": score_a,
        "score_b": score_b,
        "ct_team": "A",
        "team_a": audited_ct,
        "team_b": audited_t,
        "overall_confidence": confidence,
        "notes": "ID взяты из окна игроков; счёт и K/A/D взяты только из исходного скриншота и сопоставлены по никам.",
    }


def image_urls(message: discord.Message) -> list[str]:
    valid_ext = (".png", ".jpg", ".jpeg", ".webp")
    urls: list[str] = []
    for part in message_parts(message):
        for attachment in getattr(part, "attachments", None) or []:
            filename = str(getattr(attachment, "filename", "")).lower()
            content_type = str(getattr(attachment, "content_type", "") or "")
            if content_type.startswith("image/") or filename.endswith(valid_ext):
                url = getattr(attachment, "url", None)
                if url:
                    urls.append(str(url))
        for embed in getattr(part, "embeds", None) or []:
            if embed.image and embed.image.url:
                urls.append(str(embed.image.url))
            if embed.thumbnail and embed.thumbnail.url:
                urls.append(str(embed.thumbnail.url))
    return list(dict.fromkeys(urls))


async def download_image(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.read()


def image_proxy_urls(message: discord.Message) -> dict[str, str]:
    """media.discordapp.net mirrors of card images, used when a link fails."""
    proxies: dict[str, str] = {}
    for part in message_parts(message):
        media_items = list(getattr(part, "attachments", None) or [])
        for embed in getattr(part, "embeds", None) or []:
            media_items.extend(
                (getattr(embed, "image", None), getattr(embed, "thumbnail", None))
            )
        for media in media_items:
            url = getattr(media, "url", None)
            proxy_url = getattr(media, "proxy_url", None)
            if url and proxy_url and str(proxy_url) != str(url):
                proxies[str(url)] = str(proxy_url)
    return proxies


async def download_card_images(session, message: discord.Message, urls: list[str]) -> list[bytes]:
    """Download card images, skipping links that fail after the proxy retry."""
    proxies = image_proxy_urls(message)
    errors: list[str] = []

    async def fetch(url: str) -> Optional[bytes]:
        for candidate in (url, proxies.get(url)):
            if not candidate:
                continue
            try:
                return await download_image(session, candidate)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
        return None

    downloaded = await asyncio.gather(*(fetch(url) for url in urls))
    images = [image for image in downloaded if image is not None]
    if not images:
        raise RuntimeError(
            "Не удалось скачать скриншот карточки: " + " | ".join(errors)[:800]
        )
    if errors:
        log.warning(
            "Часть изображений карточки не скачалась, продолжаю без них: %s",
            " | ".join(errors)[:800],
        )
    return images


def prepare_image(raw: bytes) -> tuple[str, str]:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    longest = max(image.width, image.height)
    if longest < 2400:
        scale = min(3.0, 2400 / longest)
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95, optimize=True)
    return base64.b64encode(out.getvalue()).decode("ascii"), "image/jpeg"


def extract_short_player_ids(message_text: str, match_id: Optional[int]) -> list[int]:
    """Extract ten short registration IDs in roster order from Discord text."""
    missing_stats = re.search(
        r"Нет\s+статистики\s+для\s+игроков\s*:\s*([^\n]+)",
        message_text,
        re.I,
    )
    search_areas = [missing_stats.group(1)] if missing_stats else []
    search_areas.append(message_text)

    for area in search_areas:
        ids: list[int] = []
        values = re.findall(r"(?<!\d)#\s*(\d{1,5})(?!\d)", area)
        if not values:
            values = re.findall(r"(?:^|\n|[@•]\s*)(\d{2,5})\s*\|", area, re.M)
        for value in values:
            player_id = int(value)
            if match_id is not None and player_id == int(match_id):
                continue
            if player_id not in ids:
                ids.append(player_id)
        if len(ids) == 10:
            return ids
    return []


def explicit_ct_team_from_card(message_text: str) -> Optional[str]:
    """Trust explicit Team A/B CT/T labels in review cards."""
    header_a = re.search(r"Команда\s*A[^\n]*", message_text, re.I)
    header_b = re.search(r"Команда\s*B[^\n]*", message_text, re.I)
    if not header_a or not header_b:
        return None
    side_a = re.search(r"\b(CT|T)\b", header_a.group(0), re.I)
    side_b = re.search(r"\b(CT|T)\b", header_b.group(0), re.I)
    if side_a and side_a.group(1).upper() == "CT":
        return "A"
    if side_b and side_b.group(1).upper() == "CT":
        return "B"
    return None


def parse_card_roster_slots(message_text: str) -> Optional[dict[str, list[dict]]]:
    """Read ten roster slots and K/A/D even when a slot is a long mention."""
    header_a = re.search(r"Команда\s*A[^\n]*", message_text, re.I)
    header_b = re.search(r"Команда\s*B[^\n]*", message_text, re.I)
    if not header_a or not header_b or header_b.start() <= header_a.start():
        return None

    line_pattern = re.compile(
        r"^\s*[•·-]?\s*(.*?)\s*[—–]\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)",
        re.M,
    )

    def parse_section(section: str) -> list[dict]:
        slots: list[dict] = []
        for found in line_pattern.finditer(section):
            label = found.group(1).strip()
            short_id_match = re.search(r"(?<!\d)#\s*(\d{1,5})(?!\d)", label)
            short_id = int(short_id_match.group(1)) if short_id_match else None
            nickname = re.sub(r"<@!?\d{15,22}>", "", label)
            nickname = re.sub(r"(?<!\d)#\s*\d{1,5}(?!\d)", "", nickname)
            nickname = nickname.strip(" @|`*_.,")
            nickname = re.sub(r"^[^\w\[({]+", "", nickname, flags=re.UNICODE)
            nickname = strip_leading_clan_tags(nickname)
            slots.append({
                "id": short_id,
                "nickname": nickname,
                "kills": int(found.group(2)),
                "assists": int(found.group(3)),
                "deaths": int(found.group(4)),
            })
            if len(slots) == 5:
                break
        return slots

    team_a = parse_section(message_text[header_a.end():header_b.start()])
    team_b = parse_section(message_text[header_b.end():])
    if len(team_a) != 5 or len(team_b) != 5:
        return None
    return {"team_a": team_a, "team_b": team_b}


def parse_card_roster_identities(
    message_text: str,
) -> Optional[dict[str, list[dict]]]:
    """Read review-card rosters that contain only `#ID nickname` lines.

    New review cards no longer print K/A/D in their text.  The IDs and names
    still identify the two card teams; the actual score and statistics must
    be read from the original scoreboard image.
    """
    header_a = re.search(r"Команда\s*A[^\n]*", message_text, re.I)
    header_b = re.search(r"Команда\s*B[^\n]*", message_text, re.I)
    if not header_a or not header_b or header_b.start() <= header_a.start():
        return None

    def parse_section(section: str) -> list[dict]:
        players: list[dict] = []
        for raw_line in section.splitlines():
            line = raw_line.strip().strip("`*_")
            found = re.match(
                # Status/emoji prefixes are decoration, not nickname text:
                # `❓ @#124 | OLD | Shkiper`.
                r"^[^0-9\n]{0,32}#\s*(\d{1,5})\s*(?:\|\s*)?(.+?)\s*$",
                line,
            )
            if not found:
                continue
            nickname = re.sub(
                r"\s*[—–-]\s*\d+\s*/\s*\d+\s*/\s*\d+\s*$",
                "",
                found.group(2),
            ).strip(" @|`*_.,")
            if not nickname:
                continue
            players.append(
                {
                    "id": int(found.group(1)),
                    "nickname": strip_leading_clan_tags(nickname),
                    "kills": 0,
                    "assists": 0,
                    "deaths": 0,
                }
            )
            if len(players) == 5:
                break
        return players

    team_a = parse_section(message_text[header_a.end():header_b.start()])
    team_b = parse_section(message_text[header_b.end():])
    if len(team_a) != 5 or len(team_b) != 5:
        return None
    all_ids = [player["id"] for player in [*team_a, *team_b]]
    if len(set(all_ids)) != 10:
        return None
    return {"team_a": team_a, "team_b": team_b}


def result_from_card_and_visual_audit(
    message_text: str,
    audit: dict,
) -> Optional[dict]:
    """Build a result from card IDs/nicks and the original scoreboard only."""
    rosters = parse_card_roster_identities(message_text)
    match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", message_text, re.I)
    if rosters is None or match is None:
        return None

    try:
        confidence = float(audit.get("overall_confidence", 0) or 0)
        score_left = int(audit["score_left"])
        score_right = int(audit["score_right"])
        left_players = list(audit["left_players"])
        right_players = list(audit["right_players"])
    except (KeyError, TypeError, ValueError):
        return None

    if not audit.get("is_scoreboard") or not audit.get("is_final_result"):
        return None
    if (
        confidence < 0.90
        or not (0 <= score_left <= 99 and 0 <= score_right <= 99)
        or not (1 <= len(left_players) <= 5)
        or not (1 <= len(right_players) <= 5)
    ):
        return None

    def valid_visual_player(player: dict) -> bool:
        try:
            values = [int(player[key]) for key in ("kills", "assists", "deaths")]
        except (KeyError, TypeError, ValueError):
            return False
        return all(0 <= value <= 100 for value in values)

    if not all(valid_visual_player(player) for player in [*left_players, *right_players]):
        return None

    def best_alignment(
        card_players: list[dict], visual_players: list[dict]
    ) -> tuple[tuple[Optional[int], ...], int, float, float]:
        best_order: tuple[Optional[int], ...] = tuple([None] * 5)
        best_count = 0
        best_average = -1.0
        best_minimum = -1.0
        # Match only reliable nickname pairs. A card player with no reliable
        # row (missing from the screenshot or shown under a wrong nickname)
        # remains unmatched and is registered as 0/0/13. Unmatched visual
        # rows are ignored instead of donating their stats to another ID.
        from itertools import combinations

        max_pairs = min(5, len(visual_players))
        for pair_count in range(max_pairs, 0, -1):
            for card_indices in combinations(range(5), pair_count):
                for visual_indices in permutations(range(len(visual_players)), pair_count):
                    scores = [
                        nickname_similarity(
                            card_players[card_index].get("nickname", ""),
                            visual_players[visual_index].get("nickname", ""),
                        )
                        for card_index, visual_index in zip(card_indices, visual_indices)
                    ]
                    if any(score < 0.72 for score in scores):
                        continue
                    average = sum(scores) / pair_count
                    minimum = min(scores)
                    metric = (pair_count, average, minimum)
                    if metric > (best_count, best_average, best_minimum):
                        mapping: list[Optional[int]] = [None] * 5
                        for card_index, visual_index in zip(card_indices, visual_indices):
                            mapping[card_index] = visual_index
                        best_order = tuple(mapping)
                        best_count = pair_count
                        best_average = average
                        best_minimum = minimum
            if best_count == pair_count:
                break
        return best_order, best_count, best_average, best_minimum

    card_a = rosters["team_a"]
    card_b = rosters["team_b"]
    direct_a = best_alignment(card_a, left_players)
    direct_b = best_alignment(card_b, right_players)
    swapped_a = best_alignment(card_a, right_players)
    swapped_b = best_alignment(card_b, left_players)
    direct_count = direct_a[1] + direct_b[1]
    swapped_count = swapped_a[1] + swapped_b[1]
    direct_names = direct_a[2] + direct_b[2]
    swapped_names = swapped_a[2] + swapped_b[2]

    if (direct_count, direct_names) >= (swapped_count, swapped_names):
        chosen_count, other_count = direct_count, swapped_count
        chosen_names, other_names = direct_names, swapped_names
        alignment_a, alignment_b = direct_a, direct_b
        visual_a, visual_b = left_players, right_players
        score_a, score_b = score_left, score_right
        side_a = str(audit.get("side_left") or "").upper()
    else:
        chosen_count, other_count = swapped_count, direct_count
        chosen_names, other_names = swapped_names, direct_names
        alignment_a, alignment_b = swapped_a, swapped_b
        visual_a, visual_b = right_players, left_players
        score_a, score_b = score_right, score_left
        side_a = str(audit.get("side_right") or "").upper()

    if (
        chosen_count < 3
        or min(alignment_a[1], alignment_b[1]) < 1
        or min(alignment_a[3], alignment_b[3]) < 0.72
        or (
            chosen_count == other_count
            and chosen_names - other_names < 0.08
        )
        or side_a not in {"CT", "T"}
    ):
        log.error(
            "Матч #%s: карточка неоднозначно сопоставлена с исходным табло "
            "(direct=%s/%.3f swapped=%s/%.3f side_a=%s).",
            match.group(1), direct_count, direct_names,
            swapped_count, swapped_names, side_a or "?",
        )
        return None

    def merge_team(
        card_players: list[dict],
        visual_players: list[dict],
        order: tuple[Optional[int], ...],
    ) -> list[dict]:
        resolved_order = list(order)
        unmatched_card_indices = [
            index for index, visual_index in enumerate(order)
            if visual_index is None
        ]
        used_visual_indices = {
            visual_index for visual_index in order if visual_index is not None
        }
        unused_visual_indices = [
            index for index in range(len(visual_players))
            if index not in used_visual_indices
        ]
        forced_wrong_nickname_indices: set[int] = set()

        # If all five scoreboard rows are visible, nobody may receive a fake
        # 0/0/13. Reliably matched rows establish the team; assign all remaining
        # rows one-to-one by the best global nickname score. These players keep
        # their real screenshot K/A/D and receive only a nickname-mismatch
        # reason when their names genuinely differ.
        if (
            len(visual_players) == 5
            and unmatched_card_indices
            and len(unmatched_card_indices) == len(unused_visual_indices)
        ):
            best_visual_order = max(
                permutations(unused_visual_indices),
                key=lambda candidate_order: sum(
                    nickname_similarity(
                        card_players[card_index].get("nickname", ""),
                        visual_players[visual_index].get("nickname", ""),
                    )
                    for card_index, visual_index in zip(
                        unmatched_card_indices,
                        candidate_order,
                    )
                ),
            )
            for card_index, visual_index in zip(
                unmatched_card_indices,
                best_visual_order,
            ):
                resolved_order[card_index] = visual_index
                forced_wrong_nickname_indices.add(card_index)
            unmatched_card_indices = []
            unused_visual_indices = []

        # Every unused visible row means that one unmatched roster player was
        # shown under a different nickname. Any remaining unmatched roster
        # slots were not visible on the screenshot at all. Pair only for the
        # warning reason; the recognition/matching result itself is unchanged.
        wrong_nickname_card_indices: set[int] = set()
        available_cards = set(unmatched_card_indices)
        for visual_index in unused_visual_indices:
            if not available_cards:
                break
            card_index = max(
                available_cards,
                key=lambda index: nickname_similarity(
                    card_players[index].get("nickname", ""),
                    visual_players[visual_index].get("nickname", ""),
                ),
            )
            wrong_nickname_card_indices.add(card_index)
            available_cards.remove(card_index)

        merged: list[dict] = []
        for card_index, (card_player, visual_index) in enumerate(
            zip(card_players, resolved_order)
        ):
            if visual_index is None:
                merged.append(
                    {
                        "id": int(card_player["id"]),
                        "nickname": str(card_player.get("nickname", "")),
                        "card_nickname": str(card_player.get("nickname", "")),
                        "kills": 0,
                        "assists": 0,
                        "deaths": 13,
                        "warning_reason": (
                            "неправильный ник"
                            if card_index in wrong_nickname_card_indices
                            else "нет на скриншоте"
                        ),
                    }
                )
                continue
            visual_player = visual_players[visual_index]
            kills = int(visual_player["kills"])
            assists = int(visual_player["assists"])
            deaths = int(visual_player["deaths"])
            warning_reason: Optional[str] = None
            if kills == 0 and assists == 0 and deaths == 0:
                deaths = 13
                warning_reason = "додж статистики"
            elif card_index in forced_wrong_nickname_indices:
                warning_reason = "неправильный ник"
            merged_player = {
                # The card itself is authoritative for registration IDs.
                "id": int(card_player["id"]),
                "nickname": str(
                    visual_player.get("nickname")
                    or card_player.get("nickname", "")
                ),
                # A nickname-mismatch warning must tag the registered Discord
                # player, whose name differs from the screenshot by definition.
                "card_nickname": str(card_player.get("nickname", "")),
                "kills": kills,
                "assists": assists,
                "deaths": deaths,
            }
            if warning_reason is not None:
                merged_player["warning_reason"] = warning_reason
            merged.append(merged_player)
        return merged

    notes = (
        "ID и ники взяты из карточки; счёт, стороны и K/A/D — только "
        "из исходного скриншота. Отсутствующий на табло игрок получает "
        "0/0/13. «Получить игроков» не использовалось."
    )
    return {
        "is_match_result": True,
        "match_id": int(match.group(1)),
        "score_a": score_a,
        "score_b": score_b,
        "ct_team": "A" if side_a == "CT" else "B",
        "team_a": merge_team(card_a, visual_a, alignment_a[0]),
        "team_b": merge_team(card_b, visual_b, alignment_b[0]),
        "overall_confidence": confidence,
        "notes": notes,
    }


def result_from_review_card_and_modal(
    message_text: str,
    modal_text: str,
    visual_audit: Optional[dict] = None,
) -> Optional[dict]:
    """Use short IDs from `Получить игроков`; fuzzy-match names and absent rows."""
    if visual_audit is not None and (
        not visual_audit.get("is_scoreboard")
        or not visual_audit.get("is_final_result")
        or visual_audit.get("has_live_gameplay_hud")
    ):
        return None
    modal = parse_players_modal(modal_text)
    slots = parse_card_roster_slots(message_text)
    match = re.search(r"Результат\s+матча\s*#\s*(\d+)", message_text, re.I)
    headers = [re.search(rf"Команда\s*{x}[^\n]*", message_text, re.I) for x in "AB"]
    if modal is None or slots is None or match is None or not all(headers):
        return None

    def score(header: str) -> Optional[int]:
        found = re.search(r"(?:CT|T)?\s*[-–—:|·]\s*(\d+)\s*[-–—:|·]\s*[KК][/\\][AА][/\\][CDСД]", header, re.I)
        return int(found.group(1)) if found else None

    score_a, score_b = score(headers[0].group(0)), score(headers[1].group(0))
    if score_a is None or score_b is None:
        found = re.search(r"Распознано\s+со\s+скриншота\s*:\s*(\d+)\s*[-:]\s*(\d+)", message_text, re.I)
        if not found:
            return None
        score_a, score_b = map(int, found.groups())

    def kad(player: dict) -> tuple[int, int, int]:
        value = tuple(int(player.get(key, 0)) for key in ("kills", "assists", "deaths"))
        return (0, 0, 13) if value in {(0, 0, 0), (0, 0, 13)} else value

    ids_a = {p["id"] for p in slots["team_a"] if p.get("id")}
    ids_b = {p["id"] for p in slots["team_b"] if p.get("id")}
    ids_ct = {p["id"] for p in modal["CT"]}
    ids_t = {p["id"] for p in modal["T"]}
    direct = len(ids_a & ids_ct) + len(ids_b & ids_t)
    swapped = len(ids_a & ids_t) + len(ids_b & ids_ct)
    stated = re.search(r"(?:команда\s*)?A\s+начинала\s+за\s+(CT|T)\b", message_text, re.I)
    if stated:
        side_a = stated.group(1).upper()
    elif direct != swapped:
        side_a = "CT" if direct > swapped else "T"
    else:
        return None
    side_b = "T" if side_a == "CT" else "CT"

    visual_players: list[dict] = []
    if (
        visual_audit
        and visual_audit.get("is_scoreboard")
        and visual_audit.get("is_final_result")
        and not visual_audit.get("has_live_gameplay_hud")
    ):
        visual_players = [
            *list(visual_audit.get("left_players", [])),
            *list(visual_audit.get("right_players", [])),
        ]

    def visual_stats_for_nickname(nickname: str) -> Optional[tuple[int, int, int]]:
        if not nickname or not visual_players:
            return None
        ranked = sorted(
            (
                (nickname_similarity(nickname, player.get("nickname", "")), player)
                for player in visual_players
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.72:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
            return None
        return kad(ranked[0][1])

    def assign(card: list[dict], helper: list[dict]) -> Optional[list[dict]]:
        assigned: list[Optional[int]] = [None] * 5
        unused = {int(p["id"]) for p in helper}
        by_id = {int(p["id"]): p for p in helper}
        for i, player in enumerate(card):
            if player.get("id") is not None:
                player_id = int(player["id"])
                # IDs rendered inside Discord display names can be stale.
                # Trust them only when that ID is present in «Получить игроков».
                if player_id in unused:
                    assigned[i] = player_id
                    unused.remove(player_id)
        # Named helper formats: versus also matches версус/versustop/111versus.
        for i, player in enumerate(card):
            if assigned[i] is not None:
                continue
            candidates = [pid for pid in unused if by_id[pid].get("nickname") and nicknames_match(player.get("nickname", ""), by_id[pid]["nickname"])]
            if len(candidates) == 1:
                assigned[i] = candidates[0]; unused.remove(candidates[0])
        # The helper preserves roster order; then use elimination.
        for i, helper_player in enumerate(helper):
            pid = int(helper_player["id"])
            if assigned[i] is None and pid in unused:
                assigned[i] = pid; unused.remove(pid)
        missing = [i for i, pid in enumerate(assigned) if pid is None]
        if len(missing) == len(unused) == 1:
            assigned[missing[0]] = unused.pop()
        if any(pid is None for pid in assigned) or unused:
            return None
        output = []
        for player, pid in zip(card, assigned):
            helper_player = by_id[int(pid)]
            helper_raw = (
                int(helper_player.get("kills", 0) or 0),
                int(helper_player.get("assists", 0) or 0),
                int(helper_player.get("deaths", 0) or 0),
            )
            # Non-zero statistics from «Получить игроков» are already known
            # and must not be read again from the screenshot.
            if helper_raw != (0, 0, 0):
                kills, assists, deaths = helper_raw
            else:
                # Only a 0/0/0 row needs recovery. Find its cleaned nickname
                # on the final scoreboard (tags such as CLION/OLD are ignored).
                recovered = visual_stats_for_nickname(str(player.get("nickname", "")))
                card_stats = kad(player)
                if recovered is not None and recovered != (0, 0, 13):
                    kills, assists, deaths = recovered
                elif card_stats != (0, 0, 13):
                    kills, assists, deaths = card_stats
                else:
                    kills, assists, deaths = (0, 0, 13)
            output.append({"id": int(pid), "nickname": player.get("nickname", ""), "kills": kills, "assists": assists, "deaths": deaths, "confidence": 1.0})
        return output

    team_a = assign(slots["team_a"], modal[side_a])
    team_b = assign(slots["team_b"], modal[side_b])
    if team_a is None or team_b is None:
        return None
    if len({p["id"] for p in [*team_a, *team_b]}) != 10:
        return None
    return {"is_match_result": True, "match_id": int(match.group(1)), "score_a": score_a, "score_b": score_b, "ct_team": "A" if side_a == "CT" else "B", "team_a": team_a, "team_b": team_b, "overall_confidence": 1.0, "notes": "ID и ненулевая статистика взяты из «Получить игроков»; только строки 0/0/0 восстановлены по очищенному нику на скриншоте или зарегистрированы 0/0/13."}


def reconcile_numeric_mentions(result: dict, message_text: str) -> bool:
    """Match long numeric mentions to scoreboard players by exact K/A/D."""
    slots_by_team = parse_card_roster_slots(message_text)
    if slots_by_team is None:
        return True

    for team_key in ("team_a", "team_b"):
        slots = slots_by_team[team_key]
        candidates = result.get(team_key, [])
        if len(candidates) != 5:
            return False
        unused = set(range(5))
        reconciled: list[dict] = []

        for slot in slots:
            exact = [
                index for index in unused
                if candidates[index].get("kills") == slot["kills"]
                and candidates[index].get("assists") == slot["assists"]
                and candidates[index].get("deaths") == slot["deaths"]
            ]
            chosen: Optional[int] = None
            recovered_id = slot["id"]

            if recovered_id is not None:
                same_id = [
                    index for index in unused
                    if candidates[index].get("id") == recovered_id
                ]
                chosen = same_id[0] if same_id else (exact[0] if len(exact) == 1 else None)
            elif len(exact) == 1:
                chosen = exact[0]
                candidate = candidates[chosen]
                nickname_id = re.search(
                    r"(?:^|\[|#)(\d{2,5})(?:\]|\s|\|)",
                    str(candidate.get("nickname", "")),
                )
                recovered_id = int(nickname_id.group(1)) if nickname_id else candidate.get("id")

            if chosen is None or not isinstance(recovered_id, int) or recovered_id <= 0:
                return False

            unused.remove(chosen)
            player = dict(candidates[chosen])
            player.update({
                "id": recovered_id,
                "kills": slot["kills"],
                "assists": slot["assists"],
                "deaths": slot["deaths"],
            })
            if slot["nickname"] and not player.get("nickname"):
                player["nickname"] = slot["nickname"]
            if slot["nickname"]:
                player.setdefault("card_nickname", slot["nickname"])
            reconciled.append(player)

        result[team_key] = reconciled
    return True


def parse_complete_card(message_text: str) -> Optional[dict]:
    """Read exact IDs/KAD from any complete result card; AI only selects CT/T."""
    match = re.search(r"Результат\s+матча\s*#\s*(\d+)", message_text, re.I)
    header_a = re.search(r"Команда\s*A[^\n]*", message_text, re.I)
    header_b = re.search(r"Команда\s*B[^\n]*", message_text, re.I)
    if not match or not header_a or not header_b or header_b.start() <= header_a.start():
        return None

    def header_score(header: str) -> Optional[int]:
        found = re.search(
            r"(?:CT|T)?\s*[·•:|\-–—]\s*(\d+)\s*[·•:|\-–—]\s*"
            r"[KК][/\\][AА][/\\][CDСД]",
            header,
            re.I,
        )
        return int(found.group(1)) if found else None

    score_a = header_score(header_a.group(0))
    score_b = header_score(header_b.group(0))
    if score_a is None or score_b is None:
        recognized = re.search(
            r"Распознано\s+со\s+скриншота\s*:\s*(\d+)\s*[-:]\s*(\d+)",
            message_text,
            re.I,
        )
        if not recognized:
            return None
        score_a, score_b = int(recognized.group(1)), int(recognized.group(2))

    player_pattern = re.compile(
        r"#\s*(\d{1,5})\s*(?:\|\s*)?([^\n—–]+?)\s*[—–-]\s*"
        r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)",
        re.I,
    )

    def parse_team(section: str) -> list[dict]:
        team: list[dict] = []
        for found in player_pattern.finditer(section):
            kills, assists, deaths = map(int, found.group(3, 4, 5))
            if kills == 0 and assists == 0 and deaths == 0:
                deaths = 13
            team.append(
                {
                    "id": int(found.group(1)),
                    "nickname": strip_leading_clan_tags(
                        found.group(2).strip(" `*_.,")
                    ),
                    "kills": kills,
                    "assists": assists,
                    "deaths": deaths,
                    "confidence": 0.99,
                }
            )
            if len(team) == 5:
                break
        return team

    team_a = parse_team(message_text[header_a.end():header_b.start()])
    team_b = parse_team(message_text[header_b.end():])
    if len(team_a) != 5 or len(team_b) != 5:
        return None

    ct_team = explicit_ct_team_from_card(message_text)

    return {
        "is_match_result": True,
        "match_id": int(match.group(1)),
        "score_a": score_a,
        "score_b": score_b,
        "ct_team": ct_team,
        "team_a": team_a,
        "team_b": team_b,
        "overall_confidence": 0.99,
        "notes": "ID, команды и K/A/D взяты напрямую из карточки.",
    }


def is_review_result_card(message_text: str) -> bool:
    """Detect only an explicit review status on the current result card.

    Do not trigger on an incidental phrase like `на проверку` in instructions,
    history, replies, or other embedded text.
    """
    # The title starts with a shield emoji in real Discord embeds, therefore
    # it must not be anchored to the beginning of a plain-text line.
    explicit_title = re.search(
        r"Результат\s+матча\s*#\s*\d+[^\n]{0,160}?на\s+проверку\b",
        message_text,
        re.I,
    )
    if explicit_title:
        return True

    lines = [line.strip() for line in message_text.splitlines() if line.strip()]
    for line in lines[:12]:
        if re.fullmatch(
            r"(?:⚠️\s*)?(?:статус\s*[:—–-]\s*)?на\s+проверку[.!]?",
            line,
            re.I,
        ):
            return True
    return False


def readable_score_from_context(message_text: str) -> Optional[tuple[int, int]]:
    """Extract the best already-readable A:B score for diagnostic logs."""
    recognized = re.search(
        r"Распознано\s+со\s+скриншота\s*:\s*(\d{1,2})\s*[-:]\s*(\d{1,2})",
        message_text,
        re.I,
    )
    if recognized:
        return int(recognized.group(1)), int(recognized.group(2))
    headers = [re.search(rf"Команда\s*{team}[^\n]*", message_text, re.I) for team in "AB"]
    values: list[int] = []
    for header in headers:
        if not header:
            return None
        found = re.search(
            r"(?:CT|T)?\s*[-–—:|·]\s*(\d{1,2})\s*[-–—:|·]\s*[KК][/\\][AА][/\\][CDСД]",
            header.group(0),
            re.I,
        )
        if not found:
            return None
        values.append(int(found.group(1)))
    return values[0], values[1]


def visual_audit_summary(audit: Optional[dict]) -> str:
    """What the model read from the scoreboard, for Discord error logs."""
    if not isinstance(audit, dict):
        return "ЧТО МОДЕЛЬ ПРОЧИТАЛА НА ТАБЛО: нет данных"

    def flag(key: str) -> str:
        return "да" if audit.get(key) else "нет"

    lines = [
        "ЧТО МОДЕЛЬ ПРОЧИТАЛА НА ТАБЛО:",
        f"  финальный экран={flag('is_final_result')} "
        f"табло={flag('is_scoreboard')} "
        f"живая игра/TAB={flag('has_live_gameplay_hud')} "
        f"сдача={flag('is_surrender')}",
        f"  уверенность={audit.get('overall_confidence')} "
        f"счёт слева:справа={audit.get('score_left')}:{audit.get('score_right')} "
        f"стороны={audit.get('side_left')}/{audit.get('side_right')}",
    ]
    for title, key in (("СЛЕВА", "left_players"), ("СПРАВА", "right_players")):
        players = audit.get(key) or []
        lines.append(f"  {title}: игроков {len(players)}")
        for player in players[:5]:
            if isinstance(player, dict):
                lines.append(
                    f"    ник={player.get('nickname', '')} "
                    f"K/A/D={player.get('kills')}/{player.get('assists')}/"
                    f"{player.get('deaths')}"
                )
    notes = str(audit.get("notes") or "").strip()
    if notes:
        lines.append(f"  пояснение модели: {notes[:600]}")
    lines.append(
        "  нужно для регистрации: финальный экран=да, живая игра/TAB=нет, "
        "уверенность ≥ 0.90, совпало ≥ 3 ников и хотя бы 1 на каждой стороне"
    )
    return "\n".join(lines)


def full_match_diagnostics(
    message_text: str,
    modal_text: Optional[str] = None,
    result: Optional[dict] = None,
) -> str:
    """Render score and every available player row for Railway error logs."""
    lines: list[str] = []
    score = readable_score_from_context(message_text)
    if result and result.get("score_a") is not None and result.get("score_b") is not None:
        lines.append(f"СЧЁТ A:B = {result['score_a']}:{result['score_b']}")
    elif score:
        lines.append(f"СЧЁТ A:B = {score[0]}:{score[1]}")
    else:
        lines.append("СЧЁТ A:B = НЕ ПРОЧИТАН")

    def add_players(title: str, players: list[dict]) -> None:
        lines.append(title)
        if not players:
            lines.append("  игроков разобрать не удалось")
            return
        for position, player in enumerate(players, 1):
            kills = int(player.get("kills", 0) or 0)
            assists = int(player.get("assists", 0) or 0)
            deaths = int(player.get("deaths", 0) or 0)
            if kills == assists == deaths == 0:
                deaths = 13
            player_id = player.get("id")
            nickname = str(player.get("nickname", "") or "неизвестный ник")
            lines.append(
                f"  {position}. ID={player_id if player_id is not None else 'длинный/не найден'} "
                f"ник={nickname} K/A/D={kills}/{assists}/{deaths}"
            )

    if result:
        add_players("КОМАНДА A:", list(result.get("team_a", [])))
        add_players("КОМАНДА B:", list(result.get("team_b", [])))
    else:
        slots = parse_card_roster_slots(message_text)
        if slots:
            add_players("КОМАНДА A ИЗ КАРТОЧКИ:", slots["team_a"])
            add_players("КОМАНДА B ИЗ КАРТОЧКИ:", slots["team_b"])
        else:
            identities = parse_card_roster_identities(message_text)

            def add_identities(title: str, players: list[dict]) -> None:
                lines.append(title)
                if not players:
                    lines.append("  игроков разобрать не удалось")
                    return
                for position, player in enumerate(players, 1):
                    lines.append(
                        f"  {position}. ID={player['id']} "
                        f"ник={player.get('nickname', '')} K/A/D=ИЗ СКРИНШОТА"
                    )

            add_identities(
                "КОМАНДА A ИЗ КАРТОЧКИ:",
                identities["team_a"] if identities else [],
            )
            add_identities(
                "КОМАНДА B ИЗ КАРТОЧКИ:",
                identities["team_b"] if identities else [],
            )

    if modal_text:
        modal = parse_players_modal(modal_text)
        add_players("ПОЛУЧИТЬ ИГРОКОВ — CT:", modal["CT"] if modal else [])
        add_players("ПОЛУЧИТЬ ИГРОКОВ — T:", modal["T"] if modal else [])
    return "\n".join(lines)


async def recognize_match(
    images: list[bytes],
    message_text: str = "",
    score_only: bool = False,
    visual_audit: bool = False,
) -> dict:
    if score_only and visual_audit:
        raise ValueError("score_only и visual_audit нельзя включать одновременно")
    card_result = None if (score_only or visual_audit) else parse_complete_card(message_text)
    card_has_placeholder_stats = bool(
        card_result
        and any(
            (
                int(player.get("kills", -1)),
                int(player.get("assists", -1)),
                int(player.get("deaths", -1)),
            ) == (0, 0, 13)
            for player in [
                *card_result.get("team_a", []),
                *card_result.get("team_b", []),
            ]
        )
    )
    if (
        card_result is not None
        and card_result.get("ct_team") in ("A", "B")
        # A normal completed game has a 13+ winner.  A sub-13 result may be a
        # surrender and must still inspect the screenshot before registration.
        and max(
            int(card_result.get("score_a", 0) or 0),
            int(card_result.get("score_b", 0) or 0),
        ) >= 13
        and not card_has_placeholder_stats
    ):
        log.info(
            "Матч #%s разобран напрямую без запроса к ИИ",
            card_result["match_id"],
        )
        return card_result

    player_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "nickname": {"type": "string"},
            "kills": {"type": "integer", "minimum": 0, "maximum": 100},
            "assists": {"type": "integer", "minimum": 0, "maximum": 100},
            "deaths": {"type": "integer", "minimum": 0, "maximum": 100},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "id",
            "nickname",
            "kills",
            "assists",
            "deaths",
            "confidence",
        ],
        "additionalProperties": False,
    }
    response_schema = {
        "type": "object",
        "properties": {
            "is_match_result": {"type": "boolean"},
            "is_surrender": {"type": "boolean"},
            "winner_team": {"type": ["string", "null"], "enum": ["A", "B", None]},
            "match_id": {"type": ["integer", "null"]},
            "score_a": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
            "score_b": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
            "ct_team": {"type": ["string", "null"], "enum": ["A", "B", None]},
            "team_a": {"type": "array", "items": player_schema, "maxItems": 5},
            "team_b": {"type": "array", "items": player_schema, "maxItems": 5},
            "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "notes": {"type": "string"},
        },
        "required": [
            "is_match_result",
            "is_surrender",
            "winner_team",
            "match_id",
            "score_a",
            "score_b",
            "ct_team",
            "team_a",
            "team_b",
            "overall_confidence",
            "notes",
        ],
        "additionalProperties": False,
    }

    if visual_audit:
        visual_player_schema = {
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "kills": {"type": "integer", "minimum": 0, "maximum": 100},
                "assists": {"type": "integer", "minimum": 0, "maximum": 100},
                "deaths": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["nickname", "kills", "assists", "deaths"],
            "additionalProperties": False,
        }
        response_schema = {
            "type": "object",
            "properties": {
                "is_scoreboard": {"type": "boolean"},
                "is_final_result": {"type": "boolean"},
                "has_live_gameplay_hud": {"type": "boolean"},
                "is_surrender": {"type": "boolean"},
                "winner_side": {"type": ["string", "null"], "enum": ["LEFT", "RIGHT", None]},
                "score_left": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
                "score_right": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
                "side_left": {"type": ["string", "null"], "enum": ["CT", "T", None]},
                "side_right": {"type": ["string", "null"], "enum": ["CT", "T", None]},
                "left_players": {"type": "array", "items": visual_player_schema, "minItems": 1, "maxItems": 5},
                "right_players": {"type": "array", "items": visual_player_schema, "minItems": 1, "maxItems": 5},
                "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "notes": {"type": "string"},
            },
            "required": ["is_scoreboard", "is_final_result", "has_live_gameplay_hud", "is_surrender", "winner_side", "score_left", "score_right", "side_left", "side_right", "left_players", "right_players", "overall_confidence", "notes"],
            "additionalProperties": False,
        }

    if visual_audit:
        prompt = """Strictly transcribe the attached STANDOFF 2 scoreboard from the pixels.
This bot accepts both the final post-match results screen AND an opened TAB scoreboard during live gameplay. Set is_final_result=true and is_scoreboard=true for both cases.
Set has_live_gameplay_hud=true when active-match elements are visible around/through the table (weapons, hands, crosshair, etc.). Even if live elements are present, YOU MUST FULLY TRANSCRIBE the scoreboard and set is_final_result=true.
Copy the two large score numbers in visible LEFT-to-RIGHT order. Never add the current or next round: if the image displays 8 and 13, return 8 and 13, never 8 and 14.
If the result says `СДАЛИСЬ`/surrendered, set is_surrender=true. Set winner_side to the side that DID NOT surrender. The `СДАЛИСЬ` label belongs to the side that surrendered, so the opposite side is the winner. For a normal completed game set is_surrender=false and winner_side=null. Keep score_left/score_right as the raw numbers visibly printed; the program will convert the winner to 13.
Return side_left and side_right as CT or T. Transcribe every VISIBLE player per side, top to bottom. A side can contain from one to five visible rows when players are absent; never invent missing rows. The match may be accepted when at least four card players are reliably matched in total.
Russian columns У, П, С mean kills, assists, deaths. On the T/ATTACK side a MONEY column appears before У/П/С; ignore money. Ignore score/points and ping after deaths.
For nicknames, ignore the faded clan/tag prefix before the actual nickname. Examples: `[CLION] Zerro` and `CLION | Zerro` mean nickname `Zerro`; `[swean] Кредо` means nickname `Кредо`. `OLD` is always a clan/league tag, never the player's nickname: `OLD|Shkiper`, `OLD | Shkiper`, `[OLD] Shkiper`, and `🔴 OLD — Shkiper` all mean nickname `Shkiper`.
The appended Discord card text contains the ten roster nicknames. Use those names only as spelling/OCR candidates for visually compatible scoreboard rows. This is especially important for short or decorated names such as card `McL` versus scoreboard `[xtng] McL Bo$$`, `clutch` versus `[xtng] clutch lv david`, and `fellmy` versus `fellmy Bo$$`. Never copy K/A/D or scores from the text.
Do not infer, increment, normalize, or copy statistics from Discord text. Only the attached game screenshot is evidence.
Set confidence below 0.90 if any score or K/A/D digit is unclear. Return only valid JSON."""
    elif score_only:
        prompt = """Read ONLY the final score of this FACEIT/CS2 match from the attached result screenshot.
Reject an opened TAB scoreboard from a match that is still being played. Gameplay HUD, weapon/hands, health/armor/ammo, crosshair, live kill feed, pause icon, spectator controls, or minimap means this is not a final result: set is_match_result=false.
The Discord card text identifies Team A and Team B. Return score_a and score_b as rounds won by those exact teams, mapping the scoreboard sides to A/B by player nicknames when needed.
Ignore any helper template containing `<счёт A> <счёт B>`: those are placeholders, not a score.
Read the large final scoreboard/result score from the image. Typical valid results are 13:0 through overtime scores.
For a surrender result (`СДАЛИСЬ`), set is_surrender=true and winner_team to the team that did not surrender. Keep the losing team's displayed round count, but return 13 for the winning team. For a normal result set is_surrender=false and winner_team=null.
Set is_match_result=true when a final match scoreboard is visible. Return the visible match number when available, otherwise null.
For this score-only request return team_a=[] and team_b=[], ct_team=null. overall_confidence describes confidence in the two score numbers. Explain briefly in notes."""
    else:
        prompt = """You receive one or more screenshots of the SAME FACEIT/CS2 match result.
Accept ONLY a dedicated final post-match result screen. If the table is merely opened with TAB during live gameplay and the image also shows a weapon/hands, health/armor/ammo, crosshair, live kill feed, pause icon, spectator controls, or minimap, set is_match_result=false and do not build a registration, even when the table shows 13 or `ПОБЕДА`.
The Discord result card contains match number and two rosters: Team A and Team B, with numeric IDs like #37 and nicknames. The small CS2 scoreboard contains each nickname and columns K, A, D.
Build a registration result:
- match_id: number after 'Результат матча #'.
- Team A must always be returned in team_a; Team B in team_b.
- score_a and score_b are rounds won by Team A and Team B. The CS2 scoreboard may label sides ATTACK/DEFENSE or T/CT and teams can be on either side; map score to A/B by matching player nicknames.
- Surrendered games are valid. If the screenshot says `СДАЛИСЬ`, set is_surrender=true, identify which side surrendered, and set winner_team to the opposite Team A/B roster. Register the winner with 13 rounds and keep the loser's displayed round count. Read and match every visible player's K/A/D exactly as in a normal match. For normal games set is_surrender=false and winner_team=null.
- ct_team MUST be `A` when Team A is on the CT/DEFENSE side of the screenshot, or `B` when Team B is on CT/DEFENSE. Never assume Team A is CT. Determine it by matching roster nicknames and scores to the CT/DEFENSE half of the scoreboard.
- Cards titled 'на проверку' are valid match results and MUST be registered when match number, score and rosters can be recovered. These cards often already contain short # IDs and K/A/D next to every player; use those values directly even when the attached scoreboard is small or blurry.
- In review cards, strings like `@#64 | kanei — 8/2/12` mean registration id=64, nickname=kanei, kills=8, assists=2, deaths=12. The @ formatting does not turn the short # number into a Discord user ID.
- If a review card says a player was not found and shows 0/0/0, apply the required absent-row default 0/0/13 and still keep that player.
- For every roster player return the SHORT registration ID printed with # immediately before the nickname/mention. It is usually 2, 3 or 4 digits (for example #37, #539, #1639). Use the complete short # number. NEVER use a long Discord mention/user ID such as 1524375653149966517.
- NEVER invent positional IDs such as 1,2,3,4,5 or 5,4,3,2,1. Array position is not a player ID. If the card contains a line like `Нет статистики для игроков: #89, #124, ...`, those ten short # numbers are the roster IDs in displayed order and must be returned exactly.
- Some roster names are Discord mentions or contain only digits. A numeric-only mention is NOT the nickname. Identify that player by the K/A/D printed beside or below the roster entry, then match those K/A/D values to the unique scoreboard row and recover the real nickname from the scoreboard.
- Review cards can mix normal entries and raw mentions, for example `<@1277880356242067460> — 17/1/10`. The 17/1/10 belongs to that exact roster slot. Match it only against a scoreboard row on the same team/side with the same K/A/D.
- After matching a raw numeric mention to its scoreboard row, read the complete leading 2-, 3-, or 4-digit registration number immediately before the nickname (often displayed as `#89 Nick`, `[89] Nick`, or `89 | Nick`). Use that number as id. Never use the row number 1-5 and never take digits from the long Discord mention.
- Explicit side labels in the card are authoritative. `Команда A - T` and `Команда B - CT` means ct_team=B; `Команда A - CT` means ct_team=A. Never reverse explicit labels based on assumptions.
- K/A/D printed in a review card is authoritative. Copy it exactly for every roster slot; use the image only to recover the nickname and short ID for numeric mentions.
- IMPORTANT EXCEPTION: `0/0/13` printed in the Discord card is a missing-match placeholder, not authoritative statistics. If that roster nickname exists on the attached scoreboard, replace the placeholder with the exact visible K/A/D from the screenshot. Ignore clan tags such as `OLD |`, `[OLD]`, `[NOOBS]` and similar prefixes while matching. Example: card `OLD | Hatefull — 0/0/13` plus scoreboard `Hatefull — 22/3/13` must return Hatefull as 22/3/13, never 0/0/13.
- When several numeric-only roster entries exist, solve them globally: compare all visible K/A/D values and all still-unmatched scoreboard rows, and never assign one scoreboard row twice. Use team membership, roster order and remaining unmatched rows as tie-breakers.
- Fuzzy nickname matching is REQUIRED. Ignore case, spaces, punctuation, clan tags, decorative prefixes/suffixes and extra text. A roster nickname contained inside a scoreboard nickname is a match: for example `versus`, `versusproto`, `[TAG]versus` and `versus_123` refer to the same player when there is no conflicting roster nickname.
- Match obvious Cyrillic/Latin phonetic spellings too. For example Latin `versus` may appear as Cyrillic `версус`.
- Never assign one scoreboard row to two roster players. Prefer the unique strongest nickname match across all ten roster players.
- If a roster player has NO matching scoreboard row, return that player with kills=0, assists=0, deaths=13. Keep confidence at least 0.90 when absence is clear.
- Scoreboard columns are usually kills, assists, deaths, score/points, ping. Return ONLY kills, assists, deaths.
- Keep all five roster players and their roster order exactly as shown in Team A and Team B.
- If several screenshots are supplied, combine their information.
- If a matching row exists but an individual number is unreadable, lower confidence and explain in notes; do not use 0/0/13 unless the whole row is absent.
- is_match_result=false for unrelated images; then use null IDs/scores and empty teams.
- A valid result has exactly five players in each team."""
    if message_text.strip():
        prompt += f"\nAuthoritative Discord card text:\n{message_text[:6000]}"

    parts: list[dict] = [{"text": prompt}]
    for raw in images[:4]:
        image_b64, mime = prepare_image(raw)
        parts.append({"inline_data": {"mime_type": mime, "data": image_b64}})

    gemini_payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseJsonSchema": response_schema,
        },
    }

    openai_content: list[dict] = [
        {
            "type": "text",
            "text": prompt
            + "\nReturn ONLY valid JSON matching this schema:\n"
            + json.dumps(response_schema, ensure_ascii=False),
        }
    ]
    for image_part in parts[1:]:
        inline = image_part["inline_data"]
        openai_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{inline['mime_type']};base64,{inline['data']}"
                },
            }
        )

    timeout = aiohttp.ClientTimeout(total=120)
    retryable_statuses = {429, 500, 502, 503, 504}
    assigned_model, _assigned_api_key, assigned_key_number = next_gemini_assignment()
    models = [assigned_model, *(name for name in GEMINI_MODELS if name != assigned_model)]
    key_slots = ai_key_order(assigned_key_number - 1)
    log.info(
        "Игра назначена модели %s, первый ключ: %s",
        assigned_model,
        key_slots[0]["label"] if key_slots else "нет",
    )
    # Keys are tried best first (see ai_key_order), each with every model. A
    # key that reports a spent limit or balance is not tried with the other
    # models: they share its balance.

    def request_for(model: str, api_key: str) -> tuple[str, dict, dict]:
        if AI_API_STYLE == "openai":
            return (
                f"{GEMINI_BASE_URL}/chat/completions",
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                {
                    "model": model,
                    "messages": [{"role": "user", "content": openai_content}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
        return (
            f"{GEMINI_BASE_URL}/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": api_key},
            gemini_payload,
        )

    def parse_model_output(data: object) -> dict:
        try:
            if AI_API_STYLE == "openai":
                output_text = data["choices"][0]["message"]["content"]
            else:
                output_text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"ИИ не вернул результат распознавания: {str(data)[:200]}"
            ) from exc
        if isinstance(output_text, list):
            output_text = "".join(
                item.get("text", "") for item in output_text if isinstance(item, dict)
            )
        output_text = str(output_text).strip()
        if output_text.startswith("```"):
            output_text = output_text.split("\n", 1)[1]
            output_text = output_text.rsplit("```", 1)[0].strip()
        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            raise RuntimeError("ИИ вернул JSON не в виде объекта")
        return parsed

    result: Optional[dict] = None
    used_slot: Optional[dict] = None
    failures: list[str] = []
    attempt_number = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for slot in key_slots:
            for model in models:
                url, headers, request_payload = request_for(model, slot["key"])
                # Transient errors are retried only on the first attempt of
                # the game; every other key/model pair gets one attempt.
                tries = max(1, GEMINI_MAX_RETRIES) if attempt_number == 0 else 1
                attempt_number += 1
                error = "нет ответа"
                key_spent = False
                for retry in range(tries):
                    try:
                        async with session.post(
                            url, json=request_payload, headers=headers
                        ) as response:
                            status = response.status
                            body = await response.text()
                        if status < 400:
                            result = parse_model_output(json.loads(body))
                            break
                        error = f"HTTP {status}: {body[:300]}"
                        log.warning(
                            "ИИ %s (%s) вернул %s",
                            model,
                            slot["label"],
                            error,
                        )
                        if is_ai_key_exhausted(status, body):
                            key_spent = True
                            break
                        if status not in retryable_statuses:
                            break
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {str(exc)[:300]}"
                        log.warning(
                            "ИИ %s (%s): %s",
                            model,
                            slot["label"],
                            error,
                        )
                    if retry + 1 < tries:
                        await asyncio.sleep(3 * (2**retry))
                if result is not None:
                    used_slot = slot
                    break
                failures.append(f"{model} / {slot['label']}: {error}")
                if key_spent:
                    note_ai_key_exhausted(slot, error)
                    break
            if result is not None:
                break

    if result is None or used_slot is None:
        note_ai_all_failed()
        raise RuntimeError(
            "ИИ не распознал скриншот ни одной моделью и ключом. "
            + " | ".join(failures)[:1500]
        )
    note_ai_key_success(used_slot)
    if failures:
        log.info("Скриншот распознан: %s, %s", model, used_slot["label"])
    # Apply the same clan-tag cleanup to every recognition route.  Tags such
    # as `OLD | Shkiper`, `[NOOBS] TRIXI67` and `[XAskу] apathy` identify the
    # clan/league, not any part of the player's nickname.
    for team_key in ("team_a", "team_b", "left_players", "right_players"):
        for player in result.get(team_key, []) or []:
            if isinstance(player, dict) and player.get("nickname"):
                player["nickname"] = strip_leading_clan_tags(
                    str(player["nickname"])
                )
    if result.get("is_surrender"):
        if visual_audit:
            winner_side = result.get("winner_side")
            if winner_side == "LEFT":
                result["score_left"] = 13
            elif winner_side == "RIGHT":
                result["score_right"] = 13
            else:
                result["overall_confidence"] = 0.0
                result["notes"] = (
                    str(result.get("notes", ""))
                    + " Не удалось определить победившую сторону при сдаче."
                ).strip()
        else:
            winner_team = result.get("winner_team")
            if winner_team == "A":
                result["score_a"] = 13
            elif winner_team == "B":
                result["score_b"] = 13
            else:
                result["overall_confidence"] = 0.0
                result["notes"] = (
                    str(result.get("notes", ""))
                    + " Не удалось определить победившую команду при сдаче."
                ).strip()
    if score_only or visual_audit:
        return result
    # A genuine 0/0/0 row is registered as 0/0/13, while retaining the exact
    # warning reason so it cannot later be mistaken for a nickname mismatch.
    for team_key in ("team_a", "team_b"):
        for player in result.get(team_key, []):
            if (
                int(player.get("kills", 0) or 0) == 0
                and int(player.get("assists", 0) or 0) == 0
                and int(player.get("deaths", 0) or 0) == 0
            ):
                player["deaths"] = 13
                player["warning_reason"] = "додж статистики"
    explicit_ct_team = explicit_ct_team_from_card(message_text)
    if explicit_ct_team in ("A", "B"):
        result["ct_team"] = explicit_ct_team
    if not reconcile_numeric_mentions(result, message_text):
        result["overall_confidence"] = 0.0
        result["notes"] = (
            "Не удалось однозначно сопоставить цифровые упоминания "
            "со строками таблицы по K/A/D; команда не будет отправлена."
        )
    if card_result is not None:
        # A complete Discord card can still contain synthetic 0/0/13 rows
        # when its own nickname parser treated a clan tag (for example OLD)
        # as part of the nickname. Recover only those placeholders from the
        # screenshot-backed AI result; keep every other card row untouched.
        recovered_placeholders = 0
        for team_key in ("team_a", "team_b"):
            visual_candidates = list(result.get(team_key, []))
            for card_player in card_result.get(team_key, []):
                card_stats = (
                    int(card_player.get("kills", -1)),
                    int(card_player.get("assists", -1)),
                    int(card_player.get("deaths", -1)),
                )
                if card_stats != (0, 0, 13):
                    continue
                ranked = sorted(
                    (
                        (
                            nickname_similarity(
                                card_player.get("nickname", ""),
                                candidate.get("nickname", ""),
                            ),
                            candidate,
                        )
                        for candidate in visual_candidates
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if not ranked or ranked[0][0] < 0.72:
                    continue
                if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.08:
                    continue
                candidate = ranked[0][1]
                recovered_stats = (
                    int(candidate.get("kills", -1)),
                    int(candidate.get("assists", -1)),
                    int(candidate.get("deaths", -1)),
                )
                if recovered_stats == (0, 0, 13) or min(recovered_stats) < 0:
                    continue
                card_player.setdefault(
                    "card_nickname",
                    str(card_player.get("nickname", "")),
                )
                card_player["nickname"] = strip_leading_clan_tags(
                    str(candidate.get("nickname") or card_player.get("nickname", ""))
                )
                card_player["kills"], card_player["assists"], card_player["deaths"] = recovered_stats
                card_player.pop("warning_reason", None)
                recovered_placeholders += 1
        if recovered_placeholders:
            card_result["notes"] += (
                f" Восстановлено строк 0/0/13 со скриншота: {recovered_placeholders}."
            )
        if result.get("is_surrender"):
            # Keep exact player K/A/D from the complete Discord card, but use
            # the normalized 13:X surrender score read from the screenshot.
            card_result["score_a"] = int(result["score_a"])
            card_result["score_b"] = int(result["score_b"])
            card_result["notes"] += " Счёт сдачи нормализован до 13 раундов победителю."
        ct_team = explicit_ct_team or result.get("ct_team")
        if ct_team not in ("A", "B"):
            card_result["overall_confidence"] = 0.0
            card_result["notes"] = "Не удалось надёжно определить сторону CT."
        else:
            card_result["ct_team"] = ct_team
            card_result["notes"] += f" CT определена как команда {ct_team}."
        return card_result
    return result


WARNING_REASON_LABELS = {
    "нет на скриншоте": "Обнуление игровой статистики",
    "неправильный ник": "Несоответствие игрового никнейма.",
    "додж статистики": "Обнуление игровой статистики",
}


def zeroed_result_players(result: dict) -> list[dict]:
    """Players whose final registration row is the synthetic 0/0/13."""
    zeroed: list[dict] = []
    for player in [*result.get("team_a", []), *result.get("team_b", [])]:
        try:
            stats = tuple(
                int(player.get(key, -1))
                for key in ("kills", "assists", "deaths")
            )
        except (TypeError, ValueError):
            continue
        if stats == (0, 0, 13):
            zeroed.append(player)
    return zeroed


# A dense ten-row scoreboard is sometimes read one row short: the last row of
# a side is the usual victim, and the card player behind it is then sent as
# 0/0/13 with a warning. The statistics printed on the screenshot belong to
# that player, so before the =g command is sent every 0/0/13 row is searched
# again with a focused read of the same screenshot.
RECOVERY_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "players": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "card_nickname": {"type": "string"},
                    "found": {"type": "boolean"},
                    "side": {
                        "type": ["string", "null"],
                        "enum": ["CT", "T", None],
                    },
                    "nickname": {"type": ["string", "null"]},
                    "kills": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "assists": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "deaths": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "required": [
                    "card_nickname",
                    "found",
                    "side",
                    "nickname",
                    "kills",
                    "assists",
                    "deaths",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["players", "notes"],
    "additionalProperties": False,
}

RECOVERY_PROMPT = """You received this STANDOFF 2 final result scoreboard before, but the rows of the card players listed below were not found or came back empty. They are about to be registered with 0 kills, 0 assists and 13 deaths.
Read the attached scoreboard ONE MORE TIME and find the row of every nickname below.
The table shows one row per player, up to five rows on each side of the scoreboard. Go through BOTH sides row by row, from top to bottom, and check the LAST row of each side explicitly: exactly that row is missed most often. Do not report a row as absent while any row of that side is still unattributed.
Russian columns У, П, С mean kills, assists, deaths. On the attack side a money column may stand before У/П/С: ignore money, and ignore score/points and ping after deaths. Copy only the digits printed next to the nickname.
A row may show a clan/league tag before the nickname, such as `[GT] Taule`, `GT | Taule`, `OLD | Taule` or `🔴 GT — Taule`. Such a row belongs to the player whose nickname matches apart from that tag.
The scoreboard nickname may also be longer than the card nickname (`McL` on the card versus `[xtng] McL Bo$$` on the board): accept the row when the card nickname is contained in the visible nickname.
For every requested player return: card_nickname exactly as listed below; found=true with side CT or T, the visible nickname and its exact kills, assists and deaths; or found=false when no row of the whole table belongs to that nickname.
Never return a 0/0/13 or 0/0/0 placeholder and never invent, copy or increment numbers: read the pixels of that exact row. If a digit of a found row is unreadable, return found=false and explain in notes.
Return only valid JSON.

Players to find:
"""


def parse_ai_json_answer(data: object) -> Optional[dict]:
    """Read the JSON object out of an OpenAI- or Gemini-style answer."""
    if AI_API_STYLE == "openai":
        try:
            output_text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
    else:
        try:
            output_text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(output_text, list):
        output_text = "".join(
            item.get("text", "") for item in output_text if isinstance(item, dict)
        )
    if not isinstance(output_text, str) or not output_text.strip():
        return None
    text = output_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def request_ai_json(
    prompt: str,
    images: list[bytes],
    schema: dict,
) -> Optional[dict]:
    """One JSON answer from the same model/key pool used for recognition."""
    parts: list[dict] = [{"text": prompt}]
    for raw in images[:4]:
        image_b64, mime = prepare_image(raw)
        parts.append({"inline_data": {"mime_type": mime, "data": image_b64}})
    content: list[dict] = [
        {
            "type": "text",
            "text": prompt
            + "\nReturn ONLY valid JSON matching this schema:\n"
            + json.dumps(schema, ensure_ascii=False),
        }
    ]
    for image_part in parts[1:]:
        inline = image_part["inline_data"]
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{inline['mime_type']};base64,{inline['data']}"
                },
            }
        )

    def request_for(model: str, api_key: str) -> tuple[str, dict, dict]:
        if AI_API_STYLE == "openai":
            return (
                f"{GEMINI_BASE_URL}/chat/completions",
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                {
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
        return (
            f"{GEMINI_BASE_URL}/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": api_key},
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            },
        )

    timeout = aiohttp.ClientTimeout(total=90)
    assigned_model, _assigned_key, assigned_key_number = next_gemini_assignment()
    models = [
        assigned_model,
        *(name for name in GEMINI_MODELS if name != assigned_model),
    ]
    key_slots = ai_key_order(assigned_key_number - 1)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for slot in key_slots:
            for model in models:
                url, headers, payload = request_for(model, slot["key"])
                try:
                    async with session.post(
                        url, json=payload, headers=headers
                    ) as response:
                        status = response.status
                        body = await response.text()
                    if status >= 400:
                        log.warning(
                            "Повторное чтение строк 0/0/13: ИИ %s (%s) вернул "
                            "HTTP %s: %s",
                            model,
                            slot["label"],
                            status,
                            body[:200],
                        )
                        if is_ai_key_exhausted(status, body):
                            note_ai_key_exhausted(
                                slot, f"HTTP {status}: {body[:300]}"
                            )
                            break
                        continue
                    parsed = parse_ai_json_answer(json.loads(body))
                    if parsed is not None:
                        note_ai_key_success(slot)
                        return parsed
                    log.warning(
                        "Повторное чтение строк 0/0/13: ИИ %s (%s) вернул "
                        "ответ без JSON",
                        model,
                        slot["label"],
                    )
                except Exception as exc:
                    log.warning(
                        "Повторное чтение строк 0/0/13: ИИ %s (%s) — %s: %s",
                        model,
                        slot["label"],
                        type(exc).__name__,
                        str(exc)[:200],
                    )
    return None


async def recover_zeroed_rows(
    images: list[bytes],
    players: list[dict],
) -> dict[str, tuple[int, int, int]]:
    """Second focused read of the scoreboard for the players sent as 0/0/13."""
    names: list[str] = []
    seen: set[str] = set()
    for player in players:
        nickname = str(
            player.get("card_nickname") or player.get("nickname") or ""
        ).strip()
        normalized = normalize_nickname(nickname)
        if not nickname or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(nickname)
    if not images or not names:
        return {}
    prompt = RECOVERY_PROMPT + "\n".join(f"- {name}" for name in names)
    try:
        answer = await request_ai_json(prompt, images, RECOVERY_ROW_SCHEMA)
    except Exception:
        log.exception("Повторное чтение строк 0/0/13 не удалось")
        return {}
    if not isinstance(answer, dict):
        return {}
    rows: dict[str, tuple[int, int, int]] = {}
    for row in answer.get("players", []) or []:
        if not isinstance(row, dict) or not row.get("found"):
            continue
        try:
            stats = (
                int(row["kills"]),
                int(row["assists"]),
                int(row["deaths"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if any(value < 0 or value > 100 for value in stats):
            continue
        # A placeholder row is not evidence: the real row was never read.
        if stats in {(0, 0, 0), (0, 0, 13)}:
            continue
        nickname = str(row.get("card_nickname") or "").strip()
        if not nickname:
            continue
        board_nickname = str(row.get("nickname") or "").strip()
        if board_nickname and not nicknames_match(nickname, board_nickname):
            log.warning(
                "Повторное чтение 0/0/13: ник карточки %r и ник на табло %r "
                "не совпали — строка не восстановлена.",
                nickname,
                board_nickname,
            )
            continue
        rows[normalize_nickname(nickname)] = stats
    return rows


def apply_recovered_rows(
    result: dict,
    rows: dict[str, tuple[int, int, int]],
) -> list[str]:
    """Give the screenshot statistics back to players who were zeroed."""
    recovered: list[str] = []
    if not rows:
        return recovered
    for player in [*result.get("team_a", []), *result.get("team_b", [])]:
        try:
            current = tuple(
                int(player.get(key, -1))
                for key in ("kills", "assists", "deaths")
            )
        except (TypeError, ValueError):
            continue
        if current != (0, 0, 13):
            continue
        nickname = str(player.get("nickname") or "").strip()
        card_nickname = str(player.get("card_nickname") or "").strip()
        stats = rows.get(normalize_nickname(card_nickname or nickname))
        if stats is None:
            continue
        if nickname and not player.get("card_nickname"):
            player["card_nickname"] = nickname
        player["kills"], player["assists"], player["deaths"] = stats
        player.pop("warning_reason", None)
        recovered.append(
            f"#{player.get('id')} {card_nickname or nickname} → "
            f"{stats[0]}/{stats[1]}/{stats[2]}"
        )
    return recovered


def mark_zero_stat_warning_reasons(result: dict) -> int:
    """Apply warning rules without confusing a nickname miss with stat dodge.

    An already detected nickname reason is authoritative. Only a row that is
    really registered as 0/0/13 is a statistics reset: the player disappeared
    from the final table, or his visible row came back empty. A low kill count
    is not a reset — a scoreboard row such as 3/4/17 is a played match and must
    never produce a warning. Nickname mismatch is used only when an unmatched
    visible row proves that the player played under another nick.
    """
    marked = 0
    for player in [*result.get("team_a", []), *result.get("team_b", [])]:
        try:
            kills = int(player.get("kills", -1))
            assists = int(player.get("assists", -1))
            deaths = int(player.get("deaths", -1))
        except (TypeError, ValueError):
            continue

        if player.get("warning_reason"):
            continue
        if (kills, assists, deaths) == (0, 0, 13):
            player["warning_reason"] = "додж статистики"
            marked += 1
    return marked


def format_registration(result: dict) -> str:
    ct_team = result.get("ct_team")
    if ct_team not in ("A", "B"):
        raise ValueError("Не определена команда, игравшая за CT")
    ct_players = result["team_a"] if ct_team == "A" else result["team_b"]
    t_players = result["team_b"] if ct_team == "A" else result["team_a"]
    lines = [
        f"=g {result['match_id']} {result['score_a']} {result['score_b']}",
        "",
        "CT",
    ]
    for player in ct_players:
        k = int(player.get("kills", 0) or 0)
        a = int(player.get("assists", 0) or 0)
        d = int(player.get("deaths", 0) or 0)
        if k == 0 and a == 0 and d == 0:
            d = 13
        lines.append(f"{player['id']} {k} {a} {d}")
    lines.extend(["", "T"])
    for player in t_players:
        k = int(player.get("kills", 0) or 0)
        a = int(player.get("assists", 0) or 0)
        d = int(player.get("deaths", 0) or 0)
        if k == 0 and a == 0 and d == 0:
            d = 13
        lines.append(f"{player['id']} {k} {a} {d}")
    return "\n".join(lines)


async def send_original_card_to_log(
    source_message: discord.Message,
    log_channel,
    match_id: int,
    fallback_title: Optional[str] = None,
) -> None:
    """Forward the original game card to logs, with a readable fallback."""
    forward_message = getattr(source_message, "forward", None)
    if callable(forward_message):
        try:
            await forward_message(log_channel)
            return
        except TypeError:
            try:
                await forward_message(destination=log_channel)
                return
            except Exception:
                log.warning(
                    "Не удалось переслать исходную карточку матча #%s; "
                    "используется резервная копия",
                    match_id,
                    exc_info=True,
                )
        except Exception:
            log.warning(
                "Не удалось переслать исходную карточку матча #%s; "
                "используется резервная копия",
                match_id,
                exc_info=True,
            )

    source_text = plain_message_text(source_message).strip()
    source_urls = image_urls(source_message)
    urls_text = "\n".join(source_urls[:4])
    prefix = fallback_title or f"🖼 Исходная карточка игры #{match_id}"
    prefix = f"{prefix.rstrip()}\n"
    available = max(0, 1990 - len(prefix) - len(urls_text))
    clipped_text = source_text[:available]
    chunks = [prefix.rstrip(), clipped_text, urls_text]
    await log_channel.send("\n".join(chunk for chunk in chunks if chunk).strip())


async def send_registration_log(
    result: dict,
    source_message: discord.Message,
    command_text: str,
) -> None:
    """Send every successful registration to the configured log channel."""
    if not LOG_CHANNEL_ID:
        return

    try:
        log_channel = client.get_channel(LOG_CHANNEL_ID)
        if log_channel is None:
            log_channel = await client.fetch_channel(LOG_CHANNEL_ID)

        await log_channel.send(
            f"✅ Зарегистрирована игра #{result['match_id']}\n"
            f"Счёт: {result['score_a']}:{result['score_b']}\n"
            f"Источник: <#{source_message.channel.id}>\n"
            f"```text\n{command_text}\n```"
        )
        await send_original_card_to_log(
            source_message,
            log_channel,
            int(result["match_id"]),
        )
    except Exception:
        log.exception(
            "Не удалось отправить лог матча #%s в канал %s",
            result.get("match_id"),
            LOG_CHANNEL_ID,
        )


async def send_processing_error_log(
    match_id: object,
    source_message: discord.Message,
    reason: str,
    diagnostics: str,
) -> None:
    """Send processing errors, score and all player stats to the Discord log channel."""
    if not LOG_CHANNEL_ID:
        return
    try:
        log_channel = client.get_channel(LOG_CHANNEL_ID)
        if log_channel is None:
            log_channel = await client.fetch_channel(LOG_CHANNEL_ID)
        header = (
            f"❌ Ошибка регистрации игры #{match_id}\n"
            f"Причина: {reason[:300]}\n"
            f"Источник: <#{source_message.channel.id}>\n"
        )
        # Discord messages are limited to 2000 characters. Size every chunk
        # by its own header so a long reason can never push a message over
        # the limit, and send the diagnostics in order so no row is lost.
        remaining = diagnostics or "Диагностика отсутствует"
        part = 0
        while remaining:
            prefix = header if part == 0 else f"❌ Игра #{match_id}, продолжение {part + 1}\n"
            room = 1990 - len(prefix) - len("```text\n\n```")
            chunk, remaining = remaining[:room], remaining[room:]
            await log_channel.send(f"{prefix}```text\n{chunk}\n```")
            part += 1
    except Exception:
        log.exception(
            "Не удалось отправить Discord-лог ошибки матча #%s в канал %s",
            match_id,
            LOG_CHANNEL_ID,
        )


def plain_message_text(message: discord.Message) -> str:
    chunks: list[str] = []
    for part in message_parts(message):
        content = getattr(part, "content", "")
        if content:
            chunks.append(str(content))
        for embed in getattr(part, "embeds", None) or []:
            if embed.title:
                chunks.append(str(embed.title))
            if embed.description:
                chunks.append(str(embed.description))
            for field in embed.fields:
                chunks.append(f"{field.name}\n{field.value}")
    return "\n".join(chunks)


def mentioned_users(message: discord.Message) -> list[object]:
    """Collect unique Discord users mentioned in the card or its snapshot."""
    users: dict[int, object] = {}
    for part in message_parts(message):
        for user in getattr(part, "mentions", None) or []:
            user_id = getattr(user, "id", None)
            if isinstance(user_id, int):
                users[user_id] = user
    return list(users.values())


def player_identity_names(player: dict) -> list[str]:
    """Registration (card) nickname first, screenshot nickname second."""
    names: list[str] = []
    for key in ("card_nickname", "nickname"):
        # Cards escape markdown in names (`Toma\_KFC`); Discord member search
        # and the warning text need the real nickname.
        value = re.sub(r"\\([\\`*_~|>])", r"\1", str(player.get(key) or "")).strip()
        if value and value not in names:
            names.append(value)
    return names


def registration_user_ids_from_names(
    display_names: dict[int, str],
) -> dict[int, int]:
    """Map short registration IDs to the Discord accounts mentioned in a card."""
    owners: dict[int, set[int]] = {}
    for member_id, name in display_names.items():
        registration_id = registration_id_from_display_name(name)
        if registration_id is not None:
            owners.setdefault(registration_id, set()).add(member_id)
    return {
        registration_id: next(iter(member_ids))
        for registration_id, member_ids in owners.items()
        if len(member_ids) == 1
    }


def discord_user_id_for_player(
    source_message: discord.Message,
    player: dict,
) -> Optional[int]:
    """Resolve a roster player whose raw card line holds `#ID` and a mention."""
    raw_text = plain_message_text(source_message)
    registration_id = int(player.get("id") or 0)
    if registration_id > 0:
        patterns = (
            rf"(?im)^\s*(?:[-•]\s*)?#?\s*{registration_id}\b[^\n]*?<@!?(\d{{15,22}})>",
            # Status emoji may precede the ID: `❓ #124 | <@id>`.
            rf"(?im)^[^\n]*?(?<!\d)#\s*{registration_id}(?!\d)[^\n]*?<@!?(\d{{15,22}})>",
            # A bare number after a mention can be K/A/D (`<@id> — 12/3/4`),
            # so only an explicit `#ID` may follow the mention.
            rf"(?im)^\s*<@!?(\d{{15,22}})>[^\n]*?#\s*{registration_id}(?!\d)",
        )
        for pattern in patterns:
            found = re.search(pattern, raw_text)
            if found:
                return int(found.group(1))

    for nickname in player_identity_names(player):
        nickname_digits = re.sub(r"\D", "", nickname)
        if nickname_digits == nickname and 15 <= len(nickname_digits) <= 22:
            return int(nickname_digits)
    return None


def mentioned_user_id_by_name(
    source_message: discord.Message,
    player: dict,
) -> Optional[int]:
    """Fuzzy-match a player's card nickname to the users mentioned in a card."""
    for nickname in player_identity_names(player):
        best_user_id: Optional[int] = None
        best_score = 0.0
        for user in mentioned_users(source_message):
            user_id = getattr(user, "id", None)
            if not isinstance(user_id, int):
                continue
            names = {
                str(getattr(user, "display_name", "") or ""),
                str(getattr(user, "global_name", "") or ""),
                str(getattr(user, "name", "") or ""),
            }
            score = max((nickname_similarity(nickname, name) for name in names), default=0.0)
            if score > best_score:
                best_score = score
                best_user_id = user_id
        if best_score >= 0.72:
            return best_user_id
    return None


def card_roster_discord_ids(
    source_message: discord.Message,
    display_names: Optional[dict[int, str]] = None,
) -> dict[str, list[Optional[int]]]:
    """Read Discord IDs while preserving every Team A/B roster position."""
    raw_text = plain_message_text(source_message)
    header_a = re.search(r"(?:Команда|Team)\s*A[^\n]*", raw_text, re.I)
    header_b = re.search(r"(?:Команда|Team)\s*B[^\n]*", raw_text, re.I)
    if not header_a or not header_b or header_b.start() <= header_a.start():
        return {"team_a": [], "team_b": []}
    names = display_names or {}

    def mention_ids(section: str) -> list[Optional[int]]:
        collected: list[Optional[int]] = []
        for line in section.splitlines():
            # Preserve one slot per actual roster line. Previously plain-name
            # rows were omitted, shifting all later mentions to the wrong
            # players and sometimes producing @неизвестный-пользователь.
            without_mentions = re.sub(r"<@!?\d{15,22}>", " ", line)
            mention = re.search(r"<@!?(\d{15,22})>", line)
            mention_id = int(mention.group(1)) if mention else None
            # `❓ <@id>` carries its registration number only inside the
            # member name (`#124 | Nick`); it is still a roster slot.
            # Service pings such as @Система have no number and stay ignored.
            has_short_id = bool(
                re.search(r"(?<!\d)#?\s*\d{1,5}(?!\d)", without_mentions)
            )
            has_named_id = (
                mention_id is not None
                and registration_id_from_display_name(names.get(mention_id, ""))
                is not None
            )
            if not has_short_id and not has_named_id:
                continue
            collected.append(mention_id)
            if len(collected) == 5:
                break
        # A partial roster cannot prove positions: one unreadable line would
        # move every later tag to another player.
        return collected if len(collected) == 5 else []

    return {
        "team_a": mention_ids(raw_text[header_a.end():header_b.start()]),
        "team_b": mention_ids(raw_text[header_b.end():]),
    }


def member_has_pro_league_role(member: object) -> bool:
    configured_names = {
        normalize_nickname(role_name)
        for role_name in PRO_LEAGUE_ROLE_NAMES
    }
    for role in getattr(member, "roles", None) or []:
        role_name = normalize_nickname(getattr(role, "name", ""))
        if role_name and any(
            configured and configured in role_name
            for configured in configured_names
        ):
            return True
    return False


def member_has_warning_eligible_league_role(member: object) -> bool:
    """Return True only for Prospect/Division members eligible for warnings."""
    for role in getattr(member, "roles", None) or []:
        role_name = normalize_nickname(getattr(role, "name", ""))
        if role_name and any(
            normalize_nickname(fragment) in role_name
            for fragment in WARNING_ELIGIBLE_ROLE_FRAGMENTS
        ):
            return True
    return False


async def query_warning_members(
    source_message: discord.Message,
    warning_channel,
    player: dict,
) -> list[object]:
    """Ask Discord for uncached members so a plain nickname can be tagged."""
    registration_id = int(player.get("id") or 0)
    values: list[str] = []
    if registration_id > 0:
        # League nicknames start with the registration number: `#124 | Nick`.
        # It is the most exact query, so it goes first.
        values.extend((f"#{registration_id}", str(registration_id)))
    for name in player_identity_names(player):
        nickname = strip_leading_clan_tags(name).strip()
        values.extend((nickname, re.sub(r"^\d+|\d+$", "", nickname)))
    queries: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in queries:
            queries.append(value)

    found: dict[int, object] = {}
    for guild in warning_guilds(source_message, warning_channel):
        query_members = getattr(guild, "query_members", None)
        if not callable(query_members):
            continue
        for query in queries:
            try:
                members = await query_members(query=query, limit=100, cache=True)
            except Exception:
                log.warning(
                    "Не удалось выполнить поиск участника %r на сервере %s",
                    query,
                    getattr(guild, "id", "?"),
                    exc_info=True,
                )
                continue
            for member in members or []:
                member_id = getattr(member, "id", None)
                if isinstance(member_id, int):
                    found[member_id] = member
    return list(found.values())


async def warning_member(
    user_id: int,
    source_message: discord.Message,
    warning_channel,
):
    """Find a member in either the warnings guild or source guild."""
    for user in mentioned_users(source_message):
        if getattr(user, "id", None) == user_id and getattr(user, "roles", None) is not None:
            return user

    guilds: list[object] = []
    for guild in (
        getattr(source_message, "guild", None),
        getattr(warning_channel, "guild", None),
    ):
        if guild is not None and guild not in guilds:
            guilds.append(guild)

    for guild in guilds:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
        if member is not None:
            return member
    return None


def warning_guilds(source_message: discord.Message, warning_channel) -> list[object]:
    """The card's league server and the warnings server — never other servers.

    Other shared servers have their own members with the same nicknames
    (`saiko`, `ender`) or `#number` names: that made the match ambiguous and
    left the warning untagged, or tagged a stranger.
    """
    guilds: list[object] = []
    for guild_id in source_guild_ids(source_message):
        guild = guild_by_id(guild_id, source_message)
        if guild is not None and guild not in guilds:
            guilds.append(guild)
    warning_guild = getattr(warning_channel, "guild", None)
    if warning_guild is not None and warning_guild not in guilds:
        guilds.append(warning_guild)
    return guilds


def warning_identity_candidates(
    source_message: discord.Message,
    warning_channel,
) -> list[object]:
    """Collect cached members so plain card nicknames can become real tags."""
    candidates: dict[int, object] = {}
    for user in mentioned_users(source_message):
        user_id = getattr(user, "id", None)
        if isinstance(user_id, int):
            candidates[user_id] = user
    for guild in warning_guilds(source_message, warning_channel):
        for member in getattr(guild, "members", None) or []:
            member_id = getattr(member, "id", None)
            if isinstance(member_id, int):
                # Keep the source-guild Member object first: that is where the
                # Pro League role shown in the player's profile usually lives.
                candidates.setdefault(member_id, member)
    return list(candidates.values())


async def resolve_warning_identity(
    source_message: discord.Message,
    player: dict,
    warning_channel,
    candidates: list[object],
    ordered_user_id: Optional[int] = None,
    registration_user_id: Optional[int] = None,
) -> tuple[Optional[int], Optional[object]]:
    """Resolve a roster player to the Discord account that must be tagged.

    Exact card data wins: the mention whose name carries this registration ID,
    then `#ID` and a mention on one raw line, then the roster position. Only
    after that are names compared, and always the card nickname first,
    because on a nickname mismatch the screenshot name (`csn`) belongs to no
    Discord profile.
    """
    user_id = registration_user_id
    if user_id is None:
        user_id = discord_user_id_for_player(source_message, player)
    if user_id is None:
        user_id = ordered_user_id
    if user_id is None:
        user_id = mentioned_user_id_by_name(source_message, player)
    if user_id is not None:
        member = await warning_member(
            user_id,
            source_message,
            warning_channel,
        )
        return user_id, member

    registration_id = int(player.get("id") or 0)

    def owners_of_registration_id(pool: list[object]) -> dict[int, object]:
        """Members whose server name carries this player's `#ID`."""
        owners: dict[int, object] = {}
        for member in pool:
            member_id = getattr(member, "id", None)
            if not isinstance(member_id, int):
                continue
            for name in (
                getattr(member, "display_name", None),
                getattr(member, "nick", None),
            ):
                if name and registration_id_from_display_name(str(name)) == registration_id:
                    owners[member_id] = member
        return owners

    fetched: Optional[list[object]] = None
    owners: dict[int, object] = {}
    if registration_id > 0:
        owners = owners_of_registration_id(candidates)
        if not owners:
            fetched = await query_warning_members(
                source_message,
                warning_channel,
                player,
            )
            owners = owners_of_registration_id([*candidates, *fetched])
        if len(owners) == 1:
            return next(iter(owners.items()))

    def rank(pool: list[object], nickname: str) -> list[tuple[float, int, object]]:
        ranked: list[tuple[float, int, object]] = []
        seen: set[int] = set()
        for member in pool:
            member_id = getattr(member, "id", None)
            if not isinstance(member_id, int) or member_id in seen:
                continue
            seen.add(member_id)
            names = {
                str(getattr(member, "display_name", "") or ""),
                str(getattr(member, "nick", "") or ""),
                str(getattr(member, "global_name", "") or ""),
                str(getattr(member, "name", "") or ""),
            }
            score = max(
                (nickname_similarity(nickname, name) for name in names),
                default=0.0,
            )
            ranked.append((score, member_id, member))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked

    for nickname in player_identity_names(player) or [""]:
        if len(owners) > 1:
            # Several members show this `#ID` (a stale nickname): the card
            # nickname decides between them only.
            ranked = rank(list(owners.values()), nickname)
            if ranked and ranked[0][0] >= 0.90 and (
                len(ranked) == 1 or ranked[1][0] < ranked[0][0] - 0.04
            ):
                return ranked[0][1], ranked[0][2]
            continue
        ranked = rank(candidates, nickname)
        if not ranked or ranked[0][0] < 0.90:
            # Large servers do not always cache every member. Query Discord
            # before falling back to a plain nickname, otherwise the warning
            # cannot tag the user or safely check that user's league roles.
            if fetched is None:
                fetched = await query_warning_members(
                    source_message,
                    warning_channel,
                    player,
                )
            ranked = rank([*candidates, *fetched], nickname)

        if not ranked or ranked[0][0] < 0.90:
            continue
        if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.04:
            log.warning(
                "Неоднозначный Discord-профиль для #%s %s: %.3f и %.3f",
                registration_id,
                nickname,
                ranked[0][0],
                ranked[1][0],
            )
            return None, None
        return ranked[0][1], ranked[0][2]
    return None, None


async def send_warning_with_screenshot(
    channel,
    warning_text: str,
    match_id: int,
    source_images: list[bytes],
    source_urls: list[str],
) -> None:
    """Post the mention/reason and original screenshot in one Discord message."""
    files: list[discord.File] = []
    for index, raw_image in enumerate(source_images, 1):
        extension = "png"
        try:
            with Image.open(io.BytesIO(raw_image)) as image:
                extension = {
                    "JPEG": "jpg",
                    "JPG": "jpg",
                    "WEBP": "webp",
                    "GIF": "gif",
                }.get(str(image.format or "").upper(), "png")
        except Exception:
            pass
        files.append(
            discord.File(
                io.BytesIO(raw_image),
                filename=f"match-{match_id}-warning-{index}.{extension}",
            )
        )

    if files:
        try:
            await channel.send(warning_text, files=files)
            return
        except Exception:
            log.warning(
                "Не удалось приложить скрин матча #%s к варну; "
                "отправляются ссылки на изображения",
                match_id,
                exc_info=True,
            )

    fallback = "\n".join([warning_text, *source_urls[:4]])
    await channel.send(fallback[:2000])


_background_tasks: set[asyncio.Task] = set()


def run_in_background(coroutine) -> None:
    """Keep a reference to fire-and-forget work until it finishes."""
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def notify_log_channel(text: str) -> None:
    """Post a short service notice to LOG_CHANNEL_ID; never raises."""
    if not LOG_CHANNEL_ID:
        return
    try:
        channel = client.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(LOG_CHANNEL_ID)
        await channel.send(text[:2000])
    except Exception:
        log.exception(
            "Не удалось отправить уведомление в лог-канал %s",
            LOG_CHANNEL_ID,
        )


async def download_warning_images(
    result: dict,
    source_message: discord.Message,
) -> tuple[list[bytes], list[str]]:
    """Download the card screenshot for warnings while the card still exists."""
    source_urls = image_urls(source_message)[:4]
    source_images: list[bytes] = []
    has_warnings = any(
        player.get("warning_reason") in WARNING_REASON_LABELS
        for player in [*result.get("team_a", []), *result.get("team_b", [])]
    )
    if not source_urls or not WARN_CHANNEL_ID or not has_warnings:
        return source_images, source_urls
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for source_url in source_urls:
                try:
                    source_images.append(await download_image(session, source_url))
                except Exception:
                    log.warning(
                        "Не удалось скачать изображение матча #%s для варна",
                        result.get("match_id"),
                        exc_info=True,
                    )
    except Exception:
        log.exception(
            "Не удалось подготовить изображения варнов матча #%s",
            result.get("match_id"),
        )
    return source_images, source_urls


async def send_zero_stat_warnings(
    result: dict,
    source_message: discord.Message,
    prepared_images: Optional[tuple[list[bytes], list[str]]] = None,
) -> None:
    """Warn tagged non-Pro-League players who received an unmatched 0/0/13."""
    if not WARN_CHANNEL_ID:
        log.warning(
            "Автоварны отключены: WARN_CHANNEL_ID не указан (матч #%s)",
            result.get("match_id"),
        )
        return

    warning_players: list[tuple[str, int, dict]] = []
    for team_key in ("team_a", "team_b"):
        for player_index, player in enumerate(result.get(team_key, [])):
            if player.get("warning_reason") in {
                "неправильный ник",
                "нет на скриншоте",
                "додж статистики",
            }:
                warning_players.append((team_key, player_index, player))
    if not warning_players:
        return

    try:
        warning_channel = client.get_channel(WARN_CHANNEL_ID)
        if warning_channel is None:
            warning_channel = await client.fetch_channel(WARN_CHANNEL_ID)

        warning_log_channel = None
        if LOG_CHANNEL_ID and LOG_CHANNEL_ID != WARN_CHANNEL_ID:
            try:
                warning_log_channel = client.get_channel(LOG_CHANNEL_ID)
                if warning_log_channel is None:
                    warning_log_channel = await client.fetch_channel(LOG_CHANNEL_ID)
            except Exception:
                log.exception(
                    "Не удалось открыть лог-канал %s для записи автоварнов",
                    LOG_CHANNEL_ID,
                )

        if prepared_images is None:
            prepared_images = await download_warning_images(
                result,
                source_message,
            )
        source_images, source_urls = prepared_images

        identity_candidates = warning_identity_candidates(
            source_message,
            warning_channel,
        )
        mention_names = await mention_display_names(
            plain_message_text(source_message),
            source_message,
        )
        registration_user_ids = registration_user_ids_from_names(mention_names)
        roster_discord_ids = card_roster_discord_ids(
            source_message,
            mention_names,
        )
        log.info(
            "Матч #%s: кандидатов на варн %s (%s)",
            result.get("match_id"),
            len(warning_players),
            ", ".join(
                f"#{player.get('id')} {player.get('warning_reason')}"
                for _, _, player in warning_players
            ),
        )
        sent_warnings = 0
        for team_key, player_index, player in warning_players:
            ordered_ids = roster_discord_ids.get(team_key, [])
            ordered_user_id = (
                ordered_ids[player_index]
                if player_index < len(ordered_ids)
                else None
            )
            player_registration_id = int(player.get("id") or 0)
            if ordered_user_id is not None and registration_id_from_display_name(
                mention_names.get(ordered_user_id, "")
            ) not in (None, player_registration_id):
                # The mention at this position belongs to another roster ID.
                ordered_user_id = None
            user_id, member = await resolve_warning_identity(
                source_message,
                player,
                warning_channel,
                identity_candidates,
                ordered_user_id,
                registration_user_ids.get(player_registration_id),
            )
            if user_id is not None and user_id in PRO_LEAGUE_USER_IDS:
                log.info(
                    "Варн матча #%s пропущен для Pro League пользователя %s",
                    result.get("match_id"),
                    user_id,
                )
                continue

            if member is not None and member_has_pro_league_role(member):
                log.info(
                    "Варн матча #%s пропущен: у %s есть роль Pro League",
                    result.get("match_id"),
                    user_id,
                )
                continue

            if member is not None and not member_has_warning_eligible_league_role(member):
                log.info(
                    "Варн матча #%s пропущен для %s: нет роли Prospect/Division",
                    result.get("match_id"),
                    user_id,
                )
                continue

            identity_names = player_identity_names(player)
            if user_id is None:
                log.warning(
                    "Матч #%s: Discord-профиль игрока #%s (%s) не найден, "
                    "варн будет без тега",
                    result.get("match_id"),
                    player.get("id"),
                    " / ".join(identity_names) or "без ника",
                )
            target = (
                f"<@{user_id}>"
                if user_id is not None
                else f"`{identity_names[0] if identity_names else 'ник не найден'}`"
            )
            reason_text = WARNING_REASON_LABELS.get(
                player["warning_reason"],
                "Обнуление игровой статистики",
            )
            warning_text = f"{target}\n{reason_text} - #{result['match_id']}"

            try:
                await send_warning_with_screenshot(
                    warning_channel,
                    warning_text,
                    int(result["match_id"]),
                    source_images,
                    source_urls,
                )
            except Exception:
                # One failed warning must never cancel the remaining players.
                log.exception(
                    "Не удалось отправить автоварн матча #%s игроку #%s",
                    result.get("match_id"),
                    player.get("id"),
                )
                continue
            sent_warnings += 1
            if warning_log_channel is not None:
                try:
                    await send_warning_with_screenshot(
                        warning_log_channel,
                        warning_text,
                        int(result["match_id"]),
                        source_images,
                        source_urls,
                    )
                except Exception:
                    log.exception(
                        "Не удалось записать автоварн матча #%s в лог-канал %s",
                        result.get("match_id"),
                        LOG_CHANNEL_ID,
                    )
        log.info(
            "Матч #%s: отправлено варнов %s из %s",
            result.get("match_id"),
            sent_warnings,
            len(warning_players),
        )
    except Exception:
        log.exception(
            "Не удалось отправить автоварн матча #%s в канал %s",
            result.get("match_id"),
            WARN_CHANNEL_ID,
        )


async def delete_duplicate_match_cards(
    source_message: discord.Message,
    match_id: int,
    keep_current: bool = False,
) -> int:
    """Delete duplicate source cards for one match from this channel.

    Only image result cards with the exact `Результат матча #N` title are
    touched. Registration commands and confirmation messages are preserved.
    """
    candidates: dict[int, discord.Message] = {source_message.id: source_message}
    try:
        async for candidate in source_message.channel.history(limit=BACKFILL_LIMIT):
            candidates[candidate.id] = candidate
    except Exception:
        log.exception(
            "Не удалось просмотреть канал для удаления дублей матча #%s",
            match_id,
        )

    deleted = 0
    forbidden = False
    for candidate in candidates.values():
        if keep_current and candidate.id == source_message.id:
            continue
        if not image_urls(candidate):
            continue
        text = plain_message_text(candidate)
        found = re.search(r"Результат\s+матча\s*#\s*(\d+)", text, re.I)
        if not found or int(found.group(1)) != int(match_id):
            continue
        try:
            await candidate.delete()
            deleted += 1
        except discord.NotFound:
            pass
        except discord.Forbidden:
            forbidden = True
            log.warning(
                "Нет права удалить дубль сообщения %s матча #%s",
                candidate.id,
                match_id,
            )
        except Exception:
            log.exception(
                "Не удалось удалить дубль сообщения %s матча #%s",
                candidate.id,
                match_id,
            )
    if forbidden:
        await notify_log_channel(
            f"⚠️ Матч #{match_id}: нет права удалить карточку в "
            f"<#{source_message.channel.id}> — аккаунту нужно право "
            "«Управлять сообщениями»."
        )
    return deleted


def normalize_reply_text(text: str) -> str:
    """Drop markdown, custom emoji and invisible marks: `✅️ **Готово**`."""
    text = re.sub(r"<a?:\w+:\d+>", " ", str(text))
    text = re.sub(r"[\ufe0e\ufe0f\u200b-\u200d\u2060]", "", text)
    return re.sub(r"[*_~`|>]+", "", text)


def closed_match_id(text: str) -> Optional[int]:
    """Match number of a `Готово — Матч #N закрыт со счётом X:Y` message.

    «Готово» may stand anywhere and in any formatting; the former check wanted
    it at the start of a line right after a plain ✅, so `✅ **Готово**` or a
    custom emoji made `удалить рег соо` find nothing.
    """
    normalized = normalize_reply_text(text)
    if not re.search(r"(?<!\w)готово(?!\w)", normalized, re.I):
        return None
    found = re.search(
        r"Матч\s*#\s*(\d+)\s+закрыт\s+со\s+сч[её]том\s+"
        r"\d{1,2}\s*[:\-–—]\s*\d{1,2}",
        normalized,
        re.I,
    )
    return int(found.group(1)) if found else None


def is_registration_success_confirmation(message: discord.Message) -> bool:
    """Detect only `Готово — Матч #N закрыт со счётом X:Y` messages."""
    return closed_match_id(registration_response_text(message)) is not None


async def channel_history_texts(channel):
    """Yield (message id, author id, full text) for a channel's whole history."""
    logs_from = getattr(getattr(client, "http", None), "logs_from", None)
    page = None
    if callable(logs_from):
        try:
            page = await logs_from(channel.id, 100)
        except Exception:
            log.debug("logs_from недоступен, читаю историю канала", exc_info=True)
            page = None
    if isinstance(page, list):
        while page:
            for payload in page:
                row = raw_message_row(payload)
                if row is not None:
                    yield row[0], row[1], row[2]
            if len(page) < 100:
                return
            page = await logs_from(channel.id, 100, before=page[-1]["id"])
        return
    async for candidate in channel.history(limit=None):
        yield (
            int(candidate.id),
            getattr(getattr(candidate, "author", None), "id", None),
            registration_response_text(candidate),
        )


async def delete_all_registration_confirmations(
    channel_ids: set[int],
) -> tuple[int, int, int, list[str]]:
    """Delete all successful registration confirmations in configured channels.

    Returns deleted, scanned and failed channel counts, plus samples of
    «Готово»-like messages that were not recognized, so Discord shows why
    nothing was deleted.
    """
    deleted = 0
    scanned_channels = 0
    failed_channels = 0
    unmatched: list[str] = []
    own_id = getattr(getattr(client, "user", None), "id", None)
    for channel_id in sorted(channel_ids):
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            scanned_channels += 1
            async for message_id, author_id, text in channel_history_texts(channel):
                if own_id and author_id == own_id:
                    continue
                if closed_match_id(text) is None:
                    if (
                        len(unmatched) < 2
                        and not is_result_card_text(text)
                        and re.search(r"готово|закрыт\s+со", text, re.I)
                    ):
                        unmatched.append(text[:300])
                    continue
                try:
                    await delete_channel_message(channel, message_id)
                    deleted += 1
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    log.warning(
                        "Нет права удалить подтверждение %s в канале %s",
                        message_id,
                        channel_id,
                    )
                except Exception:
                    log.exception(
                        "Не удалось удалить подтверждение %s в канале %s",
                        message_id,
                        channel_id,
                    )
        except Exception:
            failed_channels += 1
            log.exception(
                "Не удалось очистить подтверждения в канале %s",
                channel_id,
            )
    return deleted, scanned_channels, failed_channels, unmatched


def registration_response_text(message: object) -> str:
    """Every visible text of a reply: content, embeds and message components.

    plain_message_text() reads only content, embed title, description and
    fields. The registration bot can put «✅ Готово» into the embed author or
    footer, or into text components, which made a real confirmation look like
    a timeout: =g was removed while the card and «Готово» stayed.
    """
    chunks = [plain_message_text(message)]
    for part in message_parts(message):
        for embed in getattr(part, "embeds", None) or []:
            for value in (
                getattr(getattr(embed, "author", None), "name", None),
                getattr(getattr(embed, "footer", None), "text", None),
            ):
                if isinstance(value, str) and value:
                    chunks.append(value)
        components = getattr(part, "components", None)
        stack = list(components) if isinstance(components, (list, tuple)) else []
        inspected = 0
        while stack and inspected < 200:
            component = stack.pop(0)
            inspected += 1
            for attribute in ("content", "text"):
                value = getattr(component, attribute, None)
                if isinstance(value, str) and value:
                    chunks.append(value)
            for attribute in ("children", "components", "items"):
                children = getattr(component, attribute, None)
                if isinstance(children, (list, tuple)):
                    stack.extend(children)
    return "\n".join(chunks)


def raw_message_text(data: object, depth: int = 0) -> str:
    """Flatten the strings of a raw message payload, reply target excluded."""
    if depth > 8:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return "\n".join(
            raw_message_text(value, depth + 1)
            for key, value in data.items()
            if key not in {
                "referenced_message", "author", "member", "mentions",
                "id", "channel_id", "guild_id", "url", "proxy_url",
                "icon_url", "proxy_icon_url",
            }
        )
    if isinstance(data, list):
        return "\n".join(raw_message_text(item, depth + 1) for item in data)
    return ""


def registration_response_verdict(
    text: str,
    match_id: int,
    references_command: bool,
) -> Optional[bool]:
    """True for «Готово», False for «Не вышло», None for unrelated messages."""
    text = normalize_reply_text(text)
    lowered = text.lower()
    if "не вышло" in lowered:
        verdict = False
    elif "готово" in lowered or re.search(
        r"матч\s*#\s*\d+\s+закрыт\s+со\s+сч[её]том",
        lowered,
    ):
        verdict = True
    else:
        return None
    # `#348` must not accept a reply about `#3480`.
    if references_command or re.search(rf"#\s*{match_id}(?!\d)", text):
        return verdict
    return None


async def delete_channel_message(channel, message_id: int) -> None:
    """Delete a message known only by its ID."""
    get_partial_message = getattr(channel, "get_partial_message", None)
    if callable(get_partial_message):
        await get_partial_message(message_id).delete()
        return
    await client.http.delete_message(channel.id, message_id)


DISCORD_EPOCH_MS = 1420070400000


def snowflake_seconds_ago(seconds: float) -> int:
    """Smallest Discord message ID that can be created `seconds` ago or later."""
    milliseconds = int(datetime.now(timezone.utc).timestamp() * 1000 - seconds * 1000)
    return max(0, milliseconds - DISCORD_EPOCH_MS) << 22


def is_result_card_text(text: str) -> bool:
    return bool(re.search(r"Результат\s+матча\s*#\s*\d+", text, re.I))


def raw_message_row(
    payload: object,
) -> Optional[tuple[int, Optional[int], str, Optional[int]]]:
    """(id, author id, full text, replied message id) of a raw payload."""
    if not isinstance(payload, dict):
        return None
    message_id = str(payload.get("id") or "")
    author_id = str((payload.get("author") or {}).get("id") or "")
    reference_id = str(
        (payload.get("message_reference") or {}).get("message_id") or ""
    )
    if not message_id.isdigit():
        return None
    return (
        int(message_id),
        int(author_id) if author_id.isdigit() else None,
        raw_message_text(payload),
        int(reference_id) if reference_id.isdigit() else None,
    )


async def channel_messages_after(
    channel,
    after_message: discord.Message,
    limit: int,
) -> list[tuple[int, Optional[int], str, Optional[int]]]:
    """(id, author id, full text, replied message id) of newer messages.

    Raw payloads are preferred: they keep every text field even when the
    library cannot parse a new message layout.
    """
    logs_from = getattr(getattr(client, "http", None), "logs_from", None)
    if callable(logs_from):
        try:
            payloads = await logs_from(channel.id, limit, after=after_message.id)
        except Exception:
            log.debug("logs_from недоступен, читаю историю канала", exc_info=True)
            payloads = None
        if isinstance(payloads, list):
            return [
                row for row in map(raw_message_row, payloads) if row is not None
            ]

    rows = []
    async for candidate in channel.history(limit=limit, after=after_message):
        reference = getattr(candidate, "reference", None)
        rows.append((
            int(candidate.id),
            getattr(getattr(candidate, "author", None), "id", None),
            registration_response_text(candidate),
            getattr(reference, "message_id", None),
        ))
    return rows


def registration_row_verdict(
    row: tuple[int, Optional[int], str, Optional[int]],
    sent_registration: discord.Message,
    match_id: int,
) -> Optional[bool]:
    """Verdict for one message read after =g; self messages and cards excluded."""
    message_id, author_id, text, reference_id = row
    own_id = getattr(getattr(client, "user", None), "id", None)
    if own_id and author_id == own_id:
        return None
    if message_id <= int(sent_registration.id) or is_result_card_text(text):
        return None
    return registration_response_verdict(
        text,
        match_id,
        reference_id == int(sent_registration.id),
    )


async def find_registration_response(
    channel,
    sent_registration: discord.Message,
    match_id: int,
    seen: list[str],
) -> Optional[tuple[int, bool, str]]:
    """Re-read the channel after =g: catches edited or undelivered replies."""
    try:
        rows = await channel_messages_after(channel, sent_registration, 25)
    except Exception:
        log.warning(
            "Не удалось перечитать канал в ожидании ответа по матчу #%s",
            match_id,
            exc_info=True,
        )
        return None
    own_id = getattr(getattr(client, "user", None), "id", None)
    for row in rows:
        if not own_id or row[1] != own_id:
            seen.append(row[2][:200])
        verdict = registration_row_verdict(row, sent_registration, match_id)
        if verdict is not None:
            return row[0], verdict, row[2]
    return None


async def delete_match_confirmations(
    channel,
    match_id: int,
    sent_registration: discord.Message,
) -> int:
    """Delete every «Готово» for this match posted after our =g command."""
    try:
        rows = await channel_messages_after(channel, sent_registration, 50)
    except Exception:
        log.warning(
            "Матч #%s: не удалось перечитать канал для удаления «Готово»",
            match_id,
            exc_info=True,
        )
        return 0
    deleted = 0
    for row in rows:
        if registration_row_verdict(row, sent_registration, match_id) is not True:
            continue
        try:
            await delete_channel_message(channel, row[0])
            deleted += 1
            log.info("Матч #%s: удалено «Готово» (сообщение %s)", match_id, row[0])
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning("Нет права удалить «Готово» матча #%s", match_id)
            await notify_log_channel(
                f"⚠️ Матч #{match_id}: нет права удалить «Готово» в "
                f"<#{channel.id}> — аккаунту нужно право «Управлять сообщениями»."
            )
            break
        except Exception:
            log.exception(
                "Не удалось удалить «Готово» %s матча #%s",
                row[0],
                match_id,
            )
    return deleted


async def delete_match_confirmations_later(
    channel,
    match_id: int,
    sent_registration: discord.Message,
    delay: float = 15.0,
) -> None:
    """Repeat the sweep for a «Готово» that arrives after cleanup."""
    await asyncio.sleep(delay)
    await delete_match_confirmations(channel, match_id, sent_registration)


async def delete_confirmation_when_registered(message: discord.Message) -> None:
    """Safety net: remove «Готово — Матч #N закрыт» once this bot registered N.

    Covers replies that the =g wait did not bind to, such as a second
    confirmation message. Only matches in registration history are touched,
    so confirmations of games registered by people stay in the channel.
    """
    channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
    if channel_id not in NORMAL_CHANNEL_IDS | PRIORITY_CHANNEL_IDS:
        return
    author_id = getattr(getattr(message, "author", None), "id", None)
    if client.user and author_id == client.user.id:
        return
    match_id = closed_match_id(registration_response_text(message))
    if match_id is None:
        return

    # Let a running registration read this reply before it disappears.
    await asyncio.sleep(3.0)
    for _ in range(90):
        if match_id not in processing_match_ids:
            break
        await asyncio.sleep(2.0)
    if not await registration_exists(match_id):
        return
    try:
        await message.delete()
        log.info(
            "Матч #%s: удалено «Готово» %s (страховочная очистка)",
            match_id,
            message.id,
        )
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.warning("Нет права удалить «Готово» матча #%s", match_id)
    except Exception:
        log.exception("Не удалось удалить «Готово» матча #%s", match_id)


def is_already_registered_reply(text: str) -> bool:
    """«Не вышло — Результат этого матча уже внесён» and similar replies."""
    return bool(
        re.search(
            r"уже\s+(?:был[аио]?\s+)?(?:внес[её]н|зарегистрирован|закрыт)",
            normalize_reply_text(text),
            re.I,
        )
    )


async def send_registration_and_wait(
    channel,
    command_text: str,
    match_id: int,
) -> tuple[discord.Message, bool, str]:
    """Send =g and wait for the game bot's «Готово» or «Не вышло» reply.

    Listeners are armed before the command is sent, so a fast reply cannot
    slip in between. Edited replies arrive through message_edit, and the
    channel is re-read in case the gateway did not deliver the reply at all.
    A confirmed «Готово» is deleted right away; «Не вышло» is kept.
    """
    loop = asyncio.get_running_loop()
    sent_ids: list[int] = []
    earliest_reply_id = snowflake_seconds_ago(10.0)

    def reply_verdict(candidate: object) -> Optional[bool]:
        if getattr(getattr(candidate, "channel", None), "id", None) != channel.id:
            return None
        author_id = getattr(getattr(candidate, "author", None), "id", None)
        if client.user and author_id == client.user.id:
            return None
        # A reply is always newer than =g. The result card, edited by the
        # tournament bot after the match is closed, must never be taken for
        # «Готово»: that deleted the card and left the real reply behind.
        if int(getattr(candidate, "id", 0) or 0) < earliest_reply_id:
            return None
        text = registration_response_text(candidate)
        if is_result_card_text(text):
            return None
        reference = getattr(candidate, "reference", None)
        return registration_response_verdict(
            text,
            match_id,
            bool(
                sent_ids
                and reference
                and getattr(reference, "message_id", None) == sent_ids[0]
            ),
        )

    waiters = [
        asyncio.ensure_future(client.wait_for(
            "message",
            check=lambda after: reply_verdict(after) is not None,
        )),
        asyncio.ensure_future(client.wait_for(
            "message_edit",
            check=lambda before, after: reply_verdict(after) is not None,
        )),
    ]
    await asyncio.sleep(0)
    seen: list[str] = []
    response: Optional[tuple[Optional[object], int, bool, str]] = None
    try:
        sent_registration = await channel.send(command_text)
        sent_ids.append(sent_registration.id)
        deadline = loop.time() + REGISTRATION_CONFIRM_TIMEOUT
        next_history_check = loop.time() + 4.0
        while response is None:
            for waiter in waiters:
                if not waiter.done() or waiter.cancelled() or waiter.exception():
                    continue
                value = waiter.result()
                candidate = value[-1] if isinstance(value, tuple) else value
                verdict = reply_verdict(candidate)
                if verdict is not None:
                    response = (
                        candidate,
                        int(candidate.id),
                        verdict,
                        registration_response_text(candidate),
                    )
                    break
            if response is not None:
                break

            now = loop.time()
            if now >= next_history_check or now >= deadline:
                found = await find_registration_response(
                    channel,
                    sent_registration,
                    match_id,
                    seen,
                )
                if found is not None:
                    response = (None, *found)
                    break
                if now >= deadline:
                    log.warning(
                        "Матч #%s: ответ регистрационного бота не найден. "
                        "Сообщения после команды: %r",
                        match_id,
                        seen[-10:],
                    )
                    return (
                        sent_registration,
                        False,
                        "нет ответа за "
                        f"{REGISTRATION_CONFIRM_TIMEOUT:g} с. Сообщения после "
                        "команды: "
                        + (" | ".join(seen[-3:]) if seen else "нет"),
                    )
                next_history_check = loop.time() + 4.0

            pending = [waiter for waiter in waiters if not waiter.done()]
            wait_time = max(0.05, min(1.0, deadline - loop.time()))
            if pending:
                await asyncio.wait(
                    pending,
                    timeout=wait_time,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(wait_time)
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
            elif not waiter.cancelled():
                waiter.exception()

    candidate, response_id, confirmed, response_text = response
    log.info(
        "Матч #%s: ответ регистрационного бота — %s",
        match_id,
        "Готово" if confirmed else "Не вышло",
    )
    if confirmed:
        # Remove the registration bot's visible `Готово` card after we have
        # read it. Failure responses are kept for diagnostics and retry.
        try:
            if candidate is not None and callable(getattr(candidate, "delete", None)):
                await candidate.delete()
            else:
                await delete_channel_message(channel, response_id)
            log.info(
                "Матч #%s: удалено «Готово» (сообщение %s)",
                match_id,
                response_id,
            )
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning(
                "Нет права удалить подтверждение регистрации матча #%s",
                match_id,
            )
            await notify_log_channel(
                f"⚠️ Матч #{match_id}: нет права удалить «Готово» в "
                f"<#{channel.id}> — аккаунту нужно право «Управлять сообщениями»."
            )
        except Exception:
            log.exception(
                "Не удалось удалить подтверждение регистрации матча #%s",
                match_id,
            )
    elif is_already_registered_reply(response_text):
        # The match was entered earlier (by hand or by another run). The
        # reply is clutter like «Готово» and leaves together with the card.
        try:
            if candidate is not None and callable(getattr(candidate, "delete", None)):
                await candidate.delete()
            else:
                await delete_channel_message(channel, response_id)
            log.info(
                "Матч #%s: удалён ответ «уже внесён» (сообщение %s)",
                match_id,
                response_id,
            )
        except discord.NotFound:
            pass
        except Exception:
            log.warning(
                "Не удалось удалить ответ «уже внесён» матча #%s",
                match_id,
                exc_info=True,
            )
    return sent_registration, confirmed, response_text[:500]


async def delete_live_tab_card(
    message: discord.Message,
    match_id: Optional[int],
) -> None:
    """Copy a rejected live-TAB card to logs, then delete it from the channel."""
    if LOG_CHANNEL_ID:
        try:
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if log_channel is None:
                log_channel = await client.fetch_channel(LOG_CHANNEL_ID)
            await send_original_card_to_log(
                message,
                log_channel,
                int(match_id or 0),
                fallback_title=f"🖼 Карточка игры #{match_id or '?'} с открытым TAB",
            )
        except Exception:
            log.exception(
                "Не удалось скопировать TAB-карточку матча #%s в лог",
                match_id,
            )
    try:
        await message.delete()
        log.info("Матч #%s: карточка с открытым TAB удалена", match_id)
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.warning("Нет права удалить TAB-карточку матча #%s", match_id)
        await notify_log_channel(
            f"⚠️ Матч #{match_id or '?'}: нет права удалить карточку с открытым "
            f"TAB в <#{message.channel.id}> — аккаунту нужно право "
            "«Управлять сообщениями»."
        )
    except Exception:
        log.exception("Не удалось удалить TAB-карточку матча #%s", match_id)


@contextlib.asynccontextmanager
async def safe_typing(channel):
    """Show «печатает…» when Discord allows it; a refusal never stops a game."""
    indicator = None
    try:
        indicator = channel.typing()
        await indicator.__aenter__()
    except Exception as exc:
        indicator = None
        log.warning(
            "Не удалось показать «печатает…» в канале %s: %s: %s",
            getattr(channel, "id", "?"),
            type(exc).__name__,
            exc,
        )
    try:
        yield
    finally:
        if indicator is not None:
            with contextlib.suppress(Exception):
                await indicator.__aexit__(None, None, None)


async def process_upload(message: discord.Message, test_only: bool = False) -> None:
    urls = image_urls(message)
    if not urls:
        return

    context = await message_context(message)
    context_match = re.search(r"(?:матч|матча)\s*#\s*(\d+)", context, re.I)
    reserved_match_id = int(context_match.group(1)) if context_match else None
    if reserved_match_id is not None and not test_only:
        async with processing_match_lock:
            if reserved_match_id in processing_match_ids:
                log.info(
                    "Матч #%s уже обрабатывается — повторная карточка пропущена",
                    reserved_match_id,
                )
                # Do not mark this duplicate as permanently inspected. If the
                # active copy fails, the next `старт` may retry this one.
                processed_message_ids.discard(message.id)
                return
            processing_match_ids.add(reserved_match_id)

    processing_completed = False
    async with safe_typing(message.channel):
        try:
            # A match already in history (registered by this bot, or reported
            # «уже внесён» by the registration bot) needs no second =g: its
            # repeated cards are deleted without a request to the AI.
            if (
                not test_only
                and
                reserved_match_id is not None
                and await registration_exists(reserved_match_id)
            ):
                deleted_cards = await delete_duplicate_match_cards(
                    message,
                    reserved_match_id,
                    keep_current=not DELETE_SOURCE_AFTER_REGISTRATION,
                )
                log.info(
                    "Матч #%s уже есть в истории; удалено повторных карточек: %s.",
                    reserved_match_id,
                    deleted_cards,
                )
                processing_completed = True
                return

            # There are two independent registration formats:
            # 1) complete `внесён` cards already containing score and K/A/D;
            # 2) `на проверку` cards containing IDs/nicks plus a scoreboard.
            # A helper button may exist on both and must not choose the mode.
            has_players_button = find_get_players_button(message) is not None
            review_card = is_review_result_card(context)
            complete_card = parse_complete_card(context)
            score_hint = readable_score_from_context(context)
            log.info(
                "Матч #%s: complete_card=%s review_card=%s players_button=%s "
                "score_hint=%s | версия %s",
                reserved_match_id or "?",
                complete_card is not None,
                review_card,
                has_players_button,
                f"{score_hint[0]}:{score_hint[1]}" if score_hint else "не прочитан",
                BOT_VERSION,
            )
            modal_text: Optional[str] = None
            raw_images: list[bytes] = []

            async def discard_live_tab_card(audit: dict) -> bool:
                """Live TABs are now accepted just like normal final screens."""
                return False
                log.info(
                    "Матч #%s определён как открытый TAB, а не финальный экран.",
                    reserved_match_id or "?",
                )
                # Test previews are intentionally non-destructive and must
                # still return the generated =g command. Bypass only the final-
                # screen gate for this preview; real registration is unchanged.
                if test_only:
                    audit["is_scoreboard"] = True
                    audit["is_final_result"] = True
                    audit["has_live_gameplay_hud"] = False
                    audit["notes"] = (
                        str(audit.get("notes", ""))
                        + " Тестовый предпросмотр открытого TAB; в рабочем "
                        "канале такая карточка пропускается и удаляется."
                    ).strip()
                    return False
                # A card whose screenshot is an opened TAB is removed. The
                # reason, the model's reading and a copy of the card go to logs
                # first, so a wrongly rejected game can be registered by hand.
                log.info(
                    "Матч #%s пропущен как live TAB; карточка удаляется.",
                    reserved_match_id or "?",
                )
                await send_processing_error_log(
                    reserved_match_id or "?",
                    message,
                    "Скриншот распознан как открытый TAB во время игры, а не "
                    "финальный экран — игра не зарегистрирована, карточка "
                    "удалена (копия ниже).",
                    visual_audit_summary(audit),
                )
                await delete_live_tab_card(message, reserved_match_id)
                return True

            async def try_players_helper_fallback(audit: dict) -> Optional[dict]:
                """Use the helper only after proving this is a final result."""
                nonlocal modal_text
                # Never click «Получить игроков» for an opened live TAB. The
                # helper is only a fallback for weak OCR on a final screen.
                if (
                    not audit.get("is_scoreboard")
                    or not audit.get("is_final_result")
                    or audit.get("has_live_gameplay_hud")
                ):
                    log.info(
                        "Матч #%s: «Получить игроков» не нажата — финальный "
                        "экран не подтверждён.",
                        reserved_match_id or "?",
                    )
                    return None
                helper_message = message
                if (
                    is_forwarded_message(message)
                    and find_get_players_button(message) is None
                    and reserved_match_id is not None
                ):
                    original_message = await find_original_match_card(
                        reserved_match_id,
                        message,
                    )
                    if original_message is not None:
                        helper_message = original_message
                if find_get_players_button(helper_message) is None:
                    return None
                modal_text, _helper_image_urls = await get_players_response(
                    helper_message
                )
                if not modal_text:
                    return None
                helper_result = result_from_visual_audit(
                    context,
                    modal_text,
                    audit,
                )
                if helper_result is not None:
                    return helper_result
                return result_from_nonzero_modal_and_final_score(
                    context,
                    modal_text,
                    audit,
                )

            if complete_card is not None:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    raw_images = await download_card_images(session, message, urls[:4])
                # The original scoreboard is always the source of truth. A
                # generated card can contain not only fake 0/0/13 rows but an
                # incorrect score such as 0:13 while the screenshot says 9:13.
                audit = await recognize_match(
                    raw_images,
                    message_text=context,
                    visual_audit=True,
                )
                if await discard_live_tab_card(audit):
                    processing_completed = True
                    return
                result = result_from_card_and_visual_audit(context, audit)
                if result is None:
                    result = await try_players_helper_fallback(audit)
                if result is None:
                    diagnostics = (
                        full_match_diagnostics(context)
                        + "\n\n"
                        + visual_audit_summary(audit)
                    )
                    log.error(
                        "Матч #%s остановлен: карточка не прошла независимую "
                        "проверку по исходному скриншоту. audit=%r\n%s",
                        reserved_match_id or "?",
                        audit,
                        diagnostics,
                    )
                    await send_processing_error_log(
                        reserved_match_id or "?",
                        message,
                        "Карточка не прошла проверку счёта и игроков по скриншоту.",
                        diagnostics,
                    )
                    return
            elif review_card or has_players_button:
                card_rosters = parse_card_roster_identities(context)
                if card_rosters is not None:
                    # This format already contains all ten registration IDs.
                    # Do not click «Получить игроков»: read only score, sides
                    # and K/A/D from the original scoreboard screenshot.
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        raw_images = await download_card_images(session, message, urls[:4])
                    audit = await recognize_match(
                        raw_images,
                        message_text=context,
                        visual_audit=True,
                    )
                    if await discard_live_tab_card(audit):
                        processing_completed = True
                        return
                    result = result_from_card_and_visual_audit(context, audit)
                    if result is None:
                        result = await try_players_helper_fallback(audit)
                    if result is None:
                        diagnostics = (
                            full_match_diagnostics(context)
                            + "\n\n"
                            + visual_audit_summary(audit)
                        )
                        log.error(
                            "Матч #%s: не удалось сопоставить ID/ники карточки "
                            "с исходным табло. score_hint=%s context=%r "
                            "audit=%r.\n%s",
                            reserved_match_id or "?",
                            f"{score_hint[0]}:{score_hint[1]}" if score_hint else "не прочитан",
                            context[:4000],
                            audit,
                            diagnostics,
                        )
                        await send_processing_error_log(
                            reserved_match_id or "?", message,
                            "Не удалось сопоставить карточку с исходным табло.",
                            diagnostics,
                        )
                        return
                else:
                    # Keep support for old review cards whose IDs/statistics
                    # can be recovered only through the helper button.
                    # Inspect the screenshot FIRST: a live TAB card must be
                    # deleted without ever clicking «Получить игроков».
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        raw_images = await download_card_images(session, message, urls[:4])
                    audit = await recognize_match(
                        raw_images,
                        message_text=context,
                        visual_audit=True,
                    )
                    if await discard_live_tab_card(audit):
                        processing_completed = True
                        return

                    helper_message = message
                    if (
                        is_forwarded_message(message)
                        and find_get_players_button(message) is None
                        and reserved_match_id is not None
                    ):
                        original_message = await find_original_match_card(
                            reserved_match_id,
                            message,
                        )
                        if original_message is not None:
                            helper_message = original_message

                    modal_text, _helper_image_urls = await get_players_response(
                        helper_message
                    )
                    if not modal_text:
                        diagnostics = (
                            players_helper_error_text(reserved_match_id)
                            + full_match_diagnostics(context)
                        )
                        log.error(
                            "Матч #%s: не удалось открыть/прочитать «Получить игроков». "
                            "Прочитанный счёт=%s. context=%r\n%s",
                            reserved_match_id or "?",
                            f"{score_hint[0]}:{score_hint[1]}" if score_hint else "не прочитан",
                            context[:4000],
                            diagnostics,
                        )
                        await send_processing_error_log(
                            reserved_match_id or "?", message,
                            "Не удалось открыть/прочитать «Получить игроков».",
                            diagnostics,
                        )
                        return
                    # Even when the helper returns 0/0/0, the original
                    # screenshot remains authoritative. If that nickname is
                    # visible, use its real K/A/D and never emit fake 0/0/13.
                    result = result_from_review_card_and_modal(
                        context,
                        modal_text,
                        visual_audit=audit,
                    )
                    if result is None:
                        diagnostics = full_match_diagnostics(context, modal_text)
                        await send_processing_error_log(
                            reserved_match_id or "?", message,
                            "Не удалось сопоставить старую карточку с «Получить игроков».",
                            diagnostics,
                        )
                        return
            else:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    raw_images = await download_card_images(session, message, urls[:4])

                result = await recognize_match(raw_images, context)

            expected_ids = extract_short_player_ids(context, result.get("match_id"))
            returned_players = [
                *result.get("team_a", []),
                *result.get("team_b", []),
            ]
            returned_ids = [player.get("id") for player in returned_players]
            if len(expected_ids) == 10 and len(returned_ids) == 10:
                if set(returned_ids) != set(expected_ids):
                    diagnostics = full_match_diagnostics(context, modal_text if review_card else None, result)
                    log.error(
                        "Матч #%s пропущен: ID модели %s не совпали с карточкой %s\n%s",
                        result.get("match_id"),
                        returned_ids,
                        expected_ids,
                        diagnostics,
                    )
                    await send_processing_error_log(
                        result.get("match_id") or reserved_match_id or "?", message,
                        "ID результата не совпали с ID карточки.", diagnostics,
                    )
                    return
            positional_ids = [1, 2, 3, 4, 5, 5, 4, 3, 2, 1]
            if returned_ids == positional_ids or (
                len(returned_ids) == 10
                and all(isinstance(value, int) and 1 <= value <= 5 for value in returned_ids)
            ):
                diagnostics = full_match_diagnostics(context, modal_text if review_card else None, result)
                log.error(
                    "Матч #%s пропущен: модель выдумала позиционные ID %s\n%s",
                    result.get("match_id"),
                    returned_ids,
                    diagnostics,
                )
                await send_processing_error_log(
                    result.get("match_id") or reserved_match_id or "?", message,
                    "Получены выдуманные позиционные ID.", diagnostics,
                )
                return

            fatal = (
                not result.get("is_match_result")
                or result.get("match_id") is None
                or result.get("score_a") is None
                or result.get("score_b") is None
                or result.get("ct_team") not in ("A", "B")
                or len(result.get("team_a", [])) != 5
                or len(result.get("team_b", [])) != 5
            )
            if fatal:
                diagnostics = full_match_diagnostics(context, modal_text if review_card else None, result)
                log.error(
                    "Матч #%s: не удалось собрать структуру. Прочитанный счёт=%s:%s. result=%r Notes=%s\n%s",
                    result.get("match_id") or reserved_match_id or "?",
                    result.get("score_a"),
                    result.get("score_b"),
                    result,
                    result.get("notes", ""),
                    diagnostics,
                )
                await send_processing_error_log(
                    result.get("match_id") or reserved_match_id or "?", message,
                    "Не удалось собрать полную структуру матча.", diagnostics,
                )
                return

            confidence = float(result.get("overall_confidence", 0))
            if confidence < MIN_CONFIDENCE:
                diagnostics = full_match_diagnostics(context, modal_text if review_card else None, result)
                log.warning(
                    "Матч #%s распознан с низкой уверенностью %.2f. Прочитанный счёт=%s:%s. result=%r Notes=%s\n%s",
                    result.get("match_id") or reserved_match_id or "?",
                    confidence,
                    result.get("score_a"),
                    result.get("score_b"),
                    result,
                    result.get("notes", ""),
                    diagnostics,
                )
                await send_processing_error_log(
                    result.get("match_id") or reserved_match_id or "?", message,
                    f"Низкая уверенность распознавания: {confidence:.2f}.", diagnostics,
                )
                return

            all_players = [*result["team_a"], *result["team_b"]]
            player_ids = [player.get("id") for player in all_players]
            if (
                len(player_ids) != 10
                or any(not isinstance(value, int) or value <= 0 for value in player_ids)
                or len(set(player_ids)) != 10
            ):
                diagnostics = full_match_diagnostics(context, modal_text if review_card else None, result)
                log.error(
                    "Матч #%s не отправлен: недопустимые или повторяющиеся ID %s\n%s",
                    result.get("match_id"),
                    player_ids,
                    diagnostics,
                )
                await send_processing_error_log(
                    result.get("match_id") or reserved_match_id or "?", message,
                    "Недопустимые или повторяющиеся ID игроков.", diagnostics,
                )
                return

            # The screenshot stays the source of truth: a card player who is
            # printed on the final scoreboard must never be registered as
            # 0/0/13. Every row that is still zeroed is read one more time.
            zeroed_players = zeroed_result_players(result)
            if zeroed_players and raw_images:
                recovered_rows = await recover_zeroed_rows(
                    raw_images,
                    zeroed_players,
                )
                recovered_now = apply_recovered_rows(result, recovered_rows)
                if recovered_now:
                    result["notes"] = (
                        str(result.get("notes", ""))
                        + " Повторное чтение табло вернуло строки 0/0/13: "
                        + "; ".join(recovered_now) + "."
                    ).strip()
                    log.info(
                        "Матч #%s: статистика восстановлена со скриншота — %s",
                        result.get("match_id"),
                        "; ".join(recovered_now),
                    )
                else:
                    log.warning(
                        "Матч #%s: строки #%s остались 0/0/13 — на табло их "
                        "нет или повторное чтение не нашло их.",
                        result.get("match_id"),
                        ", #".join(
                            str(player.get("id")) for player in zeroed_players
                        ),
                    )
            newly_marked_warnings = mark_zero_stat_warning_reasons(result)
            if newly_marked_warnings:
                log.info(
                    "Матч #%s: дополнительно отмечено игроков 0/0/13 для варна: %s",
                    result.get("match_id"),
                    newly_marked_warnings,
                )
            command_text = format_registration(result)
            if test_only:
                # Forwarded cards outside registration channels are a safe
                # preview: return only the generated =g command. Do not save
                # stats, wait for confirmation or delete any source message.
                await message.channel.send(command_text)
                processing_completed = True
                return

            match_id = int(result["match_id"])
            if await registration_exists(match_id):
                deleted_cards = await delete_duplicate_match_cards(
                    message,
                    match_id,
                    keep_current=not DELETE_SOURCE_AFTER_REGISTRATION,
                )
                log.info(
                    "Матч #%s уже есть в истории; удалено повторных карточек: %s.",
                    match_id,
                    deleted_cards,
                )
                processing_completed = True
                return

            await asyncio.sleep(SEND_DELAY)
            sent_registration, confirmed, confirmation_text = (
                await send_registration_and_wait(
                    message.channel,
                    command_text,
                    match_id,
                )
            )
            if not confirmed and is_already_registered_reply(confirmation_text):
                log.info(
                    "Матч #%s уже внесён регистрационным ботом; карточка удаляется.",
                    match_id,
                )
                await record_registration(
                    match_id,
                    posted_at=getattr(message, "created_at", None),
                    kind="already",
                )
                with contextlib.suppress(Exception):
                    await sent_registration.delete()
                deleted_cards = await delete_duplicate_match_cards(
                    message,
                    match_id,
                    keep_current=not DELETE_SOURCE_AFTER_REGISTRATION,
                )
                await notify_log_channel(
                    f"🗑 Матч #{match_id} уже был внесён — регистрационный бот "
                    f"ответил «уже внесён». Карточка удалена из <#{message.channel.id}> "
                    f"(карточек: {deleted_cards}), в статистику не засчитан."
                )
                processing_completed = True
                return
            if not confirmed:
                log.warning(
                    "Матч #%s не подтверждён; исходная карточка сохранена. Ответ: %s",
                    match_id,
                    confirmation_text,
                )
                await send_processing_error_log(
                    match_id,
                    message,
                    "Регистрационный бот не подтвердил матч «Готово» — "
                    "карточка сохранена.",
                    (
                        f"Отправленная команда:\n{command_text}\n\n"
                        f"Ответ: {confirmation_text}"
                    ).replace("```", "ʼʼʼ"),
                )
                try:
                    await sent_registration.delete()
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception(
                        "Не удалось удалить отклонённую команду матча #%s",
                        match_id,
                    )
                return

            # Persist only after the external registration bot has explicitly
            # confirmed success. Nothing before this line may delete the source.
            await record_registration(
                match_id,
                posted_at=getattr(message, "created_at", None),
            )
            await send_registration_log(result, message, command_text)
            # Discord member lookups for warnings can take a long time. Take
            # the screenshot now and send warnings in the background, so the
            # confirmed card is cleaned up right away instead of after them.
            warning_images = await download_warning_images(result, message)
            run_in_background(
                send_zero_stat_warnings(result, message, warning_images)
            )
            if DELETE_AFTER_REGISTRATION:
                await asyncio.sleep(DELETE_DELAY)
                try:
                    await sent_registration.delete()
                except discord.NotFound:
                    # Регистрационный бот уже успел удалить команду — это нормально.
                    pass
                except Exception:
                    log.exception(
                        "Не удалось удалить сообщение регистрации матча #%s",
                        result.get("match_id"),
                    )

            await asyncio.sleep(SOURCE_DELETE_DELAY)
            deleted_duplicates = await delete_duplicate_match_cards(
                message,
                match_id,
                keep_current=not DELETE_SOURCE_AFTER_REGISTRATION,
            )
            deleted_confirmations = await delete_match_confirmations(
                message.channel,
                match_id,
                sent_registration,
            )
            run_in_background(
                delete_match_confirmations_later(
                    message.channel,
                    match_id,
                    sent_registration,
                )
            )
            log.info(
                "Матч #%s подтверждён; удалено карточек-дублей: %s, "
                "оставшихся «Готово»: %s",
                match_id,
                deleted_duplicates,
                deleted_confirmations,
            )
            processing_completed = True
            log.info(
                "Матч #%s успешно отправлен в канал %s",
                result["match_id"],
                message.channel.id,
            )
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}".strip()
            try:
                card_diagnostics = full_match_diagnostics(
                    context,
                    locals().get("modal_text"),
                    locals().get("result"),
                )
            except Exception:
                card_diagnostics = "Диагностика карточки недоступна."
            trace_tail = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)[-4:]
            )[-1500:]
            log.exception(
                "Ошибка обработки файла в process_upload.\n%s",
                card_diagnostics,
            )
            reason = f"Необработанное исключение: {error_text[:250]}"
            if re.search(
                r"HTTP 402|insufficient_balance|limit_exceeded|allowance exhausted",
                error_text,
                re.I,
            ):
                reason = (
                    "У API-ключей ИИ закончился лимит токенов или баланс "
                    "(HTTP 402) — увеличьте лимит ключа в панели ИИ-сервиса "
                    "или укажите новый ключ в Railway."
                )
            if isinstance(exc, discord.Forbidden):
                reason = (
                    f"Discord запретил действие в <#{message.channel.id}> (403): "
                    "у аккаунта нет доступа к каналу или права писать сообщения. "
                    "Проверьте права аккаунта на сервере и ID каналов в Railway."
                )
            await send_processing_error_log(
                reserved_match_id or "?", message,
                reason,
                (
                    f"ОШИБКА: {error_text[:1000]}\n\nГДЕ:\n{trace_tail}\n\n"
                    f"{card_diagnostics}"
                ).replace("```", "ʼʼʼ"),
            )
        finally:
            if not processing_completed:
                # Keep failed cards retryable during the same bot session.
                processed_message_ids.discard(message.id)
            if reserved_match_id is not None and not test_only:
                async with processing_match_lock:
                    processing_match_ids.discard(reserved_match_id)


async def process_message_once(
    message: discord.Message,
    test_only: bool = False,
) -> bool:
    """Process an image message once during the current bot session."""
    if message.id in processed_message_ids:
        return False
    if test_only:
        if (
            not (is_forwarded_message(message) or is_test_result_card(message))
            or not image_urls(message)
        ):
            return False
    elif not allowed_for_parsing(message) or not image_urls(message):
        return False

    processed_message_ids.add(message.id)
    async with processing_semaphore:
        await process_upload(message, test_only=test_only)
    return True


def describe_channel(channel: object, channel_id: int) -> str:
    """`#name (server) · ID` for Discord replies."""
    name = getattr(channel, "name", None)
    guild_name = getattr(getattr(channel, "guild", None), "name", None)
    if name and guild_name:
        return f"#{name} ({guild_name}) · `{channel_id}`"
    if name:
        return f"#{name} · `{channel_id}`"
    return f"канал `{channel_id}`"


def channel_error_hint(exc: Exception) -> str:
    if isinstance(exc, discord.NotFound):
        return " — канала с таким ID нет; проверьте ID в Railway"
    if isinstance(exc, discord.Forbidden):
        return " — у аккаунта нет доступа к каналу или к истории сообщений"
    return ""


async def backfill_one_channel_report(channel_id: int, before_time) -> dict:
    """Read old image messages from one channel and report what happened."""
    report: dict = {
        "label": f"канал `{channel_id}`",
        "scanned": 0,
        "found": 0,
        "error": None,
    }
    stage = "открыть канал"
    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        report["label"] = describe_channel(channel, channel_id)
        stage = "прочитать историю"

        log.info(
            "Читаю до %s старых сообщений из канала %s",
            BACKFILL_LIMIT,
            channel_id,
        )
        batch: list[discord.Message] = []
        async for old_message in channel.history(
            limit=BACKFILL_LIMIT,
            before=before_time,
            oldest_first=True,
        ):
            report["scanned"] += 1
            if (
                old_message.id not in processed_message_ids
                and allowed_for_parsing(old_message)
                and image_urls(old_message)
            ):
                batch.append(old_message)

            if len(batch) >= PROCESS_CONCURRENCY:
                results = await asyncio.gather(
                    *(process_message_once(item) for item in batch)
                )
                report["found"] += sum(bool(result) for result in results)
                batch.clear()

        if batch:
            results = await asyncio.gather(
                *(process_message_once(item) for item in batch)
            )
            report["found"] += sum(bool(result) for result in results)
    except Exception as exc:
        log.exception("Не удалось прочитать историю канала %s", channel_id)
        report["error"] = (
            f"не удалось {stage}: {type(exc).__name__}: {str(exc)[:200]}"
            f"{channel_error_hint(exc)}"
        )
    return report


async def backfill_one_channel(channel_id: int, before_time) -> int:
    """Read old image messages from one channel in chronological order."""
    return (await backfill_one_channel_report(channel_id, before_time))["found"]


async def backfill_channels(channel_ids: set[int], before_time) -> int:
    """Scan selected channels concurrently while preserving order per channel."""
    if not channel_ids:
        return 0
    counts = await asyncio.gather(
        *(backfill_one_channel(channel_id, before_time) for channel_id in channel_ids)
    )
    return sum(counts)


COMMANDS_HELP_TEXT = """📋 **Команды автореги**

**Авторег**
`старт все` — регистрировать игры из обычного и приоритетного каналов: сначала старые карточки, потом новые
`старт обычный` — только обычный канал
`старт приоритет` — только приоритетный канал
`енд` — остановить авторег
После перезапуска бота на Railway снова напишите `старт все`.

**Статистика и проверка**
`стата` — статистика и график по дням за 14 дней; реакции 📅 / 🕐 переключают дни и часы
`стата часы` — график по часам за 24 часа
`бот ты тут?` — проверить, что бот работает, с HTML-отчётом о настройках

**Обслуживание**
`забыть 2548` — удалить матч из памяти бота, чтобы зарегистрировать его заново
`удалить рег соо` — удалить все сообщения «Готово — Матч #… закрыт» в каналах регистрации
`апи состояние` / `состояние апи` — проверить состояние и остаток на каждом API-ключе
`команды` — этот список

**Проверка карточки без регистрации**
Перешлите карточку матча в любой канал вне регистрации — бот ответит готовой командой `=g`, ничего не отправляя в лигу."""


@client.event
async def on_raw_reaction_add(payload) -> None:
    """📅/🕐 under a stats message switch its chart between days and hours.

    A user account cannot send buttons, so reactions do their job.
    """
    message_id = int(getattr(payload, "message_id", 0) or 0)
    if message_id not in stats_messages:
        return
    user_id = getattr(payload, "user_id", None)
    if client.user and user_id == client.user.id:
        return
    emoji = str(getattr(payload, "emoji", "") or "")
    mode = next(
        (name for name, symbol in STATS_MODE_EMOJI.items() if symbol == emoji),
        None,
    )
    if mode is None:
        return
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        channel = await client.fetch_channel(payload.channel_id)
    message = await channel.fetch_message(message_id)
    with contextlib.suppress(Exception):
        await message.remove_reaction(payload.emoji, discord.Object(id=user_id))
    if stats_messages.get(message_id) == mode:
        return
    text, chart = registration_stats_message(mode)
    try:
        if chart is None:
            await message.edit(content=text)
        else:
            await message.edit(
                content=text,
                attachments=[
                    discord.File(io.BytesIO(chart), filename=f"registrations-{mode}.png")
                ],
            )
        stats_messages[message_id] = mode
    except Exception:
        log.warning(
            "Не удалось переключить график статистики, отправляю новый",
            exc_info=True,
        )
        await send_registration_stats(channel, mode)


@client.event
async def on_ready() -> None:
    log.info("Селф-бот успешно авторизован: %s | версия %s", client.user, BOT_VERSION)
    if GEMINI_RESERVE_KEYS_IGNORED:
        log.warning(
            "В GEMINI_RESERVE_API_KEYS больше %s ключей — лишние %s не используются.",
            GEMINI_RESERVE_KEYS_LIMIT,
            GEMINI_RESERVE_KEYS_IGNORED,
        )


@client.event
async def on_message_edit(
    before: discord.Message,
    after: discord.Message,
) -> None:
    """Retry a result card after the tournament bot edits it in place.

    Some cards are first posted with incomplete/failed OCR and later updated
    after «распознавание прошло со второй попытки». Discord does not emit a
    second on_message event for that update, so without this handler the ready
    card remains ignored until a manual restart/backfill.
    """
    run_in_background(delete_confirmation_when_registered(after))
    if not is_active:
        return
    channel_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
    if channel_id not in active_channel_ids:
        return
    if not image_urls(after):
        return
    after_text = plain_message_text(after)
    if not re.search(r"Результат\s+матча\s*#\s*\d+", after_text, re.I):
        return
    before_text = plain_message_text(before)
    if before_text == after_text and image_urls(before) == image_urls(after):
        return

    # The same Discord message ID was already inspected before the source bot
    # completed its second recognition attempt. Treat the edited card as new.
    processed_message_ids.discard(after.id)
    await asyncio.sleep(1.0)
    log.info("Повторно обрабатываю обновлённую карточку сообщения %s", after.id)
    await process_message_once(after)


@client.event
async def on_message(message: discord.Message) -> None:
    global is_active

    run_in_background(delete_confirmation_when_registered(message))
    command = message.content.strip().lower()

    if command == "команды":
        await message.channel.send(COMMANDS_HELP_TEXT)
        return

    if re.fullmatch(r"бот\s*,?\s*ты\s+тут\s*\?*", command, re.I):
        configured_user = None
        if MY_ACCOUNT_ID:
            guild = getattr(message, "guild", None)
            if guild is not None:
                configured_user = guild.get_member(MY_ACCOUNT_ID)
            if configured_user is None:
                configured_user = client.get_user(MY_ACCOUNT_ID)
            if configured_user is None:
                try:
                    configured_user = await client.fetch_user(MY_ACCOUNT_ID)
                except Exception:
                    log.exception(
                        "Не удалось получить пользователя MY_ACCOUNT_ID=%s",
                        MY_ACCOUNT_ID,
                    )

        if configured_user is None:
            configured_name = (
                "Не найден в Discord" if MY_ACCOUNT_ID else "MY_ACCOUNT_ID не указан"
            )
        else:
            configured_name = str(
                getattr(configured_user, "display_name", None)
                or getattr(configured_user, "global_name", None)
                or getattr(configured_user, "name", configured_user)
            )

        channel_names: list[str] = []
        for channel_id in sorted(active_channel_ids):
            channel = client.get_channel(channel_id)
            channel_name = getattr(channel, "name", None)
            channel_names.append(
                f"#{channel_name} · {channel_id}" if channel_name else str(channel_id)
            )

        counts = registration_status_counts()
        now = datetime.now(timezone.utc)
        uptime = format_uptime(int((now - BOT_STARTED_AT).total_seconds()))
        latency_value = getattr(client, "latency", None)
        latency = (
            f"{round(float(latency_value) * 1000)} мс"
            if isinstance(latency_value, (int, float)) and latency_value >= 0
            else "неизвестно"
        )
        session_user = (
            f"{client.user} · {getattr(client.user, 'id', 'неизвестно')}"
            if client.user
            else "сессия не определена"
        )
        generated_at = now.astimezone(STATS_TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")
        status = {
            "active": is_active,
            "version": BOT_VERSION,
            "uptime": uptime,
            "latency": latency,
            "processing": len(processing_match_ids),
            "configured_name": configured_name,
            "configured_id": MY_ACCOUNT_ID or "не указан",
            "session_user": session_user,
            "registrations_total": counts["total"],
            "registrations_today": counts["today"],
            "registrations_hour": counts["hour"],
            "warning_status": (
                f"канал {WARN_CHANNEL_ID}" if WARN_CHANNEL_ID else "не настроены"
            ),
            "pro_exemptions": len(PRO_LEAGUE_USER_IDS),
            "api_style": AI_API_STYLE,
            "models": ", ".join(GEMINI_MODELS),
            "key_count": ai_keys_summary(),
            "concurrency": PROCESS_CONCURRENCY,
            "confidence": f"{MIN_CONFIDENCE:.2f}",
            "channels": channel_names,
            "generated_at": generated_at,
            "timezone": str(STATS_TIMEZONE),
        }
        report_bytes = build_status_html(status).encode("utf-8")
        report_file = discord.File(
            io.BytesIO(report_bytes),
            filename=f"faceit-bot-status-{now.strftime('%Y%m%d-%H%M%S')}.html",
        )
        active_text = "запущен" if is_active else "ожидает `старт ...`"
        await message.channel.send(
            "🟢 **Я тут и готов к работе**\n"
            f"Версия: `{BOT_VERSION}`\n"
            f"Сессия Discord: **{client.user}** (`{getattr(client.user, 'id', '—')}`)\n"
            f"MY_ACCOUNT_ID: `{MY_ACCOUNT_ID or 'не указан'}` → **{configured_name}**\n"
            f"Авторег: **{active_text}** · обрабатывается игр: **{len(processing_match_ids)}**\n"
            f"Регистраций: всего **{counts['total']}**, сегодня **{counts['today']}**\n"
            f"Моделей: **{len(GEMINI_MODELS)}** · API-ключи: {ai_keys_summary()}\n"
            f"Автоварны: **{'включены' if WARN_CHANNEL_ID else 'не настроены'}** · "
            f"Pro League ID: **{len(PRO_LEAGUE_USER_IDS)}**\n"
            "Команды доступны **всем пользователям**. Подробный HTML-отчёт прикреплён.",
            file=report_file,
        )
        return

    if re.fullmatch(
        r"удалить\s+рег(?:истрационные)?\s+соо(?:бщения)?",
        command,
        re.I,
    ):
        registration_channel_ids = NORMAL_CHANNEL_IDS | PRIORITY_CHANNEL_IDS
        if not registration_channel_ids:
            await message.channel.send(
                "❌ В Railway не указаны NORMAL_CHANNEL_IDS или PRIORITY_CHANNEL_IDS."
            )
            return

        await message.channel.send(
            "🧹 Удаляю сообщения `Готово — Матч #… закрыт со счётом…` "
            "во всех каналах регистрации."
        )
        deleted, scanned, failed, unmatched = (
            await delete_all_registration_confirmations(registration_channel_ids)
        )
        suffix = f" Ошибок каналов: **{failed}**." if failed else ""
        if not deleted and unmatched:
            sample = unmatched[0].replace("```", "ʼʼʼ")
            suffix += (
                "\nПохожее сообщение не распознано как «Готово — Матч #… "
                f"закрыт»:\n```text\n{sample}\n```"
            )
        await message.channel.send(
            (
                f"✅ Очистка завершена. Каналов проверено: **{scanned}**, "
                f"сообщений удалено: **{deleted}**.{suffix}"
            )[:2000]
        )
        return

    forget_match = re.fullmatch(r"забыть\s+#?(\d+)", command, re.I)
    if forget_match:
        match_id = int(forget_match.group(1))
        async with processing_match_lock:
            if match_id in processing_match_ids:
                await message.channel.send(
                    f"⏳ Игра #{match_id} сейчас обрабатывается. Повтори команду чуть позже."
                )
                return

            existed = any(
                str(item.get("match_id")) == str(match_id)
                for item in load_registration_records()
            )
            if not existed:
                await message.channel.send(
                    f"ℹ️ Игры #{match_id} нет в памяти — забывать нечего."
                )
                return

            await forget_registration(match_id)
            processing_match_ids.discard(match_id)
            # Разрешаем следующему `старт ...` снова проверить старую карточку
            # в рамках текущего запуска бота. Остальные игры от повторной
            # регистрации всё равно защищены registration_stats.json.
            processed_message_ids.clear()

        await message.channel.send(
            f"✅ Игра #{match_id} забыта. Теперь её можно зарегистрировать заново."
        )
        return

    if command.startswith("забыть"):
        await message.channel.send("Формат команды: `забыть 2548`")
        return

    if re.fullmatch(r"апи\s+состояние|состояние\s+апи|api\s+status|апи\s+статус|статус\s+апи", command, re.I):
        await message.channel.send("⏳ Проверяю состояние API-ключей...")
        try:
            results = await check_ai_key_status()
            response = format_ai_status_message(results)
            while response:
                chunk, response = response[:2000], response[2000:]
                await message.channel.send(chunk)
        except Exception as exc:
            log.exception("Ошибка проверки API-ключей")
            await message.channel.send(f"❌ Ошибка проверки: {exc}")
        return

    stats_command = re.fullmatch(
        r"(?:стата|статистика|stats)(?:\s+(дни|часы|days|hours))?",
        command,
    )
    if stats_command:
        mode = "hours" if stats_command.group(1) in ("часы", "hours") else "days"
        await send_registration_stats(message.channel, mode)
        return

    if command == "енд" or command.startswith("старт"):
        if command == "енд":
            is_active = False
            active_channel_ids.clear()
            await message.channel.send("🛑 Авторег остановлен.")
            return

        mode = command.removeprefix("старт").strip()
        modes = {
            "обычный": ("обычный", NORMAL_CHANNEL_IDS),
            "приоритет": ("приоритет", PRIORITY_CHANNEL_IDS),
            "все": ("обычный + приоритет", NORMAL_CHANNEL_IDS | PRIORITY_CHANNEL_IDS),
        }
        if mode not in modes:
            await message.channel.send(
                "Выберите режим: `старт обычный`, `старт приоритет` или `старт все`."
            )
            return

        mode_name, selected_ids = modes[mode]
        if not selected_ids:
            await message.channel.send(
                f"❌ Для режима «{mode_name}» не указаны ID каналов в Railway."
            )
            return

        active_channel_ids.clear()
        active_channel_ids.update(selected_ids)
        is_active = True
        await message.channel.send(
            f"✅ Запущен режим «{mode_name}». Читаю старые игры, затем новые."
        )
        reports = await asyncio.gather(
            *(
                backfill_one_channel_report(channel_id, message.created_at)
                for channel_id in sorted(active_channel_ids)
            )
        )
        count = sum(report["found"] for report in reports)
        lines = []
        for report in reports:
            if report["error"]:
                lines.append(f"• {report['label']}: ❌ {report['error']}")
            else:
                lines.append(
                    f"• {report['label']}: прочитано сообщений "
                    f"{report['scanned']}, новых карточек {report['found']}"
                )
        await message.channel.send(
            (
                f"✅ Архив режима «{mode_name}» проверен. Найдено изображений: {count}.\n"
                + "\n".join(lines)
            )[:2000]
        )
        return

    # Any forwarded game card is a safe test request, including a card posted
    # by the self-bot account in the main command channel or in an active
    # registration channel. It only prints the generated =g command: no stats
    # are saved, no confirmation is awaited and no source message is deleted.
    if is_unconfigured_result_card(message):
        _unconfigured_card_channels.add(int(message.channel.id))
        run_in_background(notify_unconfigured_card_channel(message))

    if (
        (is_forwarded_message(message) or is_test_result_card(message))
        and image_urls(message)
    ):
        await process_message_once(message, test_only=True)
        return

    if is_active:
        await process_message_once(message)


if __name__ == "__main__":
    client.run(DISCORD_USER_TOKEN)
