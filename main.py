import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import wb_client
from config import ADMIN_LOGIN, ADMIN_PASSWORD, ALLOWED_ORIGIN
from database import get_db, init_db
from scheduler import start_scheduler, sync_all_cabinets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _ensure_admin()
    start_scheduler()
    yield


app = FastAPI(title="WB Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Security headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return response


# ── Admin bootstrap ───────────────────────────────────────────────────────────
async def _ensure_admin() -> None:
    if not ADMIN_PASSWORD:
        logger.warning("ADMIN_PASSWORD not set — admin account not created")
        return
    db = await get_db()
    try:
        cur = await db.execute("SELECT id FROM users WHERE login=?", (ADMIN_LOGIN,))
        row = await cur.fetchone()
        hashed = auth.hash_password(ADMIN_PASSWORD)
        if not row:
            await db.execute(
                "INSERT INTO users (login, password_hash, is_admin) VALUES (?, ?, 1)",
                (ADMIN_LOGIN, hashed),
            )
            logger.info("Admin account created: %s", ADMIN_LOGIN)
        else:
            # Always sync password from env so .env.amvera changes take effect
            await db.execute(
                "UPDATE users SET password_hash=?, is_admin=1 WHERE login=?",
                (hashed, ADMIN_LOGIN),
            )
            logger.info("Admin password synced from env: %s", ADMIN_LOGIN)
        await db.commit()
    finally:
        await db.close()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    login: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)

class CreateUserRequest(BaseModel):
    login: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    is_admin: bool = False

class CreateCabinetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    api_token: str = Field(..., min_length=10, max_length=512)

class PermissionsRequest(BaseModel):
    cabinet_id: int
    can_view_orders: bool = False
    can_view_stock: bool = False
    can_view_ads: bool = False
    can_view_finances: bool = False

class SetAllPermissionsRequest(BaseModel):
    permissions: list[PermissionsRequest]


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request):
    client_ip = request.headers.get("X-Forwarded-For", request.client.host or "").split(",")[0].strip()
    auth.check_rate_limit(client_ip)

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, password_hash, is_admin, is_active FROM users WHERE login=?",
            (body.login,),
        )
        row = await cur.fetchone()
    finally:
        await db.close()

    if not row or not row["is_active"] or not auth.verify_password(body.password, row["password_hash"]):
        auth.record_failed_login(client_ip)
        logger.warning("Failed login for '%s' from %s", body.login, client_ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    auth.clear_failed_logins(client_ip)
    logger.info("Login success: user=%s ip=%s", body.login, client_ip)
    token = auth.create_token(row["id"], bool(row["is_admin"]))
    return {"token": token, "is_admin": bool(row["is_admin"])}


# ── Cabinets (user) ───────────────────────────────────────────────────────────

@app.get("/api/cabinets")
async def list_cabinets(user: dict = Depends(auth.get_current_user)):
    db = await get_db()
    try:
        if user["is_admin"]:
            cur = await db.execute("SELECT id, name, created_at FROM wb_cabinets ORDER BY id")
            rows = await cur.fetchall()
            return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                     "permissions": {"orders": True, "stock": True, "ads": True, "finances": True}}
                    for r in rows]
        else:
            cur = await db.execute("""
                SELECT c.id, c.name, c.created_at,
                       p.can_view_orders, p.can_view_stock, p.can_view_ads, p.can_view_finances
                FROM wb_cabinets c
                JOIN user_permissions p ON p.cabinet_id=c.id
                WHERE p.user_id=?
                ORDER BY c.id
            """, (user["id"],))
            rows = await cur.fetchall()
            return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                     "permissions": {
                         "orders": bool(r["can_view_orders"]),
                         "stock": bool(r["can_view_stock"]),
                         "ads": bool(r["can_view_ads"]),
                         "finances": bool(r["can_view_finances"]),
                     }} for r in rows]
    finally:
        await db.close()


async def _assert_cabinet_permission(user: dict, cabinet_id: int, perm: str) -> None:
    if user["is_admin"]:
        return
    col_map = {
        "orders": "can_view_orders",
        "stock": "can_view_stock",
        "ads": "can_view_ads",
        "finances": "can_view_finances",
    }
    col = col_map[perm]
    db = await get_db()
    try:
        cur = await db.execute(
            f"SELECT {col} FROM user_permissions WHERE user_id=? AND cabinet_id=?",
            (user["id"], cabinet_id),
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    if not row[col]:
        raise HTTPException(status_code=403, detail="No permission")


@app.get("/api/cabinets/{cabinet_id}/orders")
async def get_orders(cabinet_id: int, date_from: str = "", date_to: str = "",
                     user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "orders")
    db = await get_db()
    try:
        query = "SELECT order_id, date, article, nm_id, status, price, region FROM orders_cache WHERE cabinet_id=?"
        params: list = [cabinet_id]
        if date_from:
            query += " AND date>=?"
            params.append(date_from)
        if date_to:
            query += " AND date<=?"
            params.append(date_to)
        query += " ORDER BY date DESC"
        cur = await db.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.get("/api/cabinets/{cabinet_id}/stock")
async def get_stock(cabinet_id: int, user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "stock")
    db = await get_db()
    try:
        cur = await db.execute("""
            SELECT s.nm_id, s.article, s.name, s.warehouse,
                   SUM(s.quantity) as quantity,
                   MIN(s.checked_at) as checked_at
            FROM stock_cache s
            WHERE s.cabinet_id=?
              AND s.checked_at = (SELECT MAX(checked_at) FROM stock_cache WHERE cabinet_id=?)
            GROUP BY s.nm_id, s.article, s.name, s.warehouse
            ORDER BY quantity ASC
        """, (cabinet_id, cabinet_id))
        rows = await cur.fetchall()

        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        cur2 = await db.execute("""
            SELECT nm_id, COUNT(*) as cnt
            FROM orders_cache
            WHERE cabinet_id=? AND date>=?
            GROUP BY nm_id
        """, (cabinet_id, since))
        sales = {r["nm_id"]: r["cnt"] / 7 for r in await cur2.fetchall()}

        result = []
        for r in rows:
            qty = r["quantity"]
            avg = sales.get(r["nm_id"], 0)
            days_left = round(qty / avg, 1) if avg > 0 else None
            alert = "grey" if days_left is None else "red" if days_left < 7 else "yellow" if days_left < 14 else "green"
            result.append({**dict(r), "days_left": days_left, "alert": alert})
        return result
    finally:
        await db.close()


@app.get("/api/cabinets/{cabinet_id}/ads")
async def get_ads(cabinet_id: int, date_from: str = "", date_to: str = "",
                  user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "ads")
    db = await get_db()
    try:
        query = "SELECT date, campaign_id, campaign_name, spend, views, clicks, orders FROM ad_stats WHERE cabinet_id=?"
        params: list = [cabinet_id]
        if date_from:
            query += " AND date>=?"
            params.append(date_from)
        if date_to:
            query += " AND date<=?"
            params.append(date_to)
        query += " ORDER BY date DESC, spend DESC"
        cur = await db.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.get("/api/cabinets/{cabinet_id}/finances")
async def get_finances(cabinet_id: int, date_from: str = "", date_to: str = "",
                       user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "finances")
    db = await get_db()
    try:
        params: list = [cabinet_id]
        date_sql = ""
        if date_from:
            date_sql += " AND date>=?"
            params.append(date_from)
        if date_to:
            date_sql += " AND date<=?"
            params.append(date_to)
        # Filter by overlap: report period overlaps with [date_from, date_to]
        overlap_sql = ""
        overlap_params: list = [cabinet_id]
        if date_from:
            overlap_sql += " AND (date_to IS NULL OR date_to>=?)"
            overlap_params.append(date_from)
        if date_to:
            overlap_sql += " AND date<=?"
            overlap_params.append(date_to)
        cur = await db.execute(
            f"SELECT date, date_to, revenue, commission, logistics, penalty, to_pay "
            f"FROM finance_report WHERE cabinet_id=? AND report_type='weekly' {overlap_sql} ORDER BY date DESC",
            overlap_params
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# ── Analytics ────────────────────────────────────────────────────────────────

@app.get("/api/cabinets/{cabinet_id}/analytics")
async def get_analytics(cabinet_id: int, date_from: str = "", date_to: str = "",
                        user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "orders")
    db = await get_db()
    try:
        base_params: list = [cabinet_id]
        date_sql = ""
        if date_from:
            date_sql += " AND date>=?"
            base_params.append(date_from)
        if date_to:
            date_sql += " AND date<=?"
            base_params.append(date_to)

        cur = await db.execute(
            f"SELECT date, COUNT(*) as cnt, SUM(price) as revenue FROM orders_cache "
            f"WHERE cabinet_id=? {date_sql} GROUP BY date ORDER BY date",
            base_params
        )
        orders_by_day = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            f"SELECT article, COUNT(*) as cnt, SUM(price) as revenue FROM orders_cache "
            f"WHERE cabinet_id=? AND article IS NOT NULL {date_sql} "
            f"GROUP BY article ORDER BY cnt DESC LIMIT 10",
            base_params
        )
        top_articles = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            f"SELECT region, COUNT(*) as cnt FROM orders_cache "
            f"WHERE cabinet_id=? AND region IS NOT NULL {date_sql} "
            f"GROUP BY region ORDER BY cnt DESC LIMIT 10",
            base_params
        )
        regions = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            f"SELECT status, COUNT(*) as cnt FROM orders_cache "
            f"WHERE cabinet_id=? {date_sql} GROUP BY status ORDER BY cnt DESC",
            base_params
        )
        statuses = [dict(r) for r in await cur.fetchall()]

        return {
            "orders_by_day": orders_by_day,
            "top_articles": top_articles,
            "regions": regions,
            "statuses": statuses,
        }
    finally:
        await db.close()


# ── Products report ──────────────────────────────────────────────────────────

@app.get("/api/cabinets/{cabinet_id}/products-report")
async def get_products_report(cabinet_id: int, date_from: str = "", date_to: str = "",
                               user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "orders")
    db = await get_db()
    try:
        # Build date filter
        params: list = [cabinet_id]
        date_sql = ""
        if date_from:
            date_sql += " AND date>=?"
            params.append(date_from)
        if date_to:
            date_sql += " AND date<=?"
            params.append(date_to)

        # Daily sales per (article, nm_id, barcode, size)
        cur = await db.execute(f"""
            SELECT article, nm_id, barcode, size, subject,
                   date, COUNT(*) as cnt,
                   SUM(price) as revenue,
                   AVG(discount_percent) as avg_disc
            FROM orders_cache
            WHERE cabinet_id=? {date_sql}
            GROUP BY article, nm_id, barcode, size, date
            ORDER BY article, size, date
        """, params)
        rows = [dict(r) for r in await cur.fetchall()]

        # Latest stock per nm_id
        cur2 = await db.execute("""
            SELECT nm_id, SUM(quantity) as qty
            FROM stock_cache
            WHERE cabinet_id=?
              AND checked_at=(SELECT MAX(checked_at) FROM stock_cache WHERE cabinet_id=?)
            GROUP BY nm_id
        """, (cabinet_id, cabinet_id))
        stock_map = {r["nm_id"]: r["qty"] for r in await cur2.fetchall()}

        # Total ad spend for the period
        ad_params: list = [cabinet_id]
        ad_date_sql = ""
        if date_from:
            ad_date_sql += " AND date>=?"
            ad_params.append(date_from)
        if date_to:
            ad_date_sql += " AND date<=?"
            ad_params.append(date_to)
        cur3 = await db.execute(
            f"SELECT COALESCE(SUM(spend),0) as total FROM ad_stats WHERE cabinet_id=? {ad_date_sql}",
            ad_params
        )
        total_rk = (await cur3.fetchone())["total"] or 0.0

    finally:
        await db.close()

    # Group by (article, nm_id, barcode, size)
    from collections import defaultdict
    items_map: dict = {}
    all_dates: set = set()

    for r in rows:
        key = (r["article"] or "", r["nm_id"] or 0, r["barcode"] or "", r["size"] or "")
        if key not in items_map:
            items_map[key] = {
                "subject": r["subject"] or "",
                "article": r["article"] or "",
                "nm_id": r["nm_id"],
                "barcode": r["barcode"] or "",
                "size": r["size"] or "",
                "sales_by_date": {},
                "total_sales": 0,
                "total_revenue": 0.0,
                "avg_discount": 0.0,
                "_disc_samples": [],
            }
        item = items_map[key]
        d = r["date"]
        all_dates.add(d)
        item["sales_by_date"][d] = item["sales_by_date"].get(d, 0) + r["cnt"]
        item["total_sales"] += r["cnt"]
        item["total_revenue"] += r["revenue"] or 0
        if r["avg_disc"]:
            item["_disc_samples"].append(r["avg_disc"])

    dates_sorted = sorted(all_dates)
    sku_count = max(len(items_map), 1)
    rk_per_sku = round(total_rk / sku_count, 2)

    result_items = []
    for key, item in items_map.items():
        _, nm_id, _, _ = key
        item["stock"] = stock_map.get(nm_id, 0)
        item["rk_spend"] = rk_per_sku
        samples = item.pop("_disc_samples")
        item["avg_discount"] = round(sum(samples) / len(samples), 1) if samples else 0
        item["total_revenue"] = round(item["total_revenue"], 2)
        result_items.append(item)

    # Sort by subject, article, size
    result_items.sort(key=lambda x: (x["subject"], x["article"], x["size"]))

    return {"dates": dates_sorted, "items": result_items, "total_rk": round(total_rk, 2)}


# ── Admin — Users ─────────────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        cur = await db.execute("SELECT id, login, is_admin, is_active, created_at FROM users ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.post("/api/admin/users", status_code=201)
async def admin_create_user(body: CreateUserRequest, admin: dict = Depends(auth.require_admin)):
    hashed = auth.hash_password(body.password)
    db = await get_db()
    try:
        try:
            await db.execute(
                "INSERT INTO users (login, password_hash, is_admin) VALUES (?, ?, ?)",
                (body.login, hashed, int(body.is_admin)),
            )
            await db.commit()
        except Exception:
            raise HTTPException(status_code=409, detail="Login already exists")
        cur = await db.execute("SELECT id, login, is_admin, is_active FROM users WHERE login=?", (body.login,))
        return dict(await cur.fetchone())
    finally:
        await db.close()


@app.delete("/api/admin/users/{user_id}", status_code=204)
async def admin_delete_user(user_id: int, admin: dict = Depends(auth.require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db = await get_db()
    try:
        await db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await db.commit()
    finally:
        await db.close()


@app.patch("/api/admin/users/{user_id}/active")
async def admin_toggle_active(user_id: int, admin: dict = Depends(auth.require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    db = await get_db()
    try:
        await db.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (user_id,))
        await db.commit()
        cur = await db.execute("SELECT id, login, is_admin, is_active FROM users WHERE id=?", (user_id,))
        return dict(await cur.fetchone())
    finally:
        await db.close()


# ── Admin — Cabinets ──────────────────────────────────────────────────────────

@app.get("/api/admin/cabinets")
async def admin_list_cabinets(admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        cur = await db.execute("SELECT id, name, created_at FROM wb_cabinets ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.post("/api/admin/cabinets", status_code=201)
async def admin_create_cabinet(body: CreateCabinetRequest, admin: dict = Depends(auth.require_admin)):
    try:
        valid = await wb_client.validate_token(body.api_token)
    except Exception:
        valid = True
    if not valid:
        raise HTTPException(status_code=400, detail="WB token rejected (401/403)")

    db = await get_db()
    try:
        try:
            cur = await db.execute(
                "INSERT INTO wb_cabinets (name, api_token) VALUES (?, ?)",
                (body.name, body.api_token),
            )
            await db.commit()
            cabinet_id = cur.lastrowid
        except Exception:
            raise HTTPException(status_code=409, detail="Token already exists")
        cur2 = await db.execute("SELECT id, name, api_token FROM wb_cabinets WHERE id=?", (cabinet_id,))
        cabinet = dict(await cur2.fetchone())
    finally:
        await db.close()

    import asyncio
    asyncio.create_task(_sync_one_cabinet(cabinet))
    return {"id": cabinet_id, "name": body.name}


async def _sync_one_cabinet(cabinet: dict) -> None:
    from scheduler import _fetch_orders, _fetch_stocks, _fetch_ads, _fetch_finances
    for fn in (_fetch_orders, _fetch_stocks, _fetch_ads, _fetch_finances):
        try:
            await fn(cabinet)
        except Exception as e:
            logger.error("Sync error for cabinet %d: %s", cabinet["id"], e)


@app.delete("/api/admin/cabinets/{cabinet_id}", status_code=204)
async def admin_delete_cabinet(cabinet_id: int, admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        await db.execute("DELETE FROM wb_cabinets WHERE id=?", (cabinet_id,))
        await db.commit()
    finally:
        await db.close()


# ── Admin — Permissions ───────────────────────────────────────────────────────

@app.get("/api/admin/users/{user_id}/permissions")
async def admin_get_permissions(user_id: int, admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        cur = await db.execute("""
            SELECT p.cabinet_id, c.name as cabinet_name,
                   p.can_view_orders, p.can_view_stock, p.can_view_ads, p.can_view_finances
            FROM user_permissions p
            JOIN wb_cabinets c ON c.id=p.cabinet_id
            WHERE p.user_id=?
        """, (user_id,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.put("/api/admin/users/{user_id}/permissions")
async def admin_set_permissions(user_id: int, body: SetAllPermissionsRequest,
                                admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        await db.execute("DELETE FROM user_permissions WHERE user_id=?", (user_id,))
        for p in body.permissions:
            if not (p.can_view_orders or p.can_view_stock or p.can_view_ads or p.can_view_finances):
                continue
            await db.execute("""
                INSERT INTO user_permissions
                    (user_id, cabinet_id, can_view_orders, can_view_stock, can_view_ads, can_view_finances)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, p.cabinet_id,
                  int(p.can_view_orders), int(p.can_view_stock),
                  int(p.can_view_ads), int(p.can_view_finances)))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


# ── Admin — manual sync ───────────────────────────────────────────────────────

@app.post("/api/admin/sync")
async def admin_force_sync(admin: dict = Depends(auth.require_admin)):
    import asyncio
    asyncio.create_task(sync_all_cabinets())
    return {"ok": True, "message": "Sync started"}


@app.post("/api/admin/reset-finances")
async def admin_reset_finances(admin: dict = Depends(auth.require_admin)):
    """Clear all finance_report rows so next sync populates fresh data."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM finance_report")
        await db.commit()
    finally:
        await db.close()
    return {"ok": True, "message": "Finance data cleared"}


@app.get("/api/admin/debug/{cabinet_id}")
async def admin_debug(cabinet_id: int, section: str = "ads", st: int = 0, admin: dict = Depends(auth.require_admin), request: Request = None):
    """Debug: directly call WB API and return raw response for diagnosis."""
    db = await get_db()
    try:
        cur = await db.execute("SELECT api_token FROM wb_cabinets WHERE id=?", (cabinet_id,))
        row = await cur.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    token = row["api_token"]

    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        if section == "fbs_warehouses":
            data = await wb_client._get(token, wb_client._BASE_MARKETPLACE, "/api/v3/warehouses")
            return {"warehouses": data, "count": len(data) if isinstance(data, list) else 0}
        elif section == "fbs_stocks":
            # Test FBS stocks: get warehouses, then query stocks for barcodes from orders
            db2 = await get_db()
            try:
                cur = await db2.execute(
                    "SELECT DISTINCT barcode FROM orders_cache WHERE cabinet_id=? AND barcode IS NOT NULL LIMIT 20",
                    (cabinet_id,))
                barcodes = [r["barcode"] for r in await cur.fetchall()]
            finally:
                await db2.close()
            stocks = await wb_client.get_fbs_stocks(token, barcodes[:20])
            return {"barcodes_tested": len(barcodes), "stocks_found": len(stocks), "sample": stocks[:5]}
        elif section == "stocks_fbs":
            # Keep for backwards compat — now just alias fbs_stocks
            stocks = await wb_client.get_stocks(token)
            return {"fbo_count": len(stocks), "sample": stocks[:2]}
        elif section == "ads_count":
            data = await wb_client._get(token, wb_client._BASE_ADVERT, "/adv/v1/promotion/count")
            return {"raw": data}
        elif section == "ads_names":
            # Test what /api/advert/v2/adverts returns with real campaign IDs
            count = await wb_client._get(token, wb_client._BASE_ADVERT, "/adv/v1/promotion/count")
            ids = []
            for g in (count.get("adverts") or [] if isinstance(count, dict) else []):
                for a in (g.get("advert_list") or []):
                    if a.get("advertId"):
                        ids.append(a["advertId"])
            if not ids:
                return {"error": "no ids"}
            data = await wb_client._get(token, wb_client._BASE_ADVERT, "/api/advert/v2/adverts", {
                "ids": ",".join(str(x) for x in ids[:10]),
            })
            return {"ids_tested": ids[:10], "raw": data, "raw_type": type(data).__name__}
        elif section == "ads_stats":
            # Use real campaign IDs from count endpoint
            count = await wb_client._get(token, wb_client._BASE_ADVERT, "/adv/v1/promotion/count")
            ids = []
            for g in (count.get("adverts") or [] if isinstance(count, dict) else []):
                if g.get("status") in (7, 9, 11):
                    for a in (g.get("advert_list") or []):
                        if a.get("advertId"):
                            ids.append(a["advertId"])
            if not ids:
                return {"error": "no campaign ids found", "count_raw": count}
            data = await wb_client._get(token, wb_client._BASE_ADVERT, "/adv/v3/fullstats", {
                "ids": ",".join(str(x) for x in ids[:5]),
                "beginDate": week_ago, "endDate": today
            })
            return {"ids_tested": ids[:5], "raw": data}
        elif section == "stocks":
            # Full flow: create → poll → download
            import asyncio
            create_resp = await wb_client._get(token, wb_client._BASE_SELLER_ANALYTICS,
                                               "/api/v1/warehouse_remains",
                                               {"groupBySize": "true", "groupByBarcode": "true"})
            task_id = (create_resp.get("data", {}).get("taskId") if isinstance(create_resp, dict) else None)
            if not task_id:
                return {"step": "create_task", "raw": create_resp, "error": "no taskId in data"}
            status = None
            for _ in range(12):
                await asyncio.sleep(5)
                sr = await wb_client._get(token, wb_client._BASE_SELLER_ANALYTICS,
                                          f"/api/v1/warehouse_remains/tasks/{task_id}/status", {})
                status = sr.get("data", {}).get("status") if isinstance(sr, dict) else None
                if status in ("done", "complete", "completed", "success"):
                    break
            if status not in ("done", "complete", "completed", "success"):
                return {"step": "poll_timeout", "task_id": task_id, "last_status": status}
            dl = await wb_client._get(token, wb_client._BASE_SELLER_ANALYTICS,
                                      f"/api/v1/warehouse_remains/tasks/{task_id}/download", {})
            items = dl if isinstance(dl, list) else []
            return {
                "step": "download", "task_id": task_id, "status": status,
                "total_items": len(items),
                "sample": items[:2] if items else [],
                "raw_type": type(dl).__name__,
                "raw_if_not_list": dl if not isinstance(dl, list) else None,
            }
        elif section == "finance":
            ninety_ago = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
            data = await wb_client._post(token, wb_client._BASE_FINANCE, "/api/finance/v1/sales-reports/list", {
                "dateFrom": ninety_ago, "dateTo": today
            })
            rows = data if isinstance(data, list) else []
            return {
                "count": len(rows),
                "date_from": ninety_ago,
                "date_to": today,
                "raw_sample": rows[:5],
                "all_keys": list(rows[0].keys()) if rows else [],
                "all_rows_summary": [
                    {
                        "dateFrom": r.get("dateFrom"),
                        "dateTo": r.get("dateTo"),
                        "retailAmountSum": r.get("retailAmountSum"),
                        "forPaySum": r.get("forPaySum"),
                        "deliveryServiceSum": r.get("deliveryServiceSum"),
                        "penaltySum": r.get("penaltySum"),
                    }
                    for r in rows
                ],
            }
        elif section == "orders":
            data = await wb_client._get(token, wb_client._BASE_STATISTICS, "/api/v1/supplier/orders", {
                "dateFrom": week_ago, "flag": 0
            })
            items = data if isinstance(data, list) else []
            return {"count": len(items), "first": items[:2] if items else []}
    except wb_client.WBApiError as e:
        return {"error": str(e), "status": e.status}
    except Exception as e:
        return {"error": str(e)}


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    return FileResponse("static/index.html")
