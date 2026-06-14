"""
Wildberries API client — covers orders, stocks, ads, finances.
All methods accept token explicitly — no global state.
"""
import httpx

_BASE_MARKETPLACE = "https://marketplace-api.wildberries.ru"
_BASE_STATISTICS   = "https://statistics-api.wildberries.ru"
_BASE_ADVERT       = "https://advert-api.wildberries.ru"


class WBApiError(Exception):
    def __init__(self, status: int, text: str):
        self.status = status
        super().__init__(f"WB API {status}: {text}")


def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}


async def _get(token: str, base: str, path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{base}{path}", headers=_headers(token), params=params or {})
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
        return r.json()


async def _post(token: str, base: str, path: str, body: dict | list) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{base}{path}", headers=_headers(token), json=body)
        if r.status_code >= 400:
            raise WBApiError(r.status_code, r.text[:300])
        return r.json()


# ── Token validation ──────────────────────────────────────────────────────────

async def validate_token(token: str) -> bool:
    """Returns True if token can reach WB API, False if clearly invalid."""
    from datetime import datetime, timedelta, timezone
    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        await _get(token, _BASE_MARKETPLACE, "/api/v3/orders", {"dateFrom": date_from, "limit": 1})
        return True
    except WBApiError as e:
        if e.status in (401, 403):
            return False
        # Any other HTTP error (429, 5xx) — token format is OK, accept it
        return True
    except Exception:
        return False


# ── Orders ────────────────────────────────────────────────────────────────────

async def get_orders(token: str, date_from: str, limit: int = 1000) -> list[dict]:
    """Returns list of orders since date_from (ISO-8601 string)."""
    data = await _get(token, _BASE_MARKETPLACE, "/api/v3/orders", {
        "dateFrom": date_from,
        "limit": limit,
    })
    return data.get("orders", []) if isinstance(data, dict) else []


# ── Stocks ────────────────────────────────────────────────────────────────────

async def get_stocks(token: str, date_from: str) -> list[dict]:
    """
    Returns current stock levels from Statistics API.
    date_from: date string YYYY-MM-DD
    """
    data = await _get(token, _BASE_STATISTICS, "/api/v1/supplier/stocks", {
        "dateFrom": date_from,
    })
    return data if isinstance(data, list) else []


# ── Advertising ───────────────────────────────────────────────────────────────

async def get_ad_campaigns(token: str) -> list[dict]:
    """Returns list of all ad campaigns."""
    data = await _get(token, _BASE_ADVERT, "/adv/v1/promotion/count")
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
    """Returns full statistics for given campaign IDs."""
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
    """
    Detailed sales report (weekly payout report).
    Returns list of report rows.
    """
    data = await _get(token, _BASE_STATISTICS, "/api/v5/supplier/reportDetailByPeriod", {
        "dateFrom": date_from,
        "dateTo": date_to,
        "rrdid": 0,
        "limit": 100000,
    })
    return data if isinstance(data, list) else []
