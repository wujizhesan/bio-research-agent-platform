"""RNA-seq domain adapter with structured tools and reproducible outputs."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom, ttest_ind

PLUGIN_NAME = 'RNA-seq and omics domain'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = ('omics.differential_expression', 'omics.pathway', 'omics.report')



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


def run_differential_expression(expression_csv, metadata_csv, output_csv,
                                condition_a=None, condition_b=None):
    expression, metadata = load_expression_matrix(expression_csv, metadata_csv)
    conditions = sorted(metadata['condition'].astype(str).unique())
    condition_a = str(condition_a or conditions[0])
    condition_b = str(condition_b or conditions[1])
    if condition_a == condition_b or {condition_a, condition_b} != set(conditions):
        raise ValueError(f'conditions must be the two observed values: {conditions}')
    samples_a = metadata.loc[metadata['condition'].astype(str) == condition_a, 'sample_id'].tolist()
    samples_b = metadata.loc[metadata['condition'].astype(str) == condition_b, 'sample_id'].tolist()
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


def search_gene_evidence(gene_ids, evidence_csv=None, provider='local',
                         cache_dir=None, timeout=15):
    try:
        from .evidence_providers import get_evidence_provider
    except ImportError:
        from evidence_providers import get_evidence_provider
    return get_evidence_provider(
        provider=provider,
        evidence_csv=evidence_csv,
        cache_dir=cache_dir,
        timeout=timeout,
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
                       evidence_timeout=15):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    de_csv = output_dir / 'differential_expression.csv'
    pathway_csv = output_dir / 'pathway_enrichment.csv'
    report_md = output_dir / 'omics_report.md'
    de_meta = run_differential_expression(
        expression_csv, metadata_csv, de_csv, condition_a, condition_b
    )
    pathway_meta = run_pathway_enrichment(de_csv, gene_sets_csv, pathway_csv)
    evidence = None
    if evidence_csv or evidence_provider in {'uniprot', 'pubmed'}:
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
    'run_differential_expression': {
        'description': 'Run a reproducible two-condition RNA-seq differential expression analysis.',
        'parameters': _parameters({
            'expression_csv': {'type': 'string'},
            'metadata_csv': {'type': 'string'},
            'output_csv': {'type': 'string'},
            'condition_a': {'type': 'string'},
            'condition_b': {'type': 'string'},
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
    'search_gene_evidence': {
        'description': 'Retrieve cited gene evidence from a structured evidence index.',
        'parameters': _parameters({
            'gene_ids': {'type': 'array', 'items': {'type': 'string'}},
            'evidence_csv': {'type': 'string'},
            'provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed']},
            'cache_dir': {'type': 'string'},
            'timeout': {'type': 'number'},
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
    parser.add_argument('--evidence-provider', choices=('local', 'uniprot', 'pubmed'), default='local')
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
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
