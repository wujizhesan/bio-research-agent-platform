from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import stat
import sys

try:
    from .artifact_store import DirectoryEntry, DirectoryManifest, _check_stat, _checked_stat, _verified_file
    from .job_execution import JobExecutionError
    from .storage_workspace import StorageIntegrityError
except ImportError:
    from artifact_store import DirectoryEntry, DirectoryManifest, _check_stat, _checked_stat, _verified_file
    from job_execution import JobExecutionError
    from storage_workspace import StorageIntegrityError


CHUNK_SIZE = 1024 * 1024
_FD_METADATA = sys.platform == 'linux'


def _capture_directory(source, info):
    if not _FD_METADATA:
        return DirectoryManifest.capture(source)
    entries = []
    pending = [(os.fspath(source), '')]
    while pending:
        directory, parent = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                current = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(current.st_mode):
                    raise StorageIntegrityError('artifact publication rejects symbolic links')
                relative = os.path.join(parent, child.name)
                entries.append((relative, current))
                if stat.S_ISDIR(current.st_mode):
                    pending.append((child.path, relative))
    return DirectoryManifest(source, info, tuple(DirectoryEntry(Path(relative), current) for relative, current in sorted(entries)))


@contextmanager
def _transfer_file(source, expected):
    if not _FD_METADATA:
        with _verified_file(source, expected) as handle:
            yield handle
        return
    try:
        handle = open(source, 'rb', opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW))
    except OSError as exc:
        raise StorageIntegrityError('artifact changed during publication') from exc
    with handle:
        _check_stat(os.fstat(handle.fileno()), expected)
        yield handle
        _check_stat(os.fstat(handle.fileno()), expected)


def _copy_file_metadata(source, destination, original, output, expected, *, root=False):
    if not _FD_METADATA:
        shutil.copystat(source, destination)
        _checked_stat(source, expected)
        return
    source_fd = original.fileno()
    target_fd = output.fileno()
    current = os.fstat(source_fd) if root else expected
    sys.audit('shutil.copystat', source, destination)
    os.utime(target_fd, ns=(current.st_atime_ns, current.st_mtime_ns))
    try:
        names = os.listxattr(source_fd)
    except OSError as exc:
        if exc.errno not in {errno.ENOTSUP, errno.ENODATA, errno.EINVAL}:
            raise
        names = ()
    for name in names:
        try:
            os.setxattr(target_fd, name, os.getxattr(source_fd, name))
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.ENODATA, errno.EINVAL}:
                raise
    os.fchmod(target_fd, stat.S_IMODE(current.st_mode))


def _transfer_error(error):
    message = (
        'plugin workspace transfer rejects symbolic links'
        if 'symbolic links' in str(error) else
        'plugin workspace changed during transfer'
    )
    return JobExecutionError(message)


def _copy_file_data(source, target, size):
    sendfile = getattr(os, 'sendfile', None)
    if sendfile is not None:
        offset = 0
        while offset < size:
            try:
                copied = sendfile(target.fileno(), source.fileno(), offset, min(size - offset, 8 * CHUNK_SIZE))
            except OSError as exc:
                if offset or exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSOCK, errno.EOPNOTSUPP}:
                    raise
                break
            if not copied:
                raise StorageIntegrityError('artifact changed during publication')
            offset += copied
        else:
            source.seek(size)
            if source.read(1):
                raise StorageIntegrityError('artifact changed during publication')
            return
    remaining = size
    buffer = memoryview(bytearray(min(remaining, CHUNK_SIZE)))
    while remaining:
        copied = source.readinto(buffer[:min(remaining, CHUNK_SIZE)])
        if not copied:
            raise StorageIntegrityError('artifact changed during publication')
        target.write(buffer[:copied])
        remaining -= copied
    if source.read(1):
        raise StorageIntegrityError('artifact changed during publication')


def _rollback_transfer_target(target, expected, resolved):
    target = Path(target)
    try:
        current = target.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
            or target.resolve() != resolved
        ):
            return
        if stat.S_ISDIR(current.st_mode):
            target.chmod(current.st_mode | stat.S_IRWXU)
            def writable_remove(function, path, _error):
                selected = Path(path)
                if selected.resolve().is_relative_to(resolved):
                    parent = selected.parent
                    if parent.resolve().is_relative_to(resolved):
                        parent.chmod(parent.lstat().st_mode | stat.S_IRWXU)
                    mode = selected.lstat().st_mode
                    selected.chmod(mode | (stat.S_IRWXU if stat.S_ISDIR(mode) else stat.S_IWRITE))
                    function(path)
            shutil.rmtree(target, onerror=writable_remove)
        else:
            target.chmod(current.st_mode | stat.S_IWRITE)
            target.unlink(missing_ok=True)
    except OSError:
        return


@dataclass(frozen=True)
class WorkspaceTransfer:
    source: Path
    info: os.stat_result
    directory_manifest: DirectoryManifest | None = None

    @classmethod
    def capture(cls, source):
        source = Path(source)
        try:
            info = source.lstat()
        except FileNotFoundError as exc:
            raise JobExecutionError(f'plugin input path is unavailable: {source.name}') from exc
        if stat.S_ISLNK(info.st_mode):
            raise JobExecutionError('plugin workspace transfer rejects symbolic links')
        if stat.S_ISDIR(info.st_mode):
            try:
                manifest = _capture_directory(source, info)
            except StorageIntegrityError as exc:
                raise _transfer_error(exc) from exc
            return cls(source, manifest.root_info, manifest)
        if not stat.S_ISREG(info.st_mode):
            raise JobExecutionError(f'plugin input path is unavailable: {source.name}')
        return cls(source, info)

    @property
    def size_bytes(self):
        if self.directory_manifest is None:
            return self.info.st_size
        return sum(entry.info.st_size for entry in self.directory_manifest.entries if stat.S_ISREG(entry.info.st_mode))

    def copy(self, target, *, directory=False):
        target = Path(target)
        owned = None

        def created(info):
            nonlocal owned
            owned = (target, info, target.resolve())

        def copy_file(source, destination, expected, root=False):
            if not _FD_METADATA:
                source = Path(source)
            with _transfer_file(source, expected) as original, open(destination, 'xb') as output:
                if root:
                    created(os.fstat(output.fileno()))
                _copy_file_data(original, output, expected.st_size)
                output.flush()
                _copy_file_metadata(source, destination, original, output, expected, root=root)

        try:
            _checked_stat(self.source, self.info)
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.directory_manifest is None:
                if directory:
                    raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(self.source))
                copy_file(self.source, target, self.info, root=True)
                _checked_stat(self.source, self.info)
            else:
                manifest = self.directory_manifest
                manifest.check_directories(self.source)
                target.mkdir(mode=0o700)
                created(target.lstat())
                source_root = os.fspath(self.source)
                target_root = os.fspath(target)
                directories = []
                for entry in manifest.entries:
                    relative = os.fspath(entry.relative)
                    source = os.path.join(source_root, relative)
                    destination = os.path.join(target_root, relative)
                    if stat.S_ISDIR(entry.info.st_mode):
                        os.mkdir(destination)
                        directories.append((source, destination))
                    elif stat.S_ISREG(entry.info.st_mode):
                        copy_file(source, destination, entry.info)
                    else:
                        raise JobExecutionError('plugin workspace transfer rejects special files')
                manifest.check_directories(self.source)
                for source, destination in reversed(directories):
                    shutil.copystat(source, destination)
                shutil.copystat(self.source, target)
                manifest.check_directories(self.source)
            return owned
        except BaseException as exc:
            if owned is not None:
                _rollback_transfer_target(*owned)
            if isinstance(exc, StorageIntegrityError):
                raise _transfer_error(exc) from exc
            raise
