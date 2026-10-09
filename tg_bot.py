"""
Telegram-бот для управления Discord-авторегом.

Команды:
  /start       — приветствие и список команд
  /старт       — запустить авторег (все каналы)
  /стоп        — остановить авторег
  /стата       — статистика регистраций
  /выдать @ник ID — выдать права на старт/стоп в Discord
  /убрать ID   — убрать права
  /права       — показать список пользователей с правами
  /статус      — состояние бота

Переменная Railway: TELEGRAM_BOT_TOKEN  — токен бота из @BotFather
                    TELEGRAM_ADMIN_IDS  — ваш Telegram user_id (числа через запятую)
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

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
# Путь к файлу для передачи команд от ТГ → Discord-боту
TG_COMMANDS_FILE = os.getenv("TG_COMMANDS_FILE", "/data/tg_commands.json")
STATS_TIMEZONE = ZoneInfo(os.getenv("STATS_TIMEZONE", "Europe/Moscow"))

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --------------------------------------------------------------------------- #
# Хранилище команд (передаёт команды основному боту через файл)
# --------------------------------------------------------------------------- #

def load_tg_commands() -> dict:
    try:
        with open(TG_COMMANDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
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


# --------------------------------------------------------------------------- #
# Права доступа
# --------------------------------------------------------------------------- #

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
    except FileNotFoundError:
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


# --------------------------------------------------------------------------- #
# Статистика регистраций
# --------------------------------------------------------------------------- #

def load_registrations() -> list[dict]:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def registration_stats_text() -> str:
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

    lines = [
        "📊 *Статистика регистраций*",
        f"Всего: *{len(moments)}*",
        f"Сегодня ({tz_label}): *{today}*",
        f"Вчера: *{yesterday}*",
        f"За 24 часа: *{since(timedelta(hours=24))}*",
        f"За 10 часов: *{since(timedelta(hours=10))}*",
        f"За 1 час: *{since(timedelta(hours=1))}*",
        f"За 30 минут: *{since(timedelta(minutes=30))}*",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #

async def tg_request(
    session: aiohttp.ClientSession,
    method: str,
    **kwargs,
) -> Optional[dict]:
    url = f"{TG_API}/{method}"
    try:
        async with session.post(url, json=kwargs, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.warning("TG API %s error: %s", method, data)
            return data
    except Exception as exc:
        log.warning("TG API %s exception: %s", method, exc)
        return None


async def send_message(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
    parse_mode: str = "Markdown",
) -> None:
    await tg_request(session, "sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)


# --------------------------------------------------------------------------- #
# Обработчики команд
# --------------------------------------------------------------------------- #

HELP_TEXT = """🤖 *Управление Discord-авторегом*

*Авторег*
/старт — запустить авторег (все каналы)
/стоп — остановить авторег

*Статистика*
/стата — статистика регистраций

*Права в Discord*
/выдать `Discord_ID` — выдать права на старт/стоп Discord-пользователю
/убрать `Discord_ID` — забрать права
/права — список пользователей с выданными правами

*Прочее*
/статус — текущее состояние
/команды — это сообщение"""


def is_admin(user_id: int) -> bool:
    return user_id in TELEGRAM_ADMIN_IDS


async def handle_update(session: aiohttp.ClientSession, update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = int(msg.get("from", {}).get("id", 0))
    text = (msg.get("text") or "").strip()

    if not text:
        return

    # Команды достаём без учёта @botname
    cmd_raw = text.split()[0].lower().lstrip("/").split("@")[0]
    args = text[len(text.split()[0]):].strip() if len(text.split()) > 1 else ""

    # Проверка прав для служебных команд
    admin_cmds = {"старт", "стоп", "выдать", "убрать", "права", "статус",
                  "start", "stop", "grant", "revoke"}
    if cmd_raw in admin_cmds and not is_admin(user_id):
        await send_message(session, chat_id,
            "❌ У вас нет прав. Только администраторы могут управлять авторегом.")
        return

    if cmd_raw in ("start", "команды", "help"):
        await send_message(session, chat_id, HELP_TEXT)

    elif cmd_raw in ("старт", "start_bot"):
        push_tg_command("старт все")
        await send_message(session, chat_id,
            "✅ Команда *старт все* отправлена Discord-боту.\n"
            "Discord-бот получит её в течение нескольких секунд.")

    elif cmd_raw in ("стоп", "stop"):
        push_tg_command("енд")
        await send_message(session, chat_id,
            "🛑 Команда *енд* отправлена Discord-боту.")

    elif cmd_raw == "стата":
        await send_message(session, chat_id, registration_stats_text())

    elif cmd_raw in ("статус", "status"):
        granted = list_discord_access()
        pending = load_tg_commands().get("pending", [])
        text_out = (
            f"📡 *Состояние бота*\n"
            f"Пользователей с Discord-правами: *{len(granted)}*\n"
            f"Ожидающих команд в очереди: *{len(pending)}*\n"
            f"Файл статистики: `{STATS_FILE}`\n"
            f"Файл прав: `{COMMAND_PERMISSIONS_FILE}`"
        )
        await send_message(session, chat_id, text_out)

    elif cmd_raw == "права":
        granted = list_discord_access()
        if not granted:
            await send_message(session, chat_id,
                "ℹ️ Нет пользователей с выданными Discord-правами на старт/стоп.")
        else:
            ids_text = "\n".join(f"• `{uid}`" for uid in granted)
            await send_message(session, chat_id,
                f"👥 *Пользователи с правами на старт/стоп в Discord:*\n{ids_text}")

    elif cmd_raw == "выдать":
        # Принимаем Discord ID: /выдать 123456789
        id_match = re.search(r"\d{5,22}", args)
        if not id_match:
            await send_message(session, chat_id,
                "Формат: `/выдать 123456789012345678`\n"
                "Где число — Discord ID пользователя (17–19 цифр).")
            return
        target_id = int(id_match.group(0))
        ok = grant_discord_access(target_id)
        if ok:
            await send_message(session, chat_id,
                f"✅ Discord-пользователю `{target_id}` выданы права на `старт` и `стоп`.")
        else:
            await send_message(session, chat_id,
                f"ℹ️ У пользователя `{target_id}` уже есть права.")

    elif cmd_raw == "убрать":
        id_match = re.search(r"\d{5,22}", args)
        if not id_match:
            await send_message(session, chat_id,
                "Формат: `/убрать 123456789012345678`")
            return
        target_id = int(id_match.group(0))
        ok = revoke_discord_access(target_id)
        if ok:
            await send_message(session, chat_id,
                f"✅ У пользователя `{target_id}` забраны права на `старт` и `стоп`.")
        else:
            await send_message(session, chat_id,
                f"ℹ️ У пользователя `{target_id}` не было выданных прав.")

    else:
        # Неизвестная команда — молчим
        pass


# --------------------------------------------------------------------------- #
# Polling loop
# --------------------------------------------------------------------------- #

async def polling_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN не задан — Telegram-бот не запущен.")
        return

    if not TELEGRAM_ADMIN_IDS:
        log.warning(
            "TELEGRAM_ADMIN_IDS не задан — все пользователи смогут управлять ботом! "
            "Добавьте свой Telegram user_id в Railway Variables."
        )

    log.info("Telegram-бот запущен. Admins: %s", TELEGRAM_ADMIN_IDS)
    offset = 0
    timeout = 30

    async with aiohttp.ClientSession() as session:
        # Сначала сброс старых апдейтов
        await tg_request(session, "getUpdates", offset=-1, timeout=1)

        while True:
            try:
                resp = await tg_request(
                    session, "getUpdates",
                    offset=offset,
                    timeout=timeout,
                    allowed_updates=["message", "edited_message"],
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
