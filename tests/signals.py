"""Generated test signals; no recordings from anyone's home."""
import numpy as np
from detector import RATE


def background(seconds, volume=1, seed=721):
    t = np.arange(round(seconds * RATE)) / RATE
    rng = np.random.default_rng(seed)
    return (volume * (0.003 * np.sin(2*np.pi*120*t) + 0.001 * np.sin(2*np.pi*240*t))
            + rng.normal(0, 0.001, len(t))).astype(np.float32)


def bell():
    chunks = []
    for freq in (740, 990):
        t = np.arange(int(0.8*RATE)) / RATE
        envelope = np.minimum(t*100, 1) * np.exp(-2.1*t)
        chunks.append((0.04*envelope*(np.sin(2*np.pi*freq*t)
                       + 0.45*np.sin(2*np.pi*freq*2.03*t))).astype(np.float32))
    return np.concatenate(chunks)


def sample():
    audio = background(2.4)
    audio[3200:3200+len(bell())] += bell()
    return audio


def peak_score(matcher, audio, blocksize=1600):
    return max(matcher.feed(audio[i:i+blocksize]) for i in range(0, len(audio), blocksize))
