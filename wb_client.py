"""
Wildberries API client — orders, stocks, ads, finances.
All methods accept token explicitly — no global state.
Handles 429 rate limits with exponential backoff.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_BASE_STATISTICS        = "https://statistics-api.wildberries.ru"
_BASE_SELLER_ANALYTICS  = "https://seller-analytics-api.wildberries.ru"
_BASE_FINANCE           = "https://finance-api.wildberries.ru"
_BASE_ADVERT            = "https://advert-api.wildberries.ru"

_MAX_RETRIES = 3


class WBApiError(Exception):
    def __init__(self, status: int, text: str):
        self.status = status
        super().__init__(f"WB API {status}: {text}")


def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}


async def _get(token: str, base: str, path: str, params: dict | None = None) -> dict | list:
    url = f"{base}{path}"
    for attempt in range(_MAX_RETRIES):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url, headers=_headers(token), params=params or {})
        if r.status_code == 429:
            wait = min(int(r.headers.get("X-Ratelimit-Reset", 60)), 60)
            logger.warning("WB rate limit hit %s, waiting %ds (attempt %d)", path, wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
        if not r.content:
            return []
        return r.json()
    raise WBApiError(429, "Rate limit: max retries exceeded")


async def _post(token: str, base: str, path: str, body: dict | list) -> dict | list:
    url = f"{base}{path}"
    for attempt in range(_MAX_RETRIES):
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=_headers(token), json=body)
        if r.status_code == 429:
            wait = min(int(r.headers.get("X-Ratelimit-Reset", 60)), 60)
            logger.warning("WB rate limit hit %s, waiting %ds (attempt %d)", url, wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
        if not r.content:
            return []
        return r.json()
    raise WBApiError(429, "Rate limit: max retries exceeded")


# ── Token validation ──────────────────────────────────────────────────────────

async def validate_token(token: str) -> bool:
    """Returns True if token can reach WB API. False only on 401/403."""
    from datetime import datetime, timedelta, timezone
    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        await _get(token, _BASE_STATISTICS, "/api/v1/supplier/orders", {"dateFrom": date_from, "flag": 0})
        return True
    except WBApiError as e:
        if e.status in (401, 403):
            return False
        return True
    except Exception:
        return False


# ── Orders (statistics API, flag=0 = all updated since dateFrom) ──────────────

async def get_orders(token: str, date_from: str) -> list[dict]:
    """
    Returns all orders since date_from.
    flag=0: returns rows where lastChangeDate >= dateFrom (up to 80 000 per call).
    Paginates via lastChangeDate of last row until empty response.
    """
    all_orders: list[dict] = []
    current_from = date_from
    while True:
        data = await _get(token, _BASE_STATISTICS, "/api/v1/supplier/orders", {
            "dateFrom": current_from,
            "flag": 0,
        })
        batch = data if isinstance(data, list) else []
        if not batch:
            break
        all_orders.extend(batch)
        if len(batch) < 80000:
            break
        # Paginate: use lastChangeDate of last row as next dateFrom
        last_change = batch[-1].get("lastChangeDate", "")
        if not last_change or last_change == current_from:
            break
        current_from = last_change
    return all_orders


# ── Stocks (async task API — replaces deprecated /api/v1/supplier/stocks) ─────

async def get_stocks(token: str) -> list[dict]:
    """
    Creates a warehouse_remains report task, polls until done, returns results.
    Uses seller-analytics-api (Аналитика token category).
    """
    # Step 1: create task
    try:
        resp = await _get(token, _BASE_SELLER_ANALYTICS, "/api/v1/warehouse_remains",
                          {"groupBySize": "true", "groupByBarcode": "true"})
    except WBApiError as e:
        logger.error("warehouse_remains create task error: %s", e)
        return []
    task_id = resp.get("data", {}).get("taskId") if isinstance(resp, dict) else None
    if not task_id:
        logger.error("warehouse_remains: no taskId in response: %s", resp)
        return []

    # Step 2: poll status (max 120s)
    status = None
    for _ in range(24):
        await asyncio.sleep(5)
        try:
            status_resp = await _get(token, _BASE_SELLER_ANALYTICS,
                                     f"/api/v1/warehouse_remains/tasks/{task_id}/status", {})
        except WBApiError:
            break
        status = status_resp.get("data", {}).get("status") if isinstance(status_resp, dict) else None
        logger.debug("warehouse_remains task %s status: %s", task_id, status)
        if status in ("done", "complete", "completed", "success"):
            break
        logger.debug("warehouse_remains task %s status=%s, waiting...", task_id, status)
    else:
        logger.error("warehouse_remains task %s timed out (last status=%s)", task_id, status)
        return []

    # Step 3: download
    try:
        data = await _get(token, _BASE_SELLER_ANALYTICS,
                          f"/api/v1/warehouse_remains/tasks/{task_id}/download", {})
    except WBApiError as e:
        logger.error("warehouse_remains download error: %s", e)
        return []
    return data if isinstance(data, list) else []


# ── Advertising ───────────────────────────────────────────────────────────────

async def get_ad_campaigns(token: str) -> list[dict]:
    """
    Get all campaigns with statuses 4/7/9/11 (ready/finished/active/paused).
    Step 1: /adv/v1/promotion/count → campaign IDs grouped by status
    Step 2: /adv/v1/advert?id=... → campaign name (one call per id, cached)
    """
    try:
        count_data = await _get(token, _BASE_ADVERT, "/adv/v1/promotion/count")
    except WBApiError:
        return []
    if not isinstance(count_data, dict):
        return []

    campaigns = []
    for status_group in count_data.get("adverts", []) or []:
        status = status_group.get("status")
        if status not in (4, 7, 9, 11):
            continue
        for adv in status_group.get("advert_list", []) or []:
            cid = adv.get("advertId")
            if cid:
                campaigns.append({"id": cid, "name": None, "status": status})

    if not campaigns:
        return []

    # Fetch names by passing actual IDs (max 50 per batch)
    name_map: dict = {}
    all_ids = [c["id"] for c in campaigns]
    for i in range(0, len(all_ids), 50):
        batch = all_ids[i:i+50]
        try:
            data = await _get(token, _BASE_ADVERT, "/api/advert/v2/adverts", {
                "ids": ",".join(str(x) for x in batch),
            })
            adverts_list = data if isinstance(data, list) else (data.get("adverts") or [] if isinstance(data, dict) else [])
            for adv in adverts_list:
                if not isinstance(adv, dict):
                    continue
                # /api/advert/v2/adverts uses "id" (not "advertId"), name in settings.name
                cid = adv.get("id") or adv.get("advertId")
                name = (adv.get("settings") or {}).get("name") or adv.get("name") or ""
                if cid:
                    name_map[cid] = name
        except WBApiError:
            pass
    for c in campaigns:
        c["name"] = name_map.get(c["id"]) or str(c["id"])

    return campaigns


async def get_ad_stats(token: str, campaign_ids: list[int], date_from: str, date_to: str) -> list[dict]:
    """
    GET /adv/v3/fullstats — aggregated stats for campaigns over date range.
    Returns list of {advertId, clicks, views, orders, sum, ctr, cr, days: []}.
    """
    if not campaign_ids:
        return []
    results = []
    for i in range(0, len(campaign_ids), 50):
        batch = campaign_ids[i:i+50]
        try:
            data = await _get(token, _BASE_ADVERT, "/adv/v3/fullstats", {
                "ids": ",".join(str(x) for x in batch),
                "beginDate": date_from,
                "endDate": date_to,
            })
        except WBApiError as e:
            logger.warning("ad stats error for batch: %s", e)
            continue
        if isinstance(data, list):
            results.extend(data)
    return results


# ── Finances ──────────────────────────────────────────────────────────────────

async def get_finance_report(token: str, date_from: str, date_to: str) -> list[dict]:
    """
    Weekly sales reports via new finance API.
    POST finance-api.wildberries.ru/api/finance/v1/sales-reports/list
    Returns list of weekly report summaries.
    """
    try:
        data = await _post(token, _BASE_FINANCE, "/api/finance/v1/sales-reports/list", {
            "dateFrom": date_from,
            "dateTo": date_to,
        })
    except WBApiError as e:
        logger.warning("finance new API error (%s), trying legacy endpoint", e)
        # Fallback to old statistics API endpoint (deprecated but still works until Jul 15)
        try:
            data = await _get(token, _BASE_STATISTICS, "/api/v5/supplier/reportDetailByPeriod", {
                "dateFrom": date_from,
                "dateTo": date_to,
                "rrdid": 0,
                "limit": 100000,
            })
        except WBApiError:
            return []
    return data if isinstance(data, list) else []
