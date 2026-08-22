"""Create grouped external holdout datasets without changing raw inputs."""
import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .ml_predictor import load_external_dataset, scaffold_key
except ImportError:
    from ml_predictor import load_external_dataset, scaffold_key


def _document_values(frame):
    for column in ('document_ids', 'document_chembl_ids', 'document_chembl_id'):
        if column in frame.columns:
            values = []
            for value in frame[column]:
                if pd.isna(value):
                    values.append(set())
                else:
                    values.append({item.strip() for item in str(value).split('|') if item.strip()})
            if any(values):
                return values, column
    raise ValueError('dataset has no usable document identifiers')


def _connected_groups(document_values, scaffold_values=None):
    parent = list(range(len(document_values)))
    owner = {}

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, documents in enumerate(document_values):
        keys = [f'document:{value}' for value in documents]
        if scaffold_values is not None:
            keys.append(f'scaffold:{scaffold_values[index]}')
        for key in keys:
            if key in owner:
                union(index, owner[key])
            else:
                owner[key] = index

    groups = defaultdict(list)
    for index in range(len(document_values)):
        groups[find(index)].append(index)
    return list(groups.values())


def _valid_split(y, train, test):
    return len(test) > 0 and len(set(y[test])) == 2 and len(set(y[train])) == 2


def document_split_indices(document_values, y, test_fraction=0.2, random_state=42):
    groups = _connected_groups(document_values)
    return _group_split_indices(groups, y, test_fraction, random_state, prefer_small=False)


def joint_split_indices(document_values, scaffold_values, y, test_fraction=0.2, random_state=42):
    groups = _connected_groups(document_values, scaffold_values)
    return _group_split_indices(groups, y, test_fraction, random_state, prefer_small=True)


def _group_split_indices(groups, y, test_fraction, random_state, prefer_small):
    if len(groups) < 2:
        return list(range(len(y))), []
    y = np.asarray(y)
    target = min(max(1, round(len(y) * test_fraction)), len(y) - 1)
    ordered = sorted(
        groups,
        key=lambda indices: (
            len(indices) if prefer_small else 0,
            hashlib.sha256(f'{random_state}:{min(indices)}'.encode('utf-8')).hexdigest(),
        ),
    )
    best = None
    if len(ordered) <= 18:
        for mask in range(1, (1 << len(ordered)) - 1):
            test = [
                index
                for group_index, indices in enumerate(ordered)
                if mask & (1 << group_index)
                for index in indices
            ]
            test_set = set(test)
            train = [index for index in range(len(y)) if index not in test_set]
            if not _valid_split(y, train, test):
                continue
            score = (abs(len(test) - target), abs(float(y[test].mean()) - float(y.mean())), sorted(test))
            if best is None or score < best[0]:
                best = (score, sorted(train), sorted(test))
    if best is not None:
        return best[1], best[2]

    if prefer_small:
        target_active = target * float(y.mean())
        candidates = [indices for indices in ordered if len(indices) <= target]
        selected_ids = set()
        test = []
        active_count = 0
        while True:
            choices = []
            for indices in candidates:
                group_id = min(indices)
                if group_id in selected_ids:
                    continue
                new_size = len(test) + len(indices)
                if new_size > target:
                    continue
                new_active = active_count + int(y[indices].sum())
                score = (
                    abs(new_size - target) / target
                    + 3 * abs(new_active - target_active) / target_active
                    + 0.001 * (
                        int(hashlib.sha256(
                            f'{random_state}:{group_id}'.encode('utf-8')
                        ).hexdigest(), 16) % 1000
                    ) / 1000
                )
                choices.append((score, group_id, indices, new_active))
            if not choices:
                break
            _, group_id, indices, active_count = min(choices, key=lambda item: item[0])
            selected_ids.add(group_id)
            test.extend(indices)
        test = sorted(test)
        test_set = set(test)
        train = [index for index in range(len(y)) if index not in test_set]
        if _valid_split(y, train, test):
            return train, test
    test = []
    for indices in ordered:
        if len(test) >= target:
            break
        if len(test) + len(indices) <= target:
            test.extend(indices)
    test = sorted(test)
    test_set = set(test)
    train = [index for index in range(len(y)) if index not in test_set]
    if not _valid_split(y, train, test):
        raise ValueError('grouped split cannot preserve both classes in train and test')
    return train, test


def build_grouped_holdout(input_csv, output_csv, provenance_path, test_fraction=0.2, mode='joint'):
    frame = load_external_dataset(input_csv)
    document_values, document_column = _document_values(frame)
    y = (frame['tag'] == 'active').astype(int).to_numpy()
    scaffolds = [scaffold_key(value) for value in frame['smiles']]
    if mode == 'joint':
        train_indices, test_indices = joint_split_indices(
            document_values, scaffolds, y, test_fraction=test_fraction
        )
        strategy = 'connected components of document IDs and Bemis-Murcko scaffolds'
    else:
        train_indices, test_indices = document_split_indices(
            document_values, y, test_fraction=test_fraction
        )
        strategy = 'connected components of document IDs'
    split = np.full(len(frame), 'train', dtype=object)
    split[test_indices] = 'test'
    output = frame.copy()
    output['split'] = split
    output_csv = Path(output_csv)
    provenance_path = Path(provenance_path)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_csv, index=False)
    train_documents = set().union(*(document_values[index] for index in train_indices))
    test_documents = set().union(*(document_values[index] for index in test_indices))
    train_scaffolds = {scaffolds[index] for index in train_indices}
    test_scaffolds = {scaffolds[index] for index in test_indices}
    provenance = {
        'source': 'grouped external holdout',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'input_csv': str(input_csv),
        'output_csv': str(output_csv),
        'mode': mode,
        'document_column': document_column,
        'test_fraction_requested': test_fraction,
        'train_rows': len(train_indices),
        'test_rows': len(test_indices),
        'train_document_count': len(train_documents),
        'test_document_count': len(test_documents),
        'document_overlap_count': len(train_documents & test_documents),
        'train_scaffold_count': len(train_scaffolds),
        'test_scaffold_count': len(test_scaffolds),
        'scaffold_overlap_count': len(train_scaffolds & test_scaffolds),
        'train_active_fraction': float(y[train_indices].mean()) if train_indices else None,
        'test_active_fraction': float(y[test_indices].mean()) if test_indices else None,
        'active_fraction_abs_diff': abs(float(y[train_indices].mean()) - float(y[test_indices].mean())) if train_indices and test_indices else None,
        'split_strategy': strategy,
        'independence_note': 'Grouping removes measured document/structure leakage; source-level independence still requires provenance review.',
    }
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create a grouped external holdout')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--provenance', required=True)
    parser.add_argument('--test-fraction', type=float, default=0.2)
    parser.add_argument('--mode', choices=('document', 'joint'), default='joint')
    args = parser.parse_args(argv)
    _, provenance = build_grouped_holdout(
        args.input, args.out, args.provenance, args.test_fraction, args.mode
    )
    print(json.dumps(provenance, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
