import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import platform
from statistics import median
import tempfile
from time import perf_counter
import tracemalloc

from scripts import benchmark_pathway_grouping_baseline as legacy
from src import omics_agent as grouped


CASES = (
    ('small', 64, 16, 8, False, 0),
    ('empty', 0, 0, 0, False, 0),
    ('no_selected', 20000, 2000, 0, False, 0),
    ('all_selected', 20000, 2000, 20000, False, 0),
    ('mixed', 20000, 2000, 2000, False, 0),
    ('many', 20000, 8000, 2000, False, 0),
    ('duplicate_string', 2, 2, 1, False, 65537),
    ('duplicate_numeric', 2, 2, 1, True, 65537),
    ('many_numeric', 20000, 8000, 2000, True, 0),
)


def fixture(root, case):
    name, genes, pathways, selected, numeric, repeats = case
    directory = root / name
    directory.mkdir()
    de_path, sets_path = directory / 'de.csv', directory / 'sets.csv'

    def gene_id(number):
        return str(number) if numeric else f'G{number:05}'

    with de_path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target, lineterminator='\n')
        writer.writerow(['gene_id', 'padj', 'log2_fc'])
        writer.writerows((gene_id(number), 0.01 if number < selected else 0.5, -2 if number % 3 else 2) for number in range(genes))
    set_rows = 0
    with sets_path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target, lineterminator='\n')
        writer.writerow(['pathway_id', 'pathway_name', 'gene_id'])
        for pathway in range(pathways):
            size = repeats or (8 if name == 'small' else 20 + pathway % 61)
            for offset in range(size):
                number = pathway % genes if repeats else (pathway * 37 + offset * (pathway % 3 + 1)) % genes
                writer.writerow((f'P{pathway:05}', f'通路,{pathway}', gene_id(number)))
                set_rows += 1
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (de_path, sets_path)}
    return de_path, sets_path, directory / 'result.csv', hashes, set_rows


def checked(module, paths, expected, encoded, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = module.run_pathway_enrichment(*paths)
        elapsed = perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if result != expected or paths[2].read_bytes() != encoded:
        raise RuntimeError('grouped pathway tests changed the full result or output CSV bytes')
    return peak if memory else elapsed


def compare(root, case, samples):
    de_path, sets_path, output, hashes, set_rows = fixture(root, case)
    paths = de_path, sets_path, output
    expected = legacy.run_pathway_enrichment(*paths)
    encoded = output.read_bytes()
    normalized = {key: value for key, value in expected.items() if key != 'output_csv'}
    fields = list(csv.reader(io.StringIO(encoded.decode('utf-8'), newline='')))
    strategies = {'legacy': legacy, 'grouped': grouped}
    for module in strategies.values():
        checked(module, paths, expected, encoded)
    pairs = []
    for number in range(samples):
        order = tuple(strategies) if number % 2 == 0 else tuple(reversed(strategies))
        pairs.append({name: checked(strategies[name], paths, expected, encoded) for name in order})
    peaks = {name: checked(module, paths, expected, encoded, memory=True) for name, module in strategies.items()}
    before, after = (median(pair[name] for pair in pairs) for name in strategies)
    name, genes, pathways, selected, numeric, repeats = case
    return {
        'case': name, 'background_genes': genes, 'pathways': pathways, 'selected_genes': selected,
        'gene_id_kind': 'numeric' if numeric else 'string', 'repeated_rows_per_pathway': repeats,
        'gene_set_rows': set_rows, 'input_sha256': hashes,
        'normalized_result': normalized,
        'normalized_result_sha256': hashlib.sha256(json.dumps(normalized, sort_keys=True).encode('utf-8')).hexdigest(),
        'output_csv_sha256': hashlib.sha256(encoded).hexdigest(),
        'output_fields_for_cross_platform_verification': fields,
        'paired_samples': samples, 'legacy_median_seconds': before, 'grouped_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'grouped_faster_count': sum(pair['grouped'] < pair['legacy'] for pair in pairs),
        'python_peak_bytes_outside_timing': peaks, 'pairs': pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pathway-grouping-', dir=arguments.workspace_root.resolve()) as raw:
        rows = [compare(Path(raw), case, arguments.samples) for case in CASES]
    print(json.dumps({
        'baseline_source_commit': legacy.BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'complete actual run_pathway_enrichment including both CSV loads, validation, gene-set grouping, background and overlap sets, hypergeometric tails, BH adjustment, sorting, result serialization and output CSV writing; excludes fixtures, verification, registry, process launch, materialization, output contract, API, queue and publication',
        'memory': 'one additional full-handler tracemalloc measurement per strategy/scenario outside timing; input fixtures and verification excluded; Python traced allocations, not RSS or untraced native allocations/OS cache',
        'all_results_and_output_bytes_equal': True,
        'timed_samples_traced': False, 'warmup_pairs_per_case': 1,
        'fixture_verification_and_memory_measurement_outside_timing': True,
        'gene_array_chunk_rows': 16384, 'scipy_batch_pathways': 1024,
        'cross_platform_verification': 'exact input and result hashes; CSV header and non-probability fields exact after matching pathway_id; p_value/padj comparison at relative tolerance 1e-12 and absolute tolerance 1e-300',
        'rows': rows,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
