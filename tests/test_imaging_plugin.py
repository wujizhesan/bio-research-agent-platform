import tempfile
import unittest
from pathlib import Path

from src.domain_registry import available_domains, run_tool


class ImagingPluginTests(unittest.TestCase):
    def test_imaging_domain_is_registered(self):
        self.assertIn('imaging', available_domains())

    def test_svg_image_qc_returns_reproducible_metadata(self):
        with tempfile.TemporaryDirectory(prefix='imaging_test_') as raw:
            root = Path(raw)
            image = root / 'cells.svg'
            image.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48">'
                '<circle cx="16" cy="16" r="8"/></svg>',
                encoding='utf-8',
            )
            result = run_tool('imaging_inspect_image', {
                'image_path': str(image),
                'output_dir': str(root / 'output'),
                'modality': 'microscopy',
            })
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['metrics']['width'], 64)
            self.assertEqual(result['metrics']['height'], 48)
            self.assertEqual(result['metrics']['channels'], 'vector')
            self.assertEqual(result['metrics']['elements'], 1)
            self.assertTrue(Path(result['manifest_path']).is_file())


if __name__ == '__main__':
    unittest.main()
