from pathlib import Path

BASELINE_SOURCE_COMMIT = 'c1a3e5d750335e21485fe275874e5ab314af1e23'


def require_columns(frame, columns, label):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f'{label} missing columns: {sorted(missing)}')


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


def _bh_adjust(values):
    import numpy as np

    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return values
    order = np.argsort(values)
    ranked = values[order]
    adjusted = np.minimum.accumulate((ranked * len(values) / np.arange(1, len(values) + 1))[::-1])[::-1]
    result = np.empty_like(adjusted)
    result[order] = np.clip(adjusted, 0.0, 1.0)
    return result


def _load_gene_sets(gene_sets_csv):
    import pandas as pd

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
    import pandas as pd
    from scipy.stats import hypergeom

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
        rows.append({
            'pathway_id': pathway_id,
            'pathway_name': pathway['pathway_name'],
            'pathway_size': len(pathway_genes),
            'overlap_count': len(overlap),
            'selected_count': len(selected),
            'overlap_genes': '|'.join(sorted(overlap)),
            'p_value': 1.0,
        })
    if selected:
        for offset in range(0, len(rows), 1024):
            batch = rows[offset:offset + 1024]
            probabilities = hypergeom.sf(
                [row['overlap_count'] - 1 for row in batch],
                len(background),
                [row['pathway_size'] for row in batch],
                len(selected),
            )
            for row, probability in zip(batch, probabilities):
                row['p_value'] = float(probability)
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


_require_columns = require_columns
