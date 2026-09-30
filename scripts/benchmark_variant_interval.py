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

from scripts import benchmark_variant_interval_baseline as legacy
from src import omics_variant_annotation as indexed


CASES = (
    ('small_csv', 16, 1, 'local', False),
    ('csv', 10000, 1000, 'local', False),
    ('csv_gz', 10000, 1000, 'local', True),
    ('gtf', 10000, 1000, 'gencode_gtf', False),
    ('gtf_gz', 10000, 1000, 'gencode_gtf', True),
    ('ann_only', 10000, 1000, 'auto', False),
)


def fixture(root, name, genes, variants, backend, compressed):
    chromosomes = min(genes, 20)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(('chrom', 'start', 'end', 'gene_id', 'gene_name', 'gene_type'))
    gtf = []
    for number in range(genes):
        chrom = f'chr{number % chromosomes + 1}'
        start = number // chromosomes * 100 + 1
        gene_name = '' if number % 13 == 0 else f'Gene{number}' + ('\u7814' if number % 17 == 0 else '')
        writer.writerow((chrom, start, start + 130, f'G{number}', gene_name, 'coding'))
        gtf.append(f'{chrom}\tsrc\tgene\t{start}\t{start + 130}\t.\t+\t.\tgene_id "G{number}"; gene_name "{gene_name}"; gene_type "coding";\n')
    annotation_data = (''.join(gtf) if backend == 'gencode_gtf' else buffer.getvalue()).encode('utf-8')
    annotation = root / (name + ('.gtf.gz' if compressed else '.gtf') if backend == 'gencode_gtf' else name + '.csv')
    annotation.write_bytes(gzip.compress(annotation_data, mtime=0) if backend == 'gencode_gtf' and compressed else annotation_data)
    records = ['##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n']
    for number in range(variants):
        chrom = str(number % chromosomes + 1)
        position = (number * 37 % max(1, genes // chromosomes)) * 100 + 20
        info = f'ANN=G|missense|MODERATE|Gene{number}|ANN{number},C|other|LOW|Gene{number}|ANN{number}' if name == 'ann_only' else '.'
        records.append(f'{chrom}\t{position}\t.\tA\tG,C\t42\tPASS\t{info}\n')
    vcf_data = ''.join(records).encode('utf-8')
    vcf = root / (name + ('.vcf.gz' if compressed else '.vcf'))
    vcf.write_bytes(gzip.compress(vcf_data, mtime=0) if compressed else vcf_data)
    arguments = {'annotation_backend': backend, 'annotation_gtf' if backend == 'gencode_gtf' else 'annotation_csv': annotation}
    return vcf, arguments, {'annotation': hashlib.sha256(annotation_data).hexdigest(), 'vcf': hashlib.sha256(vcf_data).hexdigest()}


def checked(module, vcf, output, arguments, expected, encoded, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = module.execute_variant_annotation(vcf, output, toolchain={'version': 'fixed'}, **arguments)
        elapsed = perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if result != expected or output.read_bytes() != encoded:
        raise RuntimeError('interval index changed annotation results or CSV bytes')
    return peak if memory else elapsed


def compare(root, case, samples):
    name, genes, variants, backend, compressed = case
    vcf, arguments, input_hashes = fixture(root, *case)
    output = root / 'result.csv'
    expected = legacy.execute_variant_annotation(vcf, output, toolchain={'version': 'fixed'}, **arguments)
    encoded = output.read_bytes()
    normalized = {key: value for key, value in expected.items() if key != 'output_csv'}
    csv_rows = list(csv.reader(io.StringIO(encoded.decode('utf-8'), newline='')))
    strategies = {'legacy': legacy, 'indexed': indexed}
    peaks = {name: checked(module, vcf, output, arguments, expected, encoded, memory=True) for name, module in strategies.items()}
    for module in strategies.values():
        checked(module, vcf, output, arguments, expected, encoded)
    pairs = []
    for number in range(samples):
        order = tuple(strategies) if number % 2 == 0 else tuple(reversed(strategies))
        pairs.append({name: checked(strategies[name], vcf, output, arguments, expected, encoded) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['indexed'] for pair in pairs)
    return {
        'case': name, 'annotation_rows': genes, 'variants': variants, 'alleles': expected['n_alleles'],
        'backend': backend, 'gzip_vcf': compressed, 'gzip_annotation': backend == 'gencode_gtf' and compressed,
        'decompressed_input_sha256': input_hashes,
        'normalized_result_sha256': hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'csv_fields_sha256': hashlib.sha256(json.dumps(csv_rows, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'paired_samples': samples, 'legacy_median_seconds': before, 'indexed_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'indexed_faster_count': sum(pair['indexed'] < pair['legacy'] for pair in pairs),
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
    with tempfile.TemporaryDirectory(prefix='variant-interval-', dir=arguments.workspace_root.resolve()) as raw:
        rows = [compare(Path(raw), case, arguments.samples) for case in CASES]
    print(json.dumps({
        'baseline_source_commit': legacy.BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'complete actual execute_variant_annotation including CSV/GTF load, per-call lazy chromosome grouping/record/tree construction, plain/gzip VCF read/decode/validation, ANN or interval lookup, allele expansion, CSV writing and result summary; fixed toolchain metadata; excludes fixture generation, verification, plugin registry, process launch, materialization, output contract, API, queue and publication',
        'memory': 'one extra complete handler tracemalloc run per strategy/scenario outside timing; excludes fixture generation, verification, native allocations and OS cache; not RSS',
        'all_results_and_csv_bytes_equal': True, 'timed_samples_traced': False,
        'warmup_pairs_per_case': 1, 'fixture_verification_and_memory_measurement_outside_timing': True,
        'index_setup_and_lazy_construction_in_timed_calls': True,
        'cross_platform_result_digest_excludes_only_output_path': True,
        'cross_platform_csv_digest_compares_parsed_fields': True,
        'rows': rows,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
