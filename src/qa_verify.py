"""虚拟筛选结果的自动验收。"""
import csv
import json
import sys
from pathlib import Path

try:
    from .config_loader import PROJECT_ROOT, latest_run_dir
except ImportError:
    from config_loader import PROJECT_ROOT, latest_run_dir


PASS = 'PASS'
FAIL = 'FAIL'
checks = []


def record(name, ok, detail=''):
    checks.append((bool(ok), name, detail))
    print(f'  [{PASS if ok else FAIL}] {name} {detail}')


def _finish():
    fails = [item for item in checks if not item[0]]
    print(f'\n=== 结果: {len(checks) - len(fails)}/{len(checks)} 通过 ===')
    if fails:
        print('未通过:')
        for _, name, detail in fails:
            print(f'  - {name} {detail}')
    return 0 if not fails else 1


def _check_manifest(manifest_path, result_count):
    path = Path(manifest_path)
    if not path.exists():
        print(f'  [info] 未找到运行清单，跳过: {path}')
        return
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
        status = manifest.get('status')
        total = int(manifest.get('ligand_count', -1))
        successful = int(manifest.get('successful_ligands', -1))
        failed = manifest.get('failed_ligands', [])
        ok = status in {'completed', 'completed_with_failures'}
        ok = ok and total >= 0 and successful >= 0 and isinstance(failed, list)
        ok = ok and successful + len(failed) == total and result_count <= successful
        record('运行清单一致性', ok, f'status={status}, result={result_count}/{total}')
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        record('运行清单一致性', False, str(exc))


def main(csv_path=None, report_path=None, manifest_path=None):
    checks.clear()
    if csv_path is None or report_path is None:
        output_dir = latest_run_dir(PROJECT_ROOT / 'output')
        csv_path = csv_path or output_dir / 'top_hits.csv'
        report_path = report_path or output_dir / 'report.md'
    print('=== QA 验收 ===')

    report = Path(report_path)
    record('报告文件存在且非空', report.exists() and report.stat().st_size > 0, str(report))

    result = Path(csv_path)
    if not result.exists():
        record('结果 CSV 存在', False, str(result))
        return _finish()

    try:
        with result.open(encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        record('结果 CSV 可读取', False, str(exc))
        return _finish()

    record(f'结果非空({len(rows)} 行)', len(rows) > 0)
    data = []
    for row in rows:
        try:
            data.append({
                'mol': row['mol_name'],
                'tag': row['tag'].lower(),
                'aff': float(row['affinity']),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    record('打分可解析', len(data) == len(rows))

    if not data:
        return _finish()

    affinities = [item['aff'] for item in data]
    reasonable = all(-15 <= value <= 0 for value in affinities)
    record('打分在合理范围[-15,0]', reasonable, f'range=[{min(affinities):.2f},{max(affinities):.2f}]')

    actives = [item for item in data if item['tag'] == 'active']
    inactives = [item for item in data if item['tag'] == 'inactive']
    if actives and inactives:
        sorted_all = sorted(data, key=lambda item: item['aff'])
        total = len(data)
        n_active = len(actives)
        k = min(total, max(3, int(total * 0.3)))
        top_k = sorted_all[:k]
        active_in_top = sum(item['tag'] == 'active' for item in top_k)
        random_expected = k * n_active / total
        enrichment = active_in_top / random_expected if random_expected else 0
        top1_is_active = sorted_all[0]['tag'] == 'active'
        enrich_ok = enrichment >= 1.0 and top1_is_active
        record(
            '活性富集(Top 最强hit是活性,富集倍数>=1)',
            enrich_ok,
            f'top1_active={top1_is_active}, top{k}活性={active_in_top}/{n_active}, 富集x{enrichment:.2f}',
        )
        best_active = min(item['aff'] for item in actives)
        best_inactive = min(item['aff'] for item in inactives)
        print(f'  [info] 最强活性 {best_active:.2f} vs 最强非活性 {best_inactive:.2f}')
    elif actives or inactives:
        record('存在正负对照', False, '缺少某一类对照')
    else:
        record('存在正负对照', False, '无标签数据')

    if manifest_path is None:
        manifest_path = result.with_name('run_manifest.json')
    _check_manifest(manifest_path, len(data))

    print('\n结果概览(升序):')
    for index, item in enumerate(sorted(data, key=lambda value: value['aff'])):
        print(f'  top{index + 1}: {item["mol"]} [{item["tag"]}] {item["aff"]:.2f}')
    return _finish()


if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else None
    report_path = sys.argv[2] if len(sys.argv) > 2 else None
    manifest_path = sys.argv[3] if len(sys.argv) > 3 else None
    raise SystemExit(main(csv_path, report_path, manifest_path))
