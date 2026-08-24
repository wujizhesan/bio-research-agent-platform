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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, ttest_ind

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


def run_variant_calling(bam_path, reference_fasta, output_dir, output_vcf=None,
                        region=None, min_mapping_quality=0, min_base_quality=13,
                        timeout=600):
    bam_path = Path(bam_path)
    reference_fasta = Path(reference_fasta)
    if not bam_path.is_file():
        raise ValueError(f'BAM/CRAM input does not exist: {bam_path}')
    if bam_path.suffix.lower() not in {'.bam', '.cram'}:
        raise ValueError('variant calling requires a BAM or CRAM input')
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_vcf = Path(output_vcf) if output_vcf else output_dir / 'variants.vcf'
    output_vcf.parent.mkdir(parents=True, exist_ok=True)
    raw_bcf = output_dir / 'mpileup.bcf'
    stats_path = output_dir / 'bcftools_stats.txt'
    timeout = max(1, min(int(timeout), 3600))
    min_mapping_quality = max(0, int(min_mapping_quality))
    min_base_quality = max(0, int(min_base_quality))
    samtools = shutil.which('samtools')
    bcftools = shutil.which('bcftools')
    missing_tools = [name for name, path in (('samtools', samtools), ('bcftools', bcftools)) if not path]
    provenance = {
        'inputs': {
            'bam_or_cram': {'path': str(bam_path), 'sha256': _file_sha256(bam_path)},
            'reference_fasta': {'path': str(reference_fasta), 'sha256': _file_sha256(reference_fasta)},
        },
        'parameters': {
            'region': region,
            'min_mapping_quality': min_mapping_quality,
            'min_base_quality': min_base_quality,
        },
        'tools': {},
    }
    if missing_tools:
        return _write_qc_manifest(output_dir, {
            'status': 'unavailable',
            'workflow': 'reference_based_variant_calling',
            'input_type': 'bam' if bam_path.suffix.lower() == '.bam' else 'cram',
            'output_vcf': str(output_vcf),
            'missing_tools': missing_tools,
            'reason': 'required native variant-calling tools are not installed',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'samtools': {'path': samtools, **_external_tool_version(samtools)},
        'bcftools': {'path': bcftools, **_external_tool_version(bcftools)},
    }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = _run_variant_command(command, timeout, stdout_path)
        steps.append({
            'id': step_id,
            'command': command,
            **result,
        })
        return result

    reference_index = Path(str(reference_fasta) + '.fai')
    if not reference_index.is_file():
        result = execute('reference_index', [samtools, 'faidx', str(reference_fasta)])
        if result['status'] != 'completed':
            return _write_qc_manifest(output_dir, {
                'status': 'failed',
                'workflow': 'reference_based_variant_calling',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    bam_index = Path(str(bam_path) + '.bai')
    cram_index = Path(str(bam_path) + '.crai')
    alternate_index = bam_path.with_suffix('.bai')
    if not any(path.is_file() for path in (bam_index, cram_index, alternate_index)):
        result = execute('alignment_index', [samtools, 'index', str(bam_path)])
        if result['status'] != 'completed':
            return _write_qc_manifest(output_dir, {
                'status': 'failed',
                'workflow': 'reference_based_variant_calling',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    mpileup_command = [
        bcftools, 'mpileup', '-Ou', '-f', str(reference_fasta),
        '-q', str(min_mapping_quality), '-Q', str(min_base_quality),
    ]
    if region:
        mpileup_command.extend(['-r', str(region)])
    mpileup_command.extend(['-o', str(raw_bcf), str(bam_path)])
    result = execute('mpileup', mpileup_command, output_dir / 'mpileup.log')
    if result['status'] != 'completed':
        return _write_qc_manifest(output_dir, {
            'status': 'failed',
            'workflow': 'reference_based_variant_calling',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    result = execute(
        'variant_call',
        [bcftools, 'call', '-mv', '-Ov', '-o', str(output_vcf), str(raw_bcf)],
        output_dir / 'variant_call.log',
    )
    if result['status'] != 'completed' or not output_vcf.is_file():
        return _write_qc_manifest(output_dir, {
            'status': 'failed',
            'workflow': 'reference_based_variant_calling',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    stats = execute('variant_stats', [bcftools, 'stats', str(output_vcf)], stats_path)
    stats_text = stats_path.read_text(encoding='utf-8') if stats_path.is_file() else ''
    return _write_qc_manifest(output_dir, {
        'status': 'completed' if stats['status'] == 'completed' else 'failed',
        'workflow': 'reference_based_variant_calling',
        'input_type': 'bam' if bam_path.suffix.lower() == '.bam' else 'cram',
        'inputs': [str(bam_path), str(reference_fasta)],
        'output_vcf': str(output_vcf),
        'raw_bcf': str(raw_bcf),
        'number_of_records': _parse_stat_value(stats_text, 'number of records'),
        'steps': steps,
        'provenance': provenance,
    })


def normalize_variants(vcf_path, reference_fasta, output_dir, output_vcf=None,
                       region=None, timeout=300):
    vcf_path = Path(vcf_path)
    reference_fasta = Path(reference_fasta)
    if not vcf_path.is_file():
        raise ValueError(f'VCF input does not exist: {vcf_path}')
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_vcf = Path(output_vcf) if output_vcf else output_dir / 'normalized.vcf'
    output_vcf.parent.mkdir(parents=True, exist_ok=True)
    stats_path = output_dir / 'normalized_bcftools_stats.txt'
    timeout = max(1, min(int(timeout), 3600))
    bcftools = shutil.which('bcftools')
    samtools = shutil.which('samtools')
    reference_index = Path(str(reference_fasta) + '.fai')
    missing_tools = []
    if not bcftools:
        missing_tools.append('bcftools')
    if not reference_index.is_file() and not samtools:
        missing_tools.append('samtools')
    provenance = {
        'inputs': {
            'vcf': {'path': str(vcf_path), 'sha256': _file_sha256(vcf_path)},
            'reference_fasta': {'path': str(reference_fasta), 'sha256': _file_sha256(reference_fasta)},
        },
        'parameters': {'region': region, 'normalization': 'left-align-indels-and-split-multiallelic'},
        'tools': {},
    }
    if missing_tools:
        return _write_variant_manifest(output_dir, {
            'status': 'unavailable',
            'workflow': 'vcf_normalization',
            'input_type': 'vcf',
            'output_vcf': str(output_vcf),
            'missing_tools': sorted(set(missing_tools)),
            'reason': 'required native VCF normalization tools are not installed',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'bcftools': {'path': bcftools, **_external_tool_version(bcftools)},
    }
    if samtools:
        provenance['tools']['samtools'] = {
            'path': samtools,
            **_external_tool_version(samtools),
        }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = _run_variant_command(command, timeout, stdout_path)
        steps.append({'id': step_id, 'command': command, **result})
        return result

    if not reference_index.is_file():
        result = execute('reference_index', [samtools, 'faidx', str(reference_fasta)])
        if result['status'] != 'completed':
            return _write_variant_manifest(output_dir, {
                'status': 'failed',
                'workflow': 'vcf_normalization',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    output_format = '-Ob' if output_vcf.suffix.lower() == '.bcf' else '-Oz' if output_vcf.suffix.lower() == '.gz' else '-Ov'
    normalize_command = [
        bcftools, 'norm', '-f', str(reference_fasta), '-m', '-any',
        output_format, '-o', str(output_vcf),
    ]
    if region:
        normalize_command.extend(['-r', str(region)])
    normalize_command.append(str(vcf_path))
    result = execute('normalize', normalize_command, output_dir / 'normalize.log')
    if result['status'] != 'completed' or not output_vcf.is_file():
        return _write_variant_manifest(output_dir, {
            'status': 'failed',
            'workflow': 'vcf_normalization',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    stats = execute('stats', [bcftools, 'stats', str(output_vcf)], stats_path)
    stats_text = stats_path.read_text(encoding='utf-8') if stats_path.is_file() else ''
    return _write_variant_manifest(output_dir, {
        'status': 'completed' if stats['status'] == 'completed' else 'failed',
        'workflow': 'vcf_normalization',
        'input_type': 'vcf',
        'inputs': [str(vcf_path), str(reference_fasta)],
        'output_vcf': str(output_vcf),
        'number_of_records': _parse_stat_value(stats_text, 'number of records'),
        'steps': steps,
        'provenance': provenance,
    })


def _normalize_alignment_paths(alignment_paths):
    values = alignment_paths if isinstance(alignment_paths, (list, tuple)) else [alignment_paths]
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('alignment_paths must contain at least one BAM/CRAM path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'alignment files do not exist: {missing}')
    invalid = [str(path) for path in paths if path.suffix.lower() not in {'.bam', '.cram'}]
    if invalid:
        raise ValueError(f'featureCounts requires BAM/CRAM inputs: {invalid}')
    return paths


def _feature_counts_sample_name(value):
    normalized = str(value).replace('\\', '/')
    name = normalized.rsplit('/', 1)[-1]
    for suffix in ('.bam', '.cram'):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return Path(name).stem


def _parse_feature_counts_output(counts_path, output_csv):
    frame = pd.read_csv(counts_path, sep='\t', comment='#')
    if frame.empty or 'Geneid' not in frame.columns:
        raise ValueError('featureCounts output is missing the Geneid column')
    sample_columns = list(frame.columns[6:])
    if not sample_columns:
        raise ValueError('featureCounts output has no sample count columns')
    sample_names = [_feature_counts_sample_name(column) for column in sample_columns]
    if len(set(sample_names)) != len(sample_names):
        raise ValueError('featureCounts sample names are not unique after normalization')
    result = frame.loc[:, ['Geneid', *sample_columns]].rename(columns={'Geneid': 'gene_id'})
    result['gene_id'] = result['gene_id'].astype(str)
    if result['gene_id'].eq('').any() or result['gene_id'].duplicated().any():
        raise ValueError('featureCounts gene identifiers must be non-empty and unique')
    result = result.rename(columns=dict(zip(sample_columns, sample_names)))
    result[sample_names] = result[sample_names].apply(pd.to_numeric, errors='raise')
    if result[sample_names].isna().any().any() or (result[sample_names] < 0).any().any():
        raise ValueError('featureCounts counts must be non-negative numbers')
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    return result, sample_names


def _normalize_fastq_paths(fastq_paths):
    values = fastq_paths if isinstance(fastq_paths, (list, tuple)) else [fastq_paths]
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('fastq_paths must contain at least one FASTQ path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'FASTQ inputs do not exist: {missing}')
    invalid = [str(path) for path in paths if not path.name.lower().endswith(('.fastq', '.fq', '.fastq.gz', '.fq.gz'))]
    if invalid:
        raise ValueError(f'RNA-seq alignment requires FASTQ inputs: {invalid}')
    return paths


def _fastq_sample_name(path):
    name = Path(path).name
    for suffix in ('.fastq.gz', '.fq.gz', '.fastq', '.fq'):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    for suffix in ('_R1', '_R2', '.R1', '.R2', '_1', '_2', '.1', '.2'):
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)]
            break
    return name


def _parse_hisat2_alignment_rate(stderr):
    for line in str(stderr or '').splitlines():
        if 'overall alignment rate' in line.lower():
            return line.strip().split()[0]
    return None


def _hisat2_index_complete(prefix):
    prefix = str(prefix)
    return any(Path(f'{prefix}.{suffix}').is_file() for suffix in ('1.ht2', '1.ht2l'))


def run_rnaseq_alignment(fastq_paths, reference_fasta, output_dir,
                         output_alignment_paths=None, fastq_r2_paths=None,
                         threads=1, timeout=1800):
    paths = _normalize_fastq_paths(fastq_paths)
    mate_paths = _normalize_fastq_paths(fastq_r2_paths) if fastq_r2_paths is not None else None
    if mate_paths is not None and len(mate_paths) != len(paths):
        raise ValueError('fastq_r2_paths must match the number of FASTQ R1 inputs')
    reference_fasta = Path(reference_fasta)
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    threads = max(1, min(int(threads), 64))
    timeout = max(1, min(int(timeout), 3600))
    sample_names = [_fastq_sample_name(path) for path in paths]
    if len(set(sample_names)) != len(sample_names):
        raise ValueError('FASTQ sample names must be unique')
    if mate_paths is not None:
        mate_sample_names = [_fastq_sample_name(path) for path in mate_paths]
        if sample_names != mate_sample_names:
            raise ValueError('FASTQ R1 and R2 sample names must match')
    if output_alignment_paths is None:
        alignment_paths = [output_dir / f'{sample_name}.bam' for sample_name in sample_names]
    else:
        values = output_alignment_paths if isinstance(output_alignment_paths, (list, tuple)) else [output_alignment_paths]
        if len(values) != len(paths):
            raise ValueError('output_alignment_paths must match the number of FASTQ inputs')
        alignment_paths = [Path(str(value)) for value in values]
    for path in alignment_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    hisat2 = shutil.which('hisat2')
    hisat2_build = shutil.which('hisat2-build')
    samtools = shutil.which('samtools')
    missing_tools = [
        name for name, path in (
            ('hisat2', hisat2),
            ('hisat2-build', hisat2_build),
            ('samtools', samtools),
        ) if not path
    ]
    index_prefix = output_dir / 'hisat2_index'
    index_metadata = output_dir / 'hisat2_index.json'
    reference_sha256 = _file_sha256(reference_fasta)
    provenance = {
        'inputs': {
            'fastq': [
                {'path': str(path), 'sha256': _file_sha256(path)} for path in paths
            ],
            'reference_fasta': {
                'path': str(reference_fasta),
                'sha256': reference_sha256,
            },
        },
        'parameters': {
            'layout': 'paired_end' if mate_paths is not None else 'single_end',
            'threads': threads,
        },
        'tools': {},
    }
    if mate_paths is not None:
        provenance['inputs']['fastq_r2'] = [
            {'path': str(path), 'sha256': _file_sha256(path)} for path in mate_paths
        ]
    if missing_tools:
        return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
            'status': 'unavailable',
            'workflow': 'rnaseq_alignment',
            'alignment_paths': [str(path) for path in alignment_paths],
            'missing_tools': missing_tools,
            'reason': 'HISAT2 and SAMtools are required for RNA-seq alignment',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'hisat2': {'path': hisat2, **_external_tool_version(hisat2)},
        'hisat2-build': {'path': hisat2_build, **_external_tool_version(hisat2_build)},
        'samtools': {'path': samtools, **_external_tool_version(samtools)},
    }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = _run_variant_command(command, timeout, stdout_path)
        steps.append({'id': step_id, 'command': command, **result})
        return result

    index_reusable = False
    if _hisat2_index_complete(index_prefix) and index_metadata.is_file():
        try:
            metadata = json.loads(index_metadata.read_text(encoding='utf-8'))
            index_reusable = metadata.get('reference_sha256') == reference_sha256
        except (OSError, ValueError, TypeError):
            index_reusable = False
    if not index_reusable:
        index_result = execute(
            'hisat2_index',
            [hisat2_build, '-p', str(threads), str(reference_fasta), str(index_prefix)],
            output_dir / 'hisat2_build.log',
        )
        if index_result['status'] != 'completed' or not _hisat2_index_complete(index_prefix):
            return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        index_metadata.write_text(
            json.dumps({
                'reference_fasta': str(reference_fasta),
                'reference_sha256': reference_sha256,
                'index_prefix': str(index_prefix),
            }, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
    sample_results = []
    for fastq_path, mate_path, sample_name, alignment_path in zip(
        paths, mate_paths or [None] * len(paths), sample_names, alignment_paths
    ):
        sam_path = output_dir / f'{sample_name}.hisat2.sam'
        raw_bam = output_dir / f'{sample_name}.hisat2.raw.bam'
        hisat2_reads = (
            ['-1', str(fastq_path), '-2', str(mate_path)]
            if mate_path is not None
            else ['-U', str(fastq_path)]
        )
        hisat2_result = execute(
            f'hisat2_{sample_name}',
            [
                hisat2, '-p', str(threads), '--dta',
                '-x', str(index_prefix), *hisat2_reads,
                '-S', str(sam_path),
            ],
            output_dir / f'{sample_name}.hisat2.log',
        )
        if hisat2_result['status'] != 'completed' or not sam_path.is_file():
            return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        view_result = execute(
            f'samtools_view_{sample_name}',
            [samtools, 'view', '-b', '-o', str(raw_bam), str(sam_path)],
            output_dir / f'{sample_name}.view.log',
        )
        if view_result['status'] != 'completed':
            return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        sort_result = execute(
            f'samtools_sort_{sample_name}',
            [samtools, 'sort', '-o', str(alignment_path), str(raw_bam)],
            output_dir / f'{sample_name}.sort.log',
        )
        if sort_result['status'] != 'completed' or not alignment_path.is_file():
            return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        index_result = execute(
            f'samtools_index_{sample_name}',
            [samtools, 'index', str(alignment_path)],
            output_dir / f'{sample_name}.index.log',
        )
        if index_result['status'] != 'completed':
            return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        sam_path.unlink(missing_ok=True)
        raw_bam.unlink(missing_ok=True)
        sample_result = {
            'sample_id': sample_name,
            'bam_path': str(alignment_path),
            'bai_path': str(Path(str(alignment_path) + '.bai')),
            'overall_alignment_rate': _parse_hisat2_alignment_rate(hisat2_result.get('stderr')),
        }
        if mate_path is None:
            sample_result['fastq_path'] = str(fastq_path)
        else:
            sample_result['fastq_r1_path'] = str(fastq_path)
            sample_result['fastq_r2_path'] = str(mate_path)
        sample_results.append(sample_result)
    return _write_omics_manifest(output_dir, 'rnaseq_alignment.json', {
        'status': 'completed',
        'workflow': 'rnaseq_alignment',
        'reference_fasta': str(reference_fasta),
        'index_prefix': str(index_prefix),
        'alignment_paths': [str(path) for path in alignment_paths],
        'samples': sample_results,
        'steps': steps,
        'provenance': provenance,
    })


def run_feature_counts(alignment_paths, annotation_gtf, output_dir, output_csv=None,
                       feature_type='exon', gene_id_attribute='gene_id', strand=0,
                       paired_end=False, threads=1, timeout=900):
    paths = _normalize_alignment_paths(alignment_paths)
    annotation_gtf = Path(annotation_gtf)
    if not annotation_gtf.is_file():
        raise ValueError(f'GTF annotation does not exist: {annotation_gtf}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = Path(output_csv) if output_csv else output_dir / 'expression_counts.csv'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    raw_counts = output_dir / 'featurecounts.tsv'
    summary_path = Path(str(raw_counts) + '.summary')
    timeout = max(1, min(int(timeout), 3600))
    threads = max(1, min(int(threads), 64))
    strand = int(strand)
    if strand not in {0, 1, 2}:
        raise ValueError('strand must be 0, 1 or 2')
    executable = shutil.which('featureCounts')
    provenance = {
        'inputs': {
            'alignments': [
                {'path': str(path), 'sha256': _file_sha256(path)} for path in paths
            ],
            'annotation_gtf': {
                'path': str(annotation_gtf),
                'sha256': _file_sha256(annotation_gtf),
            },
        },
        'parameters': {
            'feature_type': str(feature_type),
            'gene_id_attribute': str(gene_id_attribute),
            'strand': strand,
            'paired_end': bool(paired_end),
            'threads': threads,
        },
        'tool': {},
    }
    if not executable:
        return _write_omics_manifest(output_dir, 'feature_counts.json', {
            'status': 'unavailable',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'missing_tools': ['featureCounts'],
            'reason': 'featureCounts not found in PATH; install the Subread package',
            'provenance': provenance,
        })
    provenance['tool'] = {
        'path': executable,
        **_external_tool_version(executable),
    }
    command = [
        executable, '-T', str(threads), '-a', str(annotation_gtf),
        '-t', str(feature_type), '-g', str(gene_id_attribute),
        '-s', str(strand), '-o', str(raw_counts),
    ]
    if paired_end:
        command.extend(['-p', '--countReadPairs'])
    command.extend(str(path) for path in paths)
    result = _run_variant_command(command, timeout, output_dir / 'featurecounts.log')
    if result['status'] != 'completed' or not raw_counts.is_file():
        return _write_omics_manifest(output_dir, 'feature_counts.json', {
            'status': 'failed',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'featurecounts_output': str(raw_counts),
            'summary_path': str(summary_path),
            'command': command,
            'command_result': result,
            'provenance': provenance,
        })
    try:
        counts, sample_names = _parse_feature_counts_output(raw_counts, output_csv)
    except Exception as exc:
        return _write_omics_manifest(output_dir, 'feature_counts.json', {
            'status': 'failed',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'featurecounts_output': str(raw_counts),
            'summary_path': str(summary_path),
            'command': command,
            'error': str(exc),
            'provenance': provenance,
        })
    return _write_omics_manifest(output_dir, 'feature_counts.json', {
        'status': 'completed',
        'workflow': 'rnaseq_feature_counts',
        'output_csv': str(output_csv),
        'featurecounts_output': str(raw_counts),
        'summary_path': str(summary_path),
        'command': command,
        'n_genes': int(len(counts)),
        'n_samples': len(sample_names),
        'sample_names': sample_names,
        'provenance': provenance,
    })


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


def run_single_cell_qc(matrix_csv, output_dir, cell_id_column='cell_id',
                       min_genes=0, max_genes=None, min_counts=0,
                       max_mito_percent=100, mitochondrial_prefix='MT-'):
    matrix_csv = Path(matrix_csv)
    if not matrix_csv.is_file():
        raise ValueError(f'single-cell matrix does not exist: {matrix_csv}')
    frame = pd.read_csv(matrix_csv)
    if frame.empty:
        raise ValueError('single-cell expression matrix is empty')
    if cell_id_column not in frame.columns:
        raise ValueError(f'single-cell matrix requires column: {cell_id_column}')
    gene_columns = [column for column in frame.columns if column != cell_id_column]
    if not gene_columns:
        raise ValueError('single-cell matrix has no gene columns')
    raw_cell_ids = frame[cell_id_column]
    if raw_cell_ids.isna().any():
        raise ValueError('cell identifiers must be non-empty and unique')
    cell_ids = raw_cell_ids.astype(str).str.strip()
    if cell_ids.eq('').any() or cell_ids.duplicated().any():
        raise ValueError('cell identifiers must be non-empty and unique')
    counts = frame[gene_columns].apply(pd.to_numeric, errors='raise')
    if counts.isna().any().any() or (counts < 0).any().any():
        raise ValueError('single-cell counts must be non-negative numbers')
    min_genes = max(0, int(min_genes))
    min_counts = max(0, float(min_counts))
    max_mito_percent = float(max_mito_percent)
    if max_genes is not None:
        max_genes = max(0, int(max_genes))
    if max_mito_percent < 0 or max_mito_percent > 100:
        raise ValueError('max_mito_percent must be between 0 and 100')
    prefix = str(mitochondrial_prefix or 'MT-').upper()
    mito_columns = [
        column for column in gene_columns
        if str(column).upper().startswith(prefix)
    ]
    total_counts = counts.sum(axis=1)
    n_genes = (counts > 0).sum(axis=1)
    mito_counts = counts[mito_columns].sum(axis=1) if mito_columns else pd.Series(0.0, index=counts.index)
    mito_percent = (mito_counts / total_counts.replace(0, np.nan) * 100).fillna(0.0)
    metrics = pd.DataFrame({
        cell_id_column: cell_ids,
        'n_genes_by_counts': n_genes.astype(int),
        'total_counts': total_counts,
        'total_counts_mito': mito_counts,
        'pct_counts_mito': mito_percent,
    })
    keep = metrics['n_genes_by_counts'] >= min_genes
    keep &= metrics['total_counts'] >= min_counts
    keep &= metrics['pct_counts_mito'] <= max_mito_percent
    if max_genes is not None:
        keep &= metrics['n_genes_by_counts'] <= max_genes
    metrics['pass_qc'] = keep.astype(bool)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / 'single_cell_cell_metrics.csv'
    filtered_path = output_dir / 'single_cell_filtered_matrix.csv'
    manifest_path = output_dir / 'single_cell_qc.json'
    metrics.to_csv(metrics_path, index=False)
    filtered = frame.loc[keep].copy()
    filtered.to_csv(filtered_path, index=False)
    payload = {
        'status': 'completed',
        'input': str(matrix_csv),
        'output_dir': str(output_dir),
        'outputs': {
            'cell_metrics': str(metrics_path),
            'filtered_matrix': str(filtered_path),
        },
        'metrics': {
            'n_cells_input': int(len(frame)),
            'n_cells_passed': int(keep.sum()),
            'n_cells_filtered': int((~keep).sum()),
            'n_genes': len(gene_columns),
            'mitochondrial_genes': mito_columns,
        },
        'thresholds': {
            'min_genes': min_genes,
            'max_genes': max_genes,
            'min_counts': min_counts,
            'max_mito_percent': max_mito_percent,
            'mitochondrial_prefix': mitochondrial_prefix,
        },
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def _open_10x_text(path):
    return gzip.open(path, 'rt', encoding='utf-8', errors='replace') if str(path).lower().endswith('.gz') else open(path, 'r', encoding='utf-8', errors='replace')


def _read_10x_table(path):
    with _open_10x_text(path) as handle:
        return [line.rstrip('\r\n').split('\t') for line in handle if line.rstrip('\r\n')]


def run_single_cell_10x_qc(matrix_mtx, barcodes_tsv, features_tsv, output_dir,
                           min_genes=0, max_genes=None, min_counts=0,
                           max_mito_percent=100, mitochondrial_prefix='MT-'):
    from scipy.io import mmread, mmwrite
    from scipy.sparse import csr_matrix

    matrix_mtx = Path(matrix_mtx)
    barcodes_tsv = Path(barcodes_tsv)
    features_tsv = Path(features_tsv)
    for path in (matrix_mtx, barcodes_tsv, features_tsv):
        if not path.is_file():
            raise ValueError(f'10x input does not exist: {path}')
    with gzip.open(matrix_mtx, 'rb') if matrix_mtx.name.lower().endswith('.gz') else matrix_mtx.open('rb') as handle:
        matrix = csr_matrix(mmread(handle))
    barcodes = [row[0].strip() for row in _read_10x_table(barcodes_tsv) if row and row[0].strip()]
    features = _read_10x_table(features_tsv)
    if matrix.ndim != 2:
        raise ValueError('10x matrix must be two-dimensional')
    if matrix.shape[1] != len(barcodes):
        raise ValueError(f'10x matrix/barcode mismatch: {matrix.shape[1]} != {len(barcodes)}')
    if matrix.shape[0] != len(features):
        raise ValueError(f'10x matrix/feature mismatch: {matrix.shape[0]} != {len(features)}')
    if len(set(barcodes)) != len(barcodes):
        raise ValueError('10x barcodes must be unique')
    if any(value < 0 or not np.isfinite(value) for value in matrix.data):
        raise ValueError('10x counts must be finite and non-negative')
    feature_ids = [row[0].strip() for row in features]
    feature_names = [row[1].strip() if len(row) > 1 and row[1].strip() else row[0].strip() for row in features]
    feature_types = [row[2].strip() if len(row) > 2 else '' for row in features]
    gene_expression_indices = [
        index for index, feature_type in enumerate(feature_types)
        if feature_type.lower() == 'gene expression'
    ]
    if gene_expression_indices:
        matrix = matrix[gene_expression_indices, :]
        feature_ids = [feature_ids[index] for index in gene_expression_indices]
        feature_names = [feature_names[index] for index in gene_expression_indices]
        feature_types = [feature_types[index] for index in gene_expression_indices]
    min_genes = max(0, int(min_genes))
    min_counts = max(0, float(min_counts))
    max_mito_percent = float(max_mito_percent)
    if max_genes is not None:
        max_genes = max(0, int(max_genes))
    if max_mito_percent < 0 or max_mito_percent > 100:
        raise ValueError('max_mito_percent must be between 0 and 100')
    prefix = str(mitochondrial_prefix or 'MT-').upper()
    mito_indices = [
        index for index, name in enumerate(feature_names)
        if str(name).upper().startswith(prefix)
    ]
    cell_counts = np.asarray(matrix.sum(axis=0)).ravel()
    cell_genes = np.asarray(matrix.getnnz(axis=0)).ravel()
    mito_counts = (
        np.asarray(matrix[mito_indices, :].sum(axis=0)).ravel()
        if mito_indices else np.zeros(matrix.shape[1])
    )
    mito_percent = np.divide(
        mito_counts * 100,
        cell_counts,
        out=np.zeros_like(mito_counts, dtype=float),
        where=cell_counts != 0,
    )
    metrics = pd.DataFrame({
        'cell_id': barcodes,
        'n_genes_by_counts': cell_genes.astype(int),
        'total_counts': cell_counts,
        'total_counts_mito': mito_counts,
        'pct_counts_mito': mito_percent,
    })
    keep = metrics['n_genes_by_counts'] >= min_genes
    keep &= metrics['total_counts'] >= min_counts
    keep &= metrics['pct_counts_mito'] <= max_mito_percent
    if max_genes is not None:
        keep &= metrics['n_genes_by_counts'] <= max_genes
    metrics['pass_qc'] = keep.astype(bool)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / 'single_cell_10x_cell_metrics.csv'
    matrix_path = output_dir / 'single_cell_10x_filtered.mtx'
    barcodes_path = output_dir / 'single_cell_10x_filtered_barcodes.tsv'
    features_path = output_dir / 'single_cell_10x_filtered_features.tsv'
    manifest_path = output_dir / 'single_cell_10x_qc.json'
    metrics.to_csv(metrics_path, index=False)
    mmwrite(str(matrix_path), matrix[:, np.flatnonzero(keep.to_numpy())])
    with barcodes_path.open('w', encoding='utf-8') as handle:
        handle.write('\n'.join(barcode for barcode, passed in zip(barcodes, keep) if passed) + '\n')
    with features_path.open('w', encoding='utf-8') as handle:
        for feature_id, feature_name, feature_type in zip(feature_ids, feature_names, feature_types):
            handle.write('\t'.join((feature_id, feature_name, feature_type)) + '\n')
    payload = {
        'status': 'completed',
        'input_format': '10x_matrix_market',
        'inputs': {
            'matrix_mtx': str(matrix_mtx),
            'barcodes_tsv': str(barcodes_tsv),
            'features_tsv': str(features_tsv),
        },
        'outputs': {
            'cell_metrics': str(metrics_path),
            'filtered_matrix': str(matrix_path),
            'filtered_barcodes': str(barcodes_path),
            'filtered_features': str(features_path),
        },
        'metrics': {
            'n_cells_input': len(barcodes),
            'n_cells_passed': int(keep.sum()),
            'n_cells_filtered': int((~keep).sum()),
            'n_features_input': len(features),
            'n_gene_expression_features': len(feature_ids),
            'mitochondrial_features': [feature_names[index] for index in mito_indices],
        },
        'thresholds': {
            'min_genes': min_genes,
            'max_genes': max_genes,
            'min_counts': min_counts,
            'max_mito_percent': max_mito_percent,
            'mitochondrial_prefix': mitochondrial_prefix,
        },
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def run_metagenomics_qc(abundance_csv, output_dir, taxon_id_column='taxon_id',
                        min_total_counts=0, min_prevalence=0):
    abundance_csv = Path(abundance_csv)
    if not abundance_csv.is_file():
        raise ValueError(f'metagenomics abundance table does not exist: {abundance_csv}')
    frame = pd.read_csv(abundance_csv)
    if frame.empty:
        raise ValueError('metagenomics abundance table is empty')
    if taxon_id_column not in frame.columns:
        raise ValueError(f'abundance table requires column: {taxon_id_column}')
    sample_columns = [column for column in frame.columns if column != taxon_id_column]
    if not sample_columns:
        raise ValueError('metagenomics abundance table has no sample columns')
    taxon_ids = frame[taxon_id_column]
    if taxon_ids.isna().any():
        raise ValueError('taxon identifiers must be non-empty and unique')
    taxon_ids = taxon_ids.astype(str).str.strip()
    if taxon_ids.eq('').any() or taxon_ids.duplicated().any():
        raise ValueError('taxon identifiers must be non-empty and unique')
    counts = frame[sample_columns].apply(pd.to_numeric, errors='raise')
    values = counts.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError('metagenomics abundances must be finite and non-negative')
    min_total_counts = max(0.0, float(min_total_counts))
    min_prevalence = max(0, int(min_prevalence))
    if min_prevalence > len(sample_columns):
        raise ValueError('min_prevalence cannot exceed the number of samples')
    taxon_totals = counts.sum(axis=1)
    prevalence = (counts > 0).sum(axis=1)
    keep = (taxon_totals >= min_total_counts) & (prevalence >= min_prevalence)
    filtered = counts.loc[keep].copy()
    sample_totals = filtered.sum(axis=0)
    relative = filtered.divide(sample_totals.replace(0, np.nan), axis='columns').fillna(0.0)
    relative.insert(0, taxon_id_column, taxon_ids.loc[keep].to_numpy())
    sample_metrics = []
    for sample in sample_columns:
        sample_values = counts[sample].to_numpy(dtype=float)
        total = float(sample_values.sum())
        positive = sample_values[sample_values > 0]
        probabilities = positive / total if total else np.array([], dtype=float)
        shannon = float(-(probabilities * np.log(probabilities)).sum()) if len(probabilities) else 0.0
        sample_metrics.append({
            'sample_id': sample,
            'total_counts': total,
            'observed_taxa': int((sample_values > 0).sum()),
            'shannon_index': round(shannon, 6),
            'retained_taxa': int((relative[sample] > 0).sum()),
        })
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    relative_path = output_dir / 'metagenomics_relative_abundance.csv'
    sample_metrics_path = output_dir / 'metagenomics_sample_metrics.csv'
    manifest_path = output_dir / 'metagenomics_qc.json'
    relative.to_csv(relative_path, index=False)
    pd.DataFrame(sample_metrics).to_csv(sample_metrics_path, index=False)
    payload = {
        'status': 'completed',
        'input': str(abundance_csv),
        'output_dir': str(output_dir),
        'outputs': {
            'relative_abundance': str(relative_path),
            'sample_metrics': str(sample_metrics_path),
        },
        'metrics': {
            'n_taxa_input': int(len(frame)),
            'n_taxa_retained': int(keep.sum()),
            'n_taxa_filtered': int((~keep).sum()),
            'n_samples': len(sample_columns),
        },
        'thresholds': {
            'min_total_counts': min_total_counts,
            'min_prevalence': min_prevalence,
        },
        'provenance': {
            'normalization': 'relative_abundance_after_filtering',
            'alpha_diversity': ['observed_taxa', 'shannon_index'],
        },
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


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
    return {
        'status': manifest['status'],
        'workflow': workflow.get('name', 'omics specialist workflow'),
        'manifest': manifest,
        'manifest_path': str(manifest_path),
    }


def run_rnaseq_workbench(
    fastq_paths,
    reference_fasta,
    annotation_gtf,
    metadata_csv,
    gene_sets_csv,
    output_dir,
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
    workflow = {
        'name': 'rnaseq-specialist-workbench',
        'steps': [
            {
                'id': 'fastq_qc',
                'tool': 'omics_run_fastq_qc',
                'args': fastq_qc_args,
            },
            {
                'id': 'alignment',
                'tool': 'omics_run_rnaseq_alignment',
                'depends_on': ['fastq_qc'],
                'args': alignment_args,
            },
            {
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
            },
            {
                'id': 'analysis',
                'tool': 'omics_run_analysis',
                'depends_on': ['feature_counts'],
                'args': analysis_args,
            },
        ],
    }
    return _run_specialist_workflow(workflow, output_dir, [
        'omics_run_fastq_qc',
        'omics_run_rnaseq_alignment',
        'omics_run_feature_counts',
        'omics_run_analysis',
    ])


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
    'run_rnaseq_workbench': {
        'description': 'Run the RNA-seq specialist workbench: FASTQ QC, alignment, feature counts, differential expression and pathway analysis.',
        'parameters': _parameters({
            'fastq_paths': {'oneOf': [{'type': 'string'}, {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}]},
            'fastq_r2_paths': {'oneOf': [{'type': 'string'}, {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}]},
            'reference_fasta': {'type': 'string'},
            'annotation_gtf': {'type': 'string'},
            'metadata_csv': {'type': 'string'},
            'gene_sets_csv': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'evidence_csv': {'type': 'string'},
            'evidence_provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']},
            'statistics_backend': {'type': 'string', 'enum': list(STATISTICS_BACKENDS)},
            'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('fastq_paths', 'reference_fasta', 'annotation_gtf', 'metadata_csv', 'gene_sets_csv', 'output_dir')),
        'function': run_rnaseq_workbench,
    },
    'run_variant_workbench': {
        'description': 'Run the VCF specialist workbench: variant QC, annotation and gene evidence retrieval.',
        'parameters': _parameters({
            'vcf_path': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'annotation_csv': {'type': 'string'},
            'annotation_gtf': {'type': 'string'},
            'annotation_backend': {'type': 'string', 'enum': list(VARIANT_ANNOTATION_BACKENDS)},
            'evidence_csv': {'type': 'string'},
            'evidence_provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']},
        }, required=('vcf_path', 'output_dir')),
        'function': run_variant_workbench,
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
        'description': 'Annotate VCF variants with VCF ANN records, a local interval table or GENCODE GTF gene coordinates and return traceable gene mappings.',
        'parameters': _parameters({
            'vcf_path': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'annotation_csv': {'type': 'string'},
            'annotation_gtf': {'type': 'string'},
            'annotation_backend': {'type': 'string', 'enum': list(VARIANT_ANNOTATION_BACKENDS)},
        }, required=('vcf_path', 'output_csv')),
        'function': annotate_variants,
    },
    'inspect_toolchain': {
        'description': 'Report whether FastQC, MultiQC, HISAT2, SAMtools, bcftools, featureCounts, GATK and VEP are available in the execution environment.',
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
    'run_fastq_qc': {
        'description': 'Run native FastQC on single-end or paired-end FASTQ files and aggregate the reports with MultiQC.',
        'parameters': _parameters({
            'fastq_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'output_dir': {'type': 'string'},
            'fastq_r2_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('fastq_paths', 'output_dir')),
        'function': run_fastq_qc,
    },
    'run_variant_calling': {
        'description': 'Call small variants from an indexed BAM/CRAM against a reference FASTA with SAMtools and bcftools, producing a VCF and provenance manifest.',
        'parameters': _parameters({
            'bam_path': {'type': 'string'},
            'reference_fasta': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'output_vcf': {'type': 'string'},
            'region': {'type': 'string'},
            'min_mapping_quality': {'type': 'integer', 'minimum': 0},
            'min_base_quality': {'type': 'integer', 'minimum': 0},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('bam_path', 'reference_fasta', 'output_dir')),
        'function': run_variant_calling,
    },
    'normalize_variants': {
        'description': 'Normalize a VCF against a reference FASTA with bcftools, left-align indels, split multiallelic records and emit provenance.',
        'parameters': _parameters({
            'vcf_path': {'type': 'string'},
            'reference_fasta': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'output_vcf': {'type': 'string'},
            'region': {'type': 'string'},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('vcf_path', 'reference_fasta', 'output_dir')),
        'function': normalize_variants,
    },
    'run_rnaseq_alignment': {
        'description': 'Align single-end or paired-end RNA-seq FASTQ files to a reference FASTA with HISAT2 and emit sorted indexed BAM files with alignment provenance.',
        'parameters': _parameters({
            'fastq_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'reference_fasta': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'fastq_r2_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'output_alignment_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('fastq_paths', 'reference_fasta', 'output_dir')),
        'function': run_rnaseq_alignment,
    },
    'run_feature_counts': {
        'description': 'Generate a gene-by-sample RNA-seq count matrix from BAM/CRAM alignments and a GTF annotation using featureCounts.',
        'parameters': _parameters({
            'alignment_paths': {
                'oneOf': [
                    {'type': 'string'},
                    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                ],
            },
            'annotation_gtf': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'feature_type': {'type': 'string'},
            'gene_id_attribute': {'type': 'string'},
            'strand': {'type': 'integer', 'enum': [0, 1, 2]},
            'paired_end': {'type': 'boolean'},
            'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
        }, required=('alignment_paths', 'annotation_gtf', 'output_dir')),
        'function': run_feature_counts,
    },
    'run_single_cell_qc': {
        'description': 'Calculate single-cell expression QC metrics and write a filtered cell matrix without requiring Scanpy.',
        'parameters': _parameters({
            'matrix_csv': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'cell_id_column': {'type': 'string'},
            'min_genes': {'type': 'integer', 'minimum': 0},
            'max_genes': {'type': 'integer', 'minimum': 0},
            'min_counts': {'type': 'number', 'minimum': 0},
            'max_mito_percent': {'type': 'number', 'minimum': 0, 'maximum': 100},
            'mitochondrial_prefix': {'type': 'string'},
        }, required=('matrix_csv', 'output_dir')),
        'function': run_single_cell_qc,
    },
    'run_single_cell_10x_qc': {
        'description': 'Run single-cell QC on 10x Matrix Market, barcodes and features files, preserving sparse output artifacts.',
        'parameters': _parameters({
            'matrix_mtx': {'type': 'string'},
            'barcodes_tsv': {'type': 'string'},
            'features_tsv': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'min_genes': {'type': 'integer', 'minimum': 0},
            'max_genes': {'type': 'integer', 'minimum': 0},
            'min_counts': {'type': 'number', 'minimum': 0},
            'max_mito_percent': {'type': 'number', 'minimum': 0, 'maximum': 100},
            'mitochondrial_prefix': {'type': 'string'},
        }, required=('matrix_mtx', 'barcodes_tsv', 'features_tsv', 'output_dir')),
        'function': run_single_cell_10x_qc,
    },
    'run_metagenomics_qc': {
        'description': 'Validate a taxon or feature abundance table, calculate relative abundance and sample alpha-diversity metrics.',
        'parameters': _parameters({
            'abundance_csv': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'taxon_id_column': {'type': 'string'},
            'min_total_counts': {'type': 'number', 'minimum': 0},
            'min_prevalence': {'type': 'integer', 'minimum': 0},
        }, required=('abundance_csv', 'output_dir')),
        'function': run_metagenomics_qc,
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
