import errno
import os
from pathlib import Path
import shutil
import stat
import tempfile
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.job_execution import JobExecutionError
from src.plugin_container import ContainerToolExecutor, _copy_path
from src import workspace_transfer as transfer
from src.workspace_transfer import WorkspaceTransfer


class WorkspaceTransferTests(unittest.TestCase):
    def executor(self, root, quota=1024):
        return SimpleNamespace(
            workspace_max_bytes=quota, artifact_root=root / 'artifacts', input_roots=(root,),
        )

    def snapshot(self, root):
        paths = [root, *sorted(root.rglob('*'))] if root.is_dir() else [root]
        return [
            (str(path.relative_to(root)), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns,
             None if path.is_dir() else path.read_bytes())
            for path in paths
        ]

    def test_copy_preserves_metadata_unicode_and_empty_directories(self):
        for directory in (False, True):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / '研究输入'
                if directory:
                    (source / '空目录').mkdir(parents=True)
                    (source / '子目录').mkdir()
                    files = [source / '子目录' / '数据.bin', source / '空文件']
                else:
                    files = [source]
                for index, path in enumerate(files):
                    path.write_bytes(b'\x00\xffresearch' if index == 0 else b'')
                    path.chmod(0o640)
                for path in [source, *source.rglob('*')] if directory else [source]:
                    os.utime(path, ns=(1700000000000000000, 1700000000123456000))
                expected = self.snapshot(source)
                manifest = WorkspaceTransfer.capture(source)
                self.assertEqual(manifest.size_bytes, 10)
                _copy_path(source, root / 'target', manifest=manifest)
                self.assertEqual(self.snapshot(root / 'target'), expected)

    def test_partial_copy_and_metadata_errors_rollback_and_allow_retry(self):
        original = transfer._copy_file_data
        for directory in (False, True):
            for phase in ('copy', 'metadata', 'interrupt'):
                with self.subTest(directory=directory, phase=phase), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = root / 'source'
                    if directory:
                        source.mkdir()
                        (source / 'first').write_bytes(b'first')
                        (source / 'second').write_bytes(b'second')
                    else:
                        source.write_bytes(b'first')
                    target = root / 'target'

                    def fail_copy(src, dst, size):
                        dst.write(src.read(1))
                        raise KeyboardInterrupt('cancel') if phase == 'interrupt' else OSError('copy failed')

                    selected = '_copy_file_metadata' if phase == 'metadata' else '_copy_file_data'
                    kwargs = {'side_effect': OSError('metadata failed')} if phase == 'metadata' else {'side_effect': fail_copy}
                    with patch('src.workspace_transfer.' + selected, **kwargs):
                        with self.assertRaises(KeyboardInterrupt if phase == 'interrupt' else OSError):
                            _copy_path(source, target)
                    self.assertFalse(target.exists())
                    _copy_path(source, target)
                    self.assertEqual(self.snapshot(target), self.snapshot(source))
        self.assertIs(transfer._copy_file_data, original)

    def test_multiple_outputs_rollback_readonly_files_and_quota_failure(self):
        for failure in ('copy', 'quota', 'existing'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                first = root / 'first'
                (first / 'nested').mkdir(parents=True)
                readonly = first / 'nested' / 'readonly'
                readonly.write_bytes(b'1234')
                readonly.chmod(0o444)
                (first / 'nested').chmod(0o555)
                first.chmod(0o555)
                second = root / 'second'
                second.write_bytes(b'5678')
                target = root / 'artifacts'
                target.mkdir()
                if failure == 'existing':
                    (target / 'second').write_bytes(b'foreign')
                executor = self.executor(root, 7 if failure == 'quota' else 8)
                original = transfer._copy_file_data

                def selected_copy(src, dst, size):
                    if Path(src.name) == second:
                        dst.write(b'partial')
                        raise OSError('second failed')
                    original(src, dst, size)

                with patch('src.workspace_transfer._copy_file_data', side_effect=selected_copy if failure == 'copy' else original):
                    with self.assertRaises(OSError if failure == 'copy' else JobExecutionError):
                        ContainerToolExecutor._publish_outputs(executor, [
                            (str(first), str(target / 'first'), True), (str(second), str(target / 'second'), False),
                        ])
                self.assertFalse((target / 'first').exists())
                if failure == 'existing':
                    self.assertEqual((target / 'second').read_bytes(), b'foreign')
                else:
                    self.assertFalse((target / 'second').exists())
                first.chmod(0o755)
                (first / 'nested').chmod(0o755)
                readonly.chmod(0o644)

    def test_creation_race_and_replaced_target_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            source.write_bytes(b'data')
            target = root / 'target'
            manifest = WorkspaceTransfer.capture(source)
            target.write_bytes(b'foreign')
            with self.assertRaises(FileExistsError):
                manifest.copy(target)
            self.assertEqual(target.read_bytes(), b'foreign')
            target.unlink()
            source_dir = root / 'source-dir'
            source_dir.mkdir()
            original_mkdir = Path.mkdir

            def racing_mkdir(path, *args, **kwargs):
                if path == target:
                    original_mkdir(path)
                    (path / 'foreign').write_bytes(b'foreign')
                return original_mkdir(path, *args, **kwargs)

            with patch.object(Path, 'mkdir', racing_mkdir), self.assertRaises(FileExistsError):
                _copy_path(source_dir, target)
            self.assertEqual((target / 'foreign').read_bytes(), b'foreign')
            shutil.rmtree(target)
            owned = _copy_path(source, target)
            target.rename(root / 'moved')
            target.write_bytes(b'new owner')
            transfer._rollback_transfer_target(*owned)
            self.assertEqual(target.read_bytes(), b'new owner')
            self.assertEqual((root / 'moved').read_bytes(), b'data')

    def test_capture_and_mid_copy_file_changes_are_rejected(self):
        for mutation in ('grow', 'truncate', 'rewrite', 'delete', 'replace'):
            for during in (False, True):
                with self.subTest(mutation=mutation, during=during), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source = root / 'source'
                    source.write_bytes(b'original')
                    manifest = WorkspaceTransfer.capture(source)

                    def mutate():
                        if mutation == 'delete':
                            source.unlink()
                        elif mutation == 'replace':
                            replacement = root / 'replacement'
                            replacement.write_bytes(b'original')
                            replacement.replace(source)
                        else:
                            source.write_bytes({'grow': b'original!', 'truncate': b'x', 'rewrite': b'modified'}[mutation])
                            os.utime(source, ns=(manifest.info.st_atime_ns, manifest.info.st_mtime_ns + 10000000))

                    original = transfer._copy_file_data

                    def copy_and_mutate(src, dst, size):
                        original(src, dst, size)
                        mutate()

                    if not during:
                        mutate()
                    with patch('src.workspace_transfer._copy_file_data', side_effect=copy_and_mutate if during else original):
                        with self.assertRaises((JobExecutionError, OSError)):
                            manifest.copy(root / 'target')
                    self.assertFalse((root / 'target').exists())

    def test_directory_changes_and_metadata_phase_changes_are_rejected(self):
        for mutation in ('new-entry', 'delete-entry', 'file-metadata', 'directory-replace'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'source'
                (source / 'nested').mkdir(parents=True)
                file = source / 'nested' / 'file'
                file.write_bytes(b'data')
                manifest = WorkspaceTransfer.capture(source)
                original = transfer._copy_file_metadata
                changed = []

                def mutate_metadata(src, dst, *args, **kwargs):
                    result = original(src, dst, *args, **kwargs)
                    if Path(src) == file and not changed:
                        changed.append(True)
                        sleep(0.02)
                        if mutation == 'new-entry':
                            (source / 'nested' / 'new').write_bytes(b'new')
                        elif mutation == 'delete-entry':
                            file.unlink()
                        elif mutation == 'file-metadata':
                            info = file.stat()
                            os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns + 10000000))
                        else:
                            (source / 'nested').rename(source / 'old')
                            (source / 'nested').mkdir()
                    return result

                with patch('src.workspace_transfer._copy_file_metadata', side_effect=mutate_metadata):
                    with self.assertRaises((JobExecutionError, OSError)):
                        manifest.copy(root / 'target')
                self.assertFalse((root / 'target').exists())

    def test_symlinks_special_files_and_directory_hint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            source.write_bytes(b'data')
            with self.assertRaises(NotADirectoryError):
                WorkspaceTransfer.capture(source).copy(root / 'target', directory=True)
            self.assertFalse((root / 'target').exists())
            if hasattr(os, 'mkfifo'):
                source_dir = root / 'fifo-dir'
                source_dir.mkdir()
                os.mkfifo(source_dir / 'fifo')
                with self.assertRaisesRegex(JobExecutionError, 'special files'):
                    _copy_path(source_dir, root / 'target')
                self.assertFalse((root / 'target').exists())
            for directory in (False, True):
                for broken in (False, True):
                    with self.subTest(directory=directory, broken=broken):
                        link = root / 'link'
                        link.symlink_to(root / ('missing' if broken else 'source'), target_is_directory=directory)
                        with self.assertRaisesRegex(JobExecutionError, 'symbolic links'):
                            WorkspaceTransfer.capture(link)
                        link.unlink()
            source_dir = root / 'source-dir'
            source_dir.mkdir()
            (source_dir / 'link').symlink_to(source)
            with self.assertRaisesRegex(JobExecutionError, 'symbolic links'):
                WorkspaceTransfer.capture(source_dir)

    def test_symlink_inserted_after_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            (source / 'nested').mkdir(parents=True)
            (source / 'nested' / 'file').write_bytes(b'data')
            manifest = WorkspaceTransfer.capture(source)
            (source / 'nested').rename(root / 'outside')
            (source / 'nested').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(JobExecutionError):
                manifest.copy(root / 'target')
            self.assertFalse((root / 'target').exists())

    def test_quota_boundaries_mapping_and_manifest_reuse(self):
        for collection in (list, tuple):
            for quota in (7, 8):
                with self.subTest(collection=collection, quota=quota), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    (root / 'workspace').mkdir()
                    source = root / 'source'
                    source.mkdir()
                    (source / 'empty').mkdir()
                    (source / 'file').write_bytes(b'data')
                    inputs = collection([str(source), str(source), 42])
                    executor = self.executor(root, quota)
                    contract = ({'input_dir'}, {}, set())
                    with patch('src.plugin_container._tool_filesystem_contract', return_value=contract):
                        if quota == 7:
                            with self.assertRaisesRegex(JobExecutionError, 'input quota'):
                                ContainerToolExecutor._stage_workspace(executor, 'demo', {'input_dir': inputs}, root / 'workspace')
                        else:
                            with patch.object(WorkspaceTransfer, 'capture', wraps=WorkspaceTransfer.capture) as capture:
                                selected, outputs = ContainerToolExecutor._stage_workspace(executor, 'demo', {'input_dir': inputs}, root / 'workspace')
                            self.assertEqual(capture.call_count, 2)
                            self.assertIsInstance(selected['input_dir'], collection)
                            self.assertEqual(selected['input_dir'][2], 42)
                            self.assertEqual(outputs, ())
                            for path in selected['input_dir'][:2]:
                                self.assertEqual(self.snapshot(Path(path)), self.snapshot(source))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            empty = root / 'empty'
            empty.mkdir()
            target = root / 'target'
            target.mkdir()
            self.assertEqual(WorkspaceTransfer.capture(empty).size_bytes, 0)
            replacements = ContainerToolExecutor._publish_outputs(self.executor(root, 0), [(str(empty), str(target), True)])
            self.assertEqual(replacements, ((str(empty), str(target)),))
            self.assertTrue(target.is_dir())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'artifacts' / 'result'
            source.parent.mkdir()
            source.write_bytes(b'data')
            workspace = root / 'workspace'
            workspace.mkdir()
            executor = self.executor(root, 4)
            with patch('src.plugin_container._tool_filesystem_contract', return_value=({'result'}, {'result': 'file'}, {'result'})):
                selected, outputs = ContainerToolExecutor._stage_workspace(executor, 'demo', {'result': str(source)}, workspace)
            self.assertEqual(Path(selected['result']).read_bytes(), b'data')
            self.assertEqual(outputs, ((selected['result'], str(source), False),))
            source.unlink()
            ContainerToolExecutor._publish_outputs(executor, outputs)
            self.assertEqual(source.read_bytes(), b'data')

    def test_native_short_copies_fallback_and_partial_failure(self):
        for scenario in ('short', 'unsupported', 'partial-failure', 'early-eof'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'source'
                source.write_bytes(b'0123456789')
                calls = []

                def sendfile(dst, src, offset, count):
                    calls.append((offset, count))
                    if scenario == 'unsupported':
                        raise OSError(errno.ENOSYS, 'unsupported')
                    if scenario == 'early-eof':
                        return 0
                    if scenario == 'partial-failure' and offset:
                        raise OSError(errno.ENOSPC, 'disk full')
                    os.lseek(src, offset, os.SEEK_SET)
                    return os.write(dst, os.read(src, min(3, count)))

                with patch.object(transfer.os, 'sendfile', side_effect=sendfile, create=True):
                    if scenario in ('partial-failure', 'early-eof'):
                        with self.assertRaises((OSError, JobExecutionError)):
                            _copy_path(source, root / 'target')
                        self.assertFalse((root / 'target').exists())
                    else:
                        _copy_path(source, root / 'target')
                        self.assertEqual((root / 'target').read_bytes(), source.read_bytes())
                self.assertTrue(calls)

    def test_fallback_reads_expected_size_and_rejects_growth(self):
        from io import BytesIO

        class Reader(BytesIO):
            def readinto(self, buffer):
                self.asserted_counts.append(len(buffer))
                return super().readinto(buffer)

            def read(self, count=-1):
                self.asserted_counts.append(count)
                return super().read(count)

        source = Reader(b'x' * (transfer.CHUNK_SIZE + 5) + b'extra')
        source.asserted_counts = []
        output = BytesIO()
        with patch.object(transfer.os, 'sendfile', None, create=True):
            with self.assertRaises(transfer.StorageIntegrityError):
                transfer._copy_file_data(source, output, transfer.CHUNK_SIZE + 5)
        self.assertEqual(len(output.getvalue()), transfer.CHUNK_SIZE + 5)
        self.assertEqual(source.asserted_counts, [transfer.CHUNK_SIZE, 5, 1])

    @unittest.skipUnless(transfer._FD_METADATA, 'Linux descriptor metadata')
    def test_descriptor_metadata_preserves_xattrs_and_checks_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            source.write_bytes(b'data')
            try:
                os.setxattr(source, 'user.research', b'\x00research\xff')
            except OSError as exc:
                if exc.errno in {errno.ENOTSUP, errno.EPERM}:
                    self.skipTest('filesystem does not support user xattrs')
                raise
            source.chmod(0o440)
            target = root / 'target'
            _copy_path(source, target)
            self.assertEqual(os.getxattr(target, 'user.research'), b'\x00research\xff')
            self.assertEqual(target.stat().st_atime_ns, source.stat().st_atime_ns)
            self.assertEqual(self.snapshot(target), self.snapshot(source))
            target.chmod(0o640)
            target.unlink()
            source.chmod(0o640)
            original = transfer._copy_file_metadata

            def change_attribute(src, dst, *args, **kwargs):
                original(src, dst, *args, **kwargs)
                sleep(0.02)
                os.setxattr(src, 'user.research', b'changed')

            with patch('src.workspace_transfer._copy_file_metadata', side_effect=change_attribute):
                with self.assertRaises(JobExecutionError):
                    _copy_path(source, target)
            self.assertFalse(target.exists())
            source.chmod(0o640)


if __name__ == '__main__':
    unittest.main()
