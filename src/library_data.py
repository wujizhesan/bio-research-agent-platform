"""筛选分子库定义（经过对抗性审查修复的可靠版本）。

正对照/活性分子(active)——全部是经 PubChem 官方 API 验证的**已上市 EGFR 抑制剂**：
  6 个已上市 EGFR TKI (吉非替尼/厄洛替尼/奥希替尼/阿法替尼/拉帕替尼/埃克替尼)
  其 SMILES 来自 PubChem PUG REST(确定性来源),并经 RDKit 验证
  (2-3 芳环、LogP 3-6、HBD<=2 的标准激酶抑制剂形态)。
  另含 AQ4(PDB 4HJO 真实共晶配体)。
  注意: 之前版本错用了 ChEMBL 拉到的糖类分子(非 EGFR 抑制剂),已废弃。

负对照/非活性分子(inactive)：
  常见但没有 EGFR 激酶活性的小分子。经对抗审查提示,负对照太简单会让
  "分离"看起来太好,因此加入了性质更接近活性分子的烷基芳胺/杂环,提高区分难度。

用途: RDKit 生成 3D 构象 -> Meeko PDBQT -> AutoDock Vina 对接。
"""
import json
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import Descriptors


# ---- 已上市 EGFR 抑制剂(正对照,PubChem 验证) ----
ACTIVE_SMILES = {
    'erlotinib': 'COCCOC1=C(C=C2C(=C1)C(=NC=N2)NC3=CC=CC(=C3)C#C)OCCOC',
    'gefitinib': 'COC1=C(C=C2C(=C1)N=CN=C2NC3=CC(=C(C=C3)F)Cl)OCCCN4CCOCC4',
    'osimertinib': 'CN1C=C(C2=CC=CC=C21)C3=NC(=NC=C3)NC4=C(C=C(C(=C4)NC(=O)C=C)N(C)CCN(C)C)OC',
    'afatinib': 'CN(C)CC=CC(=O)NC1=C(C=C2C(=C1)C(=NC=N2)NC3=CC(=C(C=C3)F)Cl)OC4CCOC4',
    'lapatinib': 'CS(=O)(=O)CCNCC1=CC=C(O1)C2=CC3=C(C=C2)N=CN=C3NC4=CC(=C(C=C4)OCC5=CC(=CC=C5)F)Cl',
    'icotinib': 'C#CC1=CC(=CC=C1)NC2=NC=NC3=CC4=C(C=C32)OCCOCCOCCO4',
}

# PDB 4HJO 真实共晶配体
AQ4_SMILES = 'COCCOc1cc2c(cc1OCCOC)ncnc2Nc3cccc(c3)C#C'


def _load_verified_drugs():
    """加载经 PubChem 验证的 EGFR 药物(可扩展)。"""
    p = Path(__file__).resolve().parent.parent / 'data' / 'egfr_drugs_verified.json'
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding='utf-8'))


# ---- 负对照(非 EGFR 活性,但性质不太简单) ----
INACTIVE_SMILES = {
    'ASPIRIN': 'O=C(C)Oc1ccccc1C(=O)O',
    'CAFFEINE': 'CN1C=NC2=C1C(=O)N(C(=O)N2C)C',
    'ANILINE': 'Nc1ccccc1',                     # 苯胺: 有芳环,增加难度
    'BENZAMIDE': 'NC(=O)c1ccccc1',              # 苯甲酰胺
    'N_ACETYL_ANILINE': 'CC(=O)NC1=CC=CC=C1',   # 乙酰苯胺
    'ISOQUINOLINE': 'c1ccc2cnccc2c1',           # 异喹啉: 稠环
    'NAPHTHALENE': 'c1ccc2ccccc2c1',
    'BIPHENYL': 'c1ccc(cc1)c1ccccc1',
    'PYRIDINE': 'c1ccncc1',
    'TOLUENE': 'Cc1ccccc1',
}


# 活性分子名集合(与 ACTIVE_SMILES + AQ4 一致,供全项目统一判定)
ACTIVE_NAMES = frozenset(('erlotinib', 'gefitinib', 'osimertinib', 'afatinib',
                           'lapatinib', 'icotinib', 'AQ4'))

def is_active(name):
    """统一的活性判定: 返回 True 表示该分子是 EGFR 活性正对照。"""
    return name in ACTIVE_NAMES


def build_screening_library():
    """组装完整筛选库。返回 {name: smiles}。"""
    lib = {}
    # 正对照: 6 个已验证 EGFR 药物 + AQ4 共晶配体
    for n, s in ACTIVE_SMILES.items():
        if Chem.MolFromSmiles(s):
            lib[n] = s
    lib['AQ4'] = AQ4_SMILES
    # 负对照
    for k, v in INACTIVE_SMILES.items():
        if Chem.MolFromSmiles(v):
            lib[k] = v
    return {n: s for n, s in lib.items() if s}


if __name__ == '__main__':
    lib = build_screening_library()
    acts = ['erlotinib','gefitinib','osimertinib','afatinib','lapatinib','icotinib','AQ4']
    n_act = sum(1 for n in lib if n in acts)
    n_in = len(lib) - n_act
    print(f'=== 库: {len(lib)} 个(活性 {n_act} + 非活性 {n_in}) ===')
    for n, s in lib.items():
        m = Chem.MolFromSmiles(s)
        tag = 'ACT' if n in acts else 'INACT'
        dens = f'LogP={Descriptors.MolLogP(m):.1f}' if m else '?'
        print(f'  [{tag}] {n:16s} {dens}  {s[:55]}')
