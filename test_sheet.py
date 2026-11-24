import asyncio
import httpx


URL = "https://script.google.com/macros/s/AKfycby3Afi0mx7z9JvJj0sxwg2xlrbjQelc5rRDY5uUKC_mBhwXxRFeLCy2d0aqwuj3P4C7vQ/exec"


async def main():
    payload = {
        "trades": [
            {
                "title": "Test Gift",
                "date": "2026-09-29",
                "buy_ton": 2.5,
                "sell_ton": 3.0,
                "commission": 0,
                "status": "Продано",
            }
        ]
    }

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        print("📤 Отправляю POST...")
        response = await client.post(URL, json=payload)
        print(f"📥 Статус: {response.status_code}")
        print(f"📥 Ответ: {response.text}")


if __name__ == "__main__":
    asyncio.run(main())