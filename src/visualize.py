"""生成虚拟筛选结果图表和Top分子结构图。"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

try:
    from .config_loader import PROJECT_ROOT, latest_run_dir
except ImportError:
    from config_loader import PROJECT_ROOT, latest_run_dir

sys.path.insert(0, str(Path(__file__).resolve().parent))

REQUIRED_COLUMNS = {'mol_name', 'tag', 'affinity'}


def _load_results(csv_path):
    path = Path(csv_path)
    if not path.exists():
        print(f'[viz] 结果文件不存在: {path}')
        return None
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        print(f'[viz] 结果文件无法读取: {exc}')
        return None
    if not REQUIRED_COLUMNS.issubset(df.columns):
        print(f'[viz] 缺少必要字段: {sorted(REQUIRED_COLUMNS - set(df.columns))}')
        return None
    df = df.copy()
    df['affinity'] = pd.to_numeric(df['affinity'], errors='coerce')
    df = df.dropna(subset=['affinity']).sort_values('affinity').reset_index(drop=True)
    if df.empty:
        print('[viz] 没有可绘制的有效打分')
        return None
    return df


def plot_scores(csv_path=None, out_png=None):
    if csv_path is None or out_png is None:
        output_dir = latest_run_dir(PROJECT_ROOT / 'output')
        csv_path = csv_path or output_dir / 'top_hits.csv'
        out_png = out_png or output_dir / 'vs_plot.png'
    df = _load_results(csv_path)
    if df is None:
        return None
    colors = ['#c0392b' if tag == 'active' else '#7f8c8d' for tag in df['tag']]
    plt.figure(figsize=(10, max(4, len(df) * 0.5)))
    bars = plt.barh(df['mol_name'], df['affinity'], color=colors)
    plt.axvline(0, color='black', linewidth=0.8)
    plt.xlabel('Docking score (kcal/mol, more negative = stronger)')
    plt.title('EGFR Virtual Screening: Docking Scores (sorted)')
    for bar, value in zip(bars, df['affinity']):
        plt.text(value, bar.get_y() + bar.get_height() / 2, f' {value:.2f}', va='center', fontsize=9)
    from matplotlib.patches import Patch
    plt.legend(handles=[
        Patch(color='#c0392b', label='Active (positive control)'),
        Patch(color='#7f8c8d', label='Inactive (negative control)'),
    ])
    plt.tight_layout()
    out_path = Path(out_png)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(out_path), dpi=150, bbox_inches='tight')
    plt.close()
    print(f'[viz] 打分图: {out_path}')
    return out_path


def plot_top_mol(csv_path=None, out_png=None, n=3, library=None):
    if csv_path is None or out_png is None:
        output_dir = latest_run_dir(PROJECT_ROOT / 'output')
        csv_path = csv_path or output_dir / 'top_hits.csv'
        out_png = out_png or output_dir / 'top_mol.png'
    df = _load_results(csv_path)
    if df is None:
        return None
    from rdkit import Chem
    from rdkit.Chem import Draw
    if library is None:
        from library_data import build_screening_library
        library = build_screening_library()
    pairs = []
    for _, row in df.head(n).iterrows():
        mol = Chem.MolFromSmiles(library.get(row['mol_name'], ''))
        if mol is not None:
            pairs.append((mol, str(row['mol_name'])))
    if not pairs:
        print('[viz] 没有可绘制的分子')
        return None
    mols, legends = zip(*pairs)
    image = Draw.MolsToGridImage(
        list(mols),
        molsPerRow=min(n, len(mols)),
        subImgSize=(400, 400),
        legends=list(legends),
    )
    out_path = Path(out_png)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out_path))
    print(f'[viz] Top hit 结构图: {out_path}')
    return out_path


if __name__ == '__main__':
    plot_scores()
    plot_top_mol()
    print('\n[viz] 完成: 检查 output/vs_plot.png 和 output/top_mol.png')
