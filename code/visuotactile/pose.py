"""Local MediaPipe pose estimates in image and hand-relative coordinates."""
import time
import cv2
import numpy as np

CONNECTIONS = [(0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),(5,9),(9,10),(10,11),(11,12),
               (9,13),(13,14),(14,15),(15,16),(13,17),(0,17),(17,18),(18,19),(19,20)]


def select_hand_label(detected_labels, preferred='auto'):
    labels=[label for label in detected_labels if label in ('Left','Right')]
    if not labels: return None
    preferred=(preferred or 'auto').strip()
    if preferred in ('Left','Right'):
        if preferred in labels: return preferred
        return labels[0]
    return labels[0]


class HandPose:
    """Bounded gloved-hand fallback: full frame, ROI retry around the last hit,
    then a lazily built lower-threshold model. Fallback passes are flagged and
    never upgrade the reported confidence meaning."""

    def __init__(self, side='auto', min_detection=.5, fallback_min_detection=.25,
                 roi_retry=True, model_factory=None, *, strict_physical_side=False):
        self.side = side if side else 'auto'
        self.strict_physical_side = strict_physical_side
        if model_factory is None:
            # Only the real MediaPipe path needs the dependency. Importing it
            # unconditionally made an injected factory (the documented test seam)
            # fail on any machine without mediapipe, so the seam did not actually
            # decouple the class from the optional dependency.
            import mediapipe as mp
            model_factory = lambda det: mp.solutions.hands.Hands(
                static_image_mode=False, max_num_hands=2, model_complexity=1,
                min_detection_confidence=det, min_tracking_confidence=.5)
        factory = model_factory
        self._factory = factory
        t0 = time.perf_counter()
        self.model = factory(min_detection)
        self.model_load_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        self.fallback_min_detection = fallback_min_detection
        self.fallback_model = None
        self.roi_retry = bool(roi_retry)
        self.last_box = None  # normalized x0,y0,x1,y1 on the un-mirrored frame
        # Host-side profiling only (operator display / bottleneck evidence);
        # never a scientific metric.
        self.infer_count = 0
        self.infer_ms_ema = None

    def _run(self, model, image, mirrored, attempts, kind, remap=None):
        image = np.ascontiguousarray(image[:, ::-1]) if mirrored else np.ascontiguousarray(image)
        t0 = time.perf_counter()
        result = model.process(image)
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        attempts.append({'pass': kind, 'mirrored': mirrored,
                         'hands': 0 if not result.multi_hand_landmarks else len(result.multi_hand_landmarks),
                         'process_ms': ms})
        if not result.multi_hand_landmarks: return None
        labels = [h.classification[0].label for h in result.multi_handedness]
        if self.strict_physical_side and self.side in ('Left','Right'):
            expected = self.side if mirrored else {'Left':'Right','Right':'Left'}[self.side]
            selected = expected if expected in labels else None
        else:
            selected = select_hand_label(labels, self.side)
        if selected is None: return None
        for idx, (landmarks, handedness) in enumerate(zip(result.multi_hand_landmarks, result.multi_handedness)):
            label = handedness.classification[0]
            if label.label != selected: continue
            pts = [[p.x, p.y, p.z] for p in landmarks.landmark]
            if mirrored: pts = [[1 - x, y, z] for x, y, z in pts]
            if remap:
                origin, span, shape = remap
                pts = [[(origin[0] + x * span[0]) / shape[1], (origin[1] + y * span[1]) / shape[0], z]
                       for x, y, z in pts]
            world = [[-p.x, p.y, p.z] for p in result.multi_hand_world_landmarks[idx].landmark]
            # Angle estimates are derived from predicted landmarks, not glove encoders.
            angles = {}
            points = np.asarray(world)
            for name, ids in {'thumb':(1,2,3),'index':(5,6,7),'middle':(9,10,11),'ring':(13,14,15),'little':(17,18,19)}.items():
                a, b, c = points[list(ids)]; u = a - b; v = c - b; den = np.linalg.norm(u) * np.linalg.norm(v)
                angles[name] = None if den < 1e-8 else float(np.degrees(np.arccos(np.clip(u @ v / den, -1, 1))))
            return {'hand': label.label, 'confidence': float(label.score),
                    'confidence_kind': 'handedness_classification_not_joint_accuracy',
                    'landmarks': pts, 'world_landmarks': world, 'joint_angles_deg': angles,
                    'detection_pass': kind, 'gloved_fallback': kind != 'full'}
        return None

    def _remember_box(self, pts):
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        self.last_box = (max(0., min(xs)), max(0., min(ys)), min(1., max(xs)), min(1., max(ys)))

    def _crop(self, rgb):
        if not self.last_box or not self.roi_retry: return None
        h, w = rgb.shape[:2]
        x0, y0, x1, y1 = self.last_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bw, bh = (x1 - x0) * 2.2 / 2, (y1 - y0) * 2.2 / 2
        nx0, ny0, nx1, ny1 = max(0., cx - bw), max(0., cy - bh), min(1., cx + bw), min(1., cy + bh)
        px0, py0, px1, py1 = round(nx0 * w), round(ny0 * h), round(nx1 * w), round(ny1 * h)
        if px1 - px0 >= w and py1 - py0 >= h: return None  # covers the frame; the full pass already ran
        if px1 - px0 < 96 or py1 - py0 < 96: return None
        return (np.ascontiguousarray(rgb[py0:py1, px0:px1]), (px0, py0), (px1 - px0, py1 - py0), rgb.shape)


    def _finish(self, out, hit, mirrored, attempts):
        out.update(detected=True, input_orientation='mirrored-selfie' if mirrored else 'original-rgb')
        out.update({k: hit[k] for k in ('hand', 'confidence', 'confidence_kind', 'landmarks',
                                        'world_landmarks', 'joint_angles_deg', 'detection_pass', 'gloved_fallback')})
        self._remember_box(hit['landmarks'])
        return out

    def infer(self, rgb, device_ns, host_ns, mono_ns):
        t0 = time.perf_counter()
        out = self._infer(rgb, device_ns, host_ns, mono_ns)
        ms = (time.perf_counter() - t0) * 1000.0
        self.infer_count += 1
        self.infer_ms_ema = ms if self.infer_ms_ema is None else .8 * self.infer_ms_ema + .2 * ms
        out['timing_ms'] = {'last_ms': round(ms, 1), 'ema_ms': round(self.infer_ms_ema, 1),
                            'count': self.infer_count, 'model_load_ms': self.model_load_ms,
                            'note': 'capture-host MediaPipe wall time, profiling only, not a scientific metric'}
        return out

    def _infer(self, rgb, device_ns, host_ns, mono_ns):
        out = {'detected':False,'expected_hand':self.side,'device_ns':device_ns,'host_ns':host_ns,
               'host_mono_ns':mono_ns,'source':'mediapipe-0.10.21-vision-estimate','coordinate_system':'hand_relative_model_m',
               'metric_world_pose_valid':False,'connections':CONNECTIONS,'gloved_fallback':False,'attempts':[]}
        attempts = out['attempts']
        for mirrored in (False, True):
            hit = self._run(self.model, rgb, mirrored, attempts, 'full')
            if hit: return self._finish(out, hit, mirrored, attempts)
        crop = self._crop(rgb)
        if crop is not None:
            sub, origin, span, shape = crop
            for mirrored in (False, True):
                hit = self._run(self.model, sub, mirrored, attempts, 'roi-retry', remap=(origin, span, shape))
                if hit: return self._finish(out, hit, mirrored, attempts)
        if self.fallback_min_detection and self.fallback_min_detection > 0:
            if self.fallback_model is None:
                self.fallback_model = self._factory(self.fallback_min_detection)  # built lazily; only frames that failed every earlier pass pay this cost
            for mirrored in (False, True):
                hit = self._run(self.fallback_model, rgb, mirrored, attempts, 'low-threshold')
                if hit: return self._finish(out, hit, mirrored, attempts)
        return out

    def close(self):
        self.model.close()
        if self.fallback_model: self.fallback_model.close()


def jpeg_preview(rgb, pose=None, width=960):
    h,w=rgb.shape[:2];scale=min(1.,width/w);im=cv2.resize(rgb,(round(w*scale),round(h*scale)))
    im=cv2.cvtColor(im,cv2.COLOR_RGB2BGR)
    if pose and pose.get('detected'):
        pts=[(round(p[0]*im.shape[1]),round(p[1]*im.shape[0])) for p in pose['landmarks']]
        for a,b in CONNECTIONS:cv2.line(im,pts[a],pts[b],(91,228,174),2,cv2.LINE_AA)
        for p in pts:cv2.circle(im,p,4,(240,250,255),-1,cv2.LINE_AA)
    return cv2.imencode('.jpg',im,[cv2.IMWRITE_JPEG_QUALITY,82])[1].tobytes()
