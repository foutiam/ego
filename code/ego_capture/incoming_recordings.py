"""Automatically ingest complete session folders from the workbench inbox."""
from pathlib import Path


class IncomingRecordings:
    def __init__(self, inbox: Path):
        self.inbox = inbox
        self.observed = {}
        self.attempted = {}

    @staticmethod
    def snapshot(folder):
        return tuple(sorted(
            (file.name, info.st_size, info.st_mtime_ns)
            for file in folder.iterdir() if file.is_file() and not file.is_symlink()
            for info in (file.stat(),)
        ))

    def dispatch(self, app):
        if app.devices.session or (app.job or {}).get('status') == 'running':
            return
        if not self.inbox.is_dir() or self.inbox.resolve() != self.inbox:
            return
        current = {}
        for folder in sorted(self.inbox.iterdir()):
            if (not folder.is_dir() or folder.resolve() != folder
                    or (app.root / 'sessions' / folder.name).exists()):
                continue
            try:
                current[folder.name] = self.snapshot(folder)
            except OSError:
                continue
        previous = self.observed
        self.observed = current
        self.attempted = {name: snapshot for name, snapshot in self.attempted.items()
                          if current.get(name) == snapshot}
        for name, snapshot in current.items():
            # A copied-in folder may be populated file by file. Wait until two scans agree.
            if previous.get(name) != snapshot or self.attempted.get(name) == snapshot:
                continue
            try:
                app.import_source(name)
            except (OSError, ValueError):
                self.attempted[name] = snapshot
                continue
            try:
                app.import_recording(name)
            except ValueError:
                if not app.devices.session and (app.job or {}).get('status') != 'running':
                    self.attempted[name] = snapshot
                return
            self.attempted[name] = snapshot
            return
