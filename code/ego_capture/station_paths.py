"""Workstation paths and the local desktop file picker for capture setup."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from threading import Lock

FOLDERS = ('output_root', 'import_root', 'export_root', 'exo_video_root', 'exo_profile_root')
FILE_TYPES = {
    'exo_video_files': ('.mp4', '.mov', '.m4v'),
    'exo_profile_files': ('.json',),
    'qnbot_runtime_python': ('.exe',),
    'replay_source': ('.bin',),
}
MULTI = frozenset(('exo_video_files', 'exo_profile_files'))
FIELDS = (*FOLDERS, *FILE_TYPES)
PICKER_LOCK = Lock()


def values(config):
    return {key: config.get(key) or ([] if key in MULTI else '') for key in FIELDS}


def resolve(base, value):
    return (base / value).resolve()


def update(config, changes, base):
    if not isinstance(changes, dict) or not set(changes) <= set(FIELDS):
        raise ValueError('路径设置包含未知字段')
    old = values(config)
    normalized = {}
    directories = []
    for key, value in changes.items():
        if key in MULTI:
            if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
                raise ValueError('请输入每行一个文件路径')
            value = list(dict.fromkeys(path.strip() for path in value if path.strip()))
        else:
            if not isinstance(value, str):
                raise ValueError('请输入有效路径')
            value = value.strip()
        if key == 'output_root' and not value:
            raise ValueError('请选择录制保存目录')
        if value == old[key]:
            continue
        paths = value if key in MULTI else [value] if value else []
        for path in paths:
            location = resolve(base, path)
            if key in FOLDERS:
                if location.exists() and not location.is_dir():
                    raise ValueError(f'请选择文件夹：{path}')
                directories.append(location)
            elif location.suffix.lower() not in FILE_TYPES[key] or not location.is_file():
                raise ValueError(f'找不到符合格式的文件：{path}')

        normalized[key] = value

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    result = dict(config)
    for key, value in normalized.items():
        if value:
            result[key] = value
        else:
            result.pop(key, None)
    return result


def choose(field, base, current=''):
    if field not in FIELDS or not isinstance(current, str):
        raise ValueError('请选择有效的路径字段')
    try:
        initial = resolve(base, current) if current else base
    except (ValueError, OSError):
        initial = base
    while not initial.is_dir() and initial != initial.parent:
        initial = initial.parent
    if not PICKER_LOCK.acquire(blocking=False):
        raise ValueError('已有一个选择窗口打开，请先完成或取消。')
    try:
        child = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()), field, str(initial)],
            capture_output=True, encoding='utf-8', timeout=180, check=False,
        )
        if child.returncode:
            raise ValueError('打不开本机选择窗口，可以直接在页面填写路径。')
        return json.loads(child.stdout)
    except subprocess.TimeoutExpired as error:
        raise ValueError('选择窗口已超时，请重试。') from error
    finally:
        PICKER_LOCK.release()


def _desktop_choose(field, initial_directory):
    import tkinter as tk
    from tkinter import filedialog

    window = tk.Tk()
    window.withdraw()
    window.attributes('-topmost', True)
    options = {'parent': window, 'initialdir': initial_directory}
    try:
        if field in FOLDERS:
            selected = filedialog.askdirectory(title='选择文件夹', mustexist=True, **options)
        else:
            filetypes = [('支持的文件', ' '.join('*' + suffix for suffix in FILE_TYPES[field]))]
            if field in MULTI:
                selected = filedialog.askopenfilenames(title='选择文件（可多选）', filetypes=filetypes, **options)
            else:
                selected = filedialog.askopenfilename(title='选择文件', filetypes=filetypes, **options)
        return list(selected) if isinstance(selected, (list, tuple)) else [selected] if selected else []
    finally:
        window.destroy()


if __name__ == '__main__':
    print(json.dumps(_desktop_choose(*sys.argv[1:])))