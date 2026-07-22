"""Encrypted account repository with legacy file compatibility.

The vault is the workbench source of truth. Legacy account files are imported
once and are regenerated atomically after mutations so existing CLI entry
points can continue to consume their historical formats.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "web_data"
DEFAULT_OUTLOOK_FILE = ROOT / "outlook_accounts.txt"
DEFAULT_ICLOUD_FILE = ROOT / "icloud_accounts.txt"
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ACCOUNTS = 1000
REFRESH_LEASE_SECONDS = 30


class VaultError(RuntimeError):
    pass


class ImportValidationError(VaultError):
    def __init__(self, errors: list[dict[str, object]]):
        super().__init__("account import validation failed")
        self.errors = errors


class AccountNotFound(VaultError):
    pass


class RefreshBusy(VaultError):
    def __init__(self, retry_after: int = 1):
        super().__init__("account refresh is busy")
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class VaultAccount:
    id: int
    email: str
    kind: str
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    relay_url: str = ""
    source_file: str = ""
    token_valid: bool = True
    openai_mail_seen: bool | None = None
    openai_mail_last_seen_at: str = ""
    last_refresh_at: str = ""

    @property
    def has_otp_creds(self) -> bool:
        return bool(self.email and (self.refresh_token if self.kind == "outlook" else self.relay_url))

    @property
    def raw_line(self) -> str:
        if self.kind == "icloud":
            return f"{self.email}----{self.relay_url}"
        return "----".join((self.email, self.password, self.client_id, self.refresh_token))

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.id,
            "email": self.email,
            "kind": self.kind,
            "source_file": self.source_file,
            "has_token": self.has_otp_creds,
            "token_valid": self.token_valid,
            "openai_mail_seen": self.openai_mail_seen,
            "openai_mail_last_seen_at": self.openai_mail_last_seen_at,
            "last_refresh_at": self.last_refresh_at,
        }

    def credentials(self) -> dict[str, object]:
        return {
            "id": self.id,
            "email": self.email,
            "kind": self.kind,
            "password": self.password,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "relay_url": self.relay_url,
            "has_token": self.has_otp_creds,
        }


@dataclass(frozen=True)
class ParsedAccount:
    email: str
    kind: str
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    relay_url: str = ""
    source_file: str = ""


class AccountVault:
    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        outlook_file: Path = DEFAULT_OUTLOOK_FILE,
        icloud_file: Path = DEFAULT_ICLOUD_FILE,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "account_vault.db"
        self.key_path = self.data_dir / "account_vault.key"
        self.outlook_file = Path(outlook_file)
        self.icloud_file = Path(icloud_file)
        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cipher = Fernet(self._load_or_create_key())
        self._init_schema()
        self._bootstrap_legacy_files()
        self._protect_legacy_files()

    def _protect_legacy_files(self) -> None:
        for path in (self.outlook_file, self.icloud_file):
            if not path.exists():
                continue
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise VaultError(f"invalid account vault key: {self.key_path}") from exc
            return key
        key = Fernet.generate_key()
        self._atomic_write(self.key_path, key + b"\n", mode=0o600)
        return key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _init_schema(self) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS vault_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                email_normalized TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('outlook', 'icloud')),
                source_file TEXT NOT NULL DEFAULT '',
                token_valid INTEGER NOT NULL DEFAULT 1,
                openai_mail_seen INTEGER,
                openai_mail_last_seen_at TEXT NOT NULL DEFAULT '',
                last_refresh_at TEXT NOT NULL DEFAULT '',
                refresh_lease_token TEXT,
                refresh_lease_expires_at REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS account_credentials (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                password_ciphertext TEXT NOT NULL,
                client_id TEXT NOT NULL,
                refresh_token_ciphertext TEXT NOT NULL,
                relay_url_ciphertext TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS accounts_kind_idx ON accounts(kind, id);
            CREATE INDEX IF NOT EXISTS accounts_status_idx
                ON accounts(token_valid, openai_mail_seen);
            CREATE INDEX IF NOT EXISTS accounts_refresh_lease_idx
                ON accounts(refresh_lease_expires_at)
                WHERE refresh_lease_token IS NOT NULL;
        """
        with self._connect() as connection:
            connection.executescript(schema)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _bootstrap_legacy_files(self) -> None:
        with self._lock, self._connect() as connection:
            imported = connection.execute(
                "SELECT value FROM vault_meta WHERE key = 'legacy_import_completed'"
            ).fetchone()
            if imported is not None:
                return
            records: list[ParsedAccount] = []
            records.extend(self._parse_legacy_file(self.outlook_file, "outlook"))
            records.extend(self._parse_legacy_file(self.icloud_file, "icloud"))
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                self._upsert(connection, record, update_existing=False)
            connection.execute(
                "INSERT INTO vault_meta(key, value) VALUES('legacy_import_completed', ?)",
                (json.dumps({"imported": len(records), "at": int(time.time())}),),
            )
            connection.commit()

    def _parse_legacy_file(self, path: Path, kind: str) -> list[ParsedAccount]:
        if not path.exists():
            return []
        records: list[ParsedAccount] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                records.append(
                    parse_account_line(line, forced_kind=kind, source_file=path.name)
                )
            except ValueError:
                # Keep startup tolerant of historical malformed lines. New imports
                # are strict and report every invalid line before writing anything.
                continue
        return records

    def _encrypt(self, value: str) -> str:
        return "v1:" + self._cipher.encrypt((value or "").encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value.startswith("v1:"):
            raise VaultError("unsupported account credential version")
        try:
            return self._cipher.decrypt(value[3:].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise VaultError("account credential decryption failed") from exc

    def _upsert(
        self,
        connection: sqlite3.Connection,
        record: ParsedAccount,
        *,
        update_existing: bool,
    ) -> tuple[int, bool]:
        normalized = record.email.strip().casefold()
        existing = connection.execute(
            "SELECT id FROM accounts WHERE email_normalized = ?", (normalized,)
        ).fetchone()
        if existing is not None and not update_existing:
            return int(existing["id"]), False
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO accounts(email, email_normalized, kind, source_file)
                VALUES (?, ?, ?, ?)
                """,
                (record.email.strip(), normalized, record.kind, record.source_file),
            )
            account_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO account_credentials(
                    account_id, password_ciphertext, client_id,
                    refresh_token_ciphertext, relay_url_ciphertext
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    self._encrypt(record.password),
                    record.client_id,
                    self._encrypt(record.refresh_token),
                    self._encrypt(record.relay_url),
                ),
            )
            return account_id, True

        account_id = int(existing["id"])
        connection.execute(
            """
            UPDATE accounts
            SET email = ?, kind = ?, source_file = ?, token_valid = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (record.email.strip(), record.kind, record.source_file, account_id),
        )
        connection.execute(
            """
            UPDATE account_credentials
            SET password_ciphertext = ?, client_id = ?,
                refresh_token_ciphertext = ?, relay_url_ciphertext = ?
            WHERE account_id = ?
            """,
            (
                self._encrypt(record.password),
                record.client_id,
                self._encrypt(record.refresh_token),
                self._encrypt(record.relay_url),
                account_id,
            ),
        )
        return account_id, False

    def list_accounts(self, kind: str | None = None) -> list[VaultAccount]:
        query = """
            SELECT a.*, c.password_ciphertext, c.client_id,
                   c.refresh_token_ciphertext, c.relay_url_ciphertext
            FROM accounts a
            JOIN account_credentials c ON c.account_id = a.id
        """
        parameters: tuple[object, ...] = ()
        if kind in {"outlook", "icloud"}:
            query += " WHERE a.kind = ?"
            parameters = (kind,)
        query += " ORDER BY a.id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_account(row) for row in rows]

    def list_metadata(self, kind: str | None = None) -> list[dict[str, object]]:
        query = """
            SELECT id, email, kind, source_file, token_valid,
                   openai_mail_seen, openai_mail_last_seen_at, last_refresh_at
            FROM accounts
        """
        parameters: tuple[object, ...] = ()
        if kind in {"outlook", "icloud"}:
            query += " WHERE kind = ?"
            parameters = (kind,)
        query += " ORDER BY id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": int(row["id"]),
                "email": str(row["email"]),
                "kind": str(row["kind"]),
                "source_file": str(row["source_file"] or ""),
                "has_token": True,
                "token_valid": bool(row["token_valid"]),
                "openai_mail_seen": None if row["openai_mail_seen"] is None else bool(row["openai_mail_seen"]),
                "openai_mail_last_seen_at": str(row["openai_mail_last_seen_at"] or ""),
                "last_refresh_at": str(row["last_refresh_at"] or ""),
            }
            for row in rows
        ]

    def get_account(self, account: int | str) -> VaultAccount:
        field = "a.id" if isinstance(account, int) else "a.email_normalized"
        value: object = account if isinstance(account, int) else account.strip().casefold()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT a.*, c.password_ciphertext, c.client_id,
                       c.refresh_token_ciphertext, c.relay_url_ciphertext
                FROM accounts a
                JOIN account_credentials c ON c.account_id = a.id
                WHERE {field} = ?
                """,
                (value,),
            ).fetchone()
        if row is None:
            raise AccountNotFound(str(account))
        return self._row_to_account(row)

    def _row_to_account(self, row: sqlite3.Row) -> VaultAccount:
        seen_raw = row["openai_mail_seen"]
        return VaultAccount(
            id=int(row["id"]),
            email=str(row["email"]),
            kind=str(row["kind"]),
            password=self._decrypt(str(row["password_ciphertext"])),
            client_id=str(row["client_id"] or ""),
            refresh_token=self._decrypt(str(row["refresh_token_ciphertext"])),
            relay_url=self._decrypt(str(row["relay_url_ciphertext"])),
            source_file=str(row["source_file"] or ""),
            token_valid=bool(row["token_valid"]),
            openai_mail_seen=None if seen_raw is None else bool(seen_raw),
            openai_mail_last_seen_at=str(row["openai_mail_last_seen_at"] or ""),
            last_refresh_at=str(row["last_refresh_at"] or ""),
        )

    def import_text(
        self,
        text: str,
        *,
        forced_kind: str = "",
        update_existing: bool = False,
    ) -> dict[str, object]:
        records = parse_import_text(text, forced_kind=forced_kind)
        added = 0
        updated = 0
        duplicate = 0
        added_by_kind = {"outlook": 0, "icloud": 0}
        updated_by_kind = {"outlook": 0, "icloud": 0}
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for record in records:
                    existed = connection.execute(
                        "SELECT 1 FROM accounts WHERE email_normalized = ?",
                        (record.email.casefold(),),
                    ).fetchone() is not None
                    _, inserted = self._upsert(
                        connection, record, update_existing=update_existing
                    )
                    if inserted:
                        added += 1
                        added_by_kind[record.kind] += 1
                    elif existed and update_existing:
                        updated += 1
                        updated_by_kind[record.kind] += 1
                    else:
                        duplicate += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self.materialize_legacy_files()
        return {
            "added": added,
            "updated": updated,
            "dup": duplicate,
            "total": len(records),
            "added_by_kind": added_by_kind,
            "updated_by_kind": updated_by_kind,
        }

    def replace_text(self, text: str, *, forced_kind: str = "") -> dict[str, object]:
        """Atomically replace the complete credential pool after validation.

        The standalone console exposes an explicit replacement import mode.  It
        validates the entire payload before removing existing rows, then keeps
        the legacy compatibility files in sync with the encrypted vault.
        """
        records = parse_import_text(text, forced_kind=forced_kind)
        added_by_kind = {"outlook": 0, "icloud": 0}
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM accounts")
                for record in records:
                    self._upsert(connection, record, update_existing=True)
                    added_by_kind[record.kind] += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self.materialize_legacy_files()
        return {
            "added": len(records),
            "updated": 0,
            "dup": 0,
            "total": len(records),
            "added_by_kind": added_by_kind,
            "updated_by_kind": {"outlook": 0, "icloud": 0},
        }

    def delete(self, account: int | str) -> None:
        field = "id" if isinstance(account, int) else "email_normalized"
        value: object = account if isinstance(account, int) else account.strip().casefold()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM accounts WHERE {field} = ?", (value,))
            if cursor.rowcount != 1:
                raise AccountNotFound(str(account))
        self.materialize_legacy_files()

    def materialize_legacy_files(self) -> None:
        accounts = self.list_accounts()
        outlook = [account.raw_line for account in accounts if account.kind == "outlook"]
        icloud = [account.raw_line for account in accounts if account.kind == "icloud"]
        header = "# Generated from web_data/account_vault.db. Import changes through the workbench.\n"
        with self._lock:
            self._atomic_write(
                self.outlook_file,
                (header + "\n".join(outlook) + ("\n" if outlook else "")).encode("utf-8"),
                mode=0o600,
            )
            self._atomic_write(
                self.icloud_file,
                (header + "\n".join(icloud) + ("\n" if icloud else "")).encode("utf-8"),
                mode=0o600,
            )

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def claim_refresh(self, email: str, *, ttl: int = REFRESH_LEASE_SECONDS) -> dict[str, object]:
        normalized = email.strip().casefold()
        lease = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT a.id, a.token_valid, a.refresh_lease_token,
                       a.refresh_lease_expires_at, c.client_id,
                       c.refresh_token_ciphertext
                FROM accounts a
                JOIN account_credentials c ON c.account_id = a.id
                WHERE a.email_normalized = ? AND a.kind = 'outlook'
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise AccountNotFound(email)
            expires = float(row["refresh_lease_expires_at"] or 0)
            if row["refresh_lease_token"] and expires > now:
                connection.rollback()
                raise RefreshBusy(max(1, int(expires - now + 0.999)))
            if not bool(row["token_valid"]):
                connection.rollback()
                raise VaultError("account refresh token is marked invalid")
            connection.execute(
                """
                UPDATE accounts
                SET refresh_lease_token = ?, refresh_lease_expires_at = ?
                WHERE id = ?
                """,
                (lease, now + max(5, ttl), int(row["id"])),
            )
            connection.commit()
        return {
            "account_id": int(row["id"]),
            "lease": lease,
            "client_id": str(row["client_id"] or ""),
            "refresh_token": self._decrypt(str(row["refresh_token_ciphertext"])),
        }

    def finalize_refresh(self, account_id: int, lease: str, refresh_token: str) -> bool:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT c.refresh_token_ciphertext
                FROM accounts a
                JOIN account_credentials c ON c.account_id = a.id
                WHERE a.id = ? AND a.refresh_lease_token = ?
                """,
                (account_id, lease),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            current = self._decrypt(str(row["refresh_token_ciphertext"]))
            if refresh_token and refresh_token != current:
                connection.execute(
                    "UPDATE account_credentials SET refresh_token_ciphertext = ? WHERE account_id = ?",
                    (self._encrypt(refresh_token), account_id),
                )
            connection.execute(
                """
                UPDATE accounts
                SET refresh_lease_token = NULL, refresh_lease_expires_at = NULL,
                    last_refresh_at = CURRENT_TIMESTAMP, token_valid = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND refresh_lease_token = ?
                """,
                (account_id, lease),
            )
            connection.commit()
        if refresh_token and refresh_token != current:
            self.materialize_legacy_files()
        return True

    def release_refresh(self, account_id: int, lease: str, *, invalid: bool = False) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE accounts
                SET refresh_lease_token = NULL, refresh_lease_expires_at = NULL,
                    token_valid = CASE WHEN ? THEN 0 ELSE token_valid END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND refresh_lease_token = ?
                """,
                (1 if invalid else 0, account_id, lease),
            )

    def mark_openai_mail_seen(self, account_id: int, seen: bool) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE accounts
                SET openai_mail_seen = ?,
                    openai_mail_last_seen_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE '' END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if seen else 0, 1 if seen else 0, account_id),
            )
            if cursor.rowcount != 1:
                raise AccountNotFound(str(account_id))


def parse_account_line(
    line: str,
    *,
    forced_kind: str = "",
    source_file: str = "",
) -> ParsedAccount:
    value = str(line or "").strip()
    if not value:
        raise ValueError("empty line")
    kind = forced_kind.strip().lower()
    if kind and kind not in {"outlook", "icloud"}:
        raise ValueError("kind must be outlook or icloud")
    if not kind:
        first, separator, remainder = value.partition("----")
        if not separator:
            raise ValueError("missing field delimiter")
        if remainder.strip().startswith(("http://", "https://")) and "----" not in remainder:
            kind = "icloud"
        else:
            kind = "outlook"

    if kind == "icloud":
        email, separator, relay_url = value.partition("----")
        email = email.strip()
        relay_url = relay_url.strip()
        if not separator or not relay_url.startswith(("http://", "https://")):
            raise ValueError("icloud line must be email----http(s)://relay-url")
        _validate_email(email)
        return ParsedAccount(
            email=email,
            kind="icloud",
            relay_url=relay_url,
            source_file=source_file or "icloud_accounts.txt",
        )

    parts = value.split("----", 3)
    if len(parts) != 4:
        raise ValueError("outlook line must contain four fields")
    email, password, client_id, refresh_token = (part.strip() for part in parts)
    _validate_email(email)
    if not client_id or not refresh_token:
        raise ValueError("outlook client_id and refresh_token are required")
    if len(password) > 1024 or len(client_id) > 256 or len(refresh_token) > 16384:
        raise ValueError("account field is too long")
    return ParsedAccount(
        email=email,
        kind="outlook",
        password=password,
        client_id=client_id,
        refresh_token=refresh_token,
        source_file=source_file or "outlook_accounts.txt",
    )


def parse_import_text(text: str, *, forced_kind: str = "") -> list[ParsedAccount]:
    if not isinstance(text, str) or not text.strip():
        raise ImportValidationError([{"line": 0, "error": "导入内容为空"}])
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ImportValidationError([{"line": 0, "error": "导入内容超过 2 MiB"}])
    source_lines = [
        (line_number, raw.strip())
        for line_number, raw in enumerate(text.replace("\ufeff", "", 1).splitlines(), 1)
        if raw.strip() and not raw.strip().startswith("#")
    ]
    if len(source_lines) > MAX_IMPORT_ACCOUNTS:
        raise ImportValidationError(
            [{"line": 0, "error": f"单次最多导入 {MAX_IMPORT_ACCOUNTS} 个账号"}]
        )
    errors: list[dict[str, object]] = []
    records: list[ParsedAccount] = []
    seen: set[str] = set()
    for line_number, line in source_lines:
        try:
            record = parse_account_line(line, forced_kind=forced_kind)
            normalized = record.email.casefold()
            if normalized in seen:
                raise ValueError("导入内容中邮箱重复")
            seen.add(normalized)
            records.append(record)
        except ValueError as exc:
            errors.append({"line": line_number, "error": str(exc)})
    if not records and not errors:
        errors.append({"line": 0, "error": "没有可导入的账号"})
    if errors:
        raise ImportValidationError(errors)
    return records


def _validate_email(email: str) -> None:
    if not email or len(email) > 320 or email.count("@") != 1:
        raise ValueError("邮箱格式错误")
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(character.isspace() for character in email):
        raise ValueError("邮箱格式错误")


_default_vault: AccountVault | None = None
_default_vault_lock = threading.Lock()


def get_account_vault() -> AccountVault:
    global _default_vault
    if _default_vault is None:
        with _default_vault_lock:
            if _default_vault is None:
                data_dir = Path(os.environ.get("ACCOUNT_VAULT_DIR", DEFAULT_DATA_DIR))
                _default_vault = AccountVault(data_dir=data_dir)
    return _default_vault


def reset_default_vault_for_tests() -> None:
    global _default_vault
    with _default_vault_lock:
        _default_vault = None
