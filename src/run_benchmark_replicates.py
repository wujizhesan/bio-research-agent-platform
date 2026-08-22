"""Generate repeated random controls and compare them with a hard-decoy benchmark."""
import argparse
import json
from pathlib import Path

import pandas as pd

try:
    from .build_hard_decoy_benchmark import build_hard_decoy_benchmark, build_random_control_benchmark
    from .compare_benchmarks import compare_benchmark_replicates
    from .ml_predictor import load_external_dataset
except ImportError:
    from build_hard_decoy_benchmark import build_hard_decoy_benchmark, build_random_control_benchmark
    from compare_benchmarks import compare_benchmark_replicates
    from ml_predictor import load_external_dataset


def _normalize_id(value):
    if pd.isna(value):
        return ''
    text = str(value).strip()
    if text.endswith('.0') and text[:-2].isdigit():
        return text[:-2]
    return text


def _role_names(frame, role):
    required = {'split', 'benchmark_role', 'name'}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f'benchmark is missing columns: {sorted(missing)}')
    names = frame.loc[
        (frame['split'] == 'test') & (frame['benchmark_role'] == role), 'name'
    ].map(_normalize_id).tolist()
    if not names:
        raise ValueError(f'benchmark has no test rows with role={role}')
    if len(names) != len(set(names)):
        raise ValueError(f'benchmark role={role} contains duplicate names')
    return names


def _validate_hard_benchmark(frame, input_frame, provenance):
    active_names = _role_names(frame, 'active')
    decoy_names = _role_names(frame, 'hard_decoy')
    matched = frame.loc[
        (frame['split'] == 'test') & (frame['benchmark_role'] == 'hard_decoy'), 'matched_active'
    ].map(_normalize_id).tolist()
    input_active_names = set(input_frame.loc[
        (input_frame['split'] == 'test') & (input_frame['tag'] == 'active'), 'name'
    ].map(_normalize_id))
    if not set(active_names).issubset(input_active_names):
        raise ValueError('hard benchmark active rows are not present in the input test actives')
    if set(matched) != set(active_names):
        raise ValueError('hard decoys do not cover exactly the hard benchmark active names')
    if len(decoy_names) != len(active_names):
        raise ValueError('hard benchmark must contain one decoy per active row')
    if provenance.get('input_rows') is not None and int(provenance['input_rows']) != len(input_frame):
        raise ValueError('hard benchmark provenance input_rows does not match the current input')
    if provenance.get('test_active_rows') is not None and int(provenance['test_active_rows']) != len(active_names):
        raise ValueError('hard benchmark provenance active count does not match the CSV')
    if provenance.get('test_hard_decoy_rows') is not None and int(provenance['test_hard_decoy_rows']) != len(decoy_names):
        raise ValueError('hard benchmark provenance decoy count does not match the CSV')
    return active_names


def _validate_control(frame, active_names, hard_frame):
    control_active = _role_names(frame, 'active')
    control_decoys = _role_names(frame, 'random_control')
    if set(control_active) != set(active_names) or len(control_active) != len(active_names):
        raise ValueError('random control active names do not match the hard benchmark')
    matched = frame.loc[
        (frame['split'] == 'test') & (frame['benchmark_role'] == 'random_control'), 'matched_active'
    ].map(_normalize_id).tolist()
    if set(matched) != set(active_names):
        raise ValueError('random control decoys do not cover the hard benchmark active names')
    if len(control_decoys) != len(active_names) or len(frame[frame['split'] == 'test']) != len(active_names) * 2:
        raise ValueError('random control test split is not balanced with the hard benchmark')
    hard_train = int((hard_frame['split'] == 'train').sum())
    control_train = int((frame['split'] == 'train').sum())
    if control_train != hard_train:
        raise ValueError('random control training row count differs from hard benchmark')
    return {
        'active_names_match': True,
        'balanced_test': True,
        'train_rows_match': True,
    }


def _load_provenance(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot read hard benchmark provenance: {path}') from exc


def run_benchmark_replicates(input_csv, hard_csv, hard_provenance, control_dir,
                             seeds, comparison_output, max_tanimoto=0.5,
                             max_pairs=50, bootstrap_iterations=2000,
                             bootstrap_random_state=42):
    seeds = [int(seed) for seed in seeds]
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError('seeds must be non-empty and unique')
    input_frame = load_external_dataset(input_csv).reset_index(drop=True)
    hard_csv = Path(hard_csv)
    hard_provenance = Path(hard_provenance)
    if hard_csv.exists() and hard_provenance.exists():
        hard = pd.read_csv(hard_csv)
        hard_metadata = _load_provenance(hard_provenance)
    else:
        hard, hard_metadata = build_hard_decoy_benchmark(
            input_csv,
            hard_csv,
            hard_provenance,
            max_tanimoto=max_tanimoto,
            max_pairs=max_pairs,
        )
    active_names = _validate_hard_benchmark(hard, input_frame, hard_metadata)
    control_dir = Path(control_dir)
    control_dir.mkdir(parents=True, exist_ok=True)
    control_paths = []
    validation = []
    for seed in seeds:
        control_csv = control_dir / f'random_control_{seed}.csv'
        control_provenance = control_dir / f'random_control_{seed}.provenance.json'
        control, _ = build_random_control_benchmark(
            input_csv,
            active_names,
            control_csv,
            control_provenance,
            random_state=seed,
        )
        validation.append(_validate_control(control, active_names, hard))
        control_paths.append(str(control_csv))
    result = compare_benchmark_replicates(
        hard_csv,
        control_paths,
        comparison_output,
        bootstrap_iterations=bootstrap_iterations,
        random_state=bootstrap_random_state,
    )
    result['random_control_dir'] = str(control_dir)
    result['random_states'] = seeds
    result['validation'] = {
        'hard_active_names_valid': True,
        'control_replicates_valid': len(validation) == len(seeds),
        'active_names_match': all(item['active_names_match'] for item in validation),
        'balanced_test': all(item['balanced_test'] for item in validation),
        'train_rows_match': all(item['train_rows_match'] for item in validation),
    }
    Path(comparison_output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run repeated hard-decoy benchmark controls')
    parser.add_argument('--input', required=True)
    parser.add_argument('--hard', required=True)
    parser.add_argument('--hard-provenance', required=True)
    parser.add_argument('--control-dir', required=True)
    parser.add_argument('--seeds', nargs='+', type=int, required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-tanimoto', type=float, default=0.5)
    parser.add_argument('--max-pairs', type=int, default=50)
    parser.add_argument('--bootstrap-iterations', type=int, default=2000)
    parser.add_argument('--bootstrap-random-state', type=int, default=42)
    args = parser.parse_args(argv)
    if args.max_pairs < 1:
        raise SystemExit('--max-pairs must be positive')
    if args.bootstrap_iterations < 0:
        raise SystemExit('--bootstrap-iterations must be non-negative')
    print(json.dumps(run_benchmark_replicates(
        args.input,
        args.hard,
        args.hard_provenance,
        args.control_dir,
        args.seeds,
        args.out,
        args.max_tanimoto,
        args.max_pairs,
        args.bootstrap_iterations,
        args.bootstrap_random_state,
    ), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
