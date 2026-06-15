import os
import aiosqlite
from config import DB_PATH


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                login         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wb_cabinets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                api_token  TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_permissions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                cabinet_id       INTEGER NOT NULL REFERENCES wb_cabinets(id) ON DELETE CASCADE,
                can_view_orders  INTEGER NOT NULL DEFAULT 0,
                can_view_stock   INTEGER NOT NULL DEFAULT 0,
                can_view_ads     INTEGER NOT NULL DEFAULT 0,
                can_view_finances INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, cabinet_id)
            );

            CREATE TABLE IF NOT EXISTS orders_cache (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_id  INTEGER NOT NULL REFERENCES wb_cabinets(id) ON DELETE CASCADE,
                order_id    TEXT NOT NULL,
                date        TEXT NOT NULL,
                article     TEXT,
                nm_id       INTEGER,
                status      TEXT,
                price       REAL DEFAULT 0,
                region      TEXT,
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(cabinet_id, order_id)
            );

            CREATE TABLE IF NOT EXISTS stock_cache (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_id  INTEGER NOT NULL REFERENCES wb_cabinets(id) ON DELETE CASCADE,
                checked_at  TEXT NOT NULL,
                nm_id       INTEGER NOT NULL,
                article     TEXT,
                name        TEXT,
                quantity    INTEGER NOT NULL DEFAULT 0,
                warehouse   TEXT,
                days_left   REAL
            );

            CREATE TABLE IF NOT EXISTS ad_stats (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_id    INTEGER NOT NULL REFERENCES wb_cabinets(id) ON DELETE CASCADE,
                date          TEXT NOT NULL,
                campaign_id   INTEGER NOT NULL,
                campaign_name TEXT,
                spend         REAL NOT NULL DEFAULT 0,
                views         INTEGER NOT NULL DEFAULT 0,
                clicks        INTEGER NOT NULL DEFAULT 0,
                orders        INTEGER NOT NULL DEFAULT 0,
                UNIQUE(cabinet_id, date, campaign_id)
            );

            CREATE TABLE IF NOT EXISTS finance_report (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_id  INTEGER NOT NULL REFERENCES wb_cabinets(id) ON DELETE CASCADE,
                date        TEXT NOT NULL,
                revenue     REAL NOT NULL DEFAULT 0,
                commission  REAL NOT NULL DEFAULT 0,
                logistics   REAL NOT NULL DEFAULT 0,
                penalty     REAL NOT NULL DEFAULT 0,
                to_pay      REAL NOT NULL DEFAULT 0,
                UNIQUE(cabinet_id, date)
            );
        """)
        # Migrations: add columns if missing (safe to re-run)
        for migration in [
            "ALTER TABLE orders_cache ADD COLUMN barcode TEXT",
            "ALTER TABLE orders_cache ADD COLUMN size TEXT",
            "ALTER TABLE orders_cache ADD COLUMN subject TEXT",
            "ALTER TABLE orders_cache ADD COLUMN discount_percent REAL DEFAULT 0",
        ]:
            try:
                await db.execute(migration)
            except Exception:
                pass  # column already exists
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn
