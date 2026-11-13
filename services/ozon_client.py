import asyncio
import json
import logging
import os
import re
import sys
import aiohttp
from typing import Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


class OzonClient:
    def __init__(self, client_id: str, api_key: str):
        self.client_id = str(client_id).strip()
        self.api_key = str(api_key).strip()
        self.base_url = "https://api-seller.ozon.ru"
        self.headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json"
        }

    async def check_token_rights(self) -> Tuple[bool, str]:
        if not self.client_id or not self.api_key:
            return False, "Client ID или API-ключ не заполнены."

        url = f"{self.base_url}/v3/product/list"
        payload = {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 401:
                        return False, "AUTH_ERROR: Неверный Client ID или API-ключ Ozon. Проверьте правильность введенных данных."
                    if resp.status == 403:
                        return False, "AUTH_ERROR: Доступ запрещен. Убедитесь, что у ключа настроены права «Товары» и «Акции»."
                    if resp.status != 200:
                        return False, f"TEMP_ERROR: Ошибка API Ozon (код {resp.status})."
            except Exception as e:
                return False, f"TEMP_ERROR: Не удалось подключиться к Ozon API ({type(e).__name__})."

        return True, "Успешно"

    @staticmethod
    def _extract_real_sku(info_item: Dict[str, Any]) -> str:
        """Извлекает реальный публичный SKU Ozon (например 1628039820) вместо внутреннего product_id."""
        sku_val = info_item.get("sku") or info_item.get("fbo_sku") or info_item.get("fbs_sku")
        if sku_val and int(sku_val) > 0:
            return str(sku_val)
        sources = info_item.get("sources") or []
        for src in sources:
            s_sku = src.get("sku")
            if s_sku and int(s_sku) > 0:
                return str(s_sku)
        return str(info_item.get("id") or info_item.get("product_id") or "")

    async def get_all_product_ids(self) -> List[Dict[str, str]]:
        """Возвращает полный список товаров продавца с реальным SKU витрины Ozon."""
        url_list = f"{self.base_url}/v3/product/list"
        url_info = f"{self.base_url}/v3/product/info/list"
        raw_items = []
        last_id = ""

        async with aiohttp.ClientSession() as session:
            for _ in range(50):
                payload = {
                    "filter": {"visibility": "ALL"},
                    "last_id": last_id,
                    "limit": 1000
                }
                try:
                    async with session.post(url_list, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                        items = data.get("result", {}).get("items", [])
                        if not items:
                            break
                        for item in items:
                            raw_items.append({
                                "product_id": str(item.get("product_id")),
                                "offer_id": str(item.get("offer_id", ""))
                            })
                        new_last_id = data.get("result", {}).get("last_id", "")
                        if not new_last_id or new_last_id == last_id or len(items) < 1000:
                            break
                        last_id = new_last_id
                except Exception as e:
                    logger.warning("Ошибка получения списка товаров Ozon: %s", e)
                    break

            if not raw_items:
                return []

            pid_to_real_sku: Dict[str, str] = {}
            for i in range(0, len(raw_items), 500):
                batch_pids = [int(x["product_id"]) for x in raw_items[i:i + 500] if x["product_id"].isdigit()]
                if not batch_pids:
                    continue
                try:
                    async with session.post(
                        url_info,
                        headers=self.headers,
                        json={"product_id": batch_pids},
                        timeout=aiohttp.ClientTimeout(total=25)
                    ) as resp:
                        if resp.status == 200:
                            info_data = await resp.json()
                            info_items = info_data.get("items") or info_data.get("result", {}).get("items", [])
                            for inf in info_items:
                                pid_str = str(inf.get("id") or inf.get("product_id") or "")
                                real_sku = self._extract_real_sku(inf)
                                if pid_str and real_sku:
                                    pid_to_real_sku[pid_str] = real_sku
                except Exception as e:
                    logger.warning("Ошибка получения детальных SKU Ozon (/v3/product/info/list): %s", e)

        result = []
        for x in raw_items:
            pid = x["product_id"]
            real_sku = pid_to_real_sku.get(pid) or pid
            result.append({
                "sku": real_sku,
                "product_id": pid,
                "offer_id": x["offer_id"]
            })
        return result

    @staticmethod
    def _parse_float(val: Any) -> float:
        if not val:
            return 0.0
        try:
            # Заменяем неразрывные пробелы и валюту: "1 408 ₽" -> 1408.0
            s = str(val).replace("\u2009", "").replace("\xa0", "").replace(" ", "")
            clean = re.sub(r"[^\d.,]", "", s).replace(",", ".")
            return float(clean) if clean else 0.0
        except Exception:
            return 0.0

    @classmethod
    async def _scrape_ozon_storefront_playwright(cls, real_skus: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Забирает реальные клиентские цены витрины Ozon:
        - цену без Ozon Карты («С другими банками», напр. 1408 ₽)
        - зеленую цену с Ozon Картой («С банками Ozon», напр. 1225 ₽)
        через браузерный контекст Playwright (обходя антибот Ozon).
        """
        from playwright.async_api import async_playwright
        storefront: Dict[str, Dict[str, float]] = {}
        if not real_skus:
            return storefront

        is_win = sys.platform.startswith("win")
        use_headed = is_win
        browser = None
        context = None

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
                "--disable-background-networking",
            ]
            if use_headed:
                launch_args.append("--window-position=-2400,-2400")
            else:
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

                async def block_heavy(route, request):
                    if request.resource_type in ("image", "media", "font", "stylesheet"):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", block_heavy)

                # 1. Проходим антибот-челлендж Ozon на первой карточке
                first_sku = real_skus[0]
                try:
                    await page.goto(
                        f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=/product/{first_sku}/",
                        wait_until="domcontentloaded",
                        timeout=20000
                    )
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass

                # 2. Запрашиваем JSON-карточки пачками по 15 штук параллельно внутри открытой сессии Ozon
                chunks = [real_skus[i:i + 15] for i in range(0, len(real_skus), 15)]
                for chunk in chunks:
                    try:
                        raw_results = await page.evaluate(
                            """async (skus) => {
                                const out = {};
                                await Promise.all(skus.map(async (sku) => {
                                    try {
                                        const u = `/api/entrypoint-api.bx/page/json/v2?url=/product/${sku}/`;
                                        const r = await fetch(u, { credentials: 'include', headers: { 'Accept': 'application/json' } });
                                        if (!r.ok) return;
                                        const d = await r.json();
                                        const ws = d.widgetStates || {};
                                        for (const k of Object.keys(ws)) {
                                            if (k.includes('webPrice')) {
                                                const obj = typeof ws[k] === 'string' ? JSON.parse(ws[k]) : ws[k];
                                                out[sku] = {
                                                    cardPrice: obj.cardPrice || obj.ozonCardPrice || '',
                                                    price: obj.price || obj.originalPrice || ''
                                                };
                                                break;
                                            }
                                        }
                                    } catch (e) {}
                                }));
                                return out;
                            }""",
                            chunk
                        )
                        if isinstance(raw_results, dict):
                            for k_sku, val_obj in raw_results.items():
                                card_p = cls._parse_float(val_obj.get("cardPrice"))
                                reg_p = cls._parse_float(val_obj.get("price"))
                                if card_p > 0 or reg_p > 0:
                                    storefront[str(k_sku)] = {
                                        "client_price": reg_p if reg_p > 0 else card_p,
                                        "wallet_price": card_p if card_p > 0 else reg_p,
                                    }
                    except Exception:
                        continue
            except Exception as e:
                logger.warning("Playwright Ozon storefront error: %s", e)
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

        logger.info("Ozon витрина: получены реальные клиентские цены для %s из %s SKU", len(storefront), len(real_skus))
        return storefront

    async def get_prices(self, skus: List[str], offer_ids: List[str] | None = None) -> Dict[str, Dict[str, Any]]:
        """
        Получает:
        - seller_price: цена продавца в ЛК Ozon (напр. 2501 ₽)
        - client_price: цена для покупателя без Ozon Карты («С другими банками», напр. 1408 ₽)
        - wallet_price: зеленая цена с Ozon Картой («С банками Ozon», напр. 1225 ₽)
        - coinvest: реальный процент соинвеста Ozon (без фейковых коэффициентов)
        """
        if not skus and not offer_ids:
            return {}

        url_info = f"{self.base_url}/v3/product/info/list"
        url_prices = f"{self.base_url}/v5/product/info/prices"
        result: Dict[str, Dict[str, Any]] = {}

        async with aiohttp.ClientSession() as session:
            info_items = []
            clean_nums = [int(s) for s in (skus or []) if str(s).strip().isdigit()]

            if offer_ids:
                clean_offers = [str(o).strip() for o in offer_ids if o]
                for i in range(0, len(clean_offers), 500):
                    try:
                        async with session.post(
                            url_info,
                            headers=self.headers,
                            json={"offer_id": clean_offers[i:i + 500]},
                            timeout=aiohttp.ClientTimeout(total=25)
                        ) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                info_items.extend(d.get("items") or d.get("result", {}).get("items", []))
                    except Exception:
                        pass

            if not info_items and clean_nums:
                for key_field in ("sku", "product_id"):
                    try:
                        async with session.post(
                            url_info,
                            headers=self.headers,
                            json={key_field: clean_nums[:500]},
                            timeout=aiohttp.ClientTimeout(total=25)
                        ) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                found = d.get("items") or d.get("result", {}).get("items", [])
                                if found:
                                    info_items.extend(found)
                                    break
                    except Exception:
                        pass

            pid_to_info: Dict[str, Dict[str, Any]] = {}
            pids_for_v5: List[int] = []
            real_skus_list: List[str] = []

            for inf in info_items:
                pid_str = str(inf.get("id") or inf.get("product_id") or "")
                real_sku = self._extract_real_sku(inf)
                offer_id = str(inf.get("offer_id") or "")
                if pid_str.isdigit():
                    pids_for_v5.append(int(pid_str))
                if real_sku and real_sku.isdigit():
                    real_skus_list.append(real_sku)

                pid_to_info[pid_str] = {
                    "real_sku": real_sku,
                    "product_id": pid_str,
                    "offer_id": offer_id,
                    "price": self._parse_float(inf.get("price")),
                    "marketing_price": self._parse_float(inf.get("marketing_price")),
                    "min_price": self._parse_float(inf.get("min_price")),
                }

            v5_pids = pids_for_v5 if pids_for_v5 else clean_nums
            v5_map: Dict[str, Dict[str, Any]] = {}
            if v5_pids:
                payload = {
                    "cursor": "",
                    "filter": {
                        "product_id": v5_pids[:1000],
                        "visibility": "ALL"
                    },
                    "limit": 1000
                }
                try:
                    async with session.post(url_prices, json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("items")
                            if items is None and isinstance(data.get("result"), dict):
                                items = data["result"].get("items", [])
                            for it in (items or []):
                                pid_s = str(it.get("product_id") or "")
                                v5_map[pid_s] = it.get("price") or {}
                except Exception as e:
                    logger.warning("Ошибка Ozon v5 get_prices: %s", e)

            # Собираем реальные цены с витрины Ozon через Playwright
            storefront_map = await self._scrape_ozon_storefront_playwright(real_skus_list)

            all_pids = set(pid_to_info.keys()) | set(v5_map.keys())
            for pid_s in all_pids:
                inf = pid_to_info.get(pid_s, {})
                p_v5 = v5_map.get(pid_s, {})

                real_sku = inf.get("real_sku") or pid_s
                offer_id = inf.get("offer_id") or ""

                # 1. Цена продавца в ЛК (например 2501 ₽)
                base_seller_p = self._parse_float(p_v5.get("price")) or inf.get("price", 0.0)
                fbo_action_p = self._parse_float(p_v5.get("marketing_seller_price")) or base_seller_p
                seller_price = fbo_action_p if (0 < fbo_action_p < base_seller_p) else base_seller_p

                # 2. Реальные цены с витрины Ozon (если товар в наличии на витрине)
                sf = storefront_map.get(real_sku, {})
                sf_client_p = sf.get("client_price", 0.0)   # Напр. 1408 ₽ («С другими банками»)
                sf_wallet_p = sf.get("wallet_price", 0.0)   # Напр. 1225 ₽ («С банками Ozon»)

                api_mkt_p = (
                    inf.get("marketing_price", 0.0)
                    or self._parse_float(p_v5.get("marketing_price"))
                    or self._parse_float(p_v5.get("retail_price"))
                )

                if sf_client_p > 0:
                    client_price = sf_client_p
                    wallet_price = sf_wallet_p if sf_wallet_p > 0 else sf_client_p
                elif api_mkt_p > 0 and api_mkt_p < seller_price:
                    client_price = api_mkt_p
                    wallet_price = api_mkt_p
                else:
                    # Если витрина не ответила — честно оставляем цену продавца и 0% (без фейковых * 0.915!)
                    client_price = seller_price
                    wallet_price = seller_price

                # 3. Процент Соинвеста Ozon (считаем по цене витрины без карты относительно цены продавца, как на WB)
                coinvest = 0
                if seller_price > 0 and 0 < client_price < seller_price:
                    coinvest = round(((seller_price - client_price) / seller_price) * 100)
                elif seller_price > 0 and 0 < wallet_price < seller_price:
                    coinvest = round(((seller_price - wallet_price) / seller_price) * 100)

                entry = {
                    "seller_price": round(seller_price, 2),
                    "client_price": round(client_price, 2),
                    "wallet_price": round(wallet_price, 2),
                    "coinvest": coinvest,
                    "real_sku": real_sku,
                    "product_id": pid_s,
                    "offer_id": offer_id,
                    "from_storefront": bool(sf_client_p > 0),
                }
                result[real_sku] = entry
                result[pid_s] = entry
                if offer_id:
                    result[offer_id] = entry

        return result

    async def get_promotions(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v1/actions"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", [])
            except Exception as e:
                logger.warning("Ошибка получения акций Ozon: %s", e)
        return []

    async def get_promotion_candidates(self, action_id: int) -> List[Dict[str, Any]]:
        url_cands = f"{self.base_url}/v1/actions/candidates"
        url_prods = f"{self.base_url}/v1/actions/products"
        url_info = f"{self.base_url}/v3/product/info/list"

        raw_list: List[Dict[str, Any]] = []
        seen_pids = set()

        async with aiohttp.ClientSession() as session:
            try:
                payload_in = {"action_id": int(action_id), "limit": 1000, "offset": 0}
                async with session.post(url_prods, json=payload_in, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        in_items = data.get("result", {}).get("products", [])
                        for it in in_items:
                            pid = str(it.get("id") or it.get("product_id") or "")
                            if pid and pid not in seen_pids:
                                seen_pids.add(pid)
                                it["_in_action"] = True
                                raw_list.append(it)
            except Exception as e:
                logger.warning("Ошибка получения участвующих товаров акции Ozon %s: %s", action_id, e)

            try:
                payload_cand = {"action_id": int(action_id), "limit": 1000, "offset": 0}
                async with session.post(url_cands, json=payload_cand, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cand_items = data.get("result", {}).get("products", [])
                        for it in cand_items:
                            pid = str(it.get("id") or it.get("product_id") or "")
                            if pid and pid not in seen_pids:
                                seen_pids.add(pid)
                                it["_in_action"] = False
                                raw_list.append(it)
            except Exception as e:
                logger.warning("Ошибка получения кандидатов акции Ozon %s: %s", action_id, e)

            if not raw_list:
                return []

            pid_meta: Dict[str, Dict[str, str]] = {}
            all_pids = [int(p) for p in seen_pids if p.isdigit()]
            for i in range(0, len(all_pids), 500):
                batch_pids = all_pids[i:i + 500]
                try:
                    async with session.post(
                        url_info,
                        headers=self.headers,
                        json={"product_id": batch_pids},
                        timeout=aiohttp.ClientTimeout(total=25)
                    ) as resp:
                        if resp.status == 200:
                            info_data = await resp.json()
                            info_items = info_data.get("items") or info_data.get("result", {}).get("items", [])
                            for inf in info_items:
                                pid_s = str(inf.get("id") or inf.get("product_id") or "")
                                real_sku = self._extract_real_sku(inf)
                                offer_id = str(inf.get("offer_id") or "")
                                if pid_s:
                                    pid_meta[pid_s] = {
                                        "real_sku": real_sku or pid_s,
                                        "offer_id": offer_id
                                    }
                except Exception as e:
                    logger.warning("Ошибка маппинга SKU для акции Ozon: %s", e)

        for item in raw_list:
            pid_s = str(item.get("id") or item.get("product_id") or "")
            meta = pid_meta.get(pid_s, {})
            item["product_id"] = pid_s
            item["real_sku"] = meta.get("real_sku") or pid_s
            item["offer_id"] = meta.get("offer_id") or ""

        return raw_list

    async def activate_promotion_products(self, action_id: int, products: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if not products:
            return False, "Нет подходящих товаров для добавления."
        url = f"{self.base_url}/v1/actions/products/activate"
        payload = {"action_id": int(action_id), "products": products}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        return True, "Товары добавлены в акцию Ozon"
                    err = await resp.text()
                    return False, f"Ошибка Ozon ({resp.status}): {err[:150]}"
            except Exception as e:
                return False, f"Ошибка сети: {e}"