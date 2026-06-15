"""
Wildberries API client — orders, stocks, ads, finances.
All methods accept token explicitly — no global state.
Handles 429 rate limits with exponential backoff.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_BASE_MARKETPLACE = "https://marketplace-api.wildberries.ru"
_BASE_STATISTICS   = "https://statistics-api.wildberries.ru"
_BASE_ADVERT       = "https://advert-api.wildberries.ru"

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
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=_headers(token), params=params or {})
        if r.status_code == 429:
            wait = int(r.headers.get("X-Ratelimit-Reset", 60))
            wait = min(wait, 60)  # cap at 60s
            logger.warning("WB rate limit hit %s, waiting %ds (attempt %d)", path, wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
        return r.json()
    raise WBApiError(429, "Rate limit: max retries exceeded")


async def _post(token: str, base: str, path: str, body: dict | list) -> dict | list:
    url = f"{base}{path}"
    for attempt in range(_MAX_RETRIES):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=_headers(token), json=body)
        if r.status_code == 429:
            wait = int(r.headers.get("X-Ratelimit-Reset", 60))
            wait = min(wait, 60)
            logger.warning("WB rate limit hit %s, waiting %ds (attempt %d)", url, wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
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


# ── Orders (statistics API — full history) ────────────────────────────────────

async def get_orders(token: str, date_from: str) -> list[dict]:
    """Returns all orders since date_from using statistics API (flag=1 = by creation date)."""
    data = await _get(token, _BASE_STATISTICS, "/api/v1/supplier/orders", {
        "dateFrom": date_from,
        "flag": 1,
    })
    return data if isinstance(data, list) else []


# ── Stocks ────────────────────────────────────────────────────────────────────

async def get_stocks(token: str, date_from: str) -> list[dict]:
    data = await _get(token, _BASE_STATISTICS, "/api/v1/supplier/stocks", {"dateFrom": date_from})
    return data if isinstance(data, list) else []


# ── Advertising ───────────────────────────────────────────────────────────────

async def get_ad_campaigns(token: str) -> list[dict]:
    try:
        data = await _get(token, _BASE_ADVERT, "/adv/v1/promotion/count")
    except WBApiError:
        return []
    if not isinstance(data, dict):
        return []
    campaigns = []
    for status_group in data.get("adverts", []) or []:
        for adv in status_group.get("advert_list", []) or []:
            campaigns.append({
                "id": adv.get("advertId"),
                "name": adv.get("name", ""),
                "status": status_group.get("status"),
                "type": adv.get("type"),
            })
    return campaigns


async def get_ad_stats(token: str, campaign_ids: list[int], date_from: str, date_to: str) -> list[dict]:
    if not campaign_ids:
        return []
    body = [{"id": cid, "dates": [date_from, date_to]} for cid in campaign_ids[:50]]
    try:
        data = await _post(token, _BASE_ADVERT, "/adv/v2/fullstats", body)
    except WBApiError:
        return []
    return data if isinstance(data, list) else []


# ── Finances ──────────────────────────────────────────────────────────────────

async def get_finance_report(token: str, date_from: str, date_to: str) -> list[dict]:
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
