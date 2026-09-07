"""Immutable execution context shared by jobs, workers, workflows and plugins."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

try:
    from .observability import bind_context, current_context, trace_id as make_trace_id
except ImportError:
    from observability import bind_context, current_context, trace_id as make_trace_id


RUN_CONTEXT_VERSION = 1
_RUN_CONTEXT = ContextVar('run_context', default=None)
_RUN_ACTOR = ContextVar('run_actor', default=None)
_SENSITIVE_KEY = re.compile(
    r'(?:authorization|cookie|credential|password|passwd|secret|token|api[_-]?key)',
    re.IGNORECASE,
)
_SEED_KEY = re.compile(r'(?:^|_)(?:seed|random_seed|random_state)(?:$|_)', re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


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


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=str))
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _sanitize(value, key='', depth=0):
    if _SENSITIVE_KEY.search(str(key)):
        return '[REDACTED]'
    if depth >= 8:
        return {'type': type(value).__name__, 'digest': _digest(str(value))}
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item, str(item_key), depth + 1)
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        items = [_sanitize(item, key, depth + 1) for item in value[:100]]
        if len(value) > 100:
            items.append({'truncated_items': len(value) - 100})
        return items
    if isinstance(value, str) and len(value) > 512:
        return {
            'type': 'string',
            'length': len(value),
            'sha256': hashlib.sha256(value.encode()).hexdigest(),
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            size += len(chunk)
            digest.update(chunk)
    return {'sha256': digest.hexdigest(), 'size_bytes': size}


def _input_hashes(value, prefix='', output=None):
    output = output if output is not None else {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = f'{prefix}.{key}' if prefix else str(key)
            _input_hashes(item, name, output)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _input_hashes(item, f'{prefix}[{index}]', output)
    elif isinstance(value, (str, os.PathLike)):
        try:
            path = Path(value)
            if path.is_file():
                output[prefix or 'input'] = _hash_file(path)
        except (OSError, TypeError, ValueError):
            pass
    return output


def _random_seeds(value, prefix='', output=None):
    output = output if output is not None else {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = f'{prefix}.{key}' if prefix else str(key)
            if _SEED_KEY.search(str(key)) and isinstance(item, (int, str)):
                output[name] = item
            else:
                _random_seeds(item, name, output)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _random_seeds(item, f'{prefix}[{index}]', output)
    return output


def _actor_value(actor):
    if actor is None:
        return {'sub': 'system', 'roles': ['system'], 'auth_type': 'internal'}
    if hasattr(actor, 'as_dict'):
        actor = actor.as_dict()
    if not isinstance(actor, Mapping):
        raise TypeError('run actor must be a mapping or expose as_dict()')
    return _sanitize(dict(actor))


def _plugin_snapshot(spec):
    spec = spec or {}
    contract = {
        'parameters': spec.get('parameters') or {},
        'returns': spec.get('returns') or spec.get('result_schema') or {},
        'permissions': spec.get('permissions') or {},
        'resources': spec.get('resources') or {},
    }
    return {
        'name': str(spec.get('domain') or 'unknown'),
        'version': str(spec.get('plugin_version') or spec.get('version') or 'unknown'),
        'api_version': spec.get('plugin_api_version') or spec.get('api_version'),
        'contract_sha256': str(
            spec.get('plugin_contract_digest') or _digest(contract)
        ),
    }


@dataclass(frozen=True)
class RunContext:
    run_id: str
    trace_id: str
    tool: str
    domain: str
    created_at: str = field(default_factory=_now)
    schema_version: int = RUN_CONTEXT_VERSION
    request_id: str | None = None
    job_id: str | None = None
    parent_run_id: str | None = None
    retry_of: str | None = None
    actor: Mapping[str, Any] = field(default_factory=dict)
    plugin: Mapping[str, Any] = field(default_factory=dict)
    resources: Mapping[str, Any] = field(default_factory=dict)
    input_hashes: Mapping[str, Any] = field(default_factory=dict)
    configuration: Mapping[str, Any] = field(default_factory=dict)
    configuration_sha256: str = ''
    random_seeds: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.schema_version != RUN_CONTEXT_VERSION:
            raise ValueError(f'unsupported run context schema version: {self.schema_version}')
        for name in ('run_id', 'trace_id', 'tool', 'domain', 'created_at'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f'run context {name} is required')
        frozen_fields = (
            'actor', 'plugin', 'resources', 'input_hashes', 'configuration', 'random_seeds'
        )
        for name in frozen_fields:
            object.__setattr__(self, name, _freeze(dict(getattr(self, name))))

    def as_dict(self):
        return {
            'schema_version': self.schema_version,
            'run_id': self.run_id,
            'trace_id': self.trace_id,
            'request_id': self.request_id,
            'job_id': self.job_id,
            'parent_run_id': self.parent_run_id,
            'retry_of': self.retry_of,
            'tool': self.tool,
            'domain': self.domain,
            'created_at': self.created_at,
            'actor': _thaw(self.actor),
            'plugin': _thaw(self.plugin),
            'resources': _thaw(self.resources),
            'input_hashes': _thaw(self.input_hashes),
            'configuration': _thaw(self.configuration),
            'configuration_sha256': self.configuration_sha256,
            'random_seeds': _thaw(self.random_seeds),
        }

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError('run context must be an object')
        fields = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in fields})


def build_run_context(tool, arguments, spec=None, resources=None, priority=0,
                      job_id=None, retry_of=None, parent=None, run_id=None,
                      trace_id=None, request_id=None):
    parent = RunContext.from_dict(parent) if parent is not None else None
    observable = current_context()
    sanitized_arguments = _sanitize(arguments)
    configuration = {
        'argument_names': sorted(str(key) for key in arguments),
        'argument_types': {
            str(key): type(value).__name__
            for key, value in sorted(arguments.items(), key=lambda pair: str(pair[0]))
        },
        'priority': int(priority),
    }
    configuration_digest_input = {
        'arguments': sanitized_arguments,
        'priority': int(priority),
    }
    plugin = _plugin_snapshot(spec)
    read_arguments = (
        ((spec or {}).get('permissions') or {}).get('filesystem') or {}
    ).get('read') or []
    hashable_inputs = {
        name: arguments[name]
        for name in read_arguments
        if name in arguments
    }
    bound_actor = _RUN_ACTOR.get()
    selected_run_id = run_id or (parent.run_id if parent else uuid4().hex)
    return RunContext(
        run_id=selected_run_id,
        trace_id=(
            parent.trace_id
            if parent else (trace_id or observable.get('trace_id'))
        ) or make_trace_id(),
        request_id=(
            parent.request_id
            if parent else (request_id or observable.get('request_id'))
        ),
        job_id=job_id or (parent.job_id if parent else None),
        parent_run_id=(
            parent.run_id
            if parent is not None and selected_run_id != parent.run_id
            else (parent.parent_run_id if parent else None)
        ),
        retry_of=retry_of,
        tool=str(tool),
        domain=str((spec or {}).get('domain') or plugin['name']),
        actor=(
            bound_actor
            if bound_actor is not None
            else (parent.actor if parent else current_run_actor())
        ),
        plugin=plugin,
        resources=(
            resources
            if resources is not None
            else (parent.resources if parent else {})
        ),
        input_hashes=_input_hashes(hashable_inputs),
        configuration=configuration,
        configuration_sha256=_digest(configuration_digest_input),
        random_seeds=_random_seeds(arguments),
    )


def derive_run_context(parent, tool, arguments, spec=None, resources=None, priority=0):
    parent = RunContext.from_dict(parent)
    derived = build_run_context(
        tool,
        arguments,
        spec=spec,
        resources=resources,
        priority=priority,
        job_id=parent.job_id,
        parent=parent,
        run_id=parent.run_id,
    )
    values = derived.as_dict()
    values['parent_run_id'] = parent.parent_run_id
    values['retry_of'] = parent.retry_of
    values['actor'] = parent.as_dict()['actor']
    return RunContext.from_dict(values)


def current_run_context(as_dict=False):
    value = _RUN_CONTEXT.get()
    return value.as_dict() if as_dict and value is not None else value


def current_run_actor():
    actor = _RUN_ACTOR.get()
    return _thaw(actor) if actor is not None else _actor_value(None)


@contextmanager
def bind_run_actor(actor):
    token = _RUN_ACTOR.set(_freeze(_actor_value(actor)))
    try:
        yield current_run_actor()
    finally:
        _RUN_ACTOR.reset(token)


@contextmanager
def bind_run_context(context):
    context = RunContext.from_dict(context)
    token = _RUN_CONTEXT.set(context)
    with bind_context(
        trace_id=context.trace_id,
        request_id=context.request_id,
        job_id=context.job_id,
        run_id=context.run_id,
        tool=context.tool,
        plugin=context.domain,
    ):
        try:
            yield context
        finally:
            _RUN_CONTEXT.reset(token)
