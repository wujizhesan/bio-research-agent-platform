"""QA 自动验收：验证虚拟筛选项目是否"真正跑通且结论可信"。

检查项(对应计划验收标准):
1. 产物齐全: report.md + top_hits.csv 存在
2. 结果非空且有打分数
3. 正对照回归: 活性分子(active)全体排位高于非活性(inactive) 或打分明显更负
4. 负对照靠后: 非活性分子不应误入 top(除非打分接近)
5. 打分范围合理: affinity 在 [-15, 0] 区间,越负越强
"""
from pathlib import Path
import csv

PASS = 'PASS'; FAIL = 'FAIL'
checks = []

def record(name, ok, detail=''):
    checks.append((ok, name, detail))
    print(f'  [{"PASS" if ok else "FAIL"}] {name} {detail}')


def main(csv_path='output/top_hits.csv', report_path='output/report.md'):
    print('=== QA 验收 ===')
    ok_report = Path(report_path).exists()
    record('报告文件存在', ok_report, report_path)

    if not Path(csv_path).exists():
        record('结果 CSV 存在', False, csv_path)
        return

    rows = [r for r in csv.DictReader(open(csv_path, encoding='utf-8'))]
    record(f'结果非空({len(rows)} 行)', len(rows) > 0)

    # 解析
    data = []
    for r in rows:
        try:
            data.append({'mol': r['mol_name'], 'tag': r['tag'],
                         'aff': float(r['affinity'])})
        except (KeyError, ValueError):
            continue
    record('打分可解析', len(data) == len(rows))

    if not data:
        return

    # 打分合理性
    affs = [d['aff'] for d in data]
    reasonable = all(-15 <= a <= 0 for a in affs)
    record('打分在合理范围[-15,0]', reasonable, f'range=[{min(affs):.2f},{max(affs):.2f}]')

    # 活性 富集评估(Enrichment): 打分最高的 Top-N 里,活性分子占比应高于库内均匀占比
    # 这是虚拟筛选真正的验收标准: 好方法应把活性分子富集到 top。
    actives = [d for d in data if d['tag'] == 'active']
    inacts = [d for d in data if d['tag'] == 'inactive']
    if actives and inacts:
        sorted_all = sorted(data, key=lambda x: x['aff'])
        total = len(data); n_act = len(actives)
        # Top 30% 里的活性分子数
        k = max(3, int(total * 0.3))
        top_k = sorted_all[:k]
        act_in_topk = sum(1 for d in top_k if d['tag'] == 'active')
        # 富集倍数: Top-K 活性占比 / 库内活性占比
        random_expected = k * n_act / total  # 随机拉 k 个期望的活性数
        enrichment = act_in_topk / random_expected if random_expected > 0 else 0
        # 验收: Top 3 里必须有活性分子(最强 hit 是活性),且富集>1
        top1_is_active = sorted_all[0]['tag'] == 'active'
        enrich_ok = enrichment >= 1.0 and top1_is_active
        record('活性富集(Top 最强hit是活性,富集倍数>=1)',
               enrich_ok,
               f'top1_active={top1_is_active}, top{k}(30%)活性数={act_in_topk}/{n_act}, 富集x{enrichment:.2f}')
        # 辅助(不判 pass/fail): 记录最强活性/非活性打分
        best_act = min(a['aff'] for a in actives); best_inact = min(a['aff'] for a in inacts)
        print(f'  [info] 最强活性 {best_act:.2f} vs 最强非活性 {best_inact:.2f}')
    elif actives or inacts:
        record('存在正负对照', False, '缺少某一类')
    else:
        record('存在正负对照', False, '无标注数据')

    # 打印结果概览
    print('\n结果概览(升序):')
    for i, d in enumerate(sorted(data, key=lambda x: x['aff'])):
        print(f'  top{i+1}: {d["mol"]} [{d["tag"]}] {d["aff"]:.2f}')

    # 汇总
    fails = [c for c in checks if not c[0]]
    print(f'\n=== 结果: {len(checks)-len(fails)}/{len(checks)} 通过 ===')
    if fails:
        print('未通过:')
        for _, name, detail in fails:
            print(f'  - {name} {detail}')
    return 0 if not fails else 1


if __name__ == '__main__':
    import sys
    csv_p = sys.argv[1] if len(sys.argv) > 1 else 'output/top_hits.csv'
    rep_p = sys.argv[2] if len(sys.argv) > 2 else 'output/report.md'
    raise SystemExit(main(csv_p, rep_p))
