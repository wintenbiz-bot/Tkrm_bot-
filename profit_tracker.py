import asyncio
import hashlib
import httpx
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

HISTORY_URL = "https://api.tgmrkt.io/api/v1/history"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# На Render все меняющиеся файлы кладём на постоянный диск.
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "purchase_cache.json")

# Кэш полной истории MRKT по каждому Telegram-пользователю.
# В нём лежат сырые события в том виде, в котором их отдал MRKT.
HISTORY_CACHE_DIR = os.path.join(DATA_DIR, "history_cache")
HISTORY_CACHE_VERSION = 1

# Если кэш обновлялся меньше N секунд назад — сеть не трогаем.
HISTORY_CACHE_TTL = 20.0

# Сколько страниц максимум докачивать при инкрементальном обновлении.
# 500 страниц * 20 событий = 10000 событий — столько же, сколько
# у полного сканирования. Это нужно, чтобы после долгого простоя
# бота не оставалось «дыры» в истории.
INCREMENTAL_MAX_PAGES = 500


class ProfitTracker:
    def __init__(self, headers: dict, telegram_id: int = 0):
        self.headers = headers
        self.telegram_id = int(telegram_id or 0)
        self.purchases_cache = self._load_cache()

        # Привязываем кэш к конкретному MRKT-токену: если пользователь
        # сделает /auth с другим аккаунтом, старый кэш не подмешается.
        auth_token = str(headers.get("authorization") or "")
        self.auth_fingerprint = hashlib.sha256(
            auth_token.encode("utf-8")
        ).hexdigest()[:16]

        self._history_lock = asyncio.Lock()
        self.history_items: list = []
        self.history_keys: set[str] = set()
        self.history_initialized: bool = False
        self.history_updated_ts: float = 0.0
        self.history_last_full_scan: str | None = None
        self._history_cache_path = self._make_history_cache_path()
        self._load_history_cache()

    def _make_history_cache_path(self) -> str:
        os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)
        name = f"{self.telegram_id}.json" if self.telegram_id else "default.json"
        return os.path.join(HISTORY_CACHE_DIR, name)

    # ------------------------------------------------------------------
    # КЭШ ЦЕН ПОКУПОК (общий purchase_cache.json)
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict:
        """Загружает сохраненные покупки из локального файла."""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                print(f"⚠️ Ошибка чтения кэша покупок: {e}")
        return {}

    def _save_cache(self):
        """Сохраняет текущий реестр покупок в файл."""
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    self.purchases_cache,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            print(f"⚠️ Ошибка сохранения кэша покупок: {e}")

    # ------------------------------------------------------------------
    # КЛЮЧ СОБЫТИЯ
    # ------------------------------------------------------------------

    @staticmethod
    def _history_item_key(item: dict) -> str:
        """Стабильный ключ события для дедупликации кэша."""
        if not isinstance(item, dict):
            return ""

        raw_id = item.get("id")
        if raw_id not in (None, "", "None"):
            return f"id:{raw_id}"

        gift = item.get("gift") or {}

        key = "|".join([
            str(item.get("type") or ""),
            str(item.get("date") or ""),
            str(item.get("price") or item.get("amount") or ""),
            str(gift.get("id") or gift.get("receivedGiftId") or ""),
            str(gift.get("number") or gift.get("giftNumber") or ""),
            str(gift.get("collectionName") or gift.get("title") or ""),
        ])

        if key.strip("|"):
            return key

        # Совсем без опознавательных знаков — хотя бы детерминированный
        # отпечаток всего события, чтобы не дублировать его при обновлении.
        return "raw:" + json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    # ------------------------------------------------------------------
    # ЗАГРУЗКА / СОХРАНЕНИЕ ИСТОРИИ
    # ------------------------------------------------------------------

    def _load_history_cache(self):
        path = self._history_cache_path

        if not os.path.exists(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                return

            if int(data.get("version") or 0) != HISTORY_CACHE_VERSION:
                print(
                    f"📚 [user {self.telegram_id}] Кэш истории устарел "
                    f"(версия {data.get('version')}). Будет пересоздан."
                )
                return

            if str(data.get("auth_fingerprint") or "") != self.auth_fingerprint:
                print(
                    f"📚 [user {self.telegram_id}] Кэш истории относится "
                    f"к другому auth-токену. Будет пересоздан."
                )
                return

            items = data.get("items")
            if not isinstance(items, list):
                return

            self.history_items = [
                item for item in items if isinstance(item, dict)
            ]
            self.history_keys = {
                self._history_item_key(item)
                for item in self.history_items
            }
            self.history_keys.discard("")
            self.history_initialized = True
            self.history_updated_ts = float(data.get("updated_at") or 0.0)
            self.history_last_full_scan = data.get("last_full_scan")

            print(
                f"📚 [user {self.telegram_id}] История MRKT загружена "
                f"из кэша: {len(self.history_items)} событий."
            )

        except Exception as e:
            print(
                f"⚠️ Ошибка чтения кэша истории {path}: {e}"
            )
            self.history_items = []
            self.history_keys = set()
            self.history_initialized = False
            self.history_updated_ts = 0.0

    def _save_history_cache_sync(self):
        try:
            os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

            payload = {
                "version": HISTORY_CACHE_VERSION,
                "telegram_id": self.telegram_id,
                "auth_fingerprint": self.auth_fingerprint,
                "updated_at": float(
                    self.history_updated_ts or time.time()
                ),
                "last_full_scan": self.history_last_full_scan,
                "count": len(self.history_items),
                "items": self.history_items,
            }

            tmp = f"{self._history_cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    payload,
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(tmp, self._history_cache_path)

        except Exception as e:
            print(f"⚠️ Ошибка сохранения кэша истории: {e}")

    def _merge_history(self, new_items: list) -> int:
        """Добавляет новые события, убирает дубли и сортирует по дате."""
        merged = {}

        for item in new_items:
            if not isinstance(item, dict):
                continue
            key = self._history_item_key(item)
            if not key:
                continue
            merged[key] = item

        for item in self.history_items:
            if not isinstance(item, dict):
                continue
            key = self._history_item_key(item)
            if not key or key in merged:
                continue
            merged[key] = item

        items = list(merged.values())
        items.sort(
            key=lambda it: str(it.get("date") or ""),
            reverse=True,
        )

        self.history_items = items
        self.history_keys = {
            self._history_item_key(item)
            for item in items
        }
        self.history_keys.discard("")

        return len(items)

    async def _fetch_history_pages(
        self,
        client: httpx.AsyncClient,
        max_pages: int,
        stop_at_known: bool,
    ) -> tuple[list, bool, bool]:
        """
        Тянет историю с MRKT.

        stop_at_known=True — инкрементальный режим: идём от новых
        событий к старым и останавливаемся на странице, где встретили
        уже известное событие.

        Возвращает: (события, дошли_до_известного, успех).
        """
        fetched = []
        fetched_keys = set()
        from_date = None
        reached_known = False
        ok = False
        had_error = False
        pages = 0

        while pages < max_pages:
            params = {"limit": 20, "type": "All"}
            if from_date:
                params["from"] = from_date

            try:
                res = await client.get(
                    HISTORY_URL,
                    headers=self.headers,
                    params=params,
                )

                if res.status_code != 200:
                    print(
                        f"⚠️ История MRKT HTTP {res.status_code}: "
                        f"{res.text[:200]}"
                    )
                    had_error = True
                    break

                data = res.json()

            except Exception as e:
                print(f"⚠️ Ошибка запроса истории MRKT: {e}")
                had_error = True
                break

            ok = True

            if not isinstance(data, list) or not data:
                break

            page_has_known = False

            for item in data:
                if not isinstance(item, dict):
                    continue

                key = self._history_item_key(item)

                if (
                    stop_at_known
                    and key
                    and key in self.history_keys
                ):
                    page_has_known = True
                    continue

                if key and key in fetched_keys:
                    continue

                if key:
                    fetched_keys.add(key)

                fetched.append(item)

            if stop_at_known and page_has_known:
                reached_known = True
                break

            last_item = data[-1]
            next_from = last_item.get("date")

            if not next_from or len(data) < 20:
                break

            if next_from == from_date:
                break

            from_date = next_from
            pages += 1

        return fetched, reached_known, (ok and not had_error)

    async def fetch_all_history(
        self,
        client: httpx.AsyncClient,
        max_pages: int = 500,
        force_refresh: bool = False,
    ) -> list:
        """
        Возвращает историю MRKT.

        Первый вызов делает полное сканирование и сохраняет все события
        в history_cache/<telegram_id>.json. Следующие вызовы докачивают
        только новые события сверху и останавливаются на первом уже
        известном. Внутри HISTORY_CACHE_TTL сеть вообще не трогается.
        """
        async with self._history_lock:
            now = time.time()

            if (
                not force_refresh
                and self.history_initialized
                and self.history_updated_ts > 0
                and now - self.history_updated_ts < HISTORY_CACHE_TTL
            ):
                return list(self.history_items)

            # --------------------------------------------------------
            # ПЕРВЫЙ ЗАПУСК: полное сканирование
            # --------------------------------------------------------
            if not self.history_initialized:
                fetched, _, ok = await self._fetch_history_pages(
                    client=client,
                    max_pages=max_pages,
                    stop_at_known=False,
                )

                if not ok:
                    print(
                        f"⚠️ [user {self.telegram_id}] Не удалось "
                        f"загрузить историю MRKT (кэш пуст)."
                    )
                    return list(self.history_items)

                total = self._merge_history(fetched)
                self.history_initialized = True
                self.history_updated_ts = time.time()
                self.history_last_full_scan = datetime.now(
                    timezone.utc
                ).isoformat()

                await asyncio.to_thread(self._save_history_cache_sync)

                print(
                    f"📚 [user {self.telegram_id}] История MRKT: "
                    f"полное сканирование, событий={total}."
                )
                return list(self.history_items)

            # --------------------------------------------------------
            # ИНКРЕМЕНТАЛЬНОЕ ОБНОВЛЕНИЕ
            # --------------------------------------------------------
            fetched, reached_known, ok = await self._fetch_history_pages(
                client=client,
                max_pages=INCREMENTAL_MAX_PAGES,
                stop_at_known=True,
            )

            if not ok:
                # Сеть/API подвели — отдаём то, что уже сохранено.
                return list(self.history_items)

            before = len(self.history_items)
            total = self._merge_history(fetched)
            added = total - before
            self.history_updated_ts = time.time()

            # Перезаписываем файл только если данные реально изменились:
            # у больших историй файл может быть в несколько мегабайт.
            if added > 0:
                await asyncio.to_thread(self._save_history_cache_sync)

            if added > 0:
                print(
                    f"📚 [user {self.telegram_id}] История MRKT "
                    f"обновлена: +{added} событий "
                    f"(всего {total})."
                )
            elif not reached_known:
                print(
                    f"📚 [user {self.telegram_id}] История MRKT "
                    f"проверена: новых событий нет "
                    f"(всего {total})."
                )

            return list(self.history_items)

    # ==================================================================
    # НОВОЕ: поиск истории конкретного подарка по номеру
    # ==================================================================
    async def find_gift_by_number(
        self,
        client: httpx.AsyncClient,
        number: str,
    ) -> dict:
        """
        Ищет все события в истории MRKT, где gift.number == number.

        Возвращает:
        {
            "number": str,
            "collection": str,
            "title": str,
            "events": [
                {
                    "type": str,          # buy / sell / ...
                    "price_ton": float,
                    "price_nano": int,
                    "date": str,          # ISO
                    "month": str,         # YYYY-MM
                },
                ...
            ],
            "bought_ton": float | None,
            "bought_at": str | None,
            "sold_ton": float | None,
            "sold_at": str | None,
            "profit_ton": float | None,
        }
        """
        number = str(number).strip().lstrip("#")
        if not number:
            return {}

        history = await self.fetch_all_history(client)

        events = []
        collection = ""
        title = ""

        for item in history:
            gift = item.get("gift") or {}
            g_number = str(
                gift.get("number")
                or gift.get("giftNumber")
                or ""
            ).strip().lstrip("#")

            if g_number != number:
                continue

            event_type = str(item.get("type", "")).lower()
            price_nano = int(item.get("price") or item.get("amount") or 0)
            raw_date = item.get("date")

            month_key = ""
            if raw_date:
                try:
                    dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                    month_key = dt.strftime("%Y-%m")
                except ValueError:
                    pass

            collection = (
                gift.get("collectionName")
                or gift.get("title")
                or collection
            )
            title = gift.get("title") or title

            events.append({
                "type": event_type,
                "price_ton": price_nano / 1_000_000_000,
                "price_nano": price_nano,
                "date": raw_date,
                "month": month_key,
            })

        # Сортируем по дате (старые -> новые)
        events.sort(key=lambda x: x.get("date") or "")

        bought_nano = None
        sold_nano = None
        bought_at = None
        sold_at = None

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

        for ev in events:
            t = ev["type"]
            if t in BUY_TYPES and ev["price_nano"] > 0 and bought_nano is None:
                bought_nano = ev["price_nano"]
                bought_at = ev["date"]
            elif t in SELL_TYPES and ev["price_nano"] > 0 and sold_nano is None:
                sold_nano = ev["price_nano"]
                sold_at = ev["date"]

        profit_ton = None
        if bought_nano is not None and sold_nano is not None:
            profit_ton = (sold_nano - bought_nano) / 1_000_000_000

        return {
            "number": number,
            "collection": collection,
            "title": title,
            "events": events,
            "bought_ton": bought_nano / 1_000_000_000 if bought_nano is not None else None,
            "bought_at": bought_at,
            "sold_ton": sold_nano / 1_000_000_000 if sold_nano is not None else None,
            "sold_at": sold_at,
            "profit_ton": profit_ton,
        }

    # ==================================================================
    # Расчёт месячной прибыли (без изменений)
    # ==================================================================
    def calculate_monthly_profit(self, history_items: list) -> dict:
        # Используем постоянный кэш + текущую историю
        purchases = dict(self.purchases_cache)
        monthly_stats = defaultdict(lambda: {"profit_nano": 0, "sales_count": 0, "details": []})

        # Сортируем транзакции от старых к новым
        sorted_items = sorted(
            history_items,
            key=lambda x: x.get("date") or ""
        )

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

        for item in sorted_items:
            event_type = str(item.get("type", "")).lower()
            gift = item.get("gift") or {}

            rec_id = str(gift.get("receivedGiftId") or "").strip()
            gift_id = str(gift.get("id") or "").strip()

            keys_to_check = [k for k in [rec_id, gift_id] if k and k != "None"]
            if not keys_to_check:
                continue

            price_nano = int(item.get("price") or item.get("amount") or 0)

            # 1. Фиксируем цену покупки и добавляем в кэш
            if event_type in BUY_TYPES and price_nano > 0:
                for k in keys_to_check:
                    purchases[k] = price_nano
                    self.purchases_cache[k] = price_nano

            # 2. Обрабатываем продажу
            elif event_type in SELL_TYPES:
                raw_date = item.get("date")
                month_key = "Неизвестно"
                if raw_date:
                    try:
                        dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                        month_key = dt.strftime("%Y-%m")
                    except ValueError:
                        pass

                buy_price_nano = 0
                matched_key = None

                for k in keys_to_check:
                    if k in purchases:
                        buy_price_nano = purchases[k]
                        matched_key = k
                        break

                if buy_price_nano > 0:
                    profit_nano = price_nano - buy_price_nano
                    monthly_stats[month_key]["profit_nano"] += profit_nano
                    if matched_key:
                        purchases.pop(matched_key, None)
                else:
                    profit_nano = 0

                monthly_stats[month_key]["sales_count"] += 1

                coll_name = gift.get("collectionName") or gift.get("title") or "Подарок"
                num = gift.get("number") or gift.get("giftNumber") or ""

                monthly_stats[month_key]["details"].append({
                    "title": f"{coll_name} #{num}".strip() if num else str(coll_name),
                    "buy_ton": buy_price_nano / 1_000_000_000,
                    "sell_ton": price_nano / 1_000_000_000,
                    "profit_ton": profit_nano / 1_000_000_000,
                })

        # Сохраняем обновленный кэш покупок
        self._save_cache()

        return monthly_stats