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
    from .plugin_health import assess_plugin_health, validate_candidate
    from .observability import PLUGIN_HEALTH, log_event
except ImportError:
    from config_loader import PROJECT_ROOT
    from plugin_health import assess_plugin_health, validate_candidate
    from observability import PLUGIN_HEALTH, log_event


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


def _default_catalog_loader():
    try:
        from .domain_registry import domain_catalog
    except ImportError:
        from domain_registry import domain_catalog
    return domain_catalog()


def _default_source_loader(domain):
    try:
        from .domain_registry import REGISTRY
    except ImportError:
        from domain_registry import REGISTRY
    registered = REGISTRY.domains.get(domain)
    return registered.source if registered else None


class PluginManager:
    def __init__(self, state_path=None, catalog_loader=None, source_loader=None,
                 failure_threshold=None):
        self.state_path = Path(state_path or DEFAULT_STATE_PATH)
        self.catalog_loader = catalog_loader or _default_catalog_loader
        self.source_loader = source_loader or _default_source_loader
        configured_threshold = failure_threshold or os.environ.get(
            'PLUGIN_HEALTH_FAILURE_THRESHOLD', '3'
        )
        try:
            self.failure_threshold = max(int(configured_threshold), 1)
        except (TypeError, ValueError):
            self.failure_threshold = 3

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
            quarantined = bool(record.get('quarantined', False))
            enabled = (
                bool(record.get('enabled', True))
                and not quarantined
                and item.get('status') == 'available'
            )
            if item.get('status') != 'available':
                activation = 'unavailable'
            elif quarantined:
                activation = 'quarantined'
            elif enabled:
                activation = 'enabled'
            else:
                activation = 'disabled'
            current = dict(item)
            current.update({
                'enabled': enabled,
                'activation': activation,
                'failure_count': int(record.get('failure_count', 0)),
            })
            if record.get('updated_at'):
                current['state_updated_at'] = record['updated_at']
            if record.get('health'):
                current['health'] = dict(record['health'])
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
        health = None
        if enabled:
            health = assess_plugin_health(
                domain,
                self.source_loader(domain),
                item.get('manifest', {
                    'status': item.get('status'),
                    'requirements': [],
                }),
            )
            if not health['healthy']:
                raise ValueError(
                    f"plugin health check failed: {domain}: {health.get('reason', 'unhealthy')}"
                )
        with _state_guard(self.state_path):
            state = self._read_state()
            previous = state['plugins'].get(domain, {})
            record = {
                **previous,
                'enabled': bool(enabled),
                'quarantined': False,
                'failure_count': 0 if enabled else int(previous.get('failure_count', 0)),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            if health:
                record['health'] = health
            state['plugins'][domain] = record
            self._write_state(state)
        current = self.get(domain)
        log_event(
            'plugin.state.changed',
            plugin=domain,
            activation=current.get('activation') if current else None,
        )
        return current

    def enable(self, domain):
        return self.set_enabled(domain, True)

    def disable(self, domain):
        return self.set_enabled(domain, False)

    def validate_candidate(self, manifest):
        return validate_candidate(manifest)

    def _record_health(self, domain, health, auto_disable):
        with _state_guard(self.state_path):
            state = self._read_state()
            previous = state['plugins'].get(domain, {})
            failures = 0 if health['healthy'] else int(previous.get('failure_count', 0)) + 1
            quarantined = bool(previous.get('quarantined', False))
            enabled = bool(previous.get('enabled', True))
            if auto_disable and failures >= self.failure_threshold:
                quarantined = True
                enabled = False
            state['plugins'][domain] = {
                **previous,
                'enabled': enabled,
                'quarantined': quarantined,
                'failure_count': failures,
                'health': health,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            self._write_state(state)
        current = self.get(domain)
        PLUGIN_HEALTH.labels(domain).set(1 if health['healthy'] else 0)
        log_event(
            'plugin.health.checked',
            plugin=domain,
            status=health.get('status'),
            healthy=health.get('healthy'),
            source=health.get('source'),
            quarantined=current.get('activation') == 'quarantined' if current else None,
        )
        return current

    def check_health(self, domain=None, auto_disable=True):
        catalog = self._catalog_map()
        if domain is not None and domain not in catalog:
            raise ValueError(f'unknown plugin domain: {domain}')
        selected = [domain] if domain is not None else list(catalog)
        results = []
        for key in selected:
            item = catalog[key]
            health = assess_plugin_health(
                key,
                self.source_loader(key),
                item.get('manifest', {
                    'status': item.get('status'),
                    'requirements': [],
                }),
            )
            results.append(self._record_health(key, health, auto_disable))
        return results[0] if domain is not None else results

    def record_contract_failure(self, domain, reason):
        item = self._catalog_map().get(domain)
        if item is None:
            raise ValueError(f'unknown plugin domain: {domain}')
        health = {
            'domain': domain,
            'status': 'unhealthy',
            'healthy': False,
            'reason': str(reason),
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'source': 'runtime_contract',
        }
        return self._record_health(domain, health, True)


def is_domain_enabled(domain, state_path=None, catalog_loader=None):
    item = PluginManager(state_path=state_path, catalog_loader=catalog_loader).get(domain)
    return True if item is None else bool(item.get('enabled', True))
