import asyncio
import os
from pathlib import Path

import asyncpg


class DatabaseRoleError(RuntimeError):
    pass


def _url(name):
    value = os.environ.get(name, '').strip()
    file_name = os.environ.get(f'{name}_FILE', '').strip()
    if value and file_name:
        raise DatabaseRoleError(f'configure only one of {name} or {name}_FILE')
    if file_name:
        try:
            value = Path(file_name).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise DatabaseRoleError(f'unable to read {name}_FILE') from exc
    if not value:
        raise DatabaseRoleError(f'{name} is required')
    return value.replace('postgresql+asyncpg://', 'postgresql://', 1)


async def _verify(url_name, expected_role, forbidden_roles):
    connection = await asyncpg.connect(_url(url_name))
    try:
        row = await connection.fetchrow(
            "SELECT current_user AS username, "
            "pg_has_role(current_user, $1, 'member') AS member, "
            "rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
            expected_role,
        )
        if row is None or not row['member']:
            raise DatabaseRoleError(
                f'{url_name} user must be a member of {expected_role}'
            )
        if row['rolsuper'] or row['rolbypassrls']:
            raise DatabaseRoleError(
                f'{url_name} user must not bypass row-level security'
            )
        for forbidden_role in forbidden_roles:
            forbidden = await connection.fetchval(
                "SELECT pg_has_role(current_user, $1, 'member')",
                forbidden_role,
            )
            if forbidden:
                raise DatabaseRoleError(
                    f'{url_name} user must not be a member of {forbidden_role}'
                )
        group = await connection.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = $1",
            expected_role,
        )
        if group is None or group['rolsuper'] or group['rolbypassrls']:
            raise DatabaseRoleError(
                f'{expected_role} must exist without RLS bypass privileges'
            )
        owned_tables = await connection.fetchval(
            "SELECT count(*) FROM pg_class table_ref "
            "JOIN pg_namespace namespace_ref ON namespace_ref.oid = table_ref.relnamespace "
            "WHERE namespace_ref.nspname = 'public' "
            "AND table_ref.relkind IN ('r', 'p') "
            "AND table_ref.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
        )
        if owned_tables:
            raise DatabaseRoleError(
                f'{url_name} user must not own application tables'
            )
        return row['username']
    finally:
        await connection.close()


async def main():
    api_user, dispatcher_user, worker_user, maintenance_user = await asyncio.gather(
        _verify(
            'API_DATABASE_URL', 'bioagent_api',
            ('bioagent_dispatcher', 'bioagent_worker', 'bioagent_maintenance'),
        ),
        _verify(
            'DISPATCHER_DATABASE_URL', 'bioagent_dispatcher',
            ('bioagent_api', 'bioagent_worker', 'bioagent_maintenance'),
        ),
        _verify(
            'WORKER_DATABASE_URL', 'bioagent_worker',
            ('bioagent_api', 'bioagent_dispatcher', 'bioagent_maintenance'),
        ),
        _verify(
            'MAINTENANCE_DATABASE_URL', 'bioagent_maintenance',
            ('bioagent_api', 'bioagent_dispatcher', 'bioagent_worker'),
        ),
    )
    if len({api_user, dispatcher_user, worker_user, maintenance_user}) != 4:
        raise DatabaseRoleError(
            'API, Dispatcher, Worker and Maintenance must use distinct database users'
        )
    print(
        'database roles verified: '
        f'api={api_user} dispatcher={dispatcher_user} worker={worker_user} '
        f'maintenance={maintenance_user}'
    )


if __name__ == '__main__':
    asyncio.run(main())
