import os
from pathlib import Path

from src.file_storage import LocalFileStorage


BASELINE_SOURCE_COMMIT = '9106653ef3d8854580aa23b0c9729ff3ba439e6b'


class WalkQuotaStorage(LocalFileStorage):
    def _storage_usage(self, exclude_uploads=()) -> int:
        total = 0
        for directory, names, filenames in os.walk(self.root):
            if Path(directory) == self.root:
                names[:] = [name for name in names if name not in exclude_uploads]
            for filename in filenames:
                try:
                    total += (Path(directory) / filename).stat().st_size
                except OSError:
                    continue
        return total
