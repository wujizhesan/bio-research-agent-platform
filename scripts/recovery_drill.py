import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import asyncpg
import boto3
import redis


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.recovery.yml"
SOURCE_REVISION = "0005_job_observability"
JOB_ID = "recovery-drill-job"
PROJECT_ID = "recovery-drill-project"
FILE_ID = "0123456789abcdef0123456789abcdef"
OBJECT_KEY = f"bio-agent/{FILE_ID}/recovery-input.txt"
OBJECT_CONTENT = b"recovery-drill-scientific-input\n"


def run(command, *, env=None, stdin=None, stdout=None, check=True):
    return subprocess.run(
        [str(item) for item in command],
        cwd=ROOT,
        env=env,
        stdin=stdin,
        stdout=stdout,
        check=check,
    )


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rounded_seconds(started):
    return round(time.monotonic() - started, 3)


def write_json(path, payload):
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def write_summary(backup_dir, payload):
    metrics = payload.get("metrics", {})
    lines = [
        "## Recovery drill",
        "",
        f"- Status: `{payload['status']}`",
        f"- Stage: `{payload.get('stage', 'completed')}`",
        f"- Simulated RPO: `{metrics.get('simulated_rpo_seconds', 'n/a')}s`",
        f"- RTO: `{metrics.get('rto_seconds', 'n/a')}s`",
        f"- Total: `{metrics.get('total_seconds', 'n/a')}s`",
    ]
    if payload.get("error"):
        lines.append(f"- Error: `{payload['error_type']}: {payload['error']}`")
    (backup_dir / "summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def compose_command(project, *arguments):
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        COMPOSE_FILE,
        *arguments,
    ]


def recovery_environment(args):
    environment = os.environ.copy()
    environment.update(
        {
            "AUTO_CREATE_SCHEMA": "false",
            "DATABASE_URL": (
                "postgresql+asyncpg://bioagent:recovery-only-password@"
                f"127.0.0.1:{args.postgres_port}/bioagent_recovery"
            ),
            "RECOVERY_POSTGRES_PORT": str(args.postgres_port),
            "RECOVERY_REDIS_PORT": str(args.redis_port),
            "RECOVERY_S3_PORT": str(args.s3_port),
        }
    )
    return environment


def database_url(args):
    return (
        "postgresql://bioagent:recovery-only-password@"
        f"127.0.0.1:{args.postgres_port}/bioagent_recovery"
    )


def s3_client(args):
    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{args.s3_port}",
        region_name="us-east-1",
        aws_access_key_id="recovery-access-key",
        aws_secret_access_key="recovery-secret-key",
    )


def redis_client(args):
    return redis.Redis(
        host="127.0.0.1",
        port=args.redis_port,
        decode_responses=True,
    )


async def ensure_database_roles(args):
    connection = await asyncpg.connect(database_url(args))
    try:
        await connection.execute(
            """
            DO $roles$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'bioagent_api'
                ) THEN
                    CREATE ROLE bioagent_api NOLOGIN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'bioagent_dispatcher'
                ) THEN
                    CREATE ROLE bioagent_dispatcher NOLOGIN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'bioagent_worker'
                ) THEN
                    CREATE ROLE bioagent_worker NOLOGIN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'bioagent_maintenance'
                ) THEN
                    CREATE ROLE bioagent_maintenance NOLOGIN;
                END IF;
            END
            $roles$;
            """
        )
    finally:
        await connection.close()


async def seed_database(args):
    connection = await asyncpg.connect(database_url(args))
    try:
        await connection.execute(
            """
            INSERT INTO projects (
                project_id, name, description, owner_subject, created_at
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            PROJECT_ID,
            "Recovery drill project",
            "Synthetic recovery validation",
            "recovery-drill",
            "2026-09-13T00:00:00+00:00",
        )
        await connection.execute(
            """
            INSERT INTO project_members (
                project_id, subject, role, created_at
            ) VALUES ($1, $2, $3, $4)
            """,
            PROJECT_ID,
            "recovery-drill",
            "owner",
            "2026-09-13T00:00:00+00:00",
        )
        await connection.execute(
            """
            INSERT INTO job_records (
                job_id, tool, status, created_at, finished_at, arguments,
                result, attempts, cancel_requested, resources, priority,
                trace_id, request_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6::json, $7::json, $8, $9,
                $10::json, $11, $12, $13
            )
            """,
            JOB_ID,
            "research_catalog",
            "completed",
            "2026-09-13T00:00:00+00:00",
            "2026-09-13T00:01:00+00:00",
            '{"query":"recovery"}',
            '{"status":"verified"}',
            1,
            False,
            '{"cpu_cores":1,"memory_mb":256}',
            10,
            "recovery-trace",
            "recovery-request",
        )
        await connection.execute(
            """
            INSERT INTO job_projects (job_id, project_id, created_at)
            VALUES ($1, $2, $3)
            """,
            JOB_ID,
            PROJECT_ID,
            "2026-09-13T00:00:00+00:00",
        )
        await connection.execute(
            """
            INSERT INTO file_projects (file_id, project_id, created_at)
            VALUES ($1, $2, $3)
            """,
            FILE_ID,
            PROJECT_ID,
            "2026-09-13T00:00:00+00:00",
        )
    finally:
        await connection.close()


def seed_object_storage(args):
    client = s3_client(args)
    client.create_bucket(Bucket=args.bucket)
    client.put_object(
        Bucket=args.bucket,
        Key=OBJECT_KEY,
        Body=OBJECT_CONTENT,
        ContentType="text/plain",
        Metadata={
            "file-id": FILE_ID,
            "sha256": hashlib.sha256(OBJECT_CONTENT).hexdigest(),
            "security-status": "clean",
        },
    )


def seed_redis(args):
    client = redis_client(args)
    try:
        client.set("bio-recovery:ephemeral-job", JOB_ID)
        client.lpush("bio-recovery:queue", JOB_ID)
    finally:
        client.close()


def backup_database(args, backup_dir):
    dump_path = backup_dir / "postgres.dump"
    command = compose_command(
        args.project,
        "exec",
        "--no-TTY",
        "db",
        "pg_dump",
        "--username",
        "bioagent",
        "--dbname",
        "bioagent_recovery",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
    )
    with dump_path.open("wb") as output:
        run(command, stdout=output)
    return dump_path


def backup_objects(args, backup_dir):
    client = s3_client(args)
    objects_dir = backup_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.bucket):
        for item in page.get("Contents", []):
            key = item["Key"]
            response = client.get_object(Bucket=args.bucket, Key=key)
            content = response["Body"].read()
            filename = hashlib.sha256(key.encode("utf-8")).hexdigest()
            target = objects_dir / filename
            target.write_bytes(content)
            entries.append(
                {
                    "key": key,
                    "file": f"objects/{filename}",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_type": response.get("ContentType"),
                    "metadata": response.get("Metadata", {}),
                }
            )
    if not entries:
        raise RuntimeError("object storage backup is empty")
    return entries


def destroy_state(args):
    run(
        compose_command(
            args.project,
            "exec",
            "--no-TTY",
            "db",
            "psql",
            "--username",
            "bioagent",
            "--dbname",
            "bioagent_recovery",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
        )
    )
    client = s3_client(args)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=args.bucket, Delete={"Objects": objects})
    cache = redis_client(args)
    try:
        cache.flushdb()
    finally:
        cache.close()


async def assert_destroyed(args):
    connection = await asyncpg.connect(database_url(args))
    try:
        table = await connection.fetchval("SELECT to_regclass('public.job_records')")
        if table is not None:
            raise RuntimeError("database destruction check failed")
    finally:
        await connection.close()
    objects = s3_client(args).list_objects_v2(Bucket=args.bucket).get("Contents", [])
    if objects:
        raise RuntimeError("object storage destruction check failed")
    cache = redis_client(args)
    try:
        if cache.dbsize() != 0:
            raise RuntimeError("Redis destruction check failed")
    finally:
        cache.close()


def restore_database(args, dump_path):
    command = compose_command(
        args.project,
        "exec",
        "--no-TTY",
        "db",
        "pg_restore",
        "--username",
        "bioagent",
        "--dbname",
        "bioagent_recovery",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
    )
    with dump_path.open("rb") as source:
        run(command, stdin=source)


def restore_objects(args, backup_dir, entries):
    client = s3_client(args)
    for entry in entries:
        source = (backup_dir / entry["file"]).resolve()
        source.relative_to(backup_dir.resolve())
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"object backup checksum mismatch: {entry['key']}")
        client.put_object(
            Bucket=args.bucket,
            Key=entry["key"],
            Body=content,
            ContentType=entry.get("content_type") or "application/octet-stream",
            Metadata=entry.get("metadata") or {},
        )


async def verify_restored_database(args):
    connection = await asyncpg.connect(database_url(args))
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        row = await connection.fetchrow(
            """
            SELECT status, result, trace_id, request_id, run_context
            FROM job_records WHERE job_id = $1
            """,
            JOB_ID,
        )
        project = await connection.fetchval(
            "SELECT project_id FROM job_projects WHERE job_id = $1",
            JOB_ID,
        )
        file_project = await connection.fetchval(
            "SELECT project_id FROM file_projects WHERE file_id = $1",
            FILE_ID,
        )
        if not revision or revision == SOURCE_REVISION:
            raise RuntimeError(f"restored database was not migrated to a newer head: {revision}")
        if row is None or row["status"] != "completed":
            raise RuntimeError("durable job state was not restored")
        result = json.loads(row["result"]) if isinstance(row["result"], str) else row["result"]
        if result != {"status": "verified"}:
            raise RuntimeError("job result changed during restore")
        if row["trace_id"] != "recovery-trace" or row["request_id"] != "recovery-request":
            raise RuntimeError("job observability state changed during restore")
        if row["run_context"] is not None:
            raise RuntimeError("post-restore migration produced invalid run_context")
        if project != PROJECT_ID or file_project != PROJECT_ID:
            raise RuntimeError("project ownership state was not restored")
        return revision
    finally:
        await connection.close()


def verify_restored_objects(args):
    response = s3_client(args).get_object(Bucket=args.bucket, Key=OBJECT_KEY)
    content = response["Body"].read()
    if content != OBJECT_CONTENT:
        raise RuntimeError("restored object content mismatch")
    expected = hashlib.sha256(OBJECT_CONTENT).hexdigest()
    if response.get("Metadata", {}).get("sha256") != expected:
        raise RuntimeError("restored object metadata mismatch")
    return expected


def verify_redis_is_disposable(args):
    client = redis_client(args)
    try:
        if client.dbsize() != 0:
            raise RuntimeError("Redis data was unexpectedly restored")
    finally:
        client.close()


def execute(args):
    if not re.fullmatch(r"bio-recovery-[a-z0-9-]+", args.project):
        raise ValueError("recovery project must use the bio-recovery- prefix")
    backup_dir = Path(args.output).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("compose.log", "failure.json", "report.json", "summary.md"):
        (backup_dir / filename).unlink(missing_ok=True)
    started = time.monotonic()
    stage = "service_start"
    metrics = {}
    environment = recovery_environment(args)
    try:
        stage_started = time.monotonic()
        run(compose_command(args.project, "up", "--detach", "--wait"), env=environment)
        metrics["service_start_seconds"] = rounded_seconds(stage_started)
        stage = "role_bootstrap"
        asyncio.run(ensure_database_roles(args))
        stage = "seed"
        stage_started = time.monotonic()
        run(
            [sys.executable, "-m", "alembic", "upgrade", SOURCE_REVISION],
            env=environment,
        )
        asyncio.run(seed_database(args))
        seed_object_storage(args)
        seed_redis(args)
        metrics["seed_seconds"] = rounded_seconds(stage_started)
        stage = "backup"
        stage_started = time.monotonic()
        recovery_point_at = datetime.now(timezone.utc)
        dump_path = backup_database(args, backup_dir)
        object_entries = backup_objects(args, backup_dir)
        metrics["backup_seconds"] = rounded_seconds(stage_started)
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": {
                "source_revision": SOURCE_REVISION,
                "file": dump_path.name,
                "sha256": digest_file(dump_path),
            },
            "objects": object_entries,
            "redis": {"backed_up": False, "reason": "disposable queue and cache"},
        }
        write_json(backup_dir / "manifest.json", manifest)
        stage = "destruction"
        stage_started = time.monotonic()
        incident_at = datetime.now(timezone.utc)
        incident_started = time.monotonic()
        destroy_state(args)
        asyncio.run(assert_destroyed(args))
        metrics["destruction_seconds"] = rounded_seconds(stage_started)
        if digest_file(dump_path) != manifest["database"]["sha256"]:
            raise RuntimeError("database backup checksum mismatch")
        stage = "restore"
        stage_started = time.monotonic()
        restore_database(args, dump_path)
        restore_objects(args, backup_dir, object_entries)
        metrics["restore_seconds"] = rounded_seconds(stage_started)
        stage = "migration"
        stage_started = time.monotonic()
        run([sys.executable, "-m", "alembic", "upgrade", "head"], env=environment)
        run([sys.executable, "-m", "alembic", "current", "--check-heads"], env=environment)
        metrics["migration_seconds"] = rounded_seconds(stage_started)
        stage = "validation"
        stage_started = time.monotonic()
        restored_revision = asyncio.run(verify_restored_database(args))
        object_sha256 = verify_restored_objects(args)
        verify_redis_is_disposable(args)
        metrics["validation_seconds"] = rounded_seconds(stage_started)
        metrics["simulated_rpo_seconds"] = round(
            (incident_at - recovery_point_at).total_seconds(),
            3,
        )
        metrics["rto_seconds"] = rounded_seconds(incident_started)
        metrics["total_seconds"] = rounded_seconds(started)
        stage = "budget_validation"
        if metrics["simulated_rpo_seconds"] > args.max_rpo_seconds:
            raise RuntimeError(
                "simulated RPO exceeded budget: "
                f"{metrics['simulated_rpo_seconds']}s > {args.max_rpo_seconds}s"
            )
        if metrics["rto_seconds"] > args.max_rto_seconds:
            raise RuntimeError(
                f"RTO exceeded budget: {metrics['rto_seconds']}s > {args.max_rto_seconds}s"
            )
        report = {
            "status": "passed",
            "scenario": "synthetic",
            "source_revision": SOURCE_REVISION,
            "restored_revision": restored_revision,
            "database_sha256": manifest["database"]["sha256"],
            "object_count": len(object_entries),
            "object_sha256": object_sha256,
            "redis_restored": False,
            "recovery_point_at": recovery_point_at.isoformat(),
            "incident_at": incident_at.isoformat(),
            "budgets": {
                "max_rpo_seconds": args.max_rpo_seconds,
                "max_rto_seconds": args.max_rto_seconds,
            },
            "metrics": metrics,
            "duration_seconds": metrics["total_seconds"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(backup_dir / "report.json", report)
        write_summary(backup_dir, report)
        print(json.dumps(report, sort_keys=True))
    except Exception as error:
        metrics["total_seconds"] = rounded_seconds(started)
        failure = {
            "status": "failed",
            "scenario": "synthetic",
            "stage": stage,
            "error_type": type(error).__name__,
            "error": str(error),
            "budgets": {
                "max_rpo_seconds": args.max_rpo_seconds,
                "max_rto_seconds": args.max_rto_seconds,
            },
            "metrics": metrics,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(backup_dir / "failure.json", failure)
        write_summary(backup_dir, failure)
        raise
    finally:
        if not args.keep:
            run(
                compose_command(
                    args.project,
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ),
                env=environment,
                check=False,
            )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--project", default="bio-recovery-local")
    parser.add_argument("--postgres-port", type=int, default=15432)
    parser.add_argument("--redis-port", type=int, default=16379)
    parser.add_argument("--s3-port", type=int, default=19000)
    parser.add_argument("--bucket", default="bioagent-recovery")
    parser.add_argument("--max-rpo-seconds", type=float, default=900)
    parser.add_argument("--max-rto-seconds", type=float, default=60)
    parser.add_argument("--keep", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    execute(parse_args())
