import gzip
import json
from pathlib import Path
import tempfile
import unittest

from src.file_security import (
    ContentDisarmReconstructor,
    FileSecurityError,
    FileSecurityPipeline,
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
