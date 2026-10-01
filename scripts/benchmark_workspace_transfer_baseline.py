from pathlib import Path
import shutil

from src.job_execution import JobExecutionError
from src.plugin_container import (
    ContainerToolExecutor, _tool_filesystem_contract, _within, _safe_segment, _map_paths,
)


BASELINE_SOURCE_COMMIT = 'f983869f38f1e5eb69bbdae61fba1f4febd74ad2'


def _reject_symlinks(path):
    path = Path(path)
    candidates = [path]
    if path.is_dir():
        candidates.extend(path.rglob('*'))
    if any(candidate.is_symlink() for candidate in candidates):
        raise JobExecutionError('plugin workspace transfer rejects symbolic links')


def _tree_size(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def _copy_path(source, target):
    source = Path(source)
    target = Path(target)
    _reject_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, target)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise JobExecutionError(f'plugin input path is unavailable: {source.name}')


class LegacyWorkspaceExecutor(ContainerToolExecutor):
    def _stage_workspace(self, tool, arguments, workspace):
        reads, artifact_kinds, writes = _tool_filesystem_contract(tool)
        selected = dict(arguments)
        outputs = []
        transferred = _tree_size(workspace)
        for parameter in sorted(reads | writes):
            if parameter not in selected:
                continue
            index = 0

            def stage(raw):
                nonlocal index, transferred
                source = Path(raw).expanduser().resolve(strict=False)
                name = _safe_segment(source.name, 'path')
                parameter_dir = _safe_segment(parameter, 'argument')
                if parameter in writes:
                    if not _within(source, (self.artifact_root,)):
                        raise JobExecutionError(
                            f'plugin output path is outside the artifact root: {parameter}'
                        )
                    staged = workspace / 'outputs' / parameter_dir / str(index) / name
                    directory = artifact_kinds.get(parameter) == 'directory' or (
                        parameter.lower().endswith(('_dir', '_directory'))
                    )
                    if parameter in reads and source.exists():
                        transferred += _tree_size(source)
                        if transferred > self.workspace_max_bytes:
                            raise JobExecutionError(
                                'plugin workspace input quota exceeded'
                            )
                        _copy_path(source, staged)
                    elif directory:
                        staged.mkdir(parents=True)
                    else:
                        staged.parent.mkdir(parents=True, exist_ok=True)
                    outputs.append((str(staged), str(source), directory))
                else:
                    if _within(source, (workspace,)):
                        staged = source
                    else:
                        if not _within(source, self.input_roots):
                            raise JobExecutionError(
                                f'plugin input path is outside allowed roots: {parameter}'
                            )
                        transferred += _tree_size(source)
                        if transferred > self.workspace_max_bytes:
                            raise JobExecutionError(
                                'plugin workspace input quota exceeded'
                            )
                        staged = workspace / 'inputs' / parameter_dir / str(index) / name
                        _copy_path(source, staged)
                index += 1
                return str(staged)

            selected[parameter] = _map_paths(selected[parameter], stage)
        return selected, tuple(outputs)

    def _publish_outputs(self, outputs):
        total = 0
        replacements = []
        published = []
        try:
            for staged_raw, target_raw, directory in outputs:
                staged = Path(staged_raw)
                target = Path(target_raw)
                if not staged.exists():
                    continue
                _reject_symlinks(staged)
                total += _tree_size(staged)
                if total > self.workspace_max_bytes:
                    raise JobExecutionError('plugin workspace output quota exceeded')
                if target.exists():
                    if target.is_dir() and not any(target.iterdir()):
                        target.rmdir()
                    else:
                        raise JobExecutionError(
                            f'plugin artifact target already exists: {target.name}'
                        )
                target.parent.mkdir(parents=True, exist_ok=True)
                if directory or staged.is_dir():
                    shutil.copytree(staged, target)
                else:
                    shutil.copy2(staged, target)
                published.append(target)
                replacements.append((str(staged), str(target)))
        except Exception:
            for target in reversed(published):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            raise
        return tuple(replacements)
