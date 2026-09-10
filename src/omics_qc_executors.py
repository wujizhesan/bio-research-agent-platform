"""Single-cell and metagenomics QC executors."""

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd


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
    mito_columns = [column for column in gene_columns if str(column).upper().startswith(prefix)]
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
    frame.loc[keep].copy().to_csv(filtered_path, index=False)
    payload = {
        'status': 'completed',
        'input': str(matrix_csv),
        'output_dir': str(output_dir),
        'outputs': {'cell_metrics': str(metrics_path), 'filtered_matrix': str(filtered_path)},
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
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
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
        'outputs': {'relative_abundance': str(relative_path), 'sample_metrics': str(sample_metrics_path)},
        'metrics': {
            'n_taxa_input': int(len(frame)),
            'n_taxa_retained': int(keep.sum()),
            'n_taxa_filtered': int((~keep).sum()),
            'n_samples': len(sample_columns),
        },
        'thresholds': {'min_total_counts': min_total_counts, 'min_prevalence': min_prevalence},
        'provenance': {
            'normalization': 'relative_abundance_after_filtering',
            'alpha_diversity': ['observed_taxa', 'shannon_index'],
        },
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload
