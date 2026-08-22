"""从 ChEMBL 获取 EGFR 活性数据并生成项目外部 ML CSV。"""
import argparse
import json
from statistics import median
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

try:
    from .ml_predictor import scaffold_split_indices
except ImportError:
    from ml_predictor import scaffold_split_indices


API_BASE = 'https://www.ebi.ac.uk/chembl/api/data'
DEFAULT_TARGET = 'CHEMBL203'


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def aggregate_activity_records(
    activity_records,
    molecule_records,
    active_threshold=6.0,
    inactive_threshold=5.0,
    min_observations=2,
):
    structures = {}
    for record in molecule_records:
        molecule_id = record.get('molecule_chembl_id')
        molecule_structures = record.get('molecule_structures') or {}
        smiles = molecule_structures.get('canonical_smiles')
        if molecule_id and smiles:
            structures[molecule_id] = smiles

    observations = {}
    for record in activity_records:
        molecule_id = record.get('molecule_chembl_id')
        pchembl = _float(record.get('pchembl_value'))
        if not molecule_id or pchembl is None or molecule_id not in structures:
            continue
        if record.get('assay_type') not in (None, 'B'):
            continue
        observations.setdefault(molecule_id, []).append({
            'pchembl_value': pchembl,
            'assay_chembl_id': record.get('assay_chembl_id'),
            'document_chembl_id': record.get('document_chembl_id'),
        })

    rows = []
    for molecule_id, evidence in sorted(observations.items()):
        if len(evidence) < min_observations:
            continue
        values = [item['pchembl_value'] for item in evidence]
        pchembl_value = float(median(values))
        representative = min(
            evidence,
            key=lambda item: abs(item['pchembl_value'] - pchembl_value),
        )
        document_ids = sorted({
            item['document_chembl_id']
            for item in evidence
            if item.get('document_chembl_id')
        })
        row = {
            'name': molecule_id,
            'smiles': structures[molecule_id],
            'pchembl_value': pchembl_value,
            'pchembl_min': min(values),
            'pchembl_max': max(values),
            'evidence_count': len(evidence),
            'assay_chembl_id': representative.get('assay_chembl_id'),
            'document_chembl_id': representative.get('document_chembl_id'),
            'document_chembl_ids': '|'.join(document_ids),
            'document_count': len(document_ids),
        }
        if pchembl_value >= active_threshold:
            row['tag'] = 'active'
        elif pchembl_value <= inactive_threshold:
            row['tag'] = 'inactive'
        else:
            continue
        rows.append(row)
    return rows


def assign_scaffold_split(rows, test_fraction=0.2):
    if len(rows) < 4:
        raise ValueError('可切分的外部分子少于 4 个')
    smiles = [row['smiles'] for row in rows]
    labels = [int(row['tag'] == 'active') for row in rows]
    train_indices, test_indices = scaffold_split_indices(
        smiles, labels, test_fraction=test_fraction
    )
    if not test_indices:
        raise ValueError('无法构造 scaffold test 集')
    if set(labels[index] for index in train_indices) != {0, 1}:
        raise ValueError('scaffold train 集缺少 active 或 inactive')
    if set(labels[index] for index in test_indices) != {0, 1}:
        raise ValueError('scaffold test 集缺少 active 或 inactive')
    test_set = set(test_indices)
    output = []
    for index, row in enumerate(rows):
        item = dict(row)
        item['split'] = 'test' if index in test_set else 'train'
        output.append(item)
    return output


def _get_json(session, endpoint, params):
    response = session.get(f'{API_BASE}/{endpoint}.json', params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_records(target_id=DEFAULT_TARGET, limit=1000, session=None):
    session = session or requests.Session()
    activity_records = []
    offset = 0
    page_size = min(1000, max(1, limit))
    while len(activity_records) < limit:
        payload = _get_json(session, 'activity', {
            'target_chembl_id': target_id,
            'assay_type': 'B',
            'limit': page_size,
            'offset': offset,
        })
        batch = payload.get('activities', payload.get('data', []))
        if not batch:
            break
        activity_records.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)

    molecule_ids = sorted({
        record.get('molecule_chembl_id')
        for record in activity_records
        if record.get('molecule_chembl_id')
    })
    molecule_records = []
    for start in range(0, len(molecule_ids), 100):
        chunk = molecule_ids[start:start + 100]
        payload = _get_json(session, 'molecule', {
            'molecule_chembl_id__in': ','.join(chunk),
            'limit': len(chunk),
            'only': 'molecule_chembl_id,molecule_structures',
        })
        molecule_records.extend(payload.get('molecules', payload.get('data', [])))
    return activity_records[:limit], molecule_records


def build_external_dataset(
    target_id=DEFAULT_TARGET,
    limit=1000,
    active_threshold=6.0,
    inactive_threshold=5.0,
    min_observations=2,
    test_fraction=0.2,
    session=None,
):
    activity_records, molecule_records = fetch_records(
        target_id=target_id, limit=limit, session=session
    )
    rows = aggregate_activity_records(
        activity_records,
        molecule_records,
        active_threshold=active_threshold,
        inactive_threshold=inactive_threshold,
        min_observations=min_observations,
    )
    rows = assign_scaffold_split(rows, test_fraction=test_fraction)
    return rows, {
        'source': 'ChEMBL Web Services',
        'source_api': API_BASE,
        'target_chembl_id': target_id,
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'activity_records_fetched': len(activity_records),
        'molecules_with_structures': len(molecule_records),
        'dataset_rows': len(rows),
        'active_threshold_pchembl': active_threshold,
        'inactive_threshold_pchembl': inactive_threshold,
        'min_observations': min_observations,
        'aggregation': 'median pChEMBL per molecule',
        'test_fraction': test_fraction,
        'split_strategy': 'Bemis-Murcko scaffold split',
        'assay_filter': 'assay_type=B; pchembl_value present; repeated evidence required',
        'label_definition': 'active if pChEMBL >= active threshold; inactive if <= inactive threshold; interval excluded',
    }


def write_dataset(rows, provenance, output_csv, provenance_path):
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        'name', 'smiles', 'tag', 'split', 'pchembl_value', 'pchembl_min',
        'pchembl_max', 'evidence_count', 'document_count',
        'assay_chembl_id', 'document_chembl_id', 'document_chembl_ids',
    ]
    pd.DataFrame(rows, columns=columns).to_csv(output_csv, index=False)
    provenance['output_csv'] = str(output_csv)
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='获取 ChEMBL EGFR 外部 ML 数据集')
    parser.add_argument('--target', default=DEFAULT_TARGET)
    parser.add_argument('--limit', type=int, default=1000)
    parser.add_argument('--active-threshold', type=float, default=6.0)
    parser.add_argument('--inactive-threshold', type=float, default=5.0)
    parser.add_argument('--min-observations', type=int, default=2)
    parser.add_argument('--test-fraction', type=float, default=0.2)
    parser.add_argument('--out', default='data/external_egfr.csv')
    parser.add_argument('--provenance', default='data/external_egfr.provenance.json')
    args = parser.parse_args(argv)

    rows, provenance = build_external_dataset(
        target_id=args.target,
        limit=args.limit,
        active_threshold=args.active_threshold,
        inactive_threshold=args.inactive_threshold,
        min_observations=args.min_observations,
        test_fraction=args.test_fraction,
    )
    write_dataset(rows, provenance, args.out, args.provenance)
    print(f'[chembl] 输出 {len(rows)} 条: {args.out}')
    print(f'[chembl] provenance: {args.provenance}')


if __name__ == '__main__':
    main()