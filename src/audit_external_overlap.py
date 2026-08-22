"""Audit and optionally remove structure overlap between external datasets."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rdkit import Chem


def canonicalize_smiles(value):
    mol = Chem.MolFromSmiles(str(value).strip())
    if mol is None:
        return None
    return Chem.MolToSmiles(mol, canonical=True)


def _load_frame(path):
    frame = pd.read_csv(path)
    missing = {'smiles'} - set(frame.columns)
    if missing:
        raise ValueError(f"dataset missing fields: {', '.join(sorted(missing))}")
    frame = frame.copy()
    frame['_canonical_smiles'] = frame['smiles'].map(canonicalize_smiles)
    invalid = int(frame['_canonical_smiles'].isna().sum())
    if invalid:
        raise ValueError(f"dataset contains {invalid} invalid SMILES: {path}")
    return frame


def audit_overlap(reference_csv, candidate_csv):
    reference = _load_frame(reference_csv)
    candidate = _load_frame(candidate_csv)
    reference_structures = set(reference['_canonical_smiles'])
    candidate_structures = set(candidate['_canonical_smiles'])
    overlap = reference_structures & candidate_structures
    candidate_overlap = candidate[candidate['_canonical_smiles'].isin(overlap)]
    result = {
        'reference_csv': str(reference_csv),
        'candidate_csv': str(candidate_csv),
        'reference_rows': int(len(reference)),
        'candidate_rows': int(len(candidate)),
        'reference_unique_structures': len(reference_structures),
        'candidate_unique_structures': len(candidate_structures),
        'overlap_unique_structures': len(overlap),
        'candidate_overlap_rows': int(len(candidate_overlap)),
        'candidate_overlap_fraction': float(len(candidate_overlap) / len(candidate)) if len(candidate) else None,
        'reference_overlap_fraction': float(len(overlap) / len(reference_structures)) if reference_structures else None,
    }
    if 'tag' in candidate.columns:
        result['candidate_overlap_tag_counts'] = {
            str(key): int(value) for key, value in candidate_overlap['tag'].value_counts().to_dict().items()
        }
    return result


def remove_structure_overlap(reference_csv, candidate_csv):
    reference = _load_frame(reference_csv)
    candidate = _load_frame(candidate_csv)
    reference_structures = set(reference['_canonical_smiles'])
    filtered = candidate[~candidate['_canonical_smiles'].isin(reference_structures)].copy()
    filtered = filtered.drop(columns=['_canonical_smiles'])
    return filtered, audit_overlap(reference_csv, candidate_csv)


def write_decontaminated_dataset(reference_csv, candidate_csv, output_csv, provenance_path):
    filtered, audit = remove_structure_overlap(reference_csv, candidate_csv)
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_csv(output_csv, index=False)
    provenance = {
        'source': 'structure-overlap-filtered external dataset',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'reference_dataset': str(reference_csv),
        'candidate_dataset': str(candidate_csv),
        'filter': 'remove rows whose canonical RDKit SMILES occurs in reference dataset',
        'input_rows': int(audit['candidate_rows']),
        'removed_rows': int(audit['candidate_overlap_rows']),
        'output_rows': int(len(filtered)),
        'audit': audit,
        'independence_note': 'Structure filtering reduces direct chemical overlap but cannot prove source-record independence.',
        'output_csv': str(output_csv),
    }
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return filtered, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit and filter cross-dataset structure overlap')
    parser.add_argument('--reference', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--audit-out')
    parser.add_argument('--filtered-out')
    parser.add_argument('--provenance')
    args = parser.parse_args(argv)
    audit = audit_overlap(args.reference, args.candidate)
    if args.audit_out:
        Path(args.audit_out).write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    if args.filtered_out:
        provenance_path = args.provenance or f'{args.filtered_out}.provenance.json'
        _, provenance = write_decontaminated_dataset(
            args.reference, args.candidate, args.filtered_out, provenance_path
        )
        print(f"[overlap] filtered rows: {provenance['output_rows']}; removed: {provenance['removed_rows']}")


if __name__ == '__main__':
    main()
