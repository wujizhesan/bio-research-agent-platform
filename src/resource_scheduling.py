"""Resource requests, capacity detection, and admission control."""

from dataclasses import dataclass
import math
import os
import re
from threading import Lock


LABEL_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')


def _physical_memory_mb():
    if os.name == 'nt':
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ('length', ctypes.c_ulong),
                ('memory_load', ctypes.c_ulong),
                ('total_physical', ctypes.c_ulonglong),
                ('available_physical', ctypes.c_ulonglong),
                ('total_page_file', ctypes.c_ulonglong),
                ('available_page_file', ctypes.c_ulonglong),
                ('total_virtual', ctypes.c_ulonglong),
                ('available_virtual', ctypes.c_ulonglong),
                ('available_extended_virtual', ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return max(int(status.total_physical / 1024 / 1024), 512)
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        return max(int(pages * page_size / 1024 / 1024), 512)
    except (AttributeError, OSError, ValueError):
        return 8192


def _number(value, name, *, integer=False, minimum=0):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a number')
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a number') from exc
    if not math.isfinite(numeric):
        raise ValueError(f'{name} must be finite')
    if integer and not numeric.is_integer():
        raise ValueError(f'{name} must be an integer')
    parsed = int(numeric) if integer else numeric
    if parsed < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return parsed


def normalize_priority(value):
    priority = _number(value, 'priority', integer=True, minimum=-100)
    if priority > 100:
        raise ValueError('priority must not exceed 100')
    return priority


def _labels(value):
    if value is None:
        return ()
    if isinstance(value, str):
        value = [item.strip() for item in value.split(',') if item.strip()]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError('resource labels must be a list of strings')
    if any(not isinstance(item, str) for item in value):
        raise ValueError('resource labels must be a list of strings')
    labels = tuple(sorted({item.strip() for item in value}))
    if any(not item or not LABEL_PATTERN.fullmatch(item) for item in labels):
        raise ValueError('resource labels contain an invalid value')
    return labels


@dataclass(frozen=True)
class ResourceRequest:
    cpu_cores: float = 1.0
    memory_mb: int = 512
    gpu_count: int = 0
    gpu_memory_mb: int = 0
    labels: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value=None):
        value = value or {}
        if not isinstance(value, dict):
            raise ValueError('resources must be an object')
        allowed = {'cpu_cores', 'memory_mb', 'gpu_count', 'gpu_memory_mb', 'labels'}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError('unknown resource fields: ' + ', '.join(sorted(unknown)))
        request = cls(
            cpu_cores=_number(value.get('cpu_cores', 1), 'cpu_cores', minimum=0.1),
            memory_mb=_number(value.get('memory_mb', 512), 'memory_mb', integer=True, minimum=1),
            gpu_count=_number(value.get('gpu_count', 0), 'gpu_count', integer=True),
            gpu_memory_mb=_number(value.get('gpu_memory_mb', 0), 'gpu_memory_mb', integer=True),
            labels=_labels(value.get('labels')),
        )
        if request.gpu_memory_mb and not request.gpu_count:
            raise ValueError('gpu_memory_mb requires gpu_count greater than zero')
        return request

    def as_dict(self):
        return {
            'cpu_cores': self.cpu_cores,
            'memory_mb': self.memory_mb,
            'gpu_count': self.gpu_count,
            'gpu_memory_mb': self.gpu_memory_mb,
            'labels': list(self.labels),
        }


def merge_requests(required, requested=None):
    base = ResourceRequest.from_mapping(required)
    if requested is None:
        return base
    override = ResourceRequest.from_mapping(requested)
    return ResourceRequest(
        cpu_cores=max(base.cpu_cores, override.cpu_cores),
        memory_mb=max(base.memory_mb, override.memory_mb),
        gpu_count=max(base.gpu_count, override.gpu_count),
        gpu_memory_mb=max(base.gpu_memory_mb, override.gpu_memory_mb),
        labels=tuple(sorted(set(base.labels) | set(override.labels))),
    )


@dataclass(frozen=True)
class ResourceCapacity:
    cpu_cores: float
    memory_mb: int
    gpu_count: int = 0
    gpu_memory_mb: int = 0
    labels: tuple[str, ...] = ()

    @classmethod
    def from_env(cls):
        detected_memory = max(int(_physical_memory_mb() * 0.8), 512)
        return cls(
            cpu_cores=_number(
                os.environ.get('JOB_TOTAL_CPU_CORES', os.cpu_count() or 1),
                'JOB_TOTAL_CPU_CORES',
                minimum=0.1,
            ),
            memory_mb=_number(
                os.environ.get('JOB_TOTAL_MEMORY_MB', detected_memory),
                'JOB_TOTAL_MEMORY_MB',
                integer=True,
                minimum=1,
            ),
            gpu_count=_number(
                os.environ.get('JOB_TOTAL_GPUS', 0),
                'JOB_TOTAL_GPUS',
                integer=True,
            ),
            gpu_memory_mb=_number(
                os.environ.get('JOB_TOTAL_GPU_MEMORY_MB', 0),
                'JOB_TOTAL_GPU_MEMORY_MB',
                integer=True,
            ),
            labels=_labels(os.environ.get('JOB_WORKER_LABELS')),
        )

    def as_dict(self):
        return {
            'cpu_cores': self.cpu_cores,
            'memory_mb': self.memory_mb,
            'gpu_count': self.gpu_count,
            'gpu_memory_mb': self.gpu_memory_mb,
            'labels': list(self.labels),
        }

    def fits(self, request):
        return (
            request.cpu_cores <= self.cpu_cores
            and request.memory_mb <= self.memory_mb
            and request.gpu_count <= self.gpu_count
            and request.gpu_memory_mb <= self.gpu_memory_mb
            and set(request.labels).issubset(self.labels)
        )

    def rejection_reason(self, request):
        missing = []
        if request.cpu_cores > self.cpu_cores:
            missing.append(f'cpu_cores {request.cpu_cores}>{self.cpu_cores}')
        if request.memory_mb > self.memory_mb:
            missing.append(f'memory_mb {request.memory_mb}>{self.memory_mb}')
        if request.gpu_count > self.gpu_count:
            missing.append(f'gpu_count {request.gpu_count}>{self.gpu_count}')
        if request.gpu_memory_mb > self.gpu_memory_mb:
            missing.append(f'gpu_memory_mb {request.gpu_memory_mb}>{self.gpu_memory_mb}')
        labels = sorted(set(request.labels) - set(self.labels))
        if labels:
            missing.append('labels ' + ','.join(labels))
        return '; '.join(missing) or 'resource request is compatible'


class ResourcePool:
    def __init__(self, capacity):
        self.capacity = capacity
        self._available = capacity.as_dict()
        self._lock = Lock()

    def try_acquire(self, request):
        with self._lock:
            available = ResourceCapacity(
                cpu_cores=self._available['cpu_cores'],
                memory_mb=self._available['memory_mb'],
                gpu_count=self._available['gpu_count'],
                gpu_memory_mb=self._available['gpu_memory_mb'],
                labels=tuple(self._available['labels']),
            )
            if not available.fits(request):
                return False
            self._available['cpu_cores'] -= request.cpu_cores
            self._available['memory_mb'] -= request.memory_mb
            self._available['gpu_count'] -= request.gpu_count
            self._available['gpu_memory_mb'] -= request.gpu_memory_mb
            return True

    def release(self, request):
        with self._lock:
            self._available['cpu_cores'] += request.cpu_cores
            self._available['memory_mb'] += request.memory_mb
            self._available['gpu_count'] += request.gpu_count
            self._available['gpu_memory_mb'] += request.gpu_memory_mb

    def snapshot(self):
        with self._lock:
            return {
                'capacity': self.capacity.as_dict(),
                'available': {
                    **self._available,
                    'labels': list(self._available['labels']),
                },
            }
