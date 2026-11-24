import logging
from typing import List, Dict, Any
import httpx

logger = logging.getLogger(__name__)


async def sync_trades_to_sheet(
    script_url: str,
    trades: List[Dict[str, Any]],
    secret: str = "",
) -> dict:
    """
    Отправляет список сделок в Google Таблицу через Apps Script Web App.

    Возвращает dict:
        {"ok": bool, "added": int, "skipped": int, "error": str|None}
    """
    result = {"ok": False, "added": 0, "skipped": 0, "error": None}

    if not script_url:
        result["error"] = "no_script_url"
        logger.warning("Google Sheet Apps Script URL отсутствует.")
        return result

    if not trades:
        result["ok"] = True
        logger.info("Нет сделок для отправки в Google Sheet.")
        return result

    payload = {"trades": trades}
    if secret:
        payload["secret"] = secret

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,  # ОБЯЗАТЕЛЬНО для Apps Script
        ) as client:
            response = await client.post(script_url, json=payload)
            response.raise_for_status()

            data = response.json()

            if data.get("success"):
                result["ok"] = True
                result["added"] = int(data.get("added", 0))
                result["skipped"] = int(data.get("skipped", 0))
                logger.info(
                    f"✅ Google Sheet: добавлено {result['added']}, "
                    f"пропущено {result['skipped']}"
                )
            else:
                result["error"] = data.get("error", "unknown")
                logger.error(f"❌ Apps Script error: {result['error']}")

    except httpx.HTTPStatusError as e:
        result["error"] = f"http_{e.response.status_code}"
        logger.error(f"⚠️ HTTP ошибка Google Sheet: {e}")

    except httpx.RequestError as e:
        result["error"] = f"network_{type(e).__name__}"
        logger.error(f"⚠️ Сетевая ошибка Google Sheet: {e}")

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"⚠️ Неизвестная ошибка Google Sheet: {e}")

    return result