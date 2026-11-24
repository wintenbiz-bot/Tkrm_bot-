import httpx
import json
import os
from datetime import datetime
from collections import defaultdict

HISTORY_URL = "https://api.tgmrkt.io/api/v1/history"
CACHE_FILE = "purchase_cache.json"


class ProfitTracker:
    def __init__(self, headers: dict):
        self.headers = headers
        self.purchases_cache = self._load_cache()

    def _load_cache(self) -> dict:
        """Загружает сохраненные покупки из локального файла."""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Ошибка чтения кэша покупок: {e}")
        return {}

    def _save_cache(self):
        """Сохраняет текущий реестр покупок в файл."""
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.purchases_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Ошибка сохранения кэша покупок: {e}")

    async def fetch_all_history(self, client: httpx.AsyncClient, max_pages: int = 500) -> list:
        """Скачивает глубокую историю транзакций (до 2000 элементов)."""
        all_items = []
        from_date = None

        for _ in range(max_pages):
            params = {"limit": 20, "type": "All"}
            if from_date:
                params["from"] = from_date

            try:
                res = await client.get(HISTORY_URL, headers=self.headers, params=params)
                if res.status_code != 200:
                    break

                data = res.json()
                if not isinstance(data, list) or not data:
                    break

                all_items.extend(data)

                last_item = data[-1]
                from_date = last_item.get("date")

                if not from_date or len(data) < 20:
                    break

            except Exception as e:
                print(f"⚠️ Ошибка запроса истории MRKT: {e}")
                break

        return all_items

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