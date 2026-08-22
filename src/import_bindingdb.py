"""Import BindingDB target TSV files into the external ML dataset schema."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import pandas as pd
from rdkit import Chem

try:
    from .fetch_chembl_egfr import assign_scaffold_split
except ImportError:
    from fetch_chembl_egfr import assign_scaffold_split


AFFINITY_COLUMNS = (
    ('Ki (nM)', 'Ki'),
    ('IC50 (nM)', 'IC50'),
    ('Kd (nM)', 'Kd'),
    ('EC50 (nM)', 'EC50'),
)
SMILES_COLUMNS = ('BindingDB Ligand SMILES', 'Ligand SMILES', 'SMILES')
NAME_COLUMNS = ('BindingDB Ligand Name', 'BindingDB Reactant_set_id', 'Ligand Name')
DOCUMENT_COLUMNS = ('Article DOI', 'BindingDB Article DOI', 'Article PMID', 'BindingDB Article PMID')
TARGET_COLUMNS = ('Target ChEMBL ID', 'Target UniProt ID', 'Target Name')


def _column(df, names):
    return next((name for name in names if name in df.columns), None)


def _numeric(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    number = float(text)
    return number if number > 0 else None


def _document_ids(row):
    values = []
    for column in DOCUMENT_COLUMNS:
        if column in row.index and not pd.isna(row[column]):
            value = str(row[column]).strip()
            if value:
                values.append(value)
    return values


def filter_target_rows(df, target_chembl=None, target_uniprot=None, target_name=None):
    available = [column for column in TARGET_COLUMNS if column in df.columns]
    if not available:
        raise ValueError('BindingDB TSV 缺少可用于靶点过滤的字段')
    needles = [str(value).strip().lower() for value in (target_chembl, target_uniprot, target_name) if value]
    if not needles:
        return df.copy()
    mask = pd.Series(False, index=df.index)
    for column in available:
        values = df[column].fillna('').astype(str).str.lower()
        for needle in needles:
            mask |= values.eq(needle) | values.str.contains(needle, regex=False)
    filtered = df.loc[mask].copy()
    if filtered.empty:
        raise ValueError('BindingDB TSV 中没有匹配目标靶点的记录')
    return filtered


def normalize_bindingdb_records(df):
    smiles_column = _column(df, SMILES_COLUMNS)
    name_column = _column(df, NAME_COLUMNS)
    if not smiles_column:
        raise ValueError('BindingDB TSV 缺少配体 SMILES 字段')
    records = []
    for _, row in df.iterrows():
        smiles = str(row[smiles_column]).strip()
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue
        canonical = Chem.MolToSmiles(mol, canonical=True)
        name = str(row[name_column]).strip() if name_column and not pd.isna(row[name_column]) else canonical
        documents = _document_ids(row)
        for column, affinity_type in AFFINITY_COLUMNS:
            if column not in df.columns:
                continue
            value = _numeric(row[column])
            if value is None:
                continue
            records.append({
                'name': name,
                'smiles': canonical,
                'affinity_nm': value,
                'pactivity': 9.0 - __import__('math').log10(value),
                'affinity_type': affinity_type,
                'document_ids': '|'.join(sorted(set(documents))),
            })
    return records


def normalize_bindingdb_api_records(affinities):
    records = []
    for item in affinities:
        smiles = str(item.get('smile') or item.get('smiles') or '').strip()
        mol = Chem.MolFromSmiles(smiles)
        value = _numeric(item.get('affinity'))
        if mol is None or value is None:
            continue
        documents = [
            str(item[key]).strip()
            for key in ('doi', 'pmid')
            if item.get(key)
        ]
        records.append({
            'name': str(item.get('monomerid') or Chem.MolToSmiles(mol, canonical=True)),
            'smiles': Chem.MolToSmiles(mol, canonical=True),
            'affinity_nm': value,
            'pactivity': 9.0 - __import__('math').log10(value),
            'affinity_type': str(item.get('affinity_type') or 'unknown'),
            'document_ids': '|'.join(sorted(set(documents))),
        })
    return records

def aggregate_bindingdb_records(records, active_threshold=6.0, inactive_threshold=5.0, min_observations=2):
    grouped = {}
    for record in records:
        grouped.setdefault(record['smiles'], []).append(record)
    rows = []
    for smiles, evidence in sorted(grouped.items()):
        if len(evidence) < min_observations:
            continue
        values = [item['pactivity'] for item in evidence]
        activity = float(median(values))
        if activity >= active_threshold:
            tag = 'active'
        elif activity <= inactive_threshold:
            tag = 'inactive'
        else:
            continue
        documents = sorted({doc for item in evidence for doc in item['document_ids'].split('|') if doc})
        representative = min(evidence, key=lambda item: abs(item['pactivity'] - activity))
        rows.append({
            'name': representative['name'],
            'smiles': smiles,
            'tag': tag,
            'pactivity': activity,
            'pactivity_min': min(values),
            'pactivity_max': max(values),
            'evidence_count': len(evidence),
            'affinity_types': '|'.join(sorted({item['affinity_type'] for item in evidence})),
            'document_ids': '|'.join(documents),
            'document_count': len(documents),
        })
    return rows


def build_external_dataset(input_tsv, target_chembl=None, target_uniprot=None, target_name=None, min_observations=2, active_threshold=7.0, inactive_threshold=6.0, test_fraction=0.2):
    df = pd.read_csv(input_tsv, sep='\t', dtype=str, low_memory=False)
    filtered = filter_target_rows(df, target_chembl, target_uniprot, target_name)
    records = normalize_bindingdb_records(filtered)
    rows = aggregate_bindingdb_records(
        records,
        active_threshold=active_threshold,
        inactive_threshold=inactive_threshold,
        min_observations=min_observations,
    )
    rows = assign_scaffold_split(rows, test_fraction=test_fraction)
    provenance = {
        'source': 'BindingDB TSV',
        'input_tsv': str(input_tsv),
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'target_chembl_id': target_chembl,
        'target_uniprot_id': target_uniprot,
        'target_name': target_name,
        'raw_rows_after_target_filter': int(len(filtered)),
        'normalized_measurements': len(records),
        'dataset_rows': len(rows),
        'min_observations': min_observations,
        'active_threshold_pactivity': active_threshold,
        'inactive_threshold_pactivity': inactive_threshold,
        'aggregation': 'median pActivity per canonical SMILES',
        'activity_definition': 'pActivity = 9 - log10(affinity in nM); active >= active threshold; inactive <= inactive threshold; interval excluded',
        'split_strategy': 'Bemis-Murcko scaffold split',
    }
    return rows, provenance


def write_dataset(rows, provenance, output_csv, provenance_path):
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ['name', 'smiles', 'tag', 'split', 'pactivity', 'pactivity_min', 'pactivity_max', 'evidence_count', 'affinity_types', 'document_ids', 'document_count']
    pd.DataFrame(rows, columns=columns).to_csv(output_csv, index=False)
    provenance['output_csv'] = str(output_csv)
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import a BindingDB target TSV for external ML validation')
    parser.add_argument('--input', required=True)
    parser.add_argument('--target-chembl')
    parser.add_argument('--target-uniprot', default='P00533')
    parser.add_argument('--target-name', default='Epidermal growth factor receptor')
    parser.add_argument('--min-observations', type=int, default=2)
    parser.add_argument('--active-threshold', type=float, default=7.0)
    parser.add_argument('--inactive-threshold', type=float, default=6.0)
    parser.add_argument('--test-fraction', type=float, default=0.2)
    parser.add_argument('--out', default='data/external_bindingdb_egfr.csv')
    parser.add_argument('--provenance', default='data/external_bindingdb_egfr.provenance.json')
    args = parser.parse_args(argv)
    rows, provenance = build_external_dataset(
        args.input, args.target_chembl, args.target_uniprot, args.target_name,
        args.min_observations, args.active_threshold, args.inactive_threshold, args.test_fraction,
    )
    write_dataset(rows, provenance, args.out, args.provenance)
    print(f'[bindingdb] output {len(rows)} rows: {args.out}')


if __name__ == '__main__':
    main()