import asyncio
import json
import os
import logging
from decimal import Decimal, ROUND_DOWN
import httpx

# ============================================================
# ENDPOINTS & CONSTANTS
# ============================================================
print("🔥🔥🔥 ORDERS_MANAGER ЗАГРУЖЕН ИЗ:", __file__)
CREATE_ORDER_URL = "https://api.tgmrkt.io/api/v1/orders/create"
CANCEL_ORDER_URL = "https://api.tgmrkt.io/api/v1/orders/cancel"
MY_ORDERS_URL = "https://api.tgmrkt.io/api/v1/orders/get-my-orders"
MODELS_URL = "https://api.tgmrkt.io/api/v1/gifts/models"

NANO_TON = 1_000_000_000
PRICE_STEP_NANO = 10_000_000  # 0.01 TON
MIN_MARKET_PRICE_NANO = 500_000_000  # 0.5 TON

FLOOR_TOLERANCE_PERCENT = 1.5
FLOOR_TOLERANCE_NANO = 100_000_000  # 0.1 TON

logger = logging.getLogger(__name__)


def apply_discount(price_nano: int, discount_percent: float) -> int:
    """Уменьшает цену на заданный процент и округляет вниз до 0.01 TON."""
    if price_nano <= 0:
        return 0
    multiplier = Decimal(str(1.0 - (discount_percent / 100.0)))
    discounted_nano = int((Decimal(price_nano) * multiplier).to_integral_value(rounding=ROUND_DOWN))
    return (discounted_nano // PRICE_STEP_NANO) * PRICE_STEP_NANO


class OrdersManager:
    LIVE_ORDER_STATUSES = {
        "ACTIVE",
        "PUBLISHED",
        "PARTIALLY_FILLED",
    }

    def __init__(self, headers: dict, *args, **kwargs):
        self.headers = headers

    # ========================================================
    # FLOOR
    # ========================================================

    async def get_collection_floor(self, client: httpx.AsyncClient, collection: str) -> int:
        payload = {"collections": [collection]}

        try:
            res = await client.post(MODELS_URL, headers=self.headers, json=payload)

            if res.status_code != 200:
                logger.error(f"❌ Ошибка API floor коллекции [{res.status_code}]: {res.text[:500]}")
                return 0

            data = res.json()
            items = data if isinstance(data, list) else data.get("models", [])

            if not isinstance(items, list) or not items:
                logger.warning(f"⚠️ Не найдены модели для коллекции: {collection}")
                return 0

            floors = []
            for item in items:
                if not isinstance(item, dict):
                    continue

                floor = (
                    item.get("floorPriceNanoTons")
                    or item.get("floorPriceNanoTONs")
                    or item.get("floorPrice")
                    or 0
                )

                try:
                    floor = int(floor)
                except (TypeError, ValueError):
                    continue

                if floor > 0:
                    floors.append(floor)

            if not floors:
                logger.warning(f"⚠️ Не найден floor коллекции: {collection}")
                return 0

            return min(floors)

        except Exception as e:
            logger.error(f"⚠️ Ошибка запроса флора коллекции {collection}: {e}")
            return 0

    # ========================================================
    # CREATE ORDER
    # ========================================================

    async def _send_create_order_request(
        self,
        client: httpx.AsyncClient,
        collection: str,
        max_price_nano: int,
        discount_percent: float,
        quantity: int = 5
    ) -> bool:
        if max_price_nano < MIN_MARKET_PRICE_NANO:
            max_price_nano = MIN_MARKET_PRICE_NANO

        payload = {
            "backdropName": None,
            "collectionName": collection,
            "isTransferable": False,
            "modelName": None,
            "priceMaxNanoTONs": int(max_price_nano),
            "priceMinNanoTONs": MIN_MARKET_PRICE_NANO,
            "quantity": quantity,
            "symbolName": None
        }

        try:
            res = await client.post(CREATE_ORDER_URL, headers=self.headers, json=payload)
            if res.status_code in (200, 201):
                logger.info(
                    f"🚀 ОРДЕР СОЗДАН (-{discount_percent:.0f}%): {collection} | Модель: Любое | "
                    f"Фон: Любое | Qty: {quantity} | Max Price: {max_price_nano / NANO_TON:.2f} TON"
                )
                return True
            else:
                logger.error(f"❌ Ошибка создания ордера (-{discount_percent:.0f}%) [{res.status_code}]: {res.text}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка сети при создании ордера (-{discount_percent:.0f}%): {e}")

        return False

    async def create_orders_pair(
        self,
        client: httpx.AsyncClient,
        collection: str,
        quantity: int = 5,
        discount_1: float = 30.0,
        discount_2: float = 50.0,
        *args,
        **kwargs
    ) -> tuple[bool, bool]:
        collection_floor = await self.get_collection_floor(client, collection)

        if collection_floor <= 0:
            logger.warning(f"⏭️ ОРДЕРА ПРОПУЩЕНЫ: {collection} — флор не определен.")
            return False, False

        price_1 = apply_discount(collection_floor, discount_1)
        price_2 = apply_discount(collection_floor, discount_2)

        logger.info(
            f"📦 [COLLECTION FLOOR] {collection} | "
            f"floor={collection_floor / NANO_TON:.2f} TON | "
            f"order_{discount_1:.0f}%={price_1 / NANO_TON:.2f} TON | "
            f"order_{discount_2:.0f}%={price_2 / NANO_TON:.2f} TON"
        )

        ok_1 = await self._send_create_order_request(
            client=client,
            collection=collection,
            max_price_nano=price_1,
            discount_percent=discount_1,
            quantity=quantity
        )

        await asyncio.sleep(2.0)          ## было await asyncio.sleep(0.3)

        ok_2 = await self._send_create_order_request(
            client=client,
            collection=collection,
            max_price_nano=price_2,
            discount_percent=discount_2,
            quantity=quantity
        )

        return ok_1, ok_2

    # ========================================================
    # PROCESS ORDERS FILE
    # ========================================================

    async def process_orders_file(
        self,
        client: httpx.AsyncClient,
        file_path: str = "orders_to_create.json",
        dry_run: bool = False,
        test_limit: int | None = None,
        discount_1: float = 30.0,
        discount_2: float = 50.0,
    ) -> dict:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(base_dir, file_path) if not os.path.isabs(file_path) else file_path

        result = {
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "dry_run": dry_run,
        }

        if not os.path.exists(full_path):
            logger.error(f"❌ Файл ордеров {full_path} не найден!")
            return result

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                orders = json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка чтения {full_path}: {e}")
            return result

        if not isinstance(orders, list):
            logger.error("❌ Файл ордеров должен содержать список.")
            return result

        if test_limit is not None:
            orders = orders[:test_limit]

        result["total"] = len(orders)
        mode = "🧪 TEST MODE" if dry_run else "🚀 LIVE MODE"
        logger.info(
            f"{mode}: обработка {len(orders)} коллекций "
            f"(каждая создаст 2 ордера: -{discount_1:.0f}% и -{discount_2:.0f}%)"
        )

        for index, item in enumerate(orders, 1):
            coll = item.get("collection") if isinstance(item, dict) else item

            if not coll:
                result["skipped"] += 1
                logger.warning(f"⏭️ Пропуск позиции #{index}: отсутствует название коллекции.")
                continue

            try:
                if dry_run:
                    collection_floor = await self.get_collection_floor(client, coll)
                    result["processed"] += 1

                    if collection_floor <= 0:
                        result["skipped"] += 1
                        logger.warning(f"⏭️ TEST SKIP: {coll} — флор не определен")
                    else:
                        p1 = apply_discount(collection_floor, discount_1)
                        p2 = apply_discount(collection_floor, discount_2)
                        result["success"] += 2
                        logger.info(
                            f"🧪 TEST OK: {coll} | ALL MODELS/BACKDROPS | "
                            f"Order 1 (-{discount_1:.0f}%): {p1 / NANO_TON:.2f} TON | "
                            f"Order 2 (-{discount_2:.0f}%): {p2 / NANO_TON:.2f} TON"
                        )
                else:
                    ok_1, ok_2 = await self.create_orders_pair(
                        client=client,
                        collection=coll,
                        quantity=5,
                        discount_1=discount_1,
                        discount_2=discount_2,
                    )
                    result["processed"] += 1

                    if ok_1:
                        result["success"] += 1
                    else:
                        result["failed"] += 1

                    if ok_2:
                        result["success"] += 1
                    else:
                        result["failed"] += 1

                await asyncio.sleep(2.0)

            except Exception as e:
                result["failed"] += 1
                logger.exception(f"❌ Ошибка позиции #{index}: {coll}: {e}")

        logger.info(
            f"{'🧪 TEST' if dry_run else '🚀 LIVE'} завершён: "
            f"processed_collections={result['processed']}, success_orders={result['success']}, "
            f"failed_orders={result['failed']}, skipped={result['skipped']}"
        )
        return result

    # ========================================================
    # GET MY ORDERS
    # ========================================================

    async def get_my_orders(self, client: httpx.AsyncClient, cursor: str = "", count: int = 100) -> tuple[list, str]:
        payload = {
            "availableForStaking": None,
            "backdropNames": [],
            "collectionNames": [],
            "count": count,
            "cursor": cursor,
            "lowToHigh": False,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "ordering": "PublicationTime",
            "query": None,
            "symbolNames": []
        }
        try:
            res = await client.post(MY_ORDERS_URL, headers=self.headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                orders = data.get("orders") or data.get("items") or []

                next_cursor = (
                    data.get("cursor")
                    or data.get("nextCursor")
                    or data.get("next_cursor")
                    or ""
                )

                if not next_cursor and orders:
                    last_order = orders[-1]
                    next_cursor = str(last_order.get("id") or last_order.get("orderId") or "")

                return orders, next_cursor

        except Exception as e:
            logger.error(f"⚠️ Ошибка запроса моих ордеров: {e}")

        return [], ""

    # ========================================================
    # GET ORDERS BY COLLECTION
    # ========================================================

    async def get_orders_by_collection(
        self,
        client: httpx.AsyncClient,
        collection: str,
        count: int = 100,
    ) -> list:
        payload = {
            "availableForStaking": None,
            "backdropNames": [],
            "collectionNames": [collection],
            "count": count,
            "cursor": "",
            "lowToHigh": False,
            "maxPrice": None,
            "minPrice": None,
            "modelNames": [],
            "ordering": "PublicationTime",
            "query": None,
            "symbolNames": [],
        }

        try:
            res = await client.post(MY_ORDERS_URL, headers=self.headers, json=payload)

            if res.status_code != 200:
                logger.warning(
                    f"⚠️ get-my-orders HTTP {res.status_code} "
                    f"[{collection}]: {res.text[:200]}"
                )
                return []

            data = res.json()
            orders = data.get("orders") or data.get("items") or []

            coll_lower = collection.strip().lower()
            live = []

            for o in orders:
                coll_name = str(o.get("collectionName", "")).strip().lower()
                if coll_name != coll_lower:
                    continue

                status = str(o.get("status", "")).strip().upper()

                logger.info(
                    f"🔍 [ORDER] {collection} | "
                    f"id={o.get('id') or o.get('orderId')} | "
                    f"status={status} | "
                    f"priceMax={o.get('priceMaxNanoTONs') or o.get('priceMax') or o.get('price')}"
                )

                if status in self.LIVE_ORDER_STATUSES:
                    live.append(o)

            return live

        except Exception as e:
            logger.error(f"⚠️ Ошибка получения ордеров по {collection}: {e}")

        return []

    # ========================================================
    # CANCEL
    # ========================================================

    async def cancel_order(self, client: httpx.AsyncClient, order_id: str) -> bool:
        url = f"{CANCEL_ORDER_URL}/{order_id}"
        try:
            res = await client.post(url, headers=self.headers)
            if res.status_code in (200, 201, 204):
                logger.info(f"🗑 ОРДЕР ОТМЕНЁН: {order_id}")
                return True
            logger.error(f"❌ Ошибка отмены ордера {order_id} [{res.status_code}]: {res.text}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка при отмене ордера {order_id}: {e}")
        return False

    async def cancel_orders_by_collection(
        self,
        client: httpx.AsyncClient,
        collection: str,
    ) -> int:
        orders = await self.get_orders_by_collection(client, collection)
        cancelled = 0

        for order in orders:
            status = str(order.get("status", "")).strip().upper()
            order_id = order.get("id") or order.get("orderId")

            logger.info(
                f"🔍 [CANCEL CHECK] id={order_id} | "
                f"status='{status}' | "
                f"coll={order.get('collectionName')} | "
                f"keys={list(order.keys())}"
            )

            if status not in self.LIVE_ORDER_STATUSES:
                continue

            if not order_id:
                continue

            ok = await self.cancel_order(client, order_id)
            if ok:
                success += 1

            await asyncio.sleep(2.0)

        return cancelled

    async def cancel_all_orders(self, client: httpx.AsyncClient) -> int:
        success = 0
        current_cursor = ""
        seen_cursors = set()
        page = 1

        while True:
            logger.info(f"📄 Запрос страницы #{page} ордеров (cursor: '{current_cursor}')")
            orders, next_cursor = await self.get_my_orders(client, cursor=current_cursor, count=100)

            if not orders:
                logger.info("ℹ️ Ордера на странице не найдены. Завершение отмены.")
                break

            for order in orders:
                order_id = order.get("id") or order.get("orderId")

                logger.info(f"🔍 [CANCEL] id={order_id} | coll={order.get('collectionName')}")

                if not order_id:
                    logger.info("⏭️ Пропуск: нет order_id")
                    continue

                ok = await self.cancel_order(client, order_id)
                if ok:
                    success += 1

                await asyncio.sleep(2.0)

            if not next_cursor or next_cursor == current_cursor or next_cursor in seen_cursors:
                logger.info("🏁 Достигнут конец списка ордеров или курсор повторился.")
                break

            seen_cursors.add(current_cursor)
            current_cursor = next_cursor
            page += 1

        logger.info(f"✅ Итого отменено ордеров: {success}")
        return success

    # ========================================================
    # AUTO REFRESH ORDERS
    # ========================================================

    @staticmethod
    def _floor_changed(
        prev_floor: int | None,
        current_floor: int,
        tolerance_percent: float = FLOOR_TOLERANCE_PERCENT,
        tolerance_nano: int = FLOOR_TOLERANCE_NANO,
    ) -> bool:
        if prev_floor is None:
            return True

        if prev_floor == 0:
            return current_floor != 0

        diff = abs(current_floor - prev_floor)

        percent_limit = int(prev_floor * (tolerance_percent / 100.0))

        return diff > percent_limit and diff > tolerance_nano

    async def refresh_orders_if_floor_changed(
        self,
        client: httpx.AsyncClient,
        file_path: str = "orders_to_create.json",
        floor_state_file: str = "floor_state.json",
        quantity: int = 5,
        discount_1: float = 30.0,
        discount_2: float = 50.0,
    ) -> dict:
        result = {
            "checked": 0,
            "changed": 0,
            "orders_cancelled": 0,
            "orders_created": 0,
            "errors": 0,
        }

        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(base_dir, file_path) if not os.path.isabs(file_path) else file_path
        state_path = os.path.join(base_dir, floor_state_file) if not os.path.isabs(floor_state_file) else floor_state_file

        if not os.path.exists(full_path):
            logger.error(f"❌ Файл ордеров {full_path} не найден!")
            return result

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                orders = json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка чтения {full_path}: {e}")
            return result

        if not isinstance(orders, list):
            logger.error("❌ Файл ордеров должен содержать список.")
            return result

        floor_state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    floor_state = json.load(f)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения {state_path}: {e}")

        for item in orders:
            coll = item.get("collection") if isinstance(item, dict) else item
            if not coll:
                continue

            result["checked"] += 1

            current_floor = await self.get_collection_floor(client, coll)
            if current_floor <= 0:
                continue

            prev_floor = floor_state.get(coll)

            if not self._floor_changed(prev_floor, current_floor):
                continue

            result["changed"] += 1
            prev_floor_ton = (prev_floor / NANO_TON) if prev_floor else 0.0
            current_floor_ton = current_floor / NANO_TON
            diff_ton = current_floor_ton - prev_floor_ton

            logger.info(
                f"🔄 Флор {coll} изменился: "
                f"{prev_floor_ton:.4f} TON → "
                f"{current_floor_ton:.4f} TON "
                f"(Δ {diff_ton:+.4f} TON)"
            )

            try:
                cancelled = await self.cancel_orders_by_collection(client, coll)
                result["orders_cancelled"] += cancelled
            except Exception as e:
                logger.error(f"⚠️ Ошибка отмены ордеров {coll}: {e}")
                result["errors"] += 1
                continue

            await asyncio.sleep(2.0)

            try:
                ok_1, ok_2 = await self.create_orders_pair(
                    client=client,
                    collection=coll,
                    quantity=quantity,
                    discount_1=discount_1,
                    discount_2=discount_2,
                )
                if ok_1:
                    result["orders_created"] += 1
                if ok_2:
                    result["orders_created"] += 1
            except Exception as e:
                logger.error(f"⚠️ Ошибка создания ордеров {coll}: {e}")
                result["errors"] += 1
                continue

            floor_state[coll] = current_floor

            await asyncio.sleep(2.0)

        try:
            tmp = f"{state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(floor_state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, state_path)
        except Exception as e:
            logger.error(f"⚠️ Ошибка сохранения {state_path}: {e}")

        return result

    # ========================================================
    # SYNC ORDERS
    # ========================================================

    async def sync_orders_file(
        self,
        client: httpx.AsyncClient,
        file_path: str = "orders_to_create.json",
        quantity: int = 5,
        test_limit: int | None = None,
        dry_run: bool = False,
        price_tolerance_percent: float = 3.0,
        discount_1: float = 30.0,
        discount_2: float = 50.0,
    ) -> dict:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(base_dir, file_path) if not os.path.isabs(file_path) else file_path

        result = {
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "existing_kept": 0,
            "dry_run": dry_run,
        }

        if not os.path.exists(full_path):
            logger.error(f"❌ Файл ордеров {full_path} не найден!")
            return result

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                orders = json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка чтения {full_path}: {e}")
            return result

        if not isinstance(orders, list):
            logger.error("❌ Файл ордеров должен содержать список.")
            return result

        if test_limit is not None:
            orders = orders[:test_limit]

        result["total"] = len(orders)
        mode = "🧪 TEST MODE" if dry_run else "🚀 LIVE MODE"
        logger.info(
            f"{mode} SYNC: обработка {len(orders)} коллекций "
            f"(создаются только недостающие ордера -{discount_1:.0f}%/-{discount_2:.0f}%)"
        )

        for index, item in enumerate(orders, 1):
            coll = item.get("collection") if isinstance(item, dict) else item

            if not coll:
                result["skipped"] += 1
                logger.warning(f"⏭️ Пропуск позиции #{index}: отсутствует название коллекции.")
                continue

            try:
                floor = await self.get_collection_floor(client, coll)
                if floor <= 0:
                    result["skipped"] += 1
                    logger.warning(f"⏭️ SYNC SKIP: {coll} — флор не определён.")
                    await asyncio.sleep(2.0)
                    continue

                price_1 = apply_discount(floor, discount_1)
                price_2 = apply_discount(floor, discount_2)

                active_existing = await self.get_orders_by_collection(client, coll)

                existing_prices = set()
                for o in active_existing:
                    p = (
                        o.get("priceMaxNanoTONs")
                        or o.get("priceMax")
                        or o.get("maxPrice")
                        or o.get("price")
                        or 0
                    )
                    try:
                        p = int(p)
                    except (TypeError, ValueError):
                        continue
                    if p > 0:
                        existing_prices.add(p)

                def _has_price(target_nano: int) -> bool:
                    if target_nano <= 0:
                        return False
                    tolerance_nano = int(
                        target_nano * (price_tolerance_percent / 100.0)
                    )
                    for p in existing_prices:
                        if abs(p - target_nano) <= tolerance_nano:
                            return True
                    return False

                need_1 = not _has_price(price_1)
                need_2 = not _has_price(price_2)

                logger.info(
                    f"📦 [SYNC] {coll} | floor={floor / NANO_TON:.2f} TON | "
                    f"need -{discount_1:.0f}% ({price_1 / NANO_TON:.2f})={need_1} | "
                    f"need -{discount_2:.0f}% ({price_2 / NANO_TON:.2f})={need_2} | "
                    f"existing_active={len(active_existing)}"
                )

                if active_existing:
                    logger.info(
                        f"🔍 DEBUG существующие цены {coll}: "
                        f"{[round(p / NANO_TON, 2) for p in existing_prices]}"
                    )

                result["processed"] += 1

                if not need_1 and not need_2:
                    result["existing_kept"] += 2
                    logger.info(f"✅ {coll}: оба ордера уже стоят, пропускаю.")
                    await asyncio.sleep(2.0)
                    continue

                if dry_run:
                    if need_1:
                        result["success"] += 1
                    else:
                        result["existing_kept"] += 1

                    if need_2:
                        result["success"] += 1
                    else:
                        result["existing_kept"] += 1

                    await asyncio.sleep(2.0)
                    continue

                if need_1:
                    ok_1 = await self._send_create_order_request(
                        client=client,
                        collection=coll,
                        max_price_nano=price_1,
                        discount_percent=discount_1,
                        quantity=quantity,
                    )
                    if ok_1:
                        result["success"] += 1
                    else:
                        result["failed"] += 1
                    await asyncio.sleep(2.0)

                if need_2:
                    ok_2 = await self._send_create_order_request(
                        client=client,
                        collection=coll,
                        max_price_nano=price_2,
                        discount_percent=discount_2,
                        quantity=quantity,
                    )
                    if ok_2:
                        result["success"] += 1
                    else:
                        result["failed"] += 1
                    await asyncio.sleep(2.0)

                await asyncio.sleep(2.0)

            except Exception as e:
                result["failed"] += 1
                logger.exception(f"❌ Ошибка SYNC позиции #{index}: {coll}: {e}")

        logger.info(
            f"{'🧪 SYNC TEST' if dry_run else '🚀 SYNC LIVE'} завершён: "
            f"processed={result['processed']}, "
            f"existing_kept={result['existing_kept']}, "
            f"success_created={result['success']}, "
            f"failed={result['failed']}, "
            f"skipped={result['skipped']}"
        )
        return result