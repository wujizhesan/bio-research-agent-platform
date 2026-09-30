import hashlib
import os
from pathlib import Path

from src.storage_workspace import (
    S3ObjectReference, StorageIntegrityError, _ChecksumWriter,
    _s3_client_from_env, _safe_filename,
)


BASELINE_SOURCE_COMMIT = '5f02fd5981294ecbfc19c3abcfa2887ddcf661c5'
CHUNK_SIZE = 1024 * 1024


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
        download_fileobj = getattr(client, 'download_fileobj', None)
        if callable(download_fileobj):
            with target.open('xb') as destination:
                writer = _ChecksumWriter(destination, reference.size_bytes)
                download_fileobj(
                    reference.bucket, reference.key, writer, ExtraArgs=extra_args,
                )
            digest = writer.digest
            size = writer.size_bytes
        else:
            client.download_file(
                reference.bucket, reference.key, str(target), ExtraArgs=extra_args,
            )
            digest = hashlib.sha256()
            size = 0
            with target.open('rb') as source:
                for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                    size += len(chunk)
                    digest.update(chunk)
        if size != reference.size_bytes or target.stat().st_size != reference.size_bytes:
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
