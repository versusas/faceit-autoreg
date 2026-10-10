"""
Telegram-бот для управления Discord-авторегом.

Управление через inline-кнопки.  Статистика отправляется картинкой.
Доступ: TELEGRAM_ADMIN_IDS (env) + динамический список в файле прав.
Если у пользователя нет доступа — бот отвечает «нет доступа».

Переменная Railway: TELEGRAM_BOT_TOKEN  — токен бота из @BotFather
                    TELEGRAM_ADMIN_IDS  — ваш Telegram user_id (числа через запятую)
"""

import asyncio
import io
import json
import logging
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


# ── TG commands queue (TG → Discord bot) ───────────────────────────────────

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


def push_tg_command(cmd: str, args: str = "") -> None:
    data = load_tg_commands()
    data.setdefault("pending", []).append({
        "cmd": cmd,
        "args": args,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    save_tg_commands(data)


# ── Discord permissions (start/stop rights in Discord) ─────────────────────

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
        log.exception("Ошибка чтения %s", COMMAND_PERMISSIONS_FILE)
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


# ── Telegram permission system ─────────────────────────────────────────────

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


def grant_tg_access(tg_user_id: int, label: str = "") -> bool:
    data = _load_tg_perms()
    allowed: dict = data.get("allowed_users", {})
    key = str(tg_user_id)
    if key in allowed:
        return False
    allowed[key] = {"label": label, "granted_at": datetime.now(timezone.utc).isoformat()}
    data["allowed_users"] = allowed
    _save_tg_perms(data)
    return True


def revoke_tg_access(tg_user_id: int) -> bool:
    data = _load_tg_perms()
    allowed: dict = data.get("allowed_users", {})
    key = str(tg_user_id)
    if key not in allowed:
        return False
    del allowed[key]
    data["allowed_users"] = allowed
    _save_tg_perms(data)
    return True


def list_tg_access() -> dict:
    data = _load_tg_perms()
    return data.get("allowed_users", {})


def is_root_admin(user_id: int) -> bool:
    """Root admins = those listed in TELEGRAM_ADMIN_IDS env var."""
    return user_id in TELEGRAM_ADMIN_IDS


def has_access(user_id: int) -> bool:
    """Root admins + dynamically granted TG users."""
    if is_root_admin(user_id):
        return True
    allowed = _load_tg_perms().get("allowed_users", {})
    return str(user_id) in allowed


# ── Stats ──────────────────────────────────────────────────────────────────

def load_registrations() -> list[dict]:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def compute_stats() -> dict:
    records = [
        r for r in load_registrations()
        if r.get("kind", "registered") == "registered"
    ]
    moments: list[datetime] = []
    for r in records:
        for key in ("card_posted_at", "registered_at"):
            try:
                val = datetime.fromisoformat(str(r[key]))
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                moments.append(val.astimezone(timezone.utc))
                break
            except (KeyError, TypeError, ValueError):
                pass

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(STATS_TIMEZONE)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    tz_label = "МСК" if str(STATS_TIMEZONE) == "Europe/Moscow" else str(STATS_TIMEZONE)

    def since(delta: timedelta) -> int:
        return sum(now - delta <= m <= now for m in moments)

    today = sum(today_start <= m <= now for m in moments)
    yesterday = sum(yesterday_start <= m < today_start for m in moments)

    # Per-hour histogram for last 24h
    hourly: list[int] = []
    for h in range(24):
        start = now - timedelta(hours=24 - h)
        end = now - timedelta(hours=23 - h)
        hourly.append(sum(start <= m < end for m in moments))

    return {
        "total": len(moments),
        "today": today,
        "yesterday": yesterday,
        "h24": since(timedelta(hours=24)),
        "h10": since(timedelta(hours=10)),
        "h1": since(timedelta(hours=1)),
        "m30": since(timedelta(minutes=30)),
        "tz": tz_label,
        "generated": local_now.strftime("%d.%m.%Y %H:%M:%S"),
        "hourly": hourly,
    }


def render_stats_image(stats: dict) -> bytes:
    """Render stats dict to a PNG image."""
    W, H = 720, 560
    BG = (30, 30, 46)
    TEXT = (205, 214, 244)
    ACCENT = (137, 180, 250)
    BAR_FG = (166, 227, 161)
    BAR_BG = (49, 50, 68)
    MUTED = (147, 153, 178)
    HEADER_BG = (24, 24, 37)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Try system monospace fonts
    font_big = None
    font_med = None
    font_sm = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(font_path):
            font_big = ImageFont.truetype(font_path, 26)
            font_med = ImageFont.truetype(font_path, 18)
            font_sm = ImageFont.truetype(font_path, 14)
            break
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]:
        if os.path.exists(font_path):
            if font_med is None:
                font_med = ImageFont.truetype(font_path, 18)
            if font_sm is None:
                font_sm = ImageFont.truetype(font_path, 14)
            break
    if font_big is None:
        try:
            font_big = ImageFont.truetype("arial.ttf", 26)
            font_med = ImageFont.truetype("arial.ttf", 18)
            font_sm = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            font_big = ImageFont.load_default()
            font_med = font_big
            font_sm = font_big

    # Header
    draw.rectangle([(0, 0), (W, 56)], fill=HEADER_BG)
    draw.text((24, 14), "📊  СТАТИСТИКА РЕГИСТРАЦИЙ", fill=ACCENT, font=font_big)

    y = 76
    rows = [
        ("Всего", str(stats["total"])),
        (f"Сегодня ({stats['tz']})", str(stats["today"])),
        ("Вчера", str(stats["yesterday"])),
        ("За 24 часа", str(stats["h24"])),
        ("За 10 часов", str(stats["h10"])),
        ("За 1 час", str(stats["h1"])),
        ("За 30 минут", str(stats["m30"])),
    ]
    for label, value in rows:
        draw.text((32, y), label, fill=MUTED, font=font_med)
        draw.text((400, y), value, fill=TEXT, font=font_med)
        y += 32

    # Histogram
    y += 12
    draw.text((32, y), "Регистрации по часам (24 ч)", fill=ACCENT, font=font_med)
    y += 28
    hourly = stats.get("hourly", [0] * 24)
    max_h = max(hourly) if hourly else 1
    bar_area_w = W - 64
    bar_w = max(2, bar_area_w // 24 - 2)
    bar_max_h = 100
    for i, val in enumerate(hourly):
        x0 = 32 + i * (bar_w + 2)
        h = int(val / max(max_h, 1) * bar_max_h) if max_h > 0 else 0
        # Background bar
        draw.rectangle([(x0, y), (x0 + bar_w, y + bar_max_h)], fill=BAR_BG)
        # Value bar
        if h > 0:
            draw.rectangle([(x0, y + bar_max_h - h), (x0 + bar_w, y + bar_max_h)], fill=BAR_FG)

    # Time axis labels
    y2 = y + bar_max_h + 4
    now_utc = datetime.now(timezone.utc)
    for i in range(0, 24, 3):
        x0 = 32 + i * (bar_w + 2)
        hour_label = (now_utc - timedelta(hours=24 - i)).astimezone(STATS_TIMEZONE)
        draw.text((x0, y2), hour_label.strftime("%H"), fill=MUTED, font=font_sm)

    # Footer
    draw.text((32, H - 28), f"Обновлено: {stats['generated']}", fill=MUTED, font=font_sm)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Telegram API ───────────────────────────────────────────────────────────

async def tg_request(
    session: aiohttp.ClientSession,
    method: str,
    **kwargs,
) -> Optional[dict]:
    url = f"{TG_API}/{method}"
    try:
        async with session.post(
            url, json=kwargs, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning("TG API %s error: %s", method, data)
            return data
    except Exception as exc:
        log.warning("TG API %s exception: %s", method, exc)
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
    kwargs: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
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
    kwargs: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
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
        session,
        "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text,
        show_alert=show_alert,
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


# ── Keyboards ──────────────────────────────────────────────────────────────

def main_menu_keyboard(user_id: int) -> dict:
    buttons = [
        [
            {"text": "▶️ Старт авторег", "callback_data": "cmd:start_all"},
            {"text": "⏹ Стоп авторег", "callback_data": "cmd:stop"},
        ],
        [
            {"text": "📊 Статистика", "callback_data": "cmd:stats"},
            {"text": "📡 Статус", "callback_data": "cmd:status"},
        ],
        [
            {"text": "👥 Права Discord", "callback_data": "cmd:dc_rights"},
        ],
    ]
    if is_root_admin(user_id):
        buttons.append([
            {"text": "🔑 Права TG", "callback_data": "cmd:tg_rights"},
        ])
    return {"inline_keyboard": buttons}


def start_mode_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 Все каналы", "callback_data": "start:все"},
            ],
            [
                {"text": "📌 Обычные", "callback_data": "start:обычный"},
                {"text": "⭐ Приоритет", "callback_data": "start:приоритет"},
            ],
            [
                {"text": "◀️ Назад", "callback_data": "cmd:menu"},
            ],
        ]
    }


def dc_rights_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Список прав", "callback_data": "dc:list"},
            ],
            [
                {"text": "➕ Выдать права", "callback_data": "dc:grant_prompt"},
                {"text": "➖ Забрать права", "callback_data": "dc:revoke_prompt"},
            ],
            [
                {"text": "◀️ Назад", "callback_data": "cmd:menu"},
            ],
        ]
    }


def tg_rights_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Список TG-прав", "callback_data": "tg:list"},
            ],
            [
                {"text": "➕ Выдать TG-доступ", "callback_data": "tg:grant_prompt"},
                {"text": "➖ Забрать TG-доступ", "callback_data": "tg:revoke_prompt"},
            ],
            [
                {"text": "◀️ Назад", "callback_data": "cmd:menu"},
            ],
        ]
    }


def back_keyboard() -> dict:
    return {
        "inline_keyboard": [[{"text": "◀️ Меню", "callback_data": "cmd:menu"}]]
    }


# ── State for pending input ───────────────────────────────────────────────

user_pending_action: dict[int, str] = {}

ACCESS_DENIED_TEXT = "🚫 <b>Нет доступа</b>\n\nУ вас нет прав для управления ботом.\nОбратитесь к администратору."


# ── Handlers ───────────────────────────────────────────────────────────────

async def handle_start_command(session: aiohttp.ClientSession, chat_id: int, user_id: int) -> None:
    if not has_access(user_id):
        await send_message(session, chat_id, ACCESS_DENIED_TEXT)
        return
    await send_message(
        session,
        chat_id,
        "🤖 <b>Управление Discord-авторегом</b>\n\nВыберите действие:",
        reply_markup=main_menu_keyboard(user_id),
    )


async def handle_callback(session: aiohttp.ClientSession, callback: dict) -> None:
    cb_id = callback["id"]
    user_id = int(callback["from"]["id"])
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat_id = msg.get("chat", {}).get("id", 0)
    message_id = msg.get("message_id", 0)

    if not has_access(user_id):
        await answer_callback(session, cb_id, "🚫 Нет доступа", show_alert=True)
        return

    # ── Main menu ──
    if data == "cmd:menu":
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "🤖 <b>Управление Discord-авторегом</b>\n\nВыберите действие:",
            reply_markup=main_menu_keyboard(user_id),
        )

    # ── Start autoreg (mode select) ──
    elif data == "cmd:start_all":
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "▶️ <b>Запуск авторега</b>\n\nВыберите режим:",
            reply_markup=start_mode_keyboard(),
        )

    elif data.startswith("start:"):
        mode = data.split(":", 1)[1]
        push_tg_command(f"старт {mode}")
        await answer_callback(session, cb_id, f"✅ Команда «старт {mode}» отправлена")
        await edit_message(
            session, chat_id, message_id,
            f"✅ Команда <b>старт {mode}</b> отправлена Discord-боту.\n"
            "Авторег запустится в течение нескольких секунд.",
            reply_markup=back_keyboard(),
        )

    # ── Stop autoreg ──
    elif data == "cmd:stop":
        push_tg_command("енд")
        await answer_callback(session, cb_id, "⏹ Авторег остановлен")
        await edit_message(
            session, chat_id, message_id,
            "⏹ Команда <b>стоп</b> отправлена Discord-боту.",
            reply_markup=back_keyboard(),
        )

    # ── Stats (image) ──
    elif data == "cmd:stats":
        await answer_callback(session, cb_id, "📊 Генерирую статистику...")
        stats = compute_stats()
        try:
            img = render_stats_image(stats)
            await send_photo(
                session, chat_id, img,
                caption="📊 <b>Статистика регистраций</b>",
                reply_markup=back_keyboard(),
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
        await answer_callback(session, cb_id)
        granted_dc = list_discord_access()
        granted_tg = list_tg_access()
        pending = load_tg_commands().get("pending", [])
        text = (
            "📡 <b>Состояние</b>\n\n"
            f"Discord-пользователей с правами: <b>{len(granted_dc)}</b>\n"
            f"TG-пользователей с доступом: <b>{len(granted_tg)}</b>\n"
            f"Root-админов (env): <b>{len(TELEGRAM_ADMIN_IDS)}</b>\n"
            f"Команд в очереди: <b>{len(pending)}</b>"
        )
        await edit_message(
            session, chat_id, message_id,
            text, reply_markup=back_keyboard(),
        )

    # ── Discord rights ──
    elif data == "cmd:dc_rights":
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "👥 <b>Права Discord (старт/стоп)</b>\n\nВыберите действие:",
            reply_markup=dc_rights_keyboard(),
        )

    elif data == "dc:list":
        await answer_callback(session, cb_id)
        ids = list_discord_access()
        if ids:
            lines = "\n".join(f"• <code>{uid}</code>" for uid in ids)
            text = f"👥 <b>Discord-права:</b>\n\n{lines}"
        else:
            text = "ℹ️ Нет пользователей с выданными Discord-правами."
        await edit_message(
            session, chat_id, message_id,
            text, reply_markup=dc_rights_keyboard(),
        )

    elif data == "dc:grant_prompt":
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "dc_grant"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Discord ID</b> пользователя для выдачи прав.\n"
            "(17–19 цифр)",
            reply_markup=back_keyboard(),
        )

    elif data == "dc:revoke_prompt":
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "dc_revoke"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Discord ID</b> пользователя для снятия прав.",
            reply_markup=back_keyboard(),
        )

    # ── TG rights (root admin only) ──
    elif data == "cmd:tg_rights":
        if not is_root_admin(user_id):
            await answer_callback(session, cb_id, "🚫 Только root-admin", show_alert=True)
            return
        await answer_callback(session, cb_id)
        await edit_message(
            session, chat_id, message_id,
            "🔑 <b>Управление TG-доступом</b>\n\nВыберите действие:",
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
            text = f"🔑 <b>TG-пользователи с доступом:</b>\n\n" + "\n".join(lines)
        else:
            text = "ℹ️ Дополнительных TG-пользователей нет.\nДоступ только у root-админов из env."
        await edit_message(
            session, chat_id, message_id,
            text, reply_markup=tg_rights_keyboard(),
        )

    elif data == "tg:grant_prompt":
        if not is_root_admin(user_id):
            await answer_callback(session, cb_id, "🚫 Только root-admin", show_alert=True)
            return
        await answer_callback(session, cb_id)
        user_pending_action[user_id] = "tg_grant"
        await edit_message(
            session, chat_id, message_id,
            "✏️ Отправьте <b>Telegram ID</b> пользователя или перешлите его сообщение.\n"
            "Можно также: <code>ID метка</code> (метка опциональна).",
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
            "✏️ Отправьте <b>Telegram ID</b> пользователя для снятия доступа.",
            reply_markup=back_keyboard(),
        )

    else:
        await answer_callback(session, cb_id, "❓")


async def handle_text_input(
    session: aiohttp.ClientSession,
    chat_id: int,
    user_id: int,
    text: str,
) -> None:
    """Handle text input when the bot is waiting for a value."""
    action = user_pending_action.pop(user_id, None)
    if not action:
        return

    if not has_access(user_id):
        await send_message(session, chat_id, ACCESS_DENIED_TEXT)
        return

    # ── Discord grant/revoke ──
    if action == "dc_grant":
        match = re.search(r"\d{5,22}", text)
        if not match:
            await send_message(
                session, chat_id,
                "⚠️ Не нашёл Discord ID (17–19 цифр). Попробуйте ещё раз.",
                reply_markup=back_keyboard(),
            )
            return
        target = int(match.group(0))
        ok = grant_discord_access(target)
        msg = (
            f"✅ Discord-пользователю <code>{target}</code> выданы права."
            if ok
            else f"ℹ️ У <code>{target}</code> уже есть права."
        )
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    elif action == "dc_revoke":
        match = re.search(r"\d{5,22}", text)
        if not match:
            await send_message(
                session, chat_id,
                "⚠️ Не нашёл Discord ID. Попробуйте ещё раз.",
                reply_markup=back_keyboard(),
            )
            return
        target = int(match.group(0))
        ok = revoke_discord_access(target)
        msg = (
            f"✅ У <code>{target}</code> забраны Discord-права."
            if ok
            else f"ℹ️ У <code>{target}</code> не было прав."
        )
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    # ── TG grant/revoke (root admin only) ──
    elif action == "tg_grant":
        if not is_root_admin(user_id):
            await send_message(session, chat_id, "🚫 Только root-admin.", reply_markup=back_keyboard())
            return
        match = re.search(r"\d{4,15}", text)
        if not match:
            await send_message(
                session, chat_id,
                "⚠️ Не нашёл Telegram ID. Отправьте число.",
                reply_markup=back_keyboard(),
            )
            return
        target = int(match.group(0))
        label = text.replace(match.group(0), "").strip()[:100]
        ok = grant_tg_access(target, label)
        msg = (
            f"✅ TG-пользователю <code>{target}</code> выдан доступ к боту."
            if ok
            else f"ℹ️ У <code>{target}</code> уже есть доступ."
        )
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())

    elif action == "tg_revoke":
        if not is_root_admin(user_id):
            await send_message(session, chat_id, "🚫 Только root-admin.", reply_markup=back_keyboard())
            return
        match = re.search(r"\d{4,15}", text)
        if not match:
            await send_message(
                session, chat_id,
                "⚠️ Не нашёл Telegram ID.",
                reply_markup=back_keyboard(),
            )
            return
        target = int(match.group(0))
        ok = revoke_tg_access(target)
        msg = (
            f"✅ У TG-пользователя <code>{target}</code> забран доступ."
            if ok
            else f"ℹ️ У <code>{target}</code> не было выданного доступа."
        )
        await send_message(session, chat_id, msg, reply_markup=back_keyboard())


async def handle_forwarded_for_grant(
    session: aiohttp.ClientSession,
    chat_id: int,
    user_id: int,
    forwarded_from_id: int,
    forwarded_name: str,
) -> None:
    """If user forwarded a message while in tg_grant mode, use the sender's ID."""
    action = user_pending_action.get(user_id)
    if action != "tg_grant":
        return
    user_pending_action.pop(user_id, None)
    if not is_root_admin(user_id):
        await send_message(session, chat_id, "🚫 Только root-admin.", reply_markup=back_keyboard())
        return
    ok = grant_tg_access(forwarded_from_id, forwarded_name)
    msg = (
        f"✅ TG-пользователю <code>{forwarded_from_id}</code> ({forwarded_name}) выдан доступ."
        if ok
        else f"ℹ️ У <code>{forwarded_from_id}</code> уже есть доступ."
    )
    await send_message(session, chat_id, msg, reply_markup=back_keyboard())


# ── Update dispatcher ──────────────────────────────────────────────────────

async def handle_update(session: aiohttp.ClientSession, update: dict) -> None:
    # Callback query from inline buttons
    cb = update.get("callback_query")
    if cb:
        await handle_callback(session, cb)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = int(msg.get("from", {}).get("id", 0))
    text = (msg.get("text") or "").strip()

    # Forwarded message — maybe granting TG access
    fwd_from = msg.get("forward_from")
    if fwd_from and user_id in user_pending_action:
        fwd_id = int(fwd_from.get("id", 0))
        fwd_name = (
            (fwd_from.get("first_name") or "") + " " + (fwd_from.get("last_name") or "")
        ).strip() or str(fwd_id)
        if fwd_id:
            await handle_forwarded_for_grant(session, chat_id, user_id, fwd_id, fwd_name)
            return

    if not text:
        return

    # /start or /команды
    cmd_raw = text.split()[0].lower().lstrip("/").split("@")[0]
    if cmd_raw in ("start", "menu", "команды", "help", "старт_бот"):
        await handle_start_command(session, chat_id, user_id)
        return

    # If we're waiting for text input from this user
    if user_id in user_pending_action:
        await handle_text_input(session, chat_id, user_id, text)
        return

    # Legacy slash commands still work
    if cmd_raw in ("старт",):
        if not has_access(user_id):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        push_tg_command("старт все")
        await send_message(
            session, chat_id,
            "✅ Команда <b>старт все</b> отправлена.",
            reply_markup=back_keyboard(),
        )
    elif cmd_raw in ("стоп", "stop", "енд"):
        if not has_access(user_id):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        push_tg_command("енд")
        await send_message(
            session, chat_id,
            "⏹ Команда <b>стоп</b> отправлена.",
            reply_markup=back_keyboard(),
        )
    elif cmd_raw == "стата":
        if not has_access(user_id):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        stats = compute_stats()
        try:
            img = render_stats_image(stats)
            await send_photo(
                session, chat_id, img,
                caption="📊 <b>Статистика регистраций</b>",
                reply_markup=back_keyboard(),
            )
        except Exception:
            log.exception("Ошибка рендера статистики")
            await send_message(session, chat_id, "⚠️ Ошибка рендера.", reply_markup=back_keyboard())
    elif cmd_raw in ("выдать_тг", "grant_tg"):
        if not is_root_admin(user_id):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        args_text = text[len(text.split()[0]):].strip()
        match = re.search(r"\d{4,15}", args_text)
        if match:
            target = int(match.group(0))
            label = args_text.replace(match.group(0), "").strip()[:100]
            ok = grant_tg_access(target, label)
            msg_text = (
                f"✅ TG-доступ выдан: <code>{target}</code>"
                if ok
                else f"ℹ️ Уже есть доступ: <code>{target}</code>"
            )
            await send_message(session, chat_id, msg_text, reply_markup=back_keyboard())
        else:
            await send_message(
                session, chat_id,
                "Формат: /выдать_тг <code>TELEGRAM_ID</code> [метка]",
                reply_markup=back_keyboard(),
            )
    elif cmd_raw in ("убрать_тг", "revoke_tg"):
        if not is_root_admin(user_id):
            await send_message(session, chat_id, ACCESS_DENIED_TEXT)
            return
        args_text = text[len(text.split()[0]):].strip()
        match = re.search(r"\d{4,15}", args_text)
        if match:
            target = int(match.group(0))
            ok = revoke_tg_access(target)
            msg_text = (
                f"✅ TG-доступ убран: <code>{target}</code>"
                if ok
                else f"ℹ️ Не было доступа: <code>{target}</code>"
            )
            await send_message(session, chat_id, msg_text, reply_markup=back_keyboard())
        else:
            await send_message(
                session, chat_id,
                "Формат: /убрать_тг <code>TELEGRAM_ID</code>",
                reply_markup=back_keyboard(),
            )


# ── Polling loop ───────────────────────────────────────────────────────────

async def polling_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN не задан — Telegram-бот не запущен.")
        return

    if not TELEGRAM_ADMIN_IDS:
        log.warning(
            "TELEGRAM_ADMIN_IDS не задан — только динамически выданные TG-права будут работать."
        )

    log.info("Telegram-бот запущен. Root admins: %s", TELEGRAM_ADMIN_IDS)
    offset = 0
    timeout = 30

    async with aiohttp.ClientSession() as session:
        await tg_request(session, "getUpdates", offset=-1, timeout=1)

        while True:
            try:
                resp = await tg_request(
                    session, "getUpdates",
                    offset=offset,
                    timeout=timeout,
                    allowed_updates=["message", "edited_message", "callback_query"],
                )
                if not resp or not resp.get("ok"):
                    await asyncio.sleep(5)
                    continue

                updates = resp.get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await handle_update(session, update)
                    except Exception:
                        log.exception("Ошибка обработки апдейта %s", update.get("update_id"))

            except asyncio.CancelledError:
                log.info("Telegram polling остановлен.")
                return
            except Exception:
                log.exception("Ошибка polling loop, перезапуск через 10 с")
                await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(polling_loop())
