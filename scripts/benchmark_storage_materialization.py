import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.response import StreamingBody
from s3transfer.manager import TransferManager

from scripts.benchmark_secure_jobs import percentile
from src.storage_workspace import S3ObjectReference
from scripts.benchmark_materialization_quota_baseline import _verified_s3_download


class SimulatedS3Client:
    def __init__(self, payload, config=None):
        self.payload = payload
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self.config = config or TransferConfig()
        model_client = boto3.client(
            's3', region_name='us-east-1',
            aws_access_key_id='test', aws_secret_access_key='test',
        )
        self.meta = model_client.meta
        model_client.close()

    def _check_request(self, kwargs):
        if (
            kwargs.get('VersionId') != 'version-1'
            or kwargs.get('ExpectedBucketOwner') != '123456789012'
        ):
            raise RuntimeError('benchmark transfer lost its version or bucket owner constraint')

    def head_object(self, **kwargs):
        self._check_request(kwargs)
        return {
            'ContentLength': len(self.payload),
            'VersionId': 'version-1',
            'Metadata': {'sha256': self.sha256},
        }

    def get_object(self, Range=None, **kwargs):
        self._check_request(kwargs)
        if Range:
            left, right = Range.removeprefix('bytes=').split('-')
            start = int(left)
            stop = int(right) + 1 if right else len(self.payload)
            data = self.payload[start:stop]
        else:
            data = self.payload
        return {
            'Body': StreamingBody(io.BytesIO(data), len(data)),
            'ContentLength': len(data),
        }

    def download_file(self, bucket, key, filename, ExtraArgs=None):
        with TransferManager(self, config=self.config) as manager:
            manager.download(bucket, key, filename, extra_args=ExtraArgs).result()

    def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
        with TransferManager(self, config=self.config) as manager:
            manager.download(bucket, key, fileobj, extra_args=ExtraArgs).result()


class LegacyS3Client:
    def __init__(self, client):
        self.head_object = client.head_object
        self.download_file = client.download_file


def compare(root, payload, samples):
    client = SimulatedS3Client(payload)
    reference = S3ObjectReference(
        'research-inputs', 'bio-agent/input.bin', 'version-1', client.sha256, len(payload),
    )
    clients = {'reread': LegacyS3Client(client), 'streaming': client}
    timings = {name: [] for name in clients}
    for index in range(-1, samples):
        order = tuple(clients) if index % 2 == 0 else tuple(reversed(clients))
        for name in order:
            target = root / f'{index}-{name}' / 'input.bin'
            started = perf_counter()
            _verified_s3_download(
                reference, target, clients[name], configured_bucket='research-inputs',
                configured_prefix='bio-agent', expected_owner='123456789012',
            )
            elapsed = perf_counter() - started
            actual = hashlib.sha256()
            with target.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    actual.update(chunk)
            if target.stat().st_size != len(payload) or actual.hexdigest() != reference.sha256:
                raise RuntimeError('S3 materialization benchmark produced incorrect file contents')
            target.unlink()
            if index >= 0:
                timings[name].append(elapsed)
    result = {
        name: {
            'median_seconds': round(median(values), 6),
            'p95_seconds': round(percentile(values, 95), 6),
            'seconds': [round(value, 6) for value in values],
        }
        for name, values in timings.items()
    }
    result['comparison'] = {
        'streaming_median_improvement_percent': round(
            100 * (1 - median(timings['streaming']) / median(timings['reread'])), 1,
        ),
        'paired_streaming_wins': sum(
            stream < reread for stream, reread in zip(timings['streaming'], timings['reread'])
        ),
        'paired_samples': samples,
        'verification_reread_bytes_saved_per_download': len(payload),
    }
    return result


def benchmark(samples, sizes, workspace_root=None):
    rows = []
    with tempfile.TemporaryDirectory(
        prefix='storage_materialization_benchmark_', dir=workspace_root,
    ) as raw:
        for size in sizes:
            root = Path(raw) / str(size)
            payload = os.urandom(size * 1024 * 1024)
            rows.append({'size_mib': size, **compare(root, payload, samples)})
    return {
        'scope': 'verified local input materialization using the real classic S3 transfer manager with an in-memory S3 transport; excludes real network, queue, API, and tool execution',
        'transfer_configuration': 'default TransferConfig for both strategies',
        'verification': 'each resulting file is independently reread and SHA-256 checked outside the timed interval',
        'workspace_root': str(Path(workspace_root or tempfile.gettempdir()).resolve()),
        'warmup_pairs_per_size': 1,
        'datasets': rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--sizes-mib', default='1,16,64')
    parser.add_argument('--workspace-root')
    args = parser.parse_args(argv)
    try:
        sizes = tuple(int(item) for item in args.sizes_mib.split(','))
    except ValueError:
        parser.error('sizes-mib must contain positive integers')
    if args.samples < 2 or not sizes or any(size < 1 for size in sizes):
        parser.error('samples must be at least two and sizes must be positive')
    if args.workspace_root is not None and not Path(args.workspace_root).is_dir():
        parser.error('workspace-root must be an existing directory')
    print(json.dumps(benchmark(args.samples, sizes, args.workspace_root), sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
