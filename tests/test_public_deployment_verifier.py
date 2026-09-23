import http.cookiejar
from email.message import Message
import unittest

from scripts.verify_public_deployment import (
    ResponseSnapshot,
    VerificationError,
    require_cookie,
    require_frontend_headers,
    validate_base_url,
)


def cookie(name, *, secure=True, path='/', domain_specified=False, rest=None):
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value='value',
        port=None,
        port_specified=False,
        domain='platform.example',
        domain_specified=domain_specified,
        domain_initial_dot=False,
        path=path,
        path_specified=True,
        secure=secure,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest=rest or {},
        rfc2109=False,
    )


class PublicDeploymentVerifierTests(unittest.TestCase):
    def test_accepts_https_origin_and_rejects_unsafe_urls(self):
        self.assertEqual(
            validate_base_url('https://platform.example:8443/'),
            'https://platform.example:8443',
        )
        for value in (
            'http://platform.example',
            'https://user@platform.example',
            'https://platform.example/path',
            'https://platform.example/?debug=1',
            'https://platform.example:99999',
        ):
            with self.subTest(value=value), self.assertRaises(VerificationError):
                validate_base_url(value)

    def test_requires_production_browser_headers(self):
        headers = Message()
        headers['Strict-Transport-Security'] = 'max-age=86400'
        headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'"
        )
        headers['X-Content-Type-Options'] = 'nosniff'
        response = ResponseSnapshot(200, headers, b'', 'https://platform.example/')
        require_frontend_headers(response)
        headers.replace_header('Content-Security-Policy', "default-src 'self'")
        with self.assertRaises(VerificationError):
            require_frontend_headers(response)

    def test_requires_host_only_secure_strict_cookies(self):
        require_cookie(
            cookie(
                '__Host-bioagent_session',
                rest={'SameSite': 'Strict', 'HttpOnly': None},
            ),
            http_only=True,
        )
        require_cookie(
            cookie('__Host-bioagent_csrf', rest={'SameSite': 'Strict'}),
            http_only=False,
        )
        with self.assertRaises(VerificationError):
            require_cookie(
                cookie(
                    '__Host-bioagent_session',
                    secure=False,
                    rest={'SameSite': 'Strict', 'HttpOnly': None},
                ),
                http_only=True,
            )


if __name__ == '__main__':
    unittest.main()
