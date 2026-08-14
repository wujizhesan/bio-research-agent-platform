"""受体准备：PDB -> 干净蛋白 PDB（Vina v1.2 可直接接受的受体）。

关键经验（实测验证）：
- AutoDock Vina v1.2 能直接接受普通 PDB 受体,不需要转成 PDBQT
- 反而用 openbabel 转 PDBQT 会带 ROOT/TORS 标记,Vina 误判为柔性配体报错
- 正确做法：从 PDB 提取蛋白部分(去掉共晶配体/水/金属,它们不该参与受体)
"""
import sys
from pathlib import Path
from Bio.PDB import PDBParser, Select
from Bio.PDB.PDBIO import PDBIO


def extract_protein(pdb_path, out_pdb):
    """只保留蛋白(标准氨基酸),去掉配体/水/离子,存为 Vina 可接受的干净 PDB。"""
    p = PDBParser(QUIET=True)
    s = p.get_structure('rec', str(pdb_path))

    class ProtOnly(Select):
        def accept_residue(self, res):
            # 只保留标准氨基酸残基(残基名 3 字母, 非 HETATM)
            if res.id[0] != ' ':
                return False
            return True

    io = PDBIO()
    io.set_structure(s)
    io.save(str(out_pdb), select=ProtOnly())
    return out_pdb


def prepare(pdb_path, out_dir='output'):
    pdb_path = Path(pdb_path)
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    receptor_pdb = out_dir / 'receptor.pdb'
    extract_protein(str(pdb_path), str(receptor_pdb))
    print(f'[prepare_receptor] Vina 直接可用的受体 PDB: {receptor_pdb}')
    return receptor_pdb


if __name__ == '__main__':
    pdb = sys.argv[1] if len(sys.argv) > 1 else 'data/4hjo.pdb'
    prepare(pdb)
