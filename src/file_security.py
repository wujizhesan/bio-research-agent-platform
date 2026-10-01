"""Malware scanning and content disarm for uploaded research files."""

import codecs
from contextlib import suppress
from dataclasses import dataclass
import gzip
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import socket
import struct
import zlib

import yaml


SCAN_CHUNK_SIZE = 1024 * 1024
MAX_SCAN_REPLY_BYTES = 16 * 1024
TEXT_CONTROL_BATCH_CHARS = 64 * 1024
TEXT_RECONSTRUCTION_CHUNK_BYTES = 64 * 1024
UNSAFE_TEXT_CONTROL_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
VCF_GZIP_COMPRESSION_LEVEL = 6


class FileSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class FileSecurityResult:
    status: str
    clamav: str
    cdr: str
    scan_count: int

    def as_dict(self):
        return {
            'status': self.status,
            'clamav': self.clamav,
            'cdr': self.cdr,
            'scan_count': self.scan_count,
        }


class ClamAVScanner:
    def __init__(self, host, port=3310, timeout_seconds=30.0):
        if not host or not str(host).strip():
            raise ValueError('CLAMAV_HOST is required')
        self.host = str(host).strip()
        self.port = int(port)
        self.timeout_seconds = float(timeout_seconds)

    def scan(self, path):
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_seconds
            ) as client:
                client.settimeout(self.timeout_seconds)
                client.sendall(b'zINSTREAM\0')
                with Path(path).open('rb') as source:
                    for chunk in iter(lambda: source.read(SCAN_CHUNK_SIZE), b''):
                        client.sendall(struct.pack('>I', len(chunk)))
                        client.sendall(chunk)
                client.sendall(struct.pack('>I', 0))
                reply = bytearray()
                while len(reply) < MAX_SCAN_REPLY_BYTES:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    reply.extend(chunk)
                    if b'\0' in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise FileSecurityError('ClamAV scan service is unavailable') from exc
        message = bytes(reply).split(b'\0', 1)[0].decode(
            'utf-8', errors='replace'
        ).strip()
        if message.endswith(': OK'):
            return 'clean'
        if message.endswith(' FOUND'):
            signature = message.rsplit(':', 1)[-1].removesuffix(' FOUND').strip()
            raise FileSecurityError(
                f'ClamAV detected malware: {signature or "unknown signature"}'
            )
        raise FileSecurityError(
            f'ClamAV scan failed: {message or "empty response"}'
        )


class _HtmlTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored_depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag.lower() in {'script', 'style', 'iframe', 'object', 'embed'}:
            self.ignored_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {'script', 'style', 'iframe', 'object', 'embed'}:
            self.ignored_depth = max(self.ignored_depth - 1, 0)

    def handle_data(self, data):
        if not self.ignored_depth and data.strip():
            self.parts.append(data.strip())

    def text(self):
        return '\n'.join(self.parts) + ('\n' if self.parts else '')


class ContentDisarmReconstructor:
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

    def _text_chunks(self, source):
        decoder = codecs.getincrementaldecoder('utf-8-sig')()
        unicode_error = None
        unsafe = False
        pending_cr = False
        while True:
            chunk = source.read(TEXT_RECONSTRUCTION_CHUNK_BYTES)
            final = not chunk
            if unicode_error is None:
                try:
                    text = decoder.decode(chunk, final=final)
                except UnicodeDecodeError as exc:
                    unicode_error = exc
                else:
                    unsafe = unsafe or UNSAFE_TEXT_CONTROL_PATTERN.search(text) is not None
                    if not unsafe:
                        if pending_cr:
                            text = '\r' + text
                        pending_cr = text.endswith('\r')
                        if pending_cr:
                            text = text[:-1]
                        yield text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
            if final:
                break
        # Drain the source first so gzip errors precede UTF-8 and control errors.
        if unicode_error is not None:
            raise FileSecurityError('CDR requires UTF-8 text content') from unicode_error
        if unsafe:
            raise FileSecurityError('CDR rejected unsafe control characters')
        if pending_cr:
            yield b'\n'

    def _reconstruct_stream(self, source, output, compressed=False):
        if not compressed:
            for chunk in self._text_chunks(source):
                output.write(chunk)
            return
        level = VCF_GZIP_COMPRESSION_LEVEL
        # Match gzip.compress headers across Python and zlib versions.
        output.write(gzip.compress(b'', compresslevel=level, mtime=0)[:10])
        compressor = zlib.compressobj(level, wbits=-15)
        checksum = size = 0
        for chunk in self._text_chunks(source):
            checksum = zlib.crc32(chunk, checksum)
            size += len(chunk)
            output.write(compressor.compress(chunk))
        output.write(compressor.flush())
        output.write(struct.pack('<II', checksum, size & 0xffffffff))

    def reconstruct(self, path, filename):
        target = Path(path)
        lower_name = str(filename).lower()
        extension = Path(lower_name).suffix
        temporary = target.with_name(f'.{target.name}.cdr')
        try:
            compressed = lower_name.endswith('.vcf.gz')
            if extension in {'.json', '.yaml', '.yml', '.html', '.htm'}:
                rebuilt = self._reconstruct_text(
                    target.read_bytes(), extension,
                )
                temporary.write_bytes(rebuilt)
            else:
                opener = gzip.open if compressed else Path.open
                with opener(target, 'rb') as source, temporary.open('wb') as output:
                    self._reconstruct_stream(source, output, compressed)
            temporary.replace(target)
        except FileSecurityError:
            raise
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise FileSecurityError('CDR reconstruction failed') from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return 'reconstructed'


class FileSecurityPipeline:
    def __init__(self, clamav=None, cdr=None, required=False):
        self.clamav = clamav
        self.cdr = cdr
        self.required = bool(required)
        if self.required and (self.clamav is None or self.cdr is None):
            raise ValueError('required file security needs both ClamAV and CDR')

    def process(self, path, filename):
        scans = 0
        clamav_status = 'disabled'
        cdr_status = 'disabled'
        if self.clamav is not None:
            clamav_status = self.clamav.scan(path)
            scans += 1
        if self.cdr is not None:
            cdr_status = self.cdr.reconstruct(path, filename)
            if self.clamav is not None:
                clamav_status = self.clamav.scan(path)
                scans += 1
        return FileSecurityResult(
            status='clean',
            clamav=clamav_status,
            cdr=cdr_status,
            scan_count=scans,
        )


def build_file_security_pipeline_from_env():
    mode = os.environ.get('FILE_SECURITY_MODE', 'disabled').strip().lower()
    if mode not in {'disabled', 'optional', 'required'}:
        raise ValueError('FILE_SECURITY_MODE must be disabled, optional, or required')
    if mode == 'disabled':
        return None
    host = os.environ.get('CLAMAV_HOST', '').strip()
    clamav = None
    if host:
        clamav = ClamAVScanner(
            host,
            port=int(os.environ.get('CLAMAV_PORT', '3310')),
            timeout_seconds=float(
                os.environ.get('CLAMAV_TIMEOUT_SECONDS', '30')
            ),
        )
    cdr_mode = os.environ.get('FILE_CDR_MODE', 'normalize').strip().lower()
    if cdr_mode not in {'disabled', 'normalize'}:
        raise ValueError('FILE_CDR_MODE must be disabled or normalize')
    cdr = ContentDisarmReconstructor() if cdr_mode == 'normalize' else None
    return FileSecurityPipeline(
        clamav=clamav,
        cdr=cdr,
        required=mode == 'required',
    )
