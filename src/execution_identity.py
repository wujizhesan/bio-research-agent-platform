"""Immutable implementation identity and capability routing descriptors."""

from functools import lru_cache
import hashlib
import json
import os
import re
import shutil

try:
    from .reproducibility import plugin_snapshot, runtime_snapshot
    from .resource_scheduling import ResourceRequest
except ImportError:
    from reproducibility import plugin_snapshot, runtime_snapshot
    from resource_scheduling import ResourceRequest


EXECUTION_IDENTITY_VERSION = 1
_IMAGE_DIGEST = re.compile(r'(sha256:[0-9a-fA-F]{64})')


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _configured_toolchain(domain):
    raw = os.environ.get('WORKER_TOOLCHAIN_MANIFEST', '').strip()
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError('WORKER_TOOLCHAIN_MANIFEST must be valid JSON') from exc
    if not isinstance(document, dict):
        raise ValueError('WORKER_TOOLCHAIN_MANIFEST must be an object')
    selected = document.get(domain, document.get('default'))
    return selected if selected is not None else document


@lru_cache(maxsize=32)
def external_toolchain_snapshot(domain):
    configured = _configured_toolchain(domain)
    if configured is not None:
        snapshot = {'source': 'configured', 'tools': configured}
    elif domain == 'omics':
        try:
            from .omics_agent import TOOLCHAIN_EXECUTABLES, _external_tool_version
        except ImportError:
            from omics_agent import TOOLCHAIN_EXECUTABLES, _external_tool_version
        tools = {}
        for name, executable in sorted(TOOLCHAIN_EXECUTABLES.items()):
            path = shutil.which(executable)
            tools[name] = {
                'executable': executable,
                'available': bool(path),
                'version': _external_tool_version(path) if path else None,
            }
        snapshot = {'source': 'detected', 'tools': tools}
    else:
        snapshot = {
            'source': 'runtime',
            'runtime_fingerprint': runtime_snapshot()['fingerprint'],
        }
    snapshot['fingerprint'] = _digest(snapshot)
    return snapshot


def build_execution_identity(spec):
    plugin = plugin_snapshot(spec)
    toolchain = external_toolchain_snapshot(str(spec.get('domain') or 'unknown'))
    image_reference = os.environ.get('APP_IMAGE_REFERENCE', 'unknown')
    matched_digest = _IMAGE_DIGEST.search(image_reference)
    identity = {
        'schema_version': EXECUTION_IDENTITY_VERSION,
        'git_commit': os.environ.get('APP_GIT_SHA', 'unknown'),
        'worker_image_reference': image_reference,
        'worker_image_digest': matched_digest.group(1).lower() if matched_digest else 'unknown',
        'plugin_domain': plugin.get('domain'),
        'plugin_version': plugin.get('version'),
        'plugin_api_version': plugin.get('api_version'),
        'plugin_contract_digest': plugin.get('contract_digest'),
        'plugin_implementation_sha256': plugin.get('implementation', {}).get(
            'source_sha256'
        ),
        'external_toolchain_fingerprint': toolchain['fingerprint'],
    }
    identity['fingerprint'] = _digest(identity)
    if os.environ.get('APP_ENV', 'development').strip().lower() == 'production':
        missing = []
        if not re.fullmatch(r'[0-9a-fA-F]{40}', identity['git_commit']):
            missing.append('APP_GIT_SHA')
        if identity['worker_image_digest'] == 'unknown':
            missing.append('APP_IMAGE_REFERENCE digest')
        if not identity['plugin_implementation_sha256']:
            missing.append('plugin implementation SHA-256')
        if missing:
            raise ValueError(
                'production execution identity is incomplete: '
                + ', '.join(missing)
            )
    return identity


def identity_mismatches(expected, actual):
    fields = (
        'git_commit',
        'worker_image_digest',
        'plugin_version',
        'plugin_api_version',
        'plugin_contract_digest',
        'plugin_implementation_sha256',
        'external_toolchain_fingerprint',
        'fingerprint',
    )
    return [
        field
        for field in fields
        if expected.get(field) != actual.get(field)
    ]


def routing_descriptor(tool, resources, identity):
    request = ResourceRequest.from_mapping(resources)
    high_memory_threshold = max(int(
        os.environ.get('WORKER_HIGH_MEMORY_THRESHOLD_MB', '16384')
    ), 1)
    if request.gpu_count > 0:
        resource_class = 'gpu'
    elif request.memory_mb >= high_memory_threshold:
        resource_class = 'cpu/high-memory'
    else:
        resource_class = 'cpu/default'
    descriptor = {
        'tool': str(tool),
        'resource_class': resource_class,
        'resources': request.as_dict(),
        'required_labels': list(request.labels),
        'plugin_domain': identity.get('plugin_domain'),
        'plugin_version': identity.get('plugin_version'),
        'execution_fingerprint': identity.get('fingerprint'),
    }
    descriptor['route_id'] = _digest(descriptor)[:32]
    return descriptor
