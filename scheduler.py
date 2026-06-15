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
    # flag=0: all orders where lastChangeDate >= dateFrom
    date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        orders = await wb_client.get_orders(token, date_from)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] orders error: %s", cabinet_id, e)
        return

    db = await get_db()
    try:
        for o in orders:
            # Statistics API fields:
            # srid = unique order identifier (recommended by WB docs)
            # date = creation date, supplierArticle = article, nmId = product ID
            # finishedPrice = final price, regionName = region
            # isRealization = sold, isSupply = supply/FBO
            order_id = str(o.get("srid") or o.get("gNumber") or "")
            if not order_id:
                continue
            created = (o.get("date") or "")[:10]
            if o.get("isRealization"):
                status = "sale"
            elif o.get("isSupply"):
                status = "supply"
            else:
                status = "new"
            price = o.get("finishedPrice") or o.get("priceWithDisc") or 0
            await db.execute("""
                INSERT OR IGNORE INTO orders_cache
                    (cabinet_id, order_id, date, article, nm_id, status, price, region,
                     barcode, size, subject, discount_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cabinet_id, order_id, created,
                o.get("supplierArticle"), o.get("nmId"),
                status, price, o.get("regionName"),
                o.get("barcode"),
                o.get("techSize"),
                o.get("subject") or o.get("category"),
                o.get("discountPercent", 0) or 0,
            ))
        await db.commit()
        logger.info("[cabinet %d] orders synced: %d", cabinet_id, len(orders))
    finally:
        await db.close()


async def _fetch_stocks(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    try:
        stocks = await wb_client.get_stocks(token)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] stocks error: %s", cabinet_id, e)
        return

    if not stocks:
        logger.info("[cabinet %d] stocks: empty result", cabinet_id)
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = await get_db()
    try:
        # Remove stale snapshot (older than 1h)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.execute("DELETE FROM stock_cache WHERE cabinet_id=? AND checked_at<?",
                         (cabinet_id, cutoff))

        for s in stocks:
            # warehouse_remains response:
            # nmId, vendorCode (article), subjectName (name), techSize, volume
            # warehouses: [{warehouseName, quantity, ...}]
            nm_id = s.get("nmId")
            article = s.get("vendorCode") or s.get("supplierArticle")
            name = s.get("subjectName") or s.get("subject") or s.get("category")

            warehouses = s.get("warehouses") or []
            if warehouses:
                # Store one row per warehouse
                for wh in warehouses:
                    wh_name = wh.get("warehouseName") or wh.get("name") or "Неизвестно"
                    qty = wh.get("quantity") or wh.get("remains") or 0
                    await db.execute("""
                        INSERT OR IGNORE INTO stock_cache
                            (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (cabinet_id, checked_at, nm_id, article, name, qty, wh_name))
            else:
                # No warehouse breakdown — store aggregate
                qty = s.get("quantity") or s.get("inWayToClient") or 0
                await db.execute("""
                    INSERT OR IGNORE INTO stock_cache
                        (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (cabinet_id, checked_at, nm_id, article, name, qty, "Общий"))

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
        # /adv/v3/fullstats: GET with ids, beginDate, endDate
        stats = await wb_client.get_ad_stats(token, campaign_ids, week_ago, today)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] ads error: %s", cabinet_id, e)
        return

    campaign_names = {c["id"]: c["name"] for c in campaigns if c.get("id")}

    db = await get_db()
    try:
        for stat in stats:
            cid = stat.get("advertId")
            if not cid:
                continue
            # v3/fullstats returns aggregate fields at root level
            # Also may have per-day breakdown in stat.get("days", [])
            days = stat.get("days") or []
            if days:
                for day in days:
                    date = (day.get("date") or today)[:10]
                    await db.execute("""
                        INSERT INTO ad_stats
                            (cabinet_id, date, campaign_id, campaign_name, spend, views, clicks, orders)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cabinet_id, date, campaign_id) DO UPDATE SET
                            spend=excluded.spend, views=excluded.views,
                            clicks=excluded.clicks, orders=excluded.orders,
                            campaign_name=excluded.campaign_name
                    """, (
                        cabinet_id, date, cid, campaign_names.get(cid),
                        day.get("sum", 0), day.get("views", 0),
                        day.get("clicks", 0), day.get("orders", 0),
                    ))
            else:
                # No daily breakdown — store aggregate as single record for today
                await db.execute("""
                    INSERT INTO ad_stats
                        (cabinet_id, date, campaign_id, campaign_name, spend, views, clicks, orders)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cabinet_id, date, campaign_id) DO UPDATE SET
                        spend=excluded.spend, views=excluded.views,
                        clicks=excluded.clicks, orders=excluded.orders
                """, (
                    cabinet_id, today, cid, campaign_names.get(cid),
                    stat.get("sum", 0), stat.get("views", 0),
                    stat.get("clicks", 0), stat.get("orders", 0),
                ))
        await db.commit()
        logger.info("[cabinet %d] ads synced: %d campaigns", cabinet_id, len(stats))
    finally:
        await db.close()


async def _fetch_finances(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        rows = await wb_client.get_finance_report(token, date_from, date_to)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] finances error: %s", cabinet_id, e)
        return

    # New API returns weekly report summaries: {reportId, dateFrom, dateTo, retailAmountSum, forPaySum, ...}
    # Old API returns per-row detail: {rr_dt, retail_price_withdisc_rub, commission_percent, ...}
    db = await get_db()
    try:
        for row in rows:
            # Detect which format based on keys
            if "reportId" in row:
                # New finance API (weekly reports)
                date = (row.get("dateFrom") or row.get("dateTo") or "")[:10]
                revenue = float(row.get("retailAmountSum") or 0)
                logistics = float(row.get("deliveryServiceSum") or 0)
                penalty = float(row.get("penaltySum") or 0)
                to_pay = float(row.get("forPaySum") or 0)
                # Commission = revenue - logistics - penalty - to_pay (approx)
                commission = max(0, revenue - to_pay - logistics - penalty)
            else:
                # Old statistics API (per-row detail)
                date = (row.get("rr_dt") or "")[:10]
                if not date:
                    continue
                revenue = row.get("retail_price_withdisc_rub", 0) or 0
                pct = row.get("commission_percent", 0) or 0
                commission = round(revenue * pct / 100, 2)
                logistics = row.get("delivery_rub", 0) or 0
                penalty = row.get("penalty", 0) or 0
                to_pay = row.get("ppvz_for_pay", 0) or 0

            if not date:
                continue

            await db.execute("""
                INSERT INTO finance_report (cabinet_id, date, revenue, commission, logistics, penalty, to_pay)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cabinet_id, date) DO UPDATE SET
                    revenue=revenue+excluded.revenue,
                    commission=commission+excluded.commission,
                    logistics=logistics+excluded.logistics,
                    penalty=penalty+excluded.penalty,
                    to_pay=to_pay+excluded.to_pay
            """, (cabinet_id, date, revenue, commission, logistics, penalty, to_pay))

        await db.commit()
        logger.info("[cabinet %d] finances synced: %d rows", cabinet_id, len(rows))
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
        for fn in (_fetch_orders, _fetch_stocks, _fetch_ads, _fetch_finances):
            try:
                await fn(cabinet)
            except Exception as e:
                logger.error("[cabinet %d] %s failed: %s", cabinet["id"], fn.__name__, e)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_all_cabinets, "interval", minutes=10, id="sync_all",
                      max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started")
    return scheduler
