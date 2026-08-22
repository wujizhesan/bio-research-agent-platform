"""Compare paired hard-decoy and random-control benchmark metrics."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .ml_predictor import train_model
except ImportError:
    from ml_predictor import train_model


METRICS = ('balanced_accuracy', 'roc_auc', 'pr_auc', 'pr_auc_baseline', 'pr_auc_lift', 'top30_enrichment')


def _evaluate(path):
    _, metrics = train_model({}, external_dataset=path)
    return {
        key: metrics.get(key)
        for key in METRICS
    } | {
        'n_train': metrics.get('n_train'),
        'n_test': metrics.get('n_test'),
        'test_active_fraction': metrics.get('test_active_fraction'),
        'scaffold_overlap_count': metrics.get('scaffold_overlap_count'),
        'document_overlap_count': metrics.get('document_overlap_count'),
    }


def _metric_delta(hard, control):
    return {
        key: hard[key] - control[key]
        for key in METRICS
        if hard.get(key) is not None and control.get(key) is not None
    }


def _validate_evaluation_shapes(hard, controls):
    shape_keys = (
        'n_train', 'n_test', 'test_active_fraction',
        'scaffold_overlap_count', 'document_overlap_count',
    )
    for index, control in enumerate(controls, start=1):
        for key in shape_keys:
            expected = hard.get(key)
            actual = control.get(key)
            if isinstance(expected, float) or isinstance(actual, float):
                matches = expected is not None and actual is not None and np.isclose(expected, actual)
            else:
                matches = expected == actual
            if not matches:
                raise ValueError(
                    f'benchmark shape mismatch for control {index}: {key}={actual!r}, expected {expected!r}'
                )


def _bootstrap_summary(values, iterations=2000, random_state=42):
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {'n': 0, 'mean': None, 'std': None, 'ci95_low': None, 'ci95_high': None}
    mean = float(np.mean(array))
    std = float(np.std(array, ddof=1)) if array.size > 1 else 0.0
    if array.size == 1 or iterations <= 0:
        low = high = mean
    else:
        rng = np.random.default_rng(random_state)
        samples = rng.choice(array, size=(int(iterations), array.size), replace=True).mean(axis=1)
        low, high = np.percentile(samples, [2.5, 97.5])
    return {
        'n': int(array.size),
        'mean': mean,
        'std': std,
        'ci95_low': float(low),
        'ci95_high': float(high),
    }


def compare_benchmarks(hard_path, control_path, output_path):
    hard = _evaluate(hard_path)
    control = _evaluate(control_path)
    _validate_evaluation_shapes(hard, [control])
    delta = _metric_delta(hard, control)
    payload = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'hard_decoy_csv': str(hard_path),
        'random_control_csv': str(control_path),
        'hard_decoy': hard,
        'random_control': control,
        'hard_minus_random_delta': delta,
        'interpretation': 'Positive balanced_accuracy or ROC-AUC delta means hard-decoy performance was higher, not necessarily that the task was harder.',
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def compare_benchmark_replicates(hard_path, control_paths, output_path,
                                 bootstrap_iterations=2000, random_state=42):
    control_paths = [str(path) for path in control_paths]
    if not control_paths:
        raise ValueError('at least one random-control benchmark is required')
    hard = _evaluate(hard_path)
    controls = [_evaluate(path) for path in control_paths]
    _validate_evaluation_shapes(hard, controls)
    deltas = [_metric_delta(hard, control) for control in controls]
    control_summary = {
        key: _bootstrap_summary(
            [metrics[key] for metrics in controls if metrics.get(key) is not None],
            bootstrap_iterations,
            random_state,
        )
        for key in METRICS
    }
    delta_summary = {
        key: _bootstrap_summary(
            [delta[key] for delta in deltas if delta.get(key) is not None],
            bootstrap_iterations,
            random_state,
        )
        for key in METRICS
    }
    payload = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'hard_decoy_csv': str(hard_path),
        'random_control_csvs': control_paths,
        'n_replicates': len(controls),
        'hard_decoy': hard,
        'random_control_replicates': controls,
        'random_control_summary': control_summary,
        'hard_minus_random_summary': delta_summary,
        'interpretation': 'A negative hard-minus-random mean indicates lower hard-decoy performance. Bootstrap intervals quantify variation across random-control selections, not uncertainty from retraining or data collection.',
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description='Compare hard-decoy and random-control benchmarks')
    parser.add_argument('--hard', required=True)
    parser.add_argument('--control', nargs='+', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--bootstrap-iterations', type=int, default=2000)
    parser.add_argument('--random-state', type=int, default=42)
    args = parser.parse_args(argv)
    if args.bootstrap_iterations < 0:
        raise SystemExit('--bootstrap-iterations must be non-negative')
    if len(args.control) == 1:
        result = compare_benchmarks(args.hard, args.control[0], args.out)
    else:
        result = compare_benchmark_replicates(
            args.hard,
            args.control,
            args.out,
            args.bootstrap_iterations,
            args.random_state,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
