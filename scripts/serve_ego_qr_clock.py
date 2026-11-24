"""Optional QR-only listener for a phone on the same local network."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from ego_capture.qr_display import response


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        payload = response('/qr-timecode.html' if path == '/' else path)
        if payload is None:
            self.send_error(404)
            return
        body, media_type = payload
        if isinstance(body, dict):
            body = json.dumps(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, type=IPv4Address, help='Computer IPv4 address on the local network')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not args.bind.is_private or args.bind.is_unspecified or args.bind.is_link_local:
        parser.error('Choose the computer’s specific private LAN or loopback address.')
    with ThreadingHTTPServer((str(args.bind), args.port), Handler) as server:
        print(f'QR display: http://{args.bind}:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
