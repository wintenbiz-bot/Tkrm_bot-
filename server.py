"""
Единый сервис для Render:
  • Telegram-бот (aiogram polling) — в том же процессе;
  • Mini App (статический web/index.html);
  • внутренний API для безопасного входа и управления feed.

Запуск:
    uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager

import httpx

# MRKT известный чувствительностью к TLS-отпечатку: их рабочий пример
# использует curl_cffi. Если библиотеки нет — откатываемся на httpx.
try:
    from curl_cffi import AsyncSession as CurlAsyncSession
except ImportError:  # pragma: no cover
    CurlAsyncSession = None

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import main_multiuser as core
from monochrome_data import DEFAULT_MONOCHROME_FILE, load_monochrome_pairs

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or core.BOT_TOKEN

MAX_INIT_DATA_AGE = 24 * 60 * 60


# ============================================================
# ПРОВЕРКА TELEGRAM initData
# ============================================================

def validate_init_data(init_data: str) -> int:
    """
    Проверяет подпись Telegram WebApp и возвращает telegram_id.

    Никаких «доверяем initDataUnsafe»: подпись считается на сервере
    через HMAC-SHA256 с ключом, выведенным из BOT_TOKEN.
    """
    if not init_data or not isinstance(init_data, str):
        raise HTTPException(400, "init_data обязателен")

    parsed = dict(
        urllib.parse.parse_qsl(
            init_data,
            keep_blank_values=True,
        )
    )

    received_hash = parsed.pop("hash", "")
    if not received_hash:
        raise HTTPException(401, "В initData нет hash")

    auth_date = int(parsed.get("auth_date") or 0)
    if auth_date and time.time() - auth_date > MAX_INIT_DATA_AGE:
        raise HTTPException(401, "initData устарел, открой Mini App заново")

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "Неверная подпись initData")

    try:
        user = json.loads(parsed.get("user") or "{}")
    except Exception:
        raise HTTPException(400, "Не удалось прочитать user из initData")

    telegram_id = int(user.get("id") or 0)

    if telegram_id not in core.ALLOWED_USER_IDS:
        raise HTTPException(403, "Доступ закрыт")

    return telegram_id


def session_state(telegram_id: int) -> dict:
    session = core.get_or_create_session(telegram_id)
    return {
        "telegram_id": telegram_id,
        "auth_configured": bool(session.auth_token),
        "auth_ok": bool(session.auth_ok),
        "feed_enabled": session.feed_active(),
        "feed_seconds_left": session.feed_seconds_left(),
        "feed_window_seconds": core.FEED_WINDOW_SECONDS,
        "settings": dict(session.settings),
    }


async def verify_mrkt_token(token: str) -> bool:
    """
    Не сохраняем мусор: проверяем токен через /balance.

    MRKT чувствителен к TLS-отпечатку, поэтому используем тот же
    curl_cffi, что и авто-обмен initData. Иначе валидный токен
    может быть отклонён просто из-за «не того» ClientHello.
    """
    headers = dict(core.BASE_HEADERS)
    headers["authorization"] = token
    headers["cookie"] = f"access_token={token}"

    try:
        if CurlAsyncSession is not None:
            async with CurlAsyncSession(impersonate="chrome") as client:
                response = await client.get(
                    core.BALANCE_URL,
                    headers=headers,
                )
        else:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.get(
                    core.BALANCE_URL,
                    headers=headers,
                )
        logger.info("🔎 Проверка MRKT-токена: /balance -> %s", response.status_code)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"⚠️ Не удалось проверить MRKT-токен: {e}")
        return False


# ============================================================
# LIFESPAN: бот живёт внутри FastAPI
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.start_scheduled_tasks()

    polling_task = asyncio.create_task(
        core.dp.start_polling(core.bot),
    )

    try:
        yield
    finally:
        polling_task.cancel()
        try:
            await polling_task
        except (asyncio.CancelledError, Exception):
            pass

        for telegram_id in list(core.USER_POLL_TASKS):
            await core.stop_user_polling(telegram_id)

        for telegram_id in list(core.USER_ORDER_REFRESH_TASKS):
            await core.stop_order_refresh_loop(telegram_id)

        try:
            await core.bot.session.close()
        except Exception:
            pass


app = FastAPI(
    title="TKRM Mini App API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# СТАРЫЕ ЭНДПОИНТЫ (совместимость)
# ============================================================

@app.get("/api/health")
def health():
    return {"ok": True, "service": "tkrm"}


@app.get("/api/config")
def get_config():
    """Возвращает список валидных пар для Mini App."""
    try:
        pairs = load_monochrome_pairs(
            DEFAULT_MONOCHROME_FILE,
            base_dir=BASE_DIR,
        )
    except Exception:
        pairs = []

    matches = [
        {
            "collection": collection,
            "model_id": model,
            "backdrop_id": backdrop,
            "pair_key": f"{collection}|{model}|{backdrop}",
            "score": 1.0,
        }
        for collection, model, backdrop in pairs
    ]
    return {"matches": matches}


@app.get("/api/pnl")
def get_pnl(telegram_id: str = Query(...)):
    cache_path = core.PNL_CACHE_FILE
    if not os.path.exists(cache_path):
        return {"months": []}

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            all_data = json.load(f)
    except Exception as e:
        return {"error": str(e), "months": []}

    user_data = all_data.get(str(telegram_id), {})
    months = [
        {
            "month": month,
            "profit_ton": user_data[month].get("profit_ton", 0),
            "sales_count": user_data[month].get("sales_count", 0),
        }
        for month in sorted(user_data.keys())
    ]
    return {"months": months}


# ============================================================
# НОВЫЕ ЭНДПОИНТЫ MINI APP
# ============================================================

@app.post("/api/session/state")
async def api_session_state(payload: dict = Body(...)):
    telegram_id = validate_init_data(payload.get("init_data"))
    return session_state(telegram_id)


@app.post("/api/session/auth")
async def api_session_auth(payload: dict = Body(...)):
    """
    Два режима:

    1) без `token` — пытаемся обменять initData нашего бота на MRKT-токен.
       MRKT обычно проверяет подпись своим bot token и возвращает 403 —
       тогда пользователь один раз вставляет токен вручную.
    2) с `token` — пользователь вставил токен вручную (рабочий путь).
    """
    init_data = payload.get("init_data")
    telegram_id = validate_init_data(init_data)

    token = str(payload.get("token") or "").strip()

    if not token:
        # Повторяем payload MRKT Web App:


        # {"data": initData, "photo": "...", "appId": null}
        try:
            parsed = dict(
                urllib.parse.parse_qsl(init_data, keep_blank_values=True)
            )
            user = json.loads(parsed.get("user") or "{}")
        except Exception:
            user = {}

        auth_payload = {"data": init_data, "appId": None}
        photo = user.get("photo_url")
        if photo:
            auth_payload["photo"] = photo

        try:
            if CurlAsyncSession is not None:
                async with CurlAsyncSession(impersonate="chrome") as client:
                    response = await client.post(
                        core.AUTH_URL,
                        json=auth_payload,
                    )
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(
                        core.AUTH_URL,
                        json=auth_payload,
                    )
        except Exception as e:
            logger.warning("⚠️ MRKT auth network error: %s", e)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "network_error",
                    "detail": str(e)[:200],
                },
                status_code=502,
            )

        if response.status_code != 200:
            logger.warning(
                "⚠️ MRKT auth rejected: status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "mrkt_auth_failed",
                    "detail": response.text[:200],
                },
                status_code=502,
            )

        try:
            token = str(
                (response.json() or {}).get("token") or ""
            ).strip()
        except Exception:
            token = ""

        if not token:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "empty_token",
                    "detail": "MRKT не вернул token",
                },
                status_code=502,
            )
    else:
        if not await verify_mrkt_token(token):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_token",
                    "detail": "MRKT отклонил этот токен (/balance)",
                },
                status_code=400,
            )

    await core.apply_auth_token(
        telegram_id,
        token,
        source="miniapp_api",
    )

    return {
        "ok": True,
        "auth_configured": True,
        **session_state(telegram_id),
    }


@app.post("/api/session/feed")
async def api_session_feed(payload: dict = Body(...)):
    telegram_id = validate_init_data(payload.get("init_data"))
    enabled = bool(payload.get("enabled", True))

    if enabled:
        try:
            seconds = int(
                payload.get("seconds")
                or core.FEED_WINDOW_SECONDS
            )
        except (TypeError, ValueError):
            seconds = core.FEED_WINDOW_SECONDS

        try:
            await core.enable_feed_monitoring(telegram_id, seconds)
        except ValueError as e:
            return JSONResponse(
                {"ok": False, "error": "no_token", "detail": str(e)},
                status_code=400,
            )
    else:
        await core.disable_feed_monitoring(telegram_id)

    return {"ok": True, **session_state(telegram_id)}


@app.post("/api/session/settings")
async def api_session_settings(payload: dict = Body(...)):
    telegram_id = validate_init_data(payload.get("init_data"))
    session = core.get_or_create_session(telegram_id)

    core.apply_settings_dict(session, payload.get("settings") or {})

    if session.settings.get("auto_refresh_orders", False):
        await core.start_order_refresh_loop(telegram_id)
    else:
        await core.stop_order_refresh_loop(telegram_id)

    return {"ok": True, **session_state(telegram_id)}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Браузер всё равно просит /favicon.ico — убираем 404 из логов."""
    return Response(status_code=204)


# ============================================================
# СТАТИКА MINI APP — ПОСЛЕ API, ЧТОБЫ НЕ ПЕРЕХВАТЫВАТЬ /api/*
# ============================================================

app.mount(
    "/",
    StaticFiles(directory=WEB_DIR, html=True),
    name="miniapp",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
