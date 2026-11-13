import json
import logging
import re
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
            s = str(val).replace("\u2009", "").replace("\xa0", "").replace(" ", "")
            clean = re.sub(r"[^\d.,]", "", s).replace(",", ".")
            return float(clean) if clean else 0.0
        except Exception:
            return 0.0

    async def get_prices(self, skus: List[str], offer_ids: List[str] | None = None) -> Dict[str, Dict[str, Any]]:
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

            if info_items:
                s_inf = info_items[0]
                logger.info(
                    "OZON V3 INFO SAMPLE (offer=%s): price=%s, marketing_price=%s, min_price=%s, price_indexes=%s",
                    s_inf.get("offer_id"),
                    s_inf.get("price"),
                    s_inf.get("marketing_price"),
                    s_inf.get("min_price"),
                    json.dumps(s_inf.get("price_indexes") or {}, ensure_ascii=False)[:300]
                )

            for inf in info_items:
                pid_str = str(inf.get("id") or inf.get("product_id") or "")
                real_sku = self._extract_real_sku(inf)
                offer_id = str(inf.get("offer_id") or "")
                if pid_str.isdigit():
                    pids_for_v5.append(int(pid_str))

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
                            if items:
                                logger.info("OZON V5 PRICE SAMPLE: %s", json.dumps(items[0], ensure_ascii=False)[:600])
                            for it in (items or []):
                                pid_s = str(it.get("product_id") or "")
                                v5_map[pid_s] = it
                except Exception as e:
                    logger.warning("Ошибка Ozon v5 get_prices: %s", e)

            all_pids = set(pid_to_info.keys()) | set(v5_map.keys())
            for pid_s in all_pids:
                inf = pid_to_info.get(pid_s, {})
                it_v5 = v5_map.get(pid_s, {})
                p_v5 = it_v5.get("price") or {}
                mkt_actions = (it_v5.get("marketing_actions") or {}).get("actions") or []

                real_sku = inf.get("real_sku") or pid_s
                offer_id = inf.get("offer_id") or ""

                base_seller_p = self._parse_float(p_v5.get("price")) or inf.get("price", 0.0)
                fbo_action_p = self._parse_float(p_v5.get("marketing_seller_price")) or base_seller_p
                seller_price = base_seller_p if base_seller_p > 0 else fbo_action_p

                candidates_client = [
                    inf.get("marketing_price", 0.0),
                    self._parse_float(p_v5.get("marketing_price")),
                    self._parse_float(p_v5.get("retail_price")),
                    fbo_action_p,
                ]
                for act in mkt_actions:
                    act_p = self._parse_float(act.get("price") or act.get("action_price") or act.get("value"))
                    if act_p > 0:
                        candidates_client.append(act_p)

                valid_client = [c for c in candidates_client if c > 0]
                client_price = min(valid_client) if valid_client else seller_price
                wallet_price = client_price

                coinvest = 0
                if seller_price > 0 and 0 < client_price < seller_price:
                    coinvest = round(((seller_price - client_price) / seller_price) * 100)

                entry = {
                    "seller_price": round(seller_price, 2),
                    "client_price": round(client_price, 2),
                    "wallet_price": round(wallet_price, 2),
                    "coinvest": coinvest,
                    "real_sku": real_sku,
                    "product_id": pid_s,
                    "offer_id": offer_id,
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