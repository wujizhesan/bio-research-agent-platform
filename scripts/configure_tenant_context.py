import argparse
import asyncio
import hashlib
import os
import re
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
    expected_digest = os.environ.get('RLS_CONTEXT_SIGNING_KEY_SHA256', '').strip()
    if direct and file_name:
        raise TenantContextConfigurationError(
            'configure only one RLS context signing key source'
        )
    if file_name:
        try:
            raw = Path(file_name).read_bytes()
        except OSError as exc:
            raise TenantContextConfigurationError(
                'unable to read RLS context signing key file'
            ) from exc
        if len(raw) > 65536:
            raise TenantContextConfigurationError(
                'RLS context signing key file exceeds 65536 bytes'
            )
        if expected_digest and (
            re.fullmatch(r'[0-9a-f]{64}', expected_digest) is None
            or hashlib.sha256(raw).hexdigest() != expected_digest
        ):
            raise TenantContextConfigurationError(
                'RLS context signing key file checksum mismatch'
            )
        direct = raw.decode('utf-8').strip()
    elif expected_digest:
        raise TenantContextConfigurationError(
            'RLS context signing key checksum requires a file'
        )
    if required and len(direct) < 32:
        raise TenantContextConfigurationError(
            'RLS context signing key must be at least 32 characters'
        )
    return direct


async def configure(enable, *, stage=False):
    connection = await asyncpg.connect(_database_url())
    try:
        async with connection.transaction():
            control = await connection.fetchrow(
                'SELECT enforce_signed FROM tenant_context_control '
                'WHERE singleton FOR UPDATE'
            )
            if control is None:
                raise TenantContextConfigurationError(
                    'tenant context control row is missing'
                )
            if enable or stage:
                signing_key = _signing_key()
                key_id = os.environ.get(
                    'RLS_CONTEXT_KEY_ID', 'primary'
                ).strip() or 'primary'
                if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', key_id) is None:
                    raise TenantContextConfigurationError('invalid RLS context key ID')
                try:
                    grace_seconds = int(os.environ.get(
                        'RLS_CONTEXT_ROTATION_GRACE_SECONDS', '3600'
                    ))
                except ValueError as exc:
                    raise TenantContextConfigurationError(
                        'invalid RLS context rotation grace period'
                    ) from exc
                if not 300 <= grace_seconds <= 86400:
                    raise TenantContextConfigurationError(
                        'RLS context rotation grace period must be 300-86400 seconds'
                    )
                key_digest = hashlib.sha256(
                    signing_key.encode('utf-8')
                ).digest()
                existing = await connection.fetchrow(
                    'SELECT key_digest FROM tenant_context_keys '
                    'WHERE key_id = $1 FOR UPDATE',
                    key_id,
                )
                if existing is not None and bytes(existing['key_digest']) != key_digest:
                    raise TenantContextConfigurationError(
                        'RLS context key ID cannot be reused with different material'
                    )
                await connection.execute(
                    'UPDATE tenant_context_keys SET active = false '
                    'WHERE active AND valid_until IS NOT NULL '
                    'AND valid_until <= EXTRACT(EPOCH FROM clock_timestamp())'
                )
                active_ids = await connection.fetch(
                    'SELECT key_id FROM tenant_context_keys WHERE active FOR UPDATE'
                )
                if key_id not in {row['key_id'] for row in active_ids} and len(active_ids) > 1:
                    raise TenantContextConfigurationError(
                        'retire an earlier RLS key before starting another rotation'
                    )
                await connection.execute(
                    'UPDATE tenant_context_keys SET valid_until = '
                    'CASE WHEN valid_until IS NULL THEN '
                    'EXTRACT(EPOCH FROM clock_timestamp()) + $2 '
                    'ELSE LEAST(valid_until, EXTRACT(EPOCH FROM clock_timestamp()) + $2) '
                    'END WHERE active AND key_id <> $1',
                    key_id,
                    grace_seconds,
                )
                await connection.execute(
                    'INSERT INTO tenant_context_keys '
                    '(key_id, key_digest, active, valid_until, created_at) '
                    'VALUES ($1, $2, true, NULL, statement_timestamp()::text) '
                    'ON CONFLICT (key_id) DO UPDATE SET '
                    'active = true, valid_until = NULL',
                    key_id,
                    key_digest,
                )
            if not stage:
                await connection.execute(
                    'UPDATE tenant_context_control '
                    'SET enforce_signed = $1, '
                    'updated_at = statement_timestamp()::text '
                    'WHERE singleton',
                    bool(enable),
                )
            state = await connection.fetchrow(
                'SELECT enforce_signed, '
                '(SELECT count(*) FROM tenant_context_keys WHERE active '
                'AND (valid_until IS NULL OR valid_until > '
                'EXTRACT(EPOCH FROM clock_timestamp()))) '
                'AS active_keys '
                'FROM tenant_context_control WHERE singleton'
            )
            if state is None or (
                (enable or stage) and int(state['active_keys']) not in {1, 2}
            ):
                raise TenantContextConfigurationError('invalid active RLS key set')
            if enable and not state['enforce_signed']:
                raise TenantContextConfigurationError(
                    'signed tenant context was not enabled'
                )
            if not enable and not stage and state['enforce_signed']:
                raise TenantContextConfigurationError(
                    'signed tenant context was not disabled'
                )
    finally:
        await connection.close()
    if enable:
        print('signed tenant context enabled')
    elif stage:
        print('signed tenant context key staged')
    else:
        print('signed tenant context compatibility mode enabled')


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--enable', action='store_true')
    mode.add_argument('--disable', action='store_true')
    mode.add_argument('--stage', action='store_true')
    args = parser.parse_args(argv)
    asyncio.run(configure(args.enable, stage=args.stage))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
