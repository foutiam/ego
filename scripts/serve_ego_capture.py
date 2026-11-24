"""Start only the local capture station and its optional QR-only LAN listener."""
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_WEB = ROOT / 'web/ego-workbench/capture-workflow'
sys.path.insert(0, str(ROOT / 'code'))
from ego_capture.application import Application, CaptureRoutes, ROUTE
from ego_capture.incoming_recordings import IncomingRecordings


class CaptureHandler(CaptureRoutes, BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ('/', ROUTE):
            self.send_response(302)
            self.send_header('Location', ROUTE + '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
        elif parsed.path.startswith(ROUTE + '/'):
            self.get_capture(parsed.path.removeprefix(ROUTE), parse_qs(parsed.query))
        elif parsed.path == '/workflow' or parsed.path.startswith('/workflow/'):
            self.get_input_workspace(parsed.path, parse_qs(parsed.query))
        else:
            self.send_error(404)

    def get_input_workspace(self, path, query):
        assets = {
            '/workflow': ('index.html', 'text/html'),
            '/workflow/': ('index.html', 'text/html'),
            '/workflow/workflow.css': ('workflow.css', 'text/css'),
            '/workflow/workflow.js': ('workflow.js', 'text/javascript'),
        }
        if path in assets:
            name, content_type = assets[path]
            return self.send(200, (WORKFLOW_WEB / name).read_bytes(), content_type + '; charset=utf-8')
        registry = self.app.segments()
        try:
            if path == '/workflow/api/catalog':
                return self.send(200, {'clips': [segment.identity() for segment in registry.items()]})
            if path == '/workflow/api/input':
                from workflow_capture_inputs import capture_inputs
                segment = registry.resolve(query.get('clip', [''])[0])
                prepared = capture_inputs(segment, project=self.app.root)
                frames = []
                for frame in prepared['frames']:
                    urls = []
                    for camera in frame['cameras']:
                        image = Path(camera)
                        encoded = urlencode({'id':segment.recording_id, 'image':(Path(image.parent.name) / image.name).as_posix()})
                        urls.append(f'{ROUTE}/api/image?{encoded}')
                    frames.append(dict(time_s=frame['time_s'],views=urls,touch=frame['tactile'] is not None))
                return self.send(200, dict(recordingId=segment.recording_id, frames=frames,
                                           cameraAnchor=prepared['alignment']['cameraAnchor']))
        except (ValueError, OSError, KeyError, TypeError) as error:
            return self.send(422, {'error':str(error)})
        return self.send(404, {'error':'仅开放 01 片段输入'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith(ROUTE + '/api/'):
            self.post_capture(path.removeprefix(ROUTE))
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8793)
    parser.add_argument('--qr-bind', type=IPv4Address, help='采集电脑在私有局域网中的 IPv4 地址')
    parser.add_argument('--qr-port', type=int, default=8765)
    parser.add_argument('--offline', action='store_true', help='仅处理已保存录制，不打开串口')
    args = parser.parse_args()
    if args.qr_bind and (not args.qr_bind.is_private or args.qr_bind.is_unspecified or args.qr_bind.is_link_local):
        parser.error('二维码服务须绑定采集电脑的具体私有 IP 地址')
    with ThreadingHTTPServer(('127.0.0.1', args.port), CaptureHandler) as server:
        app = Application(args.config, port=server.server_port, standalone=True, connect_devices=not args.offline)
        server.app = app
        inbox = IncomingRecordings(app.inbox)

        def monitor():
            while not app.devices.stop.wait(2):
                app.devices.check_recording_limits()
                app.dispatch_recording_qc()
                try:
                    inbox.dispatch(app)
                except OSError as error:
                    print(f'录制导入目录暂不可读：{error}', flush=True)

        qr = None
        try:
            if args.qr_bind:
                from serve_ego_qr_clock import Handler as QRHandler
                qr = ThreadingHTTPServer((str(args.qr_bind), args.qr_port), QRHandler)
                Thread(target=qr.serve_forever, name='ego-qr-display', daemon=True).start()
                print(f'手机二维码：http://{args.qr_bind}:{qr.server_port}/', flush=True)
            Thread(target=monitor, name='ego-capture-monitor', daemon=True).start()
            print(f'采集工作台：http://127.0.0.1:{server.server_port}{ROUTE}/', flush=True)
            print(f'录制目录：{app.root}', flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if qr:
                qr.shutdown()
                qr.server_close()
            app.devices.close()


if __name__ == '__main__':
    main()
