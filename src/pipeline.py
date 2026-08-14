"""虚拟筛选主流水线：一条命令完成 受体→分子库→对接→打分报告。

用法:
    python -m src.pipeline                 # 用默认配置(data/4hjo.pdb + library_data)
    python -m src.pipeline --receptor x.pdb --out out_dir

流程:
  1. prepare_receptor : PDB 受体准备(去配体/水, Vina 可直接读的干净 PDB)
  2. build_library    : 分子库 SMILES -> 3D SDF -> PDBQT
  3. dock_vina        : 批量对接, 得每个分子的 affinity(kcal/mol)
  4. report           : 生成 top_hits.csv + report.md(打分排序,正负对照对比)

这是标准的"虚拟筛选(virtual screening)"工作流最小闭环。
"""
import argparse
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare_receptor import prepare as prep_receptor
from build_library import build_library
from library_data import build_screening_library
from dock_vina import dock_batch


def run_ml(lib, out_dir):
    """ML 活性预测初筛：用 RDKit 描述符 + 随机森林，对分子库预测活性概率。"""
    try:
        from ml_predictor import train_model, predict_activity
        clf, metrics = train_model(lib)
        names = list(lib.keys())
        res = predict_activity(clf, list(lib.values()), lib)
        prob_map = dict(res)
        from library_data import is_active
        out = []
        for n in names:
            tag = 'active' if is_active(n) else 'inactive'
            out.append({'mol_name': n, 'tag': tag, 'ml_prob': prob_map.get(lib[n], None)})
        import pandas as pd
        df = pd.DataFrame(out)
        df.to_csv(Path(out_dir) / 'ml_scores.csv', index=False)
        acc = metrics.get('accuracy') if metrics else None
        if acc:
            print(f'  [ML] 留一交叉验证 accuracy={acc:.2f} AUC={metrics.get("auc"):.2f}')
        return df
    except Exception as e:
        print(f'  [ML] 活性预测跳过: {e}')
        return None


def run(receptor_pdb: str = 'data/4hjo.pdb', out_dir: str = 'output',
        exhaustiveness: int = 8, labels: str = None):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 受体
    print('== 1/4 受体准备 ==')
    receptor = prep_receptor(receptor_pdb, 'output')

    # 2) 分子库
    print('== 2/4 分子库准备 ==')
    lib = build_screening_library()
    names = list(lib.keys()); smis = list(lib.values())
    sdf, pdbqts = build_library(smis, 'data/screening_lib.sdf', 'data/pdbqt_lib', names)

    # 2.5) ML 活性初筛(可选加分)
    print('== 2.5/4 ML 活性预测(随机森林) ==')
    ml_df = run_ml(lib, out_dir)

    # 3) 批量对接
    print(f'== 3/4 批量对接({len(pdbqts)} 配体, exhaustiveness={exhaustiveness}) ==')
    # source_pdb 传原始含共晶配体的 PDB,用于从 AQ4 算盒子中心
    df = dock_batch(pdbqts, receptor, out_dir / 'docks', exhaustiveness, source_pdb=receptor_pdb)

    # 4) 报告:合并分子名/正负对照标签
    print('== 4/4 生成报告 ==')
    # dock_batch 每行 name 是配体文件名 stem(如 ligand_0)不含序号时,已按入参顺序返回
    # pdbqt 生成顺序 = pdbqts 列表顺序 = names 顺序。dock_batch 里 ligand_{i} 对应第 i 个 pdbqts。
    # 用 pdbqt 文件名里的序号(ligand_N.pdbqt 的 N)映射回 names[N]
    seq_to_name = {int(p.stem.split('_')[-1]): names[i] for i, p in enumerate(pdbqts)}
    final = df.copy()
    def map_name(ligand_stem):
        # ligand_3.pdbqt stem = 'ligand_3'
        try:
            idx = int(ligand_stem.split('_')[-1])
            return seq_to_name.get(idx, f'UNK_{idx}')
        except Exception:
            return ligand_stem
    final['mol_name'] = final['name'].map(map_name)
    # 用与 library_data 一致的活性判定规则(避免标签不一致 bug)
    from library_data import is_active
    def tag(n):
        return 'active' if is_active(n) else 'inactive'
    final['tag'] = final['mol_name'].map(tag)
    final = final[['mol_name', 'tag', 'affinity']].sort_values('affinity').reset_index(drop=True)
    final.to_csv(out_dir / 'top_hits.csv', index=False)
    print('\n=== 虚拟筛选结果(affinity 升序,越负越强) ===')
    print(final.to_string(index=False))
    print(f'\n[完成] 结果 CSV: {out_dir}/top_hits.csv')
    # 离线生成 markdown 报告
    from report import generate_report
    generate_report(out_dir / 'top_hits.csv', out_dir / 'report.md')
    return final


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--receptor', default='data/4hjo.pdb')
    ap.add_argument('--out', default='output')
    ap.add_argument('--exhaustiveness', type=int, default=8)
    a = ap.parse_args()
    run(a.receptor, a.out, a.exhaustiveness)
