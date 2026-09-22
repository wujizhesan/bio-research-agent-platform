import argparse
import asyncio
import hashlib
import os
from pathlib import Path

import asyncpg


class TenantContextConfigurationError(RuntimeError):
    pass


def _database_url():
    value = os.environ.get('DATABASE_URL', '').strip()
    file_name = os.environ.get('DATABASE_URL_FILE', '').strip()
    if value and file_name:
        raise TenantContextConfigurationError(
            'configure only one DATABASE_URL source'
        )
    if file_name:
        try:
            value = Path(file_name).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise TenantContextConfigurationError(
                'unable to read DATABASE_URL_FILE'
            ) from exc
    if not value:
        raise TenantContextConfigurationError('DATABASE_URL is required')
    return value.replace('postgresql+asyncpg://', 'postgresql://', 1)


def _signing_key(required=True):
    direct = os.environ.get('RLS_CONTEXT_SIGNING_KEY', '').strip()
    file_name = os.environ.get('RLS_CONTEXT_SIGNING_KEY_FILE', '').strip()
    if direct and file_name:
        raise TenantContextConfigurationError(
            'configure only one RLS context signing key source'
        )
    if file_name:
        try:
            direct = Path(file_name).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise TenantContextConfigurationError(
                'unable to read RLS context signing key file'
            ) from exc
    if required and len(direct) < 32:
        raise TenantContextConfigurationError(
            'RLS context signing key must be at least 32 characters'
        )
    return direct


async def configure(enable):
    connection = await asyncpg.connect(_database_url())
    try:
        async with connection.transaction():
            if enable:
                signing_key = _signing_key()
                key_id = os.environ.get(
                    'RLS_CONTEXT_KEY_ID', 'primary'
                ).strip() or 'primary'
                key_digest = hashlib.sha256(
                    signing_key.encode('utf-8')
                ).digest()
                await connection.execute(
                    'INSERT INTO tenant_context_keys '
                    '(key_id, key_digest, active, created_at) '
                    'VALUES ($1, $2, true, statement_timestamp()::text) '
                    'ON CONFLICT (key_id) DO UPDATE SET '
                    'key_digest = EXCLUDED.key_digest, active = true',
                    key_id,
                    key_digest,
                )
                await connection.execute(
                    'UPDATE tenant_context_keys SET active = (key_id = $1)',
                    key_id,
                )
            await connection.execute(
                'UPDATE tenant_context_control '
                'SET enforce_signed = $1, '
                'updated_at = statement_timestamp()::text '
                'WHERE singleton',
                bool(enable),
            )
            state = await connection.fetchrow(
                'SELECT enforce_signed, '
                '(SELECT count(*) FROM tenant_context_keys WHERE active) '
                'AS active_keys '
                'FROM tenant_context_control WHERE singleton'
            )
            if state is None:
                raise TenantContextConfigurationError(
                    'tenant context control row is missing'
                )
            if enable and (
                not state['enforce_signed'] or int(state['active_keys']) != 1
            ):
                raise TenantContextConfigurationError(
                    'signed tenant context was not enabled'
                )
            if not enable and state['enforce_signed']:
                raise TenantContextConfigurationError(
                    'signed tenant context was not disabled'
                )
    finally:
        await connection.close()
    print(
        'signed tenant context enabled'
        if enable else 'signed tenant context compatibility mode enabled'
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--enable', action='store_true')
    mode.add_argument('--disable', action='store_true')
    args = parser.parse_args(argv)
    asyncio.run(configure(args.enable))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
