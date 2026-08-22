"""生成可追溯的虚拟筛选 Markdown 报告。"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    from .config_loader import PROJECT_ROOT, latest_run_dir
except ImportError:
    from config_loader import PROJECT_ROOT, latest_run_dir

REQUIRED_COLUMNS = {'mol_name', 'tag', 'affinity'}


def _load_json(path):
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def _fmt(value):
    return 'n/a' if value is None else f'{float(value):.2f}'


def _range_text(series):
    return 'n/a' if series.empty else f'{series.max():.2f} ~ {series.min():.2f}'


def _table_text(value):
    return str(value).replace('|', '\\|')


def generate_report(
    csv_path=None,
    out_md=None,
    receptor='EGFR',
    manifest_path=None,
    ml_metrics_path=None,
):
    if csv_path is None or out_md is None:
        output_dir = latest_run_dir(PROJECT_ROOT / 'output')
        csv_path = csv_path or output_dir / 'top_hits.csv'
        out_md = out_md or output_dir / 'report.md'
    csv_path = Path(csv_path)
    out_md = Path(out_md)
    if manifest_path is None:
        manifest_path = out_md.with_name('run_manifest.json')
    if ml_metrics_path is None:
        ml_metrics_path = out_md.with_name('ml_metrics.json')

    if not csv_path.exists():
        print(f'[report] 结果文件不存在: {csv_path}, 请先运行筛选')
        return None

    try:
        df = pd.read_csv(csv_path)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        print(f'[report] 结果文件无法读取: {exc}')
        return None

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        print(f'[report] 缺少必要字段: {", ".join(sorted(missing))}')
        return None

    df = df.copy()
    df['affinity'] = pd.to_numeric(df['affinity'], errors='coerce')
    df['tag'] = df['tag'].astype(str).str.lower()
    df = df.dropna(subset=['affinity']).sort_values('affinity').reset_index(drop=True)
    actives = df[df['tag'] == 'active']
    inactives = df[df['tag'] == 'inactive']
    manifest = _load_json(manifest_path)
    ml_metrics = _load_json(ml_metrics_path)

    lines = [
        f'# {receptor} 靶点虚拟筛选报告',
        '',
        f'- 生成时间: {datetime.now().astimezone().isoformat(timespec="seconds")}',
        f'- 分子库规模: {len(df)} 个（活性对照 {len(actives)} + 非活性对照 {len(inactives)}）',
        '- 对接引擎: AutoDock Vina v1.2.7',
        '- 打分单位: kcal/mol，数值越负表示预测结合越强',
        '',
    ]

    if manifest:
        lines.extend([
            '## 运行摘要',
            '',
            f'- 运行状态: `{manifest.get("status", "unknown")}`',
            f'- 配体总数: {manifest.get("ligand_count", len(df))}',
            f'- 成功对接: {manifest.get("successful_ligands", len(df))}',
            f'- 失败配体: {len(manifest.get("failed_ligands", []))}',
            f'- 是否恢复运行: {"是" if manifest.get("resumed_from_previous") else "否"}',
            f'- Vina 随机种子: `{manifest.get("signature", {}).get("seed", "n/a")}`',
            '',
        ])

    if ml_metrics:
        lines.extend([
            '## ML 评估摘要',
            '',
            f'- 评估方式: `{ml_metrics.get("evaluation", "unknown")}`',
            f'- 样本数: {ml_metrics.get("n_samples", "n/a")}',
            f'- 训练集/测试集: {ml_metrics.get("n_train", "n/a")} / {ml_metrics.get("n_test", "n/a")}',
            f'- 切分策略: `{ml_metrics.get("split_strategy", "n/a")}`',
            f'- 评估数据源: {ml_metrics.get("data_source", "n/a")}',
            f'- Train/Test Scaffold 重叠数: {ml_metrics.get("scaffold_overlap_count", "n/a")}',
            f'- Class imbalance ratio (majority/minority): {_fmt(ml_metrics.get("class_imbalance_ratio"))}',
            f'- Document overlap count: {ml_metrics.get("document_overlap_count", "n/a")}',
            f'- LOO 基线 Balanced accuracy: {_fmt(ml_metrics.get("loo_balanced_accuracy"))}',
            f'- Decision threshold: {_fmt(ml_metrics.get("decision_threshold"))} ({ml_metrics.get("threshold_strategy", "n/a")})',
            f'- Repeated scaffold Balanced accuracy (n={ml_metrics.get("repeated_scaffold_n", "n/a")}): {_fmt(ml_metrics.get("repeated_scaffold_balanced_accuracy_mean"))} +/- {_fmt(ml_metrics.get("repeated_scaffold_balanced_accuracy_std"))}',
            f'- Repeated scaffold ROC-AUC (n={ml_metrics.get("repeated_scaffold_n", "n/a")}): {_fmt(ml_metrics.get("repeated_scaffold_roc_auc_mean"))} +/- {_fmt(ml_metrics.get("repeated_scaffold_roc_auc_std"))}',
            f'- Balanced accuracy: {_fmt(ml_metrics.get("balanced_accuracy"))}',
            f'- ROC-AUC: {_fmt(ml_metrics.get("roc_auc"))}',
            f'- PR-AUC: {_fmt(ml_metrics.get("pr_auc"))}',
            f'- PR-AUC baseline (active prevalence): {_fmt(ml_metrics.get("pr_auc_baseline"))}',
            f'- PR-AUC lift vs baseline: {_fmt(ml_metrics.get("pr_auc_lift"))}x',
            f'- Top 30% 富集: {_fmt(ml_metrics.get("top30_enrichment"))}x',
        ])
        if ml_metrics.get('warning'):
            lines.append(f'- 说明: {ml_metrics["warning"]}')
        lines.append('')

    lines.extend(['## 结果（按结合能升序）', '', '| 排名 | 分子 | 类型 | 对接打分 |', '|---|---|---|---|'])
    for index, row in df.iterrows():
        tag_text = '活性对照' if row['tag'] == 'active' else '非活性对照' if row['tag'] == 'inactive' else row['tag']
        lines.append(f'| {index + 1} | **{_table_text(row["mol_name"])}** | {tag_text} | `{row["affinity"]:.2f}` |')
    if df.empty:
        lines.append('| - | 未找到有效打分结果 | - | - |')
    lines.append('')

    lines.extend(['## 正负对照区分度分析', ''])
    if actives.empty or inactives.empty:
        missing_group = '活性' if actives.empty else '非活性'
        lines.append(f'- 无法计算区分度：缺少{missing_group}对照分子。')
    else:
        active_weakest = actives['affinity'].max()
        inactive_strongest = inactives['affinity'].min()
        well_separated = active_weakest < inactive_strongest
        lines.append(f'- 活性分子打分范围: {_range_text(actives["affinity"])}')
        lines.append(f'- 非活性分子打分范围: {_range_text(inactives["affinity"])}')
        if well_separated:
            lines.append('- **方法区分有效**：活性对照整体强于非活性对照，且两组没有打分重叠。')
        else:
            lines.append('- 活性与非活性对照存在打分重叠，建议结合 ML、相互作用分析或重复对接进一步筛选。')

    lines.extend(['', '## 结论', ''])
    if df.empty:
        lines.append('本次运行未产出可用对接打分，暂不能形成命中结论。')
    elif not actives.empty:
        top = actives.sort_values('affinity').iloc[0]
        lines.append(f'当前最强活性命中为 **{top["mol_name"]}**，对接打分 `{top["affinity"]:.2f} kcal/mol`。')
    else:
        lines.append('当前结果中没有活性对照分子，不能确认最强命中是否属于目标活性类别。')

    text = '\n'.join(lines) + '\n'
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(text, encoding='utf-8')
    print(f'[report] 报告已生成: {out_md}')
    return text


if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else None
    generate_report(csv_path)
