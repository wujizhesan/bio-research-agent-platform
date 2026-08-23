"""RNA-seq domain adapter with structured tools and reproducible outputs."""
import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, ttest_ind

PLUGIN_NAME = 'RNA-seq and omics domain'
PLUGIN_VERSION = '0.2.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'omics.end_to_end',
    'omics.differential_expression',
    'omics.pathway',
    'omics.evidence',
    'omics.report',
    'omics.variant_annotation',
    'omics.toolchain',
    'omics.genomics_qc',
)
STATISTICS_BACKENDS = ('auto', 'scipy', 'deseq2')
VARIANT_ANNOTATION_BACKENDS = ('auto', 'local', 'vcf_ann')
TOOLCHAIN_EXECUTABLES = {
    'gatk': 'gatk',
    'samtools': 'samtools',
    'bcftools': 'bcftools',
    'vep': 'vep',
}
DESEQ2_RUNNER = Path(__file__).resolve().parents[1] / 'tools' / 'deseq2_runner.R'



def _require_columns(frame, columns, label):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f'{label} missing columns: {sorted(missing)}')


def _bh_adjust(values):
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return values
    order = np.argsort(values)
    ranked = values[order]
    adjusted = np.minimum.accumulate((ranked * len(values) / np.arange(1, len(values) + 1))[::-1])[::-1]
    result = np.empty_like(adjusted)
    result[order] = np.clip(adjusted, 0.0, 1.0)
    return result


def load_expression_matrix(expression_csv, metadata_csv):
    expression = pd.read_csv(expression_csv)
    metadata = pd.read_csv(metadata_csv)
    if expression.empty:
        raise ValueError('expression matrix is empty')
    _require_columns(expression, {'gene_id'}, 'expression matrix')
    _require_columns(metadata, {'sample_id', 'condition'}, 'metadata')
    sample_columns = [column for column in expression.columns if column != 'gene_id']
    if not sample_columns:
        raise ValueError('expression matrix has no sample columns')
    if expression['gene_id'].isna().any() or expression['gene_id'].duplicated().any():
        raise ValueError('gene_id values must be non-empty and unique')
    if metadata['sample_id'].duplicated().any():
        raise ValueError('metadata sample_id values must be unique')
    missing_metadata = set(sample_columns) - set(metadata['sample_id'])
    missing_expression = set(metadata['sample_id']) - set(sample_columns)
    if missing_metadata or missing_expression:
        raise ValueError(
            f'sample mismatch: missing_metadata={sorted(missing_metadata)}, '
            f'missing_expression={sorted(missing_expression)}'
        )
    expression[sample_columns] = expression[sample_columns].apply(pd.to_numeric, errors='raise')
    metadata = metadata.set_index('sample_id').loc[sample_columns].reset_index()
    conditions = metadata['condition'].astype(str)
    if conditions.nunique() != 2:
        raise ValueError('RNA-seq adapter currently requires exactly two conditions')
    counts = conditions.value_counts()
    if counts.min() < 2:
        raise ValueError('each condition requires at least two replicates')
    return expression, metadata


def _deseq2_runtime():
    executable = os.environ.get('DESEQ2_RSCRIPT', 'Rscript')
    rscript = shutil.which(executable)
    if not rscript:
        return {'available': False, 'backend': 'deseq2', 'reason': 'Rscript not found'}
    try:
        probe = subprocess.run(
            [rscript, '-e', "quit(status=ifelse(requireNamespace('DESeq2', quietly=TRUE), 0, 1))"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {'available': False, 'backend': 'deseq2', 'reason': str(exc)}
    if probe.returncode != 0:
        return {'available': False, 'backend': 'deseq2', 'reason': 'DESeq2 R package not installed'}
    return {'available': True, 'backend': 'deseq2', 'executable': rscript}


def statistics_backend_status():
    status = _deseq2_runtime()
    return {
        'scipy': {'available': True, 'backend': 'scipy', 'mode': 'reproducible_fallback'},
        'deseq2': status,
    }


def _resolve_statistics_backend(requested):
    requested = str(requested or 'auto').lower()
    if requested not in STATISTICS_BACKENDS:
        raise ValueError(f'unknown statistics backend: {requested}')
    if requested == 'scipy':
        return {'requested': requested, 'backend': 'scipy', 'fallback_reason': None}
    status = _deseq2_runtime()
    if requested == 'deseq2' and not status['available']:
        raise RuntimeError(status['reason'])
    if status['available']:
        return {'requested': requested, 'backend': 'deseq2', 'fallback_reason': None}
    return {'requested': requested, 'backend': 'scipy', 'fallback_reason': status['reason']}


def _run_deseq2_backend(expression_csv, metadata_csv, output_csv, condition_a, condition_b):
    status = _deseq2_runtime()
    if not status['available']:
        raise RuntimeError(status['reason'])
    if not DESEQ2_RUNNER.is_file():
        raise RuntimeError(f'DESeq2 runner not found: {DESEQ2_RUNNER}')
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    timeout = int(os.environ.get('DESEQ2_TIMEOUT_SECONDS', '300'))
    result = subprocess.run(
        [
            status['executable'],
            str(DESEQ2_RUNNER),
            str(expression_csv),
            str(metadata_csv),
            str(output_csv),
            str(condition_a),
            str(condition_b),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'DESeq2 process failed').strip()
        raise RuntimeError(detail)
    if not output_csv.is_file():
        raise RuntimeError('DESeq2 completed without producing an output CSV')
    return pd.read_csv(output_csv)


def _condition_pair(metadata, condition_a=None, condition_b=None):
    conditions = sorted(metadata['condition'].astype(str).unique())
    condition_a = str(condition_a or conditions[0])
    condition_b = str(condition_b or conditions[1])
    if condition_a == condition_b or {condition_a, condition_b} != set(conditions):
        raise ValueError(f'conditions must be the two observed values: {conditions}')
    samples_a = metadata.loc[metadata['condition'].astype(str) == condition_a, 'sample_id'].tolist()
    samples_b = metadata.loc[metadata['condition'].astype(str) == condition_b, 'sample_id'].tolist()
    return condition_a, condition_b, samples_a, samples_b


def run_differential_expression(expression_csv, metadata_csv, output_csv,
                                condition_a=None, condition_b=None,
                                statistics_backend='scipy'):
    expression, metadata = load_expression_matrix(expression_csv, metadata_csv)
    condition_a, condition_b, samples_a, samples_b = _condition_pair(metadata, condition_a, condition_b)
    backend = _resolve_statistics_backend(statistics_backend)
    if backend['backend'] == 'deseq2':
        result = _run_deseq2_backend(
            expression_csv, metadata_csv, output_csv, condition_a, condition_b
        )
        _require_columns(result, {'gene_id', 'log2_fc', 'p_value', 'padj'}, 'DESeq2 result')
        result['significant'] = (result['padj'] <= 0.05) & (result['log2_fc'].abs() >= 1.0)
        result = result.sort_values(['padj', 'p_value', 'gene_id']).reset_index(drop=True)
        output_csv = Path(output_csv)
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False)
        return {
            'status': 'completed',
            'output_csv': str(output_csv),
            'condition_a': condition_a,
            'condition_b': condition_b,
            'n_genes': int(len(result)),
            'n_significant': int(result['significant'].sum()),
            'samples_a': samples_a,
            'samples_b': samples_b,
            'backend_requested': backend['requested'],
            'backend': backend['backend'],
            'fallback_reason': backend['fallback_reason'],
        }
    values_a = expression[samples_a].to_numpy(dtype=float)
    values_b = expression[samples_b].to_numpy(dtype=float)
    means_a = values_a.mean(axis=1)
    means_b = values_b.mean(axis=1)
    test = ttest_ind(values_a, values_b, axis=1, equal_var=False, nan_policy='raise')
    result = pd.DataFrame({
        'gene_id': expression['gene_id'].astype(str),
        f'mean_{condition_a}': means_a,
        f'mean_{condition_b}': means_b,
        'log2_fc': np.log2(means_b + 1.0) - np.log2(means_a + 1.0),
        'p_value': np.nan_to_num(test.pvalue, nan=1.0, posinf=1.0, neginf=0.0),
    })
    result['padj'] = _bh_adjust(result['p_value'].to_numpy())
    result['significant'] = (result['padj'] <= 0.05) & (result['log2_fc'].abs() >= 1.0)
    result = result.sort_values(['padj', 'p_value', 'gene_id']).reset_index(drop=True)
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    return {
        'status': 'completed',
        'output_csv': str(output_csv),
        'condition_a': condition_a,
        'condition_b': condition_b,
        'n_genes': int(len(result)),
        'n_significant': int(result['significant'].sum()),
        'samples_a': samples_a,
        'samples_b': samples_b,
        'backend_requested': backend['requested'],
        'backend': backend['backend'],
        'fallback_reason': backend['fallback_reason'],
    }


def _load_gene_sets(gene_sets_csv):
    gene_sets = pd.read_csv(gene_sets_csv)
    _require_columns(gene_sets, {'pathway_id', 'pathway_name', 'gene_id'}, 'gene set table')
    gene_sets = gene_sets.dropna(subset=['pathway_id', 'gene_id']).copy()
    return {
        str(pathway_id): {
            'pathway_name': str(group['pathway_name'].iloc[0]),
            'genes': set(group['gene_id'].astype(str)),
        }
        for pathway_id, group in gene_sets.groupby('pathway_id')
    }


def run_pathway_enrichment(de_csv, gene_sets_csv, output_csv,
                           padj_cutoff=0.05, abs_log2_fc_cutoff=1.0):
    de = pd.read_csv(de_csv)
    _require_columns(de, {'gene_id', 'padj', 'log2_fc'}, 'differential expression result')
    de['gene_id'] = de['gene_id'].astype(str)
    background = set(de['gene_id'])
    selected = set(de.loc[
        (de['padj'] <= padj_cutoff) & (de['log2_fc'].abs() >= abs_log2_fc_cutoff), 'gene_id'
    ])
    rows = []
    for pathway_id, pathway in _load_gene_sets(gene_sets_csv).items():
        pathway_genes = pathway['genes'] & background
        overlap = pathway_genes & selected
        if not pathway_genes:
            continue
        p_value = float(hypergeom.sf(
            len(overlap) - 1,
            len(background),
            len(pathway_genes),
            len(selected),
        )) if selected else 1.0
        rows.append({
            'pathway_id': pathway_id,
            'pathway_name': pathway['pathway_name'],
            'pathway_size': len(pathway_genes),
            'overlap_count': len(overlap),
            'selected_count': len(selected),
            'overlap_genes': '|'.join(sorted(overlap)),
            'p_value': p_value,
        })
    result = pd.DataFrame(rows, columns=[
        'pathway_id', 'pathway_name', 'pathway_size', 'overlap_count',
        'selected_count', 'overlap_genes', 'p_value',
    ])
    if not result.empty:
        result['padj'] = _bh_adjust(result['p_value'].to_numpy())
        result = result.sort_values(['padj', 'p_value', 'pathway_id']).reset_index(drop=True)
    else:
        result['padj'] = pd.Series(dtype=float)
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    return {
        'status': 'completed',
        'output_csv': str(output_csv),
        'n_background_genes': len(background),
        'n_selected_genes': len(selected),
        'n_pathways': len(result),
        'n_significant_pathways': int((result['padj'] <= 0.05).sum()) if not result.empty else 0,
    }


def toolchain_status():
    status = {}
    for name, executable in TOOLCHAIN_EXECUTABLES.items():
        path = shutil.which(executable)
        status[name] = {
            'available': bool(path),
            'path': path,
            'reason': None if path else f'{executable} not found',
        }
    return status


GENOMICS_QC_TYPES = ('auto', 'fastq', 'bam', 'vcf')


def _normalize_qc_paths(input_path):
    values = input_path if isinstance(input_path, (list, tuple)) else [input_path]
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('input_path must contain at least one file path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'input files do not exist: {missing}')
    return paths


def _infer_qc_type(path):
    name = path.name.lower()
    if name.endswith(('.fastq', '.fq', '.fastq.gz', '.fq.gz')):
        return 'fastq'
    if name.endswith(('.bam', '.cram')):
        return 'bam'
    if name.endswith(('.vcf', '.vcf.gz', '.bcf')):
        return 'vcf'
    raise ValueError(f'cannot infer genomics QC input type from: {path}')


def _resolve_qc_type(paths, requested):
    requested = str(requested or 'auto').lower()
    if requested not in GENOMICS_QC_TYPES:
        raise ValueError(f'unknown genomics QC input type: {requested}')
    if requested != 'auto':
        return requested
    detected = {_infer_qc_type(path) for path in paths}
    if len(detected) != 1:
        raise ValueError(f'input files must share one QC type: {sorted(detected)}')
    return detected.pop()


def _fastq_file_stats(path):
    opener = gzip.open if path.name.lower().endswith('.gz') else open
    reads = 0
    bases = 0
    quality_sum = 0
    min_length = None
    max_length = 0
    with opener(path, 'rt', encoding='utf-8', errors='replace') as handle:
        while True:
            header = handle.readline()
            if not header:
                break
            sequence = handle.readline().rstrip('\r\n')
            separator = handle.readline().rstrip('\r\n')
            quality = handle.readline().rstrip('\r\n')
            if not sequence or not header.startswith('@') or not separator.startswith('+'):
                raise ValueError(f'invalid FASTQ record in: {path}')
            if len(sequence) != len(quality):
                raise ValueError(f'FASTQ sequence/quality length mismatch in: {path}')
            length = len(sequence)
            reads += 1
            bases += length
            quality_sum += sum(max(0, ord(char) - 33) for char in quality)
            min_length = length if min_length is None else min(min_length, length)
            max_length = max(max_length, length)
    return {
        'path': str(path),
        'reads': reads,
        'bases': bases,
        'min_read_length': min_length or 0,
        'max_read_length': max_length,
        'mean_read_length': round(bases / reads, 3) if reads else 0.0,
        'mean_quality': round(quality_sum / bases, 3) if bases else 0.0,
    }


def _write_qc_manifest(output_dir, payload):
    manifest_path = Path(output_dir) / 'genomics_qc.json'
    payload['manifest_path'] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def _parse_stat_value(text, label):
    for line in str(text or '').splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[0] == 'SN' and label in fields[2]:
            return fields[3] if len(fields) > 3 else None
        if len(fields) >= 2 and fields[0] == 'SN' and label in fields[1]:
            return fields[2] if len(fields) > 2 else None
    return None


def _run_external_qc(command, output_path, timeout):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            'status': 'failed',
            'error': f'command timed out after {timeout}s',
            'stdout': str(exc.stdout or ''),
            'stderr': str(exc.stderr or ''),
        }
    output_path.write_text(completed.stdout or '', encoding='utf-8')
    if completed.returncode != 0:
        return {
            'status': 'failed',
            'returncode': completed.returncode,
            'error': (completed.stderr or completed.stdout or 'external QC command failed').strip(),
            'stderr': completed.stderr or '',
        }
    return {
        'status': 'completed',
        'returncode': completed.returncode,
        'output_path': str(output_path),
        'stdout': completed.stdout or '',
    }


def run_genomics_qc(input_path, output_dir, input_type='auto', timeout=300):
    paths = _normalize_qc_paths(input_path)
    resolved_type = _resolve_qc_type(paths, input_type)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = max(1, min(int(timeout), 3600))
    if resolved_type == 'fastq':
        file_metrics = [_fastq_file_stats(path) for path in paths]
        totals = {
            'files': len(file_metrics),
            'reads': sum(item['reads'] for item in file_metrics),
            'bases': sum(item['bases'] for item in file_metrics),
        }
        total_bases = totals['bases']
        totals.update({
            'min_read_length': min(
                (item['min_read_length'] for item in file_metrics if item['reads']),
                default=0,
            ),
            'max_read_length': max(
                (item['max_read_length'] for item in file_metrics),
                default=0,
            ),
            'mean_read_length': round(
                totals['bases'] / totals['reads'], 3
            ) if totals['reads'] else 0.0,
            'mean_quality': round(
                sum(item['mean_quality'] * item['bases'] for item in file_metrics) / total_bases,
                3,
            ) if total_bases else 0.0,
        })
        return _write_qc_manifest(output_dir, {
            'status': 'completed',
            'input_type': resolved_type,
            'tool': 'python-fastq-parser',
            'inputs': [str(path) for path in paths],
            'metrics': totals,
            'files': file_metrics,
        })
    if len(paths) != 1:
        raise ValueError(f'{resolved_type} QC accepts exactly one input file')
    input_file = paths[0]
    tool_name = 'samtools' if resolved_type == 'bam' else 'bcftools'
    executable = shutil.which(tool_name)
    if not executable:
        return _write_qc_manifest(output_dir, {
            'status': 'unavailable',
            'input_type': resolved_type,
            'tool': tool_name,
            'inputs': [str(input_file)],
            'reason': f'{tool_name} not found in PATH',
        })
    if resolved_type == 'bam':
        quickcheck = _run_external_qc(
            [executable, 'quickcheck', '-v', str(input_file)],
            output_dir / 'samtools_quickcheck.txt',
            timeout,
        )
        if quickcheck['status'] != 'completed':
            return _write_qc_manifest(output_dir, {
                'status': 'failed',
                'input_type': resolved_type,
                'tool': tool_name,
                'inputs': [str(input_file)],
                'quickcheck': quickcheck,
            })
        flagstat = _run_external_qc(
            [executable, 'flagstat', str(input_file)],
            output_dir / 'samtools_flagstat.txt',
            timeout,
        )
        return _write_qc_manifest(output_dir, {
            'status': flagstat['status'],
            'input_type': resolved_type,
            'tool': tool_name,
            'inputs': [str(input_file)],
            'quickcheck': {'status': 'completed'},
            'flagstat': {
                key: value for key, value in flagstat.items() if key != 'stdout'
            },
            'total_reads': _parse_flagstat_total(flagstat.get('stdout', '')),
        })
    stats = _run_external_qc(
        [executable, 'stats', str(input_file)],
        output_dir / 'bcftools_stats.txt',
        timeout,
    )
    return _write_qc_manifest(output_dir, {
        'status': stats['status'],
        'input_type': resolved_type,
        'tool': tool_name,
        'inputs': [str(input_file)],
        'stats': {
            key: value for key, value in stats.items() if key != 'stdout'
        },
        'number_of_records': _parse_stat_value(stats.get('stdout', ''), 'number of records'),
    })


def _parse_flagstat_total(text):
    match = re.search(r'^(\d+)\s*\+\s*(\d+)\s+in total', str(text or ''), re.MULTILINE)
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2))


def _open_vcf(path):
    path = Path(path)
    if path.suffix.lower() == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8')
    return path.open('r', encoding='utf-8')


def _parse_info(raw):
    values = {}
    if raw in {'', '.'}:
        return values
    for item in raw.split(';'):
        if '=' in item:
            key, value = item.split('=', 1)
            values[key] = value
        else:
            values[item] = True
    return values


def _parse_ann(info, alt):
    records = info.get('ANN')
    if not isinstance(records, str):
        return None
    for record in records.split(','):
        fields = record.split('|')
        if not fields or fields[0] != alt:
            continue
        return {
            'gene_id': fields[4] if len(fields) > 4 else '',
            'gene_name': fields[3] if len(fields) > 3 else '',
            'effect': fields[1] if len(fields) > 1 else '',
            'impact': fields[2] if len(fields) > 2 else '',
        }
    return None


def _normalize_chrom(value):
    value = str(value).strip().lower()
    return value[3:] if value.startswith('chr') else value


def _load_variant_annotations(annotation_csv):
    if not annotation_csv:
        return None
    annotation = pd.read_csv(annotation_csv)
    _require_columns(annotation, {'chrom', 'start', 'end', 'gene_id'}, 'variant annotation table')
    if annotation.empty:
        raise ValueError('variant annotation table is empty')
    annotation = annotation.copy()
    annotation['chrom'] = annotation['chrom'].map(_normalize_chrom)
    annotation['start'] = pd.to_numeric(annotation['start'], errors='raise').astype(int)
    annotation['end'] = pd.to_numeric(annotation['end'], errors='raise').astype(int)
    if (annotation['start'] > annotation['end']).any():
        raise ValueError('variant annotation start must be less than or equal to end')
    if annotation['gene_id'].isna().any():
        raise ValueError('variant annotation gene_id must be non-empty')
    return annotation


def _local_variant_matches(annotation, chrom, position):
    if annotation is None:
        return []
    matches = annotation.loc[
        (annotation['chrom'] == _normalize_chrom(chrom))
        & (annotation['start'] <= position)
        & (annotation['end'] >= position)
    ]
    return matches.to_dict('records')


def annotate_variants(vcf_path, output_csv, annotation_csv=None,
                      annotation_backend='auto'):
    requested = str(annotation_backend or 'auto').lower()
    if requested not in VARIANT_ANNOTATION_BACKENDS:
        raise ValueError(f'unknown variant annotation backend: {requested}')
    annotation = _load_variant_annotations(annotation_csv)
    if requested == 'local' and annotation is None:
        raise ValueError('local variant annotation requires annotation_csv')
    rows = []
    n_variants = 0
    n_alleles = 0
    sources = set()
    with _open_vcf(vcf_path) as handle:
        header = None
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip('\n\r')
            if not line:
                continue
            if line.startswith('##'):
                continue
            if line.startswith('#CHROM'):
                header = line.lstrip('#').split('\t')
                continue
            if line.startswith('#'):
                continue
            if header is None:
                raise ValueError('VCF header is missing')
            fields = line.split('\t')
            if len(fields) < 8:
                raise ValueError(f'VCF row {line_number} has fewer than 8 columns')
            record = dict(zip(header, fields))
            chrom = record.get('#CHROM') or record.get('CHROM')
            if not chrom:
                raise ValueError('VCF header must include CHROM')
            try:
                position = int(record['POS'])
            except (KeyError, ValueError) as exc:
                raise ValueError(f'VCF row {line_number} has an invalid POS') from exc
            ref = record.get('REF', '')
            alternatives = [item for item in record.get('ALT', '').split(',') if item and item != '.']
            if not ref or not alternatives:
                raise ValueError(f'VCF row {line_number} has invalid REF or ALT')
            n_variants += 1
            n_alleles += len(alternatives)
            info = _parse_info(record.get('INFO', '.'))
            base_id = record.get('ID') or '.'
            for alt in alternatives:
                variant_id = base_id if base_id != '.' else f'{chrom}:{position}:{ref}>{alt}'
                ann = _parse_ann(info, alt) if requested in {'auto', 'vcf_ann'} else None
                matches = _local_variant_matches(annotation, chrom, position)
                if ann:
                    sources.add('vcf_ann')
                    rows.append({
                        'variant_id': variant_id,
                        'chrom': chrom,
                        'pos': position,
                        'ref': ref,
                        'alt': alt,
                        'qual': record.get('QUAL', '.'),
                        'filter': record.get('FILTER', '.'),
                        'gene_id': ann['gene_id'],
                        'gene_name': ann['gene_name'],
                        'effect': ann['effect'],
                        'impact': ann['impact'],
                        'annotation_source': 'vcf_ann',
                        'annotation_status': 'annotated',
                    })
                    continue
                if requested == 'vcf_ann':
                    rows.append({
                        'variant_id': variant_id,
                        'chrom': chrom,
                        'pos': position,
                        'ref': ref,
                        'alt': alt,
                        'qual': record.get('QUAL', '.'),
                        'filter': record.get('FILTER', '.'),
                        'gene_id': '',
                        'gene_name': '',
                        'effect': '',
                        'impact': '',
                        'annotation_source': 'vcf_ann',
                        'annotation_status': 'unmatched',
                    })
                    continue
                if matches:
                    sources.add('local_interval')
                    for match in matches:
                        rows.append({
                            'variant_id': variant_id,
                            'chrom': chrom,
                            'pos': position,
                            'ref': ref,
                            'alt': alt,
                            'qual': record.get('QUAL', '.'),
                            'filter': record.get('FILTER', '.'),
                            'gene_id': str(match['gene_id']),
                            'gene_name': str(match.get('gene_name', '')),
                            'effect': str(match.get('effect', '')),
                            'impact': str(match.get('impact', '')),
                            'annotation_source': 'local_interval',
                            'annotation_status': 'annotated',
                        })
                else:
                    rows.append({
                        'variant_id': variant_id,
                        'chrom': chrom,
                        'pos': position,
                        'ref': ref,
                        'alt': alt,
                        'qual': record.get('QUAL', '.'),
                        'filter': record.get('FILTER', '.'),
                        'gene_id': '',
                        'gene_name': '',
                        'effect': '',
                        'impact': '',
                        'annotation_source': 'none',
                        'annotation_status': 'unmatched',
                    })
    if header is None:
        raise ValueError('VCF header is missing')
    result = pd.DataFrame(rows, columns=[
        'variant_id', 'chrom', 'pos', 'ref', 'alt', 'qual', 'filter',
        'gene_id', 'gene_name', 'effect', 'impact', 'annotation_source',
        'annotation_status',
    ])
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    gene_ids = sorted({str(value) for value in result['gene_id'] if str(value).strip()})
    effective_backend = 'mixed' if len(sources) > 1 else (next(iter(sources)) if sources else requested)
    return {
        'status': 'completed',
        'output_csv': str(output_csv),
        'backend_requested': requested,
        'backend': effective_backend,
        'n_variants': n_variants,
        'n_alleles': n_alleles,
        'n_annotated': int((result['annotation_status'] == 'annotated').sum()),
        'n_unmatched': int((result['annotation_status'] == 'unmatched').sum()),
        'gene_ids': gene_ids,
        'toolchain': toolchain_status(),
    }


def search_gene_evidence(gene_ids, evidence_csv=None, provider='local',
                         cache_dir=None, timeout=15, genome='hg38',
                         gencode_gtf=None):
    try:
        from .evidence_providers import get_evidence_provider
    except ImportError:
        from evidence_providers import get_evidence_provider
    return get_evidence_provider(
        provider=provider,
        evidence_csv=evidence_csv,
        cache_dir=cache_dir,
        timeout=timeout,
        genome=genome,
        gencode_gtf=gencode_gtf,
    ).search(gene_ids)

def generate_omics_report(de_csv, pathway_csv, output_md, evidence=None):
    de = pd.read_csv(de_csv)
    pathways = pd.read_csv(pathway_csv)
    significant = de[de['significant'].astype(bool)] if 'significant' in de else de.iloc[0:0]
    lines = [
        '# RNA-seq Agent Analysis Report',
        '',
        f'- Generated at: {datetime.now(timezone.utc).isoformat()}',
        f'- Differential-expression result: {de_csv}',
        f'- Pathway result: {pathway_csv}',
        f'- Genes tested: {len(de)}',
        f'- Significant genes: {len(significant)}',
        f'- Pathways tested: {len(pathways)}',
        '',
        '## Top Differentially Expressed Genes',
        '',
        '| Gene | log2 FC | adjusted p-value |',
        '|---|---:|---:|',
    ]
    for _, row in significant.head(10).iterrows():
        lines.append(f'| {row["gene_id"]} | {row["log2_fc"]:.3f} | {row["padj"]:.3g} |')
    if significant.empty:
        lines.append('| None | n/a | n/a |')
    lines.extend([
        '',
        '## Top Enriched Pathways',
        '',
        '| Pathway | Overlap | adjusted p-value |',
        '|---|---:|---:|',
    ])
    if pathways.empty:
        lines.append('| None | 0 | n/a |')
    else:
        for _, row in pathways.head(10).iterrows():
            lines.append(f'| {row["pathway_name"]} | {row["overlap_count"]} | {row["padj"]:.3g} |')
    if evidence:
        lines.extend(['', '## Evidence', ''])
        lines.append(f'- Evidence matches: {evidence.get("n_matches", 0)}')
        evidence_source = evidence.get('source_file') or evidence.get('endpoint') or evidence.get('provider', 'n/a')
        lines.append(f'- Evidence source: {evidence_source}')
        for item in evidence.get('matches', [])[:10]:
            lines.append(f'- **{item.get("gene_id", "")}**: {item.get("title", "")} ({item.get("source", "")})')
    output_md = Path(output_md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {
        'status': 'completed',
        'output_md': str(output_md),
        'n_genes': int(len(de)),
        'n_significant_genes': int(len(significant)),
        'n_pathways': int(len(pathways)),
        'n_evidence_matches': int((evidence or {}).get('n_matches', 0)),
    }


def run_omics_analysis(expression_csv, metadata_csv, gene_sets_csv, output_dir,
                       evidence_csv=None, condition_a=None, condition_b=None,
                       evidence_provider='local', evidence_cache_dir=None,
                       evidence_timeout=15, statistics_backend='auto',
                       genome='hg38', gencode_gtf=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    de_csv = output_dir / 'differential_expression.csv'
    pathway_csv = output_dir / 'pathway_enrichment.csv'
    report_md = output_dir / 'omics_report.md'
    de_meta = run_differential_expression(
        expression_csv, metadata_csv, de_csv, condition_a, condition_b,
        statistics_backend=statistics_backend,
    )
    pathway_meta = run_pathway_enrichment(de_csv, gene_sets_csv, pathway_csv)
    evidence = None
    if evidence_csv or evidence_provider in {
        'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode'
    }:
        significant_genes = pd.read_csv(de_csv)
        significant_genes = significant_genes.loc[
            significant_genes['significant'], 'gene_id'
        ].astype(str).tolist()
        evidence = search_gene_evidence(
            significant_genes,
            evidence_csv=evidence_csv,
            provider=evidence_provider,
            cache_dir=evidence_cache_dir,
            timeout=evidence_timeout,
            genome=genome,
            gencode_gtf=gencode_gtf,
        )
    report_meta = generate_omics_report(de_csv, pathway_csv, report_md, evidence)
    manifest = {
        'status': 'completed',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'inputs': {
            'expression_csv': str(expression_csv),
            'metadata_csv': str(metadata_csv),
            'gene_sets_csv': str(gene_sets_csv),
            'evidence_csv': str(evidence_csv) if evidence_csv else None,
            'evidence_provider': evidence_provider,
            'evidence_cache_dir': str(evidence_cache_dir) if evidence_cache_dir else None,
            'genome': genome,
            'gencode_gtf': str(gencode_gtf) if gencode_gtf else None,
            'statistics_backend': statistics_backend,
        },
        'differential_expression': de_meta,
        'pathway_enrichment': pathway_meta,
        'report': report_meta,
    }
    (output_dir / 'omics_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return manifest


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


TOOLS = {
    'run_analysis': {
        'description': 'Run an end-to-end RNA-seq research analysis with differential expression, pathway enrichment, evidence retrieval and a traceable report.',
        'parameters': _parameters({
            'expression_csv': {'type': 'string'},
            'metadata_csv': {'type': 'string'},
            'gene_sets_csv': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'evidence_csv': {'type': 'string'},
            'condition_a': {'type': 'string'},
            'condition_b': {'type': 'string'},
            'evidence_provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']},
            'evidence_cache_dir': {'type': 'string'},
            'evidence_timeout': {'type': 'number'},
            'statistics_backend': {'type': 'string', 'enum': list(STATISTICS_BACKENDS)},
            'genome': {'type': 'string'},
            'gencode_gtf': {'type': 'string'},
        }, required=('expression_csv', 'metadata_csv', 'gene_sets_csv', 'output_dir')),
        'function': run_omics_analysis,
    },
    'run_differential_expression': {
        'description': 'Run a reproducible two-condition RNA-seq differential expression analysis.',
        'parameters': _parameters({
            'expression_csv': {'type': 'string'},
            'metadata_csv': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'condition_a': {'type': 'string'},
            'condition_b': {'type': 'string'},
            'statistics_backend': {'type': 'string', 'enum': list(STATISTICS_BACKENDS)},
        }, required=('expression_csv', 'metadata_csv', 'output_csv')),
        'function': run_differential_expression,
    },
    'run_pathway_enrichment': {
        'description': 'Run pathway enrichment against a local gene-set table.',
        'parameters': _parameters({
            'de_csv': {'type': 'string'},
            'gene_sets_csv': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'padj_cutoff': {'type': 'number'},
            'abs_log2_fc_cutoff': {'type': 'number'},
        }, required=('de_csv', 'gene_sets_csv', 'output_csv')),
        'function': run_pathway_enrichment,
    },
    'annotate_variants': {
        'description': 'Annotate VCF variants with VCF ANN records or a local genomic interval table and return traceable gene mappings.',
        'parameters': _parameters({
            'vcf_path': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'annotation_csv': {'type': 'string'},
            'annotation_backend': {'type': 'string', 'enum': list(VARIANT_ANNOTATION_BACKENDS)},
        }, required=('vcf_path', 'output_csv')),
        'function': annotate_variants,
    },
    'inspect_toolchain': {
        'description': 'Report whether GATK, SAMtools, bcftools and VEP are available in the execution environment.',
        'parameters': _parameters({}),
        'function': toolchain_status,
    },
    'run_genomics_qc': {
        'description': 'Run reproducible QC for FASTQ, BAM/CRAM or VCF/BCF using a local parser, SAMtools or bcftools.',
        'parameters': _parameters({
            'input_path': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'output_dir': {'type': 'string'},
            'input_type': {'type': 'string', 'enum': list(GENOMICS_QC_TYPES)},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('input_path', 'output_dir')),
        'function': run_genomics_qc,
    },
    'search_gene_evidence': {
        'description': 'Retrieve cited gene evidence from a structured evidence index.',
        'parameters': _parameters({
            'gene_ids': {'type': 'array', 'items': {'type': 'string'}},
            'evidence_csv': {'type': 'string'},
            'provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']},
            'cache_dir': {'type': 'string'},
            'timeout': {'type': 'number'},
            'genome': {'type': 'string'},
            'gencode_gtf': {'type': 'string'},
        }, required=('gene_ids',)),
        'function': search_gene_evidence,
    },
    'generate_omics_report': {
        'description': 'Generate a traceable RNA-seq analysis report.',
        'parameters': _parameters({
            'de_csv': {'type': 'string'},
            'pathway_csv': {'type': 'string'},
            'output_md': {'type': 'string'},
        }, required=('de_csv', 'pathway_csv', 'output_md')),
        'function': generate_omics_report,
    },
}


def run_tool(name, args):
    spec = TOOLS.get(name)
    if spec is None:
        return {'status': 'error', 'error': f'unknown tool: {name}'}
    try:
        return spec['function'](**args)
    except Exception as exc:
        return {'status': 'error', 'error': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run RNA-seq Agent analysis')
    parser.add_argument('--expression', required=True)
    parser.add_argument('--metadata', required=True)
    parser.add_argument('--gene-sets', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--evidence')
    parser.add_argument('--evidence-provider', choices=('local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg'), default='local')
    parser.add_argument('--statistics-backend', choices=STATISTICS_BACKENDS, default='auto')
    parser.add_argument('--cache-dir')
    parser.add_argument('--condition-a')
    parser.add_argument('--condition-b')
    args = parser.parse_args(argv)
    result = run_omics_analysis(
        args.expression,
        args.metadata,
        args.gene_sets,
        args.out_dir,
        args.evidence,
        args.condition_a,
        args.condition_b,
        evidence_provider=args.evidence_provider,
        evidence_cache_dir=args.cache_dir,
        statistics_backend=args.statistics_backend,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
