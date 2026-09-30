import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.benchmark_cdr_baseline import WholeFileReconstructor
from scripts.benchmark_vcf_compression import vcf_for

from src.file_security import (
    ContentDisarmReconstructor,
    FileSecurityError,
    FileSecurityPipeline,
    TEXT_RECONSTRUCTION_CHUNK_BYTES,
)


class ContentDisarmTextTests(unittest.TestCase):
    def test_ascii_controls_at_each_position_follow_the_existing_policy(self):
        cdr = ContentDisarmReconstructor()
        allowed = {9: '\t', 10: '\n', 13: '\n'}
        for code in range(32):
            for prefix, suffix in ((b'', b'end'), (b'start', b'end'), (b'start', b'')):
                with self.subTest(code=code, prefix=prefix, suffix=suffix):
                    content = prefix + bytes([code]) + suffix
                    if code in allowed:
                        self.assertEqual(
                            cdr._safe_text(content),
                            prefix.decode() + allowed[code] + suffix.decode(),
                        )
                    else:
                        with self.assertRaisesRegex(
                            FileSecurityError, '^CDR rejected unsafe control characters$',
                        ):
                            cdr._safe_text(content)

    def test_unicode_printable_and_extended_controls_are_preserved(self):
        text = ''.join(chr(code) for code in range(32, 160))
        text += '中文科研样本🙂\u2028\u2029\ufeff'
        self.assertEqual(ContentDisarmReconstructor()._safe_text(text.encode()), text)

    def test_utf8_failure_precedes_control_validation(self):
        for content in (b'\xff', b'\x00\xff', b'\xff\x00', b'\xed\xa0\x80', b'\xe2\x82'):
            with self.subTest(content=content):
                with self.assertRaisesRegex(
                    FileSecurityError, '^CDR requires UTF-8 text content$',
                ) as caught:
                    ContentDisarmReconstructor()._safe_text(content)
                self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)

    def test_bom_and_mixed_line_endings_keep_their_existing_results(self):
        for content, expected in (
            (b'', ''),
            (b'\xef\xbb\xbf', ''),
            (b'\xef\xbb\xbf\xef\xbb\xbfvalue', '\ufeffvalue'),
            (b'value\xef\xbb\xbf', 'value\ufeff'),
            (b'first\r\nsecond\rthird\nfourth\t', 'first\nsecond\nthird\nfourth\t'),
            (b'\r\r\n', '\n\n'),
            (b'\n\r', '\n\n'),
        ):
            with self.subTest(content=content):
                self.assertEqual(ContentDisarmReconstructor()._safe_text(content), expected)

    def test_reconstruction_rejects_controls_beyond_the_sniff_and_chunk_boundaries(self):
        cdr = ContentDisarmReconstructor()
        for offset in (
            64 * 1024 - 1, 64 * 1024, 64 * 1024 + 1,
            1024 * 1024 - 1, 1024 * 1024 + 1,
        ):
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(FileSecurityError, 'unsafe control'):
                    cdr._reconstruct_text(b'A' * offset + b'\x1f' + b'end', '.fastq')
        for prefix in ('中文' * (32 * 1024), '🙂' * (64 * 1024)):
            with self.subTest(prefix_character=prefix[0]):
                with self.assertRaisesRegex(FileSecurityError, 'unsafe control'):
                    cdr._reconstruct_text((prefix + '\x00' + 'end').encode(), '.fastq')

    def test_structured_formats_use_the_same_text_rules(self):
        cdr = ContentDisarmReconstructor()
        self.assertEqual(
            cdr._reconstruct_text('\ufeff{"z":2,"a":"科研"}\r\n'.encode(), '.json'),
            '{\n  "a": "科研",\n  "z": 2\n}\n'.encode(),
        )
        self.assertEqual(
            cdr._reconstruct_text(b'z: 2\r\na: 1\r', '.yaml'), b'a: 1\nz: 2\n',
        )
        self.assertEqual(
            cdr._reconstruct_text(b'<h1>Result</h1><script>steal()</script>\r\n', '.html'),
            b'Result\n',
        )
        escaped = cdr._reconstruct_text(b'{"value":"\\u0000"}', '.json')
        self.assertEqual(json.loads(escaped), {'value': '\x00'})
        for extension, content, message in (
            ('.json', b'{', 'invalid JSON'),
            ('.yml', b'!unsafe value', 'invalid YAML'),
            ('.html', b'<p>\x0b</p>', 'unsafe control'),
        ):
            with self.subTest(extension=extension):
                with self.assertRaisesRegex(FileSecurityError, message):
                    cdr._reconstruct_text(content, extension)

    def test_gzip_vcf_reconstruction_normalizes_the_decompressed_text(self):
        content = b'\xef\xbb\xbf##fileformat=VCFv4.2\r\n#CHROM\tPOS\r\n1\t10\r'
        with tempfile.TemporaryDirectory(prefix='cdr_vcf_') as raw:
            root = Path(raw).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            path = root / 'variants.vcf.gz'
            path.write_bytes(gzip.compress(content, mtime=0))
            self.assertEqual(
                ContentDisarmReconstructor().reconstruct(path, path.name), 'reconstructed',
            )
            self.assertEqual(
                gzip.decompress(path.read_bytes()),
                b'##fileformat=VCFv4.2\n#CHROM\tPOS\n1\t10\n',
            )
            self.assertEqual(list(root.iterdir()), [path])

    def test_rejected_text_is_not_replaced_or_scanned_a_second_time(self):
        class Scanner:
            def __init__(self):
                self.calls = 0

            def scan(self, _path):
                self.calls += 1
                return 'clean'

        with tempfile.TemporaryDirectory(prefix='cdr_rejection_') as raw:
            root = Path(raw).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            for name, content, message in (
                ('unsafe.txt', b'original\x00content', 'unsafe control'),
                ('invalid.txt', b'original\xffcontent', 'UTF-8'),
                ('unsafe.vcf.gz', gzip.compress(b'##fileformat=VCFv4.2\n\x00'), 'unsafe control'),
            ):
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(content)
                    scanner = Scanner()
                    pipeline = FileSecurityPipeline(
                        clamav=scanner, cdr=ContentDisarmReconstructor(), required=True,
                    )
                    with self.assertRaisesRegex(FileSecurityError, message):
                        pipeline.process(path, path.name)
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(scanner.calls, 1)
                    self.assertFalse(path.with_name(f'.{name}.cdr').exists())

    def test_vcf_gzip_output_is_deterministic_after_normalization(self):
        content = b'\xef\xbb\xbf##fileformat=VCFv4.2\r\n#CHROM\tPOS\r\n'
        content += b'1\t10\t.\tA\tC\t30\tPASS\tTAG=sample\r\n' * 4096
        normalized = content.decode('utf-8-sig').replace('\r\n', '\n').encode()
        outputs = []
        with tempfile.TemporaryDirectory(prefix='cdr_deterministic_') as raw:
            root = Path(raw).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            for filename in ('first.vcf.gz', 'SECOND.VCF.GZ'):
                path = root / filename
                path.write_bytes(gzip.compress(content, compresslevel=9, mtime=123456789))
                ContentDisarmReconstructor().reconstruct(path, filename)
                output = path.read_bytes()
                self.assertEqual(gzip.decompress(output), normalized)
                self.assertEqual(output[4:8], b'\0' * 4)
                self.assertEqual(output, gzip.compress(normalized, compresslevel=6, mtime=0))
                outputs.append(output)
            self.assertEqual(outputs[0], outputs[1])

    def test_invalid_gzip_does_not_replace_original_file(self):
        with tempfile.TemporaryDirectory(prefix='cdr_invalid_gzip_') as raw:
            root = Path(raw).resolve()
            self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
            path = root / 'variants.vcf.gz'
            original = gzip.compress(b'##fileformat=VCFv4.2\n#CHROM\tPOS\n', mtime=0)[:-8]
            path.write_bytes(original)
            with self.assertRaisesRegex(FileSecurityError, '^CDR reconstruction failed$'):
                ContentDisarmReconstructor().reconstruct(path, path.name)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [path])

    def test_streaming_reconstruction_preserves_split_unicode_bom_and_line_endings(self):
        for content in (
            b'', b'\xef\xbb\xbf', b'\xef\xbb\xbf\xef\xbb\xbfvalue',
            '科研🙂\u0085\u2028\ufeff\r\n第二行\r尾部\r'.encode(),
            b'\r\r\n\n\r\nend\r',
            b'\xef\xbb\xbf' + b'A' * (TEXT_RECONSTRUCTION_CHUNK_BYTES - 4) + '🙂\r\n科研\r'.encode(),
        ):
            expected = WholeFileReconstructor()._reconstruct_text(content, '.txt')
            for compressed in (False, True):
                for chunk_bytes in (1, 2, 7, TEXT_RECONSTRUCTION_CHUNK_BYTES):
                    if len(content) > 100 and chunk_bytes < 7:
                        continue
                    with self.subTest(compressed=compressed, chunk_bytes=chunk_bytes, size=len(content)), tempfile.TemporaryDirectory() as raw:
                        path = Path(raw) / ('sample.vcf.gz' if compressed else 'sample.fasta')
                        path.write_bytes(gzip.compress(content) if compressed else content)
                        with patch('src.file_security.TEXT_RECONSTRUCTION_CHUNK_BYTES', chunk_bytes):
                            ContentDisarmReconstructor().reconstruct(path, path.name)
                        rebuilt = path.read_bytes()
                        reference = gzip.compress(expected, compresslevel=6, mtime=0) if compressed else expected
                        self.assertEqual(rebuilt, reference)
                        self.assertEqual(list(Path(raw).iterdir()), [path])

    def test_streaming_gzip_matches_one_shot_for_multiple_levels_and_members(self):
        content = vcf_for('varied_annotations', 512 * 1024)
        normalized = WholeFileReconstructor()._reconstruct_text(content, '.vcf')
        split = len(content) // 2
        payloads = (
            gzip.compress(content, mtime=123456789),
            gzip.compress(content[:split]) + gzip.compress(content[split:]) + b'\0' * 32,
        )
        for level in (1, 6, 9):
            for payload in payloads:
                with self.subTest(level=level, members=payload is payloads[1]), tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / 'VARIANTS.VCF.GZ'
                    path.write_bytes(payload)
                    with patch('src.file_security.VCF_GZIP_COMPRESSION_LEVEL', level):
                        ContentDisarmReconstructor().reconstruct(path, path.name)
                    self.assertEqual(path.read_bytes(), gzip.compress(normalized, compresslevel=level, mtime=0))

    def test_streaming_utf8_failure_precedes_earlier_control_and_cleans_partial_output(self):
        prefix = b'A' * (TEXT_RECONSTRUCTION_CHUNK_BYTES + 1)
        for content in (
            prefix + b'\x00' + prefix + b'\xff',
            prefix + b'\xff' + prefix + b'\x00',
            prefix + b'\x00' + prefix + b'\xe2\x82',
            prefix + b'\xed\xa0\x80',
        ):
            for compressed in (False, True):
                with self.subTest(compressed=compressed, suffix=content[-8:]), tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / ('sample.vcf.gz' if compressed else 'sample.fastq')
                    original = gzip.compress(content) if compressed else content
                    path.write_bytes(original)
                    with self.assertRaisesRegex(FileSecurityError, '^CDR requires UTF-8 text content$') as caught:
                        ContentDisarmReconstructor().reconstruct(path, path.name)
                    self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(Path(raw).iterdir()), [path])

    def test_streaming_gzip_integrity_failure_precedes_text_errors(self):
        for content in (
            b'##fileformat=VCFv4.2\n' + b'\x00' + b'A' * (2 * TEXT_RECONSTRUCTION_CHUNK_BYTES),
            b'##fileformat=VCFv4.2\n' + b'\xff' + b'A' * (2 * TEXT_RECONSTRUCTION_CHUNK_BYTES),
        ):
            payload = gzip.compress(content)
            corrupt = bytearray(payload)
            corrupt[-8] ^= 1
            for original in (bytes(corrupt), payload[:-4]):
                with self.subTest(suffix=original[-8:]), tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / 'sample.vcf.gz'
                    path.write_bytes(original)
                    with self.assertRaisesRegex(FileSecurityError, '^CDR reconstruction failed$'):
                        ContentDisarmReconstructor().reconstruct(path, path.name)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(Path(raw).iterdir()), [path])

    def test_streaming_source_reads_are_bounded(self):
        open_path, open_gzip = Path.open, gzip.open
        content = b'line\r\n' * (TEXT_RECONSTRUCTION_CHUNK_BYTES // 2)
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / ('sample.vcf.gz' if compressed else 'sample.tsv')
                path.write_bytes(gzip.compress(content) if compressed else content)
                sizes = []

                class Reader:
                    def __init__(self, handle):
                        self.handle = handle

                    def __enter__(self):
                        self.handle.__enter__()
                        return self

                    def __exit__(self, *args):
                        return self.handle.__exit__(*args)

                    def read(self, size):
                        sizes.append(size)
                        return self.handle.read(size)

                def open_plain(selected, mode='r', *args, **kwargs):
                    handle = open_path(selected, mode, *args, **kwargs)
                    return Reader(handle) if selected == path and mode == 'rb' else handle

                def open_compressed(*args, **kwargs):
                    return Reader(open_gzip(*args, **kwargs))

                with patch.object(Path, 'open', open_plain), patch('src.file_security.gzip.open', open_compressed):
                    ContentDisarmReconstructor().reconstruct(path, path.name)
                self.assertGreater(len(sizes), 1)
                self.assertTrue(all(0 < size <= TEXT_RECONSTRUCTION_CHUNK_BYTES for size in sizes))
                rebuilt = gzip.decompress(path.read_bytes()) if compressed else path.read_bytes()
                self.assertEqual(rebuilt, content.replace(b'\r\n', b'\n'))

    def test_streaming_control_failure_after_written_prefix_keeps_original(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / ('sample.vcf.gz' if compressed else 'sample.tsv')
                content = b'A' * (TEXT_RECONSTRUCTION_CHUNK_BYTES + 1) + b'\x1f'
                original = gzip.compress(content) if compressed else content
                path.write_bytes(original)
                with self.assertRaisesRegex(FileSecurityError, 'unsafe control'):
                    ContentDisarmReconstructor().reconstruct(path, path.name)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(raw).iterdir()), [path])

    def test_temporary_write_and_replace_failures_preserve_original_and_clean_up(self):
        open_path = Path.open
        for extension, original in (
            ('.tsv', b'line\r\n' * 50000),
            ('.vcf.gz', gzip.compress(b'##fileformat=VCFv4.2\r\n' * 50000)),
            ('.json', b'{"z":2,"a":1}'),
        ):
            for phase in ('write', 'replace'):
                with self.subTest(extension=extension, phase=phase), tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / ('sample' + extension)
                    temporary = path.with_name('.' + path.name + '.cdr')
                    path.write_bytes(original)

                    class Writer:
                        def __init__(self, handle):
                            self.handle = handle

                        def __enter__(self):
                            self.handle.__enter__()
                            return self

                        def __exit__(self, *args):
                            return self.handle.__exit__(*args)

                        def write(self, data):
                            self.handle.write(data[:8])
                            raise OSError('injected write failure')

                    def failing_open(selected, mode='r', *args, **kwargs):
                        handle = open_path(selected, mode, *args, **kwargs)
                        return Writer(handle) if selected == temporary and mode == 'wb' else handle

                    selected_patch = patch.object(Path, 'open', failing_open) if phase == 'write' else patch.object(Path, 'replace', side_effect=OSError('injected replace failure'))
                    with selected_patch, self.assertRaisesRegex(FileSecurityError, '^CDR reconstruction failed$'):
                        ContentDisarmReconstructor().reconstruct(path, path.name)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(Path(raw).iterdir()), [path])

    def test_structured_reconstruction_keeps_existing_output_bytes(self):
        for extension, content in (
            ('.json', '\ufeff{"z":2,"a":"科研"}\r\n'.encode()),
            ('.yaml', b'z: 2\r\na: 1\r'),
            ('.html', b'<h1>Result</h1><script>steal()</script><p>text</p>\r\n'),
        ):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / ('sample' + extension)
                path.write_bytes(content)
                expected = WholeFileReconstructor()._reconstruct_text(content, extension)
                ContentDisarmReconstructor().reconstruct(path, path.name)
                self.assertEqual(path.read_bytes(), expected)
