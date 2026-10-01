import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import platform
from statistics import median
import tempfile
from time import perf_counter
import tracemalloc

import numpy as np
from scipy.io import mmread, mmwrite
from scipy.sparse import coo_matrix, csr_matrix

from scripts import benchmark_tenx_counts_baseline as legacy
from src import omics_qc_executors as batched


CASES = (
    ('small_integer', 16, 8, 32, False, False),
    ('empty', 16, 8, 0, False, False),
    ('integer', 20000, 2000, 1000000, False, False),
    ('integer_gz', 20000, 2000, 1000000, False, True),
    ('real', 20000, 2000, 1000000, True, False),
    ('real_gz', 20000, 2000, 1000000, True, True),
)


def fixture(root, case):
    name, genes, cells, nonzero, real, compressed = case
    positions = np.arange(nonzero, dtype=np.int64)
    rows = positions % genes
    columns = (positions // genes + rows * 37) % cells
    values = positions * 19 % 97 + 1
    if real:
        values = values.astype(np.float64) / 4
    matrix = coo_matrix((values, (rows, columns)), shape=(genes, cells)).tocsr()
    assert matrix.nnz == nonzero
    matrix_buffer = io.BytesIO()
    mmwrite(matrix_buffer, matrix, symmetry='general')
    encoded = {
        'matrix.mtx': matrix_buffer.getvalue(),
        'barcodes.tsv': ''.join(f'cell-{cell}\n' for cell in range(cells)).encode('utf-8'),
        'features.tsv': ''.join(f'G{gene}\t{"MT-" if gene % 20 == 0 else "Gene"}{gene}' + ('\u7814' if gene % 97 == 0 else '') + '\tGene Expression\n' for gene in range(genes)).encode('utf-8'),
    }
    paths = []
    for filename, data in encoded.items():
        path = root / (name + '-' + filename + ('.gz' if compressed else ''))
        path.write_bytes(gzip.compress(data, mtime=0) if compressed else data)
        paths.append(path)
    return paths, {name: hashlib.sha256(data).hexdigest() for name, data in encoded.items()}


def output_bytes(root):
    return {path.name: path.read_bytes() for path in root.iterdir()}


def checked(module, paths, output, expected, encoded, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = module.run_single_cell_10x_qc(*paths, output, min_genes=0, max_mito_percent=20)
        elapsed = perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if result != expected or output_bytes(output) != encoded:
        raise RuntimeError('batched count validation changed QC results or output bytes')
    return peak if memory else elapsed


def content_digest(encoded):
    digest = hashlib.sha256()
    for filename, data in sorted(encoded.items()):
        digest.update(filename.encode('utf-8'))
        if filename.endswith('.json'):
            continue
        if filename.endswith('.mtx'):
            matrix = csr_matrix(mmread(io.BytesIO(data)))
            digest.update(json.dumps({'shape': matrix.shape, 'dtype': matrix.dtype.name}).encode('utf-8'))
            digest.update(matrix.data.astype(matrix.dtype.newbyteorder('<'), copy=False).tobytes())
            digest.update(matrix.indices.astype('<i8').tobytes())
            digest.update(matrix.indptr.astype('<i8').tobytes())
        else:
            fields = list(csv.reader(io.StringIO(data.decode('utf-8'), newline=''), delimiter=',' if filename.endswith('.csv') else '\t'))
            digest.update(json.dumps(fields, ensure_ascii=False).encode('utf-8'))
    return digest.hexdigest()


def compare(root, case, samples):
    paths, input_hashes = fixture(root, case)
    output = root / 'qc'
    expected = legacy.run_single_cell_10x_qc(*paths, output, min_genes=0, max_mito_percent=20)
    encoded = output_bytes(output)
    normalized = {key: value for key, value in expected.items() if key not in ('inputs', 'outputs', 'manifest_path')}
    strategies = {'legacy': legacy, 'batched': batched}
    peaks = {label: checked(module, paths, output, expected, encoded, memory=True) for label, module in strategies.items()}
    for module in strategies.values():
        checked(module, paths, output, expected, encoded)
    pairs = []
    for number in range(samples):
        order = tuple(strategies) if number % 2 == 0 else tuple(reversed(strategies))
        pairs.append({label: checked(strategies[label], paths, output, expected, encoded) for label in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['batched'] for pair in pairs)
    name, genes, cells, nonzero, real, compressed = case
    return {
        'case': name, 'genes': genes, 'cells': cells, 'stored_counts': nonzero,
        'count_dtype': 'float64' if real else 'int64', 'gzip_inputs': compressed,
        'decompressed_input_sha256': input_hashes,
        'normalized_result_sha256': hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'output_content_sha256': content_digest(encoded), 'paired_samples': samples,
        'legacy_median_seconds': before, 'batched_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'batched_faster_count': sum(pair['batched'] < pair['legacy'] for pair in pairs),
        'python_peak_bytes_outside_timing': peaks, 'pairs': pairs,
    }


def validation_memory_probe():
    rows = []
    for length in (1048576, 4194304):
        data = np.ones(length, dtype=np.float64)
        peaks = {}
        for name, validator in (('legacy', legacy.validate_counts), ('batched', batched._validate_10x_counts)):
            tracemalloc.start()
            try:
                validator(data)
                peaks[name] = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        if peaks['batched'] >= 150000:
            raise RuntimeError('validation temporary allocations exceeded the bounded allowance')
        rows.append({'stored_counts': length, 'count_dtype': 'float64', 'python_peak_bytes': peaks})
    return {'chunk_counts': 65536, 'allocation_limit_bytes': 150000, 'input_allocation_outside_measurement': True, 'rows': rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tenx-counts-', dir=arguments.workspace_root.resolve()) as raw:
        rows = [compare(Path(raw), case, arguments.samples) for case in CASES]
        memory_probe = validation_memory_probe()
    print(json.dumps({
        'baseline_source_commit': legacy.BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'complete actual run_single_cell_10x_qc including plain/gzip MatrixMarket and TSV loading, CSR conversion, all input validation, feature selection, cell/mitochondrial metrics, filtering, and all output writes; excludes fixture creation, comparison, plugin registry, process launch, materialization, output contract, API, queue and publication',
        'memory': 'one extra full handler tracemalloc run per strategy/scenario outside timing; excludes fixture allocation, verification, native allocations and OS cache; not RSS',
        'all_results_and_output_bytes_equal': True, 'timed_samples_traced': False,
        'warmup_pairs_per_case': 1, 'fixture_verification_and_memory_measurement_outside_timing': True,
        'cross_platform_result_digest_excludes_only_paths': True,
        'cross_platform_output_digest_compares_sparse_matrix_and_table_fields': True,
        'rows': rows, 'validation_memory_probe_outside_timing': memory_probe,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
