"""虚拟筛选结果报告生成(不依赖 LLM,离线可用)。

把 top_hits.csv 转成可读的 markdown 报告:
- 结果表(按打分排序)
- 正负对照区分度分析
- 关键命中的解读

注意: 这是"离线可运行的报告层"。若配置了可用的 LLM key,
agent.py 可在此基础上追加"自然语言生成式摘要",实现完整的 LLM Agent。
"""
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))


def generate_report(csv_path='output/top_hits.csv', out_md='output/report.md', receptor='EGFR'):
    csv_path = Path(csv_path)
    if not csv_path.exists():
        print(f'[report] 结果文件不存在: {csv_path},请先运行筛选')
        return None
    df = pd.read_csv(csv_path)

    actives = df[df['tag'] == 'active'].sort_values('affinity')
    inacts = df[df['tag'] == 'inactive'].sort_values('affinity')

    # 区分度指标:活性分子最低打分 vs 非活性分子最高打分
    # 若活性整体打分更低(更强)且无重叠,说明方法区分有效
    active_best = actives['affinity'].max()   # 活性里最弱的
    inactive_best = inacts['affinity'].min()  # 非活性里最强的
    well_separated = active_best < inactive_best

    lines = []
    lines.append(f'# {receptor} 靶点 虚拟筛选报告')
    lines.append('')
    lines.append(f'- 分子库规模: {len(df)} 个(活性对照 {len(actives)} + 非活性对照 {len(inacts)})')
    lines.append(f'- 对接引擎: AutoDock Vina v1.2.7(Apache-2.0)')
    lines.append(f'- 打分单位: kcal/mol,越负结合越强')
    lines.append('')
    lines.append('## 结果(按结合能升序)')
    lines.append('')
    lines.append('| 排名 | 分子 | 类型 | 对接打分 |')
    lines.append('|---|---|---|---|')
    for i, r in df.iterrows():
        tag_zh = '活性(正对照)' if r['tag'] == 'active' else '非活性(负对照)'
        lines.append(f"| {i+1} | **{r['mol_name']}** | {tag_zh} | `{r['affinity']:.2f}` |")
    lines.append('')
    lines.append('## 正负对照区分度分析')
    lines.append('')
    lines.append(f'- 活性分子打分范围: {actives["affinity"].max():.2f} ~ {actives["affinity"].min():.2f}')
    lines.append(f'- 非活性分子打分范围: {inacts["affinity"].max():.2f} ~ {inacts["affinity"].min():.2f}')
    if well_separated:
        lines.append(f'- **方法区分有效**:活性分子整体结合更强(更负),与非活性无打分重叠。')
        lines.append(f'  说明该虚拟筛选方法能有效筛出 {receptor} 的潜在活性分子。')
    else:
        lines.append(f'- 活性和非活性打分有重叠,需结合 ML 打分或聚类进一步优化。')
    lines.append('')
    lines.append('## 结论')
    lines.append('')
    lines.append(f'本流水线完成了从受体准备→分子库→对接→打分的完整 {receptor} 虚拟筛选。')
    if len(actives):
        top = actives.iloc[0]
        lines.append(f'最强 hit: **{top["mol_name"]}**,对接打分 {top["affinity"]:.2f} kcal/mol。')
    lines.append('')
    lines.append(f'生成时间: 2026 虚拟筛选 demo')

    txt = '\n'.join(lines)
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(txt, encoding='utf-8')
    print(f'[report] 报告已生成: {out_md}')
    return txt


if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else 'output/top_hits.csv'
    generate_report(p)
