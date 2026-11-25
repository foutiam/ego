"""Live DirectShow EXO previews and session-local video recording."""
from __future__ import annotations

import csv
from fractions import Fraction
import re
import sys
import threading
import time

import av

from visuotactile.pose import jpeg_preview


ROLES = ('exo1', 'exo2', 'exo3')
TIME_BASE = Fraction(1, 90_000)


def directshow_sources():
    if sys.platform != 'win32':
        return []
    # FFmpeg's device listing exits intentionally. Capture only this thread's
    # log fragments, which include the stable alternative name of each device.
    if 'dshow' not in av.formats_available:
        raise ValueError('当前视频运行环境不支持 Windows 相机输入')
    if av.logging.get_level() is None or av.logging.get_level() < av.logging.INFO:
        av.logging.set_level(av.logging.INFO)
    with av.logging.Capture() as messages:
        try:
            with av.open('dummy', format='dshow', options={'list_devices': 'true'}):
                pass
        except av.error.ExitError:
            pass
    native = ''.join(message for _, name, message in messages if name == 'dshow')
    sources = []
    for name, kind, alternative in re.findall(
            r'(?m)^"([^"\r\n]+)" \((video|audio)(?:, audio)?\)[^\n]*\n\s+Alternative name "([^"\r\n]+)"', native):
        if kind == 'video':
            sources.append(dict(id=alternative, label=name, virtual=alternative.startswith('@device_sw_')))
    return sorted(sources, key=lambda item: (item['virtual'], item['label'], item['id']))


class ExoVideo:
    def __init__(self, path, source, rate):
        self.path = path
        self.source = source
        self.rate = rate
        self.container = None
        self.stream = None
        self.timestamps = None
        self.first_pts = None
        self.first_arrival_ns = None
        self.last_pts = -1
        self.frames = 0
        self.size = 0
        self.error = None

    def write(self, frame, host_ns, mono_ns):
        if self.stream is None:
            self.container = av.open(str(self.path), 'w')
            self.stream = self.container.add_stream('libx264', rate=self.rate)
            self.stream.width, self.stream.height = frame.width, frame.height
            self.stream.pix_fmt = 'yuv420p'
            self.stream.time_base = self.stream.codec_context.time_base = TIME_BASE
            self.stream.codec_context.thread_count = 1
            self.stream.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '18'}
            self.timestamps = self.path.with_suffix('.csv').open('x', encoding='utf-8', newline='')
            self.csv = csv.writer(self.timestamps)
            self.csv.writerow(('frame','video_pts','source_pts_s','host_ns','host_mono_ns'))
        elif (frame.width, frame.height) != (self.stream.width, self.stream.height):
            raise ValueError('EXO 录制中画面尺寸发生变化')
        source_pts = frame.pts * frame.time_base if frame.pts is not None and frame.time_base else None
        if self.first_arrival_ns is None:
            self.first_arrival_ns = mono_ns
            self.first_pts = source_pts
        elapsed = (source_pts - self.first_pts if source_pts is not None and self.first_pts is not None
                   else Fraction(mono_ns - self.first_arrival_ns, 1_000_000_000))
        pts = max(self.last_pts + 1, round(elapsed / TIME_BASE))
        video_frame = frame.reformat(format='yuv420p')
        video_frame.pts, video_frame.time_base = pts, TIME_BASE
        for packet in self.stream.encode(video_frame):
            self.container.mux(packet)
        self.csv.writerow((self.frames, pts, float(source_pts) if source_pts is not None else '', host_ns, mono_ns))
        self.last_pts = pts
        self.frames += 1
        if self.frames == 1 or self.frames % 16 == 0:
            self.size = self.path.stat().st_size

    def finish(self):
        try:
            if self.stream:
                for packet in self.stream.encode():
                    self.container.mux(packet)
        except Exception as error:
            self.error = self.error or str(error)
        finally:
            try:
                if self.container:
                    self.container.close()
            except Exception as error:
                self.error = self.error or str(error)
            finally:
                if self.timestamps:
                    try:
                        self.timestamps.close()
                    except OSError as error:
                        self.error = self.error or str(error)
        return dict(source=self.source, status='saved' if self.frames and not self.error else 'incomplete',
                    video=f'exo-live/{self.path.name}' if self.path.is_file() else None,
                    timestamps=f'exo-live/{self.path.with_suffix(".csv").name}' if self.timestamps else None,
                    frames=self.frames, video_time_base=str(TIME_BASE), error=self.error)


class ExoFeed:
    def __init__(self, source):
        self.source = dict(source)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.preview = None
        self.arrival_ns = 0
        self.shape = None
        self.rate = Fraction(30)
        self.error = None
        self.writer = None
        self.thread = threading.Thread(target=self.run, name='ego-exo-camera', daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                with av.open('video=' + self.source['id'], format='dshow', timeout=(4, 2)) as camera:
                    stream = camera.streams.video[0]
                    rate = stream.average_rate or stream.base_rate or Fraction(30)
                    for frame in camera.decode(video=0):
                        if self.stop.is_set():
                            return
                        wall, mono = time.time_ns(), time.monotonic_ns()
                        with self.lock:
                            self.arrival_ns = mono
                            self.shape = [frame.width, frame.height]
                            self.rate = rate
                            self.error = None
                            writer = self.writer
                            if writer and not writer.error:
                                try:
                                    writer.write(frame, wall, mono)
                                except Exception as error:
                                    writer.error = str(error)
                            want_preview = not self.preview or mono - self.preview[0] >= 100_000_000
                        if want_preview:
                            height = max(1, round(frame.height * min(frame.width, 640) / frame.width))
                            rgb = frame.reformat(width=min(frame.width, 640), height=height, format='rgb24').to_ndarray()
                            preview = jpeg_preview(rgb, width=640)
                            with self.lock:
                                self.preview = (mono, preview)
                    raise OSError('相机视频流已结束')
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    self.preview = None
                    if self.writer:
                        self.writer.error = self.writer.error or self.error
                self.stop.wait(1)


class LiveExo:
    def __init__(self):
        self.catalog = {}
        self.feeds = {}

    def discover(self):
        self.catalog = {source['id']: source for source in directshow_sources()}
        return list(self.catalog.values())

    def bind(self, role, source_id):
        if role not in ROLES:
            raise ValueError('请选择 EXO 1–3')
        old = self.feeds.get(role)
        if (old.source['id'] if old else '') == source_id:
            return
        source = self.catalog.get(source_id) if source_id else None
        if source_id and not source:
            raise ValueError('该相机已不在设备列表，刷新后重选')
        if source and any(feed.source['id'] == source_id for feed in self.feeds.values()):
            raise ValueError('这路相机已经分配给其他 EXO')
        if old:
            old.stop.set()
            self.feeds.pop(role)
        if source:
            self.feeds[role] = ExoFeed(source)

    def state(self):
        result = {}
        now = time.monotonic_ns()
        for role, feed in self.feeds.items():
            with feed.lock:
                image = feed.preview
                ready = image is not None and now - image[0] < 3_000_000_000
                result[role] = dict(id=feed.source['id'], ready=ready, shape=feed.shape,
                                    preview_id=image[0] if ready else None,
                                    error=feed.writer.error if feed.writer and feed.writer.error else feed.error)
        return result

    def start(self, session):
        if not self.feeds:
            return
        folder = session.path / 'exo-live'
        folder.mkdir()
        for role, feed in self.feeds.items():
            with feed.lock:
                feed.writer = ExoVideo(folder / f'{role}-live.mp4', feed.source, feed.rate)

    def finish(self, session):
        result = {}
        for role, feed in self.feeds.items():
            with feed.lock:
                if not feed.writer:
                    continue
                result[role] = feed.writer.finish()
                feed.writer = None
        if result:
            session.metadata['exo_live'] = result

    def bytes(self):
        return sum(feed.writer.size for feed in self.feeds.values() if feed.writer)

    def close(self):
        for feed in self.feeds.values():
            feed.stop.set()

