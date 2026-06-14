"""
Background data collection from WB API for all cabinets.
Runs inside the FastAPI process via APScheduler.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import wb_client
from database import get_db

logger = logging.getLogger(__name__)


async def _fetch_orders(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    date_from = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        orders = await wb_client.get_orders(token, date_from)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] orders error: %s", cabinet_id, e)
        return

    db = await get_db()
    try:
        for o in orders:
            order_id = str(o.get("id", ""))
            created = o.get("createdAt", "")[:10] if o.get("createdAt") else ""
            await db.execute("""
                INSERT OR IGNORE INTO orders_cache
                    (cabinet_id, order_id, date, article, nm_id, status, price, region)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cabinet_id, order_id, created,
                o.get("article"), o.get("nmId"),
                o.get("wbStatus") or o.get("status"),
                o.get("convertedPrice", 0) / 100 if o.get("convertedPrice") else 0,
                o.get("regionName"),
            ))
        await db.commit()
        logger.info("[cabinet %d] orders synced: %d", cabinet_id, len(orders))
    finally:
        await db.close()


async def _fetch_stocks(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        stocks = await wb_client.get_stocks(token, date_from)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] stocks error: %s", cabinet_id, e)
        return

    if not stocks:
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = await get_db()
    try:
        # Keep only latest snapshot: delete older entries for this cabinet
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.execute("DELETE FROM stock_cache WHERE cabinet_id=? AND checked_at<?",
                         (cabinet_id, cutoff))
        for s in stocks:
            await db.execute("""
                INSERT OR IGNORE INTO stock_cache
                    (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                cabinet_id, checked_at,
                s.get("nmId"), s.get("supplierArticle"),
                s.get("subject") or s.get("category"),
                s.get("quantity", 0),
                s.get("warehouseName"),
            ))
        await db.commit()
        logger.info("[cabinet %d] stocks synced: %d items", cabinet_id, len(stocks))
    finally:
        await db.close()


async def _fetch_ads(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        campaigns = await wb_client.get_ad_campaigns(token)
        campaign_ids = [c["id"] for c in campaigns if c.get("id")]
        stats = await wb_client.get_ad_stats(token, campaign_ids, week_ago, today)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] ads error: %s", cabinet_id, e)
        return

    campaign_names = {c["id"]: c["name"] for c in campaigns}
    db = await get_db()
    try:
        for stat in stats:
            cid = stat.get("advertId")
            for day in stat.get("days", []) or []:
                date = (day.get("date") or "")[:10]
                apps = day.get("apps", [{}])
                totals = apps[0] if apps else {}
                await db.execute("""
                    INSERT INTO ad_stats
                        (cabinet_id, date, campaign_id, campaign_name, spend, views, clicks, orders)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cabinet_id, date, campaign_id) DO UPDATE SET
                        spend=excluded.spend, views=excluded.views,
                        clicks=excluded.clicks, orders=excluded.orders
                """, (
                    cabinet_id, date, cid, campaign_names.get(cid),
                    totals.get("sum", 0), totals.get("views", 0),
                    totals.get("clicks", 0), totals.get("orders", 0),
                ))
        await db.commit()
        logger.info("[cabinet %d] ads synced: %d campaigns", cabinet_id, len(stats))
    finally:
        await db.close()


async def _fetch_finances(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_from = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        rows = await wb_client.get_finance_report(token, date_from, date_to)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] finances error: %s", cabinet_id, e)
        return

    # Aggregate by date
    by_date: dict[str, dict] = {}
    for row in rows:
        date = (row.get("rr_dt") or "")[:10]
        if not date:
            continue
        d = by_date.setdefault(date, {"revenue": 0, "commission": 0, "logistics": 0, "penalty": 0, "to_pay": 0})
        d["revenue"] += row.get("retail_price_withdisc_rub", 0) or 0
        d["commission"] += row.get("commission_percent", 0) or 0
        d["logistics"] += row.get("delivery_rub", 0) or 0
        d["penalty"] += row.get("penalty", 0) or 0
        d["to_pay"] += row.get("ppvz_for_pay", 0) or 0

    db = await get_db()
    try:
        for date, vals in by_date.items():
            await db.execute("""
                INSERT INTO finance_report (cabinet_id, date, revenue, commission, logistics, penalty, to_pay)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cabinet_id, date) DO UPDATE SET
                    revenue=excluded.revenue, commission=excluded.commission,
                    logistics=excluded.logistics, penalty=excluded.penalty, to_pay=excluded.to_pay
            """, (cabinet_id, date, vals["revenue"], vals["commission"],
                  vals["logistics"], vals["penalty"], vals["to_pay"]))
        await db.commit()
        logger.info("[cabinet %d] finances synced: %d days", cabinet_id, len(by_date))
    finally:
        await db.close()


async def sync_all_cabinets() -> None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT id, name, api_token FROM wb_cabinets")
        cabinets = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    for cabinet in cabinets:
        await _fetch_orders(cabinet)
        await _fetch_stocks(cabinet)
        await _fetch_ads(cabinet)
        await _fetch_finances(cabinet)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_all_cabinets, "interval", minutes=10, id="sync_all",
                      max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started")
    return scheduler
