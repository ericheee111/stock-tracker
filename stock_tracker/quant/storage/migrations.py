"""Explicit, checksum-verified SQLite migrations for quantitative evidence.

Nothing in the live application imports or invokes this module automatically.
Callers must explicitly choose a database path and opt into applying changes.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..core.time import utc_now


class MigrationContractError(RuntimeError):
    """Raised when migration history or SQL cannot be trusted."""


class MigrationState(StrEnum):
    APPLIED = "APPLIED"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str
    accepted_checksums: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PlannedMigration:
    version: int
    name: str
    checksum: str
    state: MigrationState


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    database: str
    migrations: tuple[PlannedMigration, ...]

    @property
    def pending(self) -> tuple[PlannedMigration, ...]:
        return tuple(
            migration
            for migration in self.migrations
            if migration.state is MigrationState.PENDING
        )

    @property
    def applied(self) -> tuple[PlannedMigration, ...]:
        return tuple(
            migration
            for migration in self.migrations
            if migration.state is MigrationState.APPLIED
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "apply_required": bool(self.pending),
            "applied_count": len(self.applied),
            "pending_count": len(self.pending),
            "migrations": [
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                    "state": migration.state.value,
                }
                for migration in self.migrations
            ],
        }


_MIGRATION_FILE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_FORBIDDEN_TRANSACTION = re.compile(
    r"(?im)^\s*(BEGIN\s+(?:TRANSACTION|IMMEDIATE|EXCLUSIVE)|"
    r"COMMIT|END\s+TRANSACTION|ROLLBACK|SAVEPOINT|RELEASE)\b"
)


def migration_directory() -> Path:
    return Path(__file__).with_name("migrations")


def _canonicalize_migration_bytes(
    raw: bytes,
    *,
    filename: str,
) -> tuple[str, bytes, frozenset[str]]:
    """Normalize line endings without weakening migration content identity.

    Git may materialize one tracked SQL file as LF or CRLF depending on the
    platform. Line endings are not part of SQLite migration semantics, while
    every other UTF-8 code point remains checksum-significant. The compatible
    checksum set also lets databases created by the previous raw-byte scheme
    survive a Windows/Linux checkout transition.
    """

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationContractError(
            f"migration is not UTF-8: {filename}"
        ) from exc
    canonical_sql = decoded.replace("\r\n", "\n").replace("\r", "\n")
    canonical = canonical_sql.encode("utf-8")
    legacy_variants = {
        raw,
        canonical,
        canonical_sql.replace("\n", "\r\n").encode("utf-8"),
        canonical_sql.replace("\n", "\r").encode("utf-8"),
    }
    accepted = frozenset(
        hashlib.sha256(candidate).hexdigest()
        for candidate in legacy_variants
    )
    return canonical_sql, canonical, accepted


def load_migrations(directory: str | Path | None = None) -> tuple[Migration, ...]:
    """Load and validate ordered migration files from the package directory."""

    root = Path(directory) if directory is not None else migration_directory()
    if not root.is_dir():
        raise MigrationContractError(f"migration directory does not exist: {root}")
    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(root.glob("*.sql")):
        match = _MIGRATION_FILE.fullmatch(path.name)
        if match is None:
            raise MigrationContractError(f"invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationContractError(f"duplicate migration version: {version}")
        raw = path.read_bytes()
        sql, canonical, accepted_checksums = _canonicalize_migration_bytes(
            raw,
            filename=path.name,
        )
        if _FORBIDDEN_TRANSACTION.search(sql):
            raise MigrationContractError(
                f"migration cannot manage its own transaction: {path.name}"
            )
        checksum = hashlib.sha256(canonical).hexdigest()
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sql=sql,
                checksum=checksum,
                accepted_checksums=accepted_checksums,
            )
        )
        seen_versions.add(version)
    if not migrations:
        raise MigrationContractError("no SQL migrations were found")
    versions = [migration.version for migration in migrations]
    if versions != sorted(versions):
        raise MigrationContractError("migration versions are not sorted")
    return tuple(migrations)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def applied_migrations(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    if not _table_exists(connection, "quant_schema_migration"):
        return {}
    rows = connection.execute(
        "SELECT version, name, checksum FROM quant_schema_migration ORDER BY version"
    ).fetchall()
    return {int(version): (str(name), str(checksum)) for version, name, checksum in rows}


def plan_connection(
    connection: sqlite3.Connection,
    *,
    database_label: str = ":connection:",
    migrations: Iterable[Migration] | None = None,
) -> MigrationPlan:
    expected = tuple(migrations or load_migrations())
    applied = applied_migrations(connection)
    known_versions = {migration.version for migration in expected}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise MigrationContractError(
            "database contains unknown quant migration versions: "
            + ", ".join(str(version) for version in unknown)
        )
    planned: list[PlannedMigration] = []
    for migration in expected:
        recorded = applied.get(migration.version)
        if recorded is None:
            state = MigrationState.PENDING
        else:
            name, checksum = recorded
            accepted_checksums = migration.accepted_checksums or frozenset(
                {migration.checksum}
            )
            if name != migration.name or checksum not in accepted_checksums:
                raise MigrationContractError(
                    f"migration {migration.version:04d} history does not match source"
                )
            state = MigrationState.APPLIED
        planned.append(
            PlannedMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                state=state,
            )
        )
    return MigrationPlan(database=database_label, migrations=tuple(planned))


def plan_database(
    database: str | Path,
    *,
    migrations: Iterable[Migration] | None = None,
) -> MigrationPlan:
    """Plan without creating a missing database file."""

    path = Path(database)
    expected = tuple(migrations or load_migrations())
    if not path.exists():
        return MigrationPlan(
            database=str(path),
            migrations=tuple(
                PlannedMigration(
                    migration.version,
                    migration.name,
                    migration.checksum,
                    MigrationState.PENDING,
                )
                for migration in expected
            ),
        )
    read_only_uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as connection:
        return plan_connection(
            connection,
            database_label=str(path),
            migrations=expected,
        )


def iter_sql_statements(sql: str) -> tuple[str, ...]:
    """Split SQLite statements while preserving complete trigger bodies."""

    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationContractError("migration ends with an incomplete SQL statement")
    return tuple(statements)


def apply_connection(
    connection: sqlite3.Connection,
    *,
    database_label: str = ":connection:",
    migrations: Iterable[Migration] | None = None,
) -> MigrationPlan:
    """Apply each pending migration atomically and verify its checksum history."""

    expected = tuple(migrations or load_migrations())
    connection.execute("PRAGMA foreign_keys = ON")
    initial_plan = plan_connection(
        connection,
        database_label=database_label,
        migrations=expected,
    )
    by_version = {migration.version: migration for migration in expected}
    for pending in initial_plan.pending:
        migration = by_version[pending.version]
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in iter_sql_statements(migration.sql):
                connection.execute(statement)
            migration_parameters: tuple[int, str, str, str] = (
                migration.version,
                migration.name,
                migration.checksum,
                utc_now().isoformat(),
            )
            connection.execute(
                """
                INSERT INTO quant_schema_migration(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                migration_parameters,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return plan_connection(
        connection,
        database_label=database_label,
        migrations=expected,
    )


def apply_database(
    database: str | Path,
    *,
    migrations: Iterable[Migration] | None = None,
) -> MigrationPlan:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        return apply_connection(
            connection,
            database_label=str(path),
            migrations=migrations,
        )
