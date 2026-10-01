import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_tenant_context import (
    TenantContextConfigurationError,
    _signing_key,
)


class TenantContextConfigTests(unittest.TestCase):
    def test_file_key_rejects_changed_release_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'rls.key'
            path.write_text('o' * 32, encoding='utf-8')
            environment = {
                'RLS_CONTEXT_SIGNING_KEY_FILE': str(path),
                'RLS_CONTEXT_SIGNING_KEY_SHA256': hashlib.sha256(
                    path.read_bytes(),
                ).hexdigest(),
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(_signing_key(), 'o' * 32)
                path.write_text('n' * 32, encoding='utf-8')
                with self.assertRaisesRegex(
                    TenantContextConfigurationError, 'checksum mismatch',
                ):
                    _signing_key()
