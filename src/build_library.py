"""配体库准备：SMILES -> 3D SDF -> PDBQT（用于对接）。

流程（CADD 基础知识）：
- SMILES 是分子的"一维字符串表示"(如 erlotinib 的 SMILES)
- RDKit 把它转成 3D 构象,输出 SDF
- Meeko 再把 SDF 转成 AutoDock Vina 要的 PDBQT(配体),同时标定可旋转键
"""
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import AllChem
from meeko import MoleculePreparation, PDBQTWriterLegacy


def smiles_to_sdf(smiles_list, out_sdf, names=None):
    """把 SMILES 列表转成一个多分子 SDF 文件。"""
    writer = Chem.SDWriter(str(out_sdf))
    for i, smi in enumerate(smiles_list):
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        mol = Chem.AddHs(mol)
        # 生成初始 3D 构象
        AllChem.EmbedMolecule(mol, randomSeed=42)
        AllChem.MMFFOptimizeMolecule(mol)
        n = names[i] if names and i < len(names) else f'MOL_{i}'
        mol.SetProp('_Name', n)
        writer.write(mol)
    writer.close()
    return out_sdf


def sdf_to_pdbqt(sdf_path, out_dir, prefix='ligand'):
    """用 meeko 把 SDF 转为每个分子的 PDBQT 文件。"""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    prep = MoleculePreparation()
    mols = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
    pdbqt_paths = []
    for i, mol in enumerate(mols):
        if mol is None:
            continue
        mols_prepared = prep.prepare(mol)
        for j, prepared in enumerate(mols_prepared):
            # meeko v0.5+ 用 PDBQTWriterLegacy 生成 PDBQT
            pdbqt_string, is_ok, err_msg = PDBQTWriterLegacy.write_string(prepared)
            if not is_ok:
                print(f'  [warn] 分子 {i} PDBQT 生成失败: {err_msg}')
                continue
            pdbqt_path = out_dir / f'{prefix}_{i}.pdbqt'
            pdbqt_path.write_text(pdbqt_string)
            pdbqt_paths.append(pdbqt_path)
    return pdbqt_paths


def build_library(smiles_list, out_sdf, out_pdbqt_dir, names=None):
    sdf = smiles_to_sdf(smiles_list, out_sdf, names)
    pdbqts = sdf_to_pdbqt(sdf, out_pdbqt_dir)
    return sdf, pdbqts


if __name__ == '__main__':
    # 示例：erlotinib(EGFR 抑制剂) + 阿司匹林(非活性对照)
    test_smiles = [
        'COc1cc(OCCCN2CCOCC2)c(cc1Nc1ncc(F)cc1-c1cccc(OC)c1)OC',  # erlotinib
        'CC(=O)Oc1ccccc1C(=O)O',  # aspirin
    ]
    sdf, pdbqts = build_library(test_smiles, 'data/test_lib.sdf', 'data/pdbqt_test')
    print(f'[build_library] SDF: {sdf}')
    print(f'[build_library] PDBQT 数量: {len(pdbqts)}')
    for p in pdbqts:
        print('  ', p.name, p.stat().st_size, 'bytes')
