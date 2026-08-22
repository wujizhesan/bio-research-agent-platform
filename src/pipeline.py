"""虚拟筛选主流水线：一条命令完成 受体→分子库→对接→打分报告。

用法:
    python -m src.pipeline                 # 用默认配置(data/4hjo.pdb + library_data)
    python -m src.pipeline --receptor x.pdb --out out_dir

流程:
  1. prepare_receptor : PDB 受体准备(去配体/水, Vina 可直接读的干净 PDB)
  2. build_library    : 分子库 SMILES -> 3D SDF -> PDBQT
  3. dock_vina        : 批量对接, 得每个分子的 affinity(kcal/mol)
  4. report           : 生成 top_hits.csv + report.md(打分排序,正负对照对比)

这是标准的"虚拟筛选(virtual screening)"工作流最小闭环。
"""
import argparse
import hashlib
import importlib.util
import json
import re
import platform
import sys
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import PROJECT_ROOT, load_config, resolve_path
from plugin_loader import CADD_BACKEND_CONTRACTS, load_contract, plugin_info, require_callable


def _now():
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _signature_path(path):
    path = Path(path)
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)

def _library_sha256(names, smiles):
    payload = json.dumps(list(zip(names, smiles)), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _runtime_signature():
    distributions = ('rdkit', 'meeko', 'scikit-learn', 'pandas', 'numpy', 'biopython', 'requests', 'pyyaml', 'matplotlib')
    packages = {}
    for distribution in distributions:
        try:
            packages[distribution] = version(distribution)
        except PackageNotFoundError:
            packages[distribution] = None
    return {
        'python': platform.python_version(),
        'platform': platform.platform(),
        'packages': packages,
    }

def _load_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def _validate_run_id(run_id):
    if run_id is None:
        return None
    value = str(run_id)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value):
        raise ValueError('run_id must contain only letters, numbers, dot, underscore, or hyphen')
    return value


def _run_fingerprint(receptor_path, vina_path, config_path, library_path, names, smiles,
                     external_dataset, exhaustiveness, seed, box_center, box_size):
    payload = {
        'receptor_sha256': _file_sha256(receptor_path),
        'vina_sha256': _file_sha256(vina_path),
        'config_sha256': _file_sha256(config_path),
        'library_module_sha256': _file_sha256(library_path),
        'library': list(zip(names, smiles)),
        'external_dataset_sha256': _file_sha256(external_dataset) if external_dataset else None,
        'exhaustiveness': exhaustiveness,
        'seed': seed,
        'box_center': box_center,
        'box_size': box_size,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _resolve_run_dir(base_dir, output_cfg, runtime_cfg, run_id, fingerprint):
    explicit_id = _validate_run_id(run_id)
    isolate = bool(output_cfg.get('isolate_runs', runtime_cfg.get('isolate_runs', False)) or explicit_id)
    if not isolate:
        return base_dir, None
    directory_name = explicit_id or fingerprint[:16]
    runs_dir = Path(output_cfg.get('runs_dir', 'runs'))
    if not runs_dir.is_absolute():
        runs_dir = base_dir / runs_dir
    return runs_dir / directory_name, directory_name


def _update_latest_run(base_dir, run_dir, run_id, status):
    relative_path = str(run_dir.relative_to(base_dir)).replace('\\', '/')
    _save_json(base_dir / 'latest_run.json', {
        'run_id': run_id,
        'path': relative_path,
        'status': status,
        'updated_at': _now(),
    })


def _load_runtime_dependencies():
    try:
        from prepare_receptor import prepare
        from build_library import build_library
        from dock_vina import dock_batch
    except ImportError as exc:
        package = getattr(exc, 'name', None) or str(exc)
        raise RuntimeError(
            f'缺少运行依赖 {package}，请先执行 python -m pip install -r requirements.txt'
        ) from exc
    return prepare, build_library, dock_batch

def _ligand_index(path):
    try:
        return int(Path(path).stem.rsplit('_', 1)[1])
    except (AttributeError, IndexError, ValueError):
        return None


def _map_ligand_names(pdbqts, names):
    mapping = {}
    for path in pdbqts:
        index = _ligand_index(path)
        if index is not None and 0 <= index < len(names):
            mapping[index] = names[index]
    return mapping

def _load_library_module(module_path):
    path = resolve_path(module_path)
    if not path.exists():
        raise FileNotFoundError(f'分子库模块不存在: {path}')
    spec = importlib.util.spec_from_file_location('_cadd_library_data', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'无法加载分子库模块: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, 'build_screening_library') or not hasattr(module, 'is_active'):
        raise AttributeError('分子库模块必须提供 build_screening_library 和 is_active')
    return module

def _load_backends(plugin_cfg):
    loaded = {
        contract.key: load_contract(contract, plugin_cfg.get(contract.key))
        for contract in CADD_BACKEND_CONTRACTS
    }
    return (
        require_callable(loaded['receptor_backend'], 'prepare'),
        require_callable(loaded['library_backend'], 'build_library'),
        require_callable(loaded['docking_backend'], 'dock_batch'),
        loaded['ml_backend'],
        require_callable(loaded['report_backend'], 'generate_report'),
    )


def check_environment(config_path=None):
    try:
        cfg = load_config(config_path)
        receptor_cfg = cfg.get('receptor', {})
        vina_cfg = cfg.get('vina', {})
        receptor_path = resolve_path(receptor_cfg.get('pdb_path', 'data/4hjo.pdb'))
        vina_path = resolve_path(vina_cfg.get('exe', 'tools/vina_1.2.7_win.exe'))
        library_path = resolve_path(cfg.get('library', {}).get('data_module', 'src/library_data.py'))
        checks = [
            ('receptor_file', receptor_path.exists(), receptor_path),
            ('vina_executable', vina_path.exists(), vina_path),
            ('library_module', library_path.exists(), library_path),
        ]
        center = receptor_cfg.get('box_center')
        size = receptor_cfg.get('box_size')
        checks.append(('box_center', isinstance(center, list) and len(center) == 3, center))
        checks.append((
            'box_size',
            isinstance(size, list) and len(size) == 3 and all(float(value) > 0 for value in size),
            size,
        ))
        external_value = cfg.get('ml', {}).get('external_dataset')
        if external_value:
            external_path = resolve_path(external_value)
            checks.append(('external_dataset', external_path.exists(), external_path))
        plugin_specs = cfg.get('plugins', {})
        for contract in CADD_BACKEND_CONTRACTS:
            spec = plugin_specs.get(contract.key)
            if spec:
                try:
                    plugin = load_contract(contract, spec)
                    checks.append((
                        f'plugin:{contract.key}',
                        True,
                        plugin_info(plugin, contract, spec),
                    ))
                except (ImportError, AttributeError, TypeError, ValueError) as exc:
                    checks.append((f'plugin:{contract.key}', False, str(exc)))
    except (FileNotFoundError, TypeError, ValueError, OSError) as exc:
        print(f'[check] invalid configuration: {exc}')
        return 1

    required_modules = [
        ('RDKit', 'rdkit'),
        ('Meeko', 'meeko'),
        ('Biopython', 'Bio'),
        ('pandas', 'pandas'),
        ('NumPy', 'numpy'),
        ('scikit-learn', 'sklearn'),
        ('PyYAML', 'yaml'),
    ]
    optional_modules = [
        ('Requests', 'requests'),
        ('Matplotlib', 'matplotlib'),
        ('Streamlit', 'streamlit'),
    ]
    required_checks = {
        'receptor_file',
        'vina_executable',
        'library_module',
        'box_center',
        'box_size',
        'external_dataset',
    }
    failed = []
    for label, module in required_modules + optional_modules:
        checks.append((f'dependency:{label}', importlib.util.find_spec(module) is not None, module))
    for label, ok, detail in checks:
        state = 'OK' if ok else 'MISSING'
        print(f'  [{state}] {label}: {detail}')
        if not ok and (label in required_checks or label.startswith('plugin:') or label.startswith('dependency:') and label.split(':', 1)[1] in {item[0] for item in required_modules}):
            failed.append(label)
    if failed:
        print(f'[check] failed: {", ".join(failed)}')
        print('[check] install requirements and verify config paths')
        return 1
    print('[check] core environment check passed')
    return 0

def _generate_visualizations(csv_path, out_dir, library=None):
    try:
        from visualize import plot_scores, plot_top_mol
        plot_scores(csv_path, Path(out_dir) / 'vs_plot.png')
        plot_top_mol(csv_path, Path(out_dir) / 'top_mol.png', library=library)
    except Exception as exc:
        print(f'[viz] 可视化跳过，不影响核心结果: {exc}')

def run_ml(lib, out_dir, is_active_fn=None, test_fraction=0.25, external_dataset=None, ml_backend=None):
    """ML 活性预测初筛：用 RDKit 描述符 + 随机森林，对分子库预测活性概率。"""
    try:
        if ml_backend is None:
            from ml_predictor import train_model, predict_activity
        else:
            train_model = require_callable(ml_backend, 'train_model')
            predict_activity = require_callable(ml_backend, 'predict_activity')
        clf, metrics = train_model(lib, is_active_fn=is_active_fn, test_fraction=test_fraction, external_dataset=external_dataset)
        names = list(lib.keys())
        res = predict_activity(clf, list(lib.values()), lib)
        prob_map = dict(res)
        label_fn = is_active_fn
        if label_fn is None:
            from library_data import is_active as label_fn
        out = []
        for n in names:
            tag = 'active' if label_fn(n) else 'inactive'
            out.append({'mol_name': n, 'tag': tag, 'ml_prob': prob_map.get(lib[n], None)})
        import pandas as pd
        df = pd.DataFrame(out)
        df.to_csv(Path(out_dir) / 'ml_scores.csv', index=False)
        if metrics:
            metrics_path = Path(out_dir) / 'ml_metrics.json'
            metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            if metrics.get('accuracy') is not None:
                fmt = lambda value: f'{value:.2f}' if value is not None else 'n/a'
                print(
                    f'  [ML] {metrics.get("evaluation", "unknown")} n={metrics["n_samples"]} '
                    f'balanced_accuracy={fmt(metrics["balanced_accuracy"])} '
                    f'ROC-AUC={fmt(metrics["roc_auc"])} '
                    f'PR-AUC={fmt(metrics["pr_auc"])} '
                    f'EF30={fmt(metrics["top30_enrichment"])}'
                )
        return df
    except Exception as e:
        failure = {
            'evaluation': 'failed',
            'data_source': str(external_dataset) if external_dataset else 'screening_library',
            'error': str(e),
            'warning': 'ML evaluation failed; no usable predictions were generated',
        }
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / 'ml_metrics.json').write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        if external_dataset:
            raise RuntimeError(f'External ML evaluation failed: {e}') from e
        print(f'  [ML] evaluation skipped: {e}')
        return None

def run(receptor_pdb=None, out_dir=None, exhaustiveness=None, labels=None,
        config_path=None, resume=None, external_dataset=None, run_id=None):
    cfg = load_config(config_path)
    receptor_cfg = cfg.get('receptor', {})
    vina_cfg = cfg.get('vina', {})
    output_cfg = cfg.get('output', {})
    runtime_cfg = cfg.get('runtime', {})
    ml_cfg = cfg.get('ml', {})
    plugin_cfg = cfg.get('plugins', {})
    loaded_backends = {
        contract.key: load_contract(contract, plugin_cfg.get(contract.key))
        for contract in CADD_BACKEND_CONTRACTS
    }
    prep_receptor = require_callable(loaded_backends['receptor_backend'], 'prepare')
    build_library = require_callable(loaded_backends['library_backend'], 'build_library')
    dock_batch = require_callable(loaded_backends['docking_backend'], 'dock_batch')
    ml_backend = loaded_backends['ml_backend']
    report_backend = require_callable(loaded_backends['report_backend'], 'generate_report')
    plugin_metadata = {
        contract.key: plugin_info(loaded_backends[contract.key], contract, plugin_cfg.get(contract.key))
        for contract in CADD_BACKEND_CONTRACTS
    }

    external_dataset_value = external_dataset if external_dataset is not None else ml_cfg.get('external_dataset')
    external_dataset = resolve_path(external_dataset_value) if external_dataset_value else None
    library_module_path = resolve_path(cfg.get('library', {}).get('data_module', 'src/library_data.py'))
    library_module = _load_library_module(library_module_path)
    build_screening_library = library_module.build_screening_library
    is_active = library_module.is_active

    receptor_pdb = resolve_path(receptor_pdb or receptor_cfg.get('pdb_path', 'data/4hjo.pdb'))
    explicit_out_dir = out_dir is not None
    base_out_dir = resolve_path(out_dir or output_cfg.get('dir', 'output'))
    exhaustiveness = int(exhaustiveness if exhaustiveness is not None else vina_cfg.get('exhaustiveness', 8))
    seed_value = vina_cfg.get('seed', 42)
    seed = int(seed_value) if seed_value is not None else None
    vina_exe = resolve_path(vina_cfg.get('exe', 'tools/vina_1.2.7_win.exe'))
    box_center = receptor_cfg.get('box_center')
    box_center = tuple(float(v) for v in box_center) if box_center else None
    box_size = receptor_cfg.get('box_size')
    box_size = tuple(float(v) for v in box_size) if box_size else None
    resume = bool(runtime_cfg.get('resume', True) if resume is None else resume)
    config_file = resolve_path(config_path) if config_path else PROJECT_ROOT / 'config.yaml'
    test_fraction = float(ml_cfg.get('test_fraction', 0.25))

    if not receptor_pdb.exists():
        raise FileNotFoundError(f'受体文件不存在: {receptor_pdb}')
    if external_dataset and not external_dataset.exists():
        raise FileNotFoundError(f'外部 ML 数据集不存在: {external_dataset}')

    if not vina_exe.exists():
        raise FileNotFoundError(f'Vina 可执行文件不存在: {vina_exe}')
    lib = build_screening_library()
    names = list(lib.keys())
    smis = list(lib.values())
    run_fingerprint = _run_fingerprint(
        receptor_pdb,
        vina_exe,
        config_file,
        library_module_path,
        names,
        smis,
        external_dataset,
        exhaustiveness,
        seed,
        box_center,
        box_size,
    )
    run_output_cfg = dict(output_cfg)
    if explicit_out_dir and run_id is None:
        run_output_cfg['isolate_runs'] = False
    out_dir, resolved_run_id = _resolve_run_dir(
        base_out_dir,
        output_cfg,
        run_output_cfg,
        run_id,
        run_fingerprint,
    )
    csv_path = out_dir / output_cfg.get('csv', 'top_hits.csv')
    report_path = out_dir / output_cfg.get('report', 'report.md')
    base_out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    print('== 1/4 受体准备 ==')
    receptor = prep_receptor(receptor_pdb, out_dir)

    print('== 2/4 分子库准备 ==')
    library_dir = out_dir / 'library'
    sdf, pdbqts = build_library(smis, library_dir / 'screening_lib.sdf', library_dir / 'pdbqt', names)
    built_indices = {_ligand_index(path) for path in pdbqts}
    built_indices.discard(None)
    library_failed_ligands = [names[i] for i in range(len(names)) if i not in built_indices]
    if library_failed_ligands:
        print(f'  [build_library] 建库失败 {len(library_failed_ligands)} 个')

    signature = {
        'receptor': str(receptor_pdb),
        'receptor_sha256': _file_sha256(receptor_pdb),
        'vina_sha256': _file_sha256(vina_exe),
        'library_sha256': _library_sha256(names, smis),
        'library_module_sha256': _file_sha256(library_module_path),
        'config_sha256': _file_sha256(config_file),
        'ml_external_dataset_sha256': _file_sha256(external_dataset) if external_dataset else None,
        'plugins': plugin_metadata,
        'runtime': _runtime_signature(),
        'code_sha256': {
            _signature_path(path): _file_sha256(path)
            for path in (
                Path(__file__),
                Path(__file__).with_name('prepare_receptor.py'),
                Path(__file__).with_name('build_library.py'),
                Path(__file__).with_name('dock_vina.py'),
                Path(__file__).with_name('ml_predictor.py'),
                Path(__file__).with_name('report.py'),
                Path(__file__).with_name('plugin_loader.py'),
                library_module_path,
            )
            if path.exists()
        },
        'vina_exe': str(vina_exe),
        'exhaustiveness': exhaustiveness,
        'seed': seed,
        'ml_test_fraction': test_fraction,
        'box_center': list(box_center) if box_center else None,
        'box_size': list(box_size) if box_size else None,
        'ligands': names,
    }
    manifest_path = out_dir / 'run_manifest.json'
    previous_manifest = _load_json(manifest_path)
    resume_run = resume and previous_manifest and previous_manifest.get('signature') == signature
    if resume and previous_manifest and not resume_run:
        print('  [resume] 配置或分子库已变化，忽略旧运行清单')
    manifest = {
        'status': 'running',
        'run_id': resolved_run_id,
        'run_dir': str(out_dir),
        'run_fingerprint': run_fingerprint,
        'started_at': _now(),
        'finished_at': None,
        'config_path': str(config_file),
        'signature': signature,
        'ligand_count': len(names),
        'docking_candidate_count': len(pdbqts),
        'library_sdf': str(sdf),
        'library_pdbqt_dir': str(library_dir / 'pdbqt'),
        'ml_external_dataset': str(external_dataset) if external_dataset else None,
        'plugins': plugin_metadata,
        'library_failed_ligands': library_failed_ligands,
        'resume_requested': resume,
        'resumed_from_previous': bool(resume_run),
        'successful_ligands': 0,
        'failed_ligands': library_failed_ligands,
    }
    _save_json(manifest_path, manifest)
    _update_latest_run(base_out_dir, out_dir, resolved_run_id or 'default', 'running')

    print('== 2.5/4 ML 活性预测(随机森林) ==')
    try:
        ml_df = run_ml(lib, out_dir, is_active_fn=is_active, test_fraction=test_fraction, external_dataset=external_dataset, ml_backend=ml_backend)
    except RuntimeError as exc:
        manifest.update({
            'status': 'failed',
            'finished_at': _now(),
            'failure_stage': 'ml',
            'error': str(exc),
        })
        _save_json(manifest_path, manifest)
        _update_latest_run(base_out_dir, out_dir, resolved_run_id or 'default', 'failed')
        raise

    print(f'== 3/4 批量对接({len(pdbqts)} 配体, exhaustiveness={exhaustiveness}) ==')
    df = dock_batch(pdbqts, receptor, out_dir / 'docks', exhaustiveness, source_pdb=receptor_pdb,
                    center=box_center, size=box_size, vina_exe=vina_exe, resume=bool(resume_run), seed=seed)

    print('== 4/4 生成报告 ==')
    seq_to_name = _map_ligand_names(pdbqts, names)
    final = df.copy()
    def map_name(ligand_stem):
        try:
            idx = int(ligand_stem.split('_')[-1])
            return seq_to_name.get(idx, f'UNK_{idx}')
        except Exception:
            return ligand_stem
    final['mol_name'] = final['name'].map(map_name)
    def tag(n):
        return 'active' if is_active(n) else 'inactive'
    final['tag'] = final['mol_name'].map(tag)
    final = final[['mol_name', 'tag', 'affinity']].sort_values('affinity').reset_index(drop=True)
    final.to_csv(csv_path, index=False)
    print('\n=== 虚拟筛选结果(affinity 升序,越负越强) ===')
    print(final.to_string(index=False))
    print(f'\n[完成] 结果 CSV: {csv_path}')

    success_ids = {str(value) for value in df.get('name', [])}
    docking_failed_ligands = [names[i] for i in built_indices if f'ligand_{i}' not in success_ids]
    failed_ligands = library_failed_ligands + docking_failed_ligands
    manifest.update({
        'status': 'completed' if not failed_ligands else 'completed_with_failures',
        'finished_at': _now(),
        'successful_ligands': len(pdbqts) - len(docking_failed_ligands),
        'failed_ligands': failed_ligands,
        'docking_failed_ligands': docking_failed_ligands,
    })
    _save_json(manifest_path, manifest)
    _update_latest_run(base_out_dir, out_dir, resolved_run_id or 'default', manifest['status'])

    generate_report = report_backend
    generate_report(
        csv_path,
        report_path,
        receptor=receptor_cfg.get('pdb_id', 'EGFR'),
        manifest_path=manifest_path,
        ml_metrics_path=out_dir / 'ml_metrics.json',
    )
    _generate_visualizations(csv_path, out_dir, lib)
    return final


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--receptor', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--exhaustiveness', type=int, default=None)
    ap.add_argument('--no-resume', action='store_true')
    ap.add_argument('--check', action='store_true', help='检查配置、文件和运行依赖')
    ap.add_argument('--external-dataset', default=None, help='explicit external ML train/test CSV')
    ap.add_argument('--run-id', default=None, help='explicit isolated run identifier')
    a = ap.parse_args()
    if a.check:
        raise SystemExit(check_environment(a.config))
    try:
        run(a.receptor, a.out, a.exhaustiveness, config_path=a.config, resume=False if a.no_resume else None, external_dataset=a.external_dataset, run_id=a.run_id)
    except RuntimeError as exc:
        print(f'[pipeline] {exc}', file=sys.stderr)
        raise SystemExit(2)
