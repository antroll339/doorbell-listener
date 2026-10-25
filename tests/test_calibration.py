"""Exercise the real Tk calibration flow with generated audio and no hardware I/O."""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from detector import RATE, noise_profile
from signals import background, bell, peak_score


@unittest.skipUnless(sys.platform == "win32", "Windows Tk application")
class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name)
        with patch.dict(os.environ, {"DOORBELL_DATA_DIR": str(data)}):
            import widget
        self.widget = widget
        self.addCleanup(patch.stopall)
        patch.object(widget, "DATA", data).start()
        devices = [dict(name="Microphone Array (test)", index=0, host="Windows WASAPI",
                        max_input_channels=1, max_output_channels=0)]
        patch.object(widget, "audio_devices", return_value=devices).start()
        patch.object(widget.App, "start_tray").start()
        patch.object(widget.App, "open_stream", return_value=True).start()
        patch.object(widget.App, "play_audio").start()
        self.app = widget.App()
        self.addCleanup(self.close_app)
        self.app.root.withdraw()
        self.app.open_settings()
        self.app.settings.withdraw()

    def close_app(self):
        # Cancel scheduled callbacks so the next Tk instance starts cleanly.
        for job in self.app.root.tk.call("after", "info"):
            self.app.root.after_cancel(job)
        self.app.shutdown()

    def record(self):
        a = self.app
        a.start_calibration()
        self.assertEqual(a.phase, "noise")
        a.parts = [background(10)]
        a.finish_noise()
        self.assertEqual(a.phase, "walk")
        audio = background(23, seed=66)
        # Five distinct bell presses, plus a different sound to reject.
        for second in (1, 5, 9, 13, 17):
            audio[second*RATE:second*RATE+len(bell())] += bell()
        t = np.arange(RATE) / RATE
        audio[21*RATE:22*RATE] += (0.03*np.sin(2*np.pi*1800*t)).astype(np.float32)
        a.set_phase("bell")
        a.parts = [audio]
        a.finish_bell()
        self.assertEqual(a.phase, "review")
        self.assertEqual(len(a.candidates), 6)

    def test_review_select_save_reload_and_detect(self):
        self.record()
        a = self.app
        self.assertEqual(str(a.accept_button["state"]), "disabled")
        a.select_all_clips()
        self.assertEqual(len(a.candidate_list.curselection()), 6)
        a.clear_selection()
        self.assertEqual(len(a.candidate_list.curselection()), 0)
        a.candidate_list.selection_set(0, 4)
        a.candidate_list.activate(3)
        a.selection_changed()
        a.preview_clip()
        self.assertIs(a.play_audio.call_args.args[0], a.candidates[3]["clip"])
        a.accept_button.invoke()
        self.assertTrue(a.monitoring)
        profile = self.widget.read_json(self.widget.DATA / "profile.json")
        self.assertEqual(len(profile["clips"]), 5, "Every selected clip must be kept")
        a.matcher = None
        a.noise = None
        a.load_profile()
        self.assertIsNotNone(a.matcher)
        fresh = background(3, seed=23)
        fresh[RATE:RATE+len(bell())] += bell()*0.7
        self.assertGreaterEqual(peak_score(a.matcher, fresh), .80)
        self.assertNotIn("bell-6.wav", " ".join(profile["clips"]))

    def test_cancel_restores_saved_profile_and_discards_pending_review(self):
        self.record()
        self.app.candidate_list.selection_set(0)
        self.app.accept_clips()
        saved = self.widget.read_json(self.widget.DATA / "profile.json")
        self.record()
        self.app.cancel()
        self.assertFalse(self.app.monitoring)
        self.assertIsNotNone(self.app.matcher)
        self.assertFalse(self.app.candidates)
        self.assertIsNone(self.app.session_dir)
        self.assertEqual(self.widget.read_json(self.widget.DATA / "profile.json"), saved)

    def test_saved_microphone_is_not_silently_replaced(self):
        self.widget.write_json(self.widget.DATA / "profile.json", {"input": "Missing mic"})
        self.app.load_profile()
        self.assertIsNone(self.app.matcher)


if __name__ == "__main__":
    unittest.main()
