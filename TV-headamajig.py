# TV-headamajig TM
# Copyright (c) 2026 novaur

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# modding info comments:
# line 109 --> assets
# line 472 --> asset names/info
# line 497 --> mic integration with assets
# line 514 --> TV remote local webpage html

import os
import time
import subprocess
import tempfile
from pathlib import Path

import qrcode
import threading
import soundcard as sc
import numpy as np

import pygame
from werkzeug.utils import secure_filename
from flask import Flask, render_template_string, redirect, url_for, request
import cv2

import socket
from PIL import Image, ImageSequence

print("hello world! your TV is alive")

# lock threading
media_lock = threading.Lock() # protects video_cap, mode, gif_frames, etc

# pygame setup
pygame.init()
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

WIDTH, HEIGHT = 800, 600
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("TV-headamajig")
clock = pygame.time.Clock()
fullscreen = False

qr_margin = True
qr_info = "scan this QR code to open the TV remote, press 'Q' to toggle displaying the QR code"
qr_info_text = "scan to open web remote, press 'Q' to toggle overlay"
qr_info_text_fullscreen = "scan to open web remote, press 'Q' to toggle overlay"

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_IMAGE = {"png", "jpg", "jpeg", "webp", "bmp"}
ALLOWED_VIDEO = {"mp4", "avi", "mov", "mkv", "webm"}
ALLOWED_GIF   = {"gif"}

# shared state
current_index = 0
video_bounce = 0
running = True
mode = "faces" # modes: "faces", "image", "video", "gif"

custom_image = None # pygame surface

# video / gif state
video_cap = None
video_path = None
video_fps = 60
frame_interval = 1.0 / 60
last_frame_time = 0.0
video_paused = False
video_frame_surf = None # last good frame (prevents flicker)
target_size = None # (w, h) after scaling

# gif state
gif_frames = [] # list of pygame surfaces
gif_durations = [] # ms per frame
gif_index = 0
gif_last_time = 0.0

# audio
audio_temp_path = None
has_audio = False

# load built-in faces
image_list = []
folder_path = "assets"

if not os.path.isdir(folder_path):
    print(f"folder '{folder_path}' not found!")
else:
    print(f"looking in folder: {os.path.abspath(folder_path)}")
    for i in range(1, 35): # edit '35' to match the amount of assets you have plus one
        # ^^^ the default set of assets i had personally was 34, so it's set to 35 as the end range
        filename = f"TV{i}.png" # assets should be named "TV1.png", "TV2.png", etc... (unless you want to change the naming)
        full_path = os.path.join(folder_path, filename)
        if os.path.exists(full_path):
            try:
                img = pygame.image.load(full_path).convert_alpha()
                image_list.append(img)
                print(f"loaded: {filename}")
            except Exception as e:
                print(f"failed to load {filename}: {e}")

print(f"\ntotal images loaded: {len(image_list)}")

# helpers
def toggle_fullscreen():
    global screen, fullscreen, WIDTH, HEIGHT, qr_info, qr_info_text, qr_info_text_fullscreen, qr_margin
    fullscreen = not fullscreen
    if fullscreen:
        qr_margin = False
        qr_info_text = qr_info
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        WIDTH, HEIGHT = screen.get_size()
        overlay = create_overlay(screen.get_size())
        print("→ fullscreen ON")
    else:
        qr_margin = True
        qr_info_text = qr_info_text_fullscreen
        screen = pygame.display.set_mode((800, 600))
        WIDTH, HEIGHT = 800, 600
        overlay = create_overlay(screen.get_size())
        print("→ fullscreen OFF")
        
def _mic_listener():
    """background thread that reads the default microphone with soundcard."""
    global mic_volume, _mic_running

    try:
        mic = sc.default_microphone()
        print(f"[mic] using: {mic.name}")

        # 16 kHz mono, small blocks
        with mic.recorder(samplerate=16000, channels=1, blocksize=1024) as recorder:
            while _mic_running:
                data = recorder.record(numframes=1024) # shape (1024, 1)
                volume = np.sqrt(np.mean(data**2))

                # smooth + sensitivity
                raw = float(volume) * mic_sensitivity
                mic_volume = mic_volume * 0.55 + min(raw, 1.0) * 0.45
        
        volume = np.sqrt(np.mean(data**2))
        # DEBUG --> don't use unless testing
        #if volume > 0.01:
        #    print(f"[mic raw] {volume:.4f}  → smoothed {mic_volume:.3f}")

    except Exception as e:
        print(f"[mic] listener error: {e}")
        mic_volume = 0.0
    finally:
        _mic_running = False

def start_microphone():
    global _mic_thread, _mic_running, mic_enabled
    if _mic_running:
        return

    _mic_running = True
    mic_enabled = True
    _mic_thread = threading.Thread(target=_mic_listener, daemon=True)
    _mic_thread.start()
    print("[mic] started")

def stop_microphone():
    global _mic_running, mic_enabled, mic_volume, is_talking, current_index
    _mic_running = False
    mic_enabled = False
    mic_volume = 0.0

    if is_talking:
        current_index = original_face_index
        is_talking = False

    # give the thread a moment to exit
    if _mic_thread is not None:
        _mic_thread.join(timeout=1.0)
    print("[mic] stopped")

def update_mic_volume():
    global current_index, original_face_index, is_talking

    if not mic_enabled:
        if is_talking:
            current_index = original_face_index
            is_talking = False
        return

    if mode != "faces":
        return

    # talking face switching
    if current_index in TALKING_PAIRS:
        if mic_volume >= talk_threshold and not is_talking:
            original_face_index = current_index
            current_index = TALKING_PAIRS[current_index]
            is_talking = True
        elif mic_volume < talk_threshold * 0.7 and is_talking:
            current_index = original_face_index
            is_talking = False

    elif current_index in TALKING_PAIRS_REVERSE:
        if mic_volume < talk_threshold * 0.7 and is_talking:
            current_index = original_face_index
            is_talking = False

def get_reactive_scale(base_scale=1.0):
    """pure multiplier based on mic volume (1.0 = no change)."""
    if not mic_enabled or mic_volume < mic_threshold:
        return base_scale
    # 1.0 → up to ~1.18
    boost = 1.0 + (mic_volume - mic_threshold) * 0.25
    return base_scale * min(boost, 1.18)

def scale_surface_reactive(surf):
    """fit to window first, then apply mic scale + bounce."""
    if surf is None:
        return None, 0

    w, h = surf.get_size()

    # fit inside the current window
    fit = min(WIDTH / w, HEIGHT / h) * 0.95

    # apply microphone boost on top
    reactive = get_reactive_scale(base_scale=1.0)
    final_scale = fit * reactive

    new_size = (max(1, int(w * final_scale)), max(1, int(h * final_scale)))
    scaled = pygame.transform.smoothscale(surf, new_size)

    # bounce
    bounce = 0
    if mic_enabled and mic_volume > mic_threshold:
        bounce = int((mic_volume - mic_threshold) * 14)

    return scaled, bounce

def stop_media():
    global mode, custom_image, video_cap, video_path
    global video_frame_surf, gif_frames, has_audio, audio_temp_path
    global video_paused, gif_index

    with media_lock:
        if video_cap is not None:
            try:
                video_cap.release()
            except Exception:
                pass
            video_cap = None

        video_path = None
        video_frame_surf = None
        gif_frames = []
        gif_index = 0
        custom_image = None
        mode = "faces"
        video_paused = False

        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

        if audio_temp_path and os.path.exists(audio_temp_path):
            try:
                os.remove(audio_temp_path)
            except OSError:
                pass
        audio_temp_path = None
        has_audio = False

def start_video(path):
    global video_cap, video_path, mode, video_fps, frame_interval
    global last_frame_time, video_paused, video_frame_surf

    # stop everything first (releases opencv),
    # then extract audio, then open opencv.
    stop_media()

    # extract audio while no opencv capture is open
    extract_audio(path)

    with media_lock:
        video_cap = cv2.VideoCapture(path)
        if not video_cap.isOpened():
            print("could not open video")
            video_cap = None
            return

        video_path = path
        mode = "video"
        video_fps = video_cap.get(cv2.CAP_PROP_FPS) or 30.0
        if video_fps < 1 or video_fps > 120:
            video_fps = 30.0
        frame_interval = 1.0 / video_fps
        last_frame_time = time.perf_counter()
        video_paused = False
        video_frame_surf = None

        # make sure we start at the beginning
        video_cap.set(cv2.CAP_PROP_POS_MSEC, 0)

    print(f"[web] playing video: {path} @ {video_fps:.1f} fps")

def load_custom_image(path):
    global custom_image, mode
    stop_media()
    try:
        img = pygame.image.load(path).convert_alpha()
        custom_image = img
        mode = "image"
        print(f"[web] loaded custom image: {path}")
    except Exception as e:
        print(f"failed to load image: {e}")

def extract_audio(path: str) -> bool:
    """extract audio with ffmpeg CLI (avoids moviepy + OpenCV FFmpeg clash)."""
    global audio_temp_path, has_audio
    has_audio = False
    audio_temp_path = None

    try:
        # create a unique temp wav
        fd, audio_temp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        # -y overwrite, -vn no video, short timeout, quiet
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            audio_temp_path
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60
        )
        if result.returncode != 0 or not os.path.exists(audio_temp_path) or os.path.getsize(audio_temp_path) < 1000:
            # no audio track or extraction failed
            if audio_temp_path and os.path.exists(audio_temp_path):
                os.remove(audio_temp_path)
            audio_temp_path = None
            return False

        pygame.mixer.music.load(audio_temp_path)
        pygame.mixer.music.play()
        has_audio = True
        print("[audio] playing extracted track")
        return True

    except Exception as e:
        print(f"[audio] extract failed: {e}")
        if audio_temp_path and os.path.exists(audio_temp_path):
            try:
                os.remove(audio_temp_path)
            except OSError:
                pass
        audio_temp_path = None
        has_audio = False
        return False

def seek_video_to_time(seconds: float):
    """seek the opencv capture to a specific time (seconds)."""
    if video_cap is None:
        return
    video_cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)

def pause_video():
    global video_paused
    if mode not in ("video", "gif"):
        return
    video_paused = not video_paused
    if mode == "video" and has_audio:
        if video_paused:
            pygame.mixer.music.pause()
        else:
            pygame.mixer.music.unpause()
    print("paused" if video_paused else "resumed")

def restart_video():
    if mode == "video" and video_cap:
        seek_video_to_time(0.0)
        if has_audio:
            pygame.mixer.music.rewind()
            pygame.mixer.music.play()
        print("restarted video")
    elif mode == "gif":
        global gif_index, gif_last_time
        gif_index = 0
        gif_last_time = time.perf_counter()

def start_gif(path):
    """load an animated gif with pillow (opencv is unreliable for gifs)."""
    global mode, gif_frames, gif_durations, gif_index, gif_last_time

    stop_media()
    try:
        img = Image.open(path)
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            # convert to RGBA pygame surface
            rgba = frame.convert("RGBA")
            data = rgba.tobytes()
            surf = pygame.image.frombuffer(data, rgba.size, "RGBA").convert_alpha()
            frames.append(surf)
            # duration in ms (default 100 ms if missing)
            durations.append(frame.info.get("duration", 100))
        img.close()

        if not frames:
            print("gif contained no frames")
            return

        gif_frames = frames
        gif_durations = durations
        gif_index = 0
        gif_last_time = time.perf_counter()
        mode = "gif"
        print(f"[web] playing gif: {path} ({len(frames)} frames)")
    except Exception as e:
        print(f"failed to load gif: {e}")

def get_gallery():
    """return list of (filename, full_path, kind) for everything in uploads/."""
    items = []
    for name in sorted(os.listdir(UPLOAD_FOLDER)):
        path = os.path.join(UPLOAD_FOLDER, name)
        if not os.path.isfile(path):
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_IMAGE:
            kind = "image"
        elif ext in ALLOWED_VIDEO:
            kind = "video"
        elif ext in ALLOWED_GIF:
            kind = "gif"
        else:
            continue
        items.append((name, path, kind))
    return items

def create_overlay(size):
    """helper function to generate a matching translucent surface."""
    surf = pygame.Surface(size, pygame.SRCALPHA)
    surf.fill((0, 0, 0, 180)) # dark tint with ~70% opacity
    return surf

# flask
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

# default faces (feel free to change or to use as is with your own assets!)
face_names = [
    "smile/default", "what/shocked", "straight closed mouth :|", "frown/sad",
    "yeah!/open mouth happy", "crying/really sad", "open mouth slightly happy",
    "anxious/nervous", "puzzled", "skeptical closed frown",
    "confused slight open mouth", "happy slight open mouth 2", "confused frown",
    "excuse me?", "mad", "mad open mouth", "haha/slightly sinister",
    "angry / madder", "green screen", "additional image #1", "rizz wink",
    "rizz no wink", "giggly/laughing", "super happy", "smug",
    "additional image #2", "additional image #3", "additional image #4",
    "additional image #5", "additional image #6",
    "additional image #7", "additional image #8", "additional image #9", "additional image #10"
]

# microphone reactive state
mic_enabled = False
mic_volume = 0.0
mic_sensitivity = 3.0
mic_threshold = 0.03 # start reacting
talk_threshold = 0.12 # switch to open-mouth face
original_face_index = 0 # remembers the face before talking
is_talking = False
_mic_thread = None
_mic_running = False

# closed-mouth index → open-mouth index
# THIS IS BASED AROUND DEFAULT ASSETS, please change accordingly if you are changing any assets above
TALKING_PAIRS = {
    0: 4,   # smile/default          → yeah!/open mouth happy
    2: 6,   # straight closed mouth  → open mouth slightly happy
    3: 5,   # frown/sad              → crying/really sad
    7: 10,  # anxious/nervous        → confused slight open mouth
    9: 12,  # skeptical closed frown → confused frown
    14: 15, # mad                    → mad open mouth
    17: 15, # angry / madder         → mad open mouth
    21: 22, # rizz no wink           → giggly/laughing
}

# inverse map
TALKING_PAIRS_REVERSE = {v: k for k, v in TALKING_PAIRS.items()}

# html for TV remote locally host web page, feel free to change!
CONTROL_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>TV Remote</title>
    <style>
        body { background:#000; color:#eee; text-align:center; padding:16px; }
        h1 { margin-bottom:8px; }
        h2 { margin-top:32px; margin-bottom:12px; color:#0ff; }
        .current { font-size:1.3rem; margin-bottom:20px; color:#0f0; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; max-width:640px; margin:0 auto; }
        a.button, button.button {
            display:block; padding:14px; background:#111; color:cyan; border:1px solid #0ff;
            text-decoration:none; border-radius:10px; font-size:0.95rem; cursor:pointer;
        }
        a.button:hover, button.button:hover { background:#222; }
        .nav { margin-top:28px; display:flex; justify-content:center; gap:16px; flex-wrap:wrap; }
        .nav a { background:#003366; }
        .gallery { max-width:640px; margin:0 auto; text-align:left; }
        .gallery-item {
            display:flex; align-items:center; justify-content:space-between;
            background:#111; border:1px solid #333; border-radius:8px;
            padding:10px 14px; margin:8px 0; gap:10px;
        }
        .gallery-item span { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .gallery-item .actions { display:flex; gap:8px; flex-shrink:0; }
        .gallery-item a { padding:8px 12px; font-size:0.85rem; }
        .danger { border-color:#f44 !important; color:#f88 !important; }
        .controls { display:flex; justify-content:center; gap:12px; flex-wrap:wrap; margin:16px 0; }
        .controls a { min-width:90px; }
        form { margin:12px 0; }
        input[type=file] { color:#ccc; }
        .hint { font-size:0.85rem; color:#888; }
    </style>
</head>
<body>
    <h1>TV Head</h1>
    <div class="current">
        {% if mode == "faces" %}
            Currently showing: Face {{ current + 1 }}
        {% elif mode == "image" %}
            Currently showing: custom image
        {% elif mode == "video" %}
            Currently playing video {% if paused %}(paused){% endif %}
        {% elif mode == "gif" %}
            Currently playing GIF
        {% endif %}
    </div>

    {% if mode == "video" or mode == "gif" %}
    <div class="controls">
        <a class="button" href="/video/pause">{{ "Resume" if paused else "Pause" }}</a>
        <a class="button" href="/video/restart">Restart</a>
        <a class="button" href="/back_to_faces">Stop</a>
    </div>
    {% endif %}

    <div class="grid">
        {% for i in range(total) %}
            <a class="button" href="/set/{{ i }}">{{ face_names[i] }}</a>
        {% endfor %}
    </div>

    <div class="nav">
        <a class="button" href="/prev">← Previous</a>
        <a class="button" href="/next">Next →</a>
        <a class="button" href="/back_to_faces">← Faces</a>
    </div>

    <h2>Upload</h2>
    <form action="/upload" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept="image/*,video/*,.gif" required>
        <br>
        <button type="submit" class="button" style="display:inline-block;margin-top:10px;">
            Upload &amp; Show / Play
        </button>
    </form>
    <p class="hint">Images · Videos (mp4/webm/mov…) · Animated GIFs</p>

    <h2>Gallery ({{ gallery|length }} files)</h2>
    <div class="gallery">
        {% if not gallery %}
            <p class="hint">No uploaded files yet.</p>
        {% endif %}
        {% for name, path, kind in gallery %}
        <div class="gallery-item">
            <span title="{{ name }}">{{ name }} <small>({{ kind }})</small></span>
            <div class="actions">
                {% if kind == "image" %}
                    <a class="button" href="/show/{{ name }}">Show</a>
                {% else %}
                    <a class="button" href="/play/{{ name }}">Play</a>
                {% endif %}
                <a class="button danger" href="/delete/{{ name }}">Delete</a>
            </div>
        </div>
        {% endfor %}
    </div>

    {% if gallery %}
    <p style="margin-top:20px;">
        <a class="button danger" href="/delete_all" onclick="return confirm('Delete ALL uploaded files?')">
            Delete all uploads
        </a>
    </p>
    {% endif %}
    
    <br>
    
    <h2>Microphone</h2>
    <div class="controls">
        <a class="button" href="/mic/toggle">
            {{ "Disable Mic" if mic_enabled else "Enable Mic" }}
        </a>
    </div>
    <p class="hint">
        Sensitivity:
        <a href="/mic/sensitivity/0.8">Low</a> ·
        <a href="/mic/sensitivity/1.5">Normal</a> ·
        <a href="/mic/sensitivity/2.5">High</a>
        {% if mic_enabled %} · Level: {{ "%.0f"|format(mic_volume*100) }}%{% endif %}
    </p>
    
    <br>
    
    <h2>Misc</h2>
    <a class="button danger" href="/reset">Reset / Recover</a>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(
        CONTROL_PAGE,
        current=current_index,
        total=len(image_list),
        face_names=face_names,
        mode=mode,
        paused=video_paused,
        gallery=get_gallery(),
        mic_enabled=mic_enabled,
        mic_volume=mic_volume,
    )

@app.route("/set/<int:idx>")
def set_face(idx):
    global current_index, is_talking, original_face_index
    stop_media()
    if 0 <= idx < len(image_list):
        current_index = idx
        original_face_index = idx
        is_talking = False
        print(f"[web] set to face {idx + 1}")
    return redirect(url_for("index"))

@app.route("/next")
def next_face():
    global current_index, is_talking, original_face_index
    stop_media()
    if image_list:
        current_index = (current_index + 1) % len(image_list)
        original_face_index = current_index
        is_talking = False
    return redirect(url_for("index"))

@app.route("/prev")
def prev_face():
    global current_index, is_talking, original_face_index
    stop_media()
    if image_list:
        current_index = (current_index - 1) % len(image_list)
        original_face_index = current_index
        is_talking = False
    return redirect(url_for("index"))

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return redirect(url_for("index"))
    f = request.files["file"]
    if not f.filename:
        return redirect(url_for("index"))

    filename = secure_filename(f.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(path)

    if ext in ALLOWED_IMAGE:
        load_custom_image(path)
    elif ext in ALLOWED_VIDEO:
        start_video(path)
    elif ext in ALLOWED_GIF:
        start_gif(path)
    else:
        print("unsupported file type:", ext)

    return redirect(url_for("index"))

@app.route("/show/<path:name>")
def show_file(name):
    path = os.path.join(UPLOAD_FOLDER, secure_filename(name))
    if os.path.isfile(path):
        load_custom_image(path)
    return redirect(url_for("index"))

@app.route("/play/<path:name>")
def play_file(name):
    path = os.path.join(UPLOAD_FOLDER, secure_filename(name))
    if not os.path.isfile(path):
        return redirect(url_for("index"))
    ext = name.rsplit(".", 1)[-1].lower()
    if ext in ALLOWED_GIF:
        start_gif(path)
    else:
        start_video(path)
    return redirect(url_for("index"))

@app.route("/delete/<path:name>")
def delete_file(name):
    path = os.path.join(UPLOAD_FOLDER, secure_filename(name))
    # if the file currently being shown is deleted, go back to faces
    if video_path and os.path.abspath(video_path) == os.path.abspath(path):
        stop_media()
    if os.path.isfile(path):
        os.remove(path)
        print(f"deleted {name}")
    return redirect(url_for("index"))

@app.route("/delete_all")
def delete_all():
    stop_media()
    for name in os.listdir(UPLOAD_FOLDER):
        path = os.path.join(UPLOAD_FOLDER, name)
        if os.path.isfile(path):
            os.remove(path)
    return redirect(url_for("index"))

@app.route("/back_to_faces")
def back_to_faces():
    stop_media()
    return redirect(url_for("index"))

@app.route("/video/pause")
def video_pause():
    pause_video()
    return redirect(url_for("index"))

@app.route("/video/restart")
def video_restart():
    restart_video()
    return redirect(url_for("index"))

@app.route("/mic/toggle")
def mic_toggle():
    global mic_enabled
    if mic_enabled:
        stop_microphone()
    else:
        start_microphone()
    return redirect(url_for("index"))

@app.route("/mic/sensitivity/<float:val>")
def mic_set_sensitivity(val):
    global mic_sensitivity
    mic_sensitivity = max(0.3, min(val, 5.0))
    return redirect(url_for("index"))

@app.route("/reset")
def reset():
    stop_media()
    return redirect(url_for("index"))

def run_web():
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

web_thread = threading.Thread(target=run_web, daemon=True)
web_thread.start()
print("\n🌐 web control panel running!")
print("on this computer:   http://localhost:8080")
print("on your phone:      http://<your-pc-ip>:8080")
print("\n")

# qr code stuff

# get the local hostname
hostname = socket.gethostname()

# get the corresponding ip address
local_ip = socket.gethostbyname(hostname)\

# encode into qr code
qr_data = "http://" + local_ip + ":8080"

# generate the local ip website's qr matrix using the qrcode library
qr = qrcode.QRCode(version=1, box_size=10, border=2)
qr.add_data(qr_data)
qr.make(fit=True)

# convert matrix into a pillow image (RGB format)
pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

# convert the pillow Image into a native pygame surface object
img_bytes = pil_img.tobytes()
img_size = pil_img.size
qr_surface = pygame.image.frombytes(img_bytes, img_size, "RGB")

# center the QR surface on the application screen
qr_rect = qr_surface.get_rect(center=(WIDTH // 2, HEIGHT // 2))
show_qrcode = True

# main pygame loop
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                if fullscreen:
                    toggle_fullscreen()
                else:
                    running = False
            elif event.key == pygame.K_F11:
                toggle_fullscreen()
            elif event.key == pygame.K_q:
                show_qrcode = not show_qrcode
            elif event.key == pygame.K_RIGHT and mode == "faces" and image_list:
                current_index = (current_index + 1) % len(image_list)
            elif event.key == pygame.K_LEFT and mode == "faces" and image_list:
                current_index = (current_index - 1) % len(image_list)
            elif event.key == pygame.K_SPACE and mode in ("video", "gif"):
                pause_video()
                
    update_mic_volume()

    screen.fill((0, 0, 0))
    
    try:
        # faces
        if mode == "faces" and image_list:
            img = image_list[current_index]
            scaled, bounce = scale_surface_reactive(img)
            rect = scaled.get_rect(center=(WIDTH // 2, HEIGHT // 2 - bounce))
            screen.blit(scaled, rect)

        # images
        elif mode == "image" and custom_image is not None: 
            scaled, bounce = scale_surface_reactive(custom_image)
            rect = scaled.get_rect(center=(WIDTH // 2, HEIGHT // 2 - bounce))
            screen.blit(scaled, rect)
    
        # video (opencv)
        elif mode == "video":
            try:
                with media_lock:
                    if video_cap is None:
                        mode = "faces"
                    else:
                        now = time.perf_counter()
                        ret = False
                        frame = None

                        if not video_paused:
                            if has_audio:
                                audio_ms = pygame.mixer.music.get_pos()
                                if audio_ms >= 0:
                                    target_sec = audio_ms / 1000.0
                                    current_sec = video_cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                                    if abs(current_sec - target_sec) > 0.12:
                                        video_cap.set(cv2.CAP_PROP_POS_MSEC, target_sec * 1000.0)

                                    ret, frame = video_cap.read()
                                    if not ret:
                                        video_cap.set(cv2.CAP_PROP_POS_MSEC, 0)
                                        try:
                                            pygame.mixer.music.rewind()
                                            pygame.mixer.music.play()
                                        except Exception:
                                            pass
                                        ret, frame = video_cap.read()
                                else:
                                    ret, frame = video_cap.read()
                            else:
                                if (now - last_frame_time) >= frame_interval:
                                    ret, frame = video_cap.read()
                                    if not ret:
                                        video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                        ret, frame = video_cap.read()
                                    last_frame_time = now

                            if ret and frame is not None:
                                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                surf = pygame.surfarray.make_surface(np.swapaxes(frame, 0, 1))
                                scaled, bounce = scale_surface_reactive(surf)
                                video_frame_surf = scaled
                                video_bounce = bounce   # store bounce for drawing
                            else:
                                video_bounce = 0
                        else:
                            video_bounce = 0

                        # always draw last good frame
                        if video_frame_surf is not None:
                            rect = video_frame_surf.get_rect(
                                center=(WIDTH // 2, HEIGHT // 2 - video_bounce)
                            )
                            screen.blit(video_frame_surf, rect)
                            
            except Exception as e:
                print(f"[video error] {e}")
                stop_media() # clean reset, goes back to faces

        # animated gifs
        elif mode == "gif" and gif_frames:
            now = time.perf_counter()
            duration_s = gif_durations[gif_index] / 1000.0
            if (now - gif_last_time) >= duration_s:
                gif_index = (gif_index + 1) % len(gif_frames)
                gif_last_time = now
            
            scaled, bounce = scale_surface_reactive(gif_frames[gif_index])
            rect = scaled.get_rect(center=(WIDTH // 2, HEIGHT // 2 - bounce))
            screen.blit(scaled, rect)
            
    except Exception as e:
        print(f"[main loop error] {e}")
        stop_media() # clean reset, goes back to faces
    
    if show_qrcode:
        overlay = create_overlay(screen.get_size())
        screen.blit(overlay, (0, 0))
        
        screen.blit(qr_surface, qr_rect)  # render the QR code to the screen
        font = pygame.font.SysFont(None, 36)
        text = font.render(qr_info_text, True, (200, 200, 200))
        text_rect = text.get_rect(center=(WIDTH // 2, HEIGHT // 2))
        
        if qr_margin == True:
            margin = 40
            text_rect.midbottom = (WIDTH // 2, HEIGHT - margin)
            screen.blit(text, text_rect)
        else:
            margin = 0
            screen.blit(text, text_rect)

    pygame.display.flip()
    clock.tick(60)

# cleanup
stop_microphone()
stop_media()
pygame.quit()
