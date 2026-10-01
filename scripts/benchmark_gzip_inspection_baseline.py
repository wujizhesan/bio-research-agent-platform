import hashlib

from src.file_storage import CHUNK_SIZE, LocalFileStorage


BASELINE_SOURCE_COMMIT = '7be9b872ee3c5cc33e052079bbb2f3cbd3daedd9'


class SeparateHashStorage(LocalFileStorage):
    def _inspect_and_hash(self, target, filename, size_bytes):
        content_type = self._inspect_content(target, filename, size_bytes)
        digest = hashlib.sha256()
        with target.open('rb') as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                digest.update(chunk)
        return content_type, digest
