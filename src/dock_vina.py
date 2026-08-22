"""AutoDock Vina 对接：调 vina_win.exe 子进程批量对接并解析打分。

原理（CADD 基础知识）：
- Vina 把配体放到受体结合口袋(由 center+size 定义的盒子)里
- 搜索配体构象/朝向,用打分函数算出结合自由能(affinity,kcal/mol,越负越好)
- exhaustiveness=搜索充分度,越大越准但越慢
"""
import subprocess
import re
from pathlib import Path
import pandas as pd


VINA_EXE = Path(__file__).resolve().parent.parent / 'tools' / 'vina_1.2.7_win.exe'
# 对接盒子尺寸(Å)
BOX_SIZE = (18.0, 18.0, 18.0)


def parse_affinity(text):
    affinities = []
    for line in text.splitlines():
        match = re.match(r'\s*\d+\s+(-?\d+\.\d+)', line)
        if match:
            affinities.append(float(match.group(1)))
    return min(affinities) if affinities else None


def receptor_center_from_ligand(pdb_with_ligand, ligand_resname='AQ4'):
    """从含共晶配体的原始 PDB 提取指定配体(如 AQ4)的坐标,计算结合口袋中心(质心)。
    这是"真算出来的"盒子中心: 用已知结合 EGFR 的共晶配体定结合口袋。
    ⚠️ 必须传"含配体的原始 PDB"(如 data/4hjo.pdb),不是去配体后的受体 PDB。
    """
    from Bio.PDB import PDBParser
    import numpy as np
    p = PDBParser(QUIET=True)
    s = p.get_structure('rec', str(pdb_with_ligand))
    coords = []
    for model in s:
        for chain in model:
            for res in chain:
                if res.id[0] != ' ' and res.resname == ligand_resname:
                    for atom in res:
                        coords.append(atom.coord)
    if coords:
        c = np.mean(coords, axis=0)
        return (float(c[0]), float(c[1]), float(c[2]))
    raise ValueError(f'在 {pdb_with_ligand} 中未找到共晶配体 {ligand_resname}, '
                     f'请确认传入了含配体的原始 PDB(而非去配体后的受体)')


def dock_one(ligand_pdbqt, receptor_pdb, out_prefix, exhaustiveness=8,
             center=None, size=None, source_pdb=None, vina_exe=None, resume=False, seed=None):
    """单个配体对接,返回 {'name','affinity'} 或 None(失败)。
    受体用 Vina 可直接接受的干净 PDB。
    source_pdb 传"含共晶配体的原始 PDB"(如 data/4hjo.pdb),用于从 AQ4 算盒子中心。"""
    receptor_pdb = Path(receptor_pdb)
    # 未显式给盒子中心时,从含共晶配体的原始 PDB 计算口袋中心(真算而非硬编码)
    if center is None:
        center = receptor_center_from_ligand(source_pdb or receptor_pdb, 'AQ4')
    size = size or BOX_SIZE
    ligand_pdbqt = Path(ligand_pdbqt)
    out_prefix = Path(out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    # Vina 要求输出 pdbqt 但会附加协调文件
    out_pdbqt = out_prefix.with_suffix('.out.pdbqt')
    log_txt = out_prefix.with_suffix('.log.txt')

    if resume and out_pdbqt.exists() and log_txt.exists():
        affinity = parse_affinity(log_txt.read_text(encoding='utf-8', errors='replace'))
        if affinity is not None:
            print(f'  [dock] {ligand_pdbqt.name} 使用已有结果 {affinity:.2f}')
            return {'name': ligand_pdbqt.stem, 'affinity': affinity, 'resumed': True}

    exe = Path(vina_exe) if vina_exe else VINA_EXE
    cmd = [
        str(exe), '--receptor', str(receptor_pdb),
        '--ligand', str(ligand_pdbqt),
        '--center_x', str(center[0]), '--center_y', str(center[1]), '--center_z', str(center[2]),
        '--size_x', str(size[0]), '--size_y', str(size[1]), '--size_z', str(size[2]),
        '--exhaustiveness', str(exhaustiveness),
        '--out', str(out_pdbqt),
    ]
    if seed is not None:
        cmd.extend(['--seed', str(seed)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        log_txt.write_text(r.stdout + '\n[stderr]\n' + r.stderr, encoding='utf-8')
        if r.returncode != 0:
            print(f'  [dock] {ligand_pdbqt.name} 失败(returncode={r.returncode}): {r.stderr[-300:]}')
            return None
        affinity = parse_affinity(r.stdout)
        if affinity is None:
            print(f'  [dock] {ligand_pdbqt.name} 未找到打分, stderr: {r.stderr[-300:]}')
            return None
        return {'name': ligand_pdbqt.stem, 'affinity': affinity}
    except subprocess.TimeoutExpired:
        print(f'  [dock] {ligand_pdbqt.name} 超时')
        return None
    except Exception as e:
        print(f'  [dock] {ligand_pdbqt.name} 错误: {e}')
        return None


def dock_batch(ligand_pdbqts, receptor_pdb, out_dir, exhaustiveness=8, source_pdb=None,
              center=None, size=None, vina_exe=None, resume=False, seed=None):
    """批量对接,返回 DataFrame(按 affinity 升序)。
    source_pdb: 含共晶配体的原始 PDB,用于从 AQ4 算盒子中心。"""
    results = []
    for i, lig in enumerate(ligand_pdbqts):
        prefix = Path(out_dir) / f'dock_{i}'
        res = dock_one(lig, receptor_pdb, prefix, exhaustiveness, center=center, size=size,
                       source_pdb=source_pdb, vina_exe=vina_exe, resume=resume, seed=seed)
        if res:
            results.append(res)
    return pd.DataFrame(results).sort_values('affinity') if results else pd.DataFrame()


if __name__ == '__main__':
    import sys
    # 单配体测试：用干净 PDB 受体
    out_dir = Path('output')
    r = dock_one('data/pdbqt_aq4/ligand_0.pdbqt', 'output/receptor.pdb', out_dir/'test_aq4')
    print('[dock] AQ4 对接结果:', r)
