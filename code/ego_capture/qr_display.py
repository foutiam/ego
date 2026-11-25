"""Serve only the QR display and the recording computer's monotonic clock."""
import time
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'web/ego-workbench/capture'
RENDERER = ROOT / 'third_party/ego-iphone-qr-sync/qrcode.js'
ASSETS = {
    '/qr-timecode.html': (WEB / 'qr-timecode.html', 'text/html; charset=utf-8'),
    '/qr-timecode.js': (WEB / 'qr-timecode.js', 'application/javascript; charset=utf-8'),
    '/qr-renderer.js': (RENDERER, 'application/javascript; charset=utf-8'),
}


@lru_cache(maxsize=1)
def screen_board():
    from ego_capture.exo_calibration import board_png
    return board_png()


def response(path):
    if path == '/qr-clock':
        return {'hostMonoMs': time.monotonic_ns() // 1_000_000}, 'application/json; charset=utf-8'
    if path == '/qr-board.png':
        return screen_board(), 'image/png'
    asset = ASSETS.get(path)
    if asset:
        file, media_type = asset
        return file.read_bytes(), media_type
    return None
