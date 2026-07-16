from __future__ import annotations

import hmac
import secrets
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

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
    paid_credits: int = 0
    trial_credits: int = 0
    trial_credit_expires_at: str | None = None


class FreeUsageLimitError(ValueError):
    pass


def usage_period() -> str:
    return utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


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
                    CREATE TABLE IF NOT EXISTS email_bindings (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        code_hash TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS free_usage (
                        user_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        period TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(user_id, kind, period),
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS promo_credit_grants (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        reference TEXT NOT NULL UNIQUE,
                        initial_credits INTEGER NOT NULL,
                        remaining_credits INTEGER NOT NULL,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS credit_debits (
                        reference TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        paid_credits INTEGER NOT NULL,
                        promo_credits INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        refunded_at TEXT,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS credit_debit_grants (
                        reference TEXT NOT NULL,
                        grant_id TEXT NOT NULL,
                        credits INTEGER NOT NULL,
                        PRIMARY KEY(reference, grant_id),
                        FOREIGN KEY(reference) REFERENCES credit_debits(reference) ON DELETE CASCADE,
                        FOREIGN KEY(grant_id) REFERENCES promo_credit_grants(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS trial_network_grants (
                        user_id TEXT PRIMARY KEY,
                        network_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trial_network_grants ON trial_network_grants(network_hash, created_at)"
                )
                connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now().isoformat(),))
                connection.execute("DELETE FROM password_resets WHERE expires_at <= ?", (utc_now().isoformat(),))
                connection.execute("DELETE FROM email_bindings WHERE expires_at <= ?", (utc_now().isoformat(),))
            self._initialized = True

    @staticmethod
    def _trial_credit_summary(connection: sqlite3.Connection, user_id: str) -> tuple[int, str | None]:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(remaining_credits), 0), MIN(expires_at)
            FROM promo_credit_grants
            WHERE user_id=? AND remaining_credits > 0 AND expires_at > ?
            """,
            (user_id, utc_now().isoformat()),
        ).fetchone()
        return int(row[0]), row[1]

    def _user_from_row(self, connection: sqlite3.Connection, row: tuple[object, ...]) -> User:
        trial_credits, trial_expires_at = self._trial_credit_summary(connection, str(row[0]))
        paid_credits = int(row[4])
        return User(
            id=str(row[0]),
            username=str(row[1]),
            email=str(row[2]) if row[2] is not None else None,
            email_verified=bool(row[3]),
            credits=paid_credits + trial_credits,
            paid_credits=paid_credits,
            trial_credits=trial_credits,
            trial_credit_expires_at=trial_expires_at,
            created_at=str(row[5]),
        )

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

    def authenticate(self, account: str, password: str) -> User | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, username, email, email_verified, credits, created_at, password_hash FROM users
                WHERE email = ? COLLATE NOCASE
                    OR (email IS NULL AND username = ? COLLATE NOCASE)
                """,
                (account, account),
            ).fetchone()
        if row is None or not verify_password(password, row[6]):
            return None
        with closing(self._connect()) as connection:
            return self._user_from_row(connection, row[:6])

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

    def create_email_binding(self, user_id: str, email: str) -> str:
        """Issue a code for a legacy account that has no bound email address."""
        self.initialize()
        now = utc_now()
        normalized_email = email.strip().lower()
        code = f"{secrets.randbelow(1_000_000):06d}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT email FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                connection.rollback()
                raise ValueError("账号不存在")
            if user[0] is not None:
                connection.rollback()
                raise ValueError("该账号已绑定邮箱")
            in_use = connection.execute(
                "SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (normalized_email,)
            ).fetchone()
            if in_use:
                connection.rollback()
                raise ValueError("该邮箱已被注册")
            try:
                connection.execute(
                    """
                    INSERT INTO email_bindings(user_id, email, code_hash, expires_at, attempts, created_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        email=excluded.email, code_hash=excluded.code_hash, expires_at=excluded.expires_at,
                        attempts=0, created_at=excluded.created_at
                    """,
                    (
                        user_id,
                        normalized_email,
                        token_hash(code),
                        (now + timedelta(minutes=settings.password_reset_minutes)).isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError("该邮箱正在被其他账号绑定") from exc
            connection.commit()
        return code

    def confirm_email_binding(self, user_id: str, code: str) -> User:
        """Persist a verified email for a legacy account without granting new credits."""
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT email, code_hash, expires_at, attempts FROM email_bindings WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if (
                binding is None
                or binding[2] <= now
                or binding[3] >= 5
                or not hmac.compare_digest(binding[1], token_hash(code))
            ):
                if binding is not None and binding[2] > now:
                    connection.execute(
                        "UPDATE email_bindings SET attempts=attempts+1 WHERE user_id=?",
                        (user_id,),
                    )
                    connection.commit()
                else:
                    connection.rollback()
                raise ValueError("验证码无效或已过期")
            user = connection.execute(
                "SELECT email FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                connection.rollback()
                raise ValueError("账号不存在")
            if user[0] is not None:
                connection.rollback()
                raise ValueError("该账号已绑定邮箱")
            try:
                connection.execute(
                    "UPDATE users SET email=?, email_verified=1 WHERE id=?",
                    (binding[0], user_id),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError("该邮箱已被注册") from exc
            connection.execute("DELETE FROM email_bindings WHERE user_id=?", (user_id,))
            row = connection.execute(
                "SELECT id, username, email, email_verified, credits, created_at FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            connection.commit()
            return self._user_from_row(connection, row)

    def confirm_email_verification(
        self, user_id: str, code: str, network_hash: str | None = None
    ) -> User:
        self.initialize()
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT code_hash, expires_at, attempts FROM email_verifications WHERE user_id = ?", (user_id,)).fetchone()
            now_value = now.isoformat()
            if row is None or row[1] <= now_value or row[2] >= 5 or not hmac.compare_digest(row[0], token_hash(code)):
                if row is not None and row[1] > now_value:
                    connection.execute("UPDATE email_verifications SET attempts=attempts+1 WHERE user_id=?", (user_id,))
                    connection.commit()
                else: connection.rollback()
                raise ValueError("验证码无效或已过期")
            was_verified = connection.execute(
                "SELECT email_verified FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if was_verified is None:
                connection.rollback()
                raise ValueError("账号不存在")
            connection.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
            grant_trial = not was_verified[0]
            if grant_trial and network_hash:
                window_start = (now - timedelta(days=settings.trial_network_window_days)).isoformat()
                grants_in_window = connection.execute(
                    "SELECT COUNT(*) FROM trial_network_grants WHERE network_hash=? AND created_at >= ?",
                    (network_hash, window_start),
                ).fetchone()[0]
                if grants_in_window >= settings.trial_network_limit:
                    grant_trial = False
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO trial_network_grants(user_id, network_hash, created_at) VALUES (?, ?, ?)",
                        (user_id, network_hash, now_value),
                    )
            if grant_trial:
                reference = f"email-verified-trial:{user_id}"
                expires_at = now + timedelta(days=settings.trial_credit_days)
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO promo_credit_grants(
                        id, user_id, reference, initial_credits, remaining_credits, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        user_id,
                        reference,
                        settings.email_verification_trial_credits,
                        settings.email_verification_trial_credits,
                        expires_at.isoformat(),
                        now_value,
                    ),
                ).rowcount
                if inserted:
                    connection.execute(
                        """
                        INSERT INTO credit_ledger(user_id, delta, kind, reference, created_at)
                        VALUES (?, ?, 'email_verified_trial', ?, ?)
                        """,
                        (user_id, settings.email_verification_trial_credits, reference, now_value),
                    )
            connection.execute("DELETE FROM email_verifications WHERE user_id=?", (user_id,))
            row = connection.execute(
                "SELECT id, username, email, email_verified, credits, created_at FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            connection.commit()
            return self._user_from_row(connection, row)

    def consume_credits(self, user_id: str, amount: int, reference: str) -> int:
        self.initialize()
        if amount < 1:
            raise ValueError("扣减额度必须大于零")
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT email_verified, credits FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None or not row[0]:
                connection.rollback(); raise ValueError("请先验证邮箱后领取额度")
            grants = connection.execute(
                """
                SELECT id, remaining_credits FROM promo_credit_grants
                WHERE user_id=? AND remaining_credits > 0 AND expires_at > ?
                ORDER BY expires_at, created_at, id
                """,
                (user_id, now),
            ).fetchall()
            promo_available = sum(int(grant[1]) for grant in grants)
            paid_credits = int(row[1])
            if paid_credits + promo_available < amount:
                connection.rollback(); raise ValueError("额度不足，请充值后继续")
            remaining = amount
            grant_debits: list[tuple[str, int]] = []
            for grant_id, available in grants:
                consumed = min(remaining, int(available))
                if not consumed:
                    break
                connection.execute(
                    "UPDATE promo_credit_grants SET remaining_credits=remaining_credits-? WHERE id=?",
                    (consumed, grant_id),
                )
                grant_debits.append((str(grant_id), consumed))
                remaining -= consumed
            paid_debit = remaining
            if paid_debit:
                connection.execute("UPDATE users SET credits=credits-? WHERE id=?", (paid_debit, user_id))
            promo_debit = amount - paid_debit
            connection.execute(
                "INSERT INTO credit_ledger(user_id, delta, kind, reference, created_at) VALUES (?, ?, 'verification', ?, ?)",
                (user_id, -amount, reference, now),
            )
            connection.execute(
                """
                INSERT INTO credit_debits(reference, user_id, paid_credits, promo_credits, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (reference, user_id, paid_debit, promo_debit, now),
            )
            connection.executemany(
                "INSERT INTO credit_debit_grants(reference, grant_id, credits) VALUES (?, ?, ?)",
                [(reference, grant_id, credits) for grant_id, credits in grant_debits],
            )
            connection.commit()
            return paid_credits + promo_available - amount

    def refund_credits(self, user_id: str, amount: int, reference: str) -> None:
        """Refund a failed submission once, keyed by its original ledger reference."""
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            debit = connection.execute(
                """
                SELECT paid_credits, promo_credits, refunded_at
                FROM credit_debits WHERE reference=? AND user_id=?
                """,
                (reference, user_id),
            ).fetchone()
            charged = connection.execute(
                "SELECT 1 FROM credit_ledger WHERE user_id=? AND reference=? AND delta=?",
                (user_id, reference, -amount),
            ).fetchone()
            refund_reference = f"refund:{reference}"
            inserted = 0
            if charged:
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO credit_ledger(user_id, delta, kind, reference, created_at)
                    VALUES (?, ?, 'verification_refund', ?, ?)
                    """,
                    (user_id, amount, refund_reference, now),
                ).rowcount
            if inserted and debit and debit[2] is None:
                if debit[0]:
                    connection.execute(
                        "UPDATE users SET credits=credits+? WHERE id=?", (debit[0], user_id)
                    )
                grant_rows = connection.execute(
                    "SELECT grant_id, credits FROM credit_debit_grants WHERE reference=?", (reference,)
                ).fetchall()
                connection.executemany(
                    "UPDATE promo_credit_grants SET remaining_credits=remaining_credits+? WHERE id=?",
                    [(credits, grant_id) for grant_id, credits in grant_rows],
                )
                connection.execute(
                    "UPDATE credit_debits SET refunded_at=? WHERE reference=?", (now, reference)
                )
            elif inserted:
                connection.execute("UPDATE users SET credits=credits+? WHERE id=?", (amount, user_id))
            connection.commit()

    def reserve_free_usage(self, user_id: str, kind: str, limit: int) -> int:
        """Atomically reserve one daily free use and return the remaining allowance."""
        self.initialize()
        if limit < 1:
            raise FreeUsageLimitError("今日免费单邮箱验证次数已用完")
        now = utc_now()
        period = usage_period()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT email_verified FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None or not row[0]:
                connection.rollback()
                raise ValueError("请先验证注册邮箱")
            usage = connection.execute(
                "SELECT count FROM free_usage WHERE user_id=? AND kind=? AND period=?",
                (user_id, kind, period),
            ).fetchone()
            current = int(usage[0]) if usage else 0
            if current >= limit:
                connection.rollback()
                raise FreeUsageLimitError("今日免费单邮箱验证次数已用完")
            connection.execute(
                """
                INSERT INTO free_usage(user_id, kind, period, count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id, kind, period) DO UPDATE SET
                    count=free_usage.count+1, updated_at=excluded.updated_at
                """,
                (user_id, kind, period, now.isoformat()),
            )
            connection.commit()
        return limit - current - 1

    def release_free_usage(self, user_id: str, kind: str) -> None:
        """Release a reservation when the corresponding job could not be queued."""
        self.initialize()
        period = usage_period()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE free_usage SET count=MAX(count-1, 0), updated_at=?
                WHERE user_id=? AND kind=? AND period=?
                """,
                (utc_now().isoformat(), user_id, kind, period),
            )

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
                SELECT users.id, users.username, users.email, users.email_verified, users.credits, users.created_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash(token), utc_now().isoformat()),
            ).fetchone()
            return self._user_from_row(connection, row) if row else None

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def delete_user(self, user_id: str) -> list[str]:
        """Delete account-owned records and return result files that may be removed."""
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_jobs = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner_id=? AND status IN ('queued', 'running')",
                (user_id,),
            ).fetchone()[0]
            if active_jobs:
                connection.rollback()
                raise ValueError("仍有正在处理的任务，请完成后再删除账户")
            jobs = connection.execute(
                "SELECT id, csv_path FROM jobs WHERE owner_id=?", (user_id,)
            ).fetchall()
            job_ids = [str(job_id) for job_id, _ in jobs]
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                connection.execute(
                    f"DELETE FROM catch_all_emails WHERE job_id IN ({placeholders})", job_ids
                )
                connection.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders})", job_ids
                )
            debit_references = [
                str(row[0])
                for row in connection.execute(
                    "SELECT reference FROM credit_debits WHERE user_id=?", (user_id,)
                ).fetchall()
            ]
            if debit_references:
                placeholders = ", ".join("?" for _ in debit_references)
                connection.execute(
                    f"DELETE FROM credit_debit_grants WHERE reference IN ({placeholders})",
                    debit_references,
                )
            for table in (
                "sessions",
                "email_verifications",
                "email_bindings",
                "password_resets",
                "free_usage",
                "trial_network_grants",
                "promo_credit_grants",
                "credit_debits",
                "credit_ledger",
                "payment_orders",
            ):
                connection.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            deleted = connection.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
            if deleted != 1:
                connection.rollback()
                raise ValueError("账户不存在")
            connection.commit()
        return [str(csv_path) for _, csv_path in jobs if csv_path]


auth_store = AuthStore()
