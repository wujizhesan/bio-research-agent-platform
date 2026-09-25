"""Execution semantics and staged artifact publication."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


VALID_EXECUTION_SEMANTICS = frozenset({'pure', 'idempotent', 'side_effecting'})
VALID_ARTIFACT_KINDS = frozenset({'file', 'directory'})
_OUTPUT_PARAMETERS = frozenset({
    'output_dir',
    'output_path',
    'output_csv',
    'output_md',
    'output_html',
    'output_vcf',
    'output_alignment_paths',
    'report_path',
})
_PATH_PARAMETER_NAMES = frozenset({'path'})
_PATH_PARAMETER_SUFFIXES = (
    '_path', '_paths', '_dir', '_directory', '_csv', '_tsv', '_gtf',
    '_fasta', '_vcf', '_bam', '_mtx', '_html', '_md',
)


def output_parameters(parameters):
    properties = (parameters or {}).get('properties', {})
    if not isinstance(properties, dict):
        return ()
    return tuple(sorted(set(properties) & _OUTPUT_PARAMETERS))


def workspace_path_contract(spec):
    spec = spec or {}
    filesystem = (spec.get('permissions') or {}).get('filesystem') or {}
    reads = set(filesystem.get('read') or ())
    writes = set(filesystem.get('write') or ())
    artifacts = {
        item['argument']: item['kind']
        for item in normalize_artifact_contracts(
            spec.get('artifacts'), spec.get('parameters')
        )
    }
    writes.update(artifacts)
    properties = (spec.get('parameters') or {}).get('properties') or {}
    for name in properties:
        lowered = name.lower()
        if (
            name not in writes
            and name not in reads
            and (
                lowered in _PATH_PARAMETER_NAMES
                or lowered.endswith(_PATH_PARAMETER_SUFFIXES)
            )
        ):
            reads.add(name)
    return reads, artifacts, writes


def normalize_artifact_contracts(value=None, parameters=None):
    properties = (parameters or {}).get('properties', {})
    if not isinstance(properties, dict):
        properties = {}
    if value is None:
        value = [
            {
                'argument': argument,
                'kind': 'directory' if _is_directory_parameter(argument) else 'file',
            }
            for argument in output_parameters(parameters)
        ]
    if not isinstance(value, (list, tuple)):
        raise ValueError('artifacts must be a list')
    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('artifact contract must be a mapping')
        argument = str(item.get('argument') or '').strip()
        if argument not in properties:
            raise ValueError(f'artifact argument is not declared in parameters: {argument}')
        if argument in seen:
            raise ValueError(f'duplicate artifact argument: {argument}')
        kind = str(item.get('kind') or '').strip().lower()
        if kind not in VALID_ARTIFACT_KINDS:
            raise ValueError('artifact kind must be file or directory')
        publish = str(item.get('publish') or 'atomic').strip().lower()
        if publish != 'atomic':
            raise ValueError('artifact publish policy must be atomic')
        overwrite = str(item.get('overwrite') or 'deny').strip().lower()
        if overwrite != 'deny':
            raise ValueError('artifact overwrite policy must be deny')
        required = item.get('required', argument in (parameters or {}).get('required', ()))
        if not isinstance(required, bool):
            raise ValueError('artifact required flag must be boolean')
        normalized.append({
            'argument': argument,
            'kind': kind,
            'publish': publish,
            'overwrite': overwrite,
            'required': required,
        })
        seen.add(argument)
    return tuple(normalized)


def normalize_execution_semantics(
    value=None,
    parameters=None,
    permissions=None,
    artifacts=None,
):
    declared_artifacts = normalize_artifact_contracts(artifacts, parameters)
    if value is not None:
        selected = str(value).strip().lower()
        if selected not in VALID_EXECUTION_SEMANTICS:
            raise ValueError(
                'execution_semantics must be pure, idempotent or side_effecting'
            )
        if selected == 'pure' and declared_artifacts:
            raise ValueError('pure tools cannot declare output artifacts')
        return selected
    permissions = permissions or {}
    filesystem = permissions.get('filesystem') or {}
    if filesystem.get('write') or permissions.get('network'):
        return 'side_effecting'
    if declared_artifacts:
        return 'side_effecting'
    return 'pure'


def execution_semantics(spec):
    spec = spec or {}
    return normalize_execution_semantics(
        spec.get('execution_semantics'),
        spec.get('parameters'),
        spec.get('permissions'),
        spec.get('artifacts'),
    )


def validate_required_artifacts(arguments, spec):
    for contract in normalize_artifact_contracts(
        (spec or {}).get('artifacts'),
        (spec or {}).get('parameters'),
    ):
        if not contract['required']:
            continue
        value = (arguments or {}).get(contract['argument'])
        if value is None or value == '' or value == []:
            raise ValueError(
                f"artifact argument is required: {contract['argument']}"
            )


def _is_directory_parameter(name):
    return name.endswith('_dir') or name.endswith('_directory')


def _replace_paths(value, replacements):
    if isinstance(value, dict):
        return {key: _replace_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_paths(item, replacements) for item in value)
    if not isinstance(value, str):
        return value
    for staged, target in replacements:
        if value == staged:
            return target
        prefix = staged + os.sep
        if value.startswith(prefix):
            return target + value[len(staged):]
    return value


@dataclass(frozen=True)
class StagedArtifact:
    ordinal: int
    parameter: str
    target: Path
    staged: Path
    directory: bool
    required: bool


class ArtifactCommit:
    def __init__(self, result, handles):
        self.result = result
        self._handles = tuple(handles)
        self.artifacts = tuple(dict(handle.record) for handle in self._handles)
        self._closed = False

    def finalize(self):
        if self._closed:
            return
        for handle in self._handles:
            handle.finalize()
        self._closed = True

    def rollback(self):
        if self._closed:
            return
        for handle in reversed(self._handles):
            handle.rollback()
        self._closed = True


class ArtifactTransaction:
    def __init__(self, arguments, artifacts):
        self.arguments = arguments
        self.artifacts = tuple(artifacts)
        self._finished = False

    @classmethod
    def prepare(cls, arguments, spec, execution_key):
        validate_required_artifacts(arguments, spec)
        selected = dict(arguments)
        artifacts = []
        seen = set()
        for contract in normalize_artifact_contracts(
            (spec or {}).get('artifacts'),
            (spec or {}).get('parameters'),
        ):
            parameter = contract['argument']
            if parameter not in selected:
                if contract['required']:
                    raise ValueError(f'artifact argument is required: {parameter}')
                continue
            raw = selected.get(parameter)
            values = raw if isinstance(raw, list) else [raw]
            staged_values = []
            for index, value in enumerate(values):
                if not isinstance(value, (str, os.PathLike)) or not str(value):
                    staged_values.append(value)
                    continue
                target = Path(value).expanduser().resolve(strict=False)
                key = str(target)
                if key in seen:
                    staged_values.append(value)
                    continue
                seen.add(key)
                suffix = f'.bioagent-{execution_key}-{index}.staging'
                staged = target.with_name(f'.{target.name}{suffix}')
                directory = contract['kind'] == 'directory'
                if staged.exists():
                    if staged.is_dir():
                        shutil.rmtree(staged)
                    else:
                        staged.unlink()
                staged.parent.mkdir(parents=True, exist_ok=True)
                if directory:
                    staged.mkdir(parents=True)
                artifacts.append(StagedArtifact(
                    len(artifacts),
                    parameter,
                    target,
                    staged,
                    directory,
                    bool(contract['required']),
                ))
                staged_values.append(str(staged))
            selected[parameter] = staged_values if isinstance(raw, list) else staged_values[0]
        return cls(selected, artifacts)

    def _validated_artifacts(self):
        selected = []
        for artifact in self.artifacts:
            if not artifact.staged.exists():
                if artifact.required:
                    raise RuntimeError(
                        f'required artifact was not generated: {artifact.parameter}'
                    )
                continue
            if artifact.staged.is_symlink():
                raise RuntimeError(
                    f'artifact cannot be a symbolic link: {artifact.parameter}'
                )
            if artifact.directory:
                if not artifact.staged.is_dir():
                    raise RuntimeError(
                        f'artifact must be a directory: {artifact.parameter}'
                    )
                files = [
                    entry for entry in artifact.staged.rglob('*')
                    if entry.is_file() and not entry.is_symlink()
                ]
                if not files:
                    if artifact.required:
                        raise RuntimeError(
                            f'required artifact directory is empty: {artifact.parameter}'
                        )
                    continue
            else:
                if not artifact.staged.is_file():
                    raise RuntimeError(
                        f'artifact must be a file: {artifact.parameter}'
                    )
                if artifact.staged.stat().st_size < 1:
                    raise RuntimeError(
                        f'artifact file is empty: {artifact.parameter}'
                    )
            selected.append(artifact)
        return tuple(selected)

    def plan(self, store, context):
        return tuple(
            store.plan(
                artifact.staged,
                artifact.target,
                artifact.parameter,
                'directory' if artifact.directory else 'file',
                context,
                artifact.ordinal,
            )
            for artifact in self._validated_artifacts()
        )

    def commit(self, result):
        replacements = []
        committed = []
        try:
            for artifact in self._validated_artifacts():
                if artifact.target.exists():
                    raise RuntimeError(
                        f'artifact target already exists: {artifact.target}'
                    )
                artifact.target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(artifact.staged, artifact.target)
                committed.append(artifact.target)
                replacements.append((str(artifact.staged), str(artifact.target)))
        except Exception:
            for target in reversed(committed):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            self.rollback()
            raise
        self._finished = True
        return _replace_paths(result, replacements)

    def publish(self, result, store, context):
        handles = []
        replacements = []
        try:
            for artifact in self._validated_artifacts():
                handle = store.publish(
                    artifact.staged,
                    artifact.target,
                    artifact.parameter,
                    'directory' if artifact.directory else 'file',
                    context,
                    artifact.ordinal,
                )
                handles.append(handle)
                replacements.append((
                    str(artifact.staged),
                    str(handle.reference),
                ))
        except Exception:
            for handle in reversed(handles):
                handle.rollback()
            self.rollback()
            raise
        self._finished = True
        return ArtifactCommit(_replace_paths(result, replacements), handles)

    def rollback(self):
        if self._finished:
            return
        for artifact in self.artifacts:
            if artifact.staged.is_dir():
                shutil.rmtree(artifact.staged, ignore_errors=True)
            else:
                artifact.staged.unlink(missing_ok=True)
        self._finished = True
