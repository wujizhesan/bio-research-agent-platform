"""Health evaluation for pluggable scientific domains."""

from datetime import datetime, timezone
from time import perf_counter

try:
    from .plugin_manifest import check_requirements, validate_install_candidate
except ImportError:
    from plugin_manifest import check_requirements, validate_install_candidate


def _now():
    return datetime.now(timezone.utc).isoformat()


def _health_callable(source):
    if source is None:
        return None
    for name in ('plugin_health', 'health_check', 'plugin_status'):
        function = getattr(source, name, None)
        if callable(function):
            return function
    return None


def _normalize_result(result):
    if isinstance(result, bool):
        return result, {}
    if not isinstance(result, dict):
        raise TypeError('plugin health check must return a boolean or mapping')
    if 'healthy' in result:
        healthy = bool(result['healthy'])
    elif 'available' in result:
        healthy = bool(result['available'])
    else:
        healthy = str(result.get('status', 'healthy')).lower() in {
            'ok', 'healthy', 'available', 'ready',
        }
    return healthy, dict(result)


def assess_plugin_health(domain, source, manifest):
    started = perf_counter()
    requirements = check_requirements(manifest.get('requirements', []))
    healthy = manifest.get('status') == 'available' and requirements['compatible']
    details = {}
    sandboxed = manifest.get('security', {}).get('trust') == 'sandboxed'
    validation = validate_install_candidate(manifest) if sandboxed else None
    if validation is not None:
        healthy = healthy and validation['compatible']
    if manifest.get('status') != 'available':
        details['reason'] = f"plugin manifest status is {manifest.get('status')}"
    elif not requirements['compatible']:
        details['reason'] = 'plugin requirements are not satisfied'
    elif validation is not None and not validation['compatible']:
        details['reason'] = '; '.join(validation['errors'])
    elif sandboxed:
        details = {
            'source': 'manifest',
            'runtime_check': 'deferred_to_isolated_execution',
            'permissions': validation['permissions'],
        }
    else:
        function = _health_callable(source)
        if function is not None:
            try:
                healthy, details = _normalize_result(function())
            except Exception as exc:
                healthy = False
                details = {'reason': f'{type(exc).__name__}: {exc}'}
    return {
        **details,
        'domain': domain,
        'status': 'healthy' if healthy else 'unhealthy',
        'healthy': healthy,
        'checked_at': _now(),
        'duration_ms': round((perf_counter() - started) * 1000, 3),
        'requirements': requirements,
    }


def validate_candidate(manifest):
    return validate_install_candidate(manifest)
