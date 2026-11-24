# TKRM Control — MRKT Sniper Bot + Mini App

Telegram-бот для MRKT с отдельными сессиями пользователей и красивой Mini App-панелью.

## Что изменилось

### 1. AUTH_TOKEN без F12
Mini App по кнопке **«Обновить токен»** отправляет подписанный `initData` на наш API
(`POST /api/session/auth`). Сервер проверяет подпись Telegram (HMAC-SHA256 с `BOT_TOKEN`)
и обменивает `initData` на MRKT-токен через официальный
`POST https://api.tgmrkt.io/api/v1/auth`.

Если MRKT отклонит обмен (например, из-за привязки initData к другому боту),
Mini App покажет поле для ручной вставки токена — после сохранения он живёт
только на сервере в `users.json`. Повторять операцию каждый раз не нужно.

> Старый хардкод-токен бота удалён из `main_multiuser.py`.
> Если он когда-либо был в публичном репозитории — **отзови его у BotFather**.

> `users.json` в `.gitignore`, поэтому старые токены не попадут на Render.
> После первого деплоя открой Mini App и один раз нажми «Обновить токен» —
> дальше всё будет автоматически.

### 2. Feed по умолчанию выключен
Раньше бот постоянно опрашивал `api.tgmrkt.io/api/v1/feed`.
Теперь:

- по умолчанию мониторинг выключен — запросов к MRKT нет;
- включить можно кнопкой в Mini App, командой `/feed` или кнопкой «⏱ Feed 30 мин»;
- через **30 минут** мониторинг отключается сам, бот присылает одноразовое уведомление;
- досрочно: `/feed_off` или «⏹ Feed стоп»;
- статус: `/feed_status`;
- интервал опроса в настройках ограничен минимумом `0.2 c`.

Окно мониторинга сохраняется в `users.json`. После рестарта оно продолжится,
только если не истекло. Новые пользователи всегда стартуют с выключенным feed.

### 3. Mini App
Полностью переписанный `web/index.html`: тёмная тема Telegram, 4 вкладки,
живой статус токена и обратный отсчёт feed, быстрые действия, haptic,
безопасное сохранение через API с fallback на `tg.sendData`.

### 4. GitHub + Render
- `requirements.txt` — зависимости;
- `render.yaml` — Web Service с постоянным диском и health-check;
- `.gitignore` — исключает `users.json`, кэши и `.env`;
- FastAPI и aiogram запускаются **в одном процессе** (`server.py`), поэтому
  API и бот используют одну и ту же память сессий.

## Локальный запуск

```bash
python -m venv .venv
. .venv/Scripts/activate      # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env         # заполни BOT_TOKEN
uvicorn server:app --host 0.0.0.0 --port 8000
```

Бот начнёт polling внутри FastAPI. Mini App откроется на `http://localhost:8000`.

Альтернатива (только бот, без API): `python main_multiuser.py`.

## Деплой на Render

1. Сделай приватный репозиторий на GitHub и запушь код.
   Убедись, что `users.json` и `.env` **не попали** в коммит.
2. Render → **New → Web Service** → выбери репозиторий.
   Если `render.yaml` подхватится автоматически — проверь значения.
3. В Environment добавь:
   - `BOT_TOKEN` — токен из BotFather;
   - `WEBAPP_URL` — публичный URL сервиса, например `https://tkrm-control.onrender.com/`
     (тогда Mini App раздаётся прямо с Render и API same-origin);
   - `PUBLIC_URL` — тот же URL. Нужен только если Mini App лежит на GitHub Pages.

   Render автоматически задаёт `RENDER_EXTERNAL_URL`, и код подхватывает его сам —
   `WEBAPP_URL`/`PUBLIC_URL` можно не указывать, если Mini App раздаётся с Render.
4. В BotFather включи **Inline Mode** для бота (`/setinline`) и привяжи
   Button URL / Menu Button к `WEBAPP_URL`.
5. Не используй Free plan: он засыпает, а sniper должен быть онлайн постоянно.
   В `render.yaml` уже стоит `plan: starter` + диск на 1 GB.

### Если сервис создан вручную (не из Blueprint)

Render по умолчанию берёт Python **3.14**, а `aiogram 3.10` тянет
`pydantic-core 2.20.1`, у которого нет готового wheel под 3.14 — сборка
падает на Rust. В корне репозитория лежит `.python-version` с `3.11.9`,
поэтому версия будет принудительно понижена. Это работает и для
вручную созданного Web Service. Альтернатива — выставить переменную
окружения `PYTHON_VERSION=3.11.9` в Dashboard.

### Вариант с GitHub Pages

Можно задеплоить `web/` на GitHub Pages. Тогда:

- `WEBAPP_URL` = адрес Pages;
- `PUBLIC_URL` = адрес Render;
- бот сам добавит в URL Mini App параметр `?api=https://<render>`.

CORS на API разрешён, а `initData` всё равно проверяется подписью на сервере.

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота, обязателен |
| `WEBAPP_URL` | URL Mini App, который бот даёт пользователю |
| `PUBLIC_URL` | публичный API, если Mini App не на Render |
| `USERS_FILE` | путь к `users.json` (на Render — на диске) |
| `PNL_CACHE_FILE` | путь к кэшу P&L |
| `DATA_DIR` | каталог для `purchase_cache.json` и `history_cache/` |

## Важно про безопасность

- `initData` проверяется через HMAC с `BOT_TOKEN`; подмена `user.id` невозможна.
- Список разрешённых пользователей задаётся в `ALLOWED_USER_IDS`
  в `main_multiuser.py`.
- Токены MRKT хранятся только в `users.json` на сервере и не попадают в браузер.
- Если старый токен бота был в открытом доступе — отзови и создай новый.
