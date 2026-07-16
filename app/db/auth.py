from __future__ import annotations

import hmac
import secrets
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta

from app.config import settings
from app.core.security import hash_password, new_token, token_hash, verify_password
from app.db.jobs import utc_now


@dataclass(frozen=True)
class User:
    id: str
    username: str
    email: str | None
    created_at: str
    email_verified: bool = False
    credits: int = 0


class AuthStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(settings.database_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            settings.database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        email TEXT UNIQUE COLLATE NOCASE,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        credits INTEGER NOT NULL DEFAULT 0,
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
                if "email" not in columns:
                    connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
                if "email_verified" not in columns:
                    connection.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
                if "credits" not in columns:
                    connection.execute("ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS email_verifications (
                        user_id TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS credit_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, delta INTEGER NOT NULL,
                        kind TEXT NOT NULL, reference TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payment_orders (
                        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, credits INTEGER NOT NULL,
                        amount_fen INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, paid_at TEXT
                    )
                    """
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE) WHERE email IS NOT NULL"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute("CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id)")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS password_resets (
                        user_id TEXT PRIMARY KEY,
                        code_hash TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now().isoformat(),))
                connection.execute("DELETE FROM password_resets WHERE expires_at <= ?", (utc_now().isoformat(),))
            self._initialized = True

    def create_user(self, email: str, password: str) -> User:
        self.initialize()
        now = utc_now().isoformat()
        normalized_email = email.lower()
        user = User(id=uuid.uuid4().hex, username=normalized_email, email=normalized_email, created_at=now)
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO users (id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user.id, user.username, user.email, hash_password(password), user.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("账号或邮箱已被注册") from exc
        return user

    def authenticate(self, email: str, password: str) -> User | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, username, email, email_verified, credits, password_hash, created_at FROM users
                WHERE email = ? COLLATE NOCASE
                """,
                (email,),
            ).fetchone()
        if row is None or not verify_password(password, row[5]):
            return None
        return User(id=row[0], username=row[1], email=row[2], email_verified=bool(row[3]), credits=row[4], created_at=row[6])

    def create_password_reset(self, email: str) -> str | None:
        self.initialize()
        now = utc_now()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if row is None:
                return None
            code = f"{secrets.randbelow(1_000_000):06d}"
            expires_at = now + timedelta(minutes=settings.password_reset_minutes)
            connection.execute(
                """
                INSERT INTO password_resets(user_id, code_hash, expires_at, attempts, created_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,
                    expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at
                """,
                (row[0], token_hash(code), expires_at.isoformat(), now.isoformat()),
            )
        return code

    def create_email_verification(self, user_id: str) -> str:
        self.initialize()
        now = utc_now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO email_verifications(user_id, code_hash, expires_at, attempts, created_at)
                VALUES (?, ?, ?, 0, ?) ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,
                expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at""",
                (user_id, token_hash(code), (now + timedelta(minutes=settings.password_reset_minutes)).isoformat(), now.isoformat()),
            )
        return code

    def confirm_email_verification(self, user_id: str, code: str) -> User:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT code_hash, expires_at, attempts FROM email_verifications WHERE user_id = ?", (user_id,)).fetchone()
            if row is None or row[1] <= now or row[2] >= 5 or not hmac.compare_digest(row[0], token_hash(code)):
                if row is not None and row[1] > now:
                    connection.execute("UPDATE email_verifications SET attempts=attempts+1 WHERE user_id=?", (user_id,))
                    connection.commit()
                else: connection.rollback()
                raise ValueError("验证码无效或已过期")
            connection.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
            inserted = connection.execute("INSERT OR IGNORE INTO credit_ledger(user_id, delta, kind, reference, created_at) VALUES (?, 100, 'email_verified', ?, ?)", (user_id, f"email-verified:{user_id}", now)).rowcount
            if inserted:
                connection.execute("UPDATE users SET credits=credits+100 WHERE id=?", (user_id,))
            connection.execute("DELETE FROM email_verifications WHERE user_id=?", (user_id,))
            row = connection.execute("SELECT id, username, email, created_at, email_verified, credits FROM users WHERE id=?", (user_id,)).fetchone()
            connection.commit()
        return User(id=row[0], username=row[1], email=row[2], created_at=row[3], email_verified=bool(row[4]), credits=row[5])

    def consume_credits(self, user_id: str, amount: int, reference: str) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT email_verified, credits FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None or not row[0]:
                connection.rollback(); raise ValueError("请先验证邮箱后领取额度")
            if row[1] < amount:
                connection.rollback(); raise ValueError("额度不足，请充值后继续")
            connection.execute("INSERT INTO credit_ledger(user_id, delta, kind, reference, created_at) VALUES (?, ?, 'verification', ?, ?)", (user_id, -amount, reference, utc_now().isoformat()))
            connection.execute("UPDATE users SET credits=credits-? WHERE id=?", (amount, user_id))
            connection.commit()
            return row[1] - amount

    def create_payment_order(self, user_id: str, packages: int) -> dict[str, int | str]:
        if packages < 1 or packages > 1000: raise ValueError("充值数量无效")
        order_id = uuid.uuid4().hex
        credits, amount_fen = packages * 100, packages * 50
        with closing(self._connect()) as connection:
            connection.execute("INSERT INTO payment_orders(id,user_id,credits,amount_fen,status,created_at) VALUES (?,?,?,?, 'pending', ?)", (order_id,user_id,credits,amount_fen,utc_now().isoformat()))
        return {"id": order_id, "credits": credits, "amount_fen": amount_fen, "status": "pending"}

    def reset_password(self, email: str, code: str, password: str) -> None:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT users.id, password_resets.code_hash, password_resets.expires_at, password_resets.attempts
                FROM users JOIN password_resets ON password_resets.user_id = users.id
                WHERE users.email = ? COLLATE NOCASE
                """,
                (email,),
            ).fetchone()
            if row is None or row[2] <= now or row[3] >= 5:
                connection.rollback()
                raise ValueError("验证码无效或已过期")
            if not hmac.compare_digest(row[1], token_hash(code)):
                connection.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE user_id = ?", (row[0],))
                connection.commit()
                raise ValueError("验证码无效或已过期")
            connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row[0]))
            connection.execute("DELETE FROM password_resets WHERE user_id = ?", (row[0],))
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (row[0],))
            connection.commit()

    def create_session(self, user_id: str) -> str:
        self.initialize()
        token = new_token()
        created_at = utc_now()
        expires_at = created_at + timedelta(days=settings.session_ttl_days)
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash(token), user_id, created_at.isoformat(), expires_at.isoformat()),
            )
        return token

    def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, users.email, users.created_at, users.email_verified, users.credits
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash(token), utc_now().isoformat()),
            ).fetchone()
        return User(id=row[0], username=row[1], email=row[2], created_at=row[3], email_verified=bool(row[4]), credits=row[5]) if row else None

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


auth_store = AuthStore()
