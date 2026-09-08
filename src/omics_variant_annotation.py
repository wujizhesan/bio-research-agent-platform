"""Local and VCF-native variant annotation."""

import gzip
from pathlib import Path

import pandas as pd

try:
    from .omics_results import variant_annotation_result
    from .omics_validation import require_columns
except ImportError:
    from omics_results import variant_annotation_result
    from omics_validation import require_columns


VARIANT_ANNOTATION_BACKENDS = ('auto', 'local', 'vcf_ann', 'gencode_gtf')
RESULT_COLUMNS = [
    'variant_id',
    'chrom',
    'pos',
    'ref',
    'alt',
    'qual',
    'filter',
    'gene_id',
    'gene_name',
    'transcript_id',
    'gene_type',
    'effect',
    'impact',
    'annotation_source',
    'annotation_status',
]


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
    require_columns(
        annotation,
        {'chrom', 'start', 'end', 'gene_id'},
        'variant annotation table',
    )
    if annotation.empty:
        raise ValueError('variant annotation table is empty')
    annotation = annotation.copy()
    annotation['chrom'] = annotation['chrom'].map(_normalize_chrom)
    annotation['start'] = pd.to_numeric(
        annotation['start'],
        errors='raise',
    ).astype(int)
    annotation['end'] = pd.to_numeric(
        annotation['end'],
        errors='raise',
    ).astype(int)
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
    with opener(
        annotation_gtf,
        'rt',
        encoding='utf-8',
        errors='replace',
    ) as handle:
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
                raise ValueError(
                    f'GTF row {line_number} has invalid coordinates'
                ) from exc
            attributes = _parse_gtf_attributes(fields[8])
            gene_id = (
                attributes.get('gene_id')
                or attributes.get('gene')
                or attributes.get('ID')
            )
            if not gene_id:
                continue
            row = {
                'chrom': fields[0],
                'start': start,
                'end': end,
                'gene_id': gene_id,
                'gene_name': attributes.get('gene_name') or attributes.get('Name', ''),
                'gene_type': (
                    attributes.get('gene_type')
                    or attributes.get('gene_biotype', '')
                ),
                'transcript_id': (
                    attributes.get('transcript_id')
                    or attributes.get('transcript', '')
                ),
            }
            (gene_rows if feature == 'gene' else transcript_rows).append(row)
    rows = gene_rows or transcript_rows
    if not rows:
        raise ValueError(
            'GTF annotation has no gene or transcript records with gene identifiers'
        )
    annotation = pd.DataFrame(rows)
    annotation['chrom'] = annotation['chrom'].map(_normalize_chrom)
    annotation['start'] = pd.to_numeric(
        annotation['start'],
        errors='raise',
    ).astype(int)
    annotation['end'] = pd.to_numeric(
        annotation['end'],
        errors='raise',
    ).astype(int)
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


def _base_result_row(record, variant_id, chrom, position, ref, alt):
    return {
        'variant_id': variant_id,
        'chrom': chrom,
        'pos': position,
        'ref': ref,
        'alt': alt,
        'qual': record.get('QUAL', '.'),
        'filter': record.get('FILTER', '.'),
    }


def _annotation_rows(record, annotation, requested, chrom, position, ref, alt):
    base_id = record.get('ID') or '.'
    variant_id = base_id if base_id != '.' else f'{chrom}:{position}:{ref}>{alt}'
    base = _base_result_row(record, variant_id, chrom, position, ref, alt)
    info = _parse_info(record.get('INFO', '.'))
    ann = _parse_ann(info, alt) if requested in {'auto', 'vcf_ann'} else None
    if ann:
        return [{
            **base,
            'gene_id': ann['gene_id'],
            'gene_name': ann['gene_name'],
            'transcript_id': '',
            'gene_type': '',
            'effect': ann['effect'],
            'impact': ann['impact'],
            'annotation_source': 'vcf_ann',
            'annotation_status': 'annotated',
        }], {'vcf_ann'}
    if requested == 'vcf_ann':
        return [{
            **base,
            'gene_id': '',
            'gene_name': '',
            'transcript_id': '',
            'gene_type': '',
            'effect': '',
            'impact': '',
            'annotation_source': 'vcf_ann',
            'annotation_status': 'unmatched',
        }], set()
    matches = _local_variant_matches(annotation, chrom, position)
    if matches:
        source = 'gencode_gtf' if requested == 'gencode_gtf' else 'local_interval'
        rows = [{
            **base,
            'gene_id': str(match['gene_id']),
            'gene_name': str(match.get('gene_name', '')),
            'transcript_id': str(match.get('transcript_id', '')),
            'gene_type': str(match.get('gene_type', '')),
            'effect': str(match.get('effect', '')),
            'impact': str(match.get('impact', '')),
            'annotation_source': source,
            'annotation_status': 'annotated',
        } for match in matches]
        return rows, {source}
    return [{
        **base,
        'gene_id': '',
        'gene_name': '',
        'transcript_id': '',
        'gene_type': '',
        'effect': '',
        'impact': '',
        'annotation_source': 'none',
        'annotation_status': 'unmatched',
    }], set()


def _load_annotation(annotation_csv, annotation_gtf, requested):
    if annotation_csv and annotation_gtf:
        raise ValueError('provide only one of annotation_csv and annotation_gtf')
    if annotation_gtf:
        if requested == 'auto':
            requested = 'gencode_gtf'
        return requested, _load_gencode_annotations(annotation_gtf)
    if requested == 'gencode_gtf':
        raise ValueError('gencode_gtf annotation requires annotation_gtf')
    annotation = _load_variant_annotations(annotation_csv)
    if requested == 'local' and annotation is None:
        raise ValueError('local variant annotation requires annotation_csv')
    return requested, annotation


def execute_variant_annotation(
    vcf_path,
    output_csv,
    annotation_csv=None,
    annotation_backend='auto',
    annotation_gtf=None,
    *,
    toolchain,
):
    requested = str(annotation_backend or 'auto').lower()
    if requested not in VARIANT_ANNOTATION_BACKENDS:
        raise ValueError(f'unknown variant annotation backend: {requested}')
    requested, annotation = _load_annotation(
        annotation_csv,
        annotation_gtf,
        requested,
    )
    rows = []
    n_variants = 0
    n_alleles = 0
    sources = set()
    with _open_vcf(vcf_path) as handle:
        header = None
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip('\n\r')
            if not line or line.startswith('##'):
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
            alternatives = [
                item
                for item in record.get('ALT', '').split(',')
                if item and item != '.'
            ]
            if not ref or not alternatives:
                raise ValueError(f'VCF row {line_number} has invalid REF or ALT')
            n_variants += 1
            n_alleles += len(alternatives)
            for alt in alternatives:
                annotation_rows, row_sources = _annotation_rows(
                    record,
                    annotation,
                    requested,
                    chrom,
                    position,
                    ref,
                    alt,
                )
                rows.extend(annotation_rows)
                sources.update(row_sources)
    if header is None:
        raise ValueError('VCF header is missing')
    result = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    gene_ids = sorted({
        str(value)
        for value in result['gene_id']
        if str(value).strip()
    })
    effective_backend = (
        'mixed'
        if len(sources) > 1
        else (next(iter(sources)) if sources else requested)
    )
    return variant_annotation_result(
        output_csv,
        requested,
        effective_backend,
        result,
        n_variants,
        n_alleles,
        gene_ids,
        toolchain,
    )
