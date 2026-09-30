import argparse
import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter

from src.file_security import (
    ContentDisarmReconstructor, FileSecurityError, TEXT_CONTROL_BATCH_CHARS,
)


ALLOWED_TEXT_CONTROLS = frozenset({'\t', '\n', '\r'})
BASELINE_SOURCE_COMMIT = '35f4a3a34dbb0ec0b9b984ea0c237f84d2ef3fdf'


class CharacterLoopReconstructor(ContentDisarmReconstructor):
    def _safe_text(self, content):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise FileSecurityError('CDR requires UTF-8 text content') from exc
        if any(
            ord(character) < 32 and character not in ALLOWED_TEXT_CONTROLS
            for character in text
        ):
            raise FileSecurityError('CDR rejected unsafe control characters')
        return text.replace('\r\n', '\n').replace('\r', '\n')


def payload_for(scenario, size):
    if scenario == 'safe_text_unicode':
        unit = '科研样本🙂\tACGTACGT\r\n结果\r\n'.encode('utf-8')
    elif scenario == 'reconstruct_vcf_gzip':
        unit = b''.join(
            f'1\t{index + 1}\t.\tA\tC\t30\tPASS\tDP={index % 101}\r\n'.encode()
            for index in range(4096)
        )
        header = b'##fileformat=VCFv4.2\r\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\r\n'
        remaining = size - len(header)
        return header + unit * (remaining // len(unit)) + unit[:remaining % len(unit)]
    else:
        unit = b'@read\r\nACGTACGTACGTACGT\r\n+\r\nIIIIIIIIIIIIIIII\r\n'
    return unit * (size // len(unit)) + b'A' * (size % len(unit))


def measure(reconstructor, scenario, payload, expected, path):
    if scenario.startswith('safe_text_'):
        started = perf_counter()
        rebuilt = reconstructor._safe_text(payload)
        elapsed = perf_counter() - started
        assert rebuilt == expected
    else:
        path.write_bytes(payload)
        started = perf_counter()
        result = reconstructor.reconstruct(path, path.name)
        elapsed = perf_counter() - started
        assert result == 'reconstructed'
        assert path.read_bytes() == expected
        assert not path.with_name(f'.{path.name}.cdr').exists()
    return {'elapsed_seconds': elapsed}


async def measure_responsiveness(reconstructor, payload, expected):
    done = asyncio.Event()
    gaps = []

    async def heartbeat():
        previous = perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.001)
            now = perf_counter()
            gaps.append(now - previous)
            previous = now

    ticker = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.001)
    started = perf_counter()
    try:
        for _ in range(2):
            rebuilt = await asyncio.to_thread(reconstructor._safe_text, payload)
        elapsed = perf_counter() - started
    finally:
        done.set()
        await ticker
    assert rebuilt == expected
    return {
        'elapsed_seconds': elapsed,
        'heartbeat_count': len(gaps),
        'maximum_heartbeat_gap_seconds': max(gaps),
        'median_heartbeat_gap_seconds': median(gaps),
    }


async def benchmark_responsiveness(samples, size_mib):
    payload = payload_for('safe_text_ascii', size_mib * 1024 * 1024)
    implementations = {
        'character_loop': CharacterLoopReconstructor(),
        'batched': ContentDisarmReconstructor(),
    }
    expected = implementations['character_loop']._safe_text(payload)
    for implementation in implementations.values():
        await measure_responsiveness(implementation, payload, expected)
    pairs = []
    for sample in range(samples):
        order = list(implementations)
        if sample % 2:
            order.reverse()
        pair = {'sample': sample, 'order': order}
        for name in order:
            pair[name] = await measure_responsiveness(
                implementations[name], payload, expected,
            )
        pairs.append(pair)
    return {
        'scope': 'Event-loop heartbeat gaps while text normalization runs in the default executor; excludes input generation and output verification',
        'source_size_mib': size_mib,
        'normalizations_per_measurement': 2,
        'heartbeat_interval_seconds': 0.001,
        'warmup_pairs': 1,
        'paired_samples': samples,
        'character_loop_median_maximum_gap_seconds': median(
            pair['character_loop']['maximum_heartbeat_gap_seconds'] for pair in pairs
        ),
        'batched_median_maximum_gap_seconds': median(
            pair['batched']['maximum_heartbeat_gap_seconds'] for pair in pairs
        ),
        'pairs': pairs,
    }


def benchmark(samples, sizes_mib, workspace_root=None):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    legacy = CharacterLoopReconstructor()
    batched = ContentDisarmReconstructor()
    rows = []
    with tempfile.TemporaryDirectory(prefix='cdr-benchmark-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for scenario in (
            'safe_text_ascii', 'safe_text_unicode',
            'reconstruct_plaintext', 'reconstruct_vcf_gzip',
        ):
            for size_mib in sizes_mib:
                content = payload_for(scenario, size_mib * 1024 * 1024)
                expected_text = legacy._safe_text(content)
                assert batched._safe_text(content) == expected_text
                normalized = expected_text.encode('utf-8')
                payload = content
                expected = expected_text
                filename = 'reads.fastq'
                if scenario == 'reconstruct_plaintext':
                    expected = normalized
                elif scenario == 'reconstruct_vcf_gzip':
                    filename = 'variants.vcf.gz'
                    payload = gzip.compress(content, mtime=0)
                    expected = gzip.compress(normalized, mtime=0)
                paths = {}
                for name in ('character_loop', 'batched'):
                    directory = root / name
                    directory.mkdir(exist_ok=True)
                    paths[name] = directory / filename
                implementations = {
                    'character_loop': legacy,
                    'batched': batched,
                }
                for name, implementation in implementations.items():
                    measure(implementation, scenario, payload, expected, paths[name])
                pairs = []
                for sample in range(samples):
                    order = list(implementations)
                    if sample % 2:
                        order.reverse()
                    pair = {'sample': sample, 'order': order}
                    for name in order:
                        pair[name] = measure(
                            implementations[name], scenario, payload, expected, paths[name],
                        )
                    pairs.append(pair)
                before = median(pair['character_loop']['elapsed_seconds'] for pair in pairs)
                after = median(pair['batched']['elapsed_seconds'] for pair in pairs)
                output = expected.encode('utf-8') if isinstance(expected, str) else expected
                rows.append({
                    'scenario': scenario,
                    'source_size_mib': size_mib,
                    'source_bytes': len(content),
                    'input_bytes': len(payload),
                    'input_sha256': hashlib.sha256(payload).hexdigest(),
                    'output_bytes': len(output),
                    'output_sha256': hashlib.sha256(output).hexdigest(),
                    'normalized_text_bytes': len(normalized),
                    'normalized_text_sha256': hashlib.sha256(normalized).hexdigest(),
                    'character_loop_median_seconds': before,
                    'batched_median_seconds': after,
                    'elapsed_reduction_percent': (before - after) / before * 100,
                    'batched_faster_count': sum(
                        pair['batched']['elapsed_seconds'] < pair['character_loop']['elapsed_seconds']
                        for pair in pairs
                    ),
                    'paired_samples': samples,
                    'pairs': pairs,
                })
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'text_control_batch_chars': TEXT_CONTROL_BATCH_CHARS,
        'scope': 'Text normalization and complete CDR reconstruction on synthetic local files; excludes ClamAV, HTTP, S3, queues and research tools',
        'timer_excludes': 'input generation, input staging, output verification and hashing',
        'output_bytes_equal': True,
        'warmup_pairs_per_scenario': 1,
        'event_loop_responsiveness': asyncio.run(
            benchmark_responsiveness(samples, max(sizes_mib)),
        ),
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--sizes-mib', default='1,16')
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        sizes = [int(item) for item in args.sizes_mib.split(',')]
    except ValueError:
        parser.error('--sizes-mib must contain positive integers separated by commas')
    if args.samples < 1 or not sizes or min(sizes) < 1:
        parser.error('--samples and --sizes-mib must be positive')
    print(json.dumps(benchmark(args.samples, sizes, args.workspace_root), indent=2))


if __name__ == '__main__':
    main()
