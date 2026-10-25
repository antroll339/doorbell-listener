import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import unittest
import numpy as np
from detector import RATE, Matcher, RingGate, noise_profile, find_candidates
from signals import background, bell, sample, peak_score


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.noise = noise_profile(background(10))
        self.clip = sample()

    def score(self, audio, blocksize=1600):
        return peak_score(Matcher(self.noise, [self.clip]), audio, blocksize)

    def test_quieter_bell_over_louder_fan(self):
        audio = background(6, volume=2, seed=72)
        audio[41317:41317+len(bell())] += bell()*0.65
        self.assertGreaterEqual(self.score(audio), 0.80)

    def test_short_chirp_at_different_block_offsets(self):
        for offset in (0, 317, 1599):
            with self.subTest(offset=offset):
                audio = background(2, seed=82)
                start = RATE + offset
                audio[start:start+3200] += bell()[:3200]
                self.assertGreaterEqual(self.score(audio), 0.80)

    def test_non_bell_sounds(self):
        rng = np.random.default_rng(5)
        t = np.arange(RATE*4) / RATE
        clap = background(4)
        clap[RATE:RATE+800] += rng.normal(0, 0.2, 800)
        cases = {
            "fan": background(4, volume=1.8, seed=42),
            "broadband": rng.normal(0, 0.015, RATE*4).astype(np.float32),
            "other tone": (0.025*np.sin(2*np.pi*1330*t)).astype(np.float32) + background(4),
            "clap": clap,
        }
        for name, audio in cases.items():
            with self.subTest(sound=name):
                self.assertLess(self.score(audio), 0.80)

    def test_candidate_order_and_background_rejection(self):
        audio = background(12)
        for second, gain in ((1, 1), (5, 0.6), (9, 1.2)):
            audio[second*RATE:second*RATE+len(bell())] += bell()*gain
        clips = find_candidates(audio, self.noise)
        self.assertEqual(len(clips), 3)
        self.assertEqual([c["start"] for c in clips], sorted(c["start"] for c in clips))
        self.assertFalse(find_candidates(background(4, seed=10), self.noise))
        self.assertFalse(find_candidates(np.zeros(RATE, np.float32), self.noise))

    def test_silent_microphone_is_rejected(self):
        with self.assertRaises(ValueError):
            noise_profile(np.zeros(RATE, np.float32))

    def test_stream_chunking(self):
        self.assertGreaterEqual(self.score(sample(), blocksize=173), 0.80)


class GateTests(unittest.TestCase):
    def simulate(self, score_at, playing_at=lambda t: False):
        gate = RingGate()
        return [i/10 for i in range(150)
                if gate.feed(score_at(i/10), .80, i/10, playing_at(i/10))]

    def test_continuous_ring_not_repeated(self):
        self.assertEqual(self.simulate(lambda t: .9 if 1 <= t < 13 else .1), [1.0])

    def test_separate_short_presses(self):
        self.assertEqual(self.simulate(lambda t: .9 if (1 <= t < 1.2 or 4 <= t < 4.2) else .1), [1.0, 4.0])

    def test_brief_dips_do_not_rearm(self):
        self.assertEqual(self.simulate(lambda t: .1 if (t < 1 or int(t*10) % 4 == 0) else .9), [1.0])

    def test_playback_suppression(self):
        self.assertEqual(self.simulate(lambda t: .9 if (1 <= t < 1.2 or 2 <= t < 2.2 or 4 <= t < 4.2) else .1,
                                       lambda t: 1.1 <= t < 2.8), [1.0, 4.0])

    def test_rearm_on_first_frame_after_quiet(self):
        self.assertEqual(self.simulate(lambda t: .9 if (1 <= t < 1.2 or 3.5 <= t < 3.7) else .1), [1.0, 3.5])


if __name__ == "__main__":
    unittest.main()
