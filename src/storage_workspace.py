"""Version-locked object references and ephemeral job input workspaces."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

CHUNK_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r'^[a-f0-9]{64}$')
S3_REFERENCE_SCHEME = 'bio+s3'


class StorageIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class S3ObjectReference:
    bucket: str
    key: str
    version_id: str
    sha256: str
    size_bytes: int

    def serialize(self):
        query = urlencode({
            'versionId': self.version_id,
            'sha256': self.sha256,
            'size': self.size_bytes,
        })
        return f'{S3_REFERENCE_SCHEME}://{quote(self.bucket, safe="")}/{quote(self.key, safe="/")}?{query}'

    @classmethod
    def parse(cls, value):
        parsed = urlparse(str(value))
        if parsed.scheme != S3_REFERENCE_SCHEME:
            return None
        query = parse_qs(parsed.query, strict_parsing=True)
        try:
            bucket = unquote(parsed.netloc)
            key = unquote(parsed.path.lstrip('/'))
            version_id = query['versionId'][0]
            sha256 = query['sha256'][0].lower()
            size_bytes = int(query['size'][0])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise StorageIntegrityError('invalid versioned storage reference') from exc
        if not bucket or not key or not version_id or version_id == 'null':
            raise StorageIntegrityError('storage reference is not version locked')
        if not SHA256_PATTERN.fullmatch(sha256) or size_bytes < 1:
            raise StorageIntegrityError('storage reference has invalid integrity metadata')
        return cls(bucket, key, version_id, sha256, size_bytes)


def _s3_client_from_env():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError('boto3 is required to materialize S3 inputs') from exc
    return boto3.client(
        's3',
        endpoint_url=os.environ.get('S3_ENDPOINT_URL') or None,
        region_name=os.environ.get('S3_REGION') or None,
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID') or None,
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY') or None,
        aws_session_token=os.environ.get('AWS_SESSION_TOKEN') or None,
    )


def _safe_filename(key):
    candidate = Path(key).name
    candidate = re.sub(r'[^A-Za-z0-9._-]+', '_', candidate).strip(' .')
    if not candidate or candidate in {'.', '..'}:
        candidate = 'input'
    if candidate.startswith('.'):
        candidate = f'input{candidate}'
    return candidate[:180]


def _verified_s3_download(
    reference,
    target,
    client,
    *,
    configured_bucket=None,
    configured_prefix=None,
    expected_owner=None,
):
    configured_bucket = (
        os.environ.get('S3_BUCKET', '').strip()
        if configured_bucket is None else str(configured_bucket).strip()
    )
    if not configured_bucket or reference.bucket != configured_bucket:
        raise StorageIntegrityError('storage reference bucket is not allowed')
    configured_prefix = (
        os.environ.get('S3_PREFIX', 'bio-agent').strip('/')
        if configured_prefix is None else str(configured_prefix).strip('/')
    )
    if configured_prefix and not reference.key.startswith(f'{configured_prefix}/'):
        raise StorageIntegrityError('storage reference key is outside the configured prefix')
    request = {
        'Bucket': reference.bucket,
        'Key': reference.key,
        'VersionId': reference.version_id,
    }
    expected_owner = (
        os.environ.get('S3_EXPECTED_BUCKET_OWNER', '').strip()
        if expected_owner is None else str(expected_owner).strip()
    )
    if expected_owner:
        request['ExpectedBucketOwner'] = expected_owner
    try:
        head = client.head_object(**request)
    except Exception as exc:
        raise StorageIntegrityError('versioned input object is unavailable') from exc
    remote_version = str(head.get('VersionId') or '')
    remote_sha256 = str((head.get('Metadata') or {}).get('sha256') or '').lower()
    remote_size = int(head.get('ContentLength') or 0)
    if remote_version != reference.version_id:
        raise StorageIntegrityError('input object version does not match its reference')
    if remote_sha256 != reference.sha256:
        raise StorageIntegrityError('input object metadata checksum does not match its reference')
    if remote_size != reference.size_bytes:
        raise StorageIntegrityError('input object size does not match its reference')
    target.parent.mkdir(parents=True, exist_ok=False)
    extra_args = {'VersionId': reference.version_id}
    if expected_owner:
        extra_args['ExpectedBucketOwner'] = expected_owner
    try:
        client.download_file(
            reference.bucket,
            reference.key,
            str(target),
            ExtraArgs=extra_args,
        )
        digest = hashlib.sha256()
        with target.open('rb') as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                digest.update(chunk)
        if target.stat().st_size != reference.size_bytes:
            raise StorageIntegrityError('downloaded input size verification failed')
        if digest.hexdigest() != reference.sha256:
            raise StorageIntegrityError('downloaded input checksum verification failed')
    except StorageIntegrityError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise StorageIntegrityError('versioned input download failed') from exc
    return target


def materialize_storage_references(
    value,
    workspace,
    client=None,
    *,
    configured_bucket=None,
    configured_prefix=None,
    expected_owner=None,
):
    workspace = Path(workspace)
    client_holder = [client]
    materialized = {}

    def materialize(item):
        if isinstance(item, str):
            reference = S3ObjectReference.parse(item)
            if reference is None:
                return item
            if item in materialized:
                return materialized[item]
            if client_holder[0] is None:
                client_holder[0] = _s3_client_from_env()
            safe_name = _safe_filename(reference.key)
            identity = hashlib.sha256(item.encode('utf-8')).hexdigest()[:24]
            target = workspace / identity / safe_name
            materialized[item] = str(_verified_s3_download(
                reference,
                target,
                client_holder[0],
                configured_bucket=configured_bucket,
                configured_prefix=configured_prefix,
                expected_owner=expected_owner,
            ))
            return materialized[item]
        if isinstance(item, dict):
            return {key: materialize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [materialize(child) for child in item]
        if isinstance(item, tuple):
            return tuple(materialize(child) for child in item)
        return item

    return materialize(value)
