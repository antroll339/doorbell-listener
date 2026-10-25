"""Small Windows doorbell widget. Audio never leaves this computer."""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import queue
import sys
if __name__ == "__main__" and sys.platform != "win32":
    raise SystemExit("Doorbell Listener currently supports Windows only.")
import threading
import time
import traceback
import wave
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import sounddevice as sd
import pystray
from PIL import Image, ImageDraw
from detector import RATE, Matcher, RingGate, noise_profile, find_candidates

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DOORBELL_DATA_DIR", str(BASE / "data"))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
BG = "#111923"
CARD = "#1c2937"
FG = "#eef5ff"
MUTED = "#a2b5ca"
GREEN = "#65e3bd"
AMBER = "#ffc879"
SHOW_EVENT_NAME = "Local\\DoorbellListener.Show.v1"
INSTANCE_MUTEX_NAME = "Local\\DoorbellListener.Instance.v1"


def tray_image(color):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, 62, 62), radius=15, fill=BG)
    draw.ellipse((27, 8, 37, 18), fill=color)
    draw.ellipse((15, 14, 49, 46), fill=color)
    draw.rectangle((15, 29, 49, 44), fill=color)
    draw.polygon([(15, 39), (9, 49), (55, 49), (49, 39)], fill=color)
    draw.ellipse((26, 49, 38, 58), fill=color)
    return image


def write_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_wav(path, data):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("Unsupported sample format")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32)/32768


def monitors():
    class INFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                      wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def callback(handle, dc, rect, param):
        info = INFO()
        info.cbSize = ctypes.sizeof(INFO)
        ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info))
        r = info.rcWork
        result.append(dict(left=r.left, top=r.top, right=r.right, bottom=r.bottom,
                           primary=bool(info.dwFlags & 1)))
        return True
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    return sorted(result, key=lambda r: not r["primary"])


def audio_devices():
    hosts = sd.query_hostapis()
    devices = [dict(d, index=i, host=hosts[d["hostapi"]]["name"])
               for i, d in enumerate(sd.query_devices())]
    # Shared WASAPI keeps the selected device independent of the system default.
    shared = [d for d in devices if d["host"] == "Windows WASAPI"]
    return shared or [d for d in devices if d["host"] == "MME"]


class App:
    def __init__(self, resume=False, show_event=None):
        self.config = read_json(DATA / "settings.json")
        self.root = tk.Tk()
        self.root.title("Doorbell Listener")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self.on_unmap)
        self.root.bind("<Control-q>", lambda event: self.shutdown())
        self.root.attributes("-topmost", bool(self.config.get("topmost", True)))
        self.root.report_callback_exception = self.callback_error
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", padding=7, font=("Segoe UI", 10))
        style.configure("TProgressbar", troughcolor=CARD, background=GREEN,
                        bordercolor=CARD, lightcolor=GREEN, darkcolor=GREEN)
        self.devices = audio_devices()
        self.inputs = [d for d in self.devices if d["max_input_channels"] > 0]
        self.outputs = [d for d in self.devices if d["max_output_channels"] > 0]
        self.input_device = next((d for d in self.inputs if d["name"] == self.config.get("input")), None)
        if self.input_device is None:
            self.input_device = next((d for d in self.inputs if "microphone array" in d["name"].lower()), None)
        # Never silently substitute an unrelated mic if the selected one is missing.
        if self.config.get("input") and not any(d["name"] == self.config["input"] for d in self.inputs):
            self.input_device = None
        self.output_name = self.config.get("output", "System default")
        if self.output_name == "\u041a\u0430\u043a \u0432 Windows":
            self.output_name = "System default"
        self.samples = queue.Queue(maxsize=20)
        self.messages = queue.Queue()
        self.stream = None
        self.drops = 0
        self.seen_drops = 0
        self.settings = None
        self.phase = "idle"
        self.phase_started = time.monotonic()
        self.parts = []
        self.recorded = 0
        self.noise = None
        self.matcher = None
        self.candidates = []
        self.recording = None
        self.monitoring = False
        self.alert = False
        self.last_alert = 0.0
        self.last_seen = "No rings detected yet"
        self.ring_gate = RingGate()
        self.level = -90.0
        self.score = 0.0
        self.last_audio = time.monotonic()
        self.last_state_write = 0.0
        self.last_render = 0.0
        self.render_cache = {}
        self.threshold = float(self.config.get("threshold", 0.80))
        self.playing = False
        self.session_dir = None
        self.selected_paths = []
        self.quitting = False
        self.in_tray = False
        self.show_event = show_event
        self.tray_commands = queue.Queue()
        self.tray_ready = threading.Event()
        self.tray = None
        self.tray_state = None
        self.audio_blocks = 0
        self.build_widget()
        self.place_widget()
        self.load_profile()
        self.start_tray()
        self.root.after(100, self.tick)
        if resume and self.matcher:
            self.root.after(300, self.start_monitoring)
        elif not self.matcher:
            self.root.after(300, self.open_settings)

    def start_tray(self):
        self.tray_images = {"active": tray_image(GREEN), "paused": tray_image(MUTED),
                            "alert": tray_image(AMBER)}
        def action(name):
            # Windows runs tray callbacks on its own thread; Tk stays on the main thread.
            return lambda icon, item: self.tray_commands.put(name)
        self.tray = pystray.Icon("doorbell-listener", self.tray_images["paused"],
                                 "Doorbell Listener — paused", menu=pystray.Menu(
            pystray.MenuItem("Open widget", action("show"), default=True),
            pystray.MenuItem("Settings", action("settings")),
            pystray.MenuItem(lambda item: "Pause" if self.monitoring else "Start listening",
                            action("toggle"), enabled=lambda item: self.phase not in ("noise", "walk", "bell")),
            pystray.MenuItem("Acknowledge alert", action("ack"), enabled=lambda item: self.alert),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", action("exit"))))
        def setup(icon):
            try:
                icon.visible = True
                self.tray_ready.set()
            except Exception as e:
                self.messages.put("Could not create the tray icon: " + str(e))
        def worker():
            try:
                self.tray.run(setup=setup)
            except Exception as e:
                self.messages.put("System tray error: " + str(e))
            finally:
                self.tray_ready.clear()
        self.tray_thread = threading.Thread(target=worker, name="doorbell-tray", daemon=True)
        self.tray_thread.start()

    def refresh_tray(self):
        state = (self.monitoring, self.alert, self.phase, self.matcher is not None)
        if not self.tray_ready.is_set() or state == self.tray_state:
            return
        try:
            kind = "alert" if self.alert else "active" if self.monitoring else "paused"
            title = "Someone is at the door!" if self.alert else "Listening for the doorbell" if self.monitoring else "Pause"
            if self.phase in ("noise", "walk", "bell"):
                title = "Calibration"
            self.tray.icon = self.tray_images[kind]
            self.tray.title = "Doorbell Listener — " + title
            self.tray.update_menu()
            self.tray_state = state
        except Exception:
            self.log_exception()

    def on_unmap(self, event):
        if event.widget == self.root and not self.quitting and self.root.state() == "iconic":
            self.root.after_idle(self.hide_if_minimized)

    def hide_if_minimized(self):
        if not self.quitting and self.root.state() == "iconic":
            self.hide_to_tray()

    def hide_to_tray(self):
        if self.quitting:
            return
        if not self.tray_ready.is_set():
            self.show_widget()
            self.notice("The tray icon is not available yet. The window will stay open.")
            return
        self.save_config()
        if self.settings is not None:
            self.settings.withdraw()
        self.in_tray = True
        self.root.withdraw()
        self.refresh_tray()

    def show_widget(self):
        if self.quitting:
            return
        self.in_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.last_render = 0

    def acknowledge(self):
        self.alert = False
        if self.tray_ready.is_set():
            try:
                self.tray.remove_notification()
            except Exception:
                self.log_exception()

    def process_tray_commands(self):
        if self.show_event and ctypes.windll.kernel32.WaitForSingleObject(self.show_event, 0) == 0:
            self.show_widget()
        while not self.tray_commands.empty():
            command = self.tray_commands.get_nowait()
            if command == "show":
                self.show_widget()
            elif command == "settings":
                self.open_settings()
            elif command == "toggle":
                if self.phase not in ("noise", "walk", "bell"):
                    self.cancel() if self.monitoring else self.start_monitoring()
            elif command == "ack":
                self.acknowledge()
            elif command == "exit":
                self.shutdown()
                return

    def label(self, parent, text="", size=10, color=FG, **kw):
        return tk.Label(parent, text=text, font=("Segoe UI", size), bg=parent.cget("bg"),
                        fg=color, **kw)

    def button(self, parent, text, command, accent=False, **kw):
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10),
                         bg=GREEN if accent else CARD, fg=BG if accent else FG,
                         activebackground=AMBER, activeforeground=BG, relief="flat",
                         bd=0, padx=12, pady=8, cursor="hand2", **kw)

    def build_widget(self):
        self.body = tk.Frame(self.root, bg=BG, padx=18, pady=14)
        self.body.pack(fill="both", expand=True)
        self.label(self.body, "DOORBELL LISTENER  ·  OFFLINE", 9, MUTED).pack(anchor="w")
        self.status_label = self.label(self.body, "Calibration needed", 18, AMBER)
        self.status_label.pack(anchor="w", pady=(11, 3))
        self.detail_label = self.label(self.body, "Choose a microphone and record your doorbell", 10, MUTED,
                                       wraplength=330, justify="left")
        self.detail_label.pack(anchor="w")
        self.meter = ttk.Progressbar(self.body, maximum=60, length=326)
        self.meter.pack(fill="x", pady=(14, 6))
        self.last_label = self.label(self.body, self.last_seen, 9, MUTED)
        self.last_label.pack(anchor="w")
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        self.main_btn = self.button(row, "Set up", self.main_action, True)
        self.main_btn.pack(side="left")
        self.button(row, "Settings", self.open_settings).pack(side="right")

    def place_widget(self, next_monitor=False):
        screens = monitors()
        if not screens:
            self.root.geometry("370x250")
            return
        current = int(self.config.get("monitor", 1 if len(screens) > 1 else 0))
        if next_monitor:
            current += 1
        current %= len(screens)
        self.config["monitor"] = current
        r = screens[current]
        x, y = r["right"] - 390, r["top"] + 34
        if not next_monitor and self.config.get("position"):
            px, py = self.config["position"]
            if any(s["left"] <= px <= s["right"]-100 and s["top"] <= py <= s["bottom"]-80 for s in screens):
                x, y = px, py
        # +negative is a signed absolute position in Tk, unlike -N (edge offset).
        self.root.geometry(f"370x250+{x}+{y}")

    def load_profile(self):
        try:
            profile = read_json(DATA / "profile.json")
            if not profile or not self.input_device or profile.get("input") != self.input_device["name"]:
                return
            self.noise = np.load(DATA / profile["noise"], allow_pickle=False)
            self.selected_paths = profile["clips"]
            clips = [load_wav(DATA / p) for p in self.selected_paths]
            self.matcher = Matcher(self.noise, clips)
        except Exception:
            self.matcher = None
            self.noise = None

    def open_stream(self):
        if self.stream is not None:
            return True
        if self.input_device is None:
            self.notice("Choose a microphone in Settings.")
            return False
        def callback(indata, frames, clock, status):
            if status:
                self.drops += 1
            try:
                self.samples.put_nowait(indata[:, 0].copy())
            except queue.Full:
                self.drops += 1
        try:
            extra = sd.WasapiSettings(exclusive=False, auto_convert=True) if self.input_device["host"] == "Windows WASAPI" else None
            self.stream = sd.InputStream(device=self.input_device["index"], samplerate=RATE,
                                         channels=1, dtype="float32", blocksize=1600,
                                         latency="high", extra_settings=extra, callback=callback)
            self.stream.start()
            self.last_audio = time.monotonic()
            return True
        except Exception as e:
            self.stop_stream()
            self.notice("Could not open the microphone: " + str(e))
            return False

    def stop_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        while not self.samples.empty():
            try:
                self.samples.get_nowait()
            except queue.Empty:
                break
        self.level = -90

    def notice(self, text):
        if self.settings is None or not self.settings.winfo_exists():
            self.open_settings()
        self.info_var.set(text)

    def open_settings(self):
        if self.in_tray:
            self.show_widget()
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.deiconify()
            self.settings.lift()
            return
        win = self.settings = tk.Toplevel(self.root)
        win.title("Doorbell Listener settings")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes("-topmost", bool(self.config.get("topmost", True)))
        win.protocol("WM_DELETE_WINDOW", self.hide_settings)
        x = self.root.winfo_x()
        y = self.root.winfo_y() + 280
        screens = monitors()
        r = next((r for r in screens if r["left"] <= x < r["right"]), screens[0] if screens else None)
        if r:
            x = max(r["left"]+20, min(x, r["right"]-700))
            y = max(r["top"]+20, min(y, r["bottom"]-730))
        win.geometry(f"700x750+{x}+{y}")
        viewport = tk.Canvas(win, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=viewport.yview)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        p = tk.Frame(viewport, bg=BG, padx=22, pady=16)
        content_id = viewport.create_window((0, 0), window=p, anchor="nw")
        p.bind("<Configure>", lambda e: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda e: viewport.itemconfigure(content_id, width=e.width))
        def wheel(event):
            if event.widget.winfo_toplevel() == win:
                viewport.yview_scroll(-int(event.delta/120), "units")
        win.bind("<MouseWheel>", wheel)
        self.label(p, "Teach the widget your doorbell", 17).pack(anchor="w")
        self.label(p, "Leave fans and other normal background noise running.", 10, MUTED).pack(anchor="w", pady=(3, 12))
        self.label(p, "Room microphone", 10).pack(anchor="w")
        self.input_var = tk.StringVar(value=self.input_device["name"] if self.input_device else "Choose a microphone")
        self.mic_combo = ttk.Combobox(p, textvariable=self.input_var,
                                     values=[d["name"] for d in self.inputs], state="readonly")
        self.mic_combo.pack(fill="x", pady=(4, 10))
        self.mic_combo.bind("<<ComboboxSelected>>", self.change_input)
        self.label(p, "Play alerts through", 10).pack(anchor="w")
        self.output_var = tk.StringVar(value=self.output_name)
        self.output_combo = ttk.Combobox(p, textvariable=self.output_var,
                                        values=["System default"] + [d["name"] for d in self.outputs], state="readonly")
        self.output_combo.pack(fill="x", pady=(4, 9))
        self.output_combo.bind("<<ComboboxSelected>>", self.change_output)
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x")
        self.button(row, "Test microphone", self.preview_mic).pack(side="left")
        self.button(row, "Test alert", lambda: self.trigger_alert(test=True)).pack(side="left", padx=8)
        self.level_var = tk.StringVar(value="Microphone off")
        self.label(p, size=9, color=MUTED, textvariable=self.level_var).pack(anchor="w", pady=(7, 10))
        plan = ("1. Record 10 seconds of background. Stay quiet; do not ring yet.\n"
                "2. You have 15 seconds to walk to the door.\n"
                "3. Record for 45 seconds. Ring 3–5 times, including short presses.")
        self.label(p, plan, 11, FG, justify="left").pack(anchor="w")
        actions = tk.Frame(p, bg=BG)
        actions.pack(fill="x", pady=10)
        self.calibrate_btn = self.button(actions, "Start calibration", self.start_calibration, True)
        self.calibrate_btn.pack(side="left")
        self.cancel_btn = self.button(actions, "Stop", self.cancel)
        self.cancel_btn.pack(side="left", padx=8)
        self.info_var = tk.StringVar(value=("Calibration saved. Test with a new ring, or record again below."
                                           if self.matcher else "Ready. Click Start calibration when you are ready."))
        self.label(p, size=11, color=AMBER, textvariable=self.info_var,
                   wraplength=630, justify="left", height=3, anchor="w").pack(fill="x")
        self.progress = ttk.Progressbar(p, maximum=100)
        self.progress.pack(fill="x", pady=(0, 10))
        self.label(p, "Review the clips. Hold Ctrl and click every clip that is your doorbell.", 10).pack(anchor="w")
        self.candidate_list = tk.Listbox(p, selectmode="extended", height=5, bg=CARD, fg=FG,
                                        selectbackground="#326a65", relief="flat", exportselection=False,
                                        font=("Segoe UI", 10))
        self.candidate_list.pack(fill="x", pady=5)
        self.candidate_list.bind("<<ListboxSelect>>", self.selection_changed)
        self.candidate_list.bind("<Control-a>", self.select_all_clips)
        self.candidate_list.bind("<Double-Button-1>", lambda event: self.preview_clip())
        self.fill_candidates()
        choices = tk.Frame(p, bg=BG)
        choices.pack(fill="x")
        self.button(choices, "Play clip", self.preview_clip).pack(side="left")
        self.accept_button = self.button(choices, "Use selected clips & start", self.accept_clips, True, state="disabled")
        self.accept_button.pack(side="left", padx=8)
        self.button(choices, "Select all", self.select_all_clips).pack(side="left")
        self.button(choices, "Clear", self.clear_selection).pack(side="left", padx=8)
        self.selection_var = tk.StringVar(value="0 clips selected · Ctrl+click to choose multiple clips")
        self.label(p, size=9, color=MUTED, textvariable=self.selection_var).pack(anchor="w", pady=(5, 0))
        sensitivity = tk.Frame(p, bg=BG)
        sensitivity.pack(fill="x", pady=(10, 2))
        self.label(sensitivity, "Match threshold", 10).pack(side="left")
        self.threshold_var = tk.DoubleVar(value=round(self.threshold * 100))
        tk.Scale(sensitivity, from_=65, to=95, orient="horizontal", variable=self.threshold_var,
                 command=self.change_threshold, bg=BG, fg=FG, troughcolor=CARD, highlightthickness=0,
                 length=210, activebackground=GREEN).pack(side="left", padx=8)
        self.top_var = tk.BooleanVar(value=bool(self.config.get("topmost", True)))
        tk.Checkbutton(sensitivity, text="Always on top", variable=self.top_var, command=self.change_topmost,
                       bg=BG, fg=FG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG).pack(side="left")
        footer = tk.Frame(p, bg=BG)
        footer.pack(fill="x", pady=4)
        self.button(footer, "Next monitor", lambda: self.place_widget(True)).pack(side="left")
        self.button(footer, "Back to widget", self.hide_settings).pack(side="right")
        self.label(p, "Calibration audio stays on this PC. Listening does not save audio.",
                   9, MUTED, wraplength=630, justify="left").pack(anchor="w", pady=(7, 0))
        self.selection_changed()

    def hide_settings(self):
        if self.phase in ("noise", "walk", "bell"):
            return
        if not self.monitoring:
            self.stop_stream()
        if self.settings is not None:
            self.settings.destroy()
            self.settings = None

    def preview_mic(self):
        if self.phase in ("noise", "walk", "bell"):
            return
        if self.open_stream():
            self.notice("Microphone on: the level meter should move. No recording is being saved.")

    def change_input(self, event=None):
        self.cancel()
        self.input_device = next(d for d in self.inputs if d["name"] == self.input_var.get())
        self.matcher = None
        self.noise = None
        self.candidates = []
        self.recording = None
        self.fill_candidates()
        self.load_profile()
        self.save_config()
        self.notice("Microphone selected. Check the level, then start calibration.")

    def change_output(self, event=None):
        self.output_name = self.output_var.get()
        self.save_config()

    def change_threshold(self, value):
        self.threshold = float(value) / 100

    def change_topmost(self):
        self.config["topmost"] = self.top_var.get()
        self.root.attributes("-topmost", self.top_var.get())
        self.settings.attributes("-topmost", self.top_var.get())
        self.save_config()

    def set_phase(self, phase):
        self.phase = phase
        self.phase_started = time.monotonic()
        self.parts = []
        self.recorded = 0

    def start_calibration(self):
        if self.phase in ("noise", "walk", "bell") or self.playing:
            return
        self.monitoring = False
        self.alert = False
        self.matcher = None
        self.noise = None
        self.stop_stream()
        if not self.open_stream():
            self.load_profile()
            return
        self.session_dir = DATA / (time.strftime("calibration-%Y%m%d-%H%M%S") + f"-{time.time_ns()%1000000:06d}")
        self.session_dir.mkdir()
        self.candidates = []
        self.recording = None
        self.fill_candidates()
        self.mic_combo.configure(state="disabled")
        self.calibrate_btn.configure(state="disabled")
        self.set_phase("noise")
        self.seen_drops = self.drops

    def finish_noise(self):
        audio = np.concatenate(self.parts)[:10*RATE]
        self.noise = noise_profile(audio)
        save_wav(self.session_dir / "background.wav", audio)
        np.save(self.session_dir / "noise.npy", self.noise, allow_pickle=False)
        self.set_phase("walk")

    def finish_bell(self):
        self.recording = np.concatenate(self.parts)[:45*RATE]
        save_wav(self.session_dir / "calibration.wav", self.recording)
        self.candidates = find_candidates(self.recording, self.noise)
        for c in self.candidates:
            c["clip"] = self.recording[c["start"]:c["end"]].copy()
        write_json(self.session_dir / "candidates.json", [
            {k: v for k, v in c.items() if k != "clip"} for c in self.candidates])
        self.set_phase("review")
        self.stop_stream()
        self.mic_combo.configure(state="readonly")
        self.calibrate_btn.configure(state="normal")
        self.progress["value"] = 100
        self.fill_candidates()
        if self.candidates:
            self.notice("Recording ready. Listen to the clips, Ctrl-click all doorbell clips, then click Use selected clips & start.")
        else:
            self.notice("No distinct sounds found. The recording was saved. Check whether the microphone can hear the doorbell.")

    def fill_candidates(self):
        if not hasattr(self, "candidate_list") or not self.candidate_list.winfo_exists():
            return
        self.candidate_list.delete(0, "end")
        for n, c in enumerate(self.candidates, 1):
            self.candidate_list.insert("end", f"{n}.  At {c['start']/RATE:.1f}s   ·   {(c['end']-c['start'])/RATE:.1f}s long")
        self.selection_changed()

    def selection_changed(self, event=None):
        if not hasattr(self, "accept_button") or not self.accept_button.winfo_exists():
            return
        count = len(self.candidate_list.curselection())
        self.accept_button.configure(state="normal" if count else "disabled")
        self.selection_var.set(f"{count} clip{'s' if count != 1 else ''} selected · Ctrl+click to choose multiple clips")

    def select_all_clips(self, event=None):
        self.candidate_list.selection_set(0, "end")
        self.selection_changed()
        return "break"

    def clear_selection(self):
        self.candidate_list.selection_clear(0, "end")
        self.selection_changed()

    def preview_clip(self):
        if self.phase in ("noise", "walk", "bell") or self.playing:
            return
        selected = self.candidate_list.curselection()
        if not selected:
            self.notice("Select a clip from the list first.")
            return
        focused = self.candidate_list.index("active")
        index = focused if focused in selected else selected[0]
        self.ring_gate.mark_alert(time.monotonic())
        self.play_audio(self.candidates[index]["clip"])

    def accept_clips(self):
        selected = list(self.candidate_list.curselection())
        if not selected:
            self.notice("Select your doorbell clips. Hold Ctrl to select more than one.")
            return
        clips = [self.candidates[i]["clip"] for i in selected]
        try:
            matcher = Matcher(self.noise, clips)
            self.selected_paths = []
            for i, clip in enumerate(clips):
                path = self.session_dir / f"bell-{i+1}.wav"
                save_wav(path, clip)
                self.selected_paths.append(str(path.relative_to(DATA)))
            write_json(DATA / "profile.json", dict(input=self.input_device["name"],
                       noise=str((self.session_dir / "noise.npy").relative_to(DATA)), clips=self.selected_paths))
            self.matcher = matcher
            self.set_phase("idle")
            self.start_monitoring()
            if self.monitoring:
                self.notice(f"Listening with {len(clips)} selected clips. Test with a new doorbell press now.")
        except Exception as e:
            self.notice(str(e))

    def cancel(self):
        was_calibrating = self.phase in ("noise", "walk", "bell", "review")
        self.monitoring = False
        self.alert = False
        self.set_phase("idle")
        self.stop_stream()
        if was_calibrating:
            self.candidates = []
            self.recording = None
            self.session_dir = None
            self.matcher = None
            self.noise = None
            self.fill_candidates()
            self.load_profile()
        if self.settings is not None and self.settings.winfo_exists():
            self.mic_combo.configure(state="readonly")
            self.calibrate_btn.configure(state="normal")
            self.info_var.set("Stopped. Microphone off.")

    def start_monitoring(self):
        if self.matcher is None:
            self.open_settings()
            return
        # Clear old context so resume cannot match a stale sound.
        self.matcher = Matcher(self.noise, [load_wav(DATA / p) for p in self.selected_paths])
        self.monitoring = self.open_stream()
        self.ring_gate.reset(time.monotonic())
        self.save_config()

    def main_action(self):
        if self.alert:
            self.acknowledge()
        elif self.monitoring:
            self.cancel()
        elif self.matcher:
            self.start_monitoring()
        else:
            self.open_settings()

    def play_audio(self, audio):
        if self.playing:
            return
        self.playing = True
        output = next((d for d in self.outputs if d["name"] == self.output_name), None)
        if self.output_name != "System default" and output is None:
            self.playing = False
            self.notice("The selected output is unavailable. Choose an output in Settings.")
            return
        def worker():
            try:
                kwargs = {}
                if output:
                    kwargs["device"] = output["index"]
                    if output["host"] == "Windows WASAPI":
                        kwargs["extra_settings"] = sd.WasapiSettings(exclusive=False, auto_convert=True)
                with sd.OutputStream(samplerate=RATE, channels=1, dtype="float32", **kwargs) as out:
                    out.write(np.ascontiguousarray(audio.reshape(-1, 1), dtype=np.float32))
            except Exception as e:
                self.messages.put("Could not play audio: " + str(e))
            finally:
                self.playing = False
        threading.Thread(target=worker, daemon=True).start()

    def trigger_alert(self, test=False):
        if self.phase in ("noise", "walk", "bell"):
            self.notice("Wait for recording to finish before testing the alert.")
            return
        self.alert = True
        self.last_alert = time.monotonic()
        self.ring_gate.mark_alert(self.last_alert)
        self.last_seen = ("Test alert" if test else "Ring") + " · " + time.strftime("%H:%M:%S")
        self.refresh_tray()
        if self.in_tray and self.tray_ready.is_set():
            try:
                self.tray.notify("Someone is ringing the doorbell. Click the bell icon to open the widget.",
                                 "Test alert" if test else "Doorbell Listener")
            except Exception:
                self.log_exception()
        t = np.arange(int(RATE*0.32), dtype=np.float32) / RATE
        tones = []
        for frequency in (740, 990, 740, 990):
            tone = 0.11 * np.sin(2*np.pi*frequency*t) * np.sin(np.linspace(0, np.pi, len(t)))**0.5
            tones.extend((tone.astype(np.float32), np.zeros(int(RATE*0.10), np.float32)))
        self.play_audio(np.concatenate(tones))
        if not test:
            with (DATA / "events.txt").open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} score={self.score:.3f}\n")

    def tick(self):
        now = time.monotonic()
        try:
            self.process_tray_commands()
            if self.quitting:
                return
            while not self.messages.empty():
                self.notice(self.messages.get_nowait())
            while not self.samples.empty():
                block = self.samples.get_nowait()
                self.audio_blocks += 1
                self.last_audio = now
                self.level = 20 * np.log10(float(np.sqrt(np.mean(block*block))) + 1e-10)
                if self.phase in ("noise", "bell"):
                    self.parts.append(block)
                    self.recorded += len(block)
                    target = (10 if self.phase == "noise" else 45) * RATE
                    if self.recorded >= target:
                        if self.phase == "noise":
                            self.finish_noise()
                        else:
                            self.finish_bell()
                elif self.monitoring and self.matcher:
                    self.score = self.matcher.feed(block)
                    if self.ring_gate.feed(self.score, self.threshold, now, self.playing):
                        self.trigger_alert()
            if self.drops != self.seen_drops:
                self.seen_drops = self.drops
                if self.phase in ("noise", "walk", "bell"):
                    self.cancel()
                    self.notice("Audio was dropped during recording. Please restart calibration.")
                elif self.monitoring and self.matcher:
                    self.matcher = Matcher(self.noise, [load_wav(DATA / p) for p in self.selected_paths])
                    self.notice("Audio was dropped. The detector reset its buffer and is listening again.")
            if self.stream is not None and (not self.stream.active or now-self.last_audio > 4):
                self.cancel()
                self.notice("The microphone stream stopped. Check the device and start listening again.")
            if self.phase == "walk" and now-self.phase_started >= 15:
                self.set_phase("bell")
            self.refresh_tray()
            if self.in_tray and not self.tray_ready.is_set():
                self.show_widget()
            if not self.in_tray and now - self.last_render >= 0.24:
                self.last_render = now
                self.render(now)
            if now-self.last_state_write > 2:
                self.last_state_write = now
                write_json(DATA / "status.json", dict(pid=os.getpid(), phase=self.phase,
                           monitoring=self.monitoring, microphone=self.input_device["name"] if self.input_device else None,
                           stream_open=self.stream is not None, level_db=round(float(self.level), 1),
                           match=round(self.score, 3), candidates=len(self.candidates),
                           calibrated=self.matcher is not None, drops=self.drops,
                           in_tray=self.in_tray, tray_ready=self.tray_ready.is_set(),
                           audio_blocks=self.audio_blocks, last_audio_age=round(now-self.last_audio, 2),
                           detector_version=2, alert=self.alert, rearmed=self.ring_gate.armed,
                           position=[self.root.winfo_x(), self.root.winfo_y()], monitors=monitors()))
        except Exception as e:
            self.cancel()
            self.notice("Error: " + str(e))
            self.log_exception()
        finally:
            if not self.quitting:
                self.root.after(100, self.tick)

    def update_control(self, widget, **values):
        changed = {}
        for key, value in values.items():
            cache_key = (str(widget), key)
            if self.render_cache.get(cache_key) != value:
                changed[key] = value
                self.render_cache[cache_key] = value
        if changed:
            widget.configure(**changed)

    @staticmethod
    def update_variable(variable, value):
        if variable.get() != value:
            variable.set(value)

    def render(self, now):
        self.update_control(self.meter, value=int(max(0, min(60, self.level+75))))
        self.update_control(self.last_label, text=self.last_seen)
        if self.alert:
            self.update_control(self.status_label, text="DOORBELL!", fg=AMBER if int(now*2)%2 else FG)
            self.update_control(self.detail_label, text="Click Acknowledge to clear this alert")
            self.update_control(self.main_btn, text="Acknowledge")
        elif self.phase in ("noise", "walk", "bell"):
            seconds = self.recorded/RATE if self.phase != "walk" else now-self.phase_started
            total = {"noise": 10, "walk": 15, "bell": 45}[self.phase]
            left = max(0, int(np.ceil(total-seconds)))
            title = {"noise": "Recording background", "walk": "Walk to the door", "bell": "Ring the doorbell"}[self.phase]
            text = {"noise": "Stay quiet and do not ring yet. Leave the fan running.",
                    "walk": "Walk to the door. Start ringing after the countdown.",
                    "bell": "Ring 3–5 times with 5–8 second pauses. Include short presses."}[self.phase]
            self.update_control(self.status_label, text=f"{title} · {left}s", fg=AMBER)
            self.update_control(self.detail_label, text=text)
            self.update_control(self.main_btn, text="Calibrating…")
            if self.settings:
                self.update_variable(self.info_var, f"{title.upper()} — {left}s remaining.\n{text}")
                self.progress["value"] = 100 * min(seconds/total, 1)
        elif self.monitoring:
            self.update_control(self.status_label, text="Listening for the doorbell", fg=GREEN)
            self.update_control(self.detail_label, text=f"Match {self.score:.0%} · threshold {self.threshold:.0%}")
            self.update_control(self.main_btn, text="Pause")
        else:
            self.update_control(self.status_label, text="Paused" if self.matcher else "Calibration needed", fg=MUTED if self.matcher else AMBER)
            self.update_control(self.detail_label, text="Microphone off" if self.stream is None else "Microphone on · checking the level")
            self.update_control(self.main_btn, text="Start" if self.matcher else "Set up")
        if self.settings is not None:
            self.update_variable(self.level_var, (f"Level: {self.level:.0f} dBFS" if self.stream else "Microphone off") +
                                 (" · very quiet / check the mute switch" if self.stream and self.level < -85 else ""))

    def save_config(self):
        self.config.update(input=self.input_device["name"] if self.input_device else None,
                           output=self.output_name, threshold=self.threshold,
                           position=[self.root.winfo_x(), self.root.winfo_y()])
        write_json(DATA / "settings.json", self.config)

    def callback_error(self, exc, value, tb):
        with (DATA / "errors.log").open("a", encoding="utf-8") as f:
            traceback.print_exception(exc, value, tb, file=f)
        self.cancel()
        self.notice("Error: " + str(value))

    def log_exception(self):
        with (DATA / "errors.log").open("a", encoding="utf-8") as f:
            traceback.print_exc(file=f)

    def shutdown(self):
        if self.quitting:
            return
        self.quitting = True
        self.save_config()
        self.stop_stream()
        if self.tray is not None:
            self.tray.stop()
        write_json(DATA / "status.json", dict(pid=os.getpid(), phase="closed", monitoring=False, stream_open=False))
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    ctypes.windll.kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.windll.kernel32.CreateEventW.restype = wintypes.HANDLE
    ctypes.windll.kernel32.OpenEventW.restype = wintypes.HANDLE
    ctypes.windll.kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.windll.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:
        existing_show_event = ctypes.windll.kernel32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)
        if existing_show_event:
            ctypes.windll.kernel32.SetEvent(existing_show_event)
            ctypes.windll.kernel32.CloseHandle(existing_show_event)
        sys.exit(0)
    show_event = ctypes.windll.kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    try:
        App(resume="--resume" in sys.argv, show_event=show_event).run()
    except Exception:
        with (DATA / "errors.log").open("a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
    finally:
        if show_event:
            ctypes.windll.kernel32.CloseHandle(show_event)
        ctypes.windll.kernel32.CloseHandle(instance_mutex)
