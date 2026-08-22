"""Local lifecycle state for discoverable domain plugins."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from threading import RLock

try:
    from .config_loader import PROJECT_ROOT
    from .domain_registry import domain_catalog
except ImportError:
    from config_loader import PROJECT_ROOT
    from domain_registry import domain_catalog


_STATE_THREAD_LOCK = RLock()


@contextmanager
def _state_guard(path):
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + '.lock')
    with _STATE_THREAD_LOCK:
        with lock_path.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

STATE_VERSION = 1
DEFAULT_STATE_PATH = PROJECT_ROOT / 'output' / 'plugin_state.json'


class PluginManager:
    def __init__(self, state_path=None, catalog_loader=None):
        self.state_path = Path(state_path or DEFAULT_STATE_PATH)
        self.catalog_loader = catalog_loader or domain_catalog

    def _read_state(self):
        if not self.state_path.exists():
            return {'version': STATE_VERSION, 'plugins': {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'invalid plugin state: {self.state_path}: {exc}')
        if not isinstance(payload, dict) or payload.get('version') != STATE_VERSION:
            raise ValueError(f'unsupported plugin state: {self.state_path}')
        if not isinstance(payload.get('plugins'), dict):
            raise ValueError('plugin state plugins must be a mapping')
        return payload

    def _write_state(self, payload):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=self.state_path.name + '.',
                suffix='.tmp',
                dir=str(self.state_path.parent),
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _catalog_map(self):
        return {item['domain']: item for item in self.catalog_loader()}

    def list(self):
        state = self._read_state()
        result = []
        for item in self.catalog_loader():
            domain = item['domain']
            record = state['plugins'].get(domain, {})
            enabled = bool(record.get('enabled', True)) and item.get('status') == 'available'
            current = dict(item)
            current.update({
                'enabled': enabled,
                'activation': 'enabled' if enabled and item.get('status') == 'available' else 'disabled',
            })
            if record.get('updated_at'):
                current['state_updated_at'] = record['updated_at']
            result.append(current)
        return result

    def get(self, domain):
        for item in self.list():
            if item['domain'] == domain:
                return item
        return None

    def set_enabled(self, domain, enabled):
        item = self._catalog_map().get(domain)
        if item is None:
            raise ValueError(f'unknown plugin domain: {domain}')
        if enabled and item.get('status') != 'available':
            raise ValueError(f'plugin is unavailable: {domain}')
        with _state_guard(self.state_path):
            state = self._read_state()
            state['plugins'][domain] = {
                'enabled': bool(enabled),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            self._write_state(state)
        return self.get(domain)

    def enable(self, domain):
        return self.set_enabled(domain, True)

    def disable(self, domain):
        return self.set_enabled(domain, False)


def is_domain_enabled(domain, state_path=None, catalog_loader=None):
    item = PluginManager(state_path=state_path, catalog_loader=catalog_loader).get(domain)
    return True if item is None else bool(item.get('enabled', True))
