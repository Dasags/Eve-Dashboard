import base64
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATABASE = DATA_DIR / "dashboard.sqlite3"


def _fernet() -> Fernet:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET muss für die Token-Verschlüsselung gesetzt sein.")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                main_character_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                token BLOB NOT NULL,
                scopes TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS characters_account_id ON characters(account_id);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS modules (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS account_modules (
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                module TEXT NOT NULL,
                PRIMARY KEY (account_id, module)
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location_id INTEGER NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('station', 'structure')),
                character_id INTEGER REFERENCES characters(id) ON DELETE SET NULL,
                region_id INTEGER,
                is_default INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS wallet_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                percentage REAL NOT NULL CHECK(percentage >= 0 AND percentage <= 100),
                position INTEGER NOT NULL DEFAULT 0,
                UNIQUE(character_id, name)
            );
            CREATE TABLE IF NOT EXISTS wallet_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('income', 'expense')),
                label_id INTEGER REFERENCES wallet_labels(id) ON DELETE SET NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                entry_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_plans (
                character_id INTEGER PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                start_balance REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                UNIQUE(account_id, name)
            );
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                type_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
                UNIQUE(watchlist_id, type_id)
            );
            CREATE TABLE IF NOT EXISTS watchlist_locations (
                watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                PRIMARY KEY(watchlist_id, location_id)
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                type_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                sampled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lowest_sell REAL,
                highest_buy REAL
            );
            """
        )
        db.executemany(
            "INSERT OR IGNORE INTO modules(name, enabled) VALUES (?, ?)",
            [
                ("dashboard", 1),
                ("skills", 0),
                ("wallet", 0),
                ("industry", 0),
                ("incursions", 0),
                ("marketwatcher", 0),
                ("planetary-industry", 0),
            ],
        )
        db.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            [
                ("motd", ""),
                ("map_height", "420"),
                ("map_width", "100"),
                ("killfeed_regions", ""),
                ("killfeed_count", "15"),
            ],
        )
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(locations)").fetchall()
        }
        if "region_id" not in columns:
            db.execute("ALTER TABLE locations ADD COLUMN region_id INTEGER")
        if "is_default" not in columns:
            db.execute("ALTER TABLE locations ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "INSERT OR IGNORE INTO locations(name, location_id, kind, region_id, is_default) "
            "VALUES ('Jita 4-4 (The Forge)', 60003760, 'station', 10000002, 1)"
        )


def get_setting(key: str, default: str = "") -> str:
    with connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def list_modules() -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT name, enabled FROM modules ORDER BY name").fetchall()
    return [{"name": row["name"], "enabled": bool(row["enabled"])} for row in rows]


def module_enabled(name: str) -> bool:
    with connection() as db:
        row = db.execute("SELECT enabled FROM modules WHERE name = ?", (name,)).fetchone()
    return bool(row and row["enabled"])


def account_module_names(account_id: int | None) -> set[str]:
    if account_id is None:
        return set()
    with connection() as db:
        rows = db.execute(
            "SELECT module FROM account_modules WHERE account_id = ?", (account_id,)
        ).fetchall()
    return {row["module"] for row in rows}


def module_allowed(account_id: int | None, name: str) -> bool:
    return module_enabled(name) or name in account_module_names(account_id)


def list_accounts_with_modules() -> list[dict]:
    with connection() as db:
        accounts = db.execute(
            "SELECT a.id, c.name FROM accounts a "
            "LEFT JOIN characters c ON c.id = a.main_character_id ORDER BY c.name"
        ).fetchall()
        grants = db.execute("SELECT account_id, module FROM account_modules").fetchall()
    granted: dict[int, set[str]] = {}
    for row in grants:
        granted.setdefault(row["account_id"], set()).add(row["module"])
    return [
        {"id": row["id"], "name": row["name"] or f"Account {row['id']}", "modules": granted.get(row["id"], set())}
        for row in accounts
    ]


def set_account_modules(account_id: int, modules: set[str]) -> bool:
    with connection() as db:
        if not db.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            return False
        db.execute("DELETE FROM account_modules WHERE account_id = ?", (account_id,))
        db.executemany(
            "INSERT INTO account_modules(account_id, module) VALUES (?, ?)",
            [(account_id, name) for name in sorted(modules)],
        )
    return True


def set_module(name: str, enabled: bool) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO modules(name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
            (name, int(enabled)),
        )


def save_character(
    character_id: int,
    name: str,
    token: dict,
    existing_account_id: int | None = None,
) -> int:
    encrypted = _fernet().encrypt(json.dumps(token).encode())
    expires_at = int(token.get("expires_at", 0))
    scopes = token.get("scope", "")
    if not scopes:
        scopes = ""
    if isinstance(scopes, list):
        scopes = " ".join(scopes)
    with connection() as db:
        prior = db.execute(
            "SELECT account_id FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if prior:
            if existing_account_id is not None and prior["account_id"] != existing_account_id:
                raise ValueError("Dieser Charakter gehört bereits zu einem anderen EVE-World-Konto.")
            account_id = prior["account_id"]
            db.execute(
                "UPDATE characters SET name = ?, token = ?, scopes = ?, expires_at = ? WHERE id = ?",
                (name, encrypted, scopes, expires_at, character_id),
            )
            return account_id

        if existing_account_id:
            account = db.execute(
                "SELECT id FROM accounts WHERE id = ?", (existing_account_id,)
            ).fetchone()
            if not account:
                raise ValueError("Das bestehende Konto wurde nicht gefunden.")
            account_id = existing_account_id
        else:
            db.execute("INSERT INTO accounts(main_character_id) VALUES (?)", (character_id,))
            account_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        db.execute(
            "INSERT INTO characters(id, account_id, name, token, scopes, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (character_id, account_id, name, encrypted, scopes, expires_at),
        )
        return account_id


def get_character(character_id: int) -> dict | None:
    with connection() as db:
        row = db.execute(
            "SELECT id, account_id, name, token, scopes, expires_at "
            "FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
    if not row:
        return None
    try:
        token = json.loads(_fernet().decrypt(row["token"]).decode())
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError("Der gespeicherte EVE-Token kann nicht entschlüsselt werden.") from exc
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "name": row["name"],
        "token": token,
        "scopes": row["scopes"].split(),
        "expires_at": row["expires_at"],
    }


def list_characters(account_id: int) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            "SELECT c.id, c.name, c.scopes, c.expires_at, a.main_character_id "
            "FROM characters c JOIN accounts a ON a.id = c.account_id "
            "WHERE c.account_id = ? ORDER BY (c.id = a.main_character_id) DESC, c.name",
            (account_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "is_main": row["id"] == row["main_character_id"],
            "scopes": row["scopes"].split(),
            "expires_at": row["expires_at"],
        }
        for row in rows
    ]


def character_belongs_to(character_id: int, account_id: int) -> bool:
    with connection() as db:
        row = db.execute(
            "SELECT 1 FROM characters WHERE id = ? AND account_id = ?",
            (character_id, account_id),
        ).fetchone()
    return row is not None


def remove_character(character_id: int, account_id: int) -> bool:
    with connection() as db:
        account = db.execute(
            "SELECT main_character_id FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        row = db.execute(
            "SELECT 1 FROM characters WHERE id = ? AND account_id = ?",
            (character_id, account_id),
        ).fetchone()
        if not account or not row or account["main_character_id"] == character_id:
            return False
        db.execute(
            "DELETE FROM characters WHERE id = ? AND account_id = ?",
            (character_id, account_id),
        )
    return True


def delete_account(account_id: int) -> bool:
    with connection() as db:
        cursor = db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return cursor.rowcount > 0


def list_locations() -> list[dict]:
    with connection() as db:
        rows = db.execute(
            "SELECT l.id, l.name, l.location_id, l.kind, l.character_id, l.region_id, "
            "l.is_default, c.name AS character_name "
            "FROM locations l LEFT JOIN characters c ON c.id = l.character_id ORDER BY l.name"
        ).fetchall()
    return [dict(row) for row in rows]


def get_location(location_pk: int) -> dict | None:
    with connection() as db:
        row = db.execute(
            "SELECT id, name, location_id, kind, character_id, region_id, is_default "
            "FROM locations WHERE id = ?",
            (location_pk,),
        ).fetchone()
    return dict(row) if row else None


def set_location_region(location_pk: int, region_id: int) -> None:
    with connection() as db:
        db.execute(
            "UPDATE locations SET region_id = ? WHERE id = ?",
            (region_id, location_pk),
        )


def add_location(name: str, location_id: int, kind: str, character_id: int | None) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO locations(name, location_id, kind, character_id) VALUES (?, ?, ?, ?)",
            (name, location_id, kind, character_id),
        )


def delete_location(location_pk: int) -> bool:
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM locations WHERE id = ? AND is_default = 0", (location_pk,)
        )
    return cursor.rowcount > 0


def create_watchlist(account_id: int, name: str) -> int:
    with connection() as db:
        db.execute(
            "INSERT INTO watchlists(account_id, name) VALUES (?, ?)",
            (account_id, name),
        )
        watchlist_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO watchlist_locations(watchlist_id, location_id) "
            "SELECT ?, id FROM locations WHERE is_default = 1",
            (watchlist_id,),
        )
        return watchlist_id


def list_watchlists(account_id: int) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            "SELECT id, name FROM watchlists WHERE account_id = ? ORDER BY id",
            (account_id,),
        ).fetchall()
    if not rows:
        create_watchlist(account_id, "Meine Liste")
        return list_watchlists(account_id)
    return [dict(row) for row in rows]


def get_watchlist(account_id: int, watchlist_id: int) -> dict | None:
    with connection() as db:
        row = db.execute(
            "SELECT id, name FROM watchlists WHERE id = ? AND account_id = ?",
            (watchlist_id, account_id),
        ).fetchone()
        if not row:
            return None
        items = db.execute(
            "SELECT id, type_id, name, quantity FROM watchlist_items "
            "WHERE watchlist_id = ? ORDER BY name",
            (watchlist_id,),
        ).fetchall()
        selected = db.execute(
            "SELECT location_id FROM watchlist_locations WHERE watchlist_id = ?",
            (watchlist_id,),
        ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "items": [dict(item) for item in items],
        "location_ids": [item["location_id"] for item in selected],
    }


def add_watchlist_item(watchlist_id: int, type_id: int, name: str, quantity: int) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO watchlist_items(watchlist_id, type_id, name, quantity) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(watchlist_id, type_id) DO UPDATE SET "
            "quantity=watchlist_items.quantity + excluded.quantity",
            (watchlist_id, type_id, name, quantity),
        )


def remove_watchlist_item(watchlist_id: int, item_id: int) -> bool:
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM watchlist_items WHERE id = ? AND watchlist_id = ?",
            (item_id, watchlist_id),
        )
    return cursor.rowcount > 0


def set_watchlist_locations(watchlist_id: int, location_ids: list[int]) -> None:
    with connection() as db:
        found = db.execute(
            f"SELECT COUNT(*) FROM locations WHERE id IN ({','.join('?' for _ in set(location_ids))})",
            tuple(set(location_ids)),
        ).fetchone()[0] if location_ids else 0
        if found != len(set(location_ids)):
            raise ValueError("Mindestens ein gewählter Standort wurde nicht gefunden.")
        db.execute("DELETE FROM watchlist_locations WHERE watchlist_id = ?", (watchlist_id,))
        db.executemany(
            "INSERT INTO watchlist_locations(watchlist_id, location_id) "
            "SELECT ?, id FROM locations WHERE id = ?",
            [(watchlist_id, location_id) for location_id in set(location_ids)],
        )


def delete_watchlist(account_id: int, watchlist_id: int) -> bool:
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM watchlists WHERE id = ? AND account_id = ?",
            (watchlist_id, account_id),
        )
    return cursor.rowcount > 0


def watchlist_prices(watchlist_id: int) -> list[dict]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT i.id AS item_id, i.name, i.type_id, i.quantity,
                   l.id AS location_pk, l.name AS location_name,
                   current.sampled_at, current.lowest_sell, current.highest_buy,
                   prior.lowest_sell AS prior_sell, prior.highest_buy AS prior_buy
            FROM watchlist_items i
            JOIN watchlist_locations wl ON wl.watchlist_id = i.watchlist_id
            JOIN locations l ON l.id = wl.location_id
            LEFT JOIN price_history current ON current.id = (
                SELECT p.id FROM price_history p
                WHERE p.watchlist_id = i.watchlist_id AND p.type_id = i.type_id
                  AND p.location_id = l.id
                ORDER BY p.sampled_at DESC, p.id DESC LIMIT 1
            )
            LEFT JOIN price_history prior ON prior.id = (
                SELECT p.id FROM price_history p
                WHERE p.watchlist_id = i.watchlist_id AND p.type_id = i.type_id
                  AND p.location_id = l.id
                  AND p.sampled_at <= datetime('now', '-24 hours')
                ORDER BY p.sampled_at DESC, p.id DESC LIMIT 1
            )
            WHERE i.watchlist_id = ? ORDER BY i.name, l.name
            """,
            (watchlist_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_prices(
    watchlist_id: int,
    type_id: int,
    location_pk: int,
    lowest_sell: float | None,
    highest_buy: float | None,
) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO price_history(watchlist_id, type_id, location_id, lowest_sell, highest_buy) "
            "VALUES (?, ?, ?, ?, ?)",
            (watchlist_id, type_id, location_pk, lowest_sell, highest_buy),
        )


def wallet_plan(account_id: int, character_id: int) -> dict:
    with connection() as db:
        plan = db.execute(
            "SELECT start_balance FROM wallet_plans WHERE account_id = ? AND character_id = ?",
            (account_id, character_id),
        ).fetchone()
        labels = db.execute(
            "SELECT id, name, percentage FROM wallet_labels "
            "WHERE account_id = ? AND character_id = ? ORDER BY position, id",
            (account_id, character_id),
        ).fetchall()
        entries = db.execute(
            "SELECT id, kind, label_id, description, amount, entry_date FROM wallet_entries "
            "WHERE account_id = ? AND character_id = ? ORDER BY entry_date DESC, id DESC",
            (account_id, character_id),
        ).fetchall()
    return {
        "start_balance": plan["start_balance"] if plan else 0,
        "labels": [dict(row) for row in labels],
        "entries": [dict(row) for row in entries],
    }


def save_wallet_start(account_id: int, character_id: int, amount: float) -> None:
    with connection() as db:
        db.execute(
            "INSERT INTO wallet_plans(character_id, account_id, start_balance) VALUES (?, ?, ?) "
            "ON CONFLICT(character_id) DO UPDATE SET account_id=excluded.account_id, "
            "start_balance=excluded.start_balance",
            (character_id, account_id, amount),
        )


def add_wallet_label(account_id: int, character_id: int, name: str, percentage: float) -> None:
    with connection() as db:
        position = db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM wallet_labels "
            "WHERE account_id = ? AND character_id = ?",
            (account_id, character_id),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO wallet_labels(account_id, character_id, name, percentage, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, character_id, name, percentage, position),
        )


def remove_wallet_label(account_id: int, character_id: int, label_id: int) -> bool:
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM wallet_labels WHERE id = ? AND account_id = ? AND character_id = ?",
            (label_id, account_id, character_id),
        )
    return cursor.rowcount > 0


def add_wallet_entry(
    account_id: int,
    character_id: int,
    kind: str,
    label_id: int | None,
    description: str,
    amount: float,
    entry_date: str,
) -> None:
    with connection() as db:
        if label_id is not None:
            label = db.execute(
                "SELECT 1 FROM wallet_labels WHERE id = ? AND account_id = ? AND character_id = ?",
                (label_id, account_id, character_id),
            ).fetchone()
            if not label:
                raise ValueError("Wallet-Beschriftung gehört nicht zu diesem Charakter.")
        db.execute(
            "INSERT INTO wallet_entries(account_id, character_id, kind, label_id, description, amount, entry_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, character_id, kind, label_id, description, amount, entry_date),
        )


def remove_wallet_entry(account_id: int, character_id: int, entry_id: int) -> bool:
    with connection() as db:
        cursor = db.execute(
            "DELETE FROM wallet_entries WHERE id = ? AND account_id = ? AND character_id = ?",
            (entry_id, account_id, character_id),
        )
    return cursor.rowcount > 0


initialize()
