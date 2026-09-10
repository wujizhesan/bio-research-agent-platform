import tomllib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackageManifestTests(unittest.TestCase):
    def test_all_source_modules_are_included_in_wheel_manifest(self):
        config = tomllib.loads(
            (PROJECT_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        )
        packaged = set(config['tool']['setuptools']['py-modules'])
        source_modules = {
            path.stem
            for path in (PROJECT_ROOT / 'src').glob('*.py')
            if path.name != '__init__.py'
        }
        self.assertSetEqual(packaged, source_modules)


if __name__ == '__main__':
    unittest.main()
