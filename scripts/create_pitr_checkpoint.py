import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


WAL_FILE = re.compile(r"^[0-9A-F]{24}$")


class PitrError(RuntimeError):
    pass


def _wal_position(value):
    normalized = str(value or "").upper()
    if not WAL_FILE.fullmatch(normalized):
        raise PitrError("PostgreSQL returned an invalid WAL segment")
    return tuple(int(normalized[index:index + 8], 16) for index in range(0, 24, 8))


def _checkpoint_label(release_tag, now):
    release = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(release_tag or "release"))
    release = release.strip("-._")[:48] or "release"
    return f"bioagent_{release}_{now.strftime('%Y%m%dT%H%M%SZ')}"


def _database_url():
    value = os.environ.get("PITR_DATABASE_URL", "").strip()
    if not value:
        raise PitrError("PITR_DATABASE_URL is required")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def atomic_write(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


async def create_pitr_checkpoint(
    connection,
    *,
    release_tag,
    timeout_seconds=60,
    poll_interval=1,
    now=None,
    monotonic=time.monotonic,
    sleep=asyncio.sleep,
):
    current = now or datetime.now(timezone.utc)
    archive_mode = await connection.fetchval("SELECT current_setting('archive_mode')")
    archive_command = await connection.fetchval(
        "SELECT current_setting('archive_command', true)"
    )
    archive_library = await connection.fetchval(
        "SELECT current_setting('archive_library', true)"
    )
    if archive_mode not in {"on", "always"}:
        raise PitrError("PostgreSQL archive_mode must be enabled")
    archive_method = archive_library or archive_command
    if not archive_method or archive_method == "(disabled)":
        raise PitrError("PostgreSQL WAL archive command or library is required")
    if await connection.fetchval("SELECT pg_is_in_recovery()"):
        raise PitrError("PITR checkpoint must run on the writable primary")

    control = await connection.fetchrow(
        "SELECT system_identifier::text FROM pg_control_system()"
    )
    before = await connection.fetchrow(
        "SELECT archived_count, failed_count FROM pg_stat_archiver"
    )
    label = _checkpoint_label(release_tag, current)
    recovery_lsn = await connection.fetchval(
        "SELECT pg_create_restore_point($1)::text",
        label,
    )
    wal_segment = await connection.fetchval(
        "SELECT pg_walfile_name($1::text::pg_lsn)",
        recovery_lsn,
    )
    target_position = _wal_position(wal_segment)
    await connection.fetchval("SELECT pg_switch_wal()::text")

    deadline = monotonic() + max(float(timeout_seconds), 0.1)
    while monotonic() < deadline:
        state = await connection.fetchrow(
            "SELECT archived_count, last_archived_wal, last_archived_time, "
            "failed_count, last_failed_wal FROM pg_stat_archiver"
        )
        if int(state["failed_count"] or 0) > int(before["failed_count"] or 0):
            raise PitrError(
                f"PostgreSQL WAL archiving failed for {state['last_failed_wal'] or 'unknown'}"
            )
        last_archived = state["last_archived_wal"]
        if last_archived and _wal_position(last_archived) >= target_position:
            return {
                "schema_version": 1,
                "status": "passed",
                "release_tag": str(release_tag),
                "restore_point": label,
                "created_at": current.astimezone(timezone.utc).isoformat(),
                "system_identifier": control["system_identifier"],
                "timeline": target_position[0],
                "recovery_lsn": recovery_lsn,
                "wal_segment": str(wal_segment).upper(),
                "last_archived_wal": str(last_archived).upper(),
                "last_archived_at": state["last_archived_time"].astimezone(
                    timezone.utc
                ).isoformat(),
                "archived_count": int(state["archived_count"]),
                "archive_mode": archive_mode,
                "archive_method_sha256": hashlib.sha256(
                    str(archive_method).encode("utf-8")
                ).hexdigest(),
            }
        await sleep(max(float(poll_interval), 0.01))
    raise PitrError("PostgreSQL WAL segment was not archived before the timeout")


async def run(args):
    import asyncpg

    connection = await asyncpg.connect(_database_url())
    try:
        result = await create_pitr_checkpoint(
            connection,
            release_tag=os.environ.get("RELEASE_TAG", "development"),
            timeout_seconds=args.timeout_seconds,
        )
        atomic_write(args.output, result)
        print(json.dumps(result, sort_keys=True))
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("PITR_CHECKPOINT_TIMEOUT_SECONDS", "60")),
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        parser.exit(1, f"PITR checkpoint rejected: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
