import asyncio
import logging
import os
import subprocess
import sys
import aiohttp
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


class WBClient:
    def __init__(self, token_prices: str | None = None, token_stats: str | None = None, token_adv: str | None = None):
        self.token_prices = token_prices
        self.token_stats = token_stats
        self.token_adv = token_adv

    async def check_token_rights(self) -> Tuple[bool, str]:
        if not self.token_prices:
            return False, "Токен не задан."

        url = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit=1"
        headers = {"Authorization": self.token_prices}
        async with aiohttp.ClientSession() as session:
            for attempt in range(2):
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status in (401, 403):
                            return False, "AUTH_ERROR: Неверный API-ключ или не включена категория «Цены и скидки» в настройках токена."
                        if resp.status in (429, 500, 502, 503, 504):
                            if attempt == 0:
                                await asyncio.sleep(2)
                                continue
                            return False, f"TEMP_ERROR: Временные техработы API Wildberries (код {resp.status})."
                        if resp.status != 200:
                            return False, f"TEMP_ERROR: Ошибка со стороны Wildberries (код {resp.status})."
                        return True, "Успешно"
                except Exception as e:
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return False, f"TEMP_ERROR: Временный таймаут соединения с API Wildberries ({type(e).__name__})."

        return True, "Успешно"

    async def check_stats_token(self) -> Tuple[bool, str]:
        if not self.token_stats:
            return False, "Токен статистики не задан."
        date_from = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        url = f"https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom={date_from}"
        headers = {"Authorization": self.token_stats}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status in (401, 403):
                        return False, "Неверный токен или не включено право «Статистика»."
                    if resp.status != 200:
                        return False, f"Ошибка Wildberries (код {resp.status})."
            except Exception:
                return False, "Не удалось подключиться к API статистики WB."
        return True, "Успешно"

    async def get_promotions(self) -> List[Dict[str, Any]]:
        if not self.token_prices:
            return []

        now = datetime.utcnow()
        start_dt = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
        end_dt = (now + timedelta(days=30)).strftime("%Y-%m-%dT23:59:59Z")

        url = (
            f"https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions"
            f"?startDateTime={start_dt}&endDateTime={end_dt}&allPromo=false&limit=50&offset=0"
        )
        headers = {"Authorization": self.token_prices}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", {}).get("promotions", [])
            except Exception as e:
                logger.warning("Ошибка получения календаря акций WB: %s", e)
        return []

    async def get_promotion_details(self, promo_id: int) -> Dict[str, Any]:
        if not self.token_prices:
            return {}
        url = f"https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/details?promotionIDs={promo_id}"
        headers = {"Authorization": self.token_prices}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        promos = data.get("data", {}).get("promotions", [])
                        return promos[0] if promos else {}
            except Exception:
                pass
        return {}

    async def get_promotion_nomenclatures(self, promo_id: int, in_action: bool = False) -> List[Dict[str, Any]]:
        if not self.token_prices:
            return []
        headers = {"Authorization": self.token_prices}
        all_noms = []
        offset = 0
        limit = 1000

        async with aiohttp.ClientSession() as session:
            for _ in range(20):
                url = (
                    f"https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/nomenclatures"
                    f"?promotionID={promo_id}&inAction={str(in_action).lower()}&limit={limit}&offset={offset}"
                )
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                        batch = data.get("data", {}).get("nomenclatures", [])
                        if not batch:
                            break
                        all_noms.extend(batch)
                        if len(batch) < limit:
                            break
                        offset += limit
                except Exception as e:
                    logger.warning("Ошибка получения товаров акции %s: %s", promo_id, e)
                    break
        return all_noms

    async def upload_to_promotion(self, promo_id: int, skus: List[int], upload_now: bool = True) -> Tuple[bool, str]:
        if not self.token_prices or not skus:
            return False, "Список артикулов пуст или нет токена."
        url = "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/upload"
        headers = {"Authorization": self.token_prices}

        async with aiohttp.ClientSession() as session:
            for i in range(0, len(skus), 1000):
                batch = [int(s) for s in skus[i:i + 1000]]
                payload = {
                    "data": {
                        "promotionID": int(promo_id),
                        "uploadNow": upload_now,
                        "nomenclatures": batch
                    }
                }
                try:
                    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status not in (200, 201):
                            err_text = await resp.text()
                            return False, f"Код {resp.status}: {err_text[:150]}"
                except Exception as e:
                    return False, f"Ошибка соединения: {e}"
        return True, "Успешно отправлено в WB"

    async def get_all_seller_goods(self) -> List[Dict[str, Any]]:
        if not self.token_prices:
            return []
        headers = {"Authorization": self.token_prices}
        result = []
        offset = 0
        limit = 1000

        async with aiohttp.ClientSession() as session:
            for _ in range(20):
                url = f"https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter?limit={limit}&offset={offset}"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                        if resp.status != 200:
                            break
                        payload = await resp.json()
                        items = payload.get("data", {}).get("listGoods", [])
                        if not items:
                            break
                        for it in items:
                            sku = str(it.get("nmID"))
                            sizes = it.get("sizes", [])
                            if sizes:
                                size_info = sizes[0]
                                seller_price = float(size_info.get("discountedPrice", 0))
                                base_price = float(size_info.get("price", 0))
                                club_price = float(size_info.get("clubDiscountedPrice", seller_price))
                            else:
                                seller_price = 0.0
                                base_price = 0.0
                                club_price = 0.0

                            result.append({
                                "sku": sku,
                                "vendor_code": it.get("vendorCode"),
                                "seller_price": round(seller_price, 2),
                                "base_price": round(base_price, 2),
                                "club_price": round(club_price, 2),
                                "discount": it.get("discount", 0)
                            })
                        if len(items) < limit:
                            break
                        offset += limit
                except Exception as e:
                    logger.warning("Ошибка пагинации товаров WB (offset=%s): %s", offset, e)
                    break
        return result

    async def get_top_sales_skus(self, top_ratio: float = 0.2) -> Dict[str, int]:
        if not self.token_stats:
            return {}
        date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
        url = f"https://statistics-api.wildberries.ru/api/v1/supplier/orders?dateFrom={date_from}"
        headers = {"Authorization": self.token_stats}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status in (401, 403) or resp.status != 200:
                        return {}
                    orders = await resp.json()
                    if not orders:
                        return {}
                    sales_count: Dict[str, int] = {}
                    for o in orders:
                        if o.get("isCancel"):
                            continue
                        sku = str(o.get("nmId"))
                        sales_count[sku] = sales_count.get(sku, 0) + 1
                    if not sales_count:
                        return {}
                    sorted_items = sorted(sales_count.items(), key=lambda x: x[1], reverse=True)
                    limit = max(1, round(len(sorted_items) * top_ratio))
                    return dict(sorted_items[:limit])
            except Exception:
                return {}

    @staticmethod
    async def _scrape_batches(skus: List[str], use_headed: bool) -> List[Dict[str, Any]]:
        from playwright.async_api import async_playwright
        products = []
        browser = None
        context = None

        if not sys.platform.startswith("win"):
            os.environ.pop("DISPLAY", None)
            use_headed = False

        async with async_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--mute-audio",
                "--no-zygote",
            ]
            if not use_headed:
                launch_args.append("--headless=new")

            try:
                browser = await p.chromium.launch(
                    headless=(not use_headed),
                    args=launch_args
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                    locale="ru-RU"
                )
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                """)
                page = await context.new_page()

                batches = [skus[i:i + 70] for i in range(0, len(skus), 70)]
                captured_template_url = None
                first_batch_done = False

                # Важно: НЕ блокируем stylesheet, иначе SPA Wildberries не отправляет запрос cards/v4/detail!
                async def intercept_requests(route, request):
                    nonlocal captured_template_url
                    if request.resource_type in ("image", "media", "font"):
                        await route.abort()
                        return
                    if ("cards/v4/detail" in request.url or "cards/v2/detail" in request.url) and "nm=" in request.url:
                        base_url = request.url.split("&nm=")[0]
                        if not captured_template_url:
                            captured_template_url = base_url
                            batch_str = ";".join(batches[0])
                            await route.continue_(url=f"{base_url}&nm={batch_str}")
                            return
                    await route.continue_()

                await page.route("**/*", intercept_requests)

                async def on_response(response):
                    nonlocal first_batch_done
                    if ("cards/v4/detail" in response.url or "cards/v2/detail" in response.url) and response.status == 200:
                        try:
                            data = await response.json()
                            items = data.get("products") or data.get("data", {}).get("products", [])
                            if items:
                                products.extend(items)
                                first_batch_done = True
                        except Exception:
                            pass

                page.on("response", on_response)

                anchor_sku = "503994462" if "503994462" in skus else ("995598603" if "995598603" in skus else skus[0])
                try:
                    await page.goto(
                        f"https://www.wildberries.ru/catalog/{anchor_sku}/detail.aspx",
                        wait_until="domcontentloaded",
                        timeout=30000
                    )
                    for _ in range(35):
                        if first_batch_done:
                            break
                        await page.wait_for_timeout(500)
                except Exception as e:
                    logger.warning("Ошибка загрузки якорной страницы WB: %s", e)

                base_api = captured_template_url or "https://u-card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30"
                start_idx = 1 if first_batch_done else 0

                for batch in batches[start_idx:]:
                    batch_str = ";".join(batch)
                    target_url = f"{base_api}&nm={batch_str}"
                    try:
                        batch_items = await page.evaluate(
                            """async (url) => {
                                try {
                                    const res = await fetch(url, { credentials: 'include' });
                                    if (!res.ok) return [];
                                    const data = await res.json();
                                    return data.products || (data.data && data.data.products) || [];
                                } catch (e) {
                                    return [];
                                }
                            }""",
                            target_url
                        )
                        if batch_items:
                            products.extend(batch_items)
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                from utils import release_memory
                release_memory()

        return products

    @classmethod
    async def fetch_client_prices(cls, skus: List[str], official_prices: Dict[str, float] | None = None) -> Dict[str, Dict[str, Any]]:
        if not skus:
            return {}

        is_win = sys.platform.startswith("win")
        products = []
        try:
            products = await cls._scrape_batches(skus, use_headed=is_win)
            logger.info("WB Playwright получил %s карточек.", len(products))
        except Exception as e:
            logger.warning("Ошибка запуска браузера Playwright WB: %s", e)

        result = {}
        for p in products:
            sku = str(p.get("id"))
            client_price = 0.0
            wallet_price = 0.0
            sizes = p.get("sizes", [])

            if sizes:
                for s in sizes:
                    if "price" in s:
                        price_obj = s["price"]
                        raw_val = price_obj.get("product") or price_obj.get("total") or 0
                        if raw_val > 0:
                            client_price = float(raw_val) / 100.0

                        raw_wallet = price_obj.get("wallet") or 0
                        if raw_wallet > 0:
                            wallet_price = float(raw_wallet) / 100.0
                        break

            if client_price == 0.0 and p.get("salePriceU"):
                client_price = float(p.get("salePriceU", 0)) / 100.0

            if client_price <= 0:
                continue

            if wallet_price <= 0:
                wallet_price = float(int(client_price * 0.97 * 100)) / 100.0

            seller_price = 0.0
            if official_prices and sku in official_prices and official_prices[sku] > 0:
                seller_price = official_prices[sku]
            elif sizes and "price" in sizes[0]:
                seller_price = float(sizes[0]["price"].get("basic", 0)) / 100.0

            if seller_price <= 0:
                seller_price = client_price

            spp = 0
            if seller_price > 0 and client_price > 0 and seller_price > client_price:
                spp = round(((seller_price - client_price) / seller_price) * 100)

            result[sku] = {
                "seller_price": round(seller_price, 2),
                "client_price": round(client_price, 2),
                "wallet_price": round(wallet_price, 2),
                "spp": spp
            }

        return result