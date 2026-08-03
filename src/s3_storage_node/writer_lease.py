from __future__ import annotations

import re
import threading
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

try:  # Imported lazily by local-only deployments.
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - exercised by runtime error path
    psycopg = None
    sql = None


class WriterLeaseError(RuntimeError):
    pass


class WriterLeaseUnavailable(WriterLeaseError):
    pass


class WriterLeaseLost(WriterLeaseError):
    pass


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class WriterLeaseConfig:
    backend: str = "local"
    lease_name: str = ""
    node_id: str = ""
    ttl_seconds: int = 15
    renew_interval_seconds: int = 5
    retry_interval_seconds: int = 1
    fence_margin_seconds: int = 2
    takeover_delay_seconds: int = 5
    connect_timeout_seconds: int = 5
    postgres_dsn_file: str = ""
    postgres_schema: str = "public"
    postgres_table: str = "s3_storage_node_writer_leases"
    auto_create: bool = True

    @property
    def epoch_table(self) -> str:
        return f"{self.postgres_table}_epochs"


@dataclass(frozen=True)
class LeaseRecord:
    lease_name: str
    owner_id: str
    fencing_token: int
    lease_until_epoch: float
    takeover_blocked: bool = False
    block_reason: str = ""


class LeaseBackend(Protocol):
    def initialize(self) -> None: ...

    def acquire(self, owner_id: str) -> LeaseRecord | None: ...

    def renew(self, owner_id: str, fencing_token: int) -> LeaseRecord | None: ...

    def release(self, owner_id: str, fencing_token: int) -> bool: ...

    def status(self) -> LeaseRecord | None: ...

    def block_takeover(self, owner_id: str, fencing_token: int, reason: str) -> bool: ...

    def unblock(self, expected_token: int) -> bool: ...


def _positive_int(raw: Any, key: str, default: int, *, allow_zero: bool = False) -> int:
    value = default if raw is None else raw
    if isinstance(value, bool) or not isinstance(value, int):
        raise WriterLeaseError(f"{key} must be an integer")
    invalid = value < 0 if allow_zero else value <= 0
    if invalid:
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise WriterLeaseError(f"{key} must be {qualifier}")
    return value


def _string(raw: Any, key: str, default: str = "") -> str:
    value = default if raw is None else raw
    if not isinstance(value, str):
        raise WriterLeaseError(f"{key} must be a string")
    return value.strip()


def _bool(raw: Any, key: str, default: bool) -> bool:
    value = default if raw is None else raw
    if not isinstance(value, bool):
        raise WriterLeaseError(f"{key} must be true or false")
    return value


def _validate_identifier(value: str, key: str, *, maximum: int = 40) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise WriterLeaseError(f"{key} must be a simple PostgreSQL identifier")
    if len(value) > maximum:
        raise WriterLeaseError(f"{key} may not exceed {maximum} characters")
    return value


def load_writer_lease(config_path: str, config: Any) -> WriterLeaseConfig:
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WriterLeaseError(f"configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise WriterLeaseError(f"invalid TOML in {config_path}: {exc}") from exc

    section = raw.get("writer_lease", {})
    if not isinstance(section, dict):
        raise WriterLeaseError("[writer_lease] must be a table")
    backend = _string(section.get("backend"), "writer_lease.backend", "local").lower()
    if backend not in {"local", "postgres"}:
        raise WriterLeaseError("writer_lease.backend must be local or postgres")

    node_id = _string(section.get("node_id"), "writer_lease.node_id", config.appliance.name)
    if not node_id or len(node_id) > 128 or any(ord(char) < 32 for char in node_id):
        raise WriterLeaseError("writer_lease.node_id must be 1-128 printable characters")

    default_lease_name = getattr(getattr(config, "data_target", None), "sentinel_id", "")
    lease_name = _string(section.get("lease_name"), "writer_lease.lease_name", default_lease_name)
    ttl = _positive_int(section.get("ttl_seconds"), "writer_lease.ttl_seconds", 15)
    renew = _positive_int(section.get("renew_interval_seconds"), "writer_lease.renew_interval_seconds", 5)
    retry = _positive_int(section.get("retry_interval_seconds"), "writer_lease.retry_interval_seconds", 1)
    margin = _positive_int(section.get("fence_margin_seconds"), "writer_lease.fence_margin_seconds", 2)
    takeover = _positive_int(
        section.get("takeover_delay_seconds"),
        "writer_lease.takeover_delay_seconds",
        5,
        allow_zero=True,
    )
    connect_timeout = _positive_int(
        section.get("connect_timeout_seconds"),
        "writer_lease.connect_timeout_seconds",
        5,
    )

    if renew + margin >= ttl:
        raise WriterLeaseError(
            "writer_lease.renew_interval_seconds + fence_margin_seconds must be less than ttl_seconds"
        )
    if retry >= ttl - margin:
        raise WriterLeaseError("writer_lease.retry_interval_seconds must be less than the safe lease window")
    if connect_timeout >= ttl - margin:
        raise WriterLeaseError("writer_lease.connect_timeout_seconds must be less than the safe lease window")

    if backend == "local":
        return WriterLeaseConfig(
            backend=backend,
            lease_name=lease_name or node_id,
            node_id=node_id,
            ttl_seconds=ttl,
            renew_interval_seconds=renew,
            retry_interval_seconds=retry,
            fence_margin_seconds=margin,
            takeover_delay_seconds=takeover,
            connect_timeout_seconds=connect_timeout,
        )

    if getattr(config.appliance, "worker_fencing_mode", "disabled") != "namespace":
        raise WriterLeaseError("writer_lease.backend=postgres requires appliance.worker_fencing_mode=namespace")
    if not lease_name:
        raise WriterLeaseError("writer_lease.lease_name is required for the postgres backend")
    dsn_file = _string(section.get("postgres_dsn_file"), "writer_lease.postgres_dsn_file")
    if not dsn_file or not Path(dsn_file).is_absolute():
        raise WriterLeaseError("writer_lease.postgres_dsn_file must be an absolute path")
    schema = _validate_identifier(
        _string(section.get("postgres_schema"), "writer_lease.postgres_schema", "public"),
        "writer_lease.postgres_schema",
    )
    table = _validate_identifier(
        _string(
            section.get("postgres_table"),
            "writer_lease.postgres_table",
            "s3_storage_node_writer_leases",
        ),
        "writer_lease.postgres_table",
    )
    if len(f"{table}_epochs") > 63:
        raise WriterLeaseError("writer_lease.postgres_table is too long for its epoch table")

    return WriterLeaseConfig(
        backend=backend,
        lease_name=lease_name,
        node_id=node_id,
        ttl_seconds=ttl,
        renew_interval_seconds=renew,
        retry_interval_seconds=retry,
        fence_margin_seconds=margin,
        takeover_delay_seconds=takeover,
        connect_timeout_seconds=connect_timeout,
        postgres_dsn_file=dsn_file,
        postgres_schema=schema,
        postgres_table=table,
        auto_create=_bool(section.get("auto_create"), "writer_lease.auto_create", True),
    )


class PostgresLeaseBackend:
    def __init__(self, config: WriterLeaseConfig, connect: Callable[..., Any] | None = None) -> None:
        if config.backend != "postgres":
            raise WriterLeaseError("PostgresLeaseBackend requires backend=postgres")
        self.config = config
        self._connect_factory = connect

    def _connect(self):
        if self._connect_factory is not None:
            return self._connect_factory()
        if psycopg is None:
            raise WriterLeaseError('postgres writer leases require the "psycopg[binary]" package')
        try:
            dsn = Path(self.config.postgres_dsn_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WriterLeaseError(f"unable to read postgres DSN file: {exc}") from exc
        if not dsn:
            raise WriterLeaseError("postgres DSN file is empty")
        try:
            return psycopg.connect(
                dsn,
                connect_timeout=self.config.connect_timeout_seconds,
                application_name=f"s3-storage-node-writer-lease:{self.config.node_id}",
            )
        except Exception as exc:  # noqa: BLE001 - database driver boundary
            raise WriterLeaseError(f"postgres writer lease connection failed: {exc}") from exc

    def _identifiers(self):
        if sql is None:
            raise WriterLeaseError('postgres writer leases require the "psycopg[binary]" package')
        return (
            sql.Identifier(self.config.postgres_schema, self.config.postgres_table),
            sql.Identifier(self.config.postgres_schema, self.config.epoch_table),
        )

    def initialize(self) -> None:
        if not self.config.auto_create:
            return
        lease_table, epoch_table = self._identifiers()
        query = sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {epoch_table} (
                singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                value bigint NOT NULL
            );
            INSERT INTO {epoch_table} (singleton, value)
            VALUES (TRUE, 0)
            ON CONFLICT (singleton) DO NOTHING;
            CREATE TABLE IF NOT EXISTS {lease_table} (
                lease_name text PRIMARY KEY,
                owner_id text NOT NULL,
                fencing_token bigint NOT NULL,
                acquired_at timestamptz NOT NULL,
                renewed_at timestamptz NOT NULL,
                lease_until timestamptz NOT NULL,
                takeover_blocked boolean NOT NULL DEFAULT FALSE,
                block_reason text NOT NULL DEFAULT ''
            );
            """
        ).format(epoch_table=epoch_table, lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query)
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"unable to initialize postgres writer lease tables: {exc}") from exc

    @staticmethod
    def _record(row: Any) -> LeaseRecord:
        return LeaseRecord(
            lease_name=str(row[0]),
            owner_id=str(row[1]),
            fencing_token=int(row[2]),
            lease_until_epoch=float(row[3]),
            takeover_blocked=bool(row[4]),
            block_reason=str(row[5] or ""),
        )

    def acquire(self, owner_id: str) -> LeaseRecord | None:
        lease_table, epoch_table = self._identifiers()
        select_epoch = sql.SQL("SELECT value FROM {} WHERE singleton = TRUE FOR UPDATE").format(epoch_table)
        select_lease = sql.SQL(
            """
            SELECT lease_name, owner_id, fencing_token,
                   EXTRACT(EPOCH FROM lease_until), takeover_blocked, block_reason,
                   lease_until + (%s * interval '1 second') <= clock_timestamp() AS eligible
            FROM {lease_table}
            WHERE lease_name = %s
            FOR UPDATE
            """
        ).format(lease_table=lease_table)
        update_epoch = sql.SQL(
            "UPDATE {} SET value = value + 1 WHERE singleton = TRUE RETURNING value"
        ).format(epoch_table)
        upsert = sql.SQL(
            """
            INSERT INTO {lease_table} AS leases (
                lease_name, owner_id, fencing_token, acquired_at, renewed_at,
                lease_until, takeover_blocked, block_reason
            ) VALUES (
                %s, %s, %s, clock_timestamp(), clock_timestamp(),
                clock_timestamp() + (%s * interval '1 second'), FALSE, ''
            )
            ON CONFLICT (lease_name) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                fencing_token = EXCLUDED.fencing_token,
                acquired_at = EXCLUDED.acquired_at,
                renewed_at = EXCLUDED.renewed_at,
                lease_until = EXCLUDED.lease_until,
                takeover_blocked = FALSE,
                block_reason = ''
            RETURNING lease_name, owner_id, fencing_token,
                      EXTRACT(EPOCH FROM lease_until), takeover_blocked, block_reason
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(select_epoch)
                    if cursor.fetchone() is None:
                        raise WriterLeaseError("postgres writer lease epoch row is missing")
                    cursor.execute(
                        select_lease,
                        (self.config.takeover_delay_seconds, self.config.lease_name),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        blocked = bool(existing[4])
                        eligible = bool(existing[6])
                        if blocked or not eligible:
                            return None
                    cursor.execute(update_epoch)
                    token_row = cursor.fetchone()
                    if token_row is None:
                        raise WriterLeaseError("failed to allocate a postgres fencing token")
                    token = int(token_row[0])
                    cursor.execute(
                        upsert,
                        (self.config.lease_name, owner_id, token, self.config.ttl_seconds),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise WriterLeaseError("postgres writer lease acquisition returned no row")
                    return self._record(row)
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"postgres writer lease acquisition failed: {exc}") from exc

    def renew(self, owner_id: str, fencing_token: int) -> LeaseRecord | None:
        lease_table, _epoch_table = self._identifiers()
        query = sql.SQL(
            """
            UPDATE {lease_table}
            SET renewed_at = clock_timestamp(),
                lease_until = clock_timestamp() + (%s * interval '1 second')
            WHERE lease_name = %s
              AND owner_id = %s
              AND fencing_token = %s
              AND takeover_blocked = FALSE
              AND lease_until > clock_timestamp()
            RETURNING lease_name, owner_id, fencing_token,
                      EXTRACT(EPOCH FROM lease_until), takeover_blocked, block_reason
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        query,
                        (self.config.ttl_seconds, self.config.lease_name, owner_id, fencing_token),
                    )
                    row = cursor.fetchone()
                    return None if row is None else self._record(row)
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"postgres writer lease renewal failed: {exc}") from exc

    def release(self, owner_id: str, fencing_token: int) -> bool:
        lease_table, _epoch_table = self._identifiers()
        query = sql.SQL(
            """
            DELETE FROM {lease_table}
            WHERE lease_name = %s AND owner_id = %s AND fencing_token = %s
              AND takeover_blocked = FALSE
            RETURNING 1
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (self.config.lease_name, owner_id, fencing_token))
                    return cursor.fetchone() is not None
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"postgres writer lease release failed: {exc}") from exc

    def status(self) -> LeaseRecord | None:
        lease_table, _epoch_table = self._identifiers()
        query = sql.SQL(
            """
            SELECT lease_name, owner_id, fencing_token,
                   EXTRACT(EPOCH FROM lease_until), takeover_blocked, block_reason
            FROM {lease_table}
            WHERE lease_name = %s
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (self.config.lease_name,))
                    row = cursor.fetchone()
                    return None if row is None else self._record(row)
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"postgres writer lease status failed: {exc}") from exc

    def block_takeover(self, owner_id: str, fencing_token: int, reason: str) -> bool:
        lease_table, _epoch_table = self._identifiers()
        query = sql.SQL(
            """
            UPDATE {lease_table}
            SET takeover_blocked = TRUE,
                block_reason = %s,
                renewed_at = clock_timestamp(),
                lease_until = GREATEST(
                    lease_until,
                    clock_timestamp() + (%s * interval '1 second')
                )
            WHERE lease_name = %s AND owner_id = %s AND fencing_token = %s
            RETURNING 1
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        query,
                        (reason[:2048], self.config.ttl_seconds, self.config.lease_name, owner_id, fencing_token),
                    )
                    return cursor.fetchone() is not None
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"failed to block postgres writer lease takeover: {exc}") from exc

    def unblock(self, expected_token: int) -> bool:
        lease_table, _epoch_table = self._identifiers()
        query = sql.SQL(
            """
            UPDATE {lease_table}
            SET takeover_blocked = FALSE, block_reason = ''
            WHERE lease_name = %s
              AND fencing_token = %s
              AND takeover_blocked = TRUE
              AND lease_until <= clock_timestamp()
            RETURNING 1
            """
        ).format(lease_table=lease_table)
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, (self.config.lease_name, expected_token))
                    return cursor.fetchone() is not None
        except WriterLeaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WriterLeaseError(f"failed to unblock postgres writer lease: {exc}") from exc


class WriterLeaseController:
    def __init__(
        self,
        config: WriterLeaseConfig,
        *,
        backend: LeaseBackend | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        on_at_risk: Callable[[str], None] | None = None,
        on_recovered: Callable[[], None] | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or (PostgresLeaseBackend(config) if config.backend == "postgres" else None)
        self.owner_id = f"{config.node_id}:{uuid.uuid4().hex}"
        self.on_update = on_update
        self.on_at_risk = on_at_risk
        self.on_recovered = on_recovered
        self.on_lost = on_lost
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initialized = False
        self._held = False
        self._at_risk = False
        self._lost = False
        self._blocked = False
        self._fencing_token = 0
        self._lease_until_epoch = 0.0
        self._deadline_monotonic = 0.0
        self._renewals_total = 0
        self._renewal_failures_total = 0
        self._last_error = ""

    @property
    def held(self) -> bool:
        with self._lock:
            return self._held

    @property
    def fencing_token(self) -> int:
        with self._lock:
            return self._fencing_token

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._held and not self._at_risk and not self._lost and not self._blocked

    def _publish(self) -> None:
        callback = self.on_update
        if callback is not None:
            callback(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining = max(0.0, self._deadline_monotonic - time.monotonic()) if self._held else 0.0
            return {
                "backend": self.config.backend,
                "scope": "distributed" if self.config.backend == "postgres" else "local",
                "lease_name": self.config.lease_name,
                "owner": self.owner_id,
                "node_id": self.config.node_id,
                "held": self._held,
                "healthy": self._held and not self._at_risk and not self._lost and not self._blocked,
                "at_risk": self._at_risk,
                "lost": self._lost,
                "takeover_blocked": self._blocked,
                "fencing_token": self._fencing_token,
                "lease_until_epoch": self._lease_until_epoch,
                "ttl_remaining_seconds": remaining,
                "renewals_total": self._renewals_total,
                "renewal_failures_total": self._renewal_failures_total,
                "last_error": self._last_error,
            }

    def acquire(self) -> int:
        with self._lock:
            if self._held and not self._lost:
                return self._fencing_token
        if self.config.backend == "local":
            with self._lock:
                self._held = True
                self._at_risk = False
                self._lost = False
                self._blocked = False
                self._fencing_token = 0
                self._last_error = ""
            self._publish()
            return 0

        assert self.backend is not None
        if not self._initialized:
            self.backend.initialize()
            self._initialized = True
        record = self.backend.acquire(self.owner_id)
        if record is None:
            current = self.backend.status()
            detail = "held by another node"
            if current is not None:
                detail = f"held by {current.owner_id} with token {current.fencing_token}"
                if current.takeover_blocked:
                    detail += f"; takeover blocked: {current.block_reason or 'no reason recorded'}"
            raise WriterLeaseUnavailable(f"writer lease {self.config.lease_name!r} unavailable: {detail}")
        self._accept_record(record)
        self._start_monitor()
        self._publish()
        return record.fencing_token

    def _accept_record(self, record: LeaseRecord) -> None:
        remaining = max(0.0, record.lease_until_epoch - time.time())
        with self._lock:
            self._held = True
            self._at_risk = False
            self._lost = False
            self._blocked = record.takeover_blocked
            self._fencing_token = record.fencing_token
            self._lease_until_epoch = record.lease_until_epoch
            self._deadline_monotonic = time.monotonic() + remaining
            self._last_error = ""

    def _start_monitor(self) -> None:
        if self.config.backend != "postgres":
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._monitor, name="writer-lease-renewal", daemon=True)
            self._thread.start()

    def _monitor(self) -> None:
        delay = float(self.config.renew_interval_seconds)
        while not self._stop.wait(delay):
            with self._lock:
                if not self._held or self._lost or self._blocked:
                    return
                token = self._fencing_token
                deadline = self._deadline_monotonic
            assert self.backend is not None
            try:
                record = self.backend.renew(self.owner_id, token)
                if record is None:
                    self._mark_lost("postgres rejected writer lease renewal")
                    return
            except Exception as exc:  # noqa: BLE001 - renewal boundary
                message = str(exc)
                first_risk = False
                with self._lock:
                    self._renewal_failures_total += 1
                    self._last_error = message
                    if not self._at_risk:
                        self._at_risk = True
                        first_risk = True
                    safe_deadline = deadline - self.config.fence_margin_seconds
                self._publish()
                if first_risk and self.on_at_risk is not None:
                    self.on_at_risk(message)
                remaining = safe_deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_lost(f"writer lease renewal deadline exceeded: {message}")
                    return
                delay = min(float(self.config.retry_interval_seconds), remaining)
                continue

            recovered = False
            remaining = max(0.0, record.lease_until_epoch - time.time())
            with self._lock:
                recovered = self._at_risk
                self._held = True
                self._at_risk = False
                self._lost = False
                self._fencing_token = record.fencing_token
                self._lease_until_epoch = record.lease_until_epoch
                self._deadline_monotonic = time.monotonic() + remaining
                self._renewals_total += 1
                self._last_error = ""
            self._publish()
            if recovered and self.on_recovered is not None:
                self.on_recovered()
            delay = float(self.config.renew_interval_seconds)

    def _mark_lost(self, reason: str) -> None:
        should_callback = False
        with self._lock:
            if not self._lost:
                should_callback = True
            self._held = False
            self._at_risk = False
            self._lost = True
            self._last_error = reason
        self._publish()
        if should_callback and self.on_lost is not None:
            self.on_lost(reason)

    def assert_usable(self) -> None:
        with self._lock:
            if self._lost or not self._held:
                raise WriterLeaseLost(self._last_error or "writer lease is not held")
            if self._at_risk:
                raise WriterLeaseLost(self._last_error or "writer lease renewal is at risk")
            if self._blocked:
                raise WriterLeaseLost("writer lease takeover is blocked after a fencing failure")

    def block_takeover(self, reason: str) -> bool:
        if self.config.backend != "postgres":
            return False
        with self._lock:
            token = self._fencing_token
        if token <= 0 or self.backend is None:
            return False
        blocked = self.backend.block_takeover(self.owner_id, token, reason)
        if blocked:
            with self._lock:
                self._blocked = True
                self._held = False
                self._last_error = reason
            self._stop.set()
            self._publish()
        return blocked

    def close(self, *, release: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, float(self.config.connect_timeout_seconds + 1)))
        with self._lock:
            held = self._held
            blocked = self._blocked
            token = self._fencing_token
        if release and held and not blocked and self.config.backend == "postgres" and self.backend is not None:
            try:
                self.backend.release(self.owner_id, token)
            except WriterLeaseError:
                pass
        with self._lock:
            self._held = False
        self._publish()

    def status(self) -> dict[str, Any]:
        if self.config.backend == "local":
            return self.snapshot()
        assert self.backend is not None
        if not self._initialized:
            self.backend.initialize()
            self._initialized = True
        record = self.backend.status()
        return {
            "config": asdict(self.config) | {"postgres_dsn_file": "<redacted>"},
            "lease": None if record is None else asdict(record),
        }

    def unblock(self, expected_token: int) -> bool:
        if self.config.backend != "postgres" or self.backend is None:
            raise WriterLeaseError("writer lease unblock requires the postgres backend")
        if not self._initialized:
            self.backend.initialize()
            self._initialized = True
        return self.backend.unblock(expected_token)
