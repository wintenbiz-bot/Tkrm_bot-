import asyncio
import copy
from contextvars import ContextVar
from collections.abc import Mapping
import io
import json
import logging
import math
import os
import random
import time
import traceback
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from profit_tracker import ProfitTracker
from sheets_sync import sync_trades_to_sheet
from monochrome_data import (
    DEFAULT_MONOCHROME_FILE,
    load_monochrome_pairs,
    resolve_matches_path,
)

import httpx
from PIL import Image, ImageDraw

from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineQuery
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    WebAppInfo,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)

# Создаем сам логгер
logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

# Секрет НЕ хранится в репозитории — только в переменных окружения Render/локально.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_env_users = os.getenv("USERS_FILE")
if _env_users:
    USERS_FILE = os.path.abspath(_env_users)  # если относительный — сделаем от cwd
else:
    USERS_FILE = os.path.join(BASE_DIR, "users.json")

_env_pnl = os.getenv("PNL_CACHE_FILE")
PNL_CACHE_FILE = (
    os.path.abspath(_env_pnl)
    if _env_pnl
    else os.path.join(BASE_DIR, "pnl_cache.json")
)

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. "
        "Установи переменную окружения BOT_TOKEN."
    )

# ============================================================
# WHITELIST — кому разрешено пользоваться ботом
# ============================================================

ALLOWED_USER_IDS = {
    1591310608,
    839742411,
}

# ============================================================
# ТЕНЕВАЯ ЗАДЕРЖКА (SHADOW DELAY)
# ============================================================
# Скрытая задержка перед обработкой матча.
# Тормозит И действие (BUY/OFFER), И уведомление в Telegram,
# потому что уведомление отправляется ПОСЛЕ process_trade().
#
# Нигде в UI, WebApp и users.json не отображается.

SHADOW_DELAYS: dict[int, float] = {
    #839742411: 3.0,     # <-- теневой пользователь
    # 1591310608: 3.0,  # <-- при необходимости
}

# ============================================================
# ОРДЕРА: ЗАБЛОКИРОВАННЫЕ ПОЛЬЗОВАТЕЛИ
# ============================================================
# Telegram-ID, которым недоступны любые операции с ордерами:
#   • 🚀 Запустить ордера
#   • 🗑 Снять все ордера
#   • тестовые команды ордеров
#   • авто-обновление ордеров
#
# Пользователь видит прямое сообщение "функция недоступна".

ORDERS_BLOCKED_USERS: set[int] = {
    #839742411,   # <-- кому отключаем ордера
    # 1591310608,
}


def orders_blocked(session) -> bool:
    return session.telegram_id in ORDERS_BLOCKED_USERS

# ============================================================
# API ENDPOINTS
# ============================================================

SALING_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"

# Обмен initData Telegram WebApp на MRKT access token.
# Именно это позволяет Mini App получать токен без F12.
AUTH_URL = "https://api.tgmrkt.io/api/v1/auth"

FEED_URL = "https://api.tgmrkt.io/api/v1/feed"
BUY_URL = "https://api.tgmrkt.io/api/v1/gifts/buy"
OFFER_URL = "https://api.tgmrkt.io/api/v1/offers/create"
BALANCE_URL = "https://api.tgmrkt.io/api/v1/balance"
GIFTS_URL = "https://api.tgmrkt.io/api/v1/gifts"
MODELS_URL = "https://api.tgmrkt.io/api/v1/gifts/models"

CDN_BASE_URL = "https://cdn.tgmrkt.io/"


# ============================================================
# НАСТРОЙКИ СНАЙПИНГА (ДИНАМИЧЕСКИЕ ИЗ WEBAPP)
# ============================================================

DEFAULT_SETTINGS = {
    # Основные тумблеры
    "auto_buy": False,
    "auto_offer": False,
    "notifications": True,
    "dedup": True,
    # Множители и лимиты цен
    "max_multiplier": 1.02,
    "min_price": 0.0,
    "max_price": 0.0,
    "offer_multiplier": 0.98,
    "order_discount_1": 30.0,
    "order_discount_2": 50.0,
    # Фильтры событий
    "notifications_black_onyx": True,
    "skip_lucky_buy": True,
    "skip_purchase": True,
    "skip_withdraw": True,
    "skip_remove_sale": True,
    "skip_unlisted": True,
    "skip_cancel_sale": True,
    "skip_inactive": True,
    # Системные
    "poll_interval": 0.5,
    "haptic": True,
    # Авто-обновление ордеров
    "auto_refresh_orders": False,
    "auto_refresh_interval": 300.0,
}

# ============================================================
# МОНИТОРИНГ FEED — ОГРАНИЧЕННОЕ ОКНО
# ============================================================
# По умолчанию бот НЕ опрашивает MRKT feed.
# Пользователь включает мониторинг вручную (Mini App / команда /feed),
# и через 30 минут он отключается сам.

FEED_WINDOW_SECONDS = 30 * 60
MAX_FEED_WINDOW_SECONDS = 60 * 60

# Устаревшие настройки, которые раньше сохранялись, но ни на что
# не влияли. При загрузке users.json они вычищаются из памяти,
# чтобы не путать и не раздувать файл.
LEGACY_SETTINGS_KEYS = {
    "max_offer",
    "round_offers",
    "skip_sold",
    "cooldown",
    "fast_mode",
    "dark_mode",
}


# ============================================================
# ДЕНЕЖНЫЕ КОНСТАНТЫ
# ============================================================

NANO_TON = 1_000_000_000

# 0.01 TON = 10_000_000 nanoTON.
#
# По твоим наблюдениям MRKT:
# 14.49 -> минимум 7.25
# 17.15 -> минимум 8.58
#
# Поэтому минимальный offer округляется ВВЕРХ
# до 0.01 TON.
PRICE_STEP_NANO = 10_000_000


# ============================================================
# HTTP HEADERS
# ============================================================

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru,en-US;q=0.9,en;q=0.8,uk;q=0.7",
    "content-type": "application/json",
    "origin": "https://cdn.tgmrkt.io",
    "referer": "https://cdn.tgmrkt.io/",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Opera GX";v="135", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36 "
        "OPR/135.0.0.0"
    ),
}

# ============================================================
# MULTI-USER: ПОЛЬЗОВАТЕЛИ, ТОКЕНЫ И ИЗОЛИРОВАННОЕ СОСТОЯНИЕ
# ============================================================

CURRENT_SESSION = ContextVar("current_session", default=None)
USER_SESSIONS = {}
USER_POLL_TASKS = {}
USER_ORDER_REFRESH_TASKS = {}


class UserSession:
    def __init__(
        self,
        telegram_id: int,
        auth_token: str = "",
        settings: dict | None = None,
    ):
        self.telegram_id = int(telegram_id)
        self.auth_token = auth_token.strip()
        self.settings = copy.deepcopy(settings or DEFAULT_SETTINGS)

        # URL Apps Script Web App для Google Таблицы этого пользователя.
        self.apps_script_url = ""

        # Отдельное состояние sniper/feed.
        self.seen_item_ids = set()
        self.floor_cache = {}

        # Отдельный balance cache.
        self.cached_balance_nano = None
        self.cached_balance_ts = 0.0

        # Отдельная защита торговой секции.
        self.reserved_balance_nano = 0
        self.trade_lock = asyncio.Lock()

        # Ограничение одновременных уведомлений (рендер картинок + Telegram)
        self.notify_sem = asyncio.Semaphore(3)

        # Отдельный флаг массовой установки ордеров.
        self.orders_running = False

        # Ручные покупки из inline-кнопки «Купить».
        # token -> {sale_id, price_nano, ...}
        self.pending_buys = {}

        # Отдельные API-сервисы с headers этого пользователя.
        self.orders_manager = None
        self.profit_tracker = None

        # -----------------------------------------------------
        # ЗДОРОВЬЕ АВТОРИЗАЦИИ
        # -----------------------------------------------------
        # True  = токен живой, фоновые циклы работают.
        # False = MRKT вернул 401, циклы должны остановиться.
        self.auth_ok = True

        # Сколько 401 подряд пришло. Нужно, чтобы не убивать
        # сессию из-за одного случайного сбоя MRKT.
        self._auth_fail_count = 0

        # Отправили ли пользователю уведомление о протухшем токене.
        self._auth_notified = False

        # -----------------------------------------------------
        # FEED: окно мониторинга (0 = выключено по умолчанию).
        # Время хранится как Unix timestamp (time.time()).
        # -----------------------------------------------------
        self.feed_enabled_until = 0.0
        self._feed_expiry_notified = False

    # ---------------------------------------------------------
    # FEED: включается только вручную и сам гаснет через 30 мин
    # ---------------------------------------------------------
    def feed_active(self) -> bool:
        """True, пока окно мониторинга ещё открыто и токен жив."""
        return bool(
            self.auth_token
            and self.auth_ok
            and time.time() < self.feed_enabled_until
        )

    def feed_seconds_left(self) -> int:
        return max(
            0,
            int(self.feed_enabled_until - time.time()),
        )

    def enable_feed(self, seconds: int = FEED_WINDOW_SECONDS) -> None:
        self.feed_enabled_until = time.time() + max(
            1,
            min(int(seconds), MAX_FEED_WINDOW_SECONDS),
        )
        self._feed_expiry_notified = False

    def disable_feed(self) -> None:
        self.feed_enabled_until = 0.0
        self._feed_expiry_notified = False

    # ---------------------------------------------------------
    # Пометить сессию как «токен мёртв» (вызывается из API-слоя)
    # ---------------------------------------------------------
    def mark_auth_expired(self, source: str = "unknown") -> bool:
        """
        Возвращает True, если статус ИЗМЕНИЛСЯ с живого на мёртвый.
        Это гарантирует, что уведомление и лог будут ровно один раз.
        """
        self._auth_fail_count += 1

        # Порог: 2 подряд 401, чтобы не реагировать на единичный сбой
        if self._auth_fail_count < 2:
            return False

        if not self.auth_ok:
            # Уже помечали ранее — ничего не делаем повторно
            return False

        self.auth_ok = False
        logger.warning(
            f"🔒 [user {self.telegram_id}] AUTH_TOKEN просрочен "
            f"(source={source}). Фоновые циклы остановлены."
        )
        return True

    # ---------------------------------------------------------
    # Сбросить состояние при успешном ответе API
    # ---------------------------------------------------------
    def mark_auth_ok(self):
        self._auth_fail_count = 0
        # auth_ok НЕ трогаем: если уже мёртв — только /auth оживит

    @property
    def headers(self) -> dict:
        """Создаёт headers именно для этого MRKT аккаунта."""
        headers = dict(BASE_HEADERS)
        headers["authorization"] = self.auth_token
        headers["cookie"] = f"access_token={self.auth_token}"
        return headers

    def ensure_services(self):
        if self.orders_manager is None:
            self.orders_manager = OrdersManager(
                headers=self.headers,
                monochrome_file=DEFAULT_MONOCHROME_FILE,
            )

        if self.profit_tracker is None:
            self.profit_tracker = ProfitTracker(
                headers=self.headers,
                telegram_id=self.telegram_id,
            )


class HeadersProxy(Mapping):
    """Сохраняет старый интерфейс HEADERS, но делает его user-specific."""

    def _headers(self):
        return get_current_session().headers

    def __getitem__(self, key):
        return self._headers()[key]

    def get(self, key, default=None):
        return self._headers().get(key, default)

    def items(self):
        return self._headers().items()

    def keys(self):
        return self._headers().keys()

    def values(self):
        return self._headers().values()

    def __iter__(self):
        return iter(self._headers())

    def __len__(self):
        return len(self._headers())

    def __contains__(self, key):
        return key in self._headers()


class SettingsProxy:
    """Старый SETTINGS, но фактически настройки текущего пользователя."""
    def __contains__(self, key):
        return key in self._settings()

    def _settings(self):
        return get_current_session().settings

    def __getitem__(self, key):
        return self._settings()[key]

    def get(self, key, default=None):
        return self._settings().get(key, default)

    def __setitem__(self, key, value):
        self._settings()[key] = value
        save_users()

    def items(self):
        return self._settings().items()


class SessionSetProxy:
    def _set(self):
        return get_current_session().seen_item_ids

    def add(self, value):
        self._set().add(value)

    def discard(self, value):
        self._set().discard(value)

    def __contains__(self, value):
        return value in self._set()

    def __len__(self):
        return len(self._set())

    def __iter__(self):
        return iter(self._set())


class SessionMapProxy:
    def __init__(self, attr):
        self.attr = attr

    def _map(self):
        return getattr(get_current_session(), self.attr)

    def get(self, key, default=None):
        return self._map().get(key, default)

    def __getitem__(self, key):
        return self._map()[key]

    def __setitem__(self, key, value):
        self._map()[key] = value


class SessionLockProxy:
    async def __aenter__(self):
        return await get_current_session().trade_lock.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        return await get_current_session().trade_lock.__aexit__(
            exc_type,
            exc,
            tb,
        )


class ManagerProxy:
    def __init__(self, attr):
        self.attr = attr

    def __getattr__(self, name):
        service = getattr(get_current_session(), self.attr)
        return getattr(service, name)


class OrdersManagerProxy(ManagerProxy):
    def __init__(self):
        super().__init__("orders_manager")


class ProfitTrackerProxy(ManagerProxy):
    def __init__(self):
        super().__init__("profit_tracker")


HEADERS = HeadersProxy()
SETTINGS = SettingsProxy()


def get_current_session() -> UserSession:
    session = CURRENT_SESSION.get()

    if session is None:
        raise RuntimeError(
            "Не установлен контекст пользователя. "
            "Функция вызвана вне Telegram handler/feed task."
        )

    session.ensure_services()
    return session


def set_current_session(session: UserSession):
    return CURRENT_SESSION.set(session)


def reset_current_session(token):
    CURRENT_SESSION.reset(token)


def load_users():
    """Загружает пользователей из локального users.json."""
    if not os.path.exists(USERS_FILE):
        return

    try:
        with open(
            USERS_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        for user_id, user_data in data.items():
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings.update(
                user_data.get("settings") or {}
            )

            # Чистим устаревшие ключи из старых users.json.
            for legacy_key in LEGACY_SETTINGS_KEYS:
                settings.pop(legacy_key, None)

            session = UserSession(
                telegram_id=int(user_id),
                auth_token=str(user_data.get("auth_token") or ""),
                settings=settings,
            )
            session.apps_script_url = str(
                user_data.get("apps_script_url") or ""
            )

            # Если окно мониторинга уже истекло — не воскрешаем его.
            stored_until = float(
                user_data.get("feed_enabled_until") or 0
            )
            session.feed_enabled_until = (
                stored_until if stored_until > time.time() else 0.0
            )

            USER_SESSIONS[int(user_id)] = session

        print(
            f"👥 Загружено пользователей: "
            f"{len(USER_SESSIONS)}"
        )

    except Exception as e:
        print(
            f"⚠️ Ошибка загрузки {USERS_FILE}: {e}"
        )


def save_users():
    """Сохраняет токены и настройки всех пользователей."""
    data = {}

    for user_id, session in USER_SESSIONS.items():
        data[str(user_id)] = {
            "auth_token": session.auth_token,
            "apps_script_url": session.apps_script_url,
            "settings": session.settings,
            "feed_enabled_until": session.feed_enabled_until,
        }

    tmp_file = f"{USERS_FILE}.tmp"

    try:
        with open(
            tmp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            tmp_file,
            USERS_FILE,
        )

    except Exception as e:
        print(
            f"⚠️ Ошибка сохранения {USERS_FILE}: {e}"
        )

def save_pnl_cache(telegram_id: int, monthly_stats: dict):
    """Сохраняет P&L для WebApp (читает server.py)."""
    try:
        all_data = {}
        if os.path.exists(PNL_CACHE_FILE):
            with open(PNL_CACHE_FILE, "r", encoding="utf-8") as f:
                all_data = json.load(f)

        serialized = {
            month_key: {
                "profit_ton": data.get("profit_nano", 0) / NANO_TON,
                "sales_count": data.get("sales_count", 0),
            }
            for month_key, data in monthly_stats.items()
        }

        all_data[str(telegram_id)] = serialized

        tmp = f"{PNL_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PNL_CACHE_FILE)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения P&L: {e}")

from collections import defaultdict


def build_trades_for_sheet(raw_history: list) -> list:
    """
    Превращает сырую историю MRKT в список сделок для Google Sheets.

    Логика:
    - группируем события по номеру подарка (gift.number)
    - по каждому подарку ищем первую покупку и первую продажу
    - если есть продажа → статус "Продано", иначе "Не Продано"
    - возвращаем по одной строке на каждый подарок
    """
    gifts = defaultdict(list)

    for item in raw_history:
        gift = item.get("gift") or {}

        number = str(
            gift.get("number")
            or gift.get("giftNumber")
            or ""
        ).strip().lstrip("#")

        if not number:
            continue

        event_type = str(item.get("type", "")).lower()
        price_nano = int(item.get("price") or item.get("amount") or 0)
        raw_date = item.get("date")

        collection = (
            gift.get("collectionName")
            or gift.get("title")
            or "Подарок"
        )

        title_full = f"{collection} #{number}"

        gifts[number].append({
            "type": event_type,
            "price_nano": price_nano,
            "date": raw_date,
            "title": title_full,
        })

    BUY_TYPES = {
        "buy",
        "purchase",
        "accept_offer",
        "mint",
        "buy_offer",
        "lucky_buy_gift",
    }
    SELL_TYPES = {
        "sale",
        "sell",
        "sold",
        "sell_offer",
    }

    trades = []

    for number, events in gifts.items():
        events.sort(key=lambda x: x.get("date") or "")

        buy_event = None
        sell_event = None

        for ev in events:
            t = ev["type"]
            if t in BUY_TYPES and ev["price_nano"] > 0 and buy_event is None:
                buy_event = ev
            elif t in SELL_TYPES and ev["price_nano"] > 0 and sell_event is None:
                sell_event = ev

        if not buy_event and not sell_event:
            continue

        title = events[0]["title"]

        if sell_event and buy_event:
            trades.append({
                "title": title,
                "date": sell_event["date"],
                "buy_ton": buy_event["price_nano"] / NANO_TON,
                "sell_ton": sell_event["price_nano"] / NANO_TON,
                "commission": 0,
                "status": "Продано",
            })
        elif buy_event:
            trades.append({
                "title": title,
                "date": buy_event["date"],
                "buy_ton": buy_event["price_nano"] / NANO_TON,
                "sell_ton": 0,
                "commission": 0,
                "status": "Не Продано",
            })
        elif sell_event:
            trades.append({
                "title": title,
                "date": sell_event["date"],
                "buy_ton": 0,
                "sell_ton": sell_event["price_nano"] / NANO_TON,
                "commission": 0,
                "status": "Продано",
            })

    return trades

def get_or_create_session(
    telegram_id: int,
) -> UserSession:
    telegram_id = int(telegram_id)

    session = USER_SESSIONS.get(
        telegram_id
    )

    if session is None:
        session = UserSession(
            telegram_id=telegram_id
        )
        USER_SESSIONS[telegram_id] = session
        save_users()

    return session


def get_session_from_event(event) -> UserSession:
    user = getattr(
        event,
        "from_user",
        None,
    )

    if user is None:
        raise RuntimeError(
            "Telegram event не содержит from_user."
        )

    return get_or_create_session(
        user.id
    )


class SessionMiddleware:
    """Устанавливает текущего пользователя для Telegram handler."""

    async def __call__(
        self,
        handler,
        event,
        data,
    ):
        session = get_session_from_event(
            event
        )

        token = set_current_session(
            session
        )

        try:
            return await handler(
                event,
                data,
            )
        finally:
            reset_current_session(
                token
            )

class WhitelistMiddleware:
    """Блокирует всех, кого нет в ALLOWED_USER_IDS."""

    async def __call__(
        self,
        handler,
        event,
        data,
    ):
        user = getattr(event, "from_user", None)

        if user is None:
            return

        if user.id not in ALLOWED_USER_IDS:
            print(
                f"🚫 Заблокирован доступ: "
                f"id={user.id} | "
                f"username=@{user.username} | "
                f"name={user.full_name}"
            )

            # Молча игнорируем — бот ничего не отвечает
            try:
                await event.answer(
                    "🔒 Доступ к боту закрыт."
                )
            except Exception:
                pass

            return

        return await handler(event, data)


async def start_user_polling(
    telegram_id: int,
):
    """Запускает отдельный feed loop для пользователя."""
    session = get_or_create_session(
        telegram_id
    )

    # Feed по умолчанию ВЫКЛЮЧЕН. Запускаем только вручную
    # включённое окно мониторинга.
    if not session.feed_active():
        logger.info(
            f"⏸ [user {telegram_id}] feed-опрос не запущен: "
            f"окно мониторинга не активно "
            f"(до конца: {session.feed_seconds_left()}s)"
        )
        return

    old_task = USER_POLL_TASKS.get(
        telegram_id
    )

    if old_task and not old_task.done():
        return

    task = asyncio.create_task(
        poll_feed_loop(
            telegram_id
        )
    )

    USER_POLL_TASKS[telegram_id] = task


async def stop_user_polling(
    telegram_id: int,
):
    task = USER_POLL_TASKS.pop(
        int(telegram_id),
        None,
    )

    if task and not task.done():
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


# ============================================================
# АВТО-ОБНОВЛЕНИЕ ОРДЕРОВ ПРИ ИЗМЕНЕНИИ ФЛОРА
# ============================================================

async def start_order_refresh_loop(telegram_id: int):
    """Запускает фоновый loop авто-обновления ордеров для пользователя."""
    session = get_or_create_session(telegram_id)

    if not session.auth_token:
        return

    # --- блокировка ордеров ---
    if orders_blocked(session):
        logger.info(
            f"🚫 [orders-blocked] user={telegram_id} — "
            f"order_refresh_loop не запускается"
        )
        return
    # --- /блокировка ордеров ---

    old_task = USER_ORDER_REFRESH_TASKS.get(telegram_id)
    if old_task and not old_task.done():
        return

    task = asyncio.create_task(order_refresh_loop(telegram_id))
    USER_ORDER_REFRESH_TASKS[telegram_id] = task


async def stop_order_refresh_loop(telegram_id: int):
    task = USER_ORDER_REFRESH_TASKS.pop(int(telegram_id), None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ============================================================
# ЕДИНАЯ ТОЧКА СМЕНЫ ТОКЕНА И УПРАВЛЕНИЯ FEED
# ============================================================

async def apply_auth_token(
    telegram_id: int,
    token: str,
    source: str = "manual",
) -> UserSession:
    """
    Привязывает свежий MRKT-токен к Telegram-пользователю.

    Используется и /auth, и inline-запросом из Mini App,
    и внутренним API сервера. Feed НЕ включается автоматически —
    только если окно мониторинга уже было открыто пользователем.
    """
    telegram_id = int(telegram_id)
    token = str(token or "").strip()

    session = get_or_create_session(telegram_id)

    await stop_user_polling(telegram_id)
    await stop_order_refresh_loop(telegram_id)

    session.auth_token = token
    session.orders_manager = None
    session.profit_tracker = None

    session.cached_balance_nano = None
    session.cached_balance_ts = 0.0
    session.seen_item_ids.clear()
    session.pending_buys.clear()
    session.floor_cache.clear()
    session.reserved_balance_nano = 0

    # Токен свежий — даём фоновым циклам ещё один шанс.
    session.auth_ok = True
    session._auth_fail_count = 0
    session._auth_notified = False

    save_users()

    # Мониторинг не включаем сами: пользователь должен нажать кнопку.
    if session.feed_active():
        await start_user_polling(telegram_id)

    if session.settings.get("auto_refresh_orders", False):
        await start_order_refresh_loop(telegram_id)

    logger.info(
        f"🔑 [user {telegram_id}] AUTH_TOKEN обновлён "
        f"(source={source}), feed_active={session.feed_active()}"
    )
    return session


async def enable_feed_monitoring(
    telegram_id: int,
    seconds: int = FEED_WINDOW_SECONDS,
) -> UserSession:
    """Включает опрос feed на ограниченное время."""
    session = get_or_create_session(int(telegram_id))

    if not session.auth_token:
        raise ValueError("Сначала подключи MRKT-аккаунт (AUTH_TOKEN).")

    # Пользователь явно включил мониторинг — даём токену ещё один шанс.
    # Если он действительно протух, get_feed() снова пометит сессию.
    session.auth_ok = True
    session._auth_fail_count = 0
    session._auth_notified = False

    session.enable_feed(seconds)
    save_users()

    await start_user_polling(session.telegram_id)

    logger.info(
        f"▶️ [user {session.telegram_id}] feed включён на "
        f"{session.feed_seconds_left()}s"
    )
    return session


async def disable_feed_monitoring(
    telegram_id: int,
) -> UserSession:
    """Немедленно выключает опрос feed."""
    session = get_or_create_session(int(telegram_id))
    session.disable_feed()
    save_users()

    await stop_user_polling(session.telegram_id)

    logger.info(f"⏹ [user {session.telegram_id}] feed выключен вручную")
    return session


def apply_settings_dict(session: UserSession, data: dict) -> None:
    """Безопасное применение настроек из Mini App / API."""
    numeric_keys = {
        "max_multiplier", "offer_multiplier",
        "min_price", "max_price",
        "poll_interval", "auto_refresh_interval",
        "order_discount_1", "order_discount_2",
    }

    for key, value in (data or {}).items():
        if key not in session.settings:
            continue

        if key in numeric_keys:
            try:
                session.settings[key] = float(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(value, bool):
            session.settings[key] = value

    # Защита от слишком частых запросов к MRKT.
    session.settings["poll_interval"] = max(
        0.2,
        float(session.settings.get("poll_interval") or 0.2),
    )
    session.settings["auto_refresh_interval"] = max(
        30.0,
        float(session.settings.get("auto_refresh_interval") or 30.0),
    )

    save_users()


async def order_refresh_loop(telegram_id: int):
    """Периодически проверяет флор и переставляет ордера при изменении."""
    session = get_or_create_session(telegram_id)
    token = set_current_session(session)

    try:
        print(f"🔄 [user {telegram_id}] Order refresh loop запущен.")

        timeout = httpx.Timeout(
            connect=5.0,
            read=15.0,
            write=10.0,
            pool=10.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:

                # Если токен мёртв — выходим
                if not session.auth_ok:
                    logger.info(
                        f"🛑 [user {telegram_id}] "
                        f"order_refresh_loop остановлен: "
                        f"AUTH_TOKEN недействителен."
                    )
                    return

                # --- блокировка ордеров ---
                if orders_blocked(session):
                    logger.info(
                        f"🛑 [orders-blocked] user={telegram_id} — "
                        f"order_refresh_loop останавливается."
                    )
                    return
                # --- /блокировка ордеров ---

                try:
                    if SETTINGS.get("auto_refresh_orders", False):
                        result = await orders_mgr.refresh_orders_if_floor_changed(
                            client=client,
                            file_path="orders_to_create.json",
                            discount_1=SETTINGS.get("order_discount_1", 30.0),
                            discount_2=SETTINGS.get("order_discount_2", 50.0),
                        )
                        if result.get("changed", 0) > 0:
                            logger.info(
                                f"🔄 [user {telegram_id}] Обновление ордеров: "
                                f"checked={result.get('checked')}, "
                                f"changed={result.get('changed')}, "
                                f"cancelled={result.get('orders_cancelled')}, "
                                f"created={result.get('orders_created')}, "
                                f"errors={result.get('errors')}"
                            )

                    interval = max(
                        30.0,
                        SETTINGS.get("auto_refresh_interval", 300.0),
                    )
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    logger.warning(
                        f"⚠️ [user {telegram_id}] "
                        f"Ошибка order_refresh_loop: {e}"
                    )
                    await asyncio.sleep(30)

    finally:
        reset_current_session(token)
        USER_ORDER_REFRESH_TASKS.pop(telegram_id, None)


load_users()

# ============================================================
# МЕНЕДЖЕР ОРДЕРОВ (ORDERS MANAGER)
# ============================================================
from orders_manager import OrdersManager

# Инициализируем менеджер с твоими HEADERS и файлом монохромов
orders_mgr = OrdersManagerProxy()

# ============================================================
# MONOCHROME GUARD
# ============================================================
from monochrome_guard import get_guard

monochrome_guard = get_guard()

# ============================================================
# FEED PAYLOAD
# ============================================================

PAYLOAD = {
    "count": 20,
    "cursor": "",
    "collectionNames": [],
    "modelNames": [],
    "backdropNames": [],
    "ordering": "Latest",
    "lowToHigh": False,
}


# ============================================================
# BOT
# ============================================================

profit_tracker = ProfitTrackerProxy()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Сначала whitelist — блокирует левых
dp.message.middleware(WhitelistMiddleware())
dp.callback_query.middleware(WhitelistMiddleware())

# Потом сессия — для тех, кто прошёл
dp.message.middleware(SessionMiddleware())
dp.callback_query.middleware(SessionMiddleware())


# ============================================================
# DEDUP
# ============================================================

seen_item_ids = SessionSetProxy()

# Максимальное количество ID в памяти.
# Чтобы бот не рос бесконечно при долгой работе.
MAX_SEEN_IDS = 50_000


# ============================================================
# WEB APP
# ============================================================

# Render сам задаёт RENDER_EXTERNAL_URL — используем его как разумный дефолт.
_render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "").strip()
    or _render_url
    or "https://winten226-byte.github.io/mrkt-miniapp/"
).rstrip("/") + "/"

# Публичный адрес API (Render). Нужен, если Mini App лежит на GitHub Pages,
# а бот/API — на Render. Если совпадает с WEBAPP_URL — используется same-origin.
PUBLIC_API_URL = (
    os.getenv("PUBLIC_URL", "").strip() or _render_url
).rstrip("/")


def build_webapp_url(session: UserSession) -> str:
    """URL Mini App с начальными настройками и статусом пользователя."""
    import base64
    from urllib.parse import urlencode

    payload = json.dumps({
        "settings": session.settings,
        "state": {
            "auth_configured": bool(session.auth_token),
            "auth_ok": bool(session.auth_ok),
            "feed_enabled": session.feed_active(),
            "feed_seconds_left": session.feed_seconds_left(),
        },
    }).encode("utf-8")

    params = {
        "s": base64.urlsafe_b64encode(payload).decode(),
    }

    if (
        PUBLIC_API_URL
        and not WEBAPP_URL.startswith(PUBLIC_API_URL)
    ):
        params["api"] = PUBLIC_API_URL

    return f"{WEBAPP_URL}?{urlencode(params)}"


# ============================================================
# VALID MATCHES
# ============================================================

VALID_PAIRS = set()


# ============================================================
# ТОРГОВОЕ ЯДРО
# ============================================================

# Защищает критическую секцию BUY/OFFER.
TRADE_LOCK = SessionLockProxy()

# Баланс не нужно запрашивать несколько раз в секунду.
BALANCE_CACHE_TTL = 2.0

# Сколько живёт кнопка «Купить» под уведомлением.
MANUAL_BUY_TTL = 600.0


# ============================================================
# КЭШ FLOOR
# ============================================================

# key:
#   (collection_lower, model_lower)
#
# value:
#   (floor_nano, timestamp)
MODEL_FLOOR_CACHE = SessionMapProxy("floor_cache")

MODEL_FLOOR_CACHE_TTL = 30.0


# ============================================================
# КЛАВИАТУРА
# ============================================================

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📊 Профиль"),
            KeyboardButton(text="📈 Прибыль (P&L)"),
            KeyboardButton(text="⚙️ Настройки"),
        ],
        [
            KeyboardButton(text="🔍 Тест выборки (50)"),
        ],
        [
            KeyboardButton(text="🚀 Запустить ордера"),
            KeyboardButton(text="🗑 Снять все ордера"),
        ],
        [
            KeyboardButton(text="⏱ Feed 30 мин"),
            KeyboardButton(text="⏹ Feed стоп"),
        ],
    ],
    resize_keyboard=True,
)

# ============================================================
# ПАСХАЛКИ
# ============================================================

@dp.message(F.text == "/67")
async def cmd_67(message: Message):
    stages = [
        ("10%",  "🟩⬛️⬛️⬛️⬛️"),
        ("28%",  "🟩🟩⬛️⬛️⬛️"),
        ("42%",  "🟩🟩🟩⬛️⬛️"),
        ("67%",  "🟩🟩🟩🟩⬛️"),
        ("99%",  "🟩🟩🟩🟩🟩"),
    ]

    msg = await message.answer(
        "🟢 Инициализация виновных в кибер приступлении...\n"
        "———————————————\n"
        f"{stages[0][0]}\n{stages[0][1]}"
    )

    for pct, bar in stages[1:]:
        await asyncio.sleep(0.5)
        await msg.edit_text(
            "🟢 Инициализация виновных в кибер приступлении...\n"
            "———————————————\n"
            f"{pct}\n{bar}"
        )

    await asyncio.sleep(0.5)
    await msg.edit_text(
        "📝 Список виновных:\n"
        "1. Мария\n"
        "2. Богдан\n"
        "3. Василий\n\n"
        "🤙🤟Список ТОЧНО не виновных:\n"
        "Вячеслав Аka: Glory"
    )


@dp.message(F.text.lower() == "/арбуз")
async def cmd_arbuz(message: Message):
    await message.answer("Ты еблан?")

# ============================================================
# SHADOW DELAY
# ============================================================

async def apply_shadow_delay(context: str = "") -> None:
    """Невидимая задержка для конкретного пользователя."""
    session = get_current_session()
    delay = SHADOW_DELAYS.get(session.telegram_id, 0.0)

    if delay <= 0:
        return

    logger.debug(
        f"🕐 [shadow] user={session.telegram_id} "
        f"ctx={context} delay={delay:.2f}s"
    )

    await asyncio.sleep(delay)

# ============================================================
# ЛОГ ДЕЙСТВИЙ ПОЛЬЗОВАТЕЛЯ
# ============================================================

def log_user_action(session, action: str, extra: str = ""):
    """
    Пишет в консоль, кто и что нажал.
    Используется для «📊 Профиль» и «📈 Прибыль (P&L)».
    """
    user_id = session.telegram_id
    tail = f" | {extra}" if extra else ""
    logger.info(
        f"👤 [action] user={user_id} | {action}{tail}"
    )

# ============================================================
# MONEY HELPERS
# ============================================================

def to_nano(value) -> int:
    """
    Приведение значения к int nanoTON.

    В торговой логике используем только int.

    Важно:
    API MRKT для balance/feed/floor обычно отдаёт
    денежные значения в nanoTON, поэтому здесь мы
    не делаем преобразование nano -> TON.
    """

    if value is None:
        return 0

    if isinstance(value, bool):
        return 0

    try:
        if isinstance(value, int):
            return value

        if isinstance(value, float):
            return int(round(value))

        value_str = str(value).strip()

        if not value_str:
            return 0

        return int(Decimal(value_str))

    except Exception:
        return 0


def nano_to_ton(nano: int) -> float:
    """
    Только для отображения.
    Не использовать в торговых расчётах.
    """
    return nano / NANO_TON


def format_ton(nano) -> str:
    """
    Форматирование nanoTON -> TON для Telegram.
    Использует Decimal + ROUND_HALF_UP, чтобы избежать
    ошибок округления из-за float (12.345 -> 12.35, не 12.34).
    """
    try:
        if nano is None:
            return "0.00"
        nano_int = int(nano)
        value = Decimal(nano_int) / Decimal(NANO_TON)
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return "0.00"


def ceil_to_step_nano(
    value_nano: int,
    step_nano: int = PRICE_STEP_NANO,
) -> int:
    """
    Округление ВВЕРХ до заданного шага.

    Например:

    7.245 TON -> 7.25 TON
    8.575 TON -> 8.58 TON
    """

    if value_nano <= 0:
        return 0

    return (
        (value_nano + step_nano - 1)
        // step_nano
    ) * step_nano


def min_offer_for_sale(price_nano: int) -> int:
    """
    Минимальный offer согласно наблюдаемому правилу MRKT:

        минимум = 50% текущей цены лота

    Затем округляем ВВЕРХ до 0.01 TON.

    Примеры:

        14.49 -> 7.25
        17.15 -> 8.58
    """

    if price_nano <= 0:
        return 0

    # Точная половина с округлением вверх
    # на уровне nanoTON.
    half_nano = (price_nano + 1) // 2

    # Затем вверх до допустимого шага 0.01 TON.
    return ceil_to_step_nano(
        half_nano,
        PRICE_STEP_NANO,
    )


def calc_floor_buy_limit(
    floor_nano: int,
    multiplier: float,
) -> int:
    """
    Максимальная цена BUY относительно floor.

    Например:

        floor = 10 TON
        multiplier = 1.02

        limit = 10.20 TON
    """

    if floor_nano <= 0:
        return 0

    result = (
        Decimal(floor_nano)
        * Decimal(str(multiplier))
    )

    return int(
        result.to_integral_value(
            rounding=ROUND_DOWN
        )
    )


def calc_offer_price(
    floor_nano: int,
    multiplier: float,
) -> int:
    """
    Рассчитывает базовый offer от floor.

    Например:

        floor = 10 TON
        multiplier = 1.01

        базовый offer = 10.10 TON

    Сумма округляется вниз до шага 0.01 TON.

    После этого min_offer_for_sale()
    гарантирует соблюдение минимального offer MRKT.
    """

    if floor_nano <= 0:
        return 0

    result = (
        Decimal(floor_nano)
        * Decimal(str(multiplier))
    )

    raw_nano = int(
        result.to_integral_value(
            rounding=ROUND_DOWN
        )
    )

    return (
        raw_nano // PRICE_STEP_NANO
    ) * PRICE_STEP_NANO

# ============================================================
# BALANCE
# ============================================================

async def get_balance_nano(
    client: httpx.AsyncClient,
    force: bool = False,
) -> int:
    """
    Получает только баланс.

    В отличие от get_user_stats(),
    эта функция не делает запросы inventory/listings.

    Это важно для sniper path.
    """

    session = get_current_session()

    now = asyncio.get_running_loop().time()

    if (
        not force
        and session.cached_balance_nano is not None
        and now - session.cached_balance_ts < BALANCE_CACHE_TTL
    ):
        return session.cached_balance_nano

    try:
        res = await client.get(
            BALANCE_URL,
            headers=HEADERS,
        )

        # -------- 401 / 403: токен протух --------
        if res.status_code in (401, 403):
            changed = session.mark_auth_expired(source="balance")
            if changed:
                asyncio.create_task(
                    notify_auth_expired(session.telegram_id)
                )

            if session.cached_balance_nano is not None:
                return session.cached_balance_nano

            return 0

        if res.status_code != 200:
            logger.warning(
                f"⚠️ Баланс HTTP {res.status_code} "
                f"[user {session.telegram_id}]: {res.text[:200]}"
            )

            if session.cached_balance_nano is not None:
                return session.cached_balance_nano

            return 0

        # Успех — сбрасываем счётчик подряд идущих 401
        session.mark_auth_ok()

        data = res.json()

        hard_nano = (
            data.get("hard")
            or data.get("balance")
            or data.get("amount")
            or 0
        )

        balance_nano = to_nano(hard_nano)

        session.cached_balance_nano = balance_nano
        session.cached_balance_ts = now

        return balance_nano

    except Exception as e:
        print(
            f"⚠️ Ошибка получения баланса: {e}"
        )

        if session.cached_balance_nano is not None:
            return session.cached_balance_nano

        return 0


# ============================================================
# СТАТИСТИКА И ПРОФИЛЬ
# ============================================================

async def get_user_stats(
    client: httpx.AsyncClient,
) -> dict:

    balance_nano = await get_balance_nano(
        client,
        force=True,
    )

    inventory_count = 0
    listings_count = 0

    base_payload = {
        "count": 20,
        "cursor": "",
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "None",
        "lowToHigh": False,
    }

    # --------------------------------------------------------
    # INVENTORY
    # --------------------------------------------------------

    try:
        payload_vault = {
            **base_payload,
            "isListed": False,
        }

        res = await client.post(
            GIFTS_URL,
            headers=HEADERS,
            json=payload_vault,
        )

        if res.status_code == 200:
            data = res.json()

            inventory_count = data.get(
                "total",
                len(data.get("gifts", [])),
            )

    except Exception as e:
        print(
            f"⚠️ Ошибка получения инвентаря: {e}"
        )

    # --------------------------------------------------------
    # LISTINGS
    # --------------------------------------------------------

    try:
        payload_listed = {
            **base_payload,
            "isListed": True,
        }

        res = await client.post(
            GIFTS_URL,
            headers=HEADERS,
            json=payload_listed,
        )

        if res.status_code == 200:
            data = res.json()

            listings_count = data.get(
                "total",
                len(data.get("gifts", [])),
            )

    except Exception as e:
        print(
            f"⚠️ Ошибка получения активных лотов: {e}"
        )

    return {
        "balance_nano": balance_nano,
        "balance": nano_to_ton(balance_nano),
        "inventory": inventory_count,
        "listings": listings_count,
    }


# ============================================================
# /START
# ============================================================

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    session = get_current_session()

    if not session.auth_token:
        start_kb = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="🔑 Подключить через Mini App",
                        web_app=WebAppInfo(
                            url=build_webapp_url(session)
                        ),
                    ),
                ],
            ],
            resize_keyboard=True,
        )

        await message.answer(
            "👋 Привет!\n\n"
            "Нажми кнопку ниже и открой Mini App — "
            "бот сам получит MRKT-токен через официальный "
            "<code>/api/v1/auth</code>. F12 и ручное копирование "
            "больше не нужны.\n\n"
            "Резервный вариант: "
            "<code>/auth ТВОЙ_AUTH_TOKEN</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=start_kb,
        )
        return

    await message.answer(
        "👋 Бот готов к работе с твоим MRKT аккаунтом!\n"
        "Используй меню снизу:",
        reply_markup=main_reply_keyboard,
    )


@dp.message(F.text.startswith("/auth "))
async def cmd_auth(message: Message):
    session = get_current_session()

    token_value = message.text.split(
        " ",
        1,
    )[1].strip()

    if not token_value:
        await message.answer(
            "❌ Токен пустой. Используй: "
            "<code>/auth ТВОЙ_AUTH_TOKEN</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await apply_auth_token(
        session.telegram_id,
        token_value,
        source="cmd_auth",
    )

    await message.answer(
        "✅ AUTH_TOKEN сохранён и привязан "
        "к твоему Telegram аккаунту.\n\n"
        "Теперь этот пользователь работает "
        "полностью отдельно от остальных.\n\n"
        "Мониторинг feed по-прежнему выключен — "
        "включи его командой <code>/feed</code> "
        "или кнопкой в Mini App.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard,
    )


# ============================================================
# FEED: ВКЛЮЧЕНИЕ НА 30 МИНУТ / ВЫКЛЮЧЕНИЕ
# ============================================================

@dp.message(F.text.in_([
    "/feed",
    "/feed_on",
    "⏱ Feed 30 мин",
]))
async def cmd_feed_on(message: Message):
    session = get_current_session()

    if not session.auth_token:
        await message.answer(
            "🔐 Сначала подключи MRKT-аккаунт через Mini App "
            "или <code>/auth ТОКЕН</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await enable_feed_monitoring(
        session.telegram_id,
        FEED_WINDOW_SECONDS,
    )

    await message.answer(
        "▶️ <b>Мониторинг feed включён на 30 минут.</b>\n\n"
        "Бот будет опрашивать MRKT только в этом окне. "
        "По истечении 30 минут опрос остановится автоматически.\n\n"
        "Остановить раньше: <code>/feed_off</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard,
    )


@dp.message(F.text.in_([
    "/feed_off",
    "/feed_stop",
    "⏹ Feed стоп",
]))
async def cmd_feed_off(message: Message):
    session = get_current_session()
    await disable_feed_monitoring(session.telegram_id)

    await message.answer(
        "⏹ <b>Мониторинг feed остановлен.</b>\n\n"
        "MRKT больше не опрашивается. "
        "Включить снова: <code>/feed</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard,
    )


@dp.message(F.text == "/feed_status")
async def cmd_feed_status(message: Message):
    session = get_current_session()

    if session.feed_active():
        text = (
            f"▶️ Feed активен: ещё "
            f"<b>{session.feed_seconds_left() // 60} мин "
            f"{session.feed_seconds_left() % 60} сек</b>.\n"
            f"Интервал опроса: "
            f"{SETTINGS.get('poll_interval', 1.0)} сек."
        )
    else:
        text = (
            "⏸ Feed выключен. Бот не стучится в MRKT "
            "для проверки ленты.\n\n"
            "Включить на 30 минут: <code>/feed</code>"
        )

    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text.startswith("/sheet "))
async def cmd_set_sheet(message: Message):
    session = get_current_session()

    url = message.text.split(" ", 1)[1].strip()

    if not url.startswith("https://script.google.com/macros/s/"):
        await message.answer(
            "❌ Неверный URL.\n"
            "Он должен начинаться с "
            "<code>https://script.google.com/macros/s/</code> "
            "и заканчиваться на <code>/exec</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    session.apps_script_url = url
    save_users()

    await message.answer(
        "✅ URL Google Таблицы сохранён.\n"
        "Теперь при нажатии «📈 Прибыль (P&L)» "
        "сделки будут дописываться автоматически."
    )

@dp.message(
    F.text.in_([
        "/remove_token",
        "/logout",
    ])
)
async def cmd_remove_token(message: Message):
    session = get_current_session()

    await stop_user_polling(
        session.telegram_id
    )

    await stop_order_refresh_loop(
        session.telegram_id
    )

    session.auth_token = ""
    session.orders_manager = None
    session.profit_tracker = None

    session.cached_balance_nano = None
    session.cached_balance_ts = 0.0
    session.seen_item_ids.clear()
    session.pending_buys.clear()
    session.floor_cache.clear()
    session.reserved_balance_nano = 0

    # ---- Сбрасываем состояние авторизации ----
    session.auth_ok = True
    session._auth_fail_count = 0
    session._auth_notified = False

    save_users()

    await message.answer(
        "🔐 AUTH_TOKEN удалён у этого пользователя.\n"
        "Торговый feed остановлен.\n\n"
        "Чтобы привязать аккаунт снова: "
        "<code>/auth ТВОЙ_AUTH_TOKEN</code>",
        parse_mode=ParseMode.HTML,
    )

# ============================================================
# CANCEL ALL ORDERS COMMAND
# ============================================================

@dp.message(F.text.in_(["/cancel_orders", "/cancel_all_orders", "🗑 Снять все ордера"]))
async def cmd_cancel_all_orders(message: Message):
    session = get_current_session()

    if not session.auth_token:
        await message.answer(
            "🔐 Сначала привяжи AUTH_TOKEN через /auth."
        )
        return

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await message.answer("🚫 Функция ордеров недоступна")
        return
    # --- /блокировка ордеров ---

    msg = await message.answer(
        "🔄 Запрос отмены всех активных ордеров..."
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        count = await orders_mgr.cancel_all_orders(client)

    await asyncio.sleep(5)

    await msg.edit_text(
        f"✅ Успешно отменено ордеров: {count} шт.\n\n"
        f"⏳ Подожди ~10 секунд перед повторным запуском ордеров."
    )

# ============================================================
# /STATS / PROFILE
# ============================================================

@dp.message(
    F.text.in_(
        [
            "/stats",
            "/profile",
            "📊 Профиль",
        ]
    )
)
async def cmd_stats(message: Message):
    session = get_current_session()

    if not session.auth_token:
        await message.answer(
            "🔐 Сначала привяжи AUTH_TOKEN через /auth."
        )
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        stats = await get_user_stats(client)

    # --- лог в консоль ---
    log_user_action(
        session,
        action="📊 Профиль",
        extra=(
            f"balance={format_ton(stats['balance_nano'])} TON | "
            f"inventory={stats['inventory']} | "
            f"listings={stats['listings']}"
        ),
    )
    # --- /лог ---

    text = (
        "👤 <b>Статистика аккаунта MRKT:</b>\n\n"
        f"💳 <b>Баланс:</b> {format_ton(stats['balance_nano'])} TON\n"
        f"📦 <b>В хранилище:</b> {stats['inventory']} шт.\n"
        f"🏷 <b>Выставлено на продажу:</b> {stats['listings']} шт."
    )

    # Пытаемся получить аватарку
    photo_file_id = None

    try:
        photos = await bot.get_user_profile_photos(
            user_id=message.from_user.id,
            limit=1,
        )

        if photos.total_count > 0:
            # Берём самый большой размер
            sizes = photos.photos[0]
            photo_file_id = sizes[-1].file_id

    except Exception as e:
        print(f"⚠️ Не удалось получить аватарку: {e}")

    if photo_file_id:
        await message.answer_photo(
            photo=photo_file_id,
            caption=text,
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
        )

# ============================================================
# /GIFT — ИСТОРИЯ ПОДАРКА (ВЫНЕСЕННАЯ ЛОГИКА)
# ============================================================

async def _process_gift_history(message: Message, number: str):
    """Внутренняя функция для получения истории по номеру."""
    session = get_current_session()

    if not session.auth_token:
        await message.answer("🔐 Сначала привяжи AUTH_TOKEN через /auth.")
        return

    msg = await message.answer(
        f"🔄 Ищу историю подарка <b>#{number}</b>...",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Получаем P&L через штатный метод трекера
            info = await profit_tracker.find_gift_by_number(client, number)

            if not info or not info.get("events"):
                await msg.edit_text(
                    f"ℹ️ Подарок <b>#{number}</b> не найден в истории MRKT.\n\n"
                    "Возможные причины:\n"
                    "• Подарок не проходил через MRKT.\n"
                    "• Номер указан неверно.",
                    parse_mode=ParseMode.HTML,
                )
                return

            # 2. Запрашиваем сырую историю, чтобы вытащить модель и фон
            # (в info['events'] их нет, так как трекер их отсекает)
            raw_history = await profit_tracker.fetch_all_history(client)
            
            target_gift = None
            for ev in raw_history:
                g = ev.get("gift") or {}
                # Сравниваем номера как строки, очищая от #
                num = str(g.get("number") or "").strip().lstrip("#")
                if num == number.strip().lstrip("#"):
                    target_gift = g
                    break

            coll_name = info.get("collection") or ""
            mod_name = ""
            back_name = ""

            if target_gift:
                coll_name = target_gift.get("collectionName") or target_gift.get("title") or coll_name
                mod_name = target_gift.get("modelName") or ""
                back_name = target_gift.get("backdropName") or ""

            # 3. Запрашиваем монохромный флор (с эндпоинта /saling)
            mono_floor_nano = 0
            if coll_name and mod_name:
                try:
                    mono_floor_nano = await monochrome_guard.get_monochrome_floor(
                        client=client,
                        headers=HEADERS,
                        collection=coll_name,
                        model=mod_name,
                    )
                except Exception as e:
                    logger.warning(f"⚠️ mono_floor error {coll_name}/{mod_name}: {e}")

            # --- Формирование сообщения ---
            coll = info.get("collection") or info.get("title") or "—"

            lines = [
                f"🎁 <b>Подарок #{info['number']}</b>",
                f"🏆 <b>Коллекция:</b> {coll}",
            ]
            
            if mod_name:
                lines.append(f"🎨 <b>Модель:</b> {mod_name}")
            if back_name:
                lines.append(f"🖼 <b>Фон:</b> {back_name}")

            lines.append(f"\n<b>Найдено событий:</b> {len(info['events'])}\n")

            if info.get("bought_ton") is not None:
                lines.append(
                    f"🟢 <b>Куплен:</b> <code>{info['bought_ton']:.2f} TON</code>"
                )
                if info.get("bought_at"):
                    lines.append(f"   📅 {info['bought_at']}")
            else:
                lines.append("🟢 <b>Покупка:</b> <i>не найдена в истории</i>")

            lines.append("")

            if info.get("sold_ton") is not None:
                lines.append(
                    f"🔴 <b>Продан:</b> <code>{info['sold_ton']:.2f} TON</code>"
                )
                if info.get("sold_at"):
                    lines.append(f"   📅 {info['sold_at']}")
            else:
                lines.append("🔴 <b>Продажа:</b> <i>не найдена (или ещё не продан)</i>")

            if info.get("profit_ton") is not None:
                lines.append("")
                lines.append(
                    f"💰 <b>Прибыль:</b> <code>{info['profit_ton']:+.2f} TON</code>"
                )

            # 4. Выводим монохромный флор
            if mod_name:
                lines.append("")
                if mono_floor_nano > 0:
                    lines.append(
                        f"🎨 <b>Флор монохромной модели:</b> <code>{format_ton(mono_floor_nano)} TON</code>"
                    )
                else:
                    lines.append(
                        f"🎨 <b>Флор монохромной модели:</b> <code>N/A</code> (нет активных лотов)"
                    )

            lines.append("")
            lines.append("───────────────")
            lines.append("<b>Все события:</b>")

            for ev in info["events"]:
                ev_type = ev.get("type") or "?"
                ev_price = ev.get("price_ton") or 0.0
                ev_date = ev.get("date") or "—"
                lines.append(
                    f"• <code>{ev_type}</code> — "
                    f"<code>{ev_price:.2f} TON</code> "
                    f"({ev_date})"
                )

            text = "\n".join(lines)

            if len(text) > 4000:
                text = text[:4000] + "\n\n<i>...отчёт обрезан</i>"

            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception as e:
                await message.answer(
                    f"⚠️ Не удалось отредактировать сообщение: {e}\n\n" + text[:3500],
                    parse_mode=ParseMode.HTML,
                )

    except Exception as e:
        await msg.edit_text(
            f"⚠️ Ошибка при поиске: <code>{str(e)[:300]}</code>",
            parse_mode=ParseMode.HTML,
        )

# ============================================================
# ОБРАБОТЧИКИ ВВОДА
# ============================================================

@dp.message(F.text.regexp(r"^/gift(\s+\S+)?"))
async def cmd_gift_history(message: Message):
    """Обработка команды /gift 123456"""
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "❌ Укажи номер подарка.\n\n"
            "Пример: <code>/gift 384069</code>\n"
            "Или просто напиши: <code>#384069</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    number = parts[1].strip().lstrip("#")
    await _process_gift_history(message, number)


@dp.message(F.text.regexp(r"^#\d+$"))
async def handle_hash_number(message: Message):
    """Ловит сообщения вида #384069 (решётка + цифры)"""
    number = message.text.strip().lstrip("#")
    await _process_gift_history(message, number)


# ============================================================
# SETTINGS
# ============================================================

@dp.message(F.text.in_(["/settings", "⚙️ Настройки"]))
async def open_settings(message: Message):
    session = get_current_session()

    url = build_webapp_url(session)

    reply_kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="⚙️ Открыть настройки Mini App",
                    web_app=WebAppInfo(url=url),
                ),
            ],
            [
                KeyboardButton(text="⬅️ Назад"),
                KeyboardButton(text="🎁 Открыть MRKT"),
            ],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        "Открой панель управления:",
        reply_markup=reply_kb,
    )

@dp.message(F.text == "⬅️ Назад")
async def cmd_back_to_main(message: Message):
    await message.answer(
        "Главное меню:",
        reply_markup=main_reply_keyboard,
    )

@dp.message(F.text == "🎁 Открыть MRKT")
async def cmd_open_mrkt(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Открыть Mini App MRKT",
                    url="https://t.me/mrkt/app",
                )
            ],
        ]
    )

    await message.answer(
        "🎁 <b>Официальный MRKT</b>\n\nНажми кнопку ниже:",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
# ============================================================
# INLINE MODE — ОБНОВЛЕНИЕ AUTH_TOKEN ИЗ MINI APP
# ============================================================

@dp.inline_query()
async def process_inline_auth_refresh(inline_query: InlineQuery):
    """
    Mini App получает новый MRKT AUTH_TOKEN,
    затем передаёт его сюда через switchInlineQuery().

    Формат:
        mrkt_refresh:<AUTH_TOKEN>
    """

    query = (inline_query.query or "").strip()

    PREFIX = "mrkt_refresh:"

    # Нас интересуют только наши запросы
    if not query.startswith(PREFIX):
        await inline_query.answer(
            results=[],
            cache_time=0,
            is_personal=True,
        )
        return

    new_token = query[len(PREFIX):].strip()

    if not new_token:
        await inline_query.answer(
            results=[],
            cache_time=0,
            is_personal=True,
        )
        return

    telegram_id = inline_query.from_user.id

    # Inline-запросы не проходят через WhitelistMiddleware,
    # поэтому проверяем доступ вручную. Иначе любой посторонний
    # пользователь мог бы привязать свой токен и запустить снайпер.
    if telegram_id not in ALLOWED_USER_IDS:
        print(
            f"🚫 [INLINE AUTH] Заблокирован доступ: "
            f"id={telegram_id} | username=@{inline_query.from_user.username}"
        )
        await inline_query.answer(
            results=[],
            cache_time=0,
            is_personal=True,
        )
        return

    print(
        f"🔑 [INLINE AUTH] Получен новый AUTH_TOKEN "
        f"для user={telegram_id}"
    )

    # --------------------------------------------------------
    # Получаем именно сессию этого Telegram-пользователя
    # --------------------------------------------------------

    await apply_auth_token(
        telegram_id,
        new_token,
        source="miniapp_inline",
    )

    # --------------------------------------------------------
    # Отвечаем Telegram
    # --------------------------------------------------------

    result = InlineQueryResultArticle(
        id=f"mrkt_auth_ok_{telegram_id}",
        title="✅ AUTH_TOKEN обновлён",
        description="MRKT токен успешно привязан к твоему аккаунту",
        input_message_content=InputTextMessageContent(
            message_text=(
                "✅ <b>AUTH_TOKEN обновлён!</b>\n\n"
                "Снайпер продолжает работу "
                "с новым MRKT токеном."
            ),
            parse_mode="HTML",
        ),
    )

    await inline_query.answer(
        results=[result],
        cache_time=0,
        is_personal=True,
    )

# ============================================================
# MINI APP SETTINGS HANDLER
# ============================================================

@dp.message(lambda msg: msg.web_app_data is not None)
async def process_webapp_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка парсинга: {e}")
        return
    
    # --------------------------------------------------------
    # ВЕТКА 1: действия из Mini App (токен / feed)
    # Используется как fallback, когда API недоступен.
    # --------------------------------------------------------
    telegram_id = message.from_user.id

    token_value = str(data.get("auth_token") or "").strip()
    if token_value:
        await apply_auth_token(
            telegram_id,
            token_value,
            source="miniapp_senddata",
        )
        await message.answer(
            "🔑 <b>MRKT-токен обновлён через Mini App.</b>\n\n"
            "Мониторинг feed остаётся выключенным, "
            "пока ты его не включишь.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.get("feed_enable"):
        try:
            await enable_feed_monitoring(
                telegram_id,
                int(data.get("feed_seconds") or FEED_WINDOW_SECONDS),
            )
            await message.answer(
                "▶️ <b>Мониторинг feed включён на 30 минут.</b>\n\n"
                "Через 30 минут бот остановит опрос MRKT автоматически.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError as e:
            await message.answer(f"⚠️ {e}")
        return

    if data.get("feed_disable"):
        await disable_feed_monitoring(telegram_id)
        await message.answer("⏹ Мониторинг feed остановлен.")
        return

    # --------------------------------------------------------
    # ВЕТКА 2: обновление настроек
    # --------------------------------------------------------
    apply_settings_dict(get_current_session(), data)

    if SETTINGS.get("auto_refresh_orders", False):
        await start_order_refresh_loop(telegram_id)
    else:
        await stop_order_refresh_loop(telegram_id)

    status_buy = "🟢 ВКЛ" if SETTINGS["auto_buy"] else "🔴 ВЫКЛ"
    status_offer = "🟢 ВКЛ" if SETTINGS["auto_offer"] else "🔴 ВЫКЛ"

    response_text = (
        "⚙️ <b>Настройки WebApp применены!</b>\n\n"
        f"🛒 <b>Авто-покупка:</b> {status_buy}\n"
        f"📩 <b>Авто-офферы:</b> {status_offer}\n"
        f"📊 <b>Лимит флора:</b> {SETTINGS['max_multiplier']}x\n"
        f"💰 <b>Мин/Макс цена:</b> {SETTINGS['min_price']} - {SETTINGS['max_price']} TON\n"
        f"⏱ <b>Интервал опроса:</b> {SETTINGS['poll_interval']}s"
    )

    await message.answer(
        response_text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard,
    )


# ============================================================
# FLOOR
# ============================================================

async def fetch_model_floor(
    client: httpx.AsyncClient,
    collection: str,
    model: str,
) -> int:
    """
    Возвращает floor в nanoTON.

    Использует TTL cache.
    """

    if not collection or not model:
        return 0

    collection_clean = (
        str(collection)
        .strip()
        .lower()
    )

    model_clean = (
        str(model)
        .strip()
        .lower()
    )

    cache_key = (
        collection_clean,
        model_clean,
    )

    now = asyncio.get_running_loop().time()

    cached = MODEL_FLOOR_CACHE.get(
        cache_key
    )

    if cached:
        floor_nano, cached_at = cached

        if (
            now - cached_at
            < MODEL_FLOOR_CACHE_TTL
        ):
            return floor_nano

    payload = {
        "collections": [collection]
    }

    try:

        res = await client.post(
            MODELS_URL,
            headers=HEADERS,
            json=payload,
        )

        if res.status_code != 200:

            print(
                f"⚠️ Ошибка API floor "
                f"[{res.status_code}]: "
                f"{res.text[:500]}"
            )

            return 0

        data = res.json()

        items_list = (
            data
            if isinstance(data, list)
            else data.get("models", [])
        )

        for item in items_list:

            item_model_name = (
                item.get("modelName")
                or item.get("name")
                or ""
            )

            if (
                str(item_model_name)
                .strip()
                .lower()
                != model_clean
            ):
                continue

            floor_nano = (
                item.get(
                    "floorPriceNanoTons"
                )
                or item.get(
                    "floorPriceNanoTONs"
                )
                or item.get(
                    "floorPrice"
                )
                or 0
            )

            floor_nano = to_nano(
                floor_nano
            )

            if floor_nano > 0:

                MODEL_FLOOR_CACHE[
                    cache_key
                ] = (
                    floor_nano,
                    now,
                )

                print(
                    f"📊 [FLOOR] "
                    f"{collection} / {model}: "
                    f"{format_ton(floor_nano)} TON"
                )

                return floor_nano

    except httpx.TimeoutException:

        print(
            "⚠️ Timeout при запросе floor"
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка запроса floor: {e}"
        )

    return 0

async def fetch_saling_floor(
    client: httpx.AsyncClient,
    collection: str,
    model: str,
) -> int:
    """Точный флор из активных продаж (/saling), как в приложении."""
    if not collection or not model:
        return 0

    payload = {
        "count": 1, # Нам нужен только самый дешевый
        "cursor": "",
        "collectionNames": [collection],
        "modelNames": [model],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "isListed": True,
    }

    try:
        res = await client.post(
            SALING_URL,
            headers=HEADERS,
            json=payload,
            timeout=8.0,
        )
        if res.status_code == 200:
            data = res.json()
            items = data.get("gifts") or data.get("items") or []
            if items:
                price = (
                    items[0].get("salePrice")
                    or items[0].get("price")
                    or items[0].get("sale", {}).get("price")
                    or 0
                )
                return to_nano(price)
    except Exception as e:
        print(f"⚠️ fetch_saling_floor error: {e}")
    return 0

async def fetch_model_floor_excluding_gift(
    client: httpx.AsyncClient,
    collection: str,
    model: str,
    exclude_gift_uuid: str = "",
    exclude_sale_id: str = "",
    count: int = 30,
) -> int:
    """
    Floor модели, ИСКЛЮЧАЯ конкретный листинг.
    """
    if not collection or not model:
        return 0

    exclude_set = {
        str(exclude_gift_uuid).strip(),
        str(exclude_sale_id).strip(),
    } - {""}

    if not exclude_set:
        return 0

    payload = {
        "count": count,
        "cursor": "",
        "collectionNames": [collection],
        "modelNames": [model],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "req": "",
        "isListed": True,
    }

    try:
        res = await client.post(
            SALING_URL,  # <--- ИЗМЕНЕНО ТУТ
            headers=HEADERS,
            json=payload,
            timeout=8.0,
        )

        if res.status_code != 200:
            print(
                f"⚠️ [FLOOR EXCL] HTTP {res.status_code} | "
                f"body={res.text[:500]}"
            )
            return 0

        data = res.json()
        items = data.get("gifts") or data.get("items") or []

        print(
            f"🔍 [FLOOR EXCL] {collection}/{model} | "
            f"items={len(items)} | exclude={exclude_set}"
        )

        for idx, item in enumerate(items[:5]):
            if not isinstance(item, dict):
                continue
            ig = item.get("gift") or {}
            print(
                f"   #{idx}: "
                f"item.id={item.get('id')} | "
                f"item.giftId={item.get('giftId')} | "
                f"item.saleId={item.get('saleId')} | "
                f"gift.id={ig.get('id')} | "
                f"gift.giftId={ig.get('giftId')} | "
                f"price={item.get('salePrice') or item.get('price') or item.get('sale', {}).get('price')}"
            )

        for item in items:
            if not isinstance(item, dict):
                continue

            item_gift = item.get("gift") or {}

            item_ids = {
                str(item.get("id") or "").strip(),
                str(item.get("giftId") or "").strip(),
                str(item.get("saleId") or "").strip(),
                str(item_gift.get("id") or "").strip(),
                str(item_gift.get("giftId") or "").strip(),
            } - {""}

            if item_ids & exclude_set:
                continue

            price = (
                item.get("salePrice")
                or item.get("price")
                or item.get("sale", {}).get("price")
                or 0
            )
            price_nano = to_nano(price)

            if price_nano > 0:
                print(
                    f"📊 [FLOOR EXCL OK] {collection}/{model}: "
                    f"next floor = {format_ton(price_nano)} TON"
                )
                return price_nano

        print(f"⚠️ [FLOOR EXCL] не найден валидный листинг после исключения")
        return 0

    except Exception as e:
        print(f"⚠️ floor_excluding error: {e}")

    return 0

# ============================================================
# BUY
# ============================================================

async def resolve_gift_sale_id(
    client: httpx.AsyncClient,
    gift_id: str,
) -> str | None:
    """
    Получает актуальные данные подарка.
    Возвращает giftSaleId только если подарок реально находится на продаже (isOnSale == True).
    """
    if not gift_id:
        return None

    url = f"{GIFTS_URL}/gift/{gift_id}"

    try:
        response = await client.get(url, headers=HEADERS)

        print(f"🔎 [GIFT DETAILS] gift_id={gift_id} | status={response.status_code}")

        if response.status_code != 200:
            print(f"❌ GIFT DETAILS ERROR | HTTP {response.status_code} | {response.text[:500]}")
            return None

        data = response.json()

        if not isinstance(data, dict):
            print("❌ GIFT DETAILS: API вернул не object")
            return None

        # Главная проверка на то, что лот еще продается
        is_on_sale = data.get("isOnSale")
        if is_on_sale is not True:
            print(f"⏭️ [SKIP SOLD] gift_id={gift_id} | isOnSale={is_on_sale}")
            return None

        sale_id = data.get("id")
        if not sale_id:
            print("❌ GIFT DETAILS: отсутствует id продажи")
            return None

        print(f"✅ [ACTIVE SALE] gift_id={gift_id} | giftSaleId={sale_id}")
        return str(sale_id)

    except httpx.TimeoutException:
        print("⚠️ GIFT DETAILS timeout")
        return None
    except Exception as e:
        print(f"⚠️ GIFT DETAILS exception: {e}")
        return None

async def execute_auto_buy(
    client: httpx.AsyncClient,
    sale_id: str,
    price_nano: int,
) -> bool:
    """Покупка конкретного giftSaleId (ID продажи, а не event.id или gift.id)."""
    if not sale_id or price_nano <= 0:
        print("❌ BUY: отсутствует giftSaleId или некорректная цена")
        return False

    payload = {
        "ids": [str(sale_id)],
        "prices": {str(sale_id): int(price_nano)},
    }

    print(f"🛒 [BUY REQUEST] sale_id={sale_id} | price={format_ton(price_nano)} TON")

    try:
        res = await client.post(BUY_URL, headers=HEADERS, json=payload)

        print(f"🔍 [BUY RESPONSE] Status: {res.status_code} | Body: {res.text[:1500]}")

        if res.status_code not in (200, 201):
            print(f"❌ HTTP ошибка BUY [{res.status_code}]")
            return False

        try:
            data = res.json()
        except Exception:
            data = None

        # Пустой массив = лот уже выкуплен или снят, покупка НЕ состоялась
        if data == []:
            print("❌ BUY FAILED: API вернул [] — покупка не состоялась.")
            return False

        if isinstance(data, dict):
            if data.get("success") is False or data.get("error") or data.get("failed"):
                print(f"❌ BUY error/failed: {data}")
                return False

        print(f"🎉 BUY ACCEPTED | sale_id={sale_id} | price={format_ton(price_nano)} TON")
        return True

    except httpx.TimeoutException:
        print("⚠️ BUY timeout")
        return False
    except Exception as e:
        print(f"⚠️ BUY exception: {e}")
        return False


# ============================================================
# OFFER
# ============================================================

async def execute_auto_offer(
    client: httpx.AsyncClient,
    gift_sale_id: str,
    offer_price_nano: int
) -> bool:

    payload = {
        "price": int(offer_price_nano),
        "giftSaleId": str(gift_sale_id)
    }

    try:
        response = await client.post(
            OFFER_URL,
            headers=HEADERS,
            json=payload
        )

        print(
            f"🔍 [OFFER RESPONSE] "
            f"Status: {response.status_code} | "
            f"Body: {response.text}"
        )

        if response.status_code in (200, 201):
            return True

        return False

    except Exception as e:
        print(f"⚠️ Ошибка отправки оффера: {e}")
        return False


# ============================================================
# RADIAL GRADIENT
# ============================================================

def hex_to_rgb(hex_val):

    if not hex_val:
        return (
            40,
            44,
            52,
        )

    if isinstance(
        hex_val,
        int,
    ):
        hex_str = f"{hex_val:06x}"

    else:
        hex_str = (
            str(hex_val)
            .lstrip("#")
        )

    try:

        return tuple(
            int(
                hex_str[i:i + 2],
                16,
            )
            for i in (
                0,
                2,
                4,
            )
        )

    except Exception:

        return (
            40,
            44,
            52,
        )


def create_radial_gradient(
    width,
    height,
    center_color,
    edge_color,
):

    base = Image.new(
        "RGBA",
        (
            width,
            height,
        ),
        edge_color + (
            255,
        ),
    )

    top = Image.new(
        "RGBA",
        (
            width,
            height,
        ),
        center_color + (
            255,
        ),
    )

    mask = Image.new(
        "L",
        (
            width,
            height,
        ),
        0,
    )

    draw = ImageDraw.Draw(
        mask
    )

    max_radius = math.sqrt(
        (width / 2) ** 2
        + (height / 2) ** 2
    )

    for r in range(
        int(max_radius),
        0,
        -2,
    ):

        alpha = int(
            255
            * (
                1
                - (
                    r
                    / max_radius
                ) ** 1.2
            )
        )

        draw.ellipse(
            [
                width / 2 - r,
                height / 2 - r,
                width / 2 + r,
                height / 2 + r,
            ],
            fill=alpha,
        )

    base.paste(
        top,
        (
            0,
            0,
        ),
        mask,
    )

    return base

def _render_gift_card_sync(
    center_color,
    edge_color,
    width: int,
    height: int,
    sym_img_bytes,
    mod_img_bytes,
) -> bytes:
    """Синхронный рендер. Запускается в отдельном потоке через to_thread."""
    card = create_radial_gradient(width, height, center_color, edge_color)

    if sym_img_bytes:
        try:
            sym_img = (
                Image.open(io.BytesIO(sym_img_bytes)).convert("RGBA")
            )
            black_overlay = Image.new(
                "RGBA", sym_img.size, (0, 0, 0, 153)
            )
            alpha_channel = sym_img.split()[3]
            black_overlay.putalpha(
                alpha_channel.point(lambda p: int(p * (153 / 255)))
            )
            icon_size = 50
            sym_img = black_overlay.resize(
                (icon_size, icon_size), Image.Resampling.LANCZOS
            )
            pos_x = width - icon_size - 25
            pos_y = height - icon_size - 25
            card.paste(sym_img, (pos_x, pos_y), mask=sym_img)
        except Exception as e:
            print(f"⚠️ Ошибка рендера узора: {e}")

    if mod_img_bytes:
        try:
            mod_img = (
                Image.open(io.BytesIO(mod_img_bytes)).convert("RGBA")
            )
            mod_size = 220
            mod_img = mod_img.resize(
                (mod_size, mod_size), Image.Resampling.LANCZOS
            )
            pos_x = (width - mod_size) // 2
            pos_y = (height - mod_size) // 2
            card.paste(mod_img, (pos_x, pos_y), mask=mod_img)
        except Exception as e:
            print(f"⚠️ Ошибка рендера модели: {e}")

    output = io.BytesIO()
    card.save(output, format="PNG")
    return output.getvalue()
# ============================================================
# GIFT CARD
# ============================================================

async def generate_gift_card(
    client: httpx.AsyncClient,
    gift_data: dict,
) -> bytes:
    width = 800
    height = 450

    c_val = (
        gift_data.get("backdropColorsCenterColor") or "#5f9a53"
    )
    e_val = (
        gift_data.get("backdropColorsEdgeColor")
        or gift_data.get("backdropColorsCenterColor")
        or "#4d8242"
    )

    center_color = hex_to_rgb(c_val)
    edge_color = hex_to_rgb(e_val)

    symbol_key = gift_data.get("symbolStickerThumbnailKey")
    model_key = gift_data.get("modelStickerThumbnailKey")

    async def _dl(key):
        if not key:
            return None
        try:
            r = await client.get(f"{CDN_BASE_URL}{key}")
            if r.status_code == 200:
                return r.content
        except Exception as e:
            print(f"⚠️ CDN download err: {e}")
        return None

    # обе картинки тянем параллельно
    sym_bytes, mod_bytes = await asyncio.gather(
        _dl(symbol_key), _dl(model_key)
    )

    # PIL — в отдельный поток, event loop свободен
    return await asyncio.to_thread(
        _render_gift_card_sync,
        center_color,
        edge_color,
        width,
        height,
        sym_bytes,
        mod_bytes,
    )

# ============================================================
# LOAD MONOCHROME MATCHES
# ============================================================

def load_monochrome_matches(
    file_name=DEFAULT_MONOCHROME_FILE,
):

    global VALID_PAIRS

    file_path = resolve_matches_path(file_name, BASE_DIR)

    if not os.path.exists(file_path):

        print(
            f"⚠️ Файл {file_name} "
            f"не найден! "
            f"Проверки будут возвращать False."
        )

        return

    try:

        pairs = load_monochrome_pairs(file_name, base_dir=BASE_DIR)

        VALID_PAIRS.clear()
        VALID_PAIRS.update(pairs)

        print(
            "✅ Успешно загружено "
            "монохромных пар в память: "
            f"{len(VALID_PAIRS)}"
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка при загрузке "
            f"{file_path}: {e}"
        )


# ============================================================
# MATCH
# ============================================================

def is_custom_match(
    gift_data: dict,
) -> bool:

    collection = (
        gift_data.get(
            "collectionName"
        )
        or gift_data.get(
            "title"
        )
        or gift_data.get(
            "giftTitle"
        )
        or ""
    ).strip().lower()

    model = (
        gift_data.get(
            "modelName"
        )
        or gift_data.get(
            "model"
        )
        or ""
    ).strip().lower()

    backdrop = (
        gift_data.get(
            "backdropName"
        )
        or gift_data.get(
            "backdrop"
        )
        or ""
    ).strip().lower()

    if not collection or not backdrop:
        return False

    return (
        collection,
        model,
        backdrop,
    ) in VALID_PAIRS


# ============================================================
# INITIAL LOAD
# ============================================================

load_monochrome_matches(
    DEFAULT_MONOCHROME_FILE
)

async def fetch_gift_details(client: httpx.AsyncClient, gift_id: str) -> dict:
    """
    Делает дозапрос GET /api/v1/gifts/gift/{gift_id}
    для получения свежих деталей и актуального giftSaleId.
    """
    if not gift_id:
        return {}

    url = f"https://api.tgmrkt.io/api/v1/gifts/gift/{gift_id}"
    
    try:
        res = await client.get(url, headers=HEADERS)
        if res.status_code == 200:
            data = res.json()
            # Раскомментируй при отладке:
            # print(
            #     "\n========== GIFT DETAILS DUMP ==========\n"
            #     + json.dumps(data, ensure_ascii=False, indent=2)
            #     + "\n========================================\n"
            # )
            return data
        else:
            print(f"⚠️ Ошибка получения деталей подарка [{res.status_code}]: {res.text[:300]}")
    except Exception as e:
        print(f"⚠️ Исключение при запросе деталей подарка: {e}")
        
    return {}

def extract_gift_sale_id(event: dict, item_data: dict, gift: dict) -> str | None:
    """
    Для /api/v1/offers/create нужен ID продажи (giftSaleId),
    а не gift.id.
    """
    return (
        event.get("giftSaleId")
        or event.get("saleId")
        or event.get("id")
        or item_data.get("giftSaleId")
        or item_data.get("saleId")
        or item_data.get("sale", {}).get("giftSaleId")
        or item_data.get("sale", {}).get("saleId")
        or item_data.get("sale", {}).get("id")
        or gift.get("giftSaleId")
        or gift.get("saleId")
    )

# ============================================================
# EXTRACT PRICE
# ============================================================

def extract_price_nano(
    item_data: dict,
    gift: dict,
) -> int:

    amount_nano = (
        item_data.get(
            "amount"
        )
        or gift.get(
            "salePrice"
        )
        or item_data.get(
            "sale",
            {},
        ).get(
            "price"
        )
        or 0
    )

    return to_nano(
        amount_nano
    )


# ============================================================
# EXTRACT FLOOR
# ============================================================

def extract_floor_nano(
    gift: dict,
) -> int:

    floor_nano = (
        gift.get(
            "floorPriceNanoTONsByModel"
        )
        or gift.get(
            "modelFloorPriceNanoTONs"
        )
        or 0
    )

    return to_nano(
        floor_nano
    )


# ============================================================
# TRADE CORE
# ============================================================

def should_ignore_event(item_data: dict) -> tuple[bool, str]:
    """Проверяет ивенты на основе тумблеров из WebApp."""
    event_type = str(item_data.get("type", "")).strip().lower()
    gift = item_data.get("gift") or {}

    # 1. Проверка Lucky Buy
    if SETTINGS["skip_lucky_buy"] and (gift.get("luckyBuy") is True or item_data.get("luckyBuy") is True):
        return True, "luckyBuy=true"

    # 2. Покупки / продажи
    if SETTINGS["skip_purchase"] and event_type in ("purchase", "buy"):
        return True, f"ignored_type_{event_type}"

    # 3. Выводы
    if SETTINGS["skip_withdraw"] and event_type in ("withdraw", "withdrawal"):
        return True, f"ignored_type_{event_type}"

    # 4. Снятия с продажи / отмены
    if SETTINGS["skip_remove_sale"] and event_type in ("remove_from_sale", "removed_from_sale"):
        return True, f"ignored_type_{event_type}"

    if SETTINGS["skip_cancel_sale"] and event_type in ("cancel_sale", "cancelled_sale"):
        return True, f"ignored_type_{event_type}"

    if SETTINGS["skip_unlisted"] and event_type == "unlisted":
        return True, "ignored_type_unlisted"

    return False, ""

async def process_trade(
    client: httpx.AsyncClient,
    item_data: dict,
) -> dict:
    """
    Полный торговый pipeline:

        feed
          ↓
        match
          ↓
        price nanoTON
          ↓
        floor nanoTON
          ↓
        BUY / OFFER
          ↓
        balance reservation
          ↓
        API
    """

    session = get_current_session()

    result = {
        "action": None,
        "success": False,
        "price_nano": 0,
        "floor_nano": 0,
        "offer_nano": 0,
        "balance_nano": 0,
        "reason": None,
    }
    
    # Самая первая и быстрая отсечка мусора
    ignored, ignore_reason = should_ignore_event(item_data)
    if ignored:
        result["reason"] = f"ignored_event ({ignore_reason})"
        return result


    item_type = str(
        item_data.get(
            "type",
            "",
        )
    ).lower()

    gift = (
        item_data.get(
            "gift"
        )
        or {}
    )

    if not gift:

        result["reason"] = (
            "no_gift"
        )

        return result

    # --------------------------------------------------------
    # EVENT TYPE
    # --------------------------------------------------------

    allowed_types = {
        "sale",
        "sell",
        "listed",
        "listing",
        "change_price",
    }

    if (
        item_type
        and item_type not in allowed_types
    ):

        result["reason"] = (
            "unsupported_event"
        )

        return result

    # --------------------------------------------------------
    # MATCH
    # --------------------------------------------------------

    if not is_custom_match(
        gift
    ):

        result["reason"] = (
            "not_match"
        )

        return result

    # --------------------------------------------------------
    # RESOLVE REAL SALE ID
    # --------------------------------------------------------
    gift_uuid = gift.get("id")
    if not gift_uuid:
        result["reason"] = "no_gift_uuid"
        print("⚠️ BUY/OFFER: в feed отсутствует gift.id")
        return result

    # Один запрос вместо двух: получаем детали подарка здесь,
    # результат переиспользуется ниже в OFFER-ветке
    gift_details_cache = await fetch_gift_details(client, str(gift_uuid))
    if not gift_details_cache:
        result["reason"] = "not_on_sale"
        print(f"⏭️ SKIP: не удалось получить детали | gift={gift_uuid}")
        return result

    if gift_details_cache.get("isOnSale") is not True:
        result["reason"] = "not_on_sale"
        print(
            f"⏭️ SKIP: giftSaleId недоступен | gift={gift_uuid} | "
            f"isOnSale={gift_details_cache.get('isOnSale')}"
        )
        return result

    sale_id = gift_details_cache.get("id")
    if not sale_id:
        result["reason"] = "not_on_sale"
        print(f"⏭️ SKIP: нет id в деталях | gift={gift_uuid}")
        return result

    sale_id = str(sale_id)

    # Нужно для inline-кнопки «Купить» под уведомлением,
    # когда авто-покупка выключена.
    result["sale_id"] = sale_id
    result["gift_id"] = str(gift_uuid)

    print(
        "🧩 [BUY IDS] "
        f"event.id={item_data.get('id')} | "
        f"gift.id={gift_uuid} | "
        f"giftSaleId={sale_id}"
    )

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    price_nano = extract_price_nano(
        item_data,
        gift,
    )

    if price_nano <= 0:

        result["reason"] = (
            "invalid_price"
        )

        print(
            "⚠️ Пропуск: "
            "некорректная цена"
        )

        return result

    result["price_nano"] = (
        price_nano
    )

    # --- ПРОВЕРКА ДИАПАЗОНА ЦЕН ИЗ WEBAPP ---
    price_ton = price_nano / NANO_TON
    min_p = SETTINGS.get("min_price", 0.0)
    max_p = SETTINGS.get("max_price", 0.0)

    if min_p > 0 and price_ton < min_p:
        result["reason"] = f"below_min_price ({price_ton} < {min_p})"
        return result

    if max_p > 0 and price_ton > max_p:
        result["reason"] = f"above_max_price ({price_ton} > {max_p})"
        return result

    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------

    coll_name = (
        gift.get("collectionName")
        or gift.get("title")
        or gift.get("giftTitle")
        or ""
    )

    mod_name = (
        gift.get("modelName")
        or gift.get("model")
        or ""
    )

    back_name = (
        gift.get("backdropName")
        or gift.get("backdrop")
        or ""
    )

    # --------------------------------------------------------
    # FLOOR
    # --------------------------------------------------------
    # Сначала — floor БЕЗ нашего листинга.
    # MRKT в /gifts возвращает в item.id id продажи (sale_id),
    # поэтому исключаем одновременно по gift UUID и по sale_id.

    floor_nano = 0

    if coll_name and mod_name:
        floor_nano = await fetch_model_floor_excluding_gift(
            client,
            coll_name,
            mod_name,
            exclude_gift_uuid=str(gift_uuid or ""),
            exclude_sale_id=str(sale_id or ""),
        )

    # Fallback — /models endpoint (с кэшем).
    # НЕ используем extract_floor_nano(gift): в feed floor уже
    # включает наш собственный лот, и в уведомлении price = floor.
    if floor_nano <= 0 and coll_name and mod_name:
        floor_nano = await fetch_model_floor(
            client,
            coll_name,
            mod_name,
        )
    # --------------------------------------------------------
    # ДИСПЛЕЙНЫЕ ПОЛЯ (только для уведомления, не для торговли)
    # --------------------------------------------------------

    # Реальный флор модели — как его видит MRKT.
    # Берем напрямую из /saling, как это делает само приложение.
    display_floor_nano = 0
    if coll_name and mod_name:
        display_floor_nano = await fetch_saling_floor(
            client, coll_name, mod_name
        )
        
    # Если /saling не ответил — fallback на данные из feed
    if display_floor_nano <= 0:
        display_floor_nano = extract_floor_nano(gift)
        
    result["display_floor_nano"] = display_floor_nano

    # Флор монохромной модели (самая низкая цена среди
    # монохромных листингов этой модели).
    mono_floor_nano = 0
    try:
        if coll_name and mod_name:
            mono_floor_nano = await monochrome_guard.get_monochrome_floor(
                client=client,
                headers=HEADERS,
                collection=coll_name,
                model=mod_name,
            )
    except Exception as e:
        logger.warning(
            f"⚠️ mono_floor error {coll_name}/{mod_name}: {e}"
        )
    result["mono_floor_nano"] = mono_floor_nano

    if floor_nano <= 0:
        result["reason"] = "no_floor"
        print(f"⚠️ Floor не найден: {coll_name} / {mod_name}")
        return result

    result["floor_nano"] = floor_nano

    # --------------------------------------------------------
    # BUY LIMIT
    # --------------------------------------------------------

    max_buy_nano = (
        calc_floor_buy_limit(
            floor_nano,
            SETTINGS.get("max_multiplier", 1.02),
        )
    )

    # ========================================================
    # MONOCHROME GUARD
    # ========================================================
    # Только если это реальный кандидат на покупку.
    if (
        SETTINGS.get("auto_buy", True)
        and price_nano <= max_buy_nano
        and monochrome_guard.has_siblings(coll_name, mod_name)
    ):
        try:
            cheap_sibling = await monochrome_guard.has_cheap_sibling(
                client=client,
                headers=HEADERS,
                collection=coll_name,
                model=mod_name,
                current_backdrop=back_name,
                floor_nano=floor_nano,
                current_price_nano=price_nano,
                exclude_gift_id=str(gift.get("id") or "") or None,
            )
        except Exception as e:
            logger.warning(
                f"⚠️ MonochromeGuard упал на {coll_name}/{mod_name}: {e}"
            )
            cheap_sibling = False

        if cheap_sibling:
            result["reason"] = "monochrome_cheap_sibling"
            logger.info(
                f"🚫 SKIP BUY: {coll_name} / {mod_name} — "
                f"рядом дешёвый сосед по модели"
            )
            return result

    # ========================================================
    # CRITICAL TRADE SECTION
    # ========================================================

    async with TRADE_LOCK:

        # Получаем максимально свежий баланс
        # непосредственно перед торговой операцией.
        balance_nano = (
            await get_balance_nano(
                client,
                force=True,
            )
        )

        available_nano = balance_nano - session.reserved_balance_nano

        if available_nano < 0:
            available_nano = 0

        result["balance_nano"] = (
            available_nano
        )

        # ====================================================
        # AUTO BUY
        # ====================================================

        if (
            SETTINGS.get("auto_buy", True)
            and price_nano <= max_buy_nano
        ):

            result["action"] = (
                "buy"
            )

            if available_nano < price_nano:

                result["reason"] = (
                    "insufficient_balance"
                )

                print(
                    "⚠️ BUY SKIP | "
                    f"price="
                    f"{format_ton(price_nano)} | "
                    f"available="
                    f"{format_ton(available_nano)}"
                )

                return result

            # ------------------------------------------------
            # RESERVE
            # ------------------------------------------------

            session.reserved_balance_nano += price_nano

            try:

                bought = (
                    await execute_auto_buy(
                        client,
                        str(sale_id),
                        price_nano,
                    )
                )

                result["success"] = (
                    bought
                )

                if bought:

                    print(
                        "🚀 AUTO BUY SUCCESS | "
                        f"sale_id={sale_id} | "
                        f"price="
                        f"{format_ton(price_nano)} TON | "
                        f"floor="
                        f"{format_ton(floor_nano)} TON"
                    )

                    # Обновляем balance cache.
                    await get_balance_nano(
                        client,
                        force=True,
                    )

                return result

            finally:

                session.reserved_balance_nano -= price_nano

        # AUTO OFFER
        if SETTINGS.get("auto_offer", False) and price_nano > max_buy_nano:
            result["action"] = "offer"

            base_offer_nano = calc_offer_price(floor_nano, SETTINGS.get("offer_multiplier", 1.01))
            min_offer_nano = min_offer_for_sale(price_nano)

            offer_nano = max(base_offer_nano, min_offer_nano)
            offer_nano = ceil_to_step_nano(offer_nano)

            result["offer_nano"] = offer_nano

            print(
                "📩 OFFER CALC | "
                f"price={format_ton(price_nano)} | "
                f"floor={format_ton(floor_nano)} | "
                f"base={format_ton(base_offer_nano)} | "
                f"minimum={format_ton(min_offer_nano)} | "
                f"final={format_ton(offer_nano)} TON"
            )

            if available_nano < offer_nano:
                result["reason"] = "insufficient_balance"
                print(f"⚠️ OFFER SKIP | need={format_ton(offer_nano)} | available={format_ton(available_nano)}")
                return result

            # ------------------------------------------------
            # REUSE CACHED DETAILS
            # ------------------------------------------------
            gift_id = gift.get("id")
            gift_details = gift_details_cache
            gift_sale_id = extract_gift_sale_id(
                gift_details, item_data, gift
            )

            print(
                "🔎 OFFER IDS | "
                f"extracted_giftSaleId={gift_sale_id} | "
                f"gift.id={gift_id}"
            )

            if not gift_sale_id:
                result["reason"] = "no_gift_sale_id"
                print("⚠️ OFFER SKIP: giftSaleId не найден даже в деталях подарка")
                return result

            print(
                "📌 ОТПРАВКА OFFER:"
                f"\n   giftSaleId = {gift_sale_id}"
                f"\n   gift.id    = {gift_id}"
                f"\n   price      = {format_ton(offer_nano)} TON"
            )

            session.reserved_balance_nano += offer_nano

            try:
                offered = await execute_auto_offer(
                    client,
                    str(gift_sale_id),
                    offer_nano,
                )

                result["success"] = offered

                if offered:
                    print(
                        "📩 AUTO OFFER SUCCESS | "
                        f"giftSaleId={gift_sale_id} | "
                        f"gift_id={gift_id} | "
                        f"offer={format_ton(offer_nano)} TON"
                    )
                    await get_balance_nano(client, force=True)

                return result

            finally:
                session.reserved_balance_nano -= offer_nano
                

        # ----------------------------------------------------
        # NOTHING (но считаем, что было бы)
        # ----------------------------------------------------

        result["reason"] = "no_action"

        # Если ни покупка, ни оффер не выполнялись —
        # вычисляем «гипотетическое» действие для уведомления.
        if result.get("action") is None:
            would_buy = price_nano <= max_buy_nano
            would_offer = price_nano > max_buy_nano

            if would_buy:
                result["would_action"] = "buy"
                result["would_price_nano"] = price_nano
            elif would_offer:
                # Считаем цену оффера так же, как в реальной ветке
                base_offer_nano = calc_offer_price(
                    floor_nano,
                    SETTINGS.get("offer_multiplier", 1.01),
                )
                min_offer_nano = min_offer_for_sale(price_nano)
                offer_nano = max(base_offer_nano, min_offer_nano)
                offer_nano = ceil_to_step_nano(offer_nano)

                result["would_action"] = "offer"
                result["would_price_nano"] = offer_nano
            else:
                result["would_action"] = None

        return result


# ============================================================
# TELEGRAM NOTIFICATION
# ============================================================

async def notify_auth_expired(telegram_id: int):
    """
    Одноразовое сообщение пользователю: токен MRKT просрочен.
    Защита от повторной отправки — через флаг session._auth_notified.
    """
    session = get_or_create_session(telegram_id)

    if session._auth_notified:
        return

    session._auth_notified = True

    text = (
        "🔒 <b>AUTH_TOKEN просрочен</b>\n\n"
        "MRKT вернул <code>401 Unauthorized</code>.\n"
        "Снайпер и авто-ордера для твоего аккаунта "
        "<b>остановлены</b>.\n\n"
        "<b>Как починить:</b>\n"
        "Открой Mini App и нажми «Обновить токен» — "
        "бот получит свежий MRKT-токен "
        "через официальный <code>/api/v1/auth</code> "
        "без F12 и ручного копирования.\n\n"
        "Резервный вариант: "
        "<code>/auth НОВЫЙ_ТОКЕН</code>."
    )

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(
            f"⚠️ Не удалось уведомить {telegram_id} "
            f"о протухшем токене: {e}"
        )


async def notify_feed_stopped(telegram_id: int):
    """Одноразовое сообщение: 30-минутное окно мониторинга закончилось."""
    session = get_or_create_session(telegram_id)

    if session._feed_expiry_notified:
        return

    session._feed_expiry_notified = True

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "⏹ <b>Мониторинг feed автоматически выключен.</b>\n\n"
                "Прошло 30 минут — бот больше не стучится в MRKT "
                "для проверки ленты.\n\n"
                "Включить снова: <code>/feed</code> "
                "или кнопка в Mini App.",
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(
            f"⚠️ Не удалось уведомить {telegram_id} "
            f"об остановке feed: {e}"
        )


async def send_telegram_notification(
    client: httpx.AsyncClient,
    item_data: dict,
):

    item_type = item_data.get(
        "type",
        "",
    )

    gift = (
        item_data.get(
            "gift"
        )
        or {}
    )

    if not gift:
        return

    coll_name = (
        gift.get(
            "collectionName"
        )
        or gift.get(
            "title"
        )
        or "N/A"
    )

    mod_name = (
        gift.get(
            "modelName"
        )
        or "N/A"
    )

    back_name = (
        gift.get(
            "backdropName"
        )
        or "N/A"
    )

    print(
        f"🔎 Новое событие "
        f"[{item_type}]: "
        f"{coll_name} | "
        f"{mod_name} | "
        f"{back_name}"
    )

    # --------------------------------------------------------
    # EVENT TYPE
    # --------------------------------------------------------

    allowed_types = {
        "sale",
        "sell",
        "listed",
        "listing",
        "change_price",
    }

    if (
        item_type
        and item_type.lower()
        not in allowed_types
    ):
        return

    # --------------------------------------------------------
    # MATCH
    # --------------------------------------------------------

    if not is_custom_match(
        gift
    ):
        return

    # --------------------------------------------------------
    # NOTIFICATION FILTERS
    # --------------------------------------------------------
    settings = get_current_session().settings

    if not settings.get("notifications", True):
        print(f"🔕 Уведомления выключены, пропускаю: {coll_name}")
        return

    back_lower = str(back_name).strip().lower()
    if back_lower in ("black", "onyx black") and not settings.get("notifications_black_onyx", True):
        print(f"🔕 Black/Onyx уведомления выключены, пропускаю: {coll_name} / {back_name}")
        return
    
    # --------------------------------------------------------
    # ⚡ ТЕНЕВАЯ ЗАДЕРЖКА
    # --------------------------------------------------------
    # Тормозит и действие (process_trade → BUY/OFFER),
    # и уведомление (bot.send_photo ниже), потому что оно
    # выполняется уже после process_trade.
    # --------------------------------------------------------
    await apply_shadow_delay(context="match")

    # --------------------------------------------------------
    # TRADE
    # --------------------------------------------------------

    try:

        trade = await process_trade(
            client,
            item_data,
        )

        reason = trade.get("reason") or ""

        # Не отправляем уведомление, если лот:
        #   • уже продан / снят (not_on_sale) — только при включённом
        #     фильтре skip_inactive;
        #   • отфильтрован по min/max цене из WebApp.
        #
        # Важно: skip_inactive управляет ТОЛЬКО уведомлениями.
        # Торговая логика всё равно не покупает лот с isOnSale != true,
        # чтобы не отправить BUY/OFFER по несуществующей продаже.
        if (
            (
                reason == "not_on_sale"
                and settings.get("skip_inactive", True)
            )
            or reason.startswith("below_min_price")
            or reason.startswith("above_max_price")
        ):
            return

    except Exception as e:
        print(f"❌ Критическая ошибка торгового ядра: {e}")
        trade = {
            "action": None,
            "success": False,
            "price_nano": 0,
            "floor_nano": 0,
            "offer_nano": 0,
            "balance_nano": 0,
            "reason": "trade_exception",
        }

    price_nano = trade.get(
        "price_nano",
        0,
    )

    floor_nano = trade.get(
        "floor_nano",
        0,
    )

    offer_nano = trade.get(
        "offer_nano",
        0,
    )

    balance_nano = trade.get(
        "balance_nano",
        0,
    )

    # --------------------------------------------------------
    # ACTION STATUS
    # --------------------------------------------------------

    action_status = ""

    if trade.get("action") == "buy":

        if trade.get("success"):

            action_status = (
                "\n\n"
                "🚀 <b>АВТО-ПОКУПКА "
                "УСПЕШНО СОВЕРШЕНА!</b>"
            )

        elif (
            trade.get("reason")
            == "insufficient_balance"
        ):

            action_status = (
                "\n\n"
                "⚠️ <b>Недостаточно баланса "
                "для покупки!</b>"
            )

        elif trade.get("reason"):

            action_status = (
                "\n\n"
                "⚠️ <b>Покупка не выполнена.</b>"
            )

    elif trade.get("action") == "offer":

        if trade.get("success"):

            action_status = (
                "\n\n"
                f"📩 <b>Авто-оффер "
                f"отправлен!</b> "
                f"({format_ton(offer_nano)} TON)"
            )

        elif (
            trade.get("reason")
            == "insufficient_balance"
        ):

            action_status = (
                "\n\n"
                "⚠️ <b>Недостаточно баланса "
                "для оффера!</b>"
            )

        elif trade.get("reason"):

            action_status = (
                "\n\n"
                "⚠️ <b>Оффер не отправлен.</b>"
            )

    # --------------------------------------------------------
    # ПРЕДПРОСМОТР ДЕЙСТВИЯ (если реального не было)
    # --------------------------------------------------------
    would_action = trade.get("would_action")
    would_price_nano = trade.get("would_price_nano", 0)

    if not action_status:

        if would_action == "buy":
            action_status = (
                "\n\n"
                "🎯 <b>БЫЛО БЫ: ПОКУПКА</b>\n"
                f"💰 Цена покупки: <code>{format_ton(would_price_nano)} TON</code>"
            )
        elif would_action == "offer":
            action_status = (
                "\n\n"
                "🎯 <b>БЫЛО БЫ: ОФФЕР</b>\n"
                f"📩 Цена оффера: <code>{format_ton(would_price_nano)} TON</code>"
            )

    # --------------------------------------------------------
    # MARKET URL
    # --------------------------------------------------------

    number = gift.get(
        "number",
        "N/A",
    )

    gift_uuid = str(
        gift.get(
            "id",
            "",
        )
    )

    clean_uuid = (
        gift_uuid.replace(
            "-",
            "",
        )
    )

    market_url = (
        "https://t.me/mrkt/app"
        f"?startapp={clean_uuid}"
    )

    keyboard_rows = [
        [
            InlineKeyboardButton(
                text="🎯 Открыть на MRKT",
                url=market_url,
            )
        ]
    ]

    # Ручная покупка: только если авто-покупка выключена,
    # бот сам НЕ покупал, но лот прошёл все проверки
    # как кандидат на покупку.
    manual_sale_id = trade.get("sale_id")
    manual_buy_available = (
        not settings.get("auto_buy", True)
        and would_action == "buy"
        and int(would_price_nano or 0) > 0
        and bool(manual_sale_id)
        and bool(clean_uuid)
    )

    if manual_buy_available:
        session = get_current_session()

        if not hasattr(session, "pending_buys"):
            session.pending_buys = {}

        now = time.time()

        # Чистим просроченные кнопки, чтобы словарь не рос бесконечно.
        session.pending_buys = {
            key: value
            for key, value in session.pending_buys.items()
            if now - float(value.get("created_ts") or 0)
            <= MANUAL_BUY_TTL
        }

        token = f"{clean_uuid}_{int(now)}"
        callback_data = f"manual_buy:{token}"

        if len(callback_data.encode("utf-8")) <= 64:
            session.pending_buys[token] = {
                "gift_uuid": gift_uuid,
                "sale_id": str(manual_sale_id),
                "price_nano": int(would_price_nano),
                "floor_nano": int(trade.get("floor_nano") or 0),
                "collection": str(coll_name or ""),
                "model": str(mod_name or ""),
                "backdrop": str(back_name or ""),
                "created_ts": now,
                "busy": False,
            }

            keyboard_rows.append([
                InlineKeyboardButton(
                    text="🛒 Купить",
                    callback_data=callback_data,
                )
            ])
            manual_button_added = True
        else:
            manual_button_added = False
    else:
        manual_button_added = False

    if manual_button_added and action_status:
        action_status += (
            "\n🛒 Можно купить вручную кнопкой ниже."
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=keyboard_rows
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    display_floor_nano = trade.get("display_floor_nano", 0) or floor_nano
    mono_floor_nano = trade.get("mono_floor_nano", 0)

    if display_floor_nano > 0:
        floor_info = (
            f"📊 <b>Флор модели:</b> "
            f"{format_ton(display_floor_nano)} TON\n"
        )
        # Если флор без нашего лота отличается — покажем отдельно
        if floor_nano > 0 and floor_nano != display_floor_nano:
            floor_info += (
                f"📊 <b>Флор без текущего лота:</b> "
                f"{format_ton(floor_nano)} TON\n"
            )
    else:
        floor_info = "📊 <b>Флор модели:</b> N/A\n"

    # Монохромный флор
    if monochrome_guard.has_siblings(coll_name, mod_name):
        if mono_floor_nano > 0:
            floor_info += (
                f"🎨 <b>Флор монохромной модели:</b> "
                f"{format_ton(mono_floor_nano)} TON\n"
            )
        else:
            floor_info += (
                f"🎨 <b>Флор монохромной модели:</b> N/A\n"
            )

    balance_info = (
        f"💳 <b>Свободный баланс:</b> "
        f"{format_ton(balance_nano)} TON\n"
    )

    price_info = (
        f"💰 <b>Цена:</b> "
        f"{format_ton(price_nano)} TON\n"
        if price_nano > 0
        else
        "💰 <b>Цена:</b> N/A\n"
    )

    text = (
        "🖤 <b>MATCH ИЗ БАЗЫ!</b>"
        f"{action_status}\n\n"
        f"🏆 <b>Коллекция:</b> "
        f"{coll_name} #{number}\n"
        f"{price_info}"
        f"{floor_info}"
        f"{balance_info}"
        f"🎨 <b>Модель:</b> "
        f"{mod_name}\n"
        f"🖼 <b>Фон:</b> "
        f"{back_name}"
    )

    # --------------------------------------------------------
    # SEND IMAGE
    # --------------------------------------------------------

    try:

        image_bytes = (
            await generate_gift_card(
                client,
                gift,
            )
        )

        photo = BufferedInputFile(
            image_bytes,
            filename="gift_card.png",
        )

        await bot.send_photo(
            chat_id=get_current_session().telegram_id,
            photo=photo,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка отправки "
            f"изображения: {e}"
        )

        try:

            await bot.send_message(
                chat_id=get_current_session().telegram_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

        except Exception as send_error:

            print(
                f"❌ Ошибка отправки "
                f"Telegram-сообщения: "
                f"{send_error}"
            )


# ============================================================
# MANUAL BUY — inline-кнопка «Купить» при выключенной авто-покупке
# ============================================================

def _manual_buy_price_from_details(details: dict) -> int:
    """Цена из деталей подарка. Возвращает только реалистичный nanoTON."""
    if not isinstance(details, dict):
        return 0

    sale = details.get("sale") or {}

    for value in (
        details.get("salePrice"),
        details.get("price"),
        sale.get("salePrice"),
        sale.get("price"),
    ):
        try:
            price_nano = to_nano(value)
        except Exception:
            continue

        # Меньше 0.001 TON — это, скорее всего, уже TON, а не nanoTON.
        # В таком случае не угадываем и используем цену из уведомления.
        if price_nano >= 1_000_000:
            return price_nano

    return 0


async def _remove_manual_buy_keyboard(
    message,
    gift_uuid: str = "",
) -> None:
    """Убирает кнопку «Купить», оставляя ссылку на лот в MRKT."""
    try:
        clean = str(gift_uuid or "").replace("-", "")

        markup = None
        if clean:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎯 Открыть на MRKT",
                            url=(
                                "https://t.me/mrkt/app"
                                f"?startapp={clean}"
                            ),
                        )
                    ]
                ]
            )

        await message.edit_reply_markup(reply_markup=markup)
    except Exception as e:
        logger.debug(
            f"manual buy: не удалось убрать кнопку: {e}"
        )


@dp.callback_query(F.data.startswith("manual_buy:"))
async def process_manual_buy(callback: CallbackQuery):
    """Ручная покупка лота из уведомления, когда auto_buy выключен."""
    session = get_current_session()

    if not hasattr(session, "pending_buys"):
        session.pending_buys = {}

    if not session.auth_token:
        await callback.answer(
            "🔐 Сначала привяжи AUTH_TOKEN через /auth.",
            show_alert=True,
        )
        return

    token = callback.data.split(":", 1)[1].strip()
    pending = session.pending_buys.get(token)

    if not pending:
        await callback.answer(
            "⌛ Кнопка устарела. Купи этот лот вручную в MRKT.",
            show_alert=True,
        )
        return

    now = time.time()

    if now - float(pending.get("created_ts") or 0) > MANUAL_BUY_TTL:
        session.pending_buys.pop(token, None)
        await callback.answer(
            "⌛ Прошло больше 10 минут. Купи лот вручную в MRKT.",
            show_alert=True,
        )
        return

    if pending.get("busy"):
        await callback.answer(
            "⏳ Покупка уже выполняется...",
            show_alert=True,
        )
        return

    pending["busy"] = True

    gift_uuid = str(pending.get("gift_uuid") or "")
    sale_id = str(pending.get("sale_id") or "")
    price_nano = int(pending.get("price_nano") or 0)

    try:
        timeout = httpx.Timeout(
            connect=5.0,
            read=15.0,
            write=10.0,
            pool=10.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1. Свежие детали: лот мог быть продан, снят
            #    или перевыставлен с новым ID/ценой.
            details = await fetch_gift_details(client, gift_uuid)

            if not details:
                await callback.answer(
                    "⚠️ Не удалось получить данные лота. Попробуй ещё раз.",
                    show_alert=True,
                )
                return

            if details.get("isOnSale") is not True:
                session.pending_buys.pop(token, None)
                await _remove_manual_buy_keyboard(
                    callback.message,
                    gift_uuid,
                )
                await callback.answer(
                    "❌ Лот уже не продаётся.",
                    show_alert=True,
                )
                return

            fresh_sale_id = details.get("id")
            if fresh_sale_id:
                sale_id = str(fresh_sale_id)

            if not sale_id:
                await callback.answer(
                    "⚠️ Не удалось определить ID продажи.",
                    show_alert=True,
                )
                return

            fresh_price_nano = _manual_buy_price_from_details(details)
            if fresh_price_nano > 0:
                # Не покупаем дороже, чем было в уведомлении:
                # цена могла измениться, пока пользователь думал.
                if fresh_price_nano > price_nano:
                    await callback.answer(
                        f"⚠️ Цена выросла: было "
                        f"{format_ton(price_nano)} TON, сейчас "
                        f"{format_ton(fresh_price_nano)} TON. "
                        f"Покупка отменена.",
                        show_alert=True,
                    )
                    return

                price_nano = fresh_price_nano

            if price_nano <= 0:
                await callback.answer(
                    "⚠️ Не удалось определить цену лота.",
                    show_alert=True,
                )
                return

            # 2. Ручная покупка не должна обходить MonochromeGuard.
            try:
                coll = str(pending.get("collection") or "")
                model = str(pending.get("model") or "")
                backdrop = str(pending.get("backdrop") or "")
                floor_nano = int(pending.get("floor_nano") or 0)

                if (
                    coll
                    and model
                    and monochrome_guard.has_siblings(coll, model)
                ):
                    cheap = await monochrome_guard.has_cheap_sibling(
                        client=client,
                        headers=HEADERS,
                        collection=coll,
                        model=model,
                        current_backdrop=backdrop,
                        floor_nano=floor_nano,
                        current_price_nano=price_nano,
                        exclude_gift_id=gift_uuid,
                    )

                    if cheap:
                        await callback.answer(
                            "🚫 Рядом есть более дешёвый монохромный лот "
                            "этой модели. Покупка отменена.",
                            show_alert=True,
                        )
                        return
            except Exception as e:
                logger.warning(
                    f"⚠️ manual buy guard error: {e}"
                )

            # 3. Покупка под общим trade lock и с резервом баланса.
            async with TRADE_LOCK:
                balance_nano = await get_balance_nano(
                    client,
                    force=True,
                )

                available_nano = (
                    balance_nano - session.reserved_balance_nano
                )

                if available_nano < 0:
                    available_nano = 0

                if available_nano < price_nano:
                    await callback.answer(
                        f"⚠️ Недостаточно баланса: нужно "
                        f"{format_ton(price_nano)} TON, доступно "
                        f"{format_ton(available_nano)} TON.",
                        show_alert=True,
                    )
                    return

                session.reserved_balance_nano += price_nano

                try:
                    bought = await execute_auto_buy(
                        client,
                        sale_id,
                        price_nano,
                    )
                finally:
                    session.reserved_balance_nano -= price_nano

            if bought:
                await get_balance_nano(client, force=True)

        if bought:
            session.pending_buys.pop(token, None)
            await _remove_manual_buy_keyboard(
                callback.message,
                gift_uuid,
            )

            logger.info(
                f"🛒 [manual buy] user={session.telegram_id} | "
                f"gift={gift_uuid} | sale={sale_id} | "
                f"price={format_ton(price_nano)} TON"
            )

            await callback.answer(
                f"✅ Куплено за {format_ton(price_nano)} TON",
                show_alert=True,
            )
        else:
            await callback.answer(
                "❌ Купить не удалось: лот уже выкуплен "
                "или цена изменилась.",
                show_alert=True,
            )

    except Exception as e:
        logger.exception(f"❌ manual buy error: {e}")
        await callback.answer(
            f"⚠️ Ошибка покупки: {str(e)[:120]}",
            show_alert=True,
        )
    finally:
        pending["busy"] = False


# ============================================================
# FEED
# ============================================================

async def get_feed(client: httpx.AsyncClient):
    session = get_current_session()

    # Если сессия уже помечена как мёртвая — не тратим запросы.
    if not session.auth_ok:
        return None

    try:
        safe_headers = dict(HEADERS) if HEADERS else {}
        response = await client.post(
            FEED_URL,
            headers=safe_headers,
            json=PAYLOAD,
        )

        if response.status_code == 200:
            session.mark_auth_ok()
            return response.json()

        # -------- 401 / 403: токен протух --------
        if response.status_code in (401, 403):
            changed = session.mark_auth_expired(source="feed")
            if changed:
                asyncio.create_task(
                    notify_auth_expired(session.telegram_id)
                )
            return None

        # -------- Прочие ошибки: логируем редко и коротко --------
        logger.warning(
            f"⚠️ Feed HTTP {response.status_code} "
            f"[user {session.telegram_id}]: {response.text[:200]}"
        )
        return None

    except httpx.TimeoutException:
        logger.warning(
            f"⚠️ Feed timeout [user {session.telegram_id}]"
        )
        return None

    except Exception as e:
        logger.warning(
            f"⚠️ Feed network error [user {session.telegram_id}]: {e}"
        )
        return None


# ============================================================
# DEDUP KEY
# ============================================================

def get_feed_item_key(
    item: dict,
):
    """
    Ключ события feed.

    ВАЖНО: используем gift.id (UUID подарка), а НЕ event.id.
    event.id у MRKT меняется на каждый re-list / change_price,
    из-за чего один и тот же лот обрабатывается повторно.
    gift.id — стабилен и уникален для конкретного NFT-подарка.
    """

    gift = (
        item.get(
            "gift"
        )
        or {}
    )

    key = gift.get("id")

    if key is None:
        # fallback: если gift.id отсутствует, пробуем sale-поля
        key = (
            item.get("giftSaleId")
            or item.get("saleId")
            or item.get("sale", {}).get("id")
        )

    if key is None:
        return None

    return str(key)


# ============================================================
# ADD SEEN ID
# ============================================================

def add_seen_id(
    item_id: str,
):

    seen_item_ids.add(
        item_id
    )

    # Ограничиваем память.
    if len(seen_item_ids) > MAX_SEEN_IDS:

        # set не хранит порядок,
        # поэтому полностью чистим часть
        # только при достижении лимита.
        remove_count = (
            len(seen_item_ids)
            - MAX_SEEN_IDS
        )

        if remove_count > 0:

            for old_id in list(
                seen_item_ids
            )[:remove_count]:

                seen_item_ids.discard(
                    old_id
                )


# ============================================================
# POLLING LOOP
# ============================================================

async def poll_feed_loop(telegram_id: int):

    session = get_or_create_session(telegram_id)
    token = set_current_session(session)

    try:

        print(
            "🚀 Снайпер запущен!"
        )

        first_run = True

        timeout = httpx.Timeout(
            connect=5.0,
            read=15.0,
            write=10.0,
            pool=10.0,
        )

        limits = httpx.Limits(
            max_connections=60,
            max_keepalive_connections=40,
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
        ) as client:

            while True:

                # ------------------------------------------------
                # Если токен мёртв — выходим из цикла совсем.
                # Запустить заново сможет только /auth или inline.
                # ------------------------------------------------
                if not session.auth_ok:
                    logger.info(
                        f"🛑 [user {session.telegram_id}] "
                        f"poll_feed_loop остановлен: "
                        f"AUTH_TOKEN недействителен."
                    )
                    return

                # ------------------------------------------------
                # Окно мониторинга истекло (30 минут) — выходим.
                # Это основное отличие от старого «вечного» цикла.
                # ------------------------------------------------
                if not session.feed_active():
                    if not session._feed_expiry_notified:
                        session._feed_expiry_notified = True
                        logger.info(
                            f"⏹ [user {session.telegram_id}] "
                            f"окно feed закончилось, опрос остановлен"
                        )
                        asyncio.create_task(
                            notify_feed_stopped(session.telegram_id)
                        )
                    return

                try:

                    feed_data = (
                        await get_feed(
                            client
                        )
                    )

                    if (
                        feed_data
                        and "items"
                        in feed_data
                    ):

                        items = (
                            feed_data.get(
                                "items"
                            )
                            or []
                        )

                        # ====================================================
                        # FIRST RUN
                        # ====================================================

                        if first_run:

                            for item in items:

                                item_key = (
                                    get_feed_item_key(
                                        item
                                    )
                                )

                                if item_key:

                                    add_seen_id(
                                        item_key
                                    )

                            first_run = False

                            print(
                                "✅ Инициализация "
                                "завершена. "
                                f"Запомнено лотов: "
                                f"{len(seen_item_ids)}"
                            )

                        # ====================================================
                        # NEW EVENTS
                        # ====================================================

                        else:
                            async def _bounded_notify(it):
                                async with session.notify_sem:
                                    try:
                                        await send_telegram_notification(
                                            client, it
                                        )
                                    except Exception as e:
                                        print(f"⚠️ notify err: {e}")

                            tasks = []
                            for item in reversed(items):
                                try:
                                    raw_res = should_ignore_event(item)
                                    if isinstance(raw_res, (tuple, list)):
                                        ignored = raw_res[0]
                                    else:
                                        ignored = bool(raw_res)

                                    if ignored:
                                        continue

                                    item_key = get_feed_item_key(item)
                                    if not item_key:
                                        continue

                                    # Тумблер «Защита от повторов»:
                                    # при выключении один и тот же лот
                                    # может обрабатываться повторно.
                                    if (
                                        SETTINGS.get("dedup", True)
                                        and item_key in seen_item_ids
                                    ):
                                        continue

                                    add_seen_id(item_key)
                                    tasks.append(
                                        asyncio.create_task(
                                            _bounded_notify(item)
                                        )
                                    )
                                except Exception as inner_e:
                                    print(
                                        f"⚠️ распаковка item: {inner_e}"
                                    )
                                    continue

                            if tasks:
                                await asyncio.gather(
                                    *tasks, return_exceptions=True
                                )

                    # --------------------------------------------------------
                    # POLL INTERVAL (ИЗ WEBAPP)
                    # --------------------------------------------------------

                    # Если сессия стала мёртвой в процессе итерации —
                    # не спим «как обычно», а сразу уходим на выход.
                    if not session.auth_ok:
                        continue

                    sleep_time = max(
                        0.2,
                        SETTINGS.get("poll_interval", 3.0),
                    )

                    # Не «просыпаемся» уже после закрытия окна.
                    remaining = (
                        session.feed_enabled_until - time.time()
                    )
                    if remaining <= 0:
                        continue

                    await asyncio.sleep(
                        min(sleep_time, remaining)
                    )

                except asyncio.CancelledError:

                    print(
                        "🛑 Polling loop остановлен."
                    )

                    raise

                except Exception as e:

                    print(
                        f"⚠️ Ошибка "
                        f"poll_feed_loop: {e}"
                    )

                    await asyncio.sleep(
                        3
                    )


    finally:
        reset_current_session(token)
        # Убираем «висячую» запись, чтобы start_user_polling
        # мог корректно перезапустить loop после /auth.
        USER_POLL_TASKS.pop(session.telegram_id, None)


# Флаг запуска ордеров хранится отдельно для каждого пользователя.


# ============================================================
# 1. ТЕСТОВЫЙ ЗАПУСК (БЕЗ СОЗДАНИЯ ОРДЕРОВ)
# ============================================================
@dp.message(F.text.in_(["/test_orders", "🧪 Тест ордеров"]))
async def cmd_test_orders(message: Message):
    session = get_current_session()
    if not session.auth_token:
        await message.answer("🔐 Сначала привяжи AUTH_TOKEN через /auth.")
        return

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await message.answer("🚫 Функция ордеров недоступна")
        return
    # --- /блокировка ордеров ---

    msg = await message.answer(
        "🧪 <b>Запускаю TEST MODE...</b>\n\n"
        "Реальные ордера создаваться НЕ будут.\n"
        "Проверяю первые 20 позиций.",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            result = await orders_mgr.process_orders_file(
                client=client,
                file_path="orders_to_create.json",
                dry_run=True,
                test_limit=20,
                discount_1=SETTINGS.get("order_discount_1", 30.0),
                discount_2=SETTINGS.get("order_discount_2", 50.0),
            )

        await msg.edit_text(
            "🧪 <b>TEST MODE завершён</b>\n\n"
            f"📋 Проверено: <b>{result['processed']}</b>\n"
            f"✅ Успешно рассчитано: <b>{result['success']}</b>\n"
            f"⏭ Пропущено: <b>{result['skipped']}</b>\n"
            f"❌ Ошибок: <b>{result['failed']}</b>\n\n"
            "🚫 <b>Ни одного реального ордера создано не было.</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception(f"Ошибка TEST MODE: {e}")
        await msg.edit_text(
            f"❌ <b>Ошибка тестового запуска:</b>\n\n<code>{str(e)[:1000]}</code>",
            parse_mode=ParseMode.HTML,
        )

# ============================================================
# РАСШИРЕННЫЙ ТЕСТ ВЫБОРКИ (50 ПОЗИЦИЙ)
# ============================================================
@dp.message(F.text.in_(["/test_sample", "🔍 Тест выборки (50)"]))
async def cmd_test_sample(message: Message):
    session = get_current_session()
    if not session.auth_token:
        await message.answer("🔐 Сначала привяжи AUTH_TOKEN через /auth.")
        return

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await message.answer("🚫 Функция ордеров недоступна")
        return
    # --- /блокировка ордеров ---

    msg = await message.answer(
        "🔍 <b>Запускаю выборочную проверку (50 позиций)...</b>\n\n"
        "Проверяю обычные цвета, Black и Onyx Black.",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            result = await orders_mgr.sync_orders_file(
                client=client,
                file_path="orders_to_create.json",
                dry_run=True,
                test_limit=50,
                discount_1=SETTINGS.get("order_discount_1", 30.0),
                discount_2=SETTINGS.get("order_discount_2", 50.0),
            )

        await msg.edit_text(
            "🧪 <b>Расширенный тест завершён</b>\n\n"
            f"📋 Проверено: <b>{result['processed']}</b>\n"
            f"✅ Рассчитано: <b>{result['success']}</b>\n"
            f"⏭ Пропущено (нет рынка): <b>{result['skipped']}</b>\n"
            f"❌ Ошибок: <b>{result['failed']}</b>\n\n"
            "Посмотри логи в консоли, чтобы увидеть детализацию по найденным лотам Black/Onyx.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception(f"Ошибка расширенного теста: {e}")
        await msg.edit_text(
            f"❌ <b>Ошибка теста:</b>\n\n<code>{str(e)[:1000]}</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# ТОЧЕЧНАЯ ПРОВЕРКА ОТМЕНЫ ОДНОГО ОРДЕРА
# ============================================================
@dp.message(F.text == "🧪 Отменить 1 ордер")
async def cmd_cancel_one_order_test(message: Message):
    session = get_current_session()
    if not session.auth_token:
        await message.answer("🔐 Сначала привяжи AUTH_TOKEN через /auth.")
        return

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await message.answer("🚫 Функция ордеров недоступна")
        return
    # --- /блокировка ордеров ---

    async with httpx.AsyncClient(timeout=15.0) as client:
        orders = await orders_mgr.get_my_orders(client, count=1)
        
        if not orders:
            await message.answer("ℹ️ У вас нет активных ордеров для проверки.")
            return

        target = orders[0]
        order_id = target.get("id") or target.get("orderId")
        coll = target.get("collectionName", "—")
        model = target.get("modelName", "—")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Подтвердить отмену", 
                        callback_data=f"confirm_cancel_one:{order_id}"
                    ),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_start_orders"),
                ]
            ]
        )

        await message.answer(
            f"🧪 <b>ТЕСТ ЭНДПОИНТА ОТМЕНЫ</b>\n\n"
            f"Найден ордер:\n"
            f"• Коллекция: <b>{coll}</b>\n"
            f"• Модель: <b>{model}</b>\n"
            f"• ID: <code>{order_id}</code>\n\n"
            f"⚠️ Нажмите кнопку ниже, чтобы проверить отмену <b>ОДНОГО</b> ордера.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


@dp.callback_query(F.data.startswith("confirm_cancel_one:"))
async def process_cancel_one_callback(callback: CallbackQuery):
    session = get_current_session()

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await callback.answer("🚫 Функция ордеров недоступна", show_alert=True)
        return
    # --- /блокировка ордеров ---

    order_id = callback.data.split(":")[1]
    await callback.answer()
    
    msg = await callback.message.edit_text("⏳ Отправка запроса на отмену...")

    async with httpx.AsyncClient(timeout=15.0) as client:
        ok = await orders_mgr.cancel_order(client, order_id)

    if ok:
        await msg.edit_text(
            f"✅ <b>Успешно!</b>\n\nЭндпоинт отменил ордер <code>{order_id}</code>.\n"
            f"Теперь можно безопасно использовать массовое снятие.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await msg.edit_text(
            f"❌ <b>Ошибка!</b>\n\nНе удалось отменить ордер. Проверьте логи консоли.",
            parse_mode=ParseMode.HTML,
        )

# ============================================================
# 2. ЗАПРОС ПОДТВЕРЖДЕНИЯ ДЛЯ РЕАЛЬНОГО ЗАПУСКА
# ============================================================
@dp.message(F.text.in_(["/start_orders", "🚀 Запустить ордера"]))
async def cmd_start_orders_confirm(message: Message):
    session = get_current_session()

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await message.answer("🚫 Функция ордеров недоступна")
        return
    # --- /блокировка ордеров ---

    if session.orders_running:
        await message.answer("⚠️ Массовая установка ордеров уже выполняется!")
        return

    # Создаем инлайн-кнопки подтверждения
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ ДА, ЗАПУСКАЕМ", callback_data="confirm_start_orders"),
                InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="cancel_start_orders"),
            ]
        ]
    )

    await message.answer(
        "⚠️ <b>ВНИМАНИЕ! Вы запускаете РЕАЛЬНУЮ установку ордеров.</b>\n\n"
        "Будут отправлены запросы на маркетплейс.\n"
        "Вы уверены, что хотите продолжить?",
        reply_markup=confirm_kb,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# 3. ОБРАБОТКА НАЖАТИЙ ИНЛАЙН-КНОПОК ПОДТВЕРЖДЕНИЯ
# ============================================================
@dp.callback_query(F.data == "cancel_start_orders")
async def process_cancel_orders(callback: CallbackQuery):
    await callback.message.edit_text("❌ Запуск массовых ордеров отменён.")
    await callback.answer()


@dp.callback_query(F.data == "confirm_start_orders")
async def process_start_orders(callback: CallbackQuery):
    session = get_current_session()

    # --- блокировка ордеров ---
    if orders_blocked(session):
        await callback.answer("🚫 Функция ордеров недоступна", show_alert=True)
        return
    # --- /блокировка ордеров ---

    if session.orders_running:
        await callback.answer("Уже запущено!", show_alert=True)
        return

    session.orders_running = True
    await callback.answer()
    
    msg = await callback.message.edit_text(
        "🚀 <b>Запускаю массовую установку ордеров...</b>\n\n"
        "Процесс может занять некоторое время.",
        parse_mode=ParseMode.HTML,
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            result = await orders_mgr.sync_orders_file(
                client=client,
                file_path="orders_to_create.json",
                dry_run=False,
                discount_1=SETTINGS.get("order_discount_1", 30.0),
                discount_2=SETTINGS.get("order_discount_2", 50.0),
            )

        await msg.edit_text(
            "🚀 <b>Синхронизация завершена</b>\n\n"
            f"📋 Обработано коллекций: <b>{result['processed']}</b>\n"
            f"✅ Уже стояло нужных: <b>{result['existing_kept']}</b>\n"
            f"🆕 Создано новых: <b>{result['success']}</b>\n"
            f"⏭ Пропущено: <b>{result['skipped']}</b>\n"
            f"❌ Ошибок: <b>{result['failed']}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception(f"Ошибка массовой установки ордеров: {e}")
        await msg.edit_text(
            f"❌ <b>Ошибка массовой установки:</b>\n\n<code>{str(e)[:1000]}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        session.orders_running = False

# --- БЛОК РАСЧЁТА И ОТОБРАЖЕНИЯ ПРИБЫЛИ ---

def make_profit_keyboard(months: list, current_index: int) -> InlineKeyboardMarkup:
    """Создаёт инлайн-кнопки переключения между месяцами."""
    buttons = []
    
    prev_btn = InlineKeyboardButton(
        text="⬅️ Назад", 
        callback_data=f"profit_page:{current_index - 1}"
    ) if current_index > 0 else None

    next_btn = InlineKeyboardButton(
        text="Вперёд ➡️", 
        callback_data=f"profit_page:{current_index + 1}"
    ) if current_index < len(months) - 1 else None

    row = []
    if prev_btn:
        row.append(prev_btn)
    if next_btn:
        row.append(next_btn)
        
    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_profit_view(months_data: dict, month_key: str) -> str:
    """Формирует текст отчёта за конкретный месяц."""
    telegram_message_limit = 4096
    report_safety_margin = 200
    data = months_data.get(month_key, {})
    profit_ton = data.get("profit_nano", 0) / 1_000_000_000
    sales_count = data.get("sales_count", 0)
    details = data.get("details", [])

    text = (
        f"📈 <b>Отчёт по прибыли за {month_key}</b>\n\n"
        f"💵 <b>Чистая прибыль:</b> <code>{profit_ton:+.2f} TON</code>\n"
        f"🏷 <b>Продаж за месяц:</b> {sales_count} шт.\n"
        "───────────────\n"
        "<b>Последние сделки:</b>\n"
    )

    if not details:
        text += "<i>Нет зафиксированных продаж за этот период.</i>\n"
    else:
        for item in details[-50:]:
            deal_text = (
                f"• <b>{item['title']}</b>\n"
                f"  Куплен: <code>{item['buy_ton']:.2f} TON</code> | "
                f"Продан: <code>{item['sell_ton']:.2f} TON</code>\n"
                f"  Прибыль: <b><code>{item['profit_ton']:+.2f} TON</code></b>\n"
            )

            if len(text) + len(deal_text) > telegram_message_limit - report_safety_margin:
                text += "\n<i>Показаны не все сделки: отчёт слишком длинный.</i>\n"
                break

            text += deal_text

    return text


@dp.message(F.text.in_(["/profit", "📈 Прибыль (P&L)"]))
async def cmd_profit(message: Message):
    session = get_current_session()

    if not session.auth_token:
        await message.answer("🔐 Сначала привяжи AUTH_TOKEN через /auth.")
        return

    msg = await message.answer("🔄 Загрузка и расчёт истории MRKT...")

    async with httpx.AsyncClient(timeout=15.0) as client:
        raw_history = await profit_tracker.fetch_all_history(client)
        monthly_stats = profit_tracker.calculate_monthly_profit(raw_history)

    # --- лог в консоль ---
    total_profit_nano = sum(
        m.get("profit_nano", 0) for m in monthly_stats.values()
    )
    total_sales = sum(
        m.get("sales_count", 0) for m in monthly_stats.values()
    )
    months_count = len(monthly_stats)

    # разбивка по месяцам
    months_breakdown = ", ".join(
        f"{mk}:{monthly_stats[mk]['profit_nano'] / NANO_TON:+.2f}"
        for mk in sorted(monthly_stats.keys())
    )

    log_user_action(
        session,
        action="📈 Прибыль (P&L)",
        extra=(
            f"months={months_count} | "
            f"sales={total_sales} | "
            f"profit={total_profit_nano / NANO_TON:+.2f} TON | "
            f"[{months_breakdown}]"
        ),
    )
    # --- /лог ---

    save_pnl_cache(session.telegram_id, monthly_stats)

    # ---- Отправка в Google Sheet (если URL привязан) ----
    sheet_status = ""

    if session.apps_script_url:
        trades_for_sheet = build_trades_for_sheet(raw_history)

        if trades_for_sheet:
            sync_result = await sync_trades_to_sheet(
                script_url=session.apps_script_url,
                trades=trades_for_sheet,
            )
            if sync_result["ok"]:
                sheet_status = (
                    f"\n\n✅ В Google Таблицу добавлено "
                    f"{sync_result['added']} новых сделок "
                    f"(пропущено дубликатов: {sync_result['skipped']})."
                )
            else:
                sheet_status = (
                    f"\n\n⚠️ Google Sheet не принял данные: "
                    f"<code>{sync_result.get('error')}</code>"
                )
        else:
            sheet_status = "\n\nℹ️ Сделок для таблицы не найдено."

    if not monthly_stats:
        await msg.edit_text(
            "ℹ️ История сделок пуста или не удалось получить данные."
            + sheet_status,
            parse_mode=ParseMode.HTML,
        )
        return

    sorted_months = sorted(monthly_stats.keys())
    latest_index = len(sorted_months) - 1
    selected_month = sorted_months[latest_index]

    text = await render_profit_view(monthly_stats, selected_month)
    kb = make_profit_keyboard(sorted_months, latest_index)

    await msg.edit_text(
        text + sheet_status,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("profit_page:"))
async def process_profit_pagination(callback: CallbackQuery):
    session = get_current_session()
    if not session.auth_token:
        await callback.answer("Сначала привяжи AUTH_TOKEN через /auth.", show_alert=True)
        return

    """Обработка клика по инлайн-кнопкам ⬅️ / ➡️."""
    target_idx = int(callback.data.split(":")[1])

    # --- лог в консоль ---
    log_user_action(
        session,
        action="📈 P&L (листалка)",
        extra=f"page_index={target_idx}",
    )
    # --- /лог ---
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        raw_history = await profit_tracker.fetch_all_history(client)
        monthly_stats = profit_tracker.calculate_monthly_profit(raw_history)

    sorted_months = sorted(monthly_stats.keys())
    
    if 0 <= target_idx < len(sorted_months):
        selected_month = sorted_months[target_idx]
        text = await render_profit_view(monthly_stats, selected_month)
        kb = make_profit_keyboard(sorted_months, target_idx)
        
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    
    await callback.answer()

# ============================================================
# MAIN
# ============================================================

async def start_scheduled_tasks():
    """
    Поднимает фоновые циклы после старта процесса.

    Feed НЕ запускается сам: только если пользователь ранее включил
    окно мониторинга и оно ещё не истекло. Авто-обновление ордеров
    запускается только если оно включено в настройках.
    """
    for telegram_id, session in list(USER_SESSIONS.items()):
        if not session.auth_token:
            continue

        if session.feed_active():
            await start_user_polling(telegram_id)

        if session.settings.get("auto_refresh_orders", False):
            await start_order_refresh_loop(telegram_id)


async def main():
    print("========================================")
    print("     MRKT SNIPER & AUTOMATION BOT")
    print("========================================")
    #print("🤖 AUTO BUY: управляется индивидуально для каждого пользователя через /settings")
    #print(f"📩 AUTO OFFER: {'ON' if SETTINGS['auto_offer'] else 'OFF'}")
    #print(f"📊 BUY MULTIPLIER: {SETTINGS['max_multiplier']}")
    #print(f"📩 OFFER MULTIPLIER: {SETTINGS['offer_multiplier']}")
    print(f"🎯 VALID MATCHES: {len(VALID_PAIRS)}")
    print("========================================")

    await start_scheduled_tasks()

    try:
        while True:
            try:
                await dp.start_polling(bot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(
                    f"⚠️ Ошибка Telegram polling: {e}"
                )
                await asyncio.sleep(5)

    finally:
        for telegram_id in list(
            USER_POLL_TASKS.keys()
        ):
            await stop_user_polling(
                telegram_id
            )

        await bot.session.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )

    asyncio.run(
        main()
    )