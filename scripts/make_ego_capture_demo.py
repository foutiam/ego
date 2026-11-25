"""Generate a disposable offline capture rehearsal separate from real recordings."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_capture_delivery import board_view, exo, fixture, recording, save, video


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('demo-station'))
    destination = parser.parse_args().directory.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT/'configs/ego-capture.example.json').read_text(encoding='utf-8'))
    station = destination/'recordings'

    with tempfile.TemporaryDirectory(dir=destination) as staging:
        _, profiles, clocks, _, _ = fixture(Path(staging))
        source_videos = station/'exo-inbox'
        source_profiles = station/'exo-calibrations'
        source_videos.mkdir(parents=True)
        source_profiles.mkdir()
        intrinsics = json.loads((profiles/'lens.json').read_text(encoding='utf-8'))
        board = exo._board().generateImage((800, 500))
        table = board_view(board, np.asarray(intrinsics['K']),
                           np.array([.12, -.09, .08]), np.array([-.23, -.12, .64]))
        exo_frames = []
        for index, clock in enumerate(clocks):
            pixels = table.copy()
            if index / 4 < 3.5 or index / 4 >= 15:
                pixels[110:370, 357:617] = clock[110:370, 190:450]
            else:
                pixels[:, 350:] = clock[:, 350:]
            exo_frames.append(pixels)
        primary = source_videos/'exo1-task.mp4'
        video(primary, exo_frames)
        for eye in (1, 2, 3):
            if eye != 1:
                shutil.copyfile(primary, source_videos/f'exo{eye}-task.mp4')
            shutil.copyfile(profiles/'lens.json', source_profiles/f'exo{eye}-lens.json')

        session = station/'sessions'/'demo-cup-001'
        recording(session, clocks)
    metadata_path = session/'metadata.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    metadata.update(object_id='demo_cup', scene_id='demo_table', split='test', format_version=1,
                    start_host_ns=100_000_000_000, evidence='synthetic_software_fixture',
                    excluded_from_training_data=True)
    save(metadata_path, metadata)
    save(session/'annotations.json', {'reviewed':False})
    save(session/'channels.json', {})
    derived = session/'derived'
    save(derived/'quality.json', {'session_id':session.name,'stereo_pairs_paired':len(clocks)-1})
    stamps = np.arange(0, 18., .02)
    grip = np.interp(stamps, [0, 5.8, 6.3, 10.6, 11.2, 18], [0, 0, 36, 28, 0, 0])
    counts = np.column_stack((grip, .64 * grip)).astype(np.uint8)
    np.savez(derived/'glove_signals.npz', time_s=stamps, score=grip, raw_values=counts,
             byte_offsets=[0, 1], channel_labels=['thumb_pad', 'index_pad'])
    with (derived/'paired_frames.jsonl').open('w', encoding='utf-8') as output:
        for index in range(len(clocks)-1):
            output.write(json.dumps(dict(t_s=index/4, device_ns=70_000_000_000+index*250_000_000,
                image=f'cam0/{index}.jpg', image1=f'cam1/{index}.jpg', stereo_delta_ms=0.))+'\n')
    config_path = destination/'capture.demo.json'
    save(config_path, config)
    print(config_path)


if __name__ == '__main__':
    main()
