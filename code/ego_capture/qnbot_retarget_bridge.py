"""Bridge decoded glove poses into the bundled QNBot retargeting SDK."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path


def _build_stamp() -> str:
    """Digest of this file, its mtime and the pid, reported by the live process.

    The station reads its web assets from disk on every request, but this bridge
    is started once and then keeps executing the module it imported. After a code
    change the page therefore shows the new text while the running bridge still
    runs the old code: on 2026-09-21 that made a finished fix look as if it had
    never been applied, and the only way to tell was to compare process start
    times with file mtimes. The station stores the whole calibration_state
    message, so the stamp reaches the page and "which source is live" becomes
    visible instead of inferred.
    """
    try:
        path = Path(__file__).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        when = time.strftime('%m-%d %H:%M', time.localtime(path.stat().st_mtime))
    except OSError:
        return 'unknown'
    return f'{digest} {when} pid{os.getpid()}'


BUILD_STAMP = _build_stamp()


def _prepare_native_runtime() -> None:
    """Expose the runtime's native dependency directory to the DLL loader.

    The bundled retargeting wheels are conda-forge builds whose shared libraries
    (pinocchio, coal/hppfcl, eigenpy, boost, tinyxml2, urdfdom) live in
    <runtime>/Library/bin, which Python does not search by default. Without it the
    first call into the native solver aborted the whole process with
    STATUS_ENTRYPOINT_NOT_FOUND (0xC06D007F): create_retargeter() succeeded, then
    calibrate()/retarget() died while resolving those libraries. Registering the
    directory before the wheels are imported keeps the solver loadable.
    """
    for prefix in (sys.prefix, getattr(sys, "base_prefix", sys.prefix)):
        directory = Path(prefix) / "Library" / "bin"
        if not directory.is_dir():
            continue
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            try:
                add_dll_directory(str(directory))
            except OSError:
                pass
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


_prepare_native_runtime()

import mujoco
import qnbot_hand_openxr_hand
from qnbot_sdk.core import (
    Algorithm,
    CalibrationConfig,
    CalibrationInteraction,
    CalibrationStageSample,
    DataKeys,
    CalibrationStore,
    DeviceSpec,
    ExternalConnection,
    QnBotConfig,
    QnBotSDK,
    RetargetingBinding,
    Side,
    Target,
    TargetKind,
)
from qnbot_sdk.glove import GloveModule


class StationCalibrationInteraction(CalibrationInteraction):
    # Per-sample wait bound, matching the SDK's own
    # TerminalCalibrationInteraction.sample_timeout_sec. A stage that cannot get
    # fresh telemetry now fails loudly instead of silently reusing stale frames.
    sample_timeout_sec = 5.0
    # Whole-stage bound. The SDK reference waits forever; the station must not
    # hang its calibration thread if the glove stops streaming mid-stage.
    stage_timeout_sec = 60.0

    def __init__(self, emit):
        self.emit=emit;self.buffer=None;self.capture=threading.Event();self.cancel=threading.Event()
        self.run_token=0;self.cleared_token=-1;self.active=False

    def collect_stage_samples(self, stage, *, source):
        if self.buffer is None: raise RuntimeError('calibration buffer is not ready')
        # Arm the latch before publishing the prompt. Clearing after the emit lost
        # any capture answered in between: the operator could see awaiting_capture and
        # reply while this thread was still between the two lines, and the later clear()
        # then swallowed that reply and waited for a capture that never came again.
        self.capture.clear()
        self.emit({'kind':'calibration_state','phase':'awaiting_capture','stage':stage.id,
                   'prompt':stage.prompt,'sample_count':stage.sample_count})
        # Fallback for a run that did not open through begin_run: drop a cancel left
        # over from an earlier run, but keep one raised during the current run so the
        # run still stops.
        if self.run_token!=self.cleared_token:
            self.cancel.clear();self.cleared_token=self.run_token
        while not self.capture.wait(.1):
            if self.cancel.is_set(): raise RuntimeError('calibration cancelled')
        key = DataKeys.source_telemetry(source)
        # SDK parity (qnbot_sdk TerminalCalibrationInteraction.collect_stage_samples,
        # core/system.py:1543-1546): the capture window starts at the buffer head as
        # it is AFTER the operator confirms the stage. The previous cursor=0 made
        # wait_next_data() return the OLDEST record still in the ring
        # (core/buffer.py _first_record_after_unlocked(key, 0)), so every stage was
        # fitted on telemetry from before the prompt instead of the pose the
        # operator had just held - the stored hand_system.yaml then did not match
        # the real hand (2026-09-21 report: official output vs live EMF disagreed).
        head = self.buffer.latest_record(key)
        cursor = 0 if head is None else head.sequence
        samples = []
        deadline = time.monotonic() + self.stage_timeout_sec
        self.emit({'kind':'calibration_state','phase':'capturing','stage':stage.id,
                   'prompt':stage.prompt,'captured':0,'sample_count':stage.sample_count})
        while len(samples) < stage.sample_count and time.monotonic() < deadline:
            if self.cancel.is_set(): raise RuntimeError('calibration cancelled')
            record = self.buffer.wait_next_data(key, after_sequence=cursor,
                                                timeout=self.sample_timeout_sec)
            if record is None:
                raise RuntimeError(
                    f'calibration requires fresh telemetry for {source.id}; '
                    f'captured {len(samples)}/{stage.sample_count}')
            cursor = record.sequence
            telemetry = record.value
            payload = getattr(telemetry, 'payload', None)
            if payload is None:
                raise RuntimeError(f'calibration requires telemetry for {source.id}')
            # Same guard as the SDK's _accepted_glove_payload: a non-glove payload
            # would be fitted into the mapping silently.
            if not (hasattr(payload, 'finger_names') and hasattr(payload, 'fingertip_local')
                    and hasattr(payload, 'raw_valid_mask')):
                raise RuntimeError(f'calibration requires GloveFeaturePayload for {source.id}')
            samples.append(CalibrationStageSample(stage_id=stage.id, payload=payload,
                                                  timestamp=record.timestamp))
            self.emit({'kind':'calibration_state','phase':'capturing','stage':stage.id,
                       'prompt':stage.prompt,'captured':len(samples),
                       'sample_count':stage.sample_count})
        if len(samples)!=stage.sample_count: raise RuntimeError('采集姿势数据超时')
        return tuple(samples)

    def begin_run(self, buffer):
        # A cancel belongs to one calibration run, not to the interaction object's
        # lifetime. self.cancel is created once in __init__; when it was set and never
        # cleared, every later calibration aborted with 'calibration cancelled' before
        # its first stage. Opening a run drops any cancel left over from an earlier one
        # while a cancel raised during this run is still honoured.
        self.buffer=buffer;self.run_token+=1;self.cleared_token=self.run_token
        self.cancel.clear();self.capture.clear()

    def request_capture(self):
        self.capture.set()

    def request_cancel(self):
        # Only a live run may be cancelled. Setting the flag while no run was in
        # flight left it set for the next calibration, which then failed at once
        # with 'calibration cancelled'.
        if self.active:
            self.cancel.set();self.capture.set()
        else:
            self.cancel.clear();self.capture.clear()

    def calibration_complete(self, **kwargs):
        # The record is written, but the run is not over: the caller still has to
        # bring the glove workflow up on the new record. Reporting the terminal
        # 'complete' here would re-enable the operator's start button while
        # calibration_interaction.active is still True, and an immediate second
        # press would then be refused with '标定已在进行中' even though nothing was
        # being captured. run_external_calibration emits the terminal state once
        # the workflow is actually up.
        self.emit({'kind':'calibration_state','phase':'finalizing',
                   'record_id':kwargs['record'].record_id,
                   'message':'标定记录已写入，正在启用官方输出…'})


NODE_NAMES = (
    "wrist",
    "thumb_metacarpal", "thumb_proximal", "thumb_distal", "thumb_tip",
    "index_metacarpal", "index_proximal", "index_intermediate", "index_distal", "index_tip",
    "middle_metacarpal", "middle_proximal", "middle_intermediate", "middle_distal", "middle_tip",
    "ring_metacarpal", "ring_proximal", "ring_intermediate", "ring_distal", "ring_tip",
    "little_metacarpal", "little_proximal", "little_intermediate", "little_distal", "little_tip",
)


def create_skeleton():
    root = Path(qnbot_hand_openxr_hand.__file__).resolve().parent
    path = root / "hands" / "openxr_hand" / "resources" / "pinocchio" / "left" / "skeleton.xml"
    model = mujoco.MjModel.from_xml_path(path.as_posix())
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)
    site_ids = {
        name: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "left_" + name)
        for name in NODE_NAMES
    }
    if any(site_id < 0 for site_id in site_ids.values()):
        raise RuntimeError("OpenXR skeleton model is missing one or more named 25-point sites")
    joint_ids = {
        model.joint(index).name: int(model.joint(index).qposadr[0])
        for index in range(model.njnt)
        if model.joint(index).name
    }
    return model, data, site_ids, joint_ids


def update_skeleton(model, data, site_ids, joint_ids, joints):
    for raw_name, raw_value in joints.items():
        name = str(raw_name)
        candidates = (name, "left_" + name, name.removeprefix("left_"))
        joint_name = next((candidate for candidate in candidates if candidate in joint_ids), None)
        if joint_name is not None:
            data.qpos[joint_ids[joint_name]] = float(raw_value)
    mujoco.mj_forward(model, data)
    wrist_id = site_ids["wrist"]
    wrist_position = [float(value) for value in data.site_xpos[wrist_id]]
    wrist_rotation = _matrix(data.site_xmat[wrist_id])
    rotation_inverse = _transpose(wrist_rotation)
    positions = []
    orientations = []
    for name in NODE_NAMES:
        site_id = site_ids[name]
        position = _mat_vec(rotation_inverse, [float(value) - wrist_position[index]
                               for index, value in enumerate(data.site_xpos[site_id])])
        relative_rotation = _mat_mul(rotation_inverse, _matrix(data.site_xmat[site_id]))
        quaternion = rotation_to_quaternion_xyzw(relative_rotation)
        positions.append([float(value) for value in position])
        orientations.append([float(value) for value in quaternion])
    return positions, orientations


def rotation_to_quaternion_xyzw(matrix):
    """Convert a right-handed rotation matrix to an xyzw unit quaternion."""
    trace = float(matrix[0][0] + matrix[1][1] + matrix[2][2])
    if trace > 0.0:
        scale = 2.0 * math.sqrt(trace + 1.0)
        w = 0.25 * scale
        x = (matrix[2][1] - matrix[1][2]) / scale
        y = (matrix[0][2] - matrix[2][0]) / scale
        z = (matrix[1][0] - matrix[0][1]) / scale
    elif matrix[0][0] > matrix[1][1] and matrix[0][0] > matrix[2][2]:
        scale = 2.0 * math.sqrt(1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2])
        w = (matrix[2][1] - matrix[1][2]) / scale
        x = 0.25 * scale
        y = (matrix[0][1] + matrix[1][0]) / scale
        z = (matrix[0][2] + matrix[2][0]) / scale
    elif matrix[1][1] > matrix[2][2]:
        scale = 2.0 * math.sqrt(1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2])
        w = (matrix[0][2] - matrix[2][0]) / scale
        x = (matrix[0][1] + matrix[1][0]) / scale
        y = 0.25 * scale
        z = (matrix[1][2] + matrix[2][1]) / scale
    else:
        scale = 2.0 * math.sqrt(1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1])
        w = (matrix[1][0] - matrix[0][1]) / scale
        x = (matrix[0][2] + matrix[2][0]) / scale
        y = (matrix[1][2] + matrix[2][1]) / scale
        z = 0.25 * scale
    norm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    return [x / norm, y / norm, z / norm, w / norm]


def _matrix(values):
    flat = [float(value) for value in values]
    return [flat[0:3], flat[3:6], flat[6:9]]


def _transpose(matrix):
    return [[matrix[row][column] for row in range(3)] for column in range(3)]


def _mat_vec(matrix, vector):
    return [sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3)]


def _mat_mul(left, right):
    return [[sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3)]
            for row in range(3)]


def stored_calibration_record(controller):
    """The record the SDK itself would reuse for this binding, or None.

    ``ensure_required`` skips the capture flow only when ``_find_existing``
    answers with a record for this binding, this operator and a calibration
    version the retargeting algorithm still accepts
    (qnbot_sdk/core/system.py:1208-1270). Asking the same question through the
    same method is what keeps this check honest: a hand-built filter could answer
    "record present" for a record the SDK then rejects, and the fallback path is
    the vendor's own first-time flow, which waits for poses the station never
    drives.
    """
    bindings = getattr(controller, 'bindings', None) or ()
    find_existing = getattr(controller, '_find_existing', None)
    if callable(find_existing):
        for binding in bindings:
            try:
                record = find_existing(binding)
            except Exception:
                continue
            if record is not None:
                return record
        return None
    # Fallback for a wheel without _find_existing: match on the same binding
    # identity, operator-matched, which is the strictest filter available here.
    store = getattr(controller, 'store', None)
    if store is None:
        return None
    load = getattr(store, 'load', None)
    if callable(load):
        load()
    operator_id = getattr(controller, 'operator_id', None)
    for binding in bindings:
        record = store.find(source=binding.source, target=binding.target,
                            operator_id=operator_id, match_operator=bool(operator_id))
        if record is not None:
            return record
    return None


def record_written_at(controller, record) -> str | None:
    """When the reused record's file was last written, in local time, or None.

    ``CalibrationRecord.created_at`` is a monotonic counter, not a clock, so the
    store file's mtime is the only wall-clock fact available. The page shows it
    because the record cannot say whether the glove was re-donned since
    (qnbot_sdk/core/calibration.py:176-198 keys it by operator and algorithm only):
    how old the file is, is the operator's only cue for judging whether the stored
    calibration still applies to the hand wearing the glove now.
    """
    store = getattr(controller, 'store', None)
    path_for = getattr(store, 'path_for', None)
    if not callable(path_for):
        return None
    try:
        return time.strftime('%Y-%m-%d %H:%M',
                             time.localtime(path_for(record).stat().st_mtime))
    except (OSError, ValueError, TypeError):
        return None


def reuse_stored_calibration(sdk, emit) -> bool:
    """Enable the official output from the record already on disk, re-measuring nothing.

    ``glove.start()`` reaches a stored record through ``ensure_required``
    (qnbot_sdk/core/system.py:1230) with the config's ``force=False``, so it
    returns that record and captures no pose. That is the only safe way to bring
    the workflow up before the operator has pressed the calibration button: with
    no record present the very same call runs the vendor's own first-time flow,
    which blocks on poses the station never drives. The record is therefore
    checked first, with the SDK's own matching rules, and the workflow is started
    only when one exists.

    Returns True when the official output is now running on a stored record. The
    message names the record instead of implying a fresh measurement, and says
    that the record belongs to one wearing session: it is keyed by operator and
    algorithm (qnbot_sdk/core/calibration.py:176-198) and cannot tell whether the
    glove was re-donned since.
    """
    controller = getattr(getattr(sdk, 'system', None), 'calibration', None)
    if controller is None:
        return False
    record = stored_calibration_record(controller)
    if record is None:
        emit({'kind': 'calibration_state', 'phase': 'absent',
              'message': '尚无已存标定记录：官方输出未启用，点击「开始 / 重新 external 标定」完成一次标定'})
        return False
    sdk.glove.start()
    emit({'kind': 'calibration_state', 'phase': 'reused',
          'record_id': getattr(record, 'record_id', None),
          'record_written': record_written_at(controller, record),
          'message': '使用上次标定成果（未重新采集）：同一佩戴会话内有效，重新佩戴后请重新标定'})
    return True


def run_external_calibration(sdk, emit) -> None:
    """Re-measure glove -> hand for real, and report only what actually happened.

    The operator pressed the calibration button, so this run must re-measure. Two
    SDK behaviours otherwise turn that click into a silent no-op that still looks
    successful:

    * ``CalibrationController.run`` returns the record already stored for this
      binding and never asks for a single pose unless ``force`` is set
      (qnbot_sdk/core/system.py:999-1019). ``glove.start()`` only reaches that
      cache through ``ensure_required`` (core/system.py:1230), so once a
      hand_system.yaml existed the UI kept announcing a completed calibration
      that had re-measured nothing - the reported "cannot re-calibrate" symptom
      of 2026-09-21.
    * ``_stage_samples_for`` replays the samples an earlier stage captured in the
      same process (core/system.py:1174-1178), so a second calibration in one
      bridge lifetime would be fitted on the first one's frames.

    ``force=True`` plus a cleared stage cache is what makes the button mean
    "re-calibrate". The glove workflow is started only after the record exists:
    ``push_frame`` fills the buffer regardless of the workflow state (the SDK's
    own first-time flow captures from inside ``glove.start()``, before
    ``workflow.start()``), and starting afterwards cannot launch a second,
    competing calibration through ``ensure_required``.
    """
    controller = getattr(getattr(sdk, 'system', None), 'calibration', None)
    if controller is None:
        raise RuntimeError('标定控制器不可用：sdk.system.calibration 缺失')
    stage_samples = getattr(controller, '_stage_samples', None)
    if stage_samples is not None:
        stage_samples.clear()
    emit({'kind': 'calibration_state', 'phase': 'starting'})
    record = controller.run(force=True)
    # The workflow is brought up on the fresh record only after it exists, so
    # ensure_required() inside start() sees a cache hit and cannot launch a
    # second, competing calibration. The terminal state is emitted only once this
    # returns, so the operator's button cannot be re-enabled while the run is
    # still finishing.
    sdk.glove.start()
    emit({'kind': 'calibration_state', 'phase': 'complete',
          'record_id': getattr(record, 'record_id', None),
          'message': '标定完成，官方输出已启用（本次已重新采集）'})


def main() -> int:
    root = Path(sys.argv[1])
    side = Side.LEFT
    device = DeviceSpec.glove(side=side, connection=ExternalConnection())
    target = Target(kind=TargetKind.HAND, name="openxr_hand", side=side)
    config = QnBotConfig(
        targets=(target,),
        devices=(device,),
        algorithms={
            "retargeting": (Algorithm(path="installed:qnbot-hand-openxr-hand"),),
        },
        retargeting_bindings=(RetargetingBinding(
            source=device.source,
            target=target,
            algorithm_id="qnbot.hand.openxr_hand.retargeting",
        ),),
        calibration=CalibrationConfig(
            operator_id="transparent-depth-capture-station",
            run_if_missing=True,
        ),
    )
    output_lock = threading.Lock()

    def emit(payload):
        # ensure_ascii keeps the wire bytes pure ASCII. The vendor runtime's stdout
        # encoding is the Windows ANSI codepage (gbk here), not UTF-8, so a Chinese
        # message written verbatim reached the station as gbk bytes and the station's
        # utf-8 reader thread died on the first one, freezing every later
        # joint_command/skeleton_pose. json.loads turns \uXXXX back into the same
        # characters, so the page still shows them.
        #
        # The state messages carry this process's build stamp so the page can show
        # which bridge source is actually running (see _build_stamp).
        if payload.get('kind') in ('ready', 'calibration_state', 'error'):
            payload = {**payload, 'build': BUILD_STAMP}
        with output_lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(',',':'))+'\n');sys.stdout.flush()

    calibration_interaction=StationCalibrationInteraction(emit)
    sdk = QnBotSDK.create(
        config,
        modules=(GloveModule(),),
        calibration_store=CalibrationStore(path=root / "qnbot-calibration"),
        calibration_interaction=calibration_interaction,
    )
    try:
        skeleton_model, skeleton_data, skeleton_sites, skeleton_joints = create_skeleton()
        skeleton_error = None
    except Exception as exc:
        skeleton_model = skeleton_data = skeleton_sites = skeleton_joints = None
        skeleton_error = f"{type(exc).__name__}: {exc}"
    calibration_thread=None

    def on_command(command: object) -> None:
        emit({"kind": "joint_command", "value": command.to_dict()})
        if skeleton_model is None or not hasattr(command, "joints"):
            return
        positions, orientations = update_skeleton(
            skeleton_model, skeleton_data, skeleton_sites, skeleton_joints, command.joints
        )
        emit({"kind": "skeleton_pose", "value": {
            "timestamp_sec": float(command.timestamp),
            "hand_side": "left",
            "coordinate_frame": "wrist_local",
            "names": list(NODE_NAMES),
            "positions_local": positions,
            "orientations_local_xyzw": orientations,
        }})

    sdk.outputs.register(target, on_command)
    try:
        sdk.connect()
    except Exception as exc:
        emit({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    emit({"kind": "ready", "skeleton_api": skeleton_model is not None,
          "skeleton_error": skeleton_error, "serial_owner": "ego_capture"})
    # GloveControlWorkflow only emits commands from its tick(), which the SDK schedules
    # on the system runtime loop. Nothing else pumps that loop in this process, so
    # without a driver thread push_frame() fills the buffer but no joint_command or
    # skeleton_pose is ever produced.
    runtime_thread = threading.Thread(target=sdk.system.run_forever,
                                      name='qnbot-runtime', daemon=True)
    runtime_thread.start()
    # Enable the official output from the record already on disk before the
    # operator does anything. Without this the workflow stayed down until a
    # calibration run finished, so a station that already had a record still
    # reported "需要标定才能显示手部pose" and drew no skeleton (2026-09-22).
    # The record is checked before start(), so this can never enter the vendor's
    # own first-time capture flow.
    try:
        reuse_stored_calibration(sdk, emit)
    except Exception as exc:
        emit({'kind': 'calibration_state', 'phase': 'absent',
              'message': f'未启用已存标定记录：{type(exc).__name__}: {exc}；可点击「开始 / 重新 external 标定」重新标定'})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("kind") == "frame":
                sdk.glove.push_frame(
                    message["fingers"],
                    timestamp_sec=float(message["timestamp_sec"]),
                )
            elif message.get('kind')=='calibration_start':
                if calibration_interaction.active:
                    # Two overlapping runs would fight over the capture latch and
                    # over the same stage ids. Refuse the second one instead.
                    emit({'kind':'calibration_state','phase':'error',
                          'message':'标定已在进行中：请先完成当前姿势采集或点击取消'})
                else:
                    calibration_interaction.begin_run(sdk.system.buffer)
                    calibration_interaction.active=True
                    def run_calibration():
                        try:
                            run_external_calibration(sdk, emit)
                        except Exception as exc: emit({'kind':'calibration_state','phase':'error','message':str(exc)})
                        finally: calibration_interaction.active=False
                    calibration_thread=threading.Thread(target=run_calibration,name='external-calibration',daemon=True);calibration_thread.start()
            elif message.get('kind')=='calibration_capture':
                calibration_interaction.request_capture()
            elif message.get('kind')=='calibration_cancel':
                calibration_interaction.request_cancel()
            elif message.get('kind')=='calibration_reuse':
                # Fall back to the record on disk without measuring anything: the
                # operator may have cancelled a run, or the workflow may have been
                # stopped, and the stored record is still the one the SDK accepts.
                if calibration_interaction.active:
                    emit({'kind':'calibration_state','phase':'error',
                          'message':'标定已在进行中：请先完成当前姿势采集或点击取消'})
                else:
                    try:
                        reuse_stored_calibration(sdk, emit)
                    except Exception as exc:
                        emit({'kind':'calibration_state','phase':'error','message':str(exc)})
            elif message.get("kind") == "stop":
                break
    except Exception as exc:
        emit({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        try:
            sdk.system.runtime_loop.stop()
        except Exception:
            pass
        try:
            sdk.disconnect()
        except RuntimeError as exc:
            # ExternalGloveDriver has no haptics writer; older SDK shutdown
            # still tries to clear a command that was never serial-backed.
            emit({"kind": "shutdown_warning", "message": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
