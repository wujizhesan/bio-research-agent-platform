"""Runtime security policy for third-party scientific plugins."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from threading import RLock

try:
    from .observability import PLUGIN_SECURITY_DECISIONS, log_event
except ImportError:
    from observability import PLUGIN_SECURITY_DECISIONS, log_event


SECURITY_PROFILE_VERSION = 1
TRUSTED_PLUGIN_KINDS = frozenset({
    'builtin', 'builtin_adapter', 'application', 'external',
})
_HOST_PATTERN = re.compile(
    r'^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*'
    r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$'
)
_EXECUTABLE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$')
_ENVIRONMENT_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]{0,127}$')
_SHELL_EXECUTABLES = frozenset({
    'sh', 'bash', 'dash', 'zsh', 'cmd', 'cmd.exe', 'powershell',
    'powershell.exe', 'pwsh', 'pwsh.exe',
})
_POLICY = ContextVar('bio_agent_plugin_security_policy', default=None)
_AUDIT_HOOK_INSTALLED = False
_IMPORT_ENVIRONMENT_LOCK = RLock()


class PluginSecurityError(PermissionError):
    pass


def _string_list(value, label):
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f'{label} must be a list of non-empty strings')
    cleaned = [item.strip() for item in value]
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f'{label} must not contain duplicates')
    return cleaned


def normalize_permissions(value, parameters=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('plugin tool permissions must be a mapping')
    unknown = set(value) - {'filesystem', 'network', 'subprocess', 'environment'}
    if unknown:
        raise ValueError('unsupported plugin permissions: ' + ', '.join(sorted(unknown)))
    filesystem = value.get('filesystem') or {}
    if not isinstance(filesystem, dict):
        raise ValueError('plugin filesystem permissions must be a mapping')
    if set(filesystem) - {'read', 'write'}:
        raise ValueError('plugin filesystem permissions only support read and write')
    read = _string_list(filesystem.get('read'), 'filesystem read parameters')
    write = _string_list(filesystem.get('write'), 'filesystem write parameters')
    properties = (parameters or {}).get('properties', {})
    if isinstance(properties, dict):
        unknown_parameters = (set(read) | set(write)) - set(properties)
        if unknown_parameters:
            raise ValueError(
                'filesystem permissions reference unknown parameters: '
                + ', '.join(sorted(unknown_parameters))
            )
    hosts = [item.lower() for item in _string_list(
        value.get('network'), 'network hosts'
    )]
    for host in hosts:
        if host == '*' or '://' in host or '/' in host or not _HOST_PATTERN.fullmatch(host):
            raise ValueError(f'invalid network host permission: {host}')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified
        ):
            raise ValueError(f'unsafe network host permission: {host}')
    executables = _string_list(value.get('subprocess'), 'subprocess executables')
    for executable in executables:
        if (
            Path(executable).name != executable
            or not _EXECUTABLE_PATTERN.fullmatch(executable)
            or executable.lower() in _SHELL_EXECUTABLES
        ):
            raise ValueError(f'unsafe subprocess executable permission: {executable}')
    environment = _string_list(value.get('environment'), 'environment variables')
    for name in environment:
        if not _ENVIRONMENT_PATTERN.fullmatch(name):
            raise ValueError(f'invalid environment variable permission: {name}')
    return {
        'filesystem': {'read': read, 'write': write},
        'network': hosts,
        'subprocess': executables,
        'environment': environment,
    }


def security_profile(kind):
    return {
        'profile_version': SECURITY_PROFILE_VERSION,
        'trust': 'trusted' if kind in TRUSTED_PLUGIN_KINDS else 'sandboxed',
        'execution': 'platform' if kind in TRUSTED_PLUGIN_KINDS else 'isolated_process',
    }


def validate_security_profile(profile, kind):
    if not isinstance(profile, dict):
        raise ValueError('plugin security profile must be a mapping')
    if set(profile) != {'profile_version', 'trust', 'execution'}:
        raise ValueError('plugin security profile has unsupported fields')
    if profile.get('profile_version') != SECURITY_PROFILE_VERSION:
        raise ValueError('unsupported plugin security profile version')
    expected = security_profile(kind)
    if profile != expected:
        raise ValueError('plugin security profile does not match plugin kind')
    return profile


def _configured_set(name):
    return {
        item.strip().lower()
        for item in os.environ.get(name, '').split(',')
        if item.strip()
    }


def _values(arguments, names):
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str):
            yield name, value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield name, item


def _resolved(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PluginSecurityError('plugin path permission contains an invalid path') from exc


def _network_addresses(hosts):
    addresses = set()
    for host in hosts:
        try:
            for result in socket.getaddrinfo(host, None):
                addresses.add(str(result[4][0]).lower())
        except OSError:
            continue
    return addresses


@dataclass(frozen=True)
class PluginPolicy:
    domain: str
    read_files: frozenset
    read_roots: tuple
    write_files: frozenset
    write_roots: tuple
    network_hosts: frozenset
    network_addresses: frozenset
    executables: frozenset


def _is_within(path, roots):
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def build_policy(domain, permissions, arguments):
    read_files = set()
    read_roots = []
    write_files = set()
    write_roots = []
    filesystem = permissions['filesystem']
    for _name, raw in _values(arguments, filesystem['read']):
        path = _resolved(raw)
        if path.is_dir():
            read_roots.append(path)
        else:
            read_files.add(path)
    for name, raw in _values(arguments, filesystem['write']):
        path = _resolved(raw)
        if path.is_dir() or name.lower().endswith(('_dir', 'directory')):
            write_roots.append(path)
        else:
            write_files.add(path)
    temporary = _resolved(tempfile.gettempdir())
    read_roots.extend((temporary, _resolved(sys.prefix), _resolved(sys.base_prefix)))
    write_roots.append(temporary)
    requested_hosts = set(permissions['network'])
    granted_hosts = _configured_set('PLUGIN_ALLOWED_NETWORK_HOSTS')
    network_hosts = requested_hosts & granted_hosts
    requested_executables = {item.lower() for item in permissions['subprocess']}
    granted_executables = _configured_set('PLUGIN_ALLOWED_EXECUTABLES')
    executables = requested_executables & granted_executables
    return PluginPolicy(
        domain=domain,
        read_files=frozenset(read_files),
        read_roots=tuple(dict.fromkeys(read_roots)),
        write_files=frozenset(write_files),
        write_roots=tuple(dict.fromkeys(write_roots)),
        network_hosts=frozenset(network_hosts),
        network_addresses=frozenset(_network_addresses(network_hosts)),
        executables=frozenset(executables),
    )


def _deny(policy, capability):
    PLUGIN_SECURITY_DECISIONS.labels(policy.domain, capability, 'denied').inc()
    log_event(
        'plugin.security.denied',
        plugin=policy.domain,
        capability=capability,
    )
    raise PluginSecurityError(
        f'plugin security policy denied {capability} for domain {policy.domain}'
    )


def _open_is_write(mode, flags):
    if isinstance(mode, str) and any(item in mode for item in 'wax+'):
        return True
    if isinstance(flags, int):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & write_flags)
    return False


def _check_read(policy, raw):
    path = _resolved(os.fsdecode(raw))
    if path not in policy.read_files and not _is_within(path, policy.read_roots):
        _deny(policy, 'filesystem.read')


def _check_write(policy, raw):
    path = _resolved(os.fsdecode(raw))
    if path not in policy.write_files and not _is_within(path, policy.write_roots):
        _deny(policy, 'filesystem.write')


def _audit(event, args):
    policy = _POLICY.get()
    if policy is None:
        return
    if event == 'open':
        raw = args[0] if args else None
        if isinstance(raw, int) or raw is None:
            return
        path = _resolved(os.fsdecode(raw))
        writing = _open_is_write(
            args[1] if len(args) > 1 else None,
            args[2] if len(args) > 2 else None,
        )
        if writing:
            _check_write(policy, path)
        else:
            _check_read(policy, path)
    elif event in {'os.listdir', 'os.scandir', 'os.chdir'} and args:
        _check_read(policy, args[0])
    elif event in {
        'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown',
        'os.truncate', 'os.unlink',
    } and args:
        _check_write(policy, args[0])
    elif event in {'os.rename', 'os.replace', 'os.link', 'os.symlink'}:
        if args:
            _check_write(policy, args[0])
        if len(args) > 1:
            _check_write(policy, args[1])
    elif event == 'socket.getaddrinfo':
        host = str(args[0] if args else '').lower()
        if host not in policy.network_hosts:
            _deny(policy, 'network.resolve')
    elif event == 'socket.connect':
        address = args[1] if len(args) > 1 else None
        host = str(address[0]).lower() if isinstance(address, tuple) and address else ''
        if host not in policy.network_hosts and host not in policy.network_addresses:
            _deny(policy, 'network.connect')
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            parsed = None
        if parsed is not None and (
            parsed.is_private or parsed.is_loopback or parsed.is_link_local
            or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified
        ):
            _deny(policy, 'network.private_address')
    elif event == 'socket.sendto':
        address = next(
            (item for item in reversed(args) if isinstance(item, tuple)),
            None,
        )
        host = str(address[0]).lower() if address else ''
        if host not in policy.network_hosts and host not in policy.network_addresses:
            _deny(policy, 'network.send')
    elif event == 'socket.bind':
        _deny(policy, 'network.bind')
    elif event == 'subprocess.Popen':
        executable = Path(str(args[0] if args else '')).name.lower()
        if executable not in policy.executables or executable in _SHELL_EXECUTABLES:
            _deny(policy, 'subprocess.execute')
    elif event == 'os.system':
        _deny(policy, 'subprocess.shell')
    elif event in {'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty'}:
        _deny(policy, 'subprocess.escape')
    elif event.startswith('ctypes.'):
        _deny(policy, 'native_library.load')
    elif event.startswith('winreg.'):
        _deny(policy, 'system.registry')


def install_audit_hook():
    global _AUDIT_HOOK_INSTALLED
    if not _AUDIT_HOOK_INSTALLED:
        sys.addaudithook(_audit)
        _AUDIT_HOOK_INSTALLED = True


@contextmanager
def enforce_plugin_import_boundary(domain, package_roots=()):
    install_audit_hook()
    temporary = _resolved(tempfile.gettempdir())
    read_roots = [temporary, _resolved(sys.prefix), _resolved(sys.base_prefix)]
    read_roots.extend(_resolved(root) for root in package_roots if root)
    policy = PluginPolicy(
        domain=domain,
        read_files=frozenset(),
        read_roots=tuple(dict.fromkeys(read_roots)),
        write_files=frozenset(),
        write_roots=(temporary,),
        network_hosts=frozenset(),
        network_addresses=frozenset(),
        executables=frozenset(),
    )
    safe_names = {
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'PYTHONUTF8', 'PYTHONIOENCODING',
    }
    with _IMPORT_ENVIRONMENT_LOCK:
        original_environment = dict(os.environ)
        sanitized = {
            name: value
            for name, value in original_environment.items()
            if name in safe_names
        }
        token = _POLICY.set(policy)
        os.environ.clear()
        os.environ.update(sanitized)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(original_environment)
            _POLICY.reset(token)


@contextmanager
def enforce_plugin_boundary(domain, security, permissions, arguments):
    if security.get('trust') == 'trusted':
        yield
        return
    if (
        os.environ.get('PLUGIN_SANDBOX_PROCESS') != '1'
        and os.environ.get('PLUGIN_ALLOW_UNTRUSTED_INLINE', '').lower()
        not in {'1', 'true', 'yes'}
    ):
        PLUGIN_SECURITY_DECISIONS.labels(
            domain, 'execution.inline', 'denied'
        ).inc()
        log_event(
            'plugin.security.denied',
            plugin=domain,
            capability='execution.inline',
        )
        raise PluginSecurityError(
            'third-party plugins require isolated process execution'
        )
    install_audit_hook()
    policy = build_policy(domain, permissions, arguments)
    PLUGIN_SECURITY_DECISIONS.labels(domain, 'execution', 'allowed').inc()
    log_event('plugin.security.allowed', plugin=domain, capability='execution')
    token = _POLICY.set(policy)
    try:
        yield
    finally:
        _POLICY.reset(token)


def sandbox_environment(base_environment, security, permissions):
    if security.get('trust') == 'trusted':
        return None
    safe_names = {
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'PYTHONUTF8', 'PYTHONIOENCODING',
    }
    allowed_environment = {
        item.strip().upper()
        for item in os.environ.get('PLUGIN_ALLOWED_ENV_VARS', '').split(',')
        if item.strip()
    }
    safe_names.update(
        name for name in permissions['environment'] if name in allowed_environment
    )
    environment = {
        name: value for name, value in base_environment.items() if name in safe_names
    }
    allowed_hosts = _configured_set('PLUGIN_ALLOWED_NETWORK_HOSTS')
    requested_hosts = set(permissions['network'])
    allowed_executables = _configured_set('PLUGIN_ALLOWED_EXECUTABLES')
    requested_executables = {
        item.lower() for item in permissions['subprocess']
    }
    environment['PLUGIN_ALLOWED_NETWORK_HOSTS'] = ','.join(
        sorted(requested_hosts & allowed_hosts)
    )
    environment['PLUGIN_ALLOWED_EXECUTABLES'] = ','.join(
        sorted(requested_executables & allowed_executables)
    )
    environment['PLUGIN_ALLOWED_ENV_VARS'] = ','.join(sorted(
        set(permissions['environment']) & allowed_environment
    ))
    environment['PLUGIN_SANDBOX_PROCESS'] = '1'
    return environment


def permission_grant_report(manifest):
    security = manifest['security']
    if security.get('trust') == 'trusted':
        return {
            'approved': True,
            'requested': {'network': [], 'subprocess': [], 'environment': []},
            'denied': {'network': [], 'subprocess': [], 'environment': []},
        }
    contracts = manifest['tool_contracts'].values()
    requested = {
        'network': sorted({
            host for contract in contracts for host in contract['permissions']['network']
        }),
        'subprocess': sorted({
            executable.lower()
            for contract in manifest['tool_contracts'].values()
            for executable in contract['permissions']['subprocess']
        }),
        'environment': sorted({
            name
            for contract in manifest['tool_contracts'].values()
            for name in contract['permissions']['environment']
        }),
    }
    granted = {
        'network': _configured_set('PLUGIN_ALLOWED_NETWORK_HOSTS'),
        'subprocess': _configured_set('PLUGIN_ALLOWED_EXECUTABLES'),
        'environment': {
            item.strip().upper()
            for item in os.environ.get('PLUGIN_ALLOWED_ENV_VARS', '').split(',')
            if item.strip()
        },
    }
    denied = {
        capability: sorted(set(values) - granted[capability])
        for capability, values in requested.items()
    }
    return {
        'approved': not any(denied.values()),
        'requested': requested,
        'denied': denied,
    }
