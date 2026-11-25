"""Optional QR-only listener for a phone on the same local network."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from ego_capture.qr_display import response


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    disable_nagle_algorithm = True
    timeout = 8
    wbufsize = 4096

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


def qr_address(value):
    ip = IPv4Address(value)
    if not ip.is_private or ip.is_unspecified or ip.is_link_local:
        raise argparse.ArgumentTypeError('请选择采集电脑的具体私有 IPv4 地址')
    return ip


def add_phone_qr_arguments(parser):
    parser.add_argument('--qr-bind', type=qr_address, help='采集电脑在私有局域网中的 IPv4 地址')
    parser.add_argument('--qr-port', type=int, default=8765)


@contextmanager
def phone_qr(app, args):
    if args.qr_bind is None:
        yield
        return
    with ThreadingHTTPServer((str(args.qr_bind), args.qr_port), Handler) as server:
        app.qr_phone_url = f'http://{args.qr_bind}:{server.server_port}/'
        worker = Thread(target=server.serve_forever, name='ego-qr-display', daemon=True)
        worker.start()
        print(f'手机二维码：{app.qr_phone_url}', flush=True)
        try:
            yield
        finally:
            server.shutdown()
            worker.join()
            app.qr_phone_url = None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, type=qr_address, help='Computer IPv4 address on the local network')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer((str(args.bind), args.port), Handler) as server:
        print(f'QR display: http://{args.bind}:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
