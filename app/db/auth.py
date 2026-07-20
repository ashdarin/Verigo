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


@dataclass(frozen=True)
class AdminCreditAdjustment:
    user: User
    delta: int
    reference: str
    created_at: str


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

    @staticmethod
    def _insert_notification(
        connection: sqlite3.Connection,
        user_id: str,
        kind: str,
        title: str,
        body: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO notifications(id, user_id, kind, title, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, user_id, kind, title, body, created_at),
        )

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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS admin_credit_grants (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        granted_by_user_id TEXT NOT NULL,
                        credits INTEGER NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        reference TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(granted_by_user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_admin_credit_grants_user ON admin_credit_grants(user_id, created_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS admin_credit_adjustments (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        adjusted_by_user_id TEXT NOT NULL,
                        delta INTEGER NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        reference TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(adjusted_by_user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_admin_credit_adjustments_user ON admin_credit_adjustments(user_id, created_at DESC)"
                )
                adjustment_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(admin_credit_adjustments)")
                }
                if "amount_fen" not in adjustment_columns:
                    connection.execute("ALTER TABLE admin_credit_adjustments ADD COLUMN amount_fen INTEGER")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        read_at TEXT,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at, created_at DESC)"
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
            row = connection.execute(
                "SELECT email_verified, credits, email FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None or not row[0]:
                connection.rollback(); raise ValueError("请先验证邮箱后领取额度")
            if row[2] and str(row[2]).lower() in settings.admin_emails:
                connection.rollback()
                return int(row[1])
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

    def adjust_paid_credits(
        self, email: str, delta: int, adjusted_by_user_id: str, note: str = "",
        amount_fen: int | None = None,
    ) -> AdminCreditAdjustment:
        """Adjust paid credits atomically with an audit record and user notification."""
        self.initialize()
        normalized_email = email.strip().lower()
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("请输入有效的注册邮箱")
        if not delta:
            raise ValueError("额度调整不能为零")
        if amount_fen is not None and amount_fen < 0:
            raise ValueError("实收金额不能为负数")
        now = utc_now().isoformat()
        adjustment_id = uuid.uuid4().hex
        reference = f"admin_adjustment:{adjustment_id}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, username, email, email_verified, credits, created_at
                FROM users WHERE email=? COLLATE NOCASE
                """,
                (normalized_email,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("未找到该注册邮箱对应的账户")
            paid_credits = int(row[4])
            if delta < 0 and paid_credits < -delta:
                connection.rollback()
                raise ValueError("该账户的可退付费额度不足")
            connection.execute(
                "UPDATE users SET credits=credits+? WHERE id=?", (delta, row[0])
            )
            connection.execute(
                """
                INSERT INTO credit_ledger(user_id, delta, kind, reference, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    delta,
                    "admin_credit_grant" if delta > 0 else "admin_credit_deduction",
                    reference,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO admin_credit_adjustments(
                    id, user_id, adjusted_by_user_id, delta, note, reference, created_at, amount_fen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (adjustment_id, row[0], adjusted_by_user_id, delta, note.strip(), reference, now, amount_fen),
            )
            amount_text = f"{abs(delta):,}"
            self._insert_notification(
                connection,
                str(row[0]),
                "credit_grant" if delta > 0 else "credit_deduction",
                "额度已到账" if delta > 0 else "额度已调整",
                (
                    f"管理员已向你的账户增加 {amount_text} 额度。"
                    if delta > 0
                    else f"管理员已从你的账户扣减 {amount_text} 额度。"
                ),
                now,
            )
            updated_row = connection.execute(
                """
                SELECT id, username, email, email_verified, credits, created_at
                FROM users WHERE id=?
                """,
                (row[0],),
            ).fetchone()
            user = self._user_from_row(connection, updated_row)
            connection.commit()
        return AdminCreditAdjustment(
            user=user, delta=delta, reference=reference, created_at=now
        )

    def wallet_snapshot(self, user_id: str) -> dict[str, object]:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, username, email, email_verified, credits, created_at FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None:
                raise ValueError("账户不存在")
            user = self._user_from_row(connection, row)
            usage_rows = connection.execute(
                """SELECT substr(created_at, 1, 10), COALESCE(SUM(-delta), 0)
                   FROM credit_ledger WHERE user_id=? AND kind='verification'
                   GROUP BY substr(created_at, 1, 10) ORDER BY 1 DESC LIMIT 14""", (user_id,)
            ).fetchall()
            usage_total = int(connection.execute(
                "SELECT COALESCE(SUM(-delta), 0) FROM credit_ledger WHERE user_id=? AND kind='verification'", (user_id,)
            ).fetchone()[0])
            adjustments = connection.execute(
                """SELECT delta, amount_fen, note, created_at FROM admin_credit_adjustments
                   WHERE user_id=? ORDER BY created_at DESC LIMIT 20""", (user_id,)
            ).fetchall()
            paid_orders = connection.execute(
                """SELECT credits, amount_fen, paid_at FROM payment_orders
                   WHERE user_id=? AND status='paid' ORDER BY paid_at DESC LIMIT 20""", (user_id,)
            ).fetchall()
            paid_used = int(connection.execute(
                "SELECT COALESCE(SUM(paid_credits), 0) FROM credit_debits WHERE user_id=?", (user_id,)
            ).fetchone()[0])
        transactions = [
            {"kind": "payment", "title": "充值到账", "credits": int(r[0]), "amount_fen": int(r[1]), "note": "", "created_at": str(r[2])}
            for r in paid_orders
        ] + [
            {"kind": "adjustment", "title": "管理员授予" if int(r[0]) > 0 else "管理员扣减", "credits": int(r[0]), "amount_fen": int(r[1]) if r[1] is not None else None, "note": str(r[2]), "created_at": str(r[3])}
            for r in adjustments
        ]
        transactions.sort(key=lambda item: str(item["created_at"]), reverse=True)
        paid_adjustments_fen = sum(
            (int(row[1]) if row[1] is not None else 0)
            for row in adjustments if int(row[0]) > 0
        )
        refund_adjustments_fen = sum(
            (int(row[1]) if row[1] is not None else 0)
            for row in adjustments if int(row[0]) < 0
        )
        paid_order_fen = sum(int(row[1]) for row in paid_orders)
        price_fen_per_100 = settings.verification_price_fen_per_100
        return {
            "price_fen_per_100": price_fen_per_100,
            "available_verifications": user.credits,
            "paid_verifications": user.paid_credits,
            "trial_verifications": user.trial_credits,
            "trial_expires_at": user.trial_credit_expires_at,
            "verifications_used": usage_total,
            "paid_verifications_used": paid_used,
            "cumulative_recharge_fen": paid_order_fen + paid_adjustments_fen,
            "cumulative_refund_fen": refund_adjustments_fen,
            "paid_used_value_yuan": round(paid_used * price_fen_per_100 / 10_000, 2),
            "remaining_paid_value_yuan": round(user.paid_credits * price_fen_per_100 / 10_000, 2),
            "usage_daily": [{"day": str(day), "verifications": int(total)} for day, total in reversed(usage_rows)],
            "transactions": transactions[:20],
        }

    def admin_account_snapshot(self, email: str) -> dict[str, object]:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id, username, email, email_verified, credits, created_at FROM users WHERE email=? COLLATE NOCASE", (email.strip().lower(),)
            ).fetchone()
            if row is None:
                raise ValueError("未找到该注册邮箱对应的账户")
            user = self._user_from_row(connection, row)
            usage = int(connection.execute("SELECT COALESCE(SUM(-delta), 0) FROM credit_ledger WHERE user_id=? AND kind='verification'", (user.id,)).fetchone()[0])
            jobs = connection.execute("SELECT status, COUNT(*) FROM jobs WHERE owner_id=? GROUP BY status", (user.id,)).fetchall()
            adjustments = connection.execute("SELECT delta, amount_fen, note, created_at FROM admin_credit_adjustments WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user.id,)).fetchall()
        return {"email": user.email, "email_verified": user.email_verified, "available_verifications": user.credits, "paid_verifications": user.paid_credits, "trial_verifications": user.trial_credits, "verifications_used": usage, "jobs": {str(status): int(count) for status, count in jobs}, "adjustments": [{"delta": int(delta), "amount_fen": int(amount) if amount is not None else None, "note": str(note), "created_at": str(created)} for delta, amount, note, created in adjustments]}

    def list_admin_accounts(self, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, object]], int]:
        self.initialize()
        with closing(self._connect()) as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM users WHERE email IS NOT NULL").fetchone()[0])
            rows = connection.execute("""SELECT u.id,u.email,u.email_verified,u.credits,u.created_at,COALESCE((SELECT SUM(remaining_credits) FROM promo_credit_grants p WHERE p.user_id=u.id AND p.remaining_credits>0 AND p.expires_at>?),0),COALESCE((SELECT SUM(-delta) FROM credit_ledger l WHERE l.user_id=u.id AND l.kind='verification'),0) FROM users u WHERE u.email IS NOT NULL ORDER BY u.created_at DESC LIMIT ? OFFSET ?""", (utc_now().isoformat(), limit, offset)).fetchall()
        return ([{"email":str(r[1]),"email_verified":bool(r[2]),"paid_verifications":int(r[3]),"trial_verifications":int(r[5]),"used_verifications":int(r[6]),"created_at":str(r[4])} for r in rows], total)

    def create_notification(self, user_id: str, kind: str, title: str, body: str) -> None:
        """Record a user-facing event for payment and other future workflows."""
        self.initialize()
        with closing(self._connect()) as connection:
            self._insert_notification(
                connection, user_id, kind, title, body, utc_now().isoformat()
            )

    def list_notifications(self, user_id: str, limit: int = 30) -> tuple[list[dict[str, str | None]], int]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, kind, title, body, created_at, read_at
                FROM notifications WHERE user_id=?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            unread = connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                (user_id,),
            ).fetchone()[0]
        return [
            {
                "id": str(row[0]), "kind": str(row[1]), "title": str(row[2]),
                "body": str(row[3]), "created_at": str(row[4]),
                "read_at": str(row[5]) if row[5] else None,
            }
            for row in rows
        ], int(unread)

    def mark_notifications_read(self, user_id: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                (utc_now().isoformat(), user_id),
            )

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

    def change_password(
        self, user_id: str, current_password: str, new_password: str, current_session: str | None
    ) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None or not verify_password(current_password, row[0]):
                connection.rollback()
                raise ValueError("原密码不正确")
            connection.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(new_password), user_id),
            )
            if current_session:
                connection.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                    (user_id, token_hash(current_session)),
                )
            else:
                connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
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
            connection.execute(
                "DELETE FROM admin_credit_grants WHERE user_id=? OR granted_by_user_id=?",
                (user_id, user_id),
            )
            connection.execute(
                "DELETE FROM admin_credit_adjustments WHERE user_id=? OR adjusted_by_user_id=?",
                (user_id, user_id),
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
                "notifications",
            ):
                connection.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            deleted = connection.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
            if deleted != 1:
                connection.rollback()
                raise ValueError("账户不存在")
            connection.commit()
        return [str(csv_path) for _, csv_path in jobs if csv_path]


auth_store = AuthStore()
