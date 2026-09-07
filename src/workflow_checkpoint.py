"""Durable workflow checkpoints and deterministic reuse fingerprints."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from time import monotonic, sleep


CHECKPOINT_VERSION = 1
_OUTPUT_KEYS = {
    'output', 'output_path', 'output_dir', 'out', 'out_dir', 'report_path',
    'manifest_path', 'result_path', 'result_csv', 'output_csv', 'output_md',
    'output_html', 'variant_output_csv', 'sequence_report_path',
}
_ARTIFACT_KEYS = _OUTPUT_KEYS | {'path', 'report'}


def _encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')


def digest_value(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_output_key(key):
    value = str(key or '').lower()
    return (
        value in _OUTPUT_KEYS
        or value.startswith('output_')
        or value.endswith('_output')
        or value.endswith('_output_path')
    )


def _is_artifact_key(key):
    value = str(key or '').lower()
    return _is_output_key(value) or value in _ARTIFACT_KEYS


def fingerprint_inputs(value, key=None, file_cache=None):
    file_cache = file_cache if file_cache is not None else {}
    if isinstance(value, dict):
        return {
            item_key: fingerprint_inputs(item, item_key, file_cache)
            for item_key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [fingerprint_inputs(item, key, file_cache) for item in value]
    if isinstance(value, str) and not _is_output_key(key):
        path = Path(value)
        if path.is_file():
            resolved = path.resolve()
            stat = resolved.stat()
            cache_key = (str(resolved), stat.st_size, stat.st_mtime_ns)
            digest = file_cache.get(cache_key)
            if digest is None:
                digest = file_sha256(resolved)
                file_cache[cache_key] = digest
            return {
                'path': str(resolved),
                'size': stat.st_size,
                'sha256': digest,
            }
    return value


def workflow_fingerprint(workflow, *, dry_run, allowed_tools):
    return digest_value({
        'workflow': workflow,
        'dry_run': bool(dry_run),
        'allowed_tools': sorted(allowed_tools) if allowed_tools is not None else None,
    })


def step_fingerprint(tool, arguments, spec, dependency_fingerprints, *, dry_run,
                     file_cache=None, environment_fingerprint=None,
                     implementation_fingerprint=None):
    return digest_value({
        'tool': tool,
        'arguments': fingerprint_inputs(arguments, file_cache=file_cache),
        'dependencies': dependency_fingerprints,
        'dry_run': bool(dry_run),
        'plugin_version': spec.get('plugin_version'),
        'plugin_api_version': spec.get('plugin_api_version'),
        'plugin_contract_digest': spec.get('plugin_contract_digest'),
        'environment_fingerprint': environment_fingerprint,
        'implementation_fingerprint': implementation_fingerprint,
    })


def collect_artifacts(value, key=None):
    artifacts = []
    if isinstance(value, dict):
        for item_key, item in value.items():
            artifacts.extend(collect_artifacts(item, item_key))
    elif isinstance(value, list):
        for item in value:
            artifacts.extend(collect_artifacts(item, key))
    elif isinstance(value, str) and _is_artifact_key(key):
        path = Path(value)
        if path.is_file():
            resolved = path.resolve()
            artifacts.append({
                'path': str(resolved),
                'size': resolved.stat().st_size,
                'sha256': file_sha256(resolved),
            })
    unique = {item['path']: item for item in artifacts}
    return [unique[path] for path in sorted(unique)]


def artifacts_valid(artifacts):
    if not isinstance(artifacts, list):
        return False
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not artifact.get('path'):
            return False
        path = Path(artifact['path'])
        if not path.is_file() or path.stat().st_size != artifact.get('size'):
            return False
        if file_sha256(path) != artifact.get('sha256'):
            return False
    return True


def resumable_retry_arguments(arguments, tool_spec):
    updated = dict(arguments)
    properties = tool_spec.get('parameters', {}).get('properties', {})
    output_path = updated.get('output_path')
    if 'resume' in properties and isinstance(output_path, str):
        try:
            checkpoint = CheckpointStore(output_path).load()
        except ValueError:
            checkpoint = None
        if checkpoint is not None:
            updated['resume'] = True
    return updated


class CheckpointStore:
    def __init__(self, path):
        self.path = Path(path) if path else None

    def load(self):
        if self.path is None or not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'invalid workflow checkpoint: {self.path}: {exc}') from exc
        if not isinstance(payload, dict):
            raise ValueError(f'invalid workflow checkpoint: {self.path}')
        version = payload.get('checkpoint_version')
        if version != CHECKPOINT_VERSION:
            raise ValueError(f'unsupported workflow checkpoint version: {version}')
        return payload

    @contextmanager
    def lock(self, timeout_seconds=None):
        if self.path is None:
            yield
            return
        configured = timeout_seconds
        if configured is None:
            try:
                configured = float(os.environ.get('WORKFLOW_CHECKPOINT_LOCK_TIMEOUT', '5'))
            except (TypeError, ValueError):
                configured = 5.0
        lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = monotonic() + max(configured, 0)
        with lock_path.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if monotonic() >= deadline:
                        raise TimeoutError(
                            f'workflow checkpoint is already in use: {self.path}'
                        ) from exc
                    sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def write(self, manifest):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.path.name + '.',
            suffix='.tmp',
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2, default=str)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
