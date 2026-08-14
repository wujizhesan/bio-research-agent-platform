"""可视化：把虚拟筛选结果画成图，供 README / 简历展示。

生成:
  1. output/vs_plot.png  —— 对接打分数值柱状图(活性 vs 非活性用不同色)
  2. output/top_mol.png  —— Top hit 分子 2D 结构图(RDKit)
"""
import sys
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # 无界面后端,适合脚本
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))


def plot_scores(csv_path='output/top_hits.csv', out_png='output/vs_plot.png'):
    """画对接打分柱状图。"""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        print('[viz] 无结果文件,请先筛选')
        return None
    df = pd.read_csv(csv_path)
    df = df.sort_values('affinity')
    colors = ['#c0392b' if t == 'active' else '#7f8c8d' for t in df['tag']]
    names = df['mol_name']

    plt.figure(figsize=(10, max(4, len(df) * 0.5)))
    bars = plt.barh(names, df['affinity'], color=colors)
    plt.axvline(0, color='black', linewidth=0.8)
    plt.xlabel('Docking score (kcal/mol, more negative = stronger)')
    plt.title('EGFR Virtual Screening: Docking Scores (sorted)')
    for b, v in zip(bars, df['affinity']):
        plt.text(v, b.get_y() + b.get_height()/2, f' {v:.2f}', va='center', fontsize=9)
    # 图例
    from matplotlib.patches import Patch
    plt.legend(handles=[
        Patch(color='#c0392b', label='Active (positive control)'),
        Patch(color='#7f8c8d', label='Inactive (negative control)'),
    ])
    plt.tight_layout()
    out_png = Path(out_png); out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(out_png), dpi=150, bbox_inches='tight')
    plt.close()
    print(f'[viz] 打分图: {out_png}')
    return out_png


def plot_top_mol(csv_path='output/top_hits.csv', out_png='output/top_mol.png', n=3):
    """画 Top hit 分子的 2D 结构图。"""
    from rdkit import Chem
    from rdkit.Chem import Draw
    from library_data import build_screening_library
    lib = build_screening_library()

    csv_path = Path(csv_path)
    if not csv_path.exists():
        print('[viz] 无结果文件')
        return None
    df = pd.read_csv(csv_path).sort_values('affinity').head(n)
    mols = [Chem.MolFromSmiles(lib.get(r['mol_name'], '')) for _, r in df.iterrows()]
    mols = [m for m in mols if m]
    if not mols:
        print('[viz] 无可绘制的分子')
        return None
    img = Draw.MolsToGridImage(mols, molsPerRow=n, subImgSize=(400, 400),
                               legends=list(df['mol_name']))
    out_png = Path(out_png); out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_png))
    print(f'[viz] Top hit 结构图: {out_png}')
    return out_png


if __name__ == '__main__':
    from library_data import build_screening_library
    lib = build_screening_library()
    plot_scores()
    plot_top_mol()
    print('\n[viz] 完成: 检查 output/vs_plot.png 和 output/top_mol.png')
