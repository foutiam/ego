"""Shared capture-station board scale and imported EXO intrinsics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ego_capture import station_paths
from ego_capture.exo_calibration import BOARD, read_profile
from visuotactile.pipeline import read_json
from visuotactile.storage import dump_json


SQUARE_KEY = 'exo_square_mm'
DEFAULT_SQUARE_MM = BOARD['square_m'] * 1000
IMPORTED_PROFILE = re.compile(r'^(.*?)--([0-9a-f]{16})\.json$')


def _valid_square(square_mm):
    if type(square_mm) not in (int, float) or not 15 <= square_mm <= 40:
        raise ValueError('请输入 15–40 mm 的实测格长')
    return round(float(square_mm), 2)


def square_mm(app):
    return _valid_square(read_json(app.config_path).get(SQUARE_KEY, DEFAULT_SQUARE_MM))


def state(app):
    profiles = []
    for profile_id, path in sorted(app.exo_intrinsics().items()):
        try:
            record = read_profile(path)
        except (OSError, ValueError, TypeError):
            continue
        intrinsics = record.get('intrinsics', record)
        imported = IMPORTED_PROFILE.fullmatch(profile_id)
        profiles.append(dict(id=profile_id, label=imported[1] if imported else profile_id,
                             image_size=intrinsics['image_size'], model=intrinsics['model']))
    return dict(square_mm=square_mm(app),
                board=dict(columns=BOARD['width'], rows=BOARD['height']), profiles=profiles)


def save(app, data):
    measured = _valid_square(data.get('square_mm'))
    with app.lock:
        config = read_json(app.config_path)
        if config.get(SQUARE_KEY, DEFAULT_SQUARE_MM) != measured:
            config[SQUARE_KEY] = measured
            dump_json(app.config_path, config)
    return state(app)


def _digest(profile):
    data = json.dumps(profile, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]


def import_profiles(app):
    chosen = station_paths.choose('exo_profile_files', app.config_base, str(app.exo_profiles))
    validated = {}
    for name in chosen:
        path = Path(name)
        if path.suffix.lower() != '.json':
            raise ValueError('请选择 EXO 内参 JSON')
        try:
            record = read_profile(path)
            identifier = _digest(record)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f'{path.name} 无法用作相机内参。{error}') from error
        validated.setdefault(identifier, (path.stem[:80], record))
    added = []
    with app.lock:
        for identifier, (label, record) in validated.items():
            existing = next((path for path in app.exo_profiles.glob(f'*--{identifier}.json')
                             if path.is_file() and not path.is_symlink()), None)
            if existing is not None:
                if _digest(read_profile(existing)) != identifier:
                    raise ValueError(f'内参库中的 {existing.name} 内容已变化，请检查档案')
                continue
            destination = app.exo_profiles / f'{label}--{identifier}.json'
            if destination.is_symlink():
                raise ValueError(f'无法写入内参库中的 {destination.name}')
            dump_json(destination, record)
            added.append(destination.name)
    return dict(state(app), added=added, selected=len(chosen))
