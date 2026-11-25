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
from test_capture_delivery import fixture, recording, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('demo-station'))
    destination = parser.parse_args().directory.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT/'configs/ego-capture.example.json').read_text(encoding='utf-8'))
    station = destination/'recordings'

    with tempfile.TemporaryDirectory(dir=destination) as staging:
        videos, profiles, clocks, _, _ = fixture(Path(staging))
        source_videos = station/'exo-inbox'
        source_profiles = station/'exo-calibrations'
        source_videos.mkdir(parents=True)
        source_profiles.mkdir()
        for eye in (1, 2, 3):
            shutil.copyfile(videos/f'exo{eye}.mp4', source_videos/f'exo{eye}-task.mp4')
            shutil.copyfile(videos/'table.mp4', source_videos/f'exo{eye}-table.mp4')
            shutil.copyfile(videos/'varied.mp4', source_videos/f'exo{eye}-intrinsics.mp4')
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
