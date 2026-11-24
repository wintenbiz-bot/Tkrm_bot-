# monochrome_guard.py
import asyncio
import logging
import time

import httpx

from monochrome_data import DEFAULT_MONOCHROME_FILE, load_monochrome_pairs

logger = logging.getLogger(__name__)

SALING_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"

# Порог: floor * (1 + PRICE_MARGIN)
PRICE_MARGIN = 0.25

# Насколько сосед должен быть дешевле нашего лота, чтобы это
# считалось «плохим сигналом». 0 = блокировать при любой более
# низкой цене. 10 = блокировать только если сосед дешевле на 10%+.
MIN_DIFF_PERCENT = 10.0

# TTL кэша результата по (collection, model)
CACHE_TTL = 20.0

# Сколько листингов максимум тянуть с MRKT
FETCH_COUNT = 200
MAX_PAGES = 3


class MonochromeGuard:
    """
    Перед покупкой монохрома проверяет:
    есть ли в этой же коллекции и этой же модели
    другой монохромный фон, продающийся дешевле floor * 1.25.

    Если есть — покупку блокируем.
    """

    def __init__(
        self,
        monochrome_file: str = DEFAULT_MONOCHROME_FILE,
    ):
        self.monochrome_file = monochrome_file

        self._mono_floor_cache: dict[tuple[str, str], tuple[float, int]] = {}

        # collection_lower -> { model_lower -> set(backdrop_lower) }
        self._by_collection_model: dict[str, dict[str, set[str]]] = {}

        # (collection_lower, model_lower) -> (timestamp, cheap_bool)
        self._result_cache: dict[tuple[str, str], tuple[float, bool]] = {}

        # (collection_lower, model_lower) -> asyncio.Lock
        self._fetch_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

        self._load_matches()

    # ------------------------------------------------------------------
    # LOAD
    # ------------------------------------------------------------------

    def _load_matches(self):
        try:
            pairs = load_monochrome_pairs(self.monochrome_file)
        except Exception as e:
            logger.error(
                f"⚠️ MonochromeGuard: ошибка чтения "
                f"{self.monochrome_file}: {e}"
            )
            return

        by_cm: dict[str, dict[str, set[str]]] = {}

        for coll, model, back in pairs:
            by_cm.setdefault(coll, {}).setdefault(model, set()).add(back)

        self._by_collection_model = by_cm

        total_models = sum(len(m) for m in by_cm.values())
        logger.info(
            f"✅ MonochromeGuard: {len(by_cm)} коллекций, "
            f"{total_models} моделей загружено."
        )

    # ------------------------------------------------------------------
    # ПУБЛИЧНЫЙ API
    # ------------------------------------------------------------------

    def siblings_of(
        self,
        collection: str,
        model: str,
        current_backdrop: str,
    ) -> set[str]:
        """
        Возвращает ВСЕ монохромные фоны этой модели,
        ВКЛЮЧАЯ текущий.

        Фон в MRKT не уникален: на маркете могут лежать несколько
        листингов одной и той же пары (model, backdrop) от разных
        продавцов. Поэтому текущий фон тоже надо проверять —
        вдруг рядом дешевле такой же.
        """
        cmap = self._by_collection_model.get(
            str(collection).strip().lower(), {}
        )
        backs = cmap.get(str(model).strip().lower(), set())
        return set(backs)

    def has_siblings(self, collection: str, model: str) -> bool:
        """
        Есть ли вообще смысл делать проверку для этой модели.

        True, если у модели в файле есть хотя бы один монохромный фон.
        (Если фон всего один — всё равно нужно проверить: рядом может
        лежать такой же от другого продавца.)
        """
        cmap = self._by_collection_model.get(
            str(collection).strip().lower(), {}
        )
        backs = cmap.get(str(model).strip().lower(), set())
        return len(backs) > 0

    def cached_cheap(
        self,
        collection: str,
        model: str,
    ) -> bool | None:
        """
        Возвращает из кэша True/False если запись свежая,
        иначе None.
        """
        key = (
            str(collection).strip().lower(),
            str(model).strip().lower(),
        )
        entry = self._result_cache.get(key)
        if not entry:
            return None
        ts, cheap = entry
        if time.monotonic() - ts > CACHE_TTL:
            self._result_cache.pop(key, None)
            return None
        return cheap

    async def get_monochrome_floor(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        collection: str,
        model: str,
    ) -> int:
        """
        Возвращает самую низкую цену (floor) среди листингов
        с монохромными фонами этой модели.
        Возвращает 0, если нет монохромных фонов или не удалось получить.
        """
        siblings = self.siblings_of(collection, model, "")
        if not siblings:
            return 0

        key = (
            str(collection).strip().lower(),
            str(model).strip().lower(),
        )

        cached = self._mono_floor_cache.get(key)
        if cached:
            ts, value = cached
            if time.monotonic() - ts <= CACHE_TTL:
                return value

        payload = {
            "count": FETCH_COUNT,
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

        model_lower = str(model).strip().lower()

        try:
            cursor = ""
            pages = 0
            while pages < MAX_PAGES:
                if cursor:
                    payload["cursor"] = cursor

                res = await client.post(
                    SALING_URL, headers=headers, json=payload, timeout=8.0
                )
                if res.status_code != 200:
                    logger.warning(
                        f"⚠️ get_monochrome_floor HTTP {res.status_code} "
                        f"[{collection}/{model}]"
                    )
                    return 0

                data = res.json()
                items = data.get("gifts") or data.get("items") or []

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    g_model = str(
                        item.get("modelName") or item.get("model") or ""
                    ).strip().lower()
                    if g_model != model_lower:
                        continue

                    g_back = str(
                        item.get("backdropName") or item.get("backdrop") or ""
                    ).strip().lower()
                    if g_back not in siblings:
                        continue

                    price = (
                        item.get("salePrice")
                        or item.get("price")
                        or item.get("sale", {}).get("price")
                        or 0
                    )
                    try:
                        price = int(price)
                    except (TypeError, ValueError):
                        continue

                    if price > 0:
                        self._mono_floor_cache[key] = (
                            time.monotonic(), price
                        )
                        return price

                next_cursor = (
                    data.get("cursor")
                    or data.get("nextCursor")
                    or data.get("next_cursor")
                    or ""
                )
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                pages += 1

        except httpx.TimeoutException:
            logger.warning(
                f"⚠️ get_monochrome_floor timeout: {collection}/{model}"
            )
        except Exception as e:
            logger.warning(
                f"⚠️ get_monochrome_floor error {collection}/{model}: {e}"
            )

        return 0

    async def has_cheap_sibling(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        collection: str,
        model: str,
        current_backdrop: str,
        floor_nano: int,
        current_price_nano: int,
        exclude_gift_id: str | None = None,
    ) -> bool:
        """
        Основная проверка. True → покупку блокируем.
        """
        siblings = self.siblings_of(collection, model, current_backdrop)
        if not siblings:
            return False

        key = (
            str(collection).strip().lower(),
            str(model).strip().lower(),
        )

        cached = self.cached_cheap(collection, model)
        if cached is not None:
            return cached

        lock = await self._get_lock(key)
        async with lock:
            cached = self.cached_cheap(collection, model)
            if cached is not None:
                return cached

            cheap = await self._fetch_and_check(
                client=client,
                headers=headers,
                collection=collection,
                model=model,
                siblings=siblings,
                floor_nano=floor_nano,
                current_price_nano=current_price_nano,
                exclude_gift_id=exclude_gift_id,
            )
            self._result_cache[key] = (time.monotonic(), cheap)
            return cheap

    # ------------------------------------------------------------------
    # ВНУТРЕННЕЕ
    # ------------------------------------------------------------------

    async def _get_lock(self, key) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._fetch_locks[key] = lock
            return lock

    async def _fetch_and_check(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        collection: str,
        model: str,
        siblings: set[str],
        floor_nano: int,
        current_price_nano: int,
        exclude_gift_id: str | None,
    ) -> bool:
        """
        Ищет любой другой листинг (кроме exclude_gift_id) в той же
        коллекции и модели, c монохромным фоном из siblings,
        дешевле floor*1.25 И существенно дешевле нашего лота
        (на MIN_DIFF_PERCENT+).
        """
        if floor_nano <= 0:
            return False

        threshold_nano = int(floor_nano * (1.0 + PRICE_MARGIN))
        model_lower = str(model).strip().lower()

        if current_price_nano > 0:
            min_allowed = int(
                current_price_nano * (1.0 - MIN_DIFF_PERCENT / 100.0)
            )
        else:
            min_allowed = 0

        payload = {
            "count": FETCH_COUNT,
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

        exclude_str = str(exclude_gift_id).strip() if exclude_gift_id else ""

        cursor = ""
        pages = 0

        try:
            while pages < MAX_PAGES:
                if cursor:
                    payload["cursor"] = cursor

                res = await client.post(
                    SALING_URL,
                    headers=headers,
                    json=payload,
                    timeout=8.0,
                )

                if res.status_code != 200:
                    logger.warning(
                        f"⚠️ MonochromeGuard HTTP {res.status_code} "
                        f"[{collection}/{model}]: {res.text[:200]}"
                    )
                    return False

                data = res.json()
                items = data.get("gifts") or data.get("items") or []

                if items and isinstance(items[0], dict):
                    logger.info(
                        f"🔍 [GUARD DEBUG] первый item keys="
                        f"{list(items[0].keys())}"
                    )

                for gift in items:
                    if not isinstance(gift, dict):
                        continue

                    if exclude_str:
                        item_gift = gift.get("gift") or {}
                        item_ids = {
                            str(gift.get("id") or "").strip(),
                            str(gift.get("giftId") or "").strip(),
                            str(gift.get("saleId") or "").strip(),
                            str(item_gift.get("id") or "").strip(),
                            str(item_gift.get("giftId") or "").strip(),
                        } - {""}

                        if exclude_str in item_ids:
                            continue

                    price_nano = (
                        gift.get("salePrice")
                        or gift.get("price")
                        or gift.get("sale", {}).get("price")
                        or 0
                    )
                    try:
                        price_nano = int(price_nano)
                    except (TypeError, ValueError):
                        continue

                    if price_nano <= 0:
                        continue

                    if price_nano > threshold_nano:
                        continue

                    if min_allowed > 0 and price_nano >= min_allowed:
                        continue

                    g_model = str(
                        gift.get("modelName")
                        or gift.get("model")
                        or ""
                    ).strip().lower()

                    if g_model != model_lower:
                        continue

                    g_back = str(
                        gift.get("backdropName")
                        or gift.get("backdrop")
                        or ""
                    ).strip().lower()

                    if g_back not in siblings:
                        continue

                    logger.info(
                        f"🚫 MonochromeGuard: {collection} / {model} — "
                        f"сосед {g_back} за "
                        f"{price_nano / 1_000_000_000:.2f} TON "
                        f"< порога "
                        f"{threshold_nano / 1_000_000_000:.2f} "
                        f"и < {min_allowed / 1_000_000_000:.2f} "
                        f"(наш лот "
                        f"{current_price_nano / 1_000_000_000:.2f} TON)"
                    )
                    return True

                next_cursor = (
                    data.get("cursor")
                    or data.get("nextCursor")
                    or data.get("next_cursor")
                    or ""
                )
                if not next_cursor or next_cursor == cursor:
                    break

                cursor = next_cursor
                pages += 1

        except httpx.TimeoutException:
            logger.warning(
                f"⚠️ MonochromeGuard timeout: {collection}/{model}"
            )
        except Exception as e:
            logger.warning(
                f"⚠️ MonochromeGuard error {collection}/{model}: {e}"
            )

        return False


# ----------------------------------------------------------------------
# Синглтон
# ----------------------------------------------------------------------

_guard_singleton: MonochromeGuard | None = None


def get_guard() -> MonochromeGuard:
    global _guard_singleton
    if _guard_singleton is None:
        _guard_singleton = MonochromeGuard()
    return _guard_singleton