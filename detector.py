"""Offline spectral template matching. No network or speech recognition."""
import numpy as np

RATE = 16000
NFFT = 1024
HOP = 320
FPS = RATE / HOP
WINDOW = np.hanning(NFFT).astype(np.float32)
EDGES = np.unique(np.round(np.geomspace(10, 450, 65)).astype(int))
BANDS = len(EDGES) - 1
BANK = np.zeros((NFFT // 2 + 1, BANDS), np.float32)
for k, (a, b) in enumerate(zip(EDGES[:-1], EDGES[1:])):
    BANK[a:b, k] = 1.0 / (b - a)


def powers(audio):
    audio = np.asarray(audio, np.float32).reshape(-1)
    if len(audio) < NFFT:
        return np.empty((0, BANDS), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, NFFT)[::HOP]
    spectrum = np.fft.rfft(frames * WINDOW, axis=1)
    return ((spectrum.real**2 + spectrum.imag**2) @ BANK).astype(np.float32)


def noise_profile(audio):
    p = powers(audio)
    if not len(p) or float(np.max(np.abs(audio))) < 1e-6:
        raise ValueError("No audio is reaching the microphone. Check the selected input device.")
    return np.maximum(np.percentile(p, 75, axis=0), 1e-10).astype(np.float32)


def features(p, noise):
    floor = np.maximum(noise, float(np.max(noise)) * 0.0001 + 1e-10)
    excess = np.maximum(p / floor - 1.4, 0)
    return np.log1p(excess).astype(np.float32)


def activity(f):
    # Require multiple frequency bins, but allow a narrow tonal bell.
    return np.mean(np.sort(f, axis=1)[:, -6:], axis=1)


def find_candidates(audio, noise):
    f = features(powers(audio), noise)
    e = activity(f)
    if not len(e):
        return []
    # Noise-only recordings should produce no candidates.
    cut = max(1.6, float(np.percentile(e, 30)) + 0.8)
    mask = e > cut
    active = np.flatnonzero(mask)
    if not len(active):
        return []
    groups = np.split(active, np.flatnonzero(np.diff(active) > int(0.65 * FPS)) + 1)
    result = []
    for g in groups:
        if len(g) < 5:
            continue
        a = max(0, int(g[0]) - 4)
        b = min(len(f), int(g[-1]) + 7)
        # Long events can be speech or music: keep reviewable clips, never auto-accept.
        b = min(b, a + int(6 * FPS))
        if b - a < 15:
            continue
        start, end = a * HOP, min(len(audio), b * HOP + NFFT)
        result.append({"start": start, "end": end, "strength": float(np.max(e[a:b]))})
    return result[:24]


def prepare_templates(clips, noise):
    templates = []
    def add_template(warped, short=False):
        norms = np.linalg.norm(warped, axis=1)
        good = norms > max(float(np.max(norms)) * 0.22, 1.0)
        if np.count_nonzero(good) < (4 if short else 6):
            return
        normalized = warped / np.maximum(norms[:, None], 1e-8)
        templates.append({"f": normalized, "mask": good,
                          "energy": float(np.median(activity(warped)[good])),
                          "short": short, "penalty": 0.06 if short else 0.0})
    for clip in clips:
        f = features(powers(clip), noise)
        e = activity(f)
        active = np.flatnonzero(e > max(1.2, float(np.max(e)) * 0.3))
        if len(active) < 5:
            continue
        # A wireless bell can make just a single short chirp. Learn its onset
        # without requiring the rest of the recorded melody to arrive.
        for offset in (0, 4, 8):
            start = int(active[0]) + offset
            for length in (5, 8, 12):
                short = f[start:start+length]
                if len(short) == length and np.count_nonzero(activity(short) > 1.2) >= length*0.8:
                    add_template(short, short=True)
        # Recognise after at most 2.4 s, even if the full melody is long.
        a = max(0, int(active[0]) - 1)
        b = min(int(active[-1]) + 3, a + 120, len(f))
        base = f[a:b]
        if len(base) < 12:
            continue
        for speed in (0.92, 1.0, 1.08):
            idx = np.linspace(0, len(base)-1, max(12, round(len(base)*speed)))
            warped = np.stack([np.interp(idx, np.arange(len(base)), base[:, j])
                               for j in range(BANDS)], axis=1).astype(np.float32)
            add_template(warped)
    return templates


def score_window(history, templates):
    best = 0.0
    for t in templates:
        n = len(t["f"])
        if len(history) < n:
            continue
        f = history[-n:]
        norms = np.linalg.norm(f, axis=1)
        normal = f / np.maximum(norms[:, None], 1e-8)
        mask = t["mask"]
        energy = float(np.median(activity(f)[mask]))
        if energy < max(1.0, t["energy"] * 0.30):
            continue
        cosine = np.sum(normal * t["f"], axis=1)[mask]
        if t.get("short") and np.count_nonzero(activity(f) > 1.0) < n*0.8:
            continue
        # The lower quantile penalises a partly matching conversation or transient.
        score = 0.70 * float(np.mean(cosine)) + 0.30 * float(np.percentile(cosine, 25)) - t.get("penalty", 0.0)
        best = max(best, score)
    return best


class Matcher:
    def __init__(self, noise, clips):
        self.noise = noise
        self.templates = prepare_templates(clips, noise)
        if not self.templates:
            raise ValueError("The selected clip has too little distinct sound. Record the doorbell again.")
        self.max_frames = max(len(t["f"]) for t in self.templates)
        self.tail = np.empty(0, np.float32)
        self.history = np.empty((0, BANDS), np.float32)

    def feed(self, audio):
        self.tail = np.concatenate((self.tail, audio))
        p = powers(self.tail)
        if not len(p):
            return 0.0
        self.tail = self.tail[len(p)*HOP:]
        f = features(p, self.noise)
        best = 0.0
        # Check every 20 ms; processing happens only once per 100-ms audio block.
        for row in f:
            self.history = np.concatenate((self.history, row[None]))[-self.max_frames:]
            best = max(best, score_window(self.history, self.templates))
        return best


class RingGate:
    """One alert per sound event; re-arm after quiet instead of a 12-second lockout."""
    def __init__(self, minimum_gap=2.5, quiet_time=0.5):
        self.minimum_gap = minimum_gap
        self.quiet_time = quiet_time
        self.reset()

    def reset(self, now=0.0):
        self.armed = True
        self.not_before = now + 0.5
        self.quiet_since = None

    def mark_alert(self, now):
        self.armed = False
        self.not_before = now + self.minimum_gap
        self.quiet_since = None

    def feed(self, score, threshold, now, playing=False):
        if playing:
            self.quiet_since = None
            return False
        if (not self.armed and self.quiet_since is not None and
                now-self.quiet_since >= self.quiet_time and now >= self.not_before):
            self.armed = True
        if score < threshold - 0.10:
            if self.quiet_since is None:
                self.quiet_since = now
        else:
            self.quiet_since = None
        if self.armed and now >= self.not_before and score >= threshold:
            self.mark_alert(now)
            return True
        return False
