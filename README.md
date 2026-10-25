# Doorbell Listener

A small Windows widget for people who miss the doorbell while wearing noise-cancelling headphones. Teach it the sound of your own bell, leave it on a second monitor or in the system tray, and get a visible alert plus a chime in your headphones.

**Local processing. No accounts, cloud service, API keys, or model downloads.**

## What it does

- Uses a microphone you choose, such as the laptop's built-in microphone, independently of your Discord or game microphone.
- Learns your room's background noise and the doorbell clips you select.
- Matches both melodies and short chirps using NumPy audio features.
- Shows an always-on-top widget with a persistent alert until acknowledged.
- Keeps listening when minimized or closed with the window's **X**; both actions move it to the tray.
- Lets you choose the alert output, move the widget between monitors, and adjust the match threshold.

This is an early Windows utility. Detection depends on the microphone, room and bell; calibrate and test it in your own setup.

## Install and run

Requires **Windows 10/11, 64-bit Python 3.12 or newer**, and a microphone that can hear the doorbell. Python 3.12 is the tested version. Include Tcl/Tk and the Python launcher when installing [Python for Windows](https://www.python.org/downloads/windows/).

1. Download this repository using **Code → Download ZIP**, then extract it to a writable folder. Or clone it with Git.
2. Double-click **setup.bat**. It creates a local `.venv` and installs the pinned dependencies. This step needs internet access.
3. Double-click **run.vbs** to open the widget without a console. **run.bat** is an alternative.

After installation, the application runs offline. It starts paused if a saved calibration exists; click **Start** to listen. For a shortcut that starts listening immediately, use `.venv\Scripts\pythonw.exe widget.py --resume` with the project folder as its working directory.

Manual setup in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe widget.py
```

## Teach it your doorbell

1. In **Settings**, choose the room microphone and your headphone output. **Test microphone** shows the incoming level; **Test alert** checks the output.
2. Leave fans and other normal background noise running. Click **Start calibration** and stay quiet for the first **10 seconds**.
3. A **15-second countdown** gives you time to walk to the door.
4. During the next **45 seconds**, ring 3–5 times, with 5–8 seconds between presses. Include short presses if your bell sometimes only chirps.
5. Review the detected clips. Select one and click **Play clip**, or double-click it. With several clips selected, Play uses the focused clip.
6. Hold **Ctrl** and click every clip containing your doorbell. **Select all** / **Ctrl+A** and **Clear** help with larger selections. Leave speech, knocks and other unrelated sounds unselected.
7. Click **Use selected clips & start**. All selected clips are saved and used; monitoring begins immediately.
8. Test with new doorbell presses. Include quiet and short rings, leaving a few seconds between them.

Calibration is saved between runs. **Stop** during a new calibration discards the pending selection and restores the previous saved profile. Changing microphones requires a matching calibration.

## Everyday use

Drag the widget to your preferred position or use **Next monitor**. The position is remembered.

Closing or minimizing the widget keeps it running in the tray. Click the bell icon, or launch the app again, to restore it. Right-click the tray icon for **Start listening / Pause**, **Settings**, **Acknowledge alert**, and **Quit**. **Ctrl+Q** in the widget also exits completely. Closing the Settings window only closes Settings.

The tray icon is green while listening, grey while paused, and amber after a detection. An alert remains visible until acknowledged. While hidden, the app also requests a Windows tray notification; Windows notification settings may suppress that banner. The headphone chime and tray alert still work.

## Tuning and troubleshooting

| Symptom | What to try |
| --- | --- |
| No input level | Check the selected microphone, its mute switch, and Windows microphone permissions for desktop apps. |
| Faint or missing short rings | Record several short presses; move the microphone closer. Check whether the microphone driver applies voice isolation or noise suppression that removes the bell. |
| Missed rings with a visible match score | Lower **Match threshold** a little, then test new rings and ordinary room sounds. |
| False alerts | Raise the threshold or recalibrate, selecting only clear doorbell clips. Keep normal fan noise present during the background step. |
| No sound in headphones | Choose the correct output and use **Test alert**. Check Windows volume/mixer settings. |
| Microphone disconnected or audio dropped | Check the device and restart listening. A dropped calibration is cancelled so damaged audio is not accepted. |

The match percentage is a similarity score, **not a probability**. The default threshold is 80%. More varied samples can help, but more templates also require more CPU. A continuous melody normally produces one alert; the detector re-arms after a quiet interval and at least 2.5 seconds. It cannot detect a button press that produces no audible sound. The computer must remain awake and the microphone available.

## Privacy and local files

Normal listening processes short buffers in memory and does not save audio. Calibration deliberately saves background audio, the calibration session, and selected clips under **data/** next to the app. That folder also holds device settings, the profile, detection timestamps/scores and local diagnostic logs. It may contain voices or other room sounds recorded during calibration.

The app has no network requests, telemetry or upload feature. Dependencies are downloaded only during installation. `data/`, audio files, environments and logs are excluded by `.gitignore`. Do not include your `data/` folder when sharing the project. Delete that folder with the app closed to remove calibration and settings. Developers can set `DOORBELL_DATA_DIR` to use a separate data folder.

## Development and tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests generate their own audio: fan-like background, a two-tone bell, short chirps, unrelated tones, broadband noise and claps. They exercise candidate extraction, streaming detection, repeat suppression, multi-clip selection, saving/reloading calibration and cancellation. The Tk integration tests replace hardware I/O; they do not open a microphone or play audio. GitHub Actions runs the suite on Windows with Python 3.12.

`detector.py` contains spectral features, template matching and alert timing. `widget.py` contains the Tk UI, sounddevice streams, local storage and pystray integration. Analysis uses 16 kHz mono audio, 100 ms input blocks, and one BLAS thread by default. There is no neural network or GPU requirement. CPU usage depends on the number of selected templates and the machine.

Bug reports and improvements are welcome. Include Windows/Python versions, the symptom, whether the microphone meter moves, and steps to reproduce. Check logs for personal device information before sharing them; audio recordings are not required for an initial report.

## License

[MIT](LICENSE). Third-party dependencies retain their own licenses.
