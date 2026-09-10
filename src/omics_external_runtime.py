"""Dependency bundle for native omics command executors."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ExternalToolDependencies:
    which: Callable[[str], str | None]
    sha256: Callable[[Path], str]
    version: Callable[[str], dict]
    run_command: Callable[..., dict]
    capture_command: Callable[..., dict]
