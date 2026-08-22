"""ML 活性预测层：scaffold holdout 或外部 train/test 数据集评估。"""
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import Descriptors
from rdkit.Chem.Scaffolds import MurckoScaffold
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, precision_score, recall_score,
    roc_auc_score, average_precision_score,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

RANDOM_STATE = 42
EXTERNAL_COLUMNS = {'name', 'smiles', 'tag', 'split'}


def get_descriptors(mol):
    """计算分子的一组 RDKit 2D 描述符作为特征向量。"""
    if mol is None:
        return None
    feats = [
        Descriptors.MolWt, Descriptors.MolLogP, Descriptors.TPSA,
        Descriptors.NumHDonors, Descriptors.NumHAcceptors, Descriptors.NumRotatableBonds,
        Descriptors.RingCount, Descriptors.NumAromaticRings,
        Descriptors.MolMR, Descriptors.FractionCSP3,
    ]
    return np.array([f(mol) for f in feats], dtype=float)


def featurize(smiles_list, return_indices=False):
    """把 SMILES 列表转成特征矩阵。"""
    X, ok, valid_indices = [], [], []
    for index, smi in enumerate(smiles_list):
        mol = Chem.MolFromSmiles(smi)
        vec = get_descriptors(mol)
        if vec is not None:
            X.append(vec)
            ok.append(smi)
            valid_indices.append(index)
    result = (np.array(X), ok)
    return result + (valid_indices,) if return_indices else result


def scaffold_key(smiles):
    """返回 Bemis-Murcko scaffold；无环分子用规范 SMILES 保持分组可区分。"""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
    return scaffold or f'acyclic:{Chem.MolToSmiles(mol, canonical=True)}'


def scaffold_split_indices(smiles_list, y, test_fraction=0.25, random_state=RANDOM_STATE):
    """按 scaffold 分组切分，返回 train/test 下标，避免同 scaffold 泄漏。"""
    groups = defaultdict(list)
    for index, smiles in enumerate(smiles_list):
        key = scaffold_key(smiles)
        if key is not None:
            groups[key].append(index)
    if len(groups) < 2:
        return list(range(len(smiles_list))), []

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: hashlib.sha256(
            f'{random_state}:{item[0]}'.encode('utf-8')
        ).hexdigest(),
    )
    target = min(max(1, round(len(smiles_list) * test_fraction)), len(smiles_list) - 1)
    y = np.asarray(y)
    best = None

    if len(ordered_groups) <= 18:
        for mask in range(1, (1 << len(ordered_groups)) - 1):
            test = [
                index
                for group_index, (_, indices) in enumerate(ordered_groups)
                if mask & (1 << group_index)
                for index in indices
            ]
            test_set = set(test)
            train = [index for index in range(len(smiles_list)) if index not in test_set]
            if len(set(y[test])) < 2 or len(set(y[train])) < 2:
                continue
            test_rate = float(y[test].mean())
            score = (abs(len(test) - target), abs(test_rate - float(y.mean())), sorted(test))
            if best is None or score < best[0]:
                best = (score, sorted(train), sorted(test))

    if best is not None:
        return best[1], best[2]

    test = []
    for _, indices in ordered_groups:
        if len(test) < target and len(test) + len(indices) < len(smiles_list):
            test.extend(indices)
    test = sorted(test)
    test_set = set(test)
    train = [index for index in range(len(smiles_list)) if index not in test_set]
    return train, test


def _validate_external_frame(df):
    canonical = []
    invalid_names = []
    for _, row in df.iterrows():
        mol = Chem.MolFromSmiles(row['smiles'])
        if mol is None:
            invalid_names.append(row['name'])
            canonical.append(None)
        else:
            canonical.append(Chem.MolToSmiles(mol, canonical=True))
    if invalid_names:
        names = ', '.join(invalid_names[:5])
        suffix = '...' if len(invalid_names) > 5 else ''
        raise ValueError(f'外部 ML 数据集包含无法解析的 SMILES: {names}{suffix}')

    duplicate_count = int(pd.Series(canonical).duplicated().sum())
    if duplicate_count:
        raise ValueError(f'外部 ML 数据集包含 {duplicate_count} 个重复规范 SMILES，已拒绝以避免数据泄漏')

    train_smiles = df.loc[df['split'] == 'train', 'smiles']
    test_smiles = df.loc[df['split'] == 'test', 'smiles']
    train_scaffolds = {scaffold_key(smiles) for smiles in train_smiles}
    test_scaffolds = {scaffold_key(smiles) for smiles in test_smiles}
    overlap = train_scaffolds & test_scaffolds
    document_column = None
    if 'document_ids' in df.columns:
        document_column = 'document_ids'
    elif 'document_chembl_ids' in df.columns:
        document_column = 'document_chembl_ids'
    elif 'document_chembl_id' in df.columns:
        document_column = 'document_chembl_id'

    def document_ids(value):
        if pd.isna(value):
            return set()
        return {item.strip() for item in str(value).split('|') if item.strip()}

    validation = {
        'invalid_smiles_count': 0,
        'duplicate_smiles_count': duplicate_count,
        'train_scaffold_count': len(train_scaffolds),
        'test_scaffold_count': len(test_scaffolds),
        'scaffold_overlap_count': len(overlap),
        'document_audit_column': document_column,
        'document_overlap_count': None,
        'train_document_count': None,
        'test_document_count': None,
    }
    if document_column:
        train_documents = set().union(*(
            document_ids(value) for value in df.loc[df['split'] == 'train', document_column]
        ))
        test_documents = set().union(*(
            document_ids(value) for value in df.loc[df['split'] == 'test', document_column]
        ))
        validation.update({
            'document_overlap_count': len(train_documents & test_documents),
            'train_document_count': len(train_documents),
            'test_document_count': len(test_documents),
        })
    return validation


def load_external_dataset(path):
    """读取带 train/test split 标记的外部标注数据集。"""
    path = Path(path)
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        raise ValueError(f'外部 ML 数据集无法读取: {path}: {exc}') from exc
    missing = EXTERNAL_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f'外部 ML 数据集缺少字段: {", ".join(sorted(missing))}')
    df = df.copy()
    df['name'] = df['name'].astype(str).str.strip()
    df['smiles'] = df['smiles'].astype(str).str.strip()
    df['tag'] = df['tag'].astype(str).str.lower().str.strip()
    df['split'] = df['split'].astype(str).str.lower().str.strip()
    if df['name'].eq('').any() or df['smiles'].eq('').any():
        raise ValueError('外部 ML 数据集存在空 name 或 smiles')
    if df['name'].duplicated().any():
        raise ValueError('外部 ML 数据集存在重复 name')
    if not set(df['tag']).issubset({'active', 'inactive'}):
        raise ValueError('外部 ML 数据集 tag 只能是 active/inactive')
    if not set(df['split']).issubset({'train', 'test'}):
        raise ValueError('外部 ML 数据集 split 只能是 train/test')
    if set(df['split']) != {'train', 'test'}:
        raise ValueError('外部 ML 数据集必须同时包含 train 和 test')
    df.attrs['validation'] = _validate_external_frame(df)
    return df


def _screening_metrics(y_true, y_prob, threshold=0.5):
    y_true = np.asarray(y_true)
    y_prob = np.asarray(y_prob)
    y_pred = (y_prob >= threshold).astype(int)
    k = max(1, int(len(y_true) * 0.3))
    top_indices = np.argsort(y_prob)[::-1][:k]
    baseline = float(y_true.mean()) if len(y_true) else 0.0
    top_fraction = float(y_true[top_indices].mean()) if len(top_indices) else 0.0
    metrics = {
        'accuracy': float(accuracy_score(y_true, y_pred)),
        'balanced_accuracy': float(balanced_accuracy_score(y_true, y_pred)),
        'precision': float(precision_score(y_true, y_pred, zero_division=0)),
        'recall': float(recall_score(y_true, y_pred, zero_division=0)),
        'top30_active_fraction': top_fraction,
        'top30_enrichment': top_fraction / baseline if baseline else None,
        'pr_auc_baseline': baseline,
    }
    try:
        metrics['auc'] = float(roc_auc_score(y_true, y_prob))
        metrics['roc_auc'] = metrics['auc']
    except ValueError:
        metrics['auc'] = None
        metrics['roc_auc'] = None
    try:
        metrics['pr_auc'] = float(average_precision_score(y_true, y_prob))
        metrics['pr_auc_lift'] = metrics['pr_auc'] / baseline if baseline else None
    except ValueError:
        metrics['pr_auc'] = None
        metrics['pr_auc_lift'] = None
    return metrics


def _class_balance_metrics(y, prefix=''):
    y = np.asarray(y)
    total = len(y)
    active = int(y.sum()) if total else 0
    inactive = int(total - active)
    minority = min(active, inactive)
    majority = max(active, inactive)
    return {
        f'{prefix}active_fraction': float(active / total) if total else None,
        f'{prefix}class_imbalance_ratio': float(majority / minority) if minority else None,
    }


def _new_model(class_weight=None, n_estimators=200):
    return RandomForestClassifier(
        n_estimators=n_estimators,
        random_state=RANDOM_STATE,
        max_depth=5,
        class_weight=class_weight,
    )

def _leave_one_out_metrics(X, y):
    from sklearn.model_selection import LeaveOneOut
    y_true, y_prob = [], []
    for train_index, test_index in LeaveOneOut().split(X):
        clf = _new_model()
        clf.fit(X[train_index], y[train_index])
        y_true.append(y[test_index][0])
        y_prob.append(clf.predict_proba(X[test_index])[0][1])
    return _screening_metrics(y_true, y_prob)


def _calibrate_threshold(X, y, groups):
    from sklearn.model_selection import GroupKFold, cross_val_predict

    if len(y) > 5000:
        return 0.5, None, 'fixed_0.5_large_dataset'
    groups = np.asarray(groups)
    unique_groups = set(groups)
    if len(unique_groups) < 5 or len(set(y)) < 2:
        return 0.5, None, 'fixed_0.5_insufficient_scaffolds'
    large_dataset = len(y) > 1000
    try:
        n_splits = 3 if large_dataset else min(5, len(unique_groups))
        cv = GroupKFold(n_splits=n_splits)
        oof_prob = cross_val_predict(
            _new_model(
                class_weight='balanced',
                n_estimators=100 if large_dataset else 200,
            ),
            X,
            y,
            cv=cv,
            groups=groups,
            method='predict_proba',
        )[:, 1]
    except ValueError:
        return 0.5, None, 'fixed_0.5_cv_unavailable'
    candidates = np.linspace(0.05, 0.95, 181)
    scores = [
        balanced_accuracy_score(y, (oof_prob >= threshold).astype(int))
        for threshold in candidates
    ]
    best_score = max(scores)
    best_threshold = min(
        (threshold for threshold, score in zip(candidates, scores) if score == best_score),
        key=lambda threshold: abs(threshold - 0.5),
    )
    strategy = 'training_only_grouped_oof_3fold' if large_dataset else 'training_only_grouped_oof'
    return float(best_threshold), float(best_score), strategy

def _repeated_scaffold_metrics(smiles_list, y_all, test_fraction=0.2, random_states=(42, 43, 44, 45, 46)):
    X, _, valid_indices = featurize(smiles_list, return_indices=True)
    smiles = np.asarray(smiles_list)[valid_indices]
    y = np.asarray(y_all)[valid_indices]
    if len(y) > 1000:
        return {
            'repeated_scaffold_n': 0,
            'repeated_scaffold_seeds': list(random_states),
            'repeated_scaffold_skip_reason': 'dataset larger than 1000 samples',
            **{
                f'repeated_scaffold_{key}_{suffix}': None
                for key in ('balanced_accuracy', 'roc_auc', 'pr_auc', 'top30_enrichment')
                for suffix in ('mean', 'std')
            },
        }
    rows = []
    for seed in random_states:
        train_indices, test_indices = scaffold_split_indices(
            smiles, y, test_fraction=test_fraction, random_state=seed
        )
        if not test_indices or len(set(y[train_indices])) < 2 or len(set(y[test_indices])) < 2:
            continue
        clf = _new_model(class_weight='balanced')
        clf.fit(X[train_indices], y[train_indices])
        probability = clf.predict_proba(X[test_indices])[:, 1]
        rows.append(_screening_metrics(y[test_indices], probability))
    metrics = {
        'repeated_scaffold_n': len(rows),
        'repeated_scaffold_seeds': list(random_states),
    }
    for key in ('balanced_accuracy', 'roc_auc', 'pr_auc', 'top30_enrichment'):
        values = [row[key] for row in rows if row.get(key) is not None]
        metrics[f'repeated_scaffold_{key}_mean'] = float(np.mean(values)) if values else None
        metrics[f'repeated_scaffold_{key}_std'] = float(np.std(values, ddof=1)) if len(values) > 1 else None
    return metrics

def _train_external_model(dataset_path):
    df = load_external_dataset(dataset_path)
    train_df = df[df['split'] == 'train'].reset_index(drop=True)
    test_df = df[df['split'] == 'test'].reset_index(drop=True)
    X_train, _, valid_train = featurize(train_df['smiles'].tolist(), return_indices=True)
    X_test, _, valid_test = featurize(test_df['smiles'].tolist(), return_indices=True)
    y_train_all = (train_df['tag'] == 'active').astype(int).to_numpy()
    y_test_all = (test_df['tag'] == 'active').astype(int).to_numpy()
    y_train = y_train_all[valid_train]
    y_test = y_test_all[valid_test]
    if len(set(y_train)) < 2:
        raise ValueError('外部 ML 数据集 train 必须同时包含 active/inactive')
    if len(y_test) == 0:
        raise ValueError('外部 ML 数据集 test 没有可解析的 SMILES')

    train_groups = np.array([scaffold_key(smiles) for smiles in train_df['smiles'].tolist()])[valid_train]
    decision_threshold, threshold_score, threshold_strategy = _calibrate_threshold(
        X_train, y_train, train_groups
    )
    clf = _new_model(class_weight='balanced')
    clf.fit(X_train, y_train)
    test_prob = clf.predict_proba(X_test)[:, 1]
    metrics = _screening_metrics(y_test, test_prob, threshold=decision_threshold)
    validation = df.attrs.get('validation', {})
    metrics.update(validation)
    metrics.update(_class_balance_metrics(np.concatenate([y_train, y_test])))
    metrics.update(_class_balance_metrics(y_test, prefix='test_'))
    metrics.update(_repeated_scaffold_metrics(
        df['smiles'].tolist(),
        (df['tag'] == 'active').astype(int).to_numpy(),
    ))
    metrics.update({
        'n_samples': int(len(y_train) + len(y_test)),
        'n_active': int((y_train.sum() + y_test.sum())),
        'n_inactive': int(len(y_train) + len(y_test) - y_train.sum() - y_test.sum()),
        'n_train': int(len(y_train)),
        'n_test': int(len(y_test)),
        'test_n_active': int(y_test.sum()),
        'test_n_inactive': int(len(y_test) - y_test.sum()),
        'evaluation': 'external_holdout',
        'split_strategy': 'declared train/test split',
        'data_source': str(Path(dataset_path)),
        'warning': '外部数据集评估结果取决于数据来源、标签质量和 train/test 设计',
        'loo_balanced_accuracy': None,
        'decision_threshold': decision_threshold,
        'threshold_strategy': threshold_strategy,
        'threshold_calibration_balanced_accuracy': threshold_score,
    })
    if validation.get('scaffold_overlap_count', 0):
        metrics['warning'] += '；警告：train/test 存在 scaffold 重叠，可能存在结构泄漏'
    if validation.get('document_overlap_count', 0):
        metrics['warning'] += '; warning: train/test share document provenance; independent validation is not established'
    if metrics.get('class_imbalance_ratio') and metrics['class_imbalance_ratio'] >= 3:
        metrics['warning'] += '; warning: class imbalance ratio=' + format(metrics['class_imbalance_ratio'], '.2f') + '; balanced accuracy is more informative than accuracy'
    test_prevalence = metrics.get('test_active_fraction')
    if test_prevalence is not None and (test_prevalence >= 0.8 or test_prevalence <= 0.2):
        metrics['warning'] += '; warning: test-set active prevalence is extreme; interpret PR-AUC against its baseline'
    if threshold_strategy.startswith('training_only_grouped_oof'):
        metrics['warning'] += '; threshold calibrated on training-only grouped OOF predictions'
    if threshold_strategy == 'fixed_0.5_large_dataset':
        metrics['warning'] += '; threshold calibration skipped for large dataset'
    if metrics.get('repeated_scaffold_skip_reason'):
        metrics['warning'] += '; repeated scaffold stability evaluation skipped for large dataset'
    return clf, metrics


def train_model(lib, is_active_fn=None, test_fraction=0.25, external_dataset=None):
    """训练模型；配置外部数据集时优先使用其声明的 train/test。"""
    if external_dataset:
        return _train_external_model(external_dataset)

    label_fn = is_active_fn
    if label_fn is None:
        from library_data import is_active as label_fn

    names = list(lib.keys())
    smiles = list(lib.values())
    y_all = np.array([1 if label_fn(name) else 0 for name in names])
    X, valid_smiles, valid_indices = featurize(smiles, return_indices=True)
    y = y_all[valid_indices]
    test_fraction = float(test_fraction)
    train_indices, test_indices = scaffold_split_indices(valid_smiles, y, test_fraction=test_fraction)
    train_scaffolds = {scaffold_key(valid_smiles[index]) for index in train_indices}
    test_scaffolds = {scaffold_key(valid_smiles[index]) for index in test_indices}

    metrics = {
        'n_samples': int(len(y)),
        'n_active': int(y.sum()),
        'n_inactive': int(len(y) - y.sum()),
        'n_train': int(len(train_indices)),
        'n_test': int(len(test_indices)),
        'evaluation': 'scaffold_holdout',
        'split_strategy': 'Bemis-Murcko scaffold split',
        'test_fraction': test_fraction,
        'data_source': 'screening_library',
        'train_scaffold_count': len(train_scaffolds),
        'test_scaffold_count': len(test_scaffolds),
        'scaffold_overlap_count': len(train_scaffolds & test_scaffolds),
        'warning': '样本量较小，scaffold holdout 仅用于方向性评估，不代表真实外部泛化能力',
    }
    metrics.update(_class_balance_metrics(y))

    if len(set(y)) < 2:
        metrics.update({
            'accuracy': None, 'balanced_accuracy': None, 'precision': None,
            'recall': None, 'auc': None, 'roc_auc': None, 'pr_auc': None,
            'top30_enrichment': None, 'loo_balanced_accuracy': None,
        })
    elif test_indices and len(set(y[train_indices])) >= 2 and len(set(y[test_indices])) >= 2:
        holdout = _new_model()
        holdout.fit(X[train_indices], y[train_indices])
        test_prob = holdout.predict_proba(X[test_indices])[:, 1]
        metrics.update(_screening_metrics(y[test_indices], test_prob))
        metrics['test_n_active'] = int(y[test_indices].sum())
        metrics['test_n_inactive'] = int(len(test_indices) - y[test_indices].sum())
        metrics['loo_balanced_accuracy'] = float(_leave_one_out_metrics(X, y)['balanced_accuracy'])
    else:
        metrics.update({
            'accuracy': None, 'balanced_accuracy': None, 'precision': None,
            'recall': None, 'auc': None, 'roc_auc': None, 'pr_auc': None,
            'top30_enrichment': None, 'loo_balanced_accuracy': None,
            'warning': '无法构造同时包含正负类别的 scaffold holdout，当前仅输出全库模型',
        })

    clf_full = _new_model()
    clf_full.fit(X, y)
    return clf_full, metrics


def predict_activity(clf, smiles_list, lib):
    """对一组分子做 ML 活性概率预测，返回 [(smiles, active_prob)]。"""
    X, valid_smiles = featurize(smiles_list)
    if len(valid_smiles) == 0:
        return []
    probs = clf.predict_proba(X)[:, 1]
    return list(zip(valid_smiles, probs))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--validate':
        dataset = load_external_dataset(sys.argv[2])
        print(json.dumps(dataset.attrs.get('validation', {}), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    from library_data import build_screening_library
    lib = build_screening_library()
    clf, metrics = train_model(lib)
    print(f'[ml_predictor] 评估方式: {metrics.get("evaluation")}')
    if metrics.get('balanced_accuracy') is not None:
        print(
            f'  balanced_accuracy={metrics["balanced_accuracy"]:.2f} '
            f'ROC-AUC={metrics["roc_auc"]:.2f} PR-AUC={metrics["pr_auc"]:.2f}'
        )
