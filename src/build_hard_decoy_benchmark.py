"""Build a property-matched hard-decoy benchmark from an external dataset."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem, DataStructs
from rdkit.Chem import Crippen, Descriptors, Lipinski, rdFingerprintGenerator, rdMolDescriptors

try:
    from .ml_predictor import load_external_dataset, scaffold_key
except ImportError:
    from ml_predictor import load_external_dataset, scaffold_key


DESCRIPTOR_NAMES = (
    'mw', 'logp', 'tpsa', 'hbd', 'hba', 'rotatable_bonds', 'rings', 'heavy_atoms'
)
MORGAN_GENERATOR = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def _document_values(frame):
    for column in ('document_ids', 'document_chembl_ids', 'document_chembl_id'):
        if column in frame.columns:
            values = []
            for value in frame[column]:
                if pd.isna(value):
                    values.append(set())
                else:
                    values.append({item.strip() for item in str(value).split('|') if item.strip()})
            return values, column
    return [set() for _ in range(len(frame))], None


def _molecule_features(smiles):
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f'cannot parse SMILES: {smiles}')
    descriptors = np.array([
        Descriptors.MolWt(molecule),
        Crippen.MolLogP(molecule),
        rdMolDescriptors.CalcTPSA(molecule),
        Lipinski.NumHDonors(molecule),
        Lipinski.NumHAcceptors(molecule),
        Lipinski.NumRotatableBonds(molecule),
        rdMolDescriptors.CalcNumRings(molecule),
        rdMolDescriptors.CalcNumHeavyAtoms(molecule),
    ], dtype=float)
    fingerprint = MORGAN_GENERATOR.GetFingerprint(molecule)
    return descriptors, fingerprint, scaffold_key(smiles)


def _match_decoys(active_rows, inactive_rows, max_tanimoto=0.5, decoys_per_active=1, max_pairs=None):
    all_rows = active_rows + inactive_rows
    features = {}
    for row in all_rows:
        features[row['_row_index']] = _molecule_features(row['smiles'])
    scale = np.std(np.vstack([features[row['_row_index']][0] for row in all_rows]), axis=0)
    scale[scale == 0] = 1.0
    available = {row['_row_index'] for row in inactive_rows}
    matches = []
    for active in sorted(active_rows, key=lambda row: str(row['name'])):
        active_descriptor, active_fp, active_scaffold = features[active['_row_index']]
        for _ in range(decoys_per_active):
            if max_pairs is not None and len(matches) >= max_pairs:
                return matches
            candidates = []
            for inactive in inactive_rows:
                if inactive['_row_index'] not in available:
                    continue
                inactive_descriptor, inactive_fp, inactive_scaffold = features[inactive['_row_index']]
                if inactive_scaffold == active_scaffold:
                    continue
                tanimoto = float(DataStructs.TanimotoSimilarity(active_fp, inactive_fp))
                if tanimoto > max_tanimoto:
                    continue
                distance = float(np.linalg.norm((active_descriptor - inactive_descriptor) / scale))
                candidates.append((distance, tanimoto, str(inactive['name']), inactive))
            if not candidates:
                break
            distance, tanimoto, _, chosen = min(candidates, key=lambda item: item[:3])
            available.remove(chosen['_row_index'])
            matches.append({
                'active': active,
                'decoy': chosen,
                'descriptor_distance': distance,
                'tanimoto': tanimoto,
            })
    return matches


def _overlap_summary(frame):
    train = frame[frame['split'] == 'train']
    test = frame[frame['split'] == 'test']
    train_scaffolds = {scaffold_key(value) for value in train['smiles']}
    test_scaffolds = {scaffold_key(value) for value in test['smiles']}
    documents, document_column = _document_values(frame)
    train_documents = set().union(*[documents[index] for index in train.index]) if len(train) else set()
    test_documents = set().union(*[documents[index] for index in test.index]) if len(test) else set()
    return {
        'document_column': document_column,
        'document_overlap_count': len(train_documents & test_documents),
        'scaffold_overlap_count': len(train_scaffolds & test_scaffolds),
    }


def build_hard_decoy_benchmark(input_csv, output_csv, provenance_path,
                               max_tanimoto=0.5, decoys_per_active=1, max_pairs=50):
    frame = load_external_dataset(input_csv).reset_index(drop=True)
    train = frame[frame['split'] == 'train'].copy()
    test = frame[frame['split'] == 'test'].copy()
    active = test[test['tag'] == 'active'].copy()
    inactive = test[test['tag'] == 'inactive'].copy()
    if active.empty or inactive.empty:
        raise ValueError('test split must contain both active and inactive molecules')

    active_rows = active.to_dict('records')
    inactive_rows = inactive.to_dict('records')
    for index, row in enumerate(active_rows):
        row['_row_index'] = f'active:{index}'
    for index, row in enumerate(inactive_rows):
        row['_row_index'] = f'inactive:{index}'
    matches = _match_decoys(active_rows, inactive_rows, max_tanimoto, decoys_per_active, max_pairs)
    if not matches:
        raise ValueError('no hard decoy pairs satisfy the scaffold and similarity constraints')

    test_rows = []
    active_output = {}
    for match in matches:
        active_name = match['active']['name']
        if active_name not in active_output:
            active_row = dict(match['active'])
            active_row.pop('_row_index', None)
            active_row['benchmark_role'] = 'active'
            active_row['matched_active'] = active_name
            active_row['decoy_descriptor_distance'] = None
            active_row['decoy_tanimoto'] = None
            active_output[active_name] = active_row

        decoy_row = dict(match['decoy'])
        decoy_name = decoy_row['name']
        decoy_row.pop('_row_index', None)
        decoy_row['source_name'] = decoy_name
        decoy_row['name'] = f'hard_decoy_{match["active"]["name"]}_{decoy_name}'
        decoy_row['tag'] = 'inactive'
        decoy_row['split'] = 'test'
        decoy_row['benchmark_role'] = 'hard_decoy'
        decoy_row['matched_active'] = match['active']['name']
        decoy_row['decoy_descriptor_distance'] = match['descriptor_distance']
        decoy_row['decoy_tanimoto'] = match['tanimoto']
        test_rows.append(decoy_row)

    output = pd.concat([train, pd.DataFrame(list(active_output.values()) + test_rows)], ignore_index=True, sort=False)
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_csv, index=False)
    overlap = _overlap_summary(output)
    provenance = {
        'source': 'property-matched hard-decoy benchmark',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'input_csv': str(input_csv),
        'output_csv': str(output_csv),
        'input_rows': int(len(frame)),
        'train_rows': int(len(train)),
        'test_rows': int(len(active_output) + len(test_rows)),
        'test_active_rows': int(len(active_output)),
        'test_hard_decoy_rows': int(len(test_rows)),
        'max_pairs': int(max_pairs) if max_pairs is not None else None,
        'max_tanimoto': float(max_tanimoto),
        'decoys_per_active': int(decoys_per_active),
        'descriptor_features': list(DESCRIPTOR_NAMES),
        'matching': 'nearest standardized descriptor distance with scaffold inequality and Morgan Tanimoto cap',
        **overlap,
        'independence_note': 'Hard-decoy matching increases chemical difficulty; source independence still depends on the input split provenance.',
    }
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output, provenance


def build_random_control_benchmark(input_csv, active_names, output_csv, provenance_path, random_state=42):
    frame = load_external_dataset(input_csv).reset_index(drop=True)
    train = frame[frame['split'] == 'train'].copy()
    test = frame[frame['split'] == 'test'].copy()
    active = test[test['name'].isin(set(active_names))].copy()
    inactive = test[test['tag'] == 'inactive'].copy()
    if active.empty or len(inactive) < len(active):
        raise ValueError('input test split has insufficient active or inactive rows for random control')
    rng = np.random.default_rng(random_state)
    selected_positions = rng.choice(len(inactive), size=len(active), replace=False)
    selected_inactive = inactive.iloc[selected_positions].reset_index(drop=True)
    test_rows = []
    for active_row, decoy_row in zip(active.sort_values('name').to_dict('records'), selected_inactive.to_dict('records')):
        active_row['benchmark_role'] = 'active'
        active_row['matched_active'] = active_row['name']
        active_row['decoy_descriptor_distance'] = None
        active_row['decoy_tanimoto'] = None
        test_rows.append(active_row)
        source_name = decoy_row['name']
        decoy_row['source_name'] = source_name
        decoy_row['name'] = f'random_decoy_{active_row["name"]}_{source_name}'
        decoy_row['tag'] = 'inactive'
        decoy_row['split'] = 'test'
        decoy_row['benchmark_role'] = 'random_control'
        decoy_row['matched_active'] = active_row['name']
        decoy_row['decoy_descriptor_distance'] = None
        decoy_row['decoy_tanimoto'] = None
        test_rows.append(decoy_row)
    output = pd.concat([train, pd.DataFrame(test_rows)], ignore_index=True, sort=False)
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_csv, index=False)
    provenance = {
        'source': 'random control benchmark',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'input_csv': str(input_csv),
        'output_csv': str(output_csv),
        'random_state': int(random_state),
        'train_rows': int(len(train)),
        'test_rows': int(len(test_rows)),
        'test_active_rows': int(len(active)),
        'test_random_control_rows': int(len(active)),
        **_overlap_summary(output),
        'independence_note': 'This control uses the same active names and test split as the paired hard-decoy benchmark; only decoy selection changes.',
    }
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build a property-matched hard-decoy benchmark')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--provenance', required=True)
    parser.add_argument('--max-tanimoto', type=float, default=0.5)
    parser.add_argument('--decoys-per-active', type=int, default=1)
    parser.add_argument('--max-pairs', type=int, default=50)
    parser.add_argument('--control-out')
    parser.add_argument('--control-provenance')
    parser.add_argument('--random-state', type=int, default=42)
    args = parser.parse_args(argv)
    if args.decoys_per_active < 1:
        raise SystemExit('--decoys-per-active must be positive')
    if args.max_pairs < 1:
        raise SystemExit('--max-pairs must be positive')
    output, provenance = build_hard_decoy_benchmark(
        args.input,
        args.out,
        args.provenance,
        args.max_tanimoto,
        args.decoys_per_active,
        args.max_pairs,
    )
    if args.control_out:
        control_provenance = args.control_provenance or f'{args.control_out}.provenance.json'
        active_names = output.loc[
            (output['split'] == 'test') & (output['benchmark_role'] == 'active'), 'name'
        ].tolist()
        build_random_control_benchmark(
            args.input,
            active_names,
            args.control_out,
            control_provenance,
            args.random_state,
        )
    print(json.dumps(provenance, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
