"""RNA-seq domain adapter with structured tools and reproducible outputs."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, ttest_ind

try:
    from .omics_results import (
        build_omics_manifest,
        differential_expression_result,
        pathway_enrichment_result,
        write_omics_manifest,
        write_omics_report,
    )
    from .omics_validation import (
        GENOMICS_QC_TYPES,
        condition_pair as _condition_pair,
        infer_qc_type as _infer_qc_type,
        load_expression_matrix,
        normalize_alignment_paths as _normalize_alignment_paths,
        require_columns as _require_columns,
    )
    from .omics_protocol import build_omics_tools
    from .omics_fastq_qc import execute_fastq_qc, execute_genomics_qc
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
    from .omics_variant_annotation import (
        VARIANT_ANNOTATION_BACKENDS,
        execute_variant_annotation,
    )
    from .omics_workbenches import run_rnaseq_workbench, run_variant_workbench
except ImportError:
    from omics_results import (
        build_omics_manifest,
        differential_expression_result,
        pathway_enrichment_result,
        write_omics_manifest,
        write_omics_report,
    )
    from omics_validation import (
        GENOMICS_QC_TYPES,
        condition_pair as _condition_pair,
        infer_qc_type as _infer_qc_type,
        load_expression_matrix,
        normalize_alignment_paths as _normalize_alignment_paths,
        require_columns as _require_columns,
    )
    from omics_protocol import build_omics_tools
    from omics_fastq_qc import execute_fastq_qc, execute_genomics_qc
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
    from omics_variant_annotation import (
        VARIANT_ANNOTATION_BACKENDS,
        execute_variant_annotation,
    )
    from omics_workbenches import run_rnaseq_workbench, run_variant_workbench

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
        capture_command=_run_external_qc,
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
    return execute_genomics_qc(
        input_path,
        output_dir,
        input_type=input_type,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


def run_fastq_qc(fastq_paths, output_dir, fastq_r2_paths=None, threads=1,
                 timeout=900):
    return execute_fastq_qc(
        fastq_paths,
        output_dir,
        fastq_r2_paths=fastq_r2_paths,
        threads=threads,
        timeout=timeout,
        dependencies=_external_tool_dependencies(),
    )


def annotate_variants(vcf_path, output_csv, annotation_csv=None,
                      annotation_backend='auto', annotation_gtf=None):
    return execute_variant_annotation(
        vcf_path,
        output_csv,
        annotation_csv=annotation_csv,
        annotation_backend=annotation_backend,
        annotation_gtf=annotation_gtf,
        toolchain=toolchain_status(),
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
