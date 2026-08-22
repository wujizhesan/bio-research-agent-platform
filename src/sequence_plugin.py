"""Adapter for the external mRNA-Forge sequence domain."""
import importlib
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

try:
    from .config_loader import PROJECT_ROOT, load_config
except ImportError:
    from config_loader import PROJECT_ROOT, load_config


PLUGIN_NAME = 'mRNA-Forge sequence domain'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'sequence.optimize',
    'sequence.score',
    'sequence.verify',
    'sequence.compare',
    'sequence.benchmark',
    'sequence.pipeline',
    'sequence.report',
)


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _configured_root():
    try:
        config = load_config()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        config = {}
    sequence_config = config.get('sequence', {}) or {}
    if sequence_config.get('enabled') is False:
        return None
    configured = sequence_config.get('root')
    if configured:
        return Path(configured).expanduser()
    env_root = os.environ.get('MRNA_FORGE_ROOT')
    if env_root:
        return Path(env_root).expanduser()
    candidates = (
        PROJECT_ROOT.parent.parent / 'EnornaAgent',
        PROJECT_ROOT.parent / 'EnornaAgent',
    )
    return next((path for path in candidates if path.is_dir()), None)


def plugin_status():
    root = _configured_root()
    if root is None:
        return {'available': False, 'reason': 'sequence plugin disabled or root not found'}
    required = (root / 'core' / 'rules.py', root / 'tools' / 'tools.py')
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return {'available': False, 'root': str(root), 'missing': missing}
    return {'available': True, 'root': str(root)}


@lru_cache(maxsize=4)
def _backend(root_text):
    root = Path(root_text)
    sys.path.insert(0, str(root))
    try:
        backend = importlib.import_module('tools.tools')
        rules = importlib.import_module('core.rules')
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass
    if not callable(getattr(backend, 'call', None)):
        raise ImportError('mRNA-Forge tools module does not expose call()')
    if hasattr(backend, 'tool_compare'):
        backend.TOOL_IMPL.setdefault('compare', backend.tool_compare)
    return backend, rules


def _get_backend():
    status = plugin_status()
    if not status.get('available'):
        raise RuntimeError(status.get('reason') or f"sequence plugin unavailable: {status}")
    return _backend(status['root'])


def _normalize_protein(protein):
    if not isinstance(protein, str):
        raise ValueError('protein must be a string')
    lines = [line.strip() for line in protein.strip().splitlines() if line.strip()]
    if lines and lines[0].startswith('>'):
        lines = lines[1:]
    value = ''.join(lines).upper()
    if not value:
        raise ValueError('protein sequence is empty')
    if not re.fullmatch(r'[ACDEFGHIKLMNPQRSTVWY*]+', value):
        raise ValueError('protein contains unsupported amino-acid symbols')
    return value


def _normalize_mrna(mrna):
    if not isinstance(mrna, str):
        raise ValueError('mrna must be a string')
    value = re.sub(r'\s+', '', mrna).upper()
    if not value:
        raise ValueError('mrna sequence is empty')
    if not re.fullmatch(r'[ACGTUN]+', value):
        raise ValueError('mrna contains unsupported nucleotide symbols')
    return value


def _envelope(operation, payload, method=None):
    payload = payload if isinstance(payload, dict) else {'value': payload}
    status = payload.get('status', 'ok')
    if status == 'error' or 'error' in payload:
        return {
            'status': 'error',
            'plugin': 'sequence',
            'operation': operation,
            'error': payload.get('error', 'sequence operation failed'),
            'result': payload,
            'provenance': {'backend': PLUGIN_NAME, 'version': PLUGIN_VERSION},
        }
    envelope = {
        'status': 'ok',
        'plugin': 'sequence',
        'operation': operation,
        'result': payload,
        'provenance': {'backend': PLUGIN_NAME, 'version': PLUGIN_VERSION},
    }
    if method:
        envelope['provenance']['method'] = method
    if isinstance(payload.get('metrics'), dict):
        envelope['metrics'] = payload['metrics']
    return envelope


def sequence_optimize(protein, molecule='linear', method='greedy'):
    protein = _normalize_protein(protein)
    backend, _ = _get_backend()
    return _envelope(
        'optimize',
        backend.call('optimize', {
            'protein': protein,
            'molecule': molecule,
            'method': method,
        }),
        method=method,
    )


def sequence_score(mrna, molecule='linear'):
    backend, _ = _get_backend()
    return _envelope('score', backend.call('score', {
        'mrna': _normalize_mrna(mrna),
        'molecule': molecule,
    }))


def sequence_verify(mrna, protein):
    backend, _ = _get_backend()
    return _envelope('verify', backend.call('verify', {
        'mrna': _normalize_mrna(mrna),
        'protein': _normalize_protein(protein),
    }))


def sequence_compare(mrna, baseline, molecule='linear'):
    backend, _ = _get_backend()
    return _envelope('compare', backend.call('compare', {
        'mrna': _normalize_mrna(mrna),
        'baseline': _normalize_protein(baseline),
        'molecule': molecule,
    }))


def sequence_benchmark(protein, molecule='linear', use_vaxpress=False):
    backend, _ = _get_backend()
    return _envelope('benchmark', backend.call('benchmark', {
        'protein': _normalize_protein(protein),
        'molecule': molecule,
        'use_vaxpress': use_vaxpress,
    }))


def sequence_pipeline(protein, molecule='linear', method='greedy'):
    optimized = sequence_optimize(protein, molecule, method)
    if optimized['status'] != 'ok':
        return optimized
    payload = optimized['result']
    mrna = payload.get('mrna')
    scored = sequence_score(mrna, molecule)
    if scored['status'] != 'ok':
        return scored
    verified = sequence_verify(mrna, protein)
    if verified['status'] != 'ok':
        return verified
    result = {
        'status': 'ok',
        'pipeline': 'optimize->score->verify',
        'molecule': molecule,
        'method': method,
        'mrna': mrna,
        'mrna_len': len(mrna),
        'metrics': scored.get('metrics', {}),
        'checks': scored.get('result', {}).get('checks', []),
        'verdict': scored.get('result', {}).get('verdict', 'REVIEW'),
        'verify': verified.get('result', {}).get('identical', False),
        'steps': [optimized, scored, verified],
    }
    return _envelope('pipeline', result, method=method)


def sequence_report(result, output_path='output/sequence_report.html'):
    _get_backend()
    root = Path(plugin_status()['root'])
    if not isinstance(result, dict):
        raise ValueError('result must be an object')
    payload = result.get('result', result)
    report_path = Path(output_path)
    if not report_path.is_absolute():
        report_path = PROJECT_ROOT / report_path
    sys.path.insert(0, str(root))
    try:
        report_builder = importlib.import_module('report.report_builder')
        built = report_builder.build_report(payload, str(report_path))
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass
    return _envelope('report', {
        'status': 'ok',
        'output_html': str(built or report_path),
    })


_sequence_properties = {
    'protein': {'type': 'string'},
    'mrna': {'type': 'string'},
    'baseline': {'type': 'string'},
    'molecule': {'type': 'string', 'enum': ['linear', 'circ', 'sa']},
    'method': {'type': 'string', 'enum': ['greedy', 'vaxpress']},
    'use_vaxpress': {'type': 'boolean'},
}


TOOLS = {
    'optimize': {
        'description': 'Optimize a protein sequence into mRNA with deterministic codon rules or optional VaxPress.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'protein', 'molecule', 'method'}}, ('protein',)),
        'function': sequence_optimize,
    },
    'score': {
        'description': 'Score an mRNA sequence with CAI, GC, GC3, UpA, UpU and rule checks.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'mrna', 'molecule'}}, ('mrna',)),
        'function': sequence_score,
    },
    'verify': {
        'description': 'Verify that an mRNA sequence translates back to the requested protein.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'mrna', 'protein'}}, ('mrna', 'protein')),
        'function': sequence_verify,
    },
    'compare': {
        'description': 'Compare an optimized mRNA against a protein-derived naive baseline.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'mrna', 'baseline', 'molecule'}}, ('mrna', 'baseline')),
        'function': sequence_compare,
    },
    'benchmark': {
        'description': 'Benchmark naive, greedy and optionally VaxPress sequence optimization.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'protein', 'molecule', 'use_vaxpress'}}, ('protein',)),
        'function': sequence_benchmark,
    },
    'pipeline': {
        'description': 'Run optimize, score and translation verification as one deterministic sequence workflow.',
        'parameters': _parameters({key: value for key, value in _sequence_properties.items() if key in {'protein', 'molecule', 'method'}}, ('protein',)),
        'function': sequence_pipeline,
    },
    'report': {
        'description': 'Render a sequence pipeline result as a standalone HTML report.',
        'parameters': _parameters({
            'result': {'type': 'object'},
            'output_path': {'type': 'string'},
        }, ('result',)),
        'function': sequence_report,
    },
}


def load_tools():
    if not plugin_status().get('available'):
        return None
    return TOOLS
