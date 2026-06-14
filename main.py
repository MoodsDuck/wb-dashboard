import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import wb_client
from config import ADMIN_LOGIN, ADMIN_PASSWORD
from database import get_db, init_db
from scheduler import start_scheduler, sync_all_cabinets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _ensure_admin()
    start_scheduler()
    yield


app = FastAPI(title="WB Dashboard", lifespan=lifespan)


async def _ensure_admin() -> None:
    if not ADMIN_PASSWORD:
        logging.warning("ADMIN_PASSWORD not set — admin account not created")
        return
    db = await get_db()
    try:
        cur = await db.execute("SELECT id FROM users WHERE login=?", (ADMIN_LOGIN,))
        row = await cur.fetchone()
        if not row:
            hashed = auth.hash_password(ADMIN_PASSWORD)
            await db.execute(
                "INSERT INTO users (login, password_hash, is_admin) VALUES (?, ?, 1)",
                (ADMIN_LOGIN, hashed),
            )
            await db.commit()
            logging.info("Admin account created: %s", ADMIN_LOGIN)
    finally:
        await db.close()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    login: str
    password: str

class CreateUserRequest(BaseModel):
    login: str
    password: str
    is_admin: bool = False

class CreateCabinetRequest(BaseModel):
    name: str
    api_token: str

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
async def login(body: LoginRequest):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, password_hash, is_admin, is_active FROM users WHERE login=?",
            (body.login,),
        )
        row = await cur.fetchone()
    finally:
        await db.close()

    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not auth.verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

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
    """Raises 403/404 if user has no access to cabinet or lacks the permission."""
    if user["is_admin"]:
        return
    db = await get_db()
    try:
        col = {"orders": "can_view_orders", "stock": "can_view_stock",
               "ads": "can_view_ads", "finances": "can_view_finances"}[perm]
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
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@app.get("/api/cabinets/{cabinet_id}/stock")
async def get_stock(cabinet_id: int, user: dict = Depends(auth.get_current_user)):
    await _assert_cabinet_permission(user, cabinet_id, "stock")
    db = await get_db()
    try:
        # Latest snapshot
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

        # Calculate avg daily sales (last 7 days)
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
            if days_left is None:
                alert = "grey"
            elif days_left < 7:
                alert = "red"
            elif days_left < 14:
                alert = "yellow"
            else:
                alert = "green"
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
        query = """
            SELECT date, campaign_id, campaign_name, spend, views, clicks, orders
            FROM ad_stats WHERE cabinet_id=?
        """
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
        query = """
            SELECT date, revenue, commission, logistics, penalty, to_pay
            FROM finance_report WHERE cabinet_id=?
        """
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


# ── Admin — Users ─────────────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(admin: dict = Depends(auth.require_admin)):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, login, is_admin, is_active, created_at FROM users ORDER BY id"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@app.post("/api/admin/users", status_code=201)
async def admin_create_user(body: CreateUserRequest, admin: dict = Depends(auth.require_admin)):
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password too short (min 6)")
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
        await db.execute(
            "UPDATE users SET is_active = 1 - is_active WHERE id=?", (user_id,)
        )
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
    valid = await wb_client.validate_token(body.api_token)
    if not valid:
        raise HTTPException(status_code=400, detail="WB token is invalid or has no permissions")
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
        # Trigger immediate sync for new cabinet
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
            logging.error("Sync error for cabinet %d: %s", cabinet["id"], e)


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
                continue  # skip if no permissions granted
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


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    return FileResponse("static/index.html")
