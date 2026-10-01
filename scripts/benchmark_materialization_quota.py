import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
from statistics import median
import tempfile
from threading import Lock
from time import perf_counter

from botocore.response import StreamingBody

from scripts.benchmark_materialization_quota_baseline import (
    BASELINE_SOURCE_COMMIT, materialize_storage_references as legacy_materialize,
)
from scripts.benchmark_storage_materialization import SimulatedS3Client
from src.plugin_container import _tree_size
from src.storage_workspace import (
    S3ObjectReference, StorageQuotaExceededError, materialize_storage_references,
)


class CountingStream(io.BytesIO):
    def __init__(self, payload, client):
        super().__init__(payload)
        self.client = client

    def read(self, size=-1):
        data = super().read(size)
        with self.client.counter_lock:
            self.client.downloaded_bytes += len(data)
        return data


class CountingS3Client(SimulatedS3Client):
    def __init__(self, payload):
        super().__init__(payload)
        self.counter_lock = Lock()
        self.head_calls = 0
        self.get_calls = 0
        self.download_calls = 0
        self.downloaded_bytes = 0

    def head_object(self, **kwargs):
        with self.counter_lock:
            self.head_calls += 1
        return super().head_object(**kwargs)

    def get_object(self, Range=None, **kwargs):
        response = super().get_object(Range=Range, **kwargs)
        data = response['Body'].read()
        response['Body'].close()
        with self.counter_lock:
            self.get_calls += 1
        response['Body'] = StreamingBody(CountingStream(data, self), len(data))
        return response

    def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
        with self.counter_lock:
            self.download_calls += 1
        return super().download_fileobj(bucket, key, fileobj, ExtraArgs=ExtraArgs)


def run(root, scenario, strategy, client, instrument=False):
    payload = client.payload
    references = [S3ObjectReference(
        'research-inputs', f'bio-agent/{index}.bin', 'version-1', client.sha256,
        len(payload),
    ).serialize() for index in range(scenario['objects'])]
    values = {
        'inputs': references,
        'repeat': tuple(references[0] for _ in range(scenario.get('repetitions', 2))),
        'other': {'local': 'local.txt', 'integer': 3, 'null': None},
    }
    quota = scenario['quota_mib'] * 1024 * 1024
    should_reject = len(references) * len(payload) > quota
    count = min(len(references), quota // len(payload)) if strategy == 'bounded' else len(references)
    expected = {}
    for value in references[:count]:
        reference = S3ObjectReference.parse(value)
        identity = hashlib.sha256(value.encode()).hexdigest()[:24]
        expected[f'materialized/{identity}/{Path(reference.key).name}'] = reference.sha256
    function = legacy_materialize if strategy == 'legacy' else materialize_storage_references
    options = {'max_bytes': quota} if strategy == 'bounded' else {}
    result = None
    with tempfile.TemporaryDirectory(prefix=strategy + '-', dir=root) as raw:
        workspace = Path(raw)
        started = perf_counter()
        try:
            result = function(
                values, workspace / 'materialized', client=client,
                configured_bucket='research-inputs', configured_prefix='bio-agent',
                expected_owner='123456789012', **options,
            )
            if _tree_size(workspace) > quota:
                raise StorageQuotaExceededError('materialized input quota exceeded')
        except StorageQuotaExceededError:
            rejected = True
        else:
            rejected = False
        elapsed = perf_counter() - started
        if rejected != should_reject:
            raise RuntimeError('materialization quota decision differs from expected result')
        actual = {}
        written_bytes = 0
        for path in workspace.rglob('*'):
            if path.is_file():
                written_bytes += path.stat().st_size
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(chunk)
                actual[path.relative_to(workspace).as_posix()] = digest.hexdigest()
        if actual != expected or written_bytes != count * len(payload):
            raise RuntimeError('downloaded file inventory, bytes or hashes differ from expected result')
        if not rejected:
            resolved = {
                value: str(workspace / relative)
                for value, relative in zip(references, expected)
            }
            if result != {
                'inputs': [resolved[value] for value in references],
                'repeat': tuple(resolved[references[0]] for _ in values['repeat']),
                'other': values['other'],
            }:
                raise RuntimeError('materialization changed nested values or duplicate mappings')
        if instrument:
            if client.download_calls != count or client.downloaded_bytes != written_bytes:
                raise RuntimeError('transport counters differ from verified file sizes')
            return {
                'head_calls': client.head_calls, 'get_calls': client.get_calls,
                'download_calls': client.download_calls,
                'downloaded_bytes': client.downloaded_bytes, 'written_bytes': written_bytes,
                'rejected': rejected,
            }
    return elapsed


def compare(root, scenario, samples):
    payload = bytes(range(256)) * (scenario['size_mib'] * 1024 * 1024 // 256)
    counters = {
        name: run(root, scenario, name, CountingS3Client(payload), instrument=True)
        for name in ('legacy', 'bounded')
    }
    clients = {name: SimulatedS3Client(payload) for name in counters}
    for name in clients:
        run(root, scenario, name, clients[name])
    pairs = []
    for index in range(samples):
        order = tuple(clients) if index % 2 == 0 else tuple(reversed(clients))
        pairs.append({name: run(root, scenario, name, clients[name]) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['bounded'] for pair in pairs)
    return {
        **scenario, 'paired_samples': samples, 'legacy_median_seconds': before,
        'bounded_median_seconds': after, 'elapsed_change_percent': (after / before - 1) * 100,
        'bounded_faster_count': sum(pair['bounded'] < pair['legacy'] for pair in pairs),
        'io_counts_outside_timing': counters,
        'downloaded_and_written_bytes_saved': counters['legacy']['written_bytes'] - counters['bounded']['written_bytes'],
        'pairs': pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    scenarios = [
        {'scenario': 'exact_1mib', 'size_mib': 1, 'objects': 1, 'quota_mib': 1},
        {'scenario': 'exact_16mib', 'size_mib': 16, 'objects': 1, 'quota_mib': 16},
        {'scenario': 'oversized_16mib', 'size_mib': 16, 'objects': 1, 'quota_mib': 8},
        {'scenario': 'aggregate_8x1mib', 'size_mib': 1, 'objects': 8, 'quota_mib': 4},
        {'scenario': 'duplicate_1mib', 'size_mib': 1, 'objects': 1, 'quota_mib': 1, 'repetitions': 64},
    ]
    with tempfile.TemporaryDirectory(prefix='materialization-quota-', dir=arguments.workspace_root.resolve()) as raw:
        rows = [compare(Path(raw), scenario, arguments.samples) for scenario in scenarios]
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'Verified S3 input materialization and the final workspace size check using the real classic S3 transfer manager with an in-memory transport; excludes real S3 network, local input staging, container execution, HTTP and queue',
        'transfer_configuration': 'default TransferConfig for both strategies',
        'decisions_nested_values_duplicate_mappings_bytes_and_hashes_verified': True,
        'rejection_inventory': 'legacy downloads all objects; bounded downloads only the prefix fitting the quota',
        'timed_samples_instrumented': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_cleanup_outside_timing': True,
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
