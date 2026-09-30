import argparse
import gzip
import hashlib
import json
from pathlib import Path
import platform
import random
from statistics import median
import tempfile
from time import perf_counter
from unittest import mock

from src.file_security import (
    ContentDisarmReconstructor, FileSecurityError, VCF_GZIP_COMPRESSION_LEVEL,
)


BASELINE_SOURCE_COMMIT = 'be43b929542e89f2f5a4b99acaf25a2d40a47a1d'


class LevelNineReconstructor(ContentDisarmReconstructor):
    def reconstruct(self, path, filename):
        target = Path(path)
        lower_name = str(filename).lower()
        try:
            if lower_name.endswith('.vcf.gz'):
                with gzip.open(target, 'rb') as source:
                    content = source.read()
                rebuilt = gzip.compress(
                    self._reconstruct_text(content, '.vcf'), mtime=0
                )
            else:
                rebuilt = self._reconstruct_text(
                    target.read_bytes(), Path(lower_name).suffix
                )
            temporary = target.with_name(f'.{target.name}.cdr')
            temporary.write_bytes(rebuilt)
            temporary.replace(target)
        except FileSecurityError:
            raise
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise FileSecurityError('CDR reconstruction failed') from exc
        return 'reconstructed'


def vcf_for(scenario, requested_bytes):
    header = (
        b'##fileformat=VCFv4.2\r\n'
        b'##contig=<ID=1>\r\n'
        b'##INFO=<ID=TAG,Number=1,Type=String,Description="Synthetic annotation">\r\n'
        b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\r\n'
    )
    if scenario == 'repeated_records':
        unit = b''.join(
            f'1\t{index + 1}\t.\tA\tC\t30\tPASS\tTAG={index % 101}\r\n'.encode()
            for index in range(4096)
        )
        whole, remainder = divmod(max(requested_bytes - len(header), 0), len(unit))
        tail = unit[:remainder]
        if tail and not tail.endswith(b'\r\n'):
            tail = tail[:tail.rfind(b'\r\n') + 2] if b'\r\n' in tail else b''
        return header + unit * whole + tail
    rng = random.Random(24681357)
    content = bytearray(header)
    position = 0
    while len(content) < requested_bytes:
        position += 1
        tag = rng.randbytes(128).hex()
        row = f'1\t{position}\t.\tA\tC\t30\tPASS\tTAG={tag}\r\n'.encode()
        if len(content) + len(row) > requested_bytes:
            break
        content.extend(row)
    return bytes(content)


def measure(reconstructor, level, path, payload, normalized, expected_output):
    path.write_bytes(payload)
    with mock.patch('src.file_security.VCF_GZIP_COMPRESSION_LEVEL', level):
        started = perf_counter()
        status = reconstructor.reconstruct(path, path.name)
        elapsed = perf_counter() - started
    rebuilt = path.read_bytes()
    output_sha = hashlib.sha256(rebuilt).hexdigest()
    assert status == 'reconstructed'
    assert gzip.decompress(rebuilt) == normalized
    assert len(rebuilt) == expected_output['output_bytes']
    assert output_sha == expected_output['output_sha256']
    assert not path.with_name(f'.{path.name}.cdr').exists()
    return {
        'elapsed_seconds': elapsed,
        'output_bytes': len(rebuilt),
        'output_sha256': output_sha,
    }


def benchmark(samples, sizes_mib, workspace_root=None):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='vcf-compression-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for scenario in ('repeated_records', 'varied_annotations'):
            for size_mib in sizes_mib:
                content = vcf_for(scenario, size_mib * 1024 * 1024)
                normalized = LevelNineReconstructor()._reconstruct_text(content, '.vcf')
                payload = gzip.compress(content, compresslevel=9, mtime=0)
                implementations = {
                    '9': LevelNineReconstructor(),
                    '6': ContentDisarmReconstructor(),
                    '1': ContentDisarmReconstructor(),
                }
                paths = {level: root / f'level-{level}.vcf.gz' for level in implementations}
                expected_outputs = {}
                for level in implementations:
                    reference = gzip.compress(normalized, compresslevel=int(level), mtime=0)
                    expected_outputs[level] = {
                        'output_bytes': len(reference),
                        'output_sha256': hashlib.sha256(reference).hexdigest(),
                    }
                del reference
                pairs = []
                for sample in range(-1, samples):
                    order = list(implementations)
                    shift = max(sample, 0) % len(order)
                    order = order[shift:] + order[:shift]
                    if sample % 2:
                        order.reverse()
                    pair = {'sample': sample, 'order': order}
                    for level in order:
                        pair[level] = measure(
                            implementations[level], int(level), paths[level], payload, normalized,
                            expected_outputs[level],
                        )
                    if sample >= 0:
                        pairs.append(pair)
                baseline = median(pair['9']['elapsed_seconds'] for pair in pairs)
                baseline_size = pairs[0]['9']['output_bytes']
                levels = []
                for level in implementations:
                    elapsed = median(pair[level]['elapsed_seconds'] for pair in pairs)
                    output_size = pairs[0][level]['output_bytes']
                    output_sha = pairs[0][level]['output_sha256']
                    assert all(pair[level]['output_bytes'] == output_size for pair in pairs)
                    assert all(pair[level]['output_sha256'] == output_sha for pair in pairs)
                    levels.append({
                        'compresslevel': int(level),
                        'median_elapsed_seconds': elapsed,
                        'elapsed_reduction_percent': (baseline - elapsed) / baseline * 100,
                        'output_bytes': output_size,
                        'output_sha256': output_sha,
                        'size_change_percent': (output_size - baseline_size) / baseline_size * 100,
                        'faster_than_level_9_count': sum(
                            pair[level]['elapsed_seconds'] < pair['9']['elapsed_seconds']
                            for pair in pairs
                        ),
                    })
                rows.append({
                    'scenario': scenario,
                    'requested_size_mib': size_mib,
                    'source_bytes': len(content),
                    'input_gzip_bytes': len(payload),
                    'input_sha256': hashlib.sha256(payload).hexdigest(),
                    'normalized_bytes': len(normalized),
                    'normalized_sha256': hashlib.sha256(normalized).hexdigest(),
                    'paired_samples': samples,
                    'levels': levels,
                    'pairs': pairs,
                })
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'scope': 'Complete VCF.gz CDR reconstruction on synthetic VCF files; excludes ClamAV, HTTP, S3 and upload inspection',
        'timer_excludes': 'fixture generation, input staging, output verification and verification hashing',
        'selected_default_compresslevel': VCF_GZIP_COMPRESSION_LEVEL,
        'warmup_triples_per_scenario': 1,
        'decompressed_output_equal': True,
        'gzip_output_deterministic_at_each_level': True,
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
