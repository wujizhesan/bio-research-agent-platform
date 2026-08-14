"""ML 活性预测层：用 RDKit 分子描述符 + 随机森林 预测分子活性。

这是"AI + 虚拟筛选"混合流水线的一环：
  - 先用 ML 模型(基于已知活性分子的描述符)对分子库初筛打分 → 预测活性概率
  - 待对接结果基础上,可把 ML 预测作为"对同一批分子"的交叉验证/融合打分

设计说明:
  - 特征: RDKit 2D 描述符(理化性质、拓扑、部分电荷等),无需 3D,快速
  - 模型: 随机森林(轻量、无需 GPU、Windows 可跑)
  - 训练标签: 分子库自带的正/负对照标签(active/inactive)
  - 演示用途: 用有标签的子集训练,并对全库预测,展示 ML 打分能力
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np
from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))


def get_descriptors(mol):
    """计算分子的一组 RDKit 2D 描述符作为特征向量。"""
    if mol is None:
        return None
    # 选一组稳定的、有代表性的描述符(RDKit 2026 命名)
    feats = [
        Descriptors.MolWt, Descriptors.MolLogP, Descriptors.TPSA,
        Descriptors.NumHDonors, Descriptors.NumHAcceptors, Descriptors.NumRotatableBonds,
        Descriptors.RingCount, Descriptors.NumAromaticRings,
        Descriptors.MolMR, Descriptors.FractionCSP3,
    ]
    return np.array([f(mol) for f in feats], dtype=float)


def featurize(smiles_list):
    """把 SMILES 列表转成特征矩阵 (n_samples, n_features)。"""
    X, ok = [], []
    for smi in smiles_list:
        m = Chem.MolFromSmiles(smi)
        vec = get_descriptors(m)
        if vec is not None:
            X.append(vec)
            ok.append(smi)
    return np.array(X), ok


def train_model(lib):
    """用分子库(带标签)训练随机森林分类器。

    用留一交叉验证(LOO-CV)客观评估 —— 避免"训练即预测"的数据泄漏。
    对库规模 26 个分子,留一法是合适且诚实的评估方式。
    返回 (clf, metrics) 其中 metrics = {'accuracy', 'auc'}
    """
    from library_data import is_active
    names = list(lib.keys())
    smis = list(lib.values())
    y = np.array([1 if is_active(n) else 0 for n in names])
    X, valid_smis = featurize(smis)
    valid_idx = [smis.index(s) for s in valid_smis]
    y = y[valid_idx]

    if len(set(y)) < 2:
        clf = RandomForestClassifier(n_estimators=200, random_state=42, max_depth=5)
        clf.fit(X, y)
        return clf, {'accuracy': None, 'auc': None, 'loo': False}

    # 留一交叉验证:每次留 1 个做测试,其余训练,完全无泄漏
    from sklearn.model_selection import LeaveOneOut
    loo = LeaveOneOut()
    y_true, y_prob = [], []
    for tr_idx, te_idx in loo.split(X):
        clf = RandomForestClassifier(n_estimators=200, random_state=42, max_depth=5)
        clf.fit(X[tr_idx], y[tr_idx])
        p = clf.predict_proba(X[te_idx])[0][1]
        y_true.append(y[te_idx][0]); y_prob.append(p)

    acc = accuracy_score(y_true, [1 if p >= 0.5 else 0 for p in y_prob])
    try:
        auc = roc_auc_score(y_true, y_prob)
    except Exception:
        auc = None

    # 在全库上再拟合一次,用于后续 predict_activity
    clf_full = RandomForestClassifier(n_estimators=200, random_state=42, max_depth=5)
    clf_full.fit(X, y)
    return clf_full, {'accuracy': acc, 'auc': auc, 'loo': True}


def predict_activity(clf, smiles_list, lib):
    """对一组分子做 ML 活性概率预测,返回 [(name, active_prob)]。"""
    X, valid_smis = featurize(smiles_list)
    if len(valid_smis) == 0:
        return []
    probs = clf.predict_proba(X)[:, 1]
    return list(zip(valid_smis, probs))


if __name__ == '__main__':
    from library_data import build_screening_library
    lib = build_screening_library()
    clf, metrics = train_model(lib)
    print('[ml_predictor] 随机森林模型训练完成(留一交叉验证)')
    if metrics.get('accuracy') is not None:
        print(f'  [留一交叉验证] accuracy={metrics["accuracy"]:.2f} '
              f'AUC={metrics["auc"]:.2f} (无数据泄漏)')
    else:
        print('  [提示] 样本类别不足,未做交叉验证评估')
    # 对全库预测
    res = predict_activity(clf, list(lib.values()), lib)
    names = list(lib.keys())
    print('\n=== ML 活性预测结果(按概率降序,前 12) ===')
    scored = sorted(
        [(names[list(lib.values()).index(smi)], p) for smi, p in res],
        key=lambda x: -x[1])
    for nm, p in scored[:12]:
        from library_data import is_active
        tag = 'ACT' if is_active(nm) else 'INACT'
        print(f'  {nm:44s} [{tag}] 活性概率={p:.2f}')
