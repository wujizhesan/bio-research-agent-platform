"""准备对接配体：SMILES -> 3D SDF -> PDBQT。"""
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem
from meeko import MoleculePreparation, PDBQTWriterLegacy


def smiles_to_sdf(smiles_list, out_sdf, names=None):
    out_sdf = Path(out_sdf)
    out_sdf.parent.mkdir(parents=True, exist_ok=True)
    writer = Chem.SDWriter(str(out_sdf))
    try:
        for index, smi in enumerate(smiles_list):
            name = names[index] if names and index < len(names) else f'MOL_{index}'
            try:
                mol = Chem.MolFromSmiles(smi)
                if mol is None:
                    raise ValueError('SMILES无法解析')
                mol = Chem.AddHs(mol)
                embed_status = AllChem.EmbedMolecule(mol, randomSeed=42)
                if embed_status < 0:
                    raise ValueError('3D构象生成失败')
                if not AllChem.MMFFHasAllMoleculeParams(mol):
                    raise ValueError('缺少MMFF参数')
                optimize_status = AllChem.MMFFOptimizeMolecule(mol)
                if optimize_status < 0:
                    raise ValueError('MMFF优化失败')
                mol.SetProp('_Name', str(name))
                mol.SetProp('_InputIndex', str(index))
                writer.write(mol)
            except (ValueError, RuntimeError) as exc:
                print(f'  [warn] 分子 {index} ({name}) 建库失败: {exc}')
    finally:
        writer.close()
    return out_sdf


def sdf_to_pdbqt(sdf_path, out_dir, prefix='ligand'):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prep = MoleculePreparation()
    mols = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
    pdbqt_paths = []
    for ordinal, mol in enumerate(mols):
        if mol is None:
            print(f'  [warn] SDF 分子 {ordinal} 无法读取')
            continue
        try:
            input_index = int(mol.GetProp('_InputIndex')) if mol.HasProp('_InputIndex') else ordinal
            prepared_molecules = prep.prepare(mol)
            for prepared in prepared_molecules:
                pdbqt_string, is_ok, err_msg = PDBQTWriterLegacy.write_string(prepared)
                if not is_ok:
                    print(f'  [warn] 分子 {input_index} PDBQT 生成失败: {err_msg}')
                    continue
                pdbqt_path = out_dir / f'{prefix}_{input_index}.pdbqt'
                pdbqt_path.write_text(pdbqt_string, encoding='utf-8')
                pdbqt_paths.append(pdbqt_path)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'  [warn] 分子 {ordinal} PDBQT 处理失败: {exc}')
    return pdbqt_paths


def build_library(smiles_list, out_sdf, out_pdbqt_dir, names=None):
    sdf = smiles_to_sdf(smiles_list, out_sdf, names)
    pdbqts = sdf_to_pdbqt(sdf, out_pdbqt_dir)
    return sdf, pdbqts


if __name__ == '__main__':
    test_smiles = [
        'COc1cc(OCCCN2CCOCC2)c(cc1Nc1ncc(F)cc1-c1cccc(OC)c1)OC',
        'CC(=O)Oc1ccccc1C(=O)O',
    ]
    sdf, pdbqts = build_library(test_smiles, 'data/test_lib.sdf', 'data/pdbqt_test')
    print(f'[build_library] SDF: {sdf}')
    print(f'[build_library] PDBQT 数量: {len(pdbqts)}')
    for path in pdbqts:
        print('  ', path.name, path.stat().st_size, 'bytes')