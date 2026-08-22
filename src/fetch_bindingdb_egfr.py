"""Fetch BindingDB EGFR measurements through the public REST API."""
import argparse
from datetime import datetime, timezone

import requests

try:
    from .fetch_chembl_egfr import assign_scaffold_split
    from .import_bindingdb import aggregate_bindingdb_records, normalize_bindingdb_api_records, write_dataset
    from .ml_predictor import scaffold_split_indices
except ImportError:
    from fetch_chembl_egfr import assign_scaffold_split
    from import_bindingdb import aggregate_bindingdb_records, normalize_bindingdb_api_records, write_dataset
    from ml_predictor import scaffold_split_indices


API_URL = 'https://bindingdb.org/rest/getLigandsByUniprots'
DEFAULT_UNIPROT = 'P00533'


def fetch_affinities(uniprot=DEFAULT_UNIPROT, cutoff=10000, session=None):
    session = session or requests.Session()
    response = session.get(API_URL, params={
        'uniprot': uniprot,
        'cutoff': cutoff,
        'response': 'application/json',
    }, timeout=120)
    response.raise_for_status()
    payload = response.json()
    root = payload.get('getLindsByUniprotsResponse') or payload.get('getLigandsByUniprotsResponse') or {}
    affinities = root.get('affinities', [])
    if not isinstance(affinities, list):
        raise ValueError('BindingDB REST 返回中没有有效 affinities 列表')
    return affinities


def _split_with_audit(rows, test_fraction):
    try:
        return assign_scaffold_split(rows, test_fraction=test_fraction), None
    except ValueError as exc:
        smiles = [row['smiles'] for row in rows]
        labels = [int(row['tag'] == 'active') for row in rows]
        train_indices, test_indices = scaffold_split_indices(
            smiles, labels, test_fraction=test_fraction
        )
        if not test_indices:
            raise
        test_set = set(test_indices)
        split_rows = []
        for index, row in enumerate(rows):
            item = dict(row)
            item['split'] = 'test' if index in test_set else 'train'
            split_rows.append(item)
        return split_rows, f'strict scaffold split unavailable: {exc}'

def build_external_dataset(uniprot=DEFAULT_UNIPROT, cutoff=10000, min_observations=2, active_threshold=7.0, inactive_threshold=6.0, test_fraction=0.2, session=None):
    affinities = fetch_affinities(uniprot, cutoff, session=session)
    records = normalize_bindingdb_api_records(affinities)
    rows = aggregate_bindingdb_records(
        records,
        active_threshold=active_threshold,
        inactive_threshold=inactive_threshold,
        min_observations=min_observations,
    )
    rows, split_warning = _split_with_audit(rows, test_fraction)
    provenance = {
        'source': 'BindingDB REST Web Services',
        'source_api': API_URL,
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'target_uniprot_id': uniprot,
        'affinity_cutoff_nm': cutoff,
        'raw_measurements': len(affinities),
        'normalized_measurements': len(records),
        'dataset_rows': len(rows),
        'min_observations': min_observations,
        'active_threshold_pactivity': active_threshold,
        'inactive_threshold_pactivity': inactive_threshold,
        'aggregation': 'median pActivity per canonical SMILES',
        'activity_definition': 'pActivity = 9 - log10(affinity in nM); active >= active threshold; inactive <= inactive threshold; interval excluded',
        'split_strategy': 'Bemis-Murcko scaffold split with audit fallback',
        'split_warning': split_warning,
        'independence_note': 'BindingDB may include records curated from ChEMBL; compare structure and document overlap before treating as independent',
    }
    return rows, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fetch BindingDB EGFR data through the public REST API')
    parser.add_argument('--uniprot', default=DEFAULT_UNIPROT)
    parser.add_argument('--cutoff', type=int, default=10000)
    parser.add_argument('--min-observations', type=int, default=2)
    parser.add_argument('--active-threshold', type=float, default=7.0)
    parser.add_argument('--inactive-threshold', type=float, default=6.0)
    parser.add_argument('--test-fraction', type=float, default=0.2)
    parser.add_argument('--out', default='data/external_bindingdb_egfr.csv')
    parser.add_argument('--provenance', default='data/external_bindingdb_egfr.provenance.json')
    args = parser.parse_args(argv)
    rows, provenance = build_external_dataset(
        args.uniprot, args.cutoff, args.min_observations, args.active_threshold, args.inactive_threshold, args.test_fraction,
    )
    write_dataset(rows, provenance, args.out, args.provenance)
    print(f'[bindingdb] output {len(rows)} rows: {args.out}')
    print(f'[bindingdb] provenance: {args.provenance}')


if __name__ == '__main__':
    main()