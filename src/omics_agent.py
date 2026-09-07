"""RNA-seq domain adapter with structured tools and reproducible outputs."""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, ttest_ind

try:
    from .omics_results import (
        build_omics_manifest,
        differential_expression_result,
        pathway_enrichment_result,
        specialist_workflow_result,
        variant_annotation_result,
        write_omics_manifest,
        write_omics_report,
    )
    from .omics_validation import (
        GENOMICS_QC_TYPES,
        condition_pair as _condition_pair,
        infer_qc_type as _infer_qc_type,
        load_expression_matrix,
        normalize_alignment_paths as _normalize_alignment_paths,
        normalize_fastq_paths as _normalize_fastq_paths,
        normalize_qc_paths as _normalize_qc_paths,
        require_columns as _require_columns,
        resolve_qc_type as _resolve_qc_type,
    )
    from .omics_protocol import build_omics_tools
    from .omics_qc_executors import (
        run_metagenomics_qc,
        run_single_cell_10x_qc,
        run_single_cell_qc,
    )
    from .omics_external_runtime import ExternalToolDependencies
    from .omics_rnaseq_executors import (
        execute_feature_counts,
        execute_rnaseq_alignment,
    )
    from .omics_variant_executors import (
        execute_variant_calling,
        execute_variant_normalization,
    )
except ImportError:
    from omics_results import (
        build_omics_manifest,
        differential_expression_result,
        pathway_enrichment_result,
        specialist_workflow_result,
        variant_annotation_result,
        write_omics_manifest,
        write_omics_report,
    )
    from omics_validation import (
        GENOMICS_QC_TYPES,
        condition_pair as _condition_pair,
        infer_qc_type as _infer_qc_type,
        load_expression_matrix,
        normalize_alignment_paths as _normalize_alignment_paths,
        normalize_fastq_paths as _normalize_fastq_paths,
        normalize_qc_paths as _normalize_qc_paths,
        require_columns as _require_columns,
        resolve_qc_type as _resolve_qc_type,
    )
    from omics_protocol import build_omics_tools
    from omics_qc_executors import (
        run_metagenomics_qc,
        run_single_cell_10x_qc,
        run_single_cell_qc,
    )
    from omics_external_runtime import ExternalToolDependencies
    from omics_rnaseq_executors import (
        execute_feature_counts,
        execute_rnaseq_alignment,
    )
    from omics_variant_executors import (
        execute_variant_calling,
        execute_variant_normalization,
    )

PLUGIN_NAME = 'RNA-seq and omics domain'
PLUGIN_VERSION = '0.7.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'omics.end_to_end',
    'omics.differential_expression',
    'omics.pathway',
    'omics.evidence',
    'omics.report',
    'omics.variant_annotation',
    'omics.variant_calling',
    'omics.variant_normalization',
    'omics.gtf_annotation',
    'omics.rnaseq_alignment',
    'omics.rnaseq_quantification',
    'omics.toolchain',
    'omics.genomics_qc',
    'omics.fastq_qc',
    'omics.single_cell_qc',
    'omics.metagenomics_qc',
)
STATISTICS_BACKENDS = ('auto', 'scipy', 'deseq2')
VARIANT_ANNOTATION_BACKENDS = ('auto', 'local', 'vcf_ann', 'gencode_gtf')
TOOLCHAIN_EXECUTABLES = {
    'gatk': 'gatk',
    'samtools': 'samtools',
    'bcftools': 'bcftools',
    'hisat2': 'hisat2',
    'hisat2-build': 'hisat2-build',
    'featureCounts': 'featureCounts',
    'fastqc': 'fastqc',
    'multiqc': 'multiqc',
    'vep': 'vep',
}
DESEQ2_RUNNER = Path(__file__).resolve().parents[1] / 'tools' / 'deseq2_runner.R'

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
        return differential_expression_result(
            output_csv,
            result,
            condition_a,
            condition_b,
            samples_a,
            samples_b,
            backend,
        )
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
    return differential_expression_result(
        output_csv,
        result,
        condition_a,
        condition_b,
        samples_a,
        samples_b,
        backend,
    )


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
    return pathway_enrichment_result(output_csv, result, background, selected)


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


def _file_sha256(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _external_tool_version(executable):
    version_command = [executable, '-v'] if Path(executable).name.lower() == 'featurecounts' else [executable, '--version']
    try:
        result = subprocess.run(
            version_command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {'available': False, 'error': str(exc)}
    output = (result.stdout or result.stderr or '').strip()
    return {
        'available': result.returncode == 0,
        'version': output.splitlines()[0] if output else None,
        'returncode': result.returncode,
    }


def _run_variant_command(command, timeout, stdout_path=None):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = str(exc.stderr or '')
        if stdout_path:
            Path(stdout_path).write_text(str(exc.stdout or ''), encoding='utf-8')
        return {
            'status': 'failed',
            'returncode': None,
            'error': f'command timed out after {timeout}s',
            'stderr': stderr,
        }
    except OSError as exc:
        return {
            'status': 'failed',
            'returncode': None,
            'error': str(exc),
            'stderr': '',
        }
    if stdout_path:
        Path(stdout_path).write_text(completed.stdout or '', encoding='utf-8')
    result = {
        'status': 'completed' if completed.returncode == 0 else 'failed',
        'returncode': completed.returncode,
    }
    if completed.returncode != 0:
        result['error'] = (completed.stderr or completed.stdout or 'external command failed').strip()
    if completed.stderr:
        result['stderr'] = completed.stderr
    return result


def _external_tool_dependencies():
    return ExternalToolDependencies(
        which=shutil.which,
        sha256=_file_sha256,
        version=_external_tool_version,
        run_command=_run_variant_command,
    )


def run_variant_calling(bam_path, reference_fasta, output_dir, output_vcf=None,
                        region=None, min_mapping_quality=0, min_base_quality=13,
                        timeout=600):
    return execute_variant_calling(
        bam_path,
        reference_fasta,
        output_dir,
        output_vcf=output_vcf,
        region=region,
        min_mapping_quality=min_mapping_quality,
        min_base_quality=min_base_quality,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


def normalize_variants(vcf_path, reference_fasta, output_dir, output_vcf=None,
                       region=None, timeout=300):
    return execute_variant_normalization(
        vcf_path,
        reference_fasta,
        output_dir,
        output_vcf=output_vcf,
        region=region,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


def run_rnaseq_alignment(fastq_paths, reference_fasta, output_dir,
                         output_alignment_paths=None, fastq_r2_paths=None,
                         threads=1, timeout=1800):
    return execute_rnaseq_alignment(
        fastq_paths,
        reference_fasta,
        output_dir,
        output_alignment_paths=output_alignment_paths,
        fastq_r2_paths=fastq_r2_paths,
        threads=threads,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


def run_feature_counts(alignment_paths, annotation_gtf, output_dir, output_csv=None,
                       feature_type='exon', gene_id_attribute='gene_id', strand=0,
                       paired_end=False, threads=1, timeout=900):
    return execute_feature_counts(
        alignment_paths,
        annotation_gtf,
        output_dir,
        output_csv=output_csv,
        feature_type=feature_type,
        gene_id_attribute=gene_id_attribute,
        strand=strand,
        paired_end=paired_end,
        threads=threads,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


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


def _write_variant_manifest(output_dir, payload):
    return _write_omics_manifest(output_dir, 'variant_normalization.json', payload)


def _write_omics_manifest(output_dir, filename, payload):
    manifest_path = Path(output_dir) / filename
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


def _parse_fastqc_summary(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        summary_name = next(
            (name for name in archive.namelist() if name.endswith('/summary.txt')),
            None,
        )
        if not summary_name:
            return []
        text = archive.read(summary_name).decode('utf-8', errors='replace')
    records = []
    for line in text.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3:
            records.append({
                'status': fields[0].lower(),
                'module': fields[1],
                'details': fields[2],
            })
    return records


def _fastqc_reports(output_dir):
    reports = []
    summaries = []
    for zip_path in sorted(output_dir.glob('*_fastqc.zip')):
        try:
            summary = _parse_fastqc_summary(zip_path)
        except (OSError, zipfile.BadZipFile) as exc:
            summary = [{'status': 'error', 'module': 'summary', 'details': str(exc)}]
        summaries.append({
            'archive': str(zip_path),
            'summary': summary,
        })
        reports.append(str(zip_path))
    reports.extend(str(path) for path in sorted(output_dir.glob('*_fastqc.html')))
    return reports, summaries


def run_fastq_qc(fastq_paths, output_dir, fastq_r2_paths=None, threads=1,
                 timeout=900):
    paths = _normalize_fastq_paths(fastq_paths)
    mate_paths = _normalize_fastq_paths(fastq_r2_paths) if fastq_r2_paths is not None else []
    if mate_paths and len(paths) != len(mate_paths):
        raise ValueError('fastq_r2_paths must match the number of FASTQ R1 inputs')
    all_paths = paths + mate_paths
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fastqc_dir = output_dir / 'fastqc'
    fastqc_dir.mkdir(parents=True, exist_ok=True)
    threads = max(1, min(int(threads), 64))
    timeout = max(1, min(int(timeout), 3600))
    provenance = {
        'inputs': [
            {'path': str(path), 'sha256': _file_sha256(path)} for path in all_paths
        ],
        'parameters': {
            'threads': threads,
            'layout': 'paired_end' if mate_paths else 'single_end',
        },
        'tools': {},
    }
    fastqc = shutil.which('fastqc')
    multiqc = shutil.which('multiqc')
    missing_tools = [name for name, path in (('fastqc', fastqc), ('multiqc', multiqc)) if not path]
    if missing_tools:
        return _write_omics_manifest(output_dir, 'fastq_qc.json', {
            'status': 'unavailable',
            'workflow': 'fastq_quality_control',
            'inputs': [str(path) for path in all_paths],
            'missing_tools': missing_tools,
            'reason': 'install FastQC and MultiQC in the execution environment',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'fastqc': {'path': fastqc, **_external_tool_version(fastqc)},
        'multiqc': {'path': multiqc, **_external_tool_version(multiqc)},
    }
    fastqc_command = [
        fastqc, '--quiet', '--threads', str(threads),
        '--outdir', str(fastqc_dir),
        *[str(path) for path in all_paths],
    ]
    fastqc_result = _run_variant_command(
        fastqc_command, timeout, output_dir / 'fastqc.log'
    )
    if fastqc_result['status'] != 'completed':
        return _write_omics_manifest(output_dir, 'fastq_qc.json', {
            'status': 'failed',
            'workflow': 'fastq_quality_control',
            'inputs': [str(path) for path in all_paths],
            'fastqc': fastqc_result,
            'command': fastqc_command,
            'provenance': provenance,
        })
    multiqc_command = [
        multiqc, '--force', '--outdir', str(output_dir),
        '--filename', 'multiqc_report.html', str(fastqc_dir),
    ]
    multiqc_result = _run_variant_command(
        multiqc_command, timeout, output_dir / 'multiqc.log'
    )
    reports, summaries = _fastqc_reports(fastqc_dir)
    module_status_counts = {}
    for report in summaries:
        for record in report['summary']:
            status = record['status']
            module_status_counts[status] = module_status_counts.get(status, 0) + 1
    if (output_dir / 'multiqc_report.html').is_file():
        reports.append(str(output_dir / 'multiqc_report.html'))
    return _write_omics_manifest(output_dir, 'fastq_qc.json', {
        'status': 'completed' if multiqc_result['status'] == 'completed' else 'failed',
        'workflow': 'fastq_quality_control',
        'inputs': [str(path) for path in all_paths],
        'reports': reports,
        'fastqc_summaries': summaries,
        'module_status_counts': module_status_counts,
        'fastqc': fastqc_result,
        'multiqc': multiqc_result,
        'commands': {
            'fastqc': fastqc_command,
            'multiqc': multiqc_command,
        },
        'provenance': provenance,
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


def _parse_gtf_attributes(text):
    attributes = {}
    for item in str(text or '').strip().strip(';').split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' in item and ' ' not in item.split('=', 1)[0]:
            key, value = item.split('=', 1)
        else:
            parts = item.split(None, 1)
            if len(parts) != 2:
                continue
            key, value = parts
        attributes[key.strip()] = value.strip().strip('"')
    return attributes


def _load_gencode_annotations(annotation_gtf):
    annotation_gtf = Path(annotation_gtf)
    if not annotation_gtf.is_file():
        raise ValueError(f'GTF annotation does not exist: {annotation_gtf}')
    opener = gzip.open if annotation_gtf.suffix.lower() == '.gz' else open
    gene_rows = []
    transcript_rows = []
    with opener(annotation_gtf, 'rt', encoding='utf-8', errors='replace') as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip('\n\r')
            if not line or line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) != 9:
                raise ValueError(f'GTF row {line_number} must contain 9 columns')
            feature = fields[2].lower()
            if feature not in {'gene', 'transcript'}:
                continue
            try:
                start = int(fields[3])
                end = int(fields[4])
            except ValueError as exc:
                raise ValueError(f'GTF row {line_number} has invalid coordinates') from exc
            attributes = _parse_gtf_attributes(fields[8])
            gene_id = attributes.get('gene_id') or attributes.get('gene') or attributes.get('ID')
            if not gene_id:
                continue
            row = {
                'chrom': fields[0],
                'start': start,
                'end': end,
                'gene_id': gene_id,
                'gene_name': attributes.get('gene_name') or attributes.get('Name', ''),
                'gene_type': attributes.get('gene_type') or attributes.get('gene_biotype', ''),
                'transcript_id': attributes.get('transcript_id') or attributes.get('transcript', ''),
            }
            (gene_rows if feature == 'gene' else transcript_rows).append(row)
    rows = gene_rows or transcript_rows
    if not rows:
        raise ValueError('GTF annotation has no gene or transcript records with gene identifiers')
    annotation = pd.DataFrame(rows)
    annotation['chrom'] = annotation['chrom'].map(_normalize_chrom)
    annotation['start'] = pd.to_numeric(annotation['start'], errors='raise').astype(int)
    annotation['end'] = pd.to_numeric(annotation['end'], errors='raise').astype(int)
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
                      annotation_backend='auto', annotation_gtf=None):
    requested = str(annotation_backend or 'auto').lower()
    if requested not in VARIANT_ANNOTATION_BACKENDS:
        raise ValueError(f'unknown variant annotation backend: {requested}')
    if annotation_csv and annotation_gtf:
        raise ValueError('provide only one of annotation_csv and annotation_gtf')
    if annotation_gtf:
        if requested == 'auto':
            requested = 'gencode_gtf'
        annotation = _load_gencode_annotations(annotation_gtf)
    elif requested == 'gencode_gtf':
        raise ValueError('gencode_gtf annotation requires annotation_gtf')
    else:
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
                        'transcript_id': '',
                        'gene_type': '',
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
                        'transcript_id': '',
                        'gene_type': '',
                        'effect': '',
                        'impact': '',
                        'annotation_source': 'vcf_ann',
                        'annotation_status': 'unmatched',
                    })
                    continue
                if matches:
                    annotation_source = 'gencode_gtf' if requested == 'gencode_gtf' else 'local_interval'
                    sources.add(annotation_source)
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
                            'transcript_id': str(match.get('transcript_id', '')),
                            'gene_type': str(match.get('gene_type', '')),
                            'effect': str(match.get('effect', '')),
                            'impact': str(match.get('impact', '')),
                            'annotation_source': annotation_source,
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
                        'transcript_id': '',
                        'gene_type': '',
                        'effect': '',
                        'impact': '',
                        'annotation_source': 'none',
                        'annotation_status': 'unmatched',
                    })
    if header is None:
        raise ValueError('VCF header is missing')
    result = pd.DataFrame(rows, columns=[
        'variant_id', 'chrom', 'pos', 'ref', 'alt', 'qual', 'filter',
        'gene_id', 'gene_name', 'transcript_id', 'gene_type', 'effect', 'impact', 'annotation_source',
        'annotation_status',
    ])
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    gene_ids = sorted({str(value) for value in result['gene_id'] if str(value).strip()})
    effective_backend = 'mixed' if len(sources) > 1 else (next(iter(sources)) if sources else requested)
    return variant_annotation_result(
        output_csv,
        requested,
        effective_backend,
        result,
        n_variants,
        n_alleles,
        gene_ids,
        toolchain_status(),
    )


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
    return write_omics_report(
        de,
        pathways,
        de_csv,
        pathway_csv,
        output_md,
        evidence,
    )


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
    manifest = build_omics_manifest(
        expression_csv,
        metadata_csv,
        gene_sets_csv,
        evidence_csv,
        evidence_provider,
        evidence_cache_dir,
        genome,
        gencode_gtf,
        statistics_backend,
        de_meta,
        pathway_meta,
        report_meta,
    )
    return write_omics_manifest(output_dir, manifest)


def _run_specialist_workflow(workflow, output_dir, allowed_tools):
    try:
        from .workflow_runner import run_workflow
    except ImportError:
        from workflow_runner import run_workflow
    output_dir = Path(output_dir)
    manifest_path = output_dir / 'omics_workflow_manifest.json'
    manifest = run_workflow(
        workflow,
        output_path=manifest_path,
        dry_run=False,
        allowed_tools=allowed_tools,
        continue_on_error=False,
    )
    return specialist_workflow_result(workflow, manifest, manifest_path)


def run_rnaseq_workbench(
    fastq_paths,
    output_dir,
    reference_fasta=None,
    annotation_gtf=None,
    metadata_csv=None,
    gene_sets_csv=None,
    fastq_r2_paths=None,
    evidence_csv=None,
    evidence_provider='local',
    statistics_backend='auto',
    threads=1,
    timeout=1800,
):
    output_dir = Path(output_dir)
    fastq_qc_args = {
        'fastq_paths': fastq_paths,
        'output_dir': str(output_dir / 'fastq_qc'),
        'threads': threads,
        'timeout': timeout,
    }
    alignment_args = {
        'fastq_paths': fastq_paths,
        'reference_fasta': reference_fasta,
        'output_dir': str(output_dir / 'alignment'),
        'threads': threads,
        'timeout': timeout,
    }
    if fastq_r2_paths is not None:
        fastq_qc_args['fastq_r2_paths'] = fastq_r2_paths
        alignment_args['fastq_r2_paths'] = fastq_r2_paths
    steps = [
        {
            'id': 'fastq_qc',
            'tool': 'omics_run_fastq_qc',
            'args': fastq_qc_args,
        },
    ]
    allowed_tools = ['omics_run_fastq_qc']
    if reference_fasta:
        steps.append({
            'id': 'alignment',
            'tool': 'omics_run_rnaseq_alignment',
            'depends_on': ['fastq_qc'],
            'args': alignment_args,
        })
        allowed_tools.append('omics_run_rnaseq_alignment')
    if annotation_gtf and reference_fasta:
        steps.append({
            'id': 'feature_counts',
            'tool': 'omics_run_feature_counts',
            'depends_on': ['alignment'],
            'args': {
                'alignment_paths': '${alignment.alignment_paths}',
                'annotation_gtf': annotation_gtf,
                'output_dir': str(output_dir / 'feature_counts'),
                'output_csv': str(output_dir / 'feature_counts' / 'expression_counts.csv'),
                'paired_end': bool(fastq_r2_paths),
                'threads': threads,
                'timeout': timeout,
            },
        })
        allowed_tools.append('omics_run_feature_counts')
    if metadata_csv and gene_sets_csv and annotation_gtf and reference_fasta:
        analysis_args = {
            'expression_csv': '${feature_counts.output_csv}',
            'metadata_csv': metadata_csv,
            'gene_sets_csv': gene_sets_csv,
            'evidence_provider': evidence_provider,
            'statistics_backend': statistics_backend,
            'output_dir': str(output_dir / 'analysis'),
        }
        if evidence_csv is not None:
            analysis_args['evidence_csv'] = evidence_csv
        steps.append({
            'id': 'analysis',
            'tool': 'omics_run_analysis',
            'depends_on': ['feature_counts'],
            'args': analysis_args,
        })
        allowed_tools.append('omics_run_analysis')
    workflow = {
        'name': 'rnaseq-specialist-workbench',
        'steps': steps,
    }
    return _run_specialist_workflow(workflow, output_dir, allowed_tools)


def run_variant_workbench(
    vcf_path,
    output_dir,
    annotation_csv=None,
    annotation_gtf=None,
    annotation_backend='auto',
    evidence_csv=None,
    evidence_provider='local',
):
    output_dir = Path(output_dir)
    annotation_args = {
        'vcf_path': vcf_path,
        'output_csv': str(output_dir / 'annotation' / 'variants_annotated.csv'),
        'annotation_backend': annotation_backend,
    }
    if annotation_csv is not None:
        annotation_args['annotation_csv'] = annotation_csv
    if annotation_gtf is not None:
        annotation_args['annotation_gtf'] = annotation_gtf
    evidence_args = {
        'gene_ids': '${annotation.gene_ids}',
        'provider': evidence_provider,
    }
    if evidence_csv is not None:
        evidence_args['evidence_csv'] = evidence_csv
    workflow = {
        'name': 'variant-specialist-workbench',
        'steps': [
            {
                'id': 'genomics_qc',
                'tool': 'omics_run_genomics_qc',
                'args': {
                    'input_path': vcf_path,
                    'input_type': 'vcf',
                    'output_dir': str(output_dir / 'genomics_qc'),
                },
            },
            {
                'id': 'annotation',
                'tool': 'omics_annotate_variants',
                'depends_on': ['genomics_qc'],
                'args': annotation_args,
            },
            {
                'id': 'evidence',
                'tool': 'omics_search_gene_evidence',
                'depends_on': ['annotation'],
                'args': evidence_args,
            },
        ],
    }
    return _run_specialist_workflow(workflow, output_dir, [
        'omics_run_genomics_qc',
        'omics_annotate_variants',
        'omics_search_gene_evidence',
    ])


TOOLS = build_omics_tools({
    'run_analysis': run_omics_analysis,
    'run_rnaseq_workbench': run_rnaseq_workbench,
    'run_variant_workbench': run_variant_workbench,
    'run_differential_expression': run_differential_expression,
    'run_pathway_enrichment': run_pathway_enrichment,
    'annotate_variants': annotate_variants,
    'inspect_toolchain': toolchain_status,
    'run_genomics_qc': run_genomics_qc,
    'run_fastq_qc': run_fastq_qc,
    'run_variant_calling': run_variant_calling,
    'normalize_variants': normalize_variants,
    'run_rnaseq_alignment': run_rnaseq_alignment,
    'run_feature_counts': run_feature_counts,
    'run_single_cell_qc': run_single_cell_qc,
    'run_single_cell_10x_qc': run_single_cell_10x_qc,
    'run_metagenomics_qc': run_metagenomics_qc,
    'search_gene_evidence': search_gene_evidence,
    'generate_omics_report': generate_omics_report,
}, STATISTICS_BACKENDS, VARIANT_ANNOTATION_BACKENDS, GENOMICS_QC_TYPES)


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
