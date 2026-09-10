"""Workflow orchestration for specialist omics workbenches."""

from pathlib import Path

try:
    from .omics_results import specialist_workflow_result
except ImportError:
    from omics_results import specialist_workflow_result


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


def build_rnaseq_workbench(
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
    steps = [{
        'id': 'fastq_qc',
        'tool': 'omics_run_fastq_qc',
        'args': fastq_qc_args,
    }]
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
                'output_csv': str(
                    output_dir / 'feature_counts' / 'expression_counts.csv'
                ),
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
    return {
        'name': 'rnaseq-specialist-workbench',
        'steps': steps,
    }, allowed_tools


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
    workflow, allowed_tools = build_rnaseq_workbench(
        fastq_paths,
        output_dir,
        reference_fasta=reference_fasta,
        annotation_gtf=annotation_gtf,
        metadata_csv=metadata_csv,
        gene_sets_csv=gene_sets_csv,
        fastq_r2_paths=fastq_r2_paths,
        evidence_csv=evidence_csv,
        evidence_provider=evidence_provider,
        statistics_backend=statistics_backend,
        threads=threads,
        timeout=timeout,
    )
    return _run_specialist_workflow(workflow, output_dir, allowed_tools)


def build_variant_workbench(
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
    return {
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
    }, [
        'omics_run_genomics_qc',
        'omics_annotate_variants',
        'omics_search_gene_evidence',
    ]


def run_variant_workbench(
    vcf_path,
    output_dir,
    annotation_csv=None,
    annotation_gtf=None,
    annotation_backend='auto',
    evidence_csv=None,
    evidence_provider='local',
):
    workflow, allowed_tools = build_variant_workbench(
        vcf_path,
        output_dir,
        annotation_csv=annotation_csv,
        annotation_gtf=annotation_gtf,
        annotation_backend=annotation_backend,
        evidence_csv=evidence_csv,
        evidence_provider=evidence_provider,
    )
    return _run_specialist_workflow(workflow, output_dir, allowed_tools)
