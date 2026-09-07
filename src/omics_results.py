"""Result serialization for omics tools."""

from datetime import datetime, timezone
import json
from pathlib import Path


def differential_expression_result(
    output_csv,
    result,
    condition_a,
    condition_b,
    samples_a,
    samples_b,
    backend,
):
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


def pathway_enrichment_result(output_csv, result, background, selected):
    return {
        'status': 'completed',
        'output_csv': str(output_csv),
        'n_background_genes': len(background),
        'n_selected_genes': len(selected),
        'n_pathways': len(result),
        'n_significant_pathways': (
            int((result['padj'] <= 0.05).sum()) if not result.empty else 0
        ),
    }


def variant_annotation_result(
    output_csv,
    requested,
    effective_backend,
    result,
    n_variants,
    n_alleles,
    gene_ids,
    toolchain,
):
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
        'toolchain': toolchain,
    }


def write_omics_report(
    de,
    pathways,
    de_source,
    pathway_source,
    output_md,
    evidence=None,
    generated_at=None,
):
    significant = de[de['significant'].astype(bool)] if 'significant' in de else de.iloc[0:0]
    lines = [
        '# RNA-seq Agent Analysis Report',
        '',
        f'- Generated at: {generated_at or datetime.now(timezone.utc).isoformat()}',
        f'- Differential-expression result: {de_source}',
        f'- Pathway result: {pathway_source}',
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
            lines.append(
                f'| {row["pathway_name"]} | {row["overlap_count"]} | {row["padj"]:.3g} |'
            )
    if evidence:
        lines.extend(['', '## Evidence', ''])
        lines.append(f'- Evidence matches: {evidence.get("n_matches", 0)}')
        evidence_source = (
            evidence.get('source_file')
            or evidence.get('endpoint')
            or evidence.get('provider', 'n/a')
        )
        lines.append(f'- Evidence source: {evidence_source}')
        for item in evidence.get('matches', [])[:10]:
            lines.append(
                f'- **{item.get("gene_id", "")}**: {item.get("title", "")} '
                f'({item.get("source", "")})'
            )
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


def build_omics_manifest(
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
    created_at=None,
):
    return {
        'status': 'completed',
        'created_at': created_at or datetime.now(timezone.utc).isoformat(),
        'inputs': {
            'expression_csv': str(expression_csv),
            'metadata_csv': str(metadata_csv),
            'gene_sets_csv': str(gene_sets_csv),
            'evidence_csv': str(evidence_csv) if evidence_csv else None,
            'evidence_provider': evidence_provider,
            'evidence_cache_dir': (
                str(evidence_cache_dir) if evidence_cache_dir else None
            ),
            'genome': genome,
            'gencode_gtf': str(gencode_gtf) if gencode_gtf else None,
            'statistics_backend': statistics_backend,
        },
        'differential_expression': de_meta,
        'pathway_enrichment': pathway_meta,
        'report': report_meta,
    }


def write_omics_manifest(output_dir, manifest):
    path = Path(output_dir) / 'omics_manifest.json'
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return manifest


def specialist_workflow_result(workflow, manifest, manifest_path):
    return {
        'status': manifest['status'],
        'workflow': workflow.get('name', 'omics specialist workflow'),
        'manifest': manifest,
        'manifest_path': str(manifest_path),
    }
