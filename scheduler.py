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
    # flag=0: all rows where lastChangeDate >= dateFrom
    date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        orders = await wb_client.get_orders(token, date_from)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] orders error: %s", cabinet_id, e)
        return

    try:
        sales = await wb_client.get_sales(token, date_from)
    except wb_client.WBApiError as e:
        logger.warning("[cabinet %d] sales error: %s", cabinet_id, e)
        sales = []

    # Map srid -> 'sale' / 'return' / 'cancel'
    sale_status: dict[str, str] = {}
    for s in sales:
        srid = str(s.get("srid") or s.get("gNumber") or "")
        if not srid:
            continue
        # /sales returns both purchases and returns: saleID prefix S = sale, R = return
        sale_id = s.get("saleID") or ""
        if sale_id.startswith("R"):
            sale_status[srid] = "return"
        else:
            sale_status[srid] = "sale"

    db = await get_db()
    try:
        for o in orders:
            order_id = str(o.get("srid") or o.get("gNumber") or "")
            if not order_id:
                continue
            created = (o.get("date") or "")[:10]
            # Status priority: paid sale > return > cancel > new
            if order_id in sale_status:
                status = sale_status[order_id]
            elif o.get("isCancel") or o.get("cancelDate"):
                status = "cancel"
            else:
                status = "new"
            price = o.get("finishedPrice") or o.get("priceWithDisc") or 0
            # UPSERT — re-syncs propagate status/price changes (cancellations, refunds).
            await db.execute("""
                INSERT INTO orders_cache
                    (cabinet_id, order_id, date, article, nm_id, status, price, region,
                     barcode, size, subject, discount_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cabinet_id, order_id) DO UPDATE SET
                    status=excluded.status,
                    price=excluded.price,
                    region=excluded.region,
                    discount_percent=excluded.discount_percent
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
        logger.info("[cabinet %d] orders synced: %d (sales=%d)",
                    cabinet_id, len(orders), len(sales))
    finally:
        await db.close()


async def _fetch_stocks(cabinet: dict) -> None:
    cabinet_id = cabinet["id"]
    token = cabinet["api_token"]

    # FBO stocks via warehouse_remains (seller-analytics API)
    try:
        fbo_stocks = await wb_client.get_stocks(token)
    except wb_client.WBApiError as e:
        logger.error("[cabinet %d] FBO stocks error: %s", cabinet_id, e)
        fbo_stocks = []

    # FBS stocks via marketplace API — use barcodes from recent orders
    db_bc = await get_db()
    try:
        cur = await db_bc.execute(
            """SELECT DISTINCT barcode, article, nm_id, subject
               FROM orders_cache
               WHERE cabinet_id=? AND barcode IS NOT NULL""",
            (cabinet_id,)
        )
        barcode_rows = await cur.fetchall()
    finally:
        await db_bc.close()

    barcode_meta = {r["barcode"]: dict(r) for r in barcode_rows}
    barcodes = list(barcode_meta.keys())

    try:
        fbs_stocks = await wb_client.get_fbs_stocks(token, barcodes)
    except Exception as e:
        logger.error("[cabinet %d] FBS stocks error: %s", cabinet_id, e)
        fbs_stocks = []

    stocks = fbo_stocks + fbs_stocks
    if not stocks:
        logger.info("[cabinet %d] stocks: empty (FBO=%d FBS=%d)", cabinet_id, len(fbo_stocks), len(fbs_stocks))
        return

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = await get_db()
    try:
        # Remove stale snapshot (older than 1h)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.execute("DELETE FROM stock_cache WHERE cabinet_id=? AND checked_at<?",
                         (cabinet_id, cutoff))

        for s in stocks:
            wh_type = s.get("warehouseType")  # "fbs" for marketplace-api rows

            if wh_type == "fbs":
                # marketplace-api FBS row: {barcode, amount, warehouseName, warehouseType}
                barcode = s.get("barcode")
                qty = s.get("amount", 0)
                wh_name = s.get("warehouseName") or "FBS"
                meta = barcode_meta.get(barcode, {})
                article = meta.get("article") or barcode
                nm_id = meta.get("nm_id")
                name = meta.get("subject") or "FBS товар"
                await db.execute("""
                    INSERT OR IGNORE INTO stock_cache
                        (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse,
                         barcode, warehouse_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fbs')
                """, (cabinet_id, checked_at, nm_id, article, name, qty, wh_name + " (FBS)", barcode))
                continue

            # warehouse_remains FBO row (groupByBarcode=true):
            # nmId, vendorCode (article), subjectName (name), barcode, techSize
            # warehouses: [{warehouseName, quantity, ...}]
            nm_id = s.get("nmId")
            article = s.get("vendorCode") or s.get("supplierArticle")
            name = s.get("subjectName") or s.get("subject") or s.get("category")
            barcode = s.get("barcode") or s.get("sku")

            warehouses = s.get("warehouses") or []
            if warehouses:
                for wh in warehouses:
                    wh_name = wh.get("warehouseName") or wh.get("name") or "Неизвестно"
                    # "Всего находится на складах" is a WB total row — skip to avoid double count
                    if wh_name.lower().startswith("всего"):
                        continue
                    qty = wh.get("quantity") or wh.get("remains") or 0
                    await db.execute("""
                        INSERT OR IGNORE INTO stock_cache
                            (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse,
                             barcode, warehouse_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fbo')
                    """, (cabinet_id, checked_at, nm_id, article, name, qty, wh_name, barcode))
            else:
                qty = s.get("quantity") or s.get("inWayToClient") or 0
                await db.execute("""
                    INSERT OR IGNORE INTO stock_cache
                        (cabinet_id, checked_at, nm_id, article, name, quantity, warehouse,
                         barcode, warehouse_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fbo')
                """, (cabinet_id, checked_at, nm_id, article, name, qty, "Общий FBO", barcode))

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
    date_from = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        weekly = await wb_client.get_finance_weekly(token, date_from, date_to)
    except Exception as e:
        logger.error("[cabinet %d] finance weekly fetch failed: %s", cabinet_id, e)
        return
    logger.info("[cabinet %d] finance weekly fetched: %d reports for %s..%s",
                cabinet_id, len(weekly), date_from, date_to)
    if not weekly:
        return

    db = await get_db()
    try:
        # Clear period before re-inserting
        await db.execute(
            "DELETE FROM finance_report WHERE cabinet_id=? AND date>=? AND date<=?",
            (cabinet_id, date_from, date_to)
        )
        # WB sometimes returns multiple records per week (different reportType).
        # Aggregate by dateFrom to avoid smaller records overwriting larger ones.
        from collections import defaultdict
        grouped: dict = defaultdict(lambda: {
            'date_to': '', 'revenue': 0.0, 'logistics': 0.0, 'penalty': 0.0,
            'storage': 0.0, 'returns': 0.0, 'other': 0.0, 'to_pay': 0.0
        })
        for row in weekly:
            date = (row.get("dateFrom") or "")[:10]
            if not date:
                continue
            g = grouped[date]
            g['date_to'] = (row.get("dateTo") or "")[:10]
            g['revenue']   += float(row.get("retailAmountSum") or 0)
            g['logistics'] += float(row.get("deliveryServiceSum") or 0)
            g['penalty']   += float(row.get("penaltySum") or 0)
            g['storage']   += float(row.get("paidStorageSum") or 0)
            g['returns']   += float(row.get("returnSum") or 0)
            # "other" = misc deductions NOT already counted as logistics/storage/penalty/commission.
            # deductionSum is total WB deductions and overlaps with commission, so don't sum it here.
            g['other']     += float(row.get("cashbackDiscountSum") or 0)
            g['other']     += float(row.get("paidAcceptanceSum") or 0)
            g['to_pay']    += float(row.get("forPaySum") or 0)

        for date, g in grouped.items():
            revenue   = g['revenue']
            logistics = g['logistics']
            penalty   = g['penalty']
            storage   = g['storage']
            returns   = g['returns']
            other     = g['other']
            to_pay    = g['to_pay']
            # Commission = gap between gross retail and net payout, minus other known costs.
            # Returns reduce revenue inside retailAmountSum already, so we don't subtract them.
            commission = max(0.0,
                revenue - to_pay - logistics - penalty - storage - other)
            await db.execute("""
                INSERT INTO finance_report
                    (cabinet_id, date, report_type, date_to, revenue, commission,
                     logistics, penalty, to_pay, storage, returns, other_deductions)
                VALUES (?, ?, 'weekly', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cabinet_id, date, report_type) DO UPDATE SET
                    date_to=excluded.date_to, revenue=excluded.revenue,
                    commission=excluded.commission, logistics=excluded.logistics,
                    penalty=excluded.penalty, to_pay=excluded.to_pay,
                    storage=excluded.storage, returns=excluded.returns,
                    other_deductions=excluded.other_deductions
            """, (cabinet_id, date, g['date_to'], revenue, commission,
                  logistics, penalty, to_pay, storage, returns, other))
        await db.commit()
        logger.info("[cabinet %d] finances synced: %d weekly reports", cabinet_id, len(weekly))
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
