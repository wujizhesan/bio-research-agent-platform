import argparse
from dataclasses import dataclass
import http.cookiejar
import json
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


MAX_RESPONSE_BYTES = 1024 * 1024


class VerificationError(RuntimeError):
    pass


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class ResponseSnapshot:
    status: int
    headers: object
    body: bytes
    url: str


def validate_base_url(value):
    try:
        parsed = urlsplit(str(value or '').strip())
        port = parsed.port
    except ValueError as exc:
        raise VerificationError('public base URL must be an HTTPS origin') from exc
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise VerificationError('public base URL must be an HTTPS origin')
    return f'https://{parsed.netloc}'


def read_secret_file(path_value):
    path = Path(path_value).resolve()
    if not path.is_file():
        raise VerificationError('deployment smoke password file is unavailable')
    value = path.read_text(encoding='utf-8').strip()
    if not value or len(value) > 4096:
        raise VerificationError('deployment smoke password is invalid')
    return value


def build_secure_opener(cookie_jar):
    return build_opener(
        RejectRedirects(),
        HTTPSHandler(context=ssl.create_default_context()),
        HTTPCookieProcessor(cookie_jar),
    )


def request_snapshot(opener, base_url, path, *, method='GET', data=None, headers=None):
    target = urljoin(f'{base_url}/', path.lstrip('/'))
    request = Request(
        target,
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        response = opener.open(request, timeout=20)
    except HTTPError as exc:
        response = exc
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise VerificationError('deployment verification response is too large')
        final_url = response.geturl()
        if urlsplit(final_url)[:2] != urlsplit(base_url)[:2]:
            raise VerificationError('deployment verification crossed origin')
        return ResponseSnapshot(
            status=int(response.status),
            headers=response.headers,
            body=body,
            url=final_url,
        )
    finally:
        response.close()


def require_status(response, expected, label):
    if response.status != expected:
        raise VerificationError(
            f'{label} returned HTTP {response.status}, expected {expected}'
        )


def require_frontend_headers(response):
    hsts = response.headers.get('Strict-Transport-Security', '')
    match = re.search(r'(?:^|;)\s*max-age=(\d+)', hsts, re.IGNORECASE)
    if not match or int(match.group(1)) < 86400:
        raise VerificationError('frontend HSTS header is missing or too short')
    csp = response.headers.get('Content-Security-Policy', '')
    required = (
        "default-src 'self'",
        "script-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    )
    if any(item not in csp for item in required) or "'unsafe-eval'" in csp:
        raise VerificationError('frontend CSP does not meet the production baseline')
    if response.headers.get('X-Content-Type-Options', '').lower() != 'nosniff':
        raise VerificationError('frontend nosniff header is missing')


def cookie_by_name(cookie_jar, name):
    matches = [cookie for cookie in cookie_jar if cookie.name == name]
    if len(matches) != 1:
        raise VerificationError(f'expected exactly one {name} cookie')
    return matches[0]


def cookie_attribute(cookie, name):
    return next(
        (value for key, value in cookie._rest.items() if key.lower() == name.lower()),
        None,
    )


def require_cookie(cookie, *, http_only):
    if not cookie.secure or cookie.path != '/' or cookie.domain_specified:
        raise VerificationError(f'{cookie.name} cookie has an unsafe scope')
    if str(cookie_attribute(cookie, 'SameSite') or '').lower() != 'strict':
        raise VerificationError(f'{cookie.name} cookie must use SameSite=Strict')
    has_http_only = any(key.lower() == 'httponly' for key in cookie._rest)
    if has_http_only != http_only:
        raise VerificationError(f'{cookie.name} cookie HttpOnly policy is invalid')


def decode_json(response, label):
    try:
        payload = json.loads(response.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f'{label} did not return valid JSON') from exc
    if not isinstance(payload, dict):
        raise VerificationError(f'{label} returned an invalid JSON object')
    return payload


def verify_public_deployment(
    base_url,
    username,
    password,
    *,
    job_tool=None,
    job_timeout_seconds=60,
    opener=None,
    cookie_jar=None,
):
    base_url = validate_base_url(base_url)
    selected_username = str(username or '').strip()
    if not selected_username or not password:
        raise VerificationError('deployment smoke credentials are required')
    cookie_jar = cookie_jar or http.cookiejar.CookieJar()
    opener = opener or build_secure_opener(cookie_jar)

    frontend = request_snapshot(opener, base_url, '/')
    require_status(frontend, 200, 'frontend')
    require_frontend_headers(frontend)

    token_response = request_snapshot(
        opener,
        base_url,
        '/api/v1/auth/token',
        method='POST',
        data=urlencode({
            'username': selected_username,
            'password': password,
        }).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    require_status(token_response, 200, 'token endpoint')
    token_payload = decode_json(token_response, 'token endpoint')
    access_token = token_payload.get('access_token')
    if not isinstance(access_token, str) or len(access_token) < 32:
        raise VerificationError('token endpoint returned an invalid access token')

    if job_tool:
        authorization = {'Authorization': f'Bearer {access_token}'}
        catalog = request_snapshot(
            opener,
            base_url,
            '/api/v1/plugins',
            headers=authorization,
        )
        require_status(catalog, 200, 'plugin catalog')
        project_response = request_snapshot(
            opener,
            base_url,
            '/api/v1/projects',
            method='POST',
            data=json.dumps({
                'name': f'Deployment smoke {int(time.time())}',
                'description': 'Automated production deployment verification',
            }).encode('utf-8'),
            headers={**authorization, 'Content-Type': 'application/json'},
        )
        require_status(project_response, 201, 'smoke project creation')
        project = decode_json(
            project_response, 'smoke project creation'
        ).get('project', {})
        project_id = project.get('project_id') if isinstance(project, dict) else None
        if not isinstance(project_id, str) or not project_id:
            raise VerificationError('smoke project creation returned no project ID')
        submitted = request_snapshot(
            opener,
            base_url,
            '/api/v1/jobs',
            method='POST',
            data=json.dumps({
                'tool': job_tool,
                'arguments': {},
                'project_id': project_id,
            }).encode('utf-8'),
            headers={**authorization, 'Content-Type': 'application/json'},
        )
        require_status(submitted, 202, 'smoke job submission')
        job = decode_json(submitted, 'smoke job submission').get('job', {})
        job_id = job.get('job_id') if isinstance(job, dict) else None
        if not isinstance(job_id, str) or not job_id:
            raise VerificationError('smoke job submission returned no job ID')
        deadline = time.monotonic() + max(float(job_timeout_seconds), 1)
        while True:
            snapshot = request_snapshot(
                opener,
                base_url,
                f'/api/v1/jobs/{job_id}',
                headers=authorization,
            )
            require_status(snapshot, 200, 'smoke job read')
            record = decode_json(snapshot, 'smoke job read').get('job', {})
            job_status = record.get('status') if isinstance(record, dict) else None
            if job_status == 'completed':
                break
            if job_status in {'failed', 'cancelled', 'dead_letter', 'blocked'}:
                raise VerificationError(f'smoke job ended as {job_status}')
            if time.monotonic() >= deadline:
                raise VerificationError('smoke job did not complete before timeout')
            time.sleep(1)

    exchange = request_snapshot(
        opener,
        base_url,
        '/api/v1/auth/session',
        method='POST',
        data=b'',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    require_status(exchange, 200, 'browser session exchange')
    session_cookie = cookie_by_name(cookie_jar, '__Host-bioagent_session')
    csrf_cookie = cookie_by_name(cookie_jar, '__Host-bioagent_csrf')
    require_cookie(session_cookie, http_only=True)
    require_cookie(csrf_cookie, http_only=False)

    session = request_snapshot(opener, base_url, '/api/v1/auth/session')
    require_status(session, 200, 'browser session read')
    session_payload = decode_json(session, 'browser session read')
    if session_payload.get('cookie_authenticated') is not True:
        raise VerificationError('browser session was not authenticated by cookie')

    denied_logout = request_snapshot(
        opener,
        base_url,
        '/api/v1/auth/logout',
        method='POST',
        data=b'',
    )
    require_status(denied_logout, 403, 'logout without CSRF token')

    logout = request_snapshot(
        opener,
        base_url,
        '/api/v1/auth/logout',
        method='POST',
        data=b'',
        headers={'X-CSRF-Token': csrf_cookie.value},
    )
    require_status(logout, 204, 'logout with CSRF token')

    revoked = request_snapshot(opener, base_url, '/api/v1/auth/session')
    require_status(revoked, 401, 'revoked browser session')
    return {'origin': base_url, 'status': 'ok'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--job-tool')
    parser.add_argument('--job-timeout-seconds', type=float, default=60)
    arguments = parser.parse_args()
    try:
        result = verify_public_deployment(
            arguments.base_url,
            arguments.username,
            read_secret_file(arguments.password_file),
            job_tool=arguments.job_tool,
            job_timeout_seconds=arguments.job_timeout_seconds,
        )
    except VerificationError as exc:
        parser.error(str(exc))
    print(f"public deployment verification passed: {urlsplit(result['origin']).hostname}")


if __name__ == '__main__':
    main()
