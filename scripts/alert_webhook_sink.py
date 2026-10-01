import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock


def create_handler(output_path):
    target = Path(output_path)
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            if self.path != '/alerts':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > 1024 * 1024:
                self.send_error(413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return
            record = {
                'received_at': datetime.now(timezone.utc).isoformat(),
                'payload': payload,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            with lock, target.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
                stream.write('\n')
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8082)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        create_handler(args.output),
    )
    server.serve_forever()


if __name__ == '__main__':
    main()
