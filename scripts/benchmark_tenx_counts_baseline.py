import gzip
import json
from pathlib import Path

BASELINE_SOURCE_COMMIT = '85dbd5cf4faeb536a7bdfcd7be00e47cad4d94c8'


def _open_10x_text(path):
    return gzip.open(path, 'rt', encoding='utf-8', errors='replace') if str(path).lower().endswith('.gz') else open(path, 'r', encoding='utf-8', errors='replace')


def _read_10x_table(path):
    with _open_10x_text(path) as handle:
        return [line.rstrip('\r\n').split('\t') for line in handle if line.rstrip('\r\n')]


def run_single_cell_10x_qc(matrix_mtx, barcodes_tsv, features_tsv, output_dir,
                           min_genes=0, max_genes=None, min_counts=0,
                           max_mito_percent=100, mitochondrial_prefix='MT-'):
    import numpy as np
    import pandas as pd
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
    gene_expression_indices = [index for index, feature_type in enumerate(feature_types) if feature_type.lower() == 'gene expression']
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
    mito_indices = [index for index, name in enumerate(feature_names) if str(name).upper().startswith(prefix)]
    cell_counts = np.asarray(matrix.sum(axis=0)).ravel()
    cell_genes = np.asarray(matrix.getnnz(axis=0)).ravel()
    mito_counts = np.asarray(matrix[mito_indices, :].sum(axis=0)).ravel() if mito_indices else np.zeros(matrix.shape[1])
    mito_percent = np.divide(mito_counts * 100, cell_counts, out=np.zeros_like(mito_counts, dtype=float), where=cell_counts != 0)
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
        'inputs': {'matrix_mtx': str(matrix_mtx), 'barcodes_tsv': str(barcodes_tsv), 'features_tsv': str(features_tsv)},
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
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def validate_counts(data):
    import numpy as np

    if any(value < 0 or not np.isfinite(value) for value in data):
        raise ValueError('10x counts must be finite and non-negative')
