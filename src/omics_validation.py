"""Input validation and normalization for omics tools."""

from pathlib import Path

import pandas as pd


GENOMICS_QC_TYPES = ('auto', 'fastq', 'bam', 'vcf')


def require_columns(frame, columns, label):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f'{label} missing columns: {sorted(missing)}')


def load_expression_matrix(expression_csv, metadata_csv):
    expression = pd.read_csv(expression_csv)
    metadata = pd.read_csv(metadata_csv)
    if expression.empty:
        raise ValueError('expression matrix is empty')
    require_columns(expression, {'gene_id'}, 'expression matrix')
    require_columns(metadata, {'sample_id', 'condition'}, 'metadata')
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
    expression[sample_columns] = expression[sample_columns].apply(
        pd.to_numeric, errors='raise'
    )
    metadata = metadata.set_index('sample_id').loc[sample_columns].reset_index()
    conditions = metadata['condition'].astype(str)
    if conditions.nunique() != 2:
        raise ValueError('RNA-seq adapter currently requires exactly two conditions')
    if conditions.value_counts().min() < 2:
        raise ValueError('each condition requires at least two replicates')
    return expression, metadata


def condition_pair(metadata, condition_a=None, condition_b=None):
    conditions = sorted(metadata['condition'].astype(str).unique())
    condition_a = str(condition_a or conditions[0])
    condition_b = str(condition_b or conditions[1])
    if condition_a == condition_b or {condition_a, condition_b} != set(conditions):
        raise ValueError(f'conditions must be the two observed values: {conditions}')
    samples_a = metadata.loc[
        metadata['condition'].astype(str) == condition_a, 'sample_id'
    ].tolist()
    samples_b = metadata.loc[
        metadata['condition'].astype(str) == condition_b, 'sample_id'
    ].tolist()
    return condition_a, condition_b, samples_a, samples_b


def normalize_alignment_paths(alignment_paths):
    values = (
        alignment_paths
        if isinstance(alignment_paths, (list, tuple))
        else [alignment_paths]
    )
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('alignment_paths must contain at least one BAM/CRAM path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'alignment files do not exist: {missing}')
    invalid = [
        str(path) for path in paths
        if path.suffix.lower() not in {'.bam', '.cram'}
    ]
    if invalid:
        raise ValueError(f'featureCounts requires BAM/CRAM inputs: {invalid}')
    return paths


def normalize_fastq_paths(fastq_paths):
    values = (
        fastq_paths if isinstance(fastq_paths, (list, tuple)) else [fastq_paths]
    )
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('fastq_paths must contain at least one FASTQ path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'FASTQ inputs do not exist: {missing}')
    suffixes = ('.fastq', '.fq', '.fastq.gz', '.fq.gz')
    invalid = [
        str(path) for path in paths if not path.name.lower().endswith(suffixes)
    ]
    if invalid:
        raise ValueError(f'RNA-seq alignment requires FASTQ inputs: {invalid}')
    return paths


def normalize_qc_paths(input_path):
    values = input_path if isinstance(input_path, (list, tuple)) else [input_path]
    if not values or any(value is None or not str(value).strip() for value in values):
        raise ValueError('input_path must contain at least one file path')
    paths = [Path(str(value)) for value in values]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f'input files do not exist: {missing}')
    return paths


def infer_qc_type(path):
    name = path.name.lower()
    if name.endswith(('.fastq', '.fq', '.fastq.gz', '.fq.gz')):
        return 'fastq'
    if name.endswith(('.bam', '.cram')):
        return 'bam'
    if name.endswith(('.vcf', '.vcf.gz', '.bcf')):
        return 'vcf'
    raise ValueError(f'cannot infer genomics QC input type from: {path}')


def resolve_qc_type(paths, requested):
    requested = str(requested or 'auto').lower()
    if requested not in GENOMICS_QC_TYPES:
        raise ValueError(f'unknown genomics QC input type: {requested}')
    if requested != 'auto':
        return requested
    detected = {infer_qc_type(path) for path in paths}
    if len(detected) != 1:
        raise ValueError(f'input files must share one QC type: {sorted(detected)}')
    return detected.pop()
