import gzip
import json
from pathlib import Path

import yaml

from src.file_security import (
    FileSecurityError, _HtmlTextExtractor, TEXT_CONTROL_BATCH_CHARS,
    UNSAFE_TEXT_CONTROL_PATTERN, VCF_GZIP_COMPRESSION_LEVEL,
)


BASELINE_SOURCE_COMMIT = 'f3b34517dadde79e7a2825f79b50fae0790344f4'


class WholeFileReconstructor:
    def _safe_text(self, content):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise FileSecurityError('CDR requires UTF-8 text content') from exc
        # Short regex calls let Python yield the GIL between batches.
        for offset in range(0, len(text), TEXT_CONTROL_BATCH_CHARS):
            if UNSAFE_TEXT_CONTROL_PATTERN.search(
                text, offset, offset + TEXT_CONTROL_BATCH_CHARS,
            ):
                raise FileSecurityError('CDR rejected unsafe control characters')
        return text.replace('\r\n', '\n').replace('\r', '\n')

    def _reconstruct_text(self, content, extension):
        text = self._safe_text(content)
        if extension == '.json':
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FileSecurityError('CDR rejected invalid JSON') from exc
            return (json.dumps(
                value, ensure_ascii=False, sort_keys=True, indent=2
            ) + '\n').encode('utf-8')
        if extension in {'.yaml', '.yml'}:
            try:
                value = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise FileSecurityError('CDR rejected invalid YAML') from exc
            return yaml.safe_dump(
                value, allow_unicode=True, sort_keys=True
            ).encode('utf-8')
        if extension in {'.html', '.htm'}:
            parser = _HtmlTextExtractor()
            parser.feed(text)
            parser.close()
            return parser.text().encode('utf-8')
        return text.encode('utf-8')

    def reconstruct(self, path, filename):
        target = Path(path)
        lower_name = str(filename).lower()
        try:
            if lower_name.endswith('.vcf.gz'):
                with gzip.open(target, 'rb') as source:
                    content = source.read()
                rebuilt = gzip.compress(
                    self._reconstruct_text(content, '.vcf'),
                    compresslevel=VCF_GZIP_COMPRESSION_LEVEL, mtime=0,
                )
            else:
                rebuilt = self._reconstruct_text(
                    target.read_bytes(), Path(lower_name).suffix
                )
            temporary = target.with_name(f'.{target.name}.cdr')
            temporary.write_bytes(rebuilt)
            temporary.replace(target)
        except FileSecurityError:
            raise
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise FileSecurityError('CDR reconstruction failed') from exc
        return 'reconstructed'
