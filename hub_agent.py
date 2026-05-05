#!/usr/bin/env python3
"""
iLeague Hub Agent v3
====================
Scan bảng điểm (Hello/9Score/Arena) trên LAN → đọc score → push lên iLeague.
Chạy nền trên PC tính tiền tại CLB.

Features:
- System tray icon (Windows/Mac)
- Config lần đầu (tên CLB + Gmail)
- Auto-start Windows
- Auto-filter chỉ bảng điểm đang active
- Web dashboard tại localhost:5050

Chạy: python3 hub_agent.py
Pack: pyinstaller --onefile --noconsole --icon=icon.ico hub_agent.py
"""

import socket
import threading
import time
import json
import os
import re
import sys
import subprocess
import unicodedata
import webbrowser
import platform
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory, redirect
from flask_cors import CORS

# ============================================================
# PATHS & CONFIG FILE
# ============================================================
IS_FROZEN = getattr(sys, 'frozen', False)  # PyInstaller
if IS_FROZEN:
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_FILE = BASE_DIR / 'ileague_hub_config.json'
VERSION = '3.0.0'

def load_config():
    """Load config from file, return defaults if not exists."""
    defaults = {
        'club_name': '',
        'email': '',
        'ileague_api': 'https://ileague.info/api.php',
        'scan_interval': 3,
        'network_scan_interval': 30,
        'scoreboard_port': 8080,
        'active_threshold_hours': 2,
        'auto_start': False,
        'setup_done': False,
        'active_tournament_id': None,
        'auto_map_enabled': True
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
            defaults.update(saved)
        except:
            pass
    return defaults

def save_config(cfg):
    """Save config to file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

config = load_config()

# ============================================================
# STATE
# ============================================================
devices = {}       # {ip: {ip, status, last_score, last_update, found_at}}
mappings = {}      # {ip: {tournament_id, match_code}}
score_log = []     # [{timestamp, ip, scores, pushed}]
agent_status = {
    'running': False,
    'last_scan': None,
    'devices_found': 0,
    'version': VERSION,
    'errors': []
}
pro_status = {
    'active': None,   # None = not checked, True/False after check
    'in_grace': False,
    'plan': None,
    'days_left': 0,
    'expires_at': None,
    'last_check': None
}

# Active tournament for auto-mapping. Restored from config on startup.
active_tournament_id = config.get('active_tournament_id')

# Cache of hub_matches response per active tournament. Refreshed every 30s.
tournament_matches_cache = {
    'tid': None,
    'matches': [],
    'fetched_at': None,
    'tournament_name': ''
}

# ============================================================
# NETWORK UTILS
# ============================================================

def get_local_subnet():
    """Get local subnet from network interfaces."""
    try:
        if platform.system() == 'Darwin':
            r = subprocess.run(['ifconfig', 'en0'], capture_output=True, text=True)
            for line in r.stdout.split('\n'):
                if 'inet ' in line and 'broadcast' in line:
                    ip = line.split()[1]
                    return '.'.join(ip.split('.')[:3])
        elif platform.system() == 'Windows':
            r = subprocess.run(['ipconfig'], capture_output=True, text=True)
            for line in r.stdout.split('\n'):
                if 'IPv4' in line:
                    ip = line.split(':')[-1].strip()
                    if ip.startswith('192.168') or ip.startswith('10.') or ip.startswith('172.'):
                        return '.'.join(ip.split('.')[:3])
        else:
            r = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
            for ip in r.stdout.strip().split():
                if ip.startswith('192.168') or ip.startswith('10.'):
                    return '.'.join(ip.split('.')[:3])
    except:
        pass
    return '192.168.1'


def scan_network():
    """Scan local network for scoreboards on configured port."""
    subnet = get_local_subnet()
    port = config.get('scoreboard_port', 8080)
    found = []

    def try_connect(ip):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            if s.connect_ex((ip, port)) == 0:
                try:
                    r = requests.get(f'http://{ip}:{port}/', timeout=2)
                    if 'Game_' in r.text:
                        found.append(ip)
                except:
                    pass
            s.close()
        except:
            pass

    threads = []
    for i in range(1, 255):
        ip = f'{subnet}.{i}'
        t = threading.Thread(target=try_connect, args=(ip,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=2)

    return found


def get_scoreboard_scores(ip):
    """Get current scores from scoreboard via HTTP."""
    port = config.get('scoreboard_port', 8080)
    try:
        r = requests.get(f'http://{ip}:{port}/', timeout=2)
        import re
        folders = re.findall(r'Game_[0-9_-]+', r.text)

        if not folders:
            return None

        latest = sorted(folders)[-1]

        r = requests.get(f'http://{ip}:{port}/{latest}/data.json', timeout=2)
        data = r.json()

        return {
            'player1_name': data.get('playerName1', 'Người chơi 1'),
            'player2_name': data.get('playerName2', 'Người chơi 2'),
            'score1': data.get('totalScore1', 0),
            'score2': data.get('totalScore2', 0),
            'turns1': data.get('turn1', 0),
            'turns2': data.get('turn2', 0),
            'avg1': data.get('avgScore1', '0'),
            'avg2': data.get('avgScore2', '0'),
            'high_run1': data.get('highRun1', 0),
            'high_run2': data.get('highRun2', 0),
            'game_type': data.get('gameType', {}).get('game', ''),
            'is_end': data.get('isEnd', False),
            'game_folder': latest
        }
    except:
        return None


# ============================================================
# ACTIVE FILTER — only show scoreboards with recent games
# ============================================================

def is_active_device(dev):
    """Check if device has a recent game (within threshold hours)."""
    threshold = config.get('active_threshold_hours', 2)
    last_update = dev.get('last_update')
    if not last_update:
        return False
    try:
        t = datetime.fromisoformat(last_update)
        return datetime.now() - t < timedelta(hours=threshold)
    except:
        return False


def get_active_devices():
    """Return only devices with recent game activity."""
    return {ip: dev for ip, dev in devices.items() if is_active_device(dev)}


# ============================================================
# PUSH TO ILEAGUE SERVER
# ============================================================

def push_devices_to_server():
    """Push active device data to iLeague server."""
    if not config.get('setup_done'):
        return
    try:
        active = get_active_devices()
        payload = {
            'agent_id': socket.gethostname().replace(' ', '_'),
            'agent_name': config.get('club_name', 'CLB'),
            'email': config.get('email', ''),
            'devices': list(active.values()),
            'version': VERSION
        }
        requests.post(f'{config["ileague_api"]}?action=hub_push',
                       json=payload, timeout=5)
    except:
        pass


def _compute_status(scores):
    """Decide match status from scoreboard reading.
    - end: game finished (isEnd=true)
    - next: not finished but no points scored yet (0-0) → trận chưa bắt đầu thực sự
    - live: actively being played
    """
    if scores.get('is_end'):
        return 'end'
    s1 = scores.get('score1') or 0
    s2 = scores.get('score2') or 0
    if s1 == 0 and s2 == 0:
        return 'next'
    return 'live'


def push_score_to_ileague(mapping, scores):
    """Push score update to iLeague server for a mapped match."""
    if not mapping or not scores:
        return False
    try:
        payload = {
            'tournament_id': mapping.get('tournament_id'),
            'match_code': mapping.get('match_code'),
            'player1_score': scores.get('score1', 0),
            'player2_score': scores.get('score2', 0),
            'innings': scores.get('turns1', 0) + scores.get('turns2', 0),
            'status': _compute_status(scores),
            'email': config.get('email', '')
        }
        # Group matches need indexes for backend to update the right slot
        if 'group_idx' in mapping and 'match_idx' in mapping:
            payload['group_idx'] = mapping['group_idx']
            payload['match_idx'] = mapping['match_idx']
        if 'cell_id' in mapping:
            payload['cell_id'] = mapping['cell_id']
        resp = requests.post(
            f'{config["ileague_api"]}?action=score_update',
            json=payload, timeout=5
        )
        result = resp.json() if resp.status_code == 200 else {}
        return result.get('updated', False)
    except:
        return False


# ============================================================
# AUTO-MAP BY PLAYER NAMES
# ============================================================

def _normalize_name(s):
    """Normalize Vietnamese player name for fuzzy comparison.
    Lowercase, strip diacritics, fold đ→d, collapse whitespace.
    NFD chỉ tách combining marks (Mn). Chữ Đ/đ là codepoint đơn nên fold tay.
    """
    if not s:
        return ''
    s = str(s).lower().replace('đ', 'd')
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.split())


def _fetch_tournament_matches(tid, force=False):
    """Fetch list of matches in a tournament from ileague.info. Cache 30s."""
    if not tid:
        return []
    cache = tournament_matches_cache
    now = time.time()
    if (not force
            and cache['tid'] == tid
            and cache['fetched_at']
            and (now - cache['fetched_at']) < 30):
        return cache['matches']
    try:
        url = config.get('ileague_api', 'https://ileague.info/api.php')
        r = requests.get(url, params={'action': 'hub_matches', 'id': tid}, timeout=8)
        if r.status_code != 200:
            return cache.get('matches', [])
        d = r.json()
        cache['tid'] = tid
        cache['matches'] = d.get('matches', []) or []
        cache['tournament_name'] = d.get('name', '')
        cache['fetched_at'] = now
        return cache['matches']
    except Exception as e:
        agent_status['errors'].append(f'Fetch matches error: {e}')
        return cache.get('matches', [])


def auto_map_by_names(ip, scores):
    """Try to map a scoreboard IP to a match by fuzzy player-name pair.

    - Skip if no active tournament set or auto-map disabled.
    - Skip if IP already manually mapped (mapping without 'auto' flag).
    - Re-map if existing auto-mapping no longer matches the names.

    Returns True if a mapping was created/updated, False otherwise.
    """
    if not config.get('auto_map_enabled', True):
        return False
    if not active_tournament_id or not scores:
        return False

    p1_norm = _normalize_name(scores.get('player1_name'))
    p2_norm = _normalize_name(scores.get('player2_name'))
    if not p1_norm or not p2_norm:
        return False

    existing = mappings.get(ip)
    if existing and not existing.get('auto'):
        # Manual mapping — never override
        return False

    matches = _fetch_tournament_matches(active_tournament_id)
    if not matches:
        return False

    candidates = []
    for m in matches:
        m1 = _normalize_name(m.get('player1'))
        m2 = _normalize_name(m.get('player2'))
        if not m1 or not m2:
            continue
        if (p1_norm == m1 and p2_norm == m2) or (p1_norm == m2 and p2_norm == m1):
            candidates.append(m)

    if not candidates:
        return False

    # Prefer matches still in play — skip already-finished trận
    pending = [m for m in candidates if m.get('status') not in ('done', 'end')]
    chosen = pending[0] if pending else candidates[0]
    new_code = chosen.get('code')

    # Already correctly auto-mapped → no-op
    if existing and existing.get('match_code') == new_code:
        return False

    mapping = {
        'tournament_id': active_tournament_id,
        'match_code': new_code,
        'auto': True,
        'mapped_at': datetime.now().isoformat(),
        'players': f"{chosen.get('player1')} vs {chosen.get('player2')}",
        'label': chosen.get('label', new_code)
    }
    if chosen.get('type') == 'group':
        mapping['group_idx'] = chosen.get('group_idx')
        mapping['match_idx'] = chosen.get('match_idx')
    elif chosen.get('type') == 'bracket':
        mapping['cell_id'] = chosen.get('cell_id')

    mappings[ip] = mapping
    print(f'🔗 Auto-mapped {ip} → {mapping["label"]} ({mapping["players"]})')
    return True


# ============================================================
# CAMERA RTSP DISCOVERY
# ============================================================
# Mỗi scoreboard Hello/9Score/Arena chạy HTTP server :8080 và đã được set
# RTSP camera trong app config. Thử probe nhiều paths để tìm RTSP URL — cache
# kết quả per-IP trong cameras.json. User có thể override manual.

CAMERAS_FILE = BASE_DIR / 'cameras.json'

# {ip: {rtsp_url, discovered, source, last_probe, probe_log[]}}
cameras_db = {}

# Common config paths to probe on scoreboard HTTP servers. The 3 partner apps
# are closed-source — list grows as we reverse-engineer in real CLBs. Each path
# is fetched and the response body searched for an rtsp:// substring.
RTSP_PROBE_PATHS = [
    '/',  # root listing
    '/settings.json', '/config.json', '/app_config.json',
    '/Settings/config.json', '/Config/settings.json',
    '/Settings/', '/Config/',
    '/app/settings.json', '/app/config.json',
    '/preferences.json', '/prefs.xml', '/shared_prefs.xml',
    '/camera.json', '/rtsp.txt', '/stream.txt', '/stream.json',
    '/info.json', '/device.json', '/device_info.json'
]

RTSP_RE = re.compile(r'rtsp://[^\s"\'<>\\\)]+', re.IGNORECASE)


def load_cameras():
    global cameras_db
    if CAMERAS_FILE.exists():
        try:
            with open(CAMERAS_FILE, 'r') as f:
                cameras_db = json.load(f)
        except Exception:
            cameras_db = {}


def save_cameras():
    try:
        with open(CAMERAS_FILE, 'w') as f:
            json.dump(cameras_db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        agent_status['errors'].append(f'Save cameras error: {e}')


def discover_rtsp_for_scoreboard(ip, port=None):
    """Probe a scoreboard HTTP server for RTSP camera URL.
    Returns (rtsp_url, probe_log). rtsp_url is None if not found.
    """
    port = port or config.get('scoreboard_port', 8080)
    log = []
    for path in RTSP_PROBE_PATHS:
        url = f'http://{ip}:{port}{path}'
        try:
            r = requests.get(url, timeout=2)
            if r.status_code != 200:
                log.append({'path': path, 'status': r.status_code, 'found': False})
                continue
            body = r.text
            m = RTSP_RE.search(body)
            if m:
                rtsp = m.group(0).rstrip('.,;')
                log.append({'path': path, 'status': 200, 'found': True, 'rtsp': rtsp})
                return rtsp, log
            log.append({'path': path, 'status': 200, 'found': False, 'size': len(body)})
        except Exception as e:
            log.append({'path': path, 'status': 'err', 'found': False, 'err': str(e)[:80]})
    return None, log


def ensure_camera_for(ip, force_reprobe=False):
    """Ensure cameras_db has an entry for IP. Probe if missing or forced.
    Returns the dict entry.
    """
    cam = cameras_db.get(ip)
    if cam and not force_reprobe and cam.get('rtsp_url') and cam.get('source') == 'manual':
        # Manual entry — never auto-overwrite
        return cam
    if cam and not force_reprobe and cam.get('rtsp_url') and cam.get('source') == 'discovered':
        # Already discovered — skip unless re-probe requested
        return cam

    rtsp, log = discover_rtsp_for_scoreboard(ip)
    cam = cameras_db.get(ip) or {}
    cam['ip'] = ip
    cam['last_probe'] = datetime.now().isoformat()
    cam['probe_log'] = log[-20:]  # keep last 20 attempts
    if rtsp:
        # Don't override manual with discovered
        if cam.get('source') != 'manual':
            cam['rtsp_url'] = rtsp
            cam['source'] = 'discovered'
            cam['discovered'] = True
    cameras_db[ip] = cam
    save_cameras()
    return cam


def set_camera_manual(ip, rtsp_url):
    """Set a manual RTSP URL for a scoreboard IP. Persists immediately."""
    cam = cameras_db.get(ip) or {'ip': ip}
    cam['rtsp_url'] = (rtsp_url or '').strip() or None
    cam['source'] = 'manual' if cam['rtsp_url'] else 'cleared'
    cam['set_at'] = datetime.now().isoformat()
    cameras_db[ip] = cam
    save_cameras()
    return cam


# ============================================================
# OVERLAY RENDERING (Pillow)
# ============================================================
# Render PNG overlays per-match composited by ffmpeg over RTSP camera feed.
# Two overlay layers per stream:
#   1. static.png  — generated once at go-live: lower-third with player names,
#                    club/tournament logo, sponsor banner strip
#   2. score.txt   — atomically rewritten every 2s with live "5 - 3"; ffmpeg
#                    drawtext reload=1 picks up changes without restart
#
# Banner crawl animation handled at ffmpeg level (overlay x= expression).

OVERLAY_DIR = BASE_DIR / 'overlays'
OVERLAY_DIR.mkdir(exist_ok=True)


def _font(size, bold=False):
    """Best-effort font loader. Falls back to PIL default."""
    try:
        from PIL import ImageFont
        # Try common Vietnamese-friendly fonts
        for path in [
            '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',  # mac
            '/Library/Fonts/Arial Unicode.ttf',
            'C:/Windows/Fonts/arial.ttf',
            'C:/Windows/Fonts/arialbd.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]:
            if os.path.exists(path):
                if bold and 'bold' not in path.lower() and 'bd' not in path.lower():
                    continue
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    except Exception:
        return None


def render_overlay_static(cell_id, mapping, scores, live_config, output_path=None):
    """Render the static overlay PNG (lower-third + logo + sponsor strip).
    Returns Path to the PNG.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        agent_status['errors'].append('Pillow not installed — cannot render overlay')
        return None

    W, H = 1280, 720
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    p1 = (scores or {}).get('player1_name', 'VĐV 1')
    p2 = (scores or {}).get('player2_name', 'VĐV 2')
    tournament = (live_config or {}).get('tournament_name', '')
    club = (live_config or {}).get('club_name', config.get('club_name', ''))

    # Lower-third banner: bottom 80px, gradient blue
    LT_H = 80
    for y in range(H - LT_H, H):
        alpha = int(220 * (y - (H - LT_H)) / LT_H + 35)
        d.line([(0, y), (W, y)], fill=(13, 35, 71, min(255, alpha)))

    # Player names (left and right of lower-third)
    f_name = _font(34, bold=True)
    f_sub = _font(18)
    if f_name:
        d.text((24, H - LT_H + 14), p1, font=f_name, fill=(255, 255, 255, 255))
        # right-aligned p2: rough estimate of text width
        try:
            w2 = d.textlength(p2, font=f_name)
        except Exception:
            w2 = len(p2) * 20
        d.text((W - 24 - w2, H - LT_H + 14), p2, font=f_name, fill=(255, 255, 255, 255))

    # Tournament + club at top center
    if tournament:
        f_top = _font(22, bold=True)
        try:
            wt = d.textlength(tournament, font=f_top) if f_top else len(tournament) * 14
        except Exception:
            wt = len(tournament) * 14
        # Top bar
        d.rectangle([(0, 0), (W, 50)], fill=(13, 35, 71, 200))
        d.text(((W - wt) // 2, 12), tournament, font=f_top or _font(18), fill=(255, 215, 64, 255))

    if club:
        d.text((24, 60), club, font=f_sub, fill=(180, 200, 230, 220))

    # Score plate placeholder — drawtext from ffmpeg fills here, but we draw a
    # dark backing box so the live text reads cleanly even on bright video.
    BOX_W, BOX_H = 220, 70
    bx, by = (W - BOX_W) // 2, 12
    d.rectangle([(bx, by), (bx + BOX_W, by + BOX_H)], fill=(0, 0, 0, 180), outline=(255, 215, 64, 255), width=2)

    # "VS" hint inside the box; the live "5 - 3" lays on top via ffmpeg drawtext
    f_vs = _font(14)
    if f_vs:
        d.text((bx + BOX_W // 2 - 8, by + BOX_H - 18), 'LIVE', font=f_vs, fill=(255, 82, 82, 255))

    out = Path(output_path) if output_path else (OVERLAY_DIR / f'static_{cell_id}.png')
    img.save(out, 'PNG')
    return out


def render_banner_strip(banners, output_path=None):
    """Render a wide horizontal banner image used for sponsor crawl."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    if not banners:
        return None

    H = 50
    pad = 60
    f = _font(22, bold=True)
    # Compute total width
    total_w = pad
    for b in banners:
        try:
            w = (50 if not f else int(round(ImageDraw.Draw(Image.new('RGBA', (1, 1))).textlength(str(b), font=f)))) + pad
        except Exception:
            w = len(str(b)) * 14 + pad
        total_w += w
    img = Image.new('RGBA', (max(total_w, 1280), H), (13, 35, 71, 200))
    d = ImageDraw.Draw(img)
    x = pad
    for b in banners:
        d.text((x, 12), str(b), font=f or _font(16), fill=(255, 215, 64, 255))
        try:
            w = int(round(d.textlength(str(b), font=f))) if f else len(str(b)) * 14
        except Exception:
            w = len(str(b)) * 14
        x += w + pad

    out = Path(output_path) if output_path else (OVERLAY_DIR / 'banner_strip.png')
    img.save(out, 'PNG')
    return out


def write_score_file(cell_id, scores):
    """Atomically write '5 - 3' to score_{cell_id}.txt for ffmpeg drawtext reload."""
    s1 = scores.get('score1') if scores else 0
    s2 = scores.get('score2') if scores else 0
    text = f'{s1 if s1 is not None else 0} - {s2 if s2 is not None else 0}'
    path = OVERLAY_DIR / f'score_{cell_id}.txt'
    tmp = path.with_suffix('.txt.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)  # atomic on POSIX/Windows
    except Exception as e:
        agent_status['errors'].append(f'write_score {cell_id}: {e}')
    return path


# ============================================================
# LIVE STREAM ORCHESTRATOR (ffmpeg → YouTube RTMP)
# ============================================================
# Per-IP state: a single ffmpeg subprocess that pulls RTSP from the camera
# associated with that scoreboard, composites the static overlay PNG, draws
# live score text, and pushes RTMP to YouTube.
#
# The watchdog thread restarts crashed subprocesses up to MAX_RESTARTS.
# The score writer thread refreshes score_{cell_id}.txt every 2s so drawtext
# reload=1 picks up new scores without restarting ffmpeg.

MAX_RESTARTS = 5
RESTART_BACKOFF_SEC = 4
SCORE_WRITE_INTERVAL_SEC = 2

# {ip: {mapping, rtsp_url, yt_stream_key, proc, started_at, status, restart_count, cell_id, last_error, ffmpeg_log}}
live_streams = {}
live_lock = threading.Lock()


def _find_font_path():
    """Locate a TTF file ffmpeg drawtext can use. Returns path or None."""
    for path in [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/Library/Fonts/Arial Unicode.ttf',
        'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]:
        if os.path.exists(path):
            return path
    return None


FONT_PATH = _find_font_path()


def _ffmpeg_binary():
    """Resolve ffmpeg binary in 3 ordered locations:
    1. PyInstaller --onefile extract dir (sys._MEIPASS) — set when bundled via --add-binary
    2. BASE_DIR (next to the executable) — for users dropping ffmpeg manually
    3. PATH — system-installed ffmpeg
    """
    name = 'ffmpeg.exe' if platform.system() == 'Windows' else 'ffmpeg'
    # 1. Check PyInstaller bundle extract dir
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        cand = Path(meipass) / name
        if cand.exists():
            return str(cand)
    # 2. Check next to executable
    bundled = BASE_DIR / name
    if bundled.exists():
        return str(bundled)
    # 3. Fallback to PATH
    return 'ffmpeg'


def _build_ffmpeg_cmd(rtsp_url, static_png, score_txt, yt_stream_key, banner_png=None):
    """Compose the ffmpeg command line for one stream.

    - Camera audio is replaced by anullsrc (silence) — YouTube requires audio
      track but most bida cameras don't carry meaningful audio.
    - drawtext reload=1 re-reads the score file each frame for live updates.
    - Banner crawl uses overlay x= expression for horizontal scroll.
    """
    target = f'rtmp://a.rtmp.youtube.com/live2/{yt_stream_key}'

    # Inputs: 0=camera, 1=silence audio, 2=static overlay PNG, [3=banner]
    cmd = [
        _ffmpeg_binary(),
        '-hide_banner', '-loglevel', 'warning',
        '-rtsp_transport', 'tcp', '-stimeout', '5000000',
        '-i', rtsp_url,
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
        '-loop', '1', '-framerate', '5', '-i', str(static_png),
    ]

    # Build filter chain
    fc = '[0:v]scale=1280:720,fps=30[v0];' \
         '[v0][2:v]overlay=0:0:shortest=0[v1]'
    if banner_png and os.path.exists(banner_png):
        cmd += ['-loop', '1', '-framerate', '5', '-i', str(banner_png)]
        # Scroll right→left at 80px/sec, anchored to bottom edge above the lower-third
        fc += ";[v1][3:v]overlay=x='W-mod(t*80\\,W+w)':y=H-130[v2]"
        last_v = '[v2]'
    else:
        last_v = '[v1]'

    # Live score drawtext — fontfile is optional but helps on Windows
    font_arg = f"fontfile='{FONT_PATH}':" if FONT_PATH else ''
    fc += (f";{last_v}drawtext=textfile='{score_txt}':reload=1:"
           f"{font_arg}fontsize=56:fontcolor=white:"
           f"x=(w-text_w)/2:y=22:box=0[vout]")

    cmd += [
        '-filter_complex', fc,
        '-map', '[vout]', '-map', '1:a',
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-b:v', '4500k', '-maxrate', '5000k', '-bufsize', '9000k',
        '-pix_fmt', 'yuv420p', '-g', '60', '-keyint_min', '60',
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
        '-f', 'flv', target,
    ]
    return cmd


def _post_live_state(action, mapping, youtube_url=''):
    """Notify ileague.info that a stream went live or stopped, so the scoreboard
    online viewer's YouTube icon links to the right URL.
    """
    if not mapping:
        return
    try:
        url = config.get('ileague_api', 'https://ileague.info/api.php')
        body = {
            'tournament_id': mapping.get('tournament_id'),
            'match_code': mapping.get('match_code'),
            'youtube_url': youtube_url,
            'email': config.get('email', '')
        }
        requests.post(f'{url}?action={action}', json=body, timeout=5)
    except Exception as e:
        agent_status['errors'].append(f'{action} push error: {e}')


def _yt_url_from_key(yt_key):
    """Best-effort viewer URL. Without YouTube API we can't resolve the watch URL,
    so we publish the channel's /live convenience URL only when a video URL was
    supplied externally. For now return an empty placeholder; UI shows youtube.com/live2 hint.
    """
    # Real watch URL requires YouTube Data API — out of scope. UI will show
    # whatever url ileague_admin pasted. For now, leave empty.
    return ''


def start_stream(ip):
    """Start an ffmpeg push stream for the scoreboard at IP.
    Requires: cameras_db[ip].rtsp_url, mappings[ip], live_config.yt_stream_key.
    Returns dict { ok, error?, stream }.
    """
    with live_lock:
        if ip in live_streams and live_streams[ip].get('proc'):
            proc = live_streams[ip]['proc']
            if proc.poll() is None:
                return {'ok': False, 'error': 'already_running', 'stream': live_streams[ip]}

        cam = cameras_db.get(ip) or {}
        rtsp = cam.get('rtsp_url')
        if not rtsp:
            return {'ok': False, 'error': 'no_rtsp_for_ip'}

        mapping = mappings.get(ip)
        if not mapping:
            return {'ok': False, 'error': 'no_mapping_for_ip'}

        # Fetch live config for the tournament (gives us yt_stream_key + assets)
        try:
            url = config.get('ileague_api', 'https://ileague.info/api.php')
            r = requests.get(url, params={'action': 'live_config', 'id': mapping['tournament_id']}, timeout=8)
            if r.status_code == 402:
                return {'ok': False, 'error': 'pro_required', 'detail': r.json()}
            if r.status_code != 200:
                return {'ok': False, 'error': f'live_config http {r.status_code}'}
            live_cfg = r.json()
        except Exception as e:
            return {'ok': False, 'error': f'live_config fetch: {e}'}

        yt_key = live_cfg.get('yt_stream_key', '').strip()
        if not yt_key:
            return {'ok': False, 'error': 'no_yt_stream_key', 'detail': 'Cấu hình YouTube stream key cho giải đấu trước'}

        cell_id = mapping.get('match_code', ip).replace('/', '_').replace(' ', '_')
        scores = (devices.get(ip) or {}).get('last_score') or {}

        # Render overlays
        static_png = render_overlay_static(cell_id, mapping, scores, live_cfg)
        banner_png = render_banner_strip(live_cfg.get('overlay_banners') or [])
        score_txt = write_score_file(cell_id, scores)

        if not static_png:
            return {'ok': False, 'error': 'overlay_render_failed (Pillow installed?)'}

        cmd = _build_ffmpeg_cmd(rtsp, static_png, score_txt, yt_key, banner_png)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=(platform.system() != 'Windows')
            )
        except FileNotFoundError:
            return {'ok': False, 'error': 'ffmpeg_not_found',
                    'detail': 'Cài ffmpeg và đảm bảo có trên PATH (hoặc bundle vào thư mục agent)'}
        except Exception as e:
            return {'ok': False, 'error': f'ffmpeg spawn: {e}'}

        stream_state = {
            'ip': ip,
            'cell_id': cell_id,
            'mapping': dict(mapping),
            'rtsp_url': rtsp,
            'yt_stream_key': yt_key,
            'tournament_id': mapping.get('tournament_id'),
            'match_code': mapping.get('match_code'),
            'proc': proc,
            'started_at': datetime.now().isoformat(),
            'status': 'starting',
            'restart_count': 0,
            'last_error': None,
            'ffmpeg_log': []  # last N stderr lines
        }
        live_streams[ip] = stream_state

    # Notify ileague.info to set YouTube link on the scoreboard online
    _post_live_state('live_started', mapping, _yt_url_from_key(yt_key))

    return {'ok': True, 'stream': _stream_dict_safe(stream_state)}


def stop_stream(ip, post_to_server=True):
    """Stop a running stream cleanly."""
    with live_lock:
        st = live_streams.get(ip)
        if not st:
            return {'ok': False, 'error': 'not_running'}
        proc = st.get('proc')
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try: proc.kill()
                except Exception: pass
        st['status'] = 'stopped'
        st['stopped_at'] = datetime.now().isoformat()
        mapping = st.get('mapping')
        del live_streams[ip]

    if post_to_server and mapping:
        _post_live_state('live_stopped', mapping, '')
    return {'ok': True}


def _stream_dict_safe(st):
    """Strip non-serializable fields (Popen) for JSON output."""
    if not st:
        return None
    proc = st.get('proc')
    return {
        'ip': st['ip'],
        'cell_id': st.get('cell_id'),
        'tournament_id': st.get('tournament_id'),
        'match_code': st.get('match_code'),
        'rtsp_url': st.get('rtsp_url'),
        'started_at': st.get('started_at'),
        'status': 'running' if (proc and proc.poll() is None) else st.get('status', 'dead'),
        'restart_count': st.get('restart_count', 0),
        'last_error': st.get('last_error'),
        'pid': proc.pid if proc else None,
        'ffmpeg_log_tail': (st.get('ffmpeg_log') or [])[-5:]
    }


def watchdog_loop():
    """Restart crashed ffmpeg processes; refresh score files every 2s."""
    last_score_write = 0
    while True:
        try:
            now = time.time()
            with live_lock:
                ips = list(live_streams.keys())

            # Update score files for all live streams every SCORE_WRITE_INTERVAL_SEC
            if now - last_score_write >= SCORE_WRITE_INTERVAL_SEC:
                last_score_write = now
                for ip in ips:
                    st = live_streams.get(ip)
                    if not st:
                        continue
                    scores = (devices.get(ip) or {}).get('last_score') or {}
                    write_score_file(st['cell_id'], scores)

            # Watchdog: restart died streams
            for ip in ips:
                st = live_streams.get(ip)
                if not st:
                    continue
                proc = st.get('proc')
                if proc is None:
                    continue
                ret = proc.poll()
                if ret is None:
                    if st.get('status') == 'starting':
                        st['status'] = 'running'
                    continue

                # Process died — capture stderr tail
                try:
                    err = (proc.stderr.read() or b'').decode('utf-8', errors='ignore')[-1500:]
                except Exception:
                    err = ''
                st['ffmpeg_log'].append(f'[{datetime.now().isoformat()}] exit={ret}\n{err}')
                st['ffmpeg_log'] = st['ffmpeg_log'][-10:]
                st['last_error'] = f'ffmpeg exit {ret}'
                st['status'] = 'crashed'

                if st['restart_count'] >= MAX_RESTARTS:
                    print(f'⛔ {ip} stream exceeded MAX_RESTARTS — giving up')
                    stop_stream(ip, post_to_server=True)
                    continue

                # Backoff and restart
                st['restart_count'] += 1
                time.sleep(RESTART_BACKOFF_SEC)
                print(f'🔄 Restarting stream {ip} (attempt {st["restart_count"]}/{MAX_RESTARTS})')
                # Reuse same params
                cmd = _build_ffmpeg_cmd(
                    st['rtsp_url'],
                    OVERLAY_DIR / f'static_{st["cell_id"]}.png',
                    OVERLAY_DIR / f'score_{st["cell_id"]}.txt',
                    st['yt_stream_key'],
                    OVERLAY_DIR / 'banner_strip.png'
                )
                try:
                    new_proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        start_new_session=(platform.system() != 'Windows')
                    )
                    st['proc'] = new_proc
                    st['status'] = 'starting'
                except Exception as e:
                    st['last_error'] = f'restart spawn: {e}'

        except Exception as e:
            agent_status['errors'].append(f'watchdog: {e}')
        time.sleep(1)


# ============================================================
# MAIN SCAN LOOP
# ============================================================

def scan_loop():
    """Background loop: scan devices, read scores, push to iLeague."""
    agent_status['running'] = True
    last_network_scan = 0

    while agent_status['running']:
        now = time.time()

        # Periodic network scan
        if now - last_network_scan > config.get('network_scan_interval', 30):
            try:
                found = scan_network()
                for ip in found:
                    if ip not in devices:
                        devices[ip] = {
                            'ip': ip,
                            'status': 'connected',
                            'last_score': None,
                            'last_update': None,
                            'found_at': datetime.now().isoformat()
                        }
                        print(f'✅ Found scoreboard: {ip}')
                        # Probe camera RTSP in background (don't block scan loop)
                        threading.Thread(
                            target=ensure_camera_for, args=(ip,), daemon=True
                        ).start()
                agent_status['devices_found'] = len(get_active_devices())
                last_network_scan = now
            except Exception as e:
                agent_status['errors'].append(f'Scan error: {e}')

        # Poll scores from all connected devices
        for ip, dev in list(devices.items()):
            try:
                scores = get_scoreboard_scores(ip)
                if scores:
                    prev = dev.get('last_score')
                    dev['last_score'] = scores
                    dev['last_update'] = datetime.now().isoformat()
                    dev['status'] = 'connected'

                    # Try auto-mapping on every scan when scoreboard players change.
                    # Cheap: cached tournament_matches + name normalize. Skips if already
                    # mapped manually or already correctly auto-mapped.
                    name_changed = (not prev
                                    or prev.get('player1_name') != scores.get('player1_name')
                                    or prev.get('player2_name') != scores.get('player2_name'))
                    if name_changed:
                        auto_map_by_names(ip, scores)

                    mapping = mappings.get(ip)
                    pushed = False
                    if mapping and (not prev or prev.get('score1') != scores.get('score1') or prev.get('score2') != scores.get('score2')):
                        pushed = push_score_to_ileague(mapping, scores)

                    if not prev or prev.get('score1') != scores.get('score1') or prev.get('score2') != scores.get('score2'):
                        score_log.append({
                            'timestamp': datetime.now().isoformat(),
                            'ip': ip,
                            'scores': f"{scores['score1']}-{scores['score2']}",
                            'pushed': pushed,
                            'mapped': mapping is not None
                        })
                        if len(score_log) > 500:
                            score_log.pop(0)
                else:
                    dev['status'] = 'no_game'
            except Exception as e:
                dev['status'] = 'error'
                agent_status['errors'].append(f'Poll error {ip}: {e}')
                if len(agent_status['errors']) > 50:
                    agent_status['errors'].pop(0)

        agent_status['last_scan'] = datetime.now().isoformat()
        agent_status['devices_found'] = len(get_active_devices())
        push_devices_to_server()
        time.sleep(config.get('scan_interval', 3))


# ============================================================
# AUTO-START (Windows + Mac)
# ============================================================

def get_exe_path():
    """Get path to current executable."""
    if IS_FROZEN:
        return sys.executable
    return os.path.abspath(__file__)


def set_autostart(enable=True):
    """Enable/disable auto-start on Windows/Mac."""
    system = platform.system()

    if system == 'Darwin':
        # macOS: LaunchAgent plist
        try:
            plist_dir = Path.home() / 'Library' / 'LaunchAgents'
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_file = plist_dir / 'info.ileague.hub.plist'
            if enable:
                exe = get_exe_path()
                if IS_FROZEN:
                    program_args = f'<string>{exe}</string>\n      <string>--background</string>'
                else:
                    program_args = f'<string>{sys.executable}</string>\n      <string>{exe}</string>\n      <string>--background</string>'
                plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>info.ileague.hub</string>
    <key>ProgramArguments</key>
    <array>
      {program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>{BASE_DIR}</string>
</dict>
</plist>'''
                plist_file.write_text(plist_content)
                subprocess.run(['launchctl', 'load', str(plist_file)], capture_output=True)
            else:
                if plist_file.exists():
                    subprocess.run(['launchctl', 'unload', str(plist_file)], capture_output=True)
                    plist_file.unlink()
            config['auto_start'] = enable
            save_config(config)
            return True
        except Exception as e:
            print(f'Mac auto-start error: {e}')
            return False

    elif system == 'Windows':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'Software\Microsoft\Windows\CurrentVersion\Run',
                                 0, winreg.KEY_SET_VALUE)
            if enable:
                exe = get_exe_path()
                winreg.SetValueEx(key, 'iLeagueHub', 0, winreg.REG_SZ, f'"{exe}" --background')
            else:
                try:
                    winreg.DeleteValue(key, 'iLeagueHub')
                except:
                    pass
            winreg.CloseKey(key)
            config['auto_start'] = enable
            save_config(config)
            return True
        except Exception as e:
            print(f'Auto-start error: {e}')
            # Fallback: Startup folder
            try:
                startup = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
                shortcut = startup / 'iLeagueHub.bat'
                if enable:
                    exe = get_exe_path()
                    shortcut.write_text(f'@echo off\nstart "" "{exe}" --background\n')
                else:
                    if shortcut.exists():
                        shortcut.unlink()
                config['auto_start'] = enable
                save_config(config)
                return True
            except:
                return False

    else:
        print(f'Auto-start not supported on {system}')
        return False


# ============================================================
# FLASK WEB API + DASHBOARD
# ============================================================

app = Flask(__name__, static_folder='static')
CORS(app)


@app.route('/')
def index():
    if not config.get('setup_done'):
        return redirect('/setup')
    return send_from_directory('static', 'index.html')


@app.route('/setup')
def setup_page():
    client_id = '536953137943-dg5stfoh8dkjld33afe6377u2idooj6i.apps.googleusercontent.com'
    return f'''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iLeague Hub — Setup</title>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;600;700;800&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Be Vietnam Pro',sans-serif;background:#0a0e1a;color:#e0e0e0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
.setup-box{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);border-radius:16px;padding:32px;max-width:440px;width:100%;text-align:center}}
h1{{color:#42a5f5;font-size:22px;margin-bottom:4px}}
.sub{{color:#666;font-size:12px;margin-bottom:24px}}
label{{display:block;font-size:12px;color:#90caf9;margin:16px 0 6px;font-weight:600;text-align:left}}
input{{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#e0e0e0;font-size:14px;font-family:inherit}}
input:focus{{outline:2px solid #42a5f5;border-color:transparent}}
.btn{{display:block;width:100%;padding:12px;border:none;border-radius:8px;background:#1e88e5;color:#fff;font-size:14px;font-weight:700;cursor:pointer;margin-top:20px;font-family:inherit}}
.btn:hover{{background:#1565c0}}
.btn:disabled{{background:#333;cursor:not-allowed}}
.note{{font-size:10px;color:#555;margin-top:12px}}
.version{{font-size:10px;color:#333;margin-top:8px}}
.google-wrap{{display:flex;justify-content:center;margin:16px 0}}
.user-info{{background:rgba(76,175,80,0.15);border:1px solid rgba(76,175,80,0.3);border-radius:8px;padding:10px 14px;margin:12px 0;text-align:left;font-size:13px;display:none}}
.user-info .name{{color:#66bb6a;font-weight:700}}
.user-info .email{{color:#90caf9;font-size:12px}}
.help-link{{color:#64b5f6;font-size:11px;text-decoration:none;display:inline-block;margin-top:14px}}
.help-link:hover{{color:#90caf9;text-decoration:underline}}
#step2{{display:none}}
</style></head><body>
<div class="setup-box">
<h1>iLeague Hub</h1>
<p class="sub">Cài đặt lần đầu — kết nối bảng điểm với iLeague</p>

<div id="step1">
<p style="color:#90caf9;font-size:13px;margin-bottom:12px;font-weight:600">Bước 1: Đăng nhập Google</p>

<div class="google-wrap">
<div id="g_id_onload"
     data-client_id="{client_id}"
     data-callback="onGoogleSignIn"
     data-auto_prompt="false">
</div>
<div class="g_id_signin"
     data-type="standard"
     data-size="large"
     data-theme="filled_blue"
     data-text="signin_with"
     data-shape="rectangular">
</div>
</div>
<p style="font-size:10px;color:#999;margin-top:8px">Cùng Gmail dùng cho iLeague trên web</p>
<a class="help-link" href="mailto:trandinhvu@gmail.com?subject=iLeague%20Hub%20-%20Lỗi%20đăng%20nhập">Lỗi đăng nhập? Liên hệ hỗ trợ</a>
</div>

<div class="user-info" id="userInfo">
<div class="name" id="userName"></div>
<div class="email" id="userEmail"></div>
</div>

<div id="step2">
<p style="color:#90caf9;font-size:13px;margin-bottom:8px;font-weight:600">Bước 2: Tên CLB</p>
<label>Tên CLB / Quán bida</label>
<input id="clubName" placeholder="VD: CLB Bida Đăng Phú" value="{config.get('club_name','')}" required>
<button class="btn" id="startBtn" onclick="doSetup()">Bắt đầu quét bảng điểm</button>
</div>

<p class="note">Liên kết bảng điểm tại CLB với tài khoản iLeague</p>
<p class="version">iLeague Hub v{VERSION}</p>
</div>

<script>
var _email = '';
var _name = '';

function onGoogleSignIn(response) {{
    var parts = response.credential.split('.');
    var payload = JSON.parse(atob(parts[1].replace(/-/g,'+').replace(/_/g,'/')));
    _email = payload.email || '';
    _name = payload.name || '';

    document.getElementById('userName').textContent = _name;
    document.getElementById('userEmail').textContent = _email;
    document.getElementById('userInfo').style.display = 'block';
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
}}

function doSetup() {{
    var club = document.getElementById('clubName').value.trim();
    if (!club) {{ alert('Nhập tên CLB'); return; }}
    if (!_email) {{ alert('Đăng nhập Google trước'); return; }}

    fetch('/api/setup', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{club_name: club, email: _email, name: _name}})
    }}).then(function() {{
        window.location.href = '/';
    }});
}}
</script>
</div></body></html>'''


@app.route('/api/setup', methods=['POST'])
def api_setup():
    data = request.json or {}
    club_name = data.get('club_name', request.form.get('club_name', '')).strip()
    email = data.get('email', request.form.get('email', '')).strip()
    if not club_name or not email:
        return jsonify({'ok': False, 'error': 'Missing club_name or email'}), 400
    config['club_name'] = club_name
    config['email'] = email
    config['user_name'] = data.get('name', '')
    config['setup_done'] = True
    save_config(config)
    return jsonify({'ok': True})


@app.route('/api/status')
def api_status():
    return jsonify({
        'running': agent_status['running'],
        'last_scan': agent_status['last_scan'],
        'devices_found': agent_status['devices_found'],
        'devices_total': len(devices),
        'devices_mapped': len(mappings),
        'club_name': config.get('club_name', ''),
        'email': config.get('email', ''),
        'auto_start': config.get('auto_start', False),
        'version': VERSION,
        'recent_errors': agent_status['errors'][-5:],
        'pro': {
            'active': pro_status['active'],
            'in_grace': pro_status['in_grace'],
            'plan': pro_status['plan'],
            'days_left': pro_status['days_left'],
            'expires_at': pro_status['expires_at']
        }
    })


@app.route('/api/pro_status')
def api_pro_status():
    """Return cached Pro license status."""
    return jsonify(pro_status)


def fetch_pro_status():
    """Query ileague.info to refresh Pro license status. Cache in pro_status global."""
    email = config.get('email', '').strip().lower()
    if not email:
        return
    try:
        url = config.get('ileague_api', 'https://ileague.info/api.php')
        r = requests.get(url, params={'action': 'check_pro', 'email': email}, timeout=8)
        if r.status_code != 200:
            return
        d = r.json()
        pro_status['active'] = bool(d.get('active'))
        pro_status['in_grace'] = bool(d.get('in_grace'))
        pro_status['plan'] = d.get('plan')
        pro_status['days_left'] = d.get('days_left', 0)
        pro_status['expires_at'] = d.get('expires_at')
        pro_status['last_check'] = datetime.now().isoformat()
    except Exception as e:
        agent_status['errors'].append(f'Pro check error: {e}')


def pro_status_loop():
    """Background: refresh Pro status every 5 minutes."""
    while True:
        fetch_pro_status()
        time.sleep(300)


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        data = request.json or {}
        for k in ['club_name', 'email', 'active_threshold_hours', 'scan_interval']:
            if k in data:
                config[k] = data[k]
        save_config(config)
        return jsonify({'ok': True})
    return jsonify({k: v for k, v in config.items()})


@app.route('/api/devices')
def api_devices():
    # Only return active devices
    active = get_active_devices()
    result = []
    for ip, dev in active.items():
        d = dict(dev)
        d['mapped'] = ip in mappings
        d['mapping'] = mappings.get(ip)
        result.append(d)
    return jsonify(result)


@app.route('/api/devices/all')
def api_devices_all():
    """Return ALL devices including inactive (for debug)."""
    result = []
    for ip, dev in devices.items():
        d = dict(dev)
        d['mapped'] = ip in mappings
        d['mapping'] = mappings.get(ip)
        d['is_active'] = is_active_device(dev)
        result.append(d)
    return jsonify(result)


@app.route('/api/scan', methods=['POST'])
def api_scan():
    found = scan_network()
    for ip in found:
        if ip not in devices:
            scores = get_scoreboard_scores(ip)
            devices[ip] = {
                'ip': ip,
                'status': 'connected' if scores else 'no_game',
                'last_score': scores,
                'last_update': datetime.now().isoformat() if scores else None,
                'found_at': datetime.now().isoformat()
            }
    active = get_active_devices()
    return jsonify({'ok': True, 'found': len(found), 'active': len(active), 'total': len(devices)})


@app.route('/api/map', methods=['POST'])
def api_map():
    data = request.json
    ip = data.get('ip')
    tournament_id = data.get('tournament_id')
    match_code = data.get('match_code')
    if not ip or not tournament_id or not match_code:
        return jsonify({'ok': False, 'error': 'Missing ip, tournament_id, or match_code'}), 400
    mappings[ip] = {'tournament_id': tournament_id, 'match_code': match_code}
    return jsonify({'ok': True})


@app.route('/api/unmap', methods=['POST'])
def api_unmap():
    ip = request.json.get('ip')
    if ip in mappings:
        del mappings[ip]
    return jsonify({'ok': True})


@app.route('/api/active_tournament', methods=['GET', 'POST'])
def api_active_tournament():
    """Get or set the tournament that auto-map should match against."""
    global active_tournament_id
    if request.method == 'POST':
        data = request.json or {}
        tid = (data.get('tournament_id') or '').strip() or None
        active_tournament_id = tid
        config['active_tournament_id'] = tid
        save_config(config)
        # Also clear stale auto-mappings from a previous tournament
        for ip in list(mappings.keys()):
            m = mappings[ip]
            if m.get('auto') and m.get('tournament_id') != tid:
                del mappings[ip]
        # Pre-fetch matches so first auto-map doesn't pay the request cost
        if tid:
            _fetch_tournament_matches(tid, force=True)
        return jsonify({
            'ok': True,
            'tournament_id': tid,
            'tournament_name': tournament_matches_cache.get('tournament_name', ''),
            'matches_count': len(tournament_matches_cache.get('matches', []))
        })
    return jsonify({
        'tournament_id': active_tournament_id,
        'tournament_name': tournament_matches_cache.get('tournament_name', ''),
        'matches': tournament_matches_cache.get('matches', []),
        'fetched_at': tournament_matches_cache.get('fetched_at')
    })


@app.route('/api/tournament_matches')
def api_tournament_matches():
    """Return cached matches for the active tournament (or refetch on demand)."""
    force = request.args.get('refresh') in ('1', 'true', 'yes')
    matches = _fetch_tournament_matches(active_tournament_id, force=force) if active_tournament_id else []
    return jsonify({
        'tournament_id': active_tournament_id,
        'tournament_name': tournament_matches_cache.get('tournament_name', ''),
        'matches': matches,
        'fetched_at': tournament_matches_cache.get('fetched_at')
    })


@app.route('/api/automap', methods=['POST'])
def api_automap():
    """Force a one-shot auto-map sweep across all active devices."""
    if not active_tournament_id:
        return jsonify({'ok': False, 'error': 'Chưa chọn giải đấu (active_tournament_id)'}), 400
    _fetch_tournament_matches(active_tournament_id, force=True)
    mapped, unmapped = 0, 0
    for ip, dev in get_active_devices().items():
        scores = dev.get('last_score')
        if not scores:
            continue
        if auto_map_by_names(ip, scores):
            mapped += 1
        elif ip not in mappings:
            unmapped += 1
    return jsonify({
        'ok': True,
        'mapped': mapped,
        'unmapped': unmapped,
        'total_active': len(get_active_devices())
    })


@app.route('/api/auto_map_toggle', methods=['POST'])
def api_auto_map_toggle():
    """Enable/disable auto-mapping persistence."""
    enabled = bool((request.json or {}).get('enabled', True))
    config['auto_map_enabled'] = enabled
    save_config(config)
    return jsonify({'ok': True, 'auto_map_enabled': enabled})


@app.route('/api/cameras')
def api_cameras_list():
    """Return all known camera entries, joined with current device state."""
    out = []
    for ip in sorted(set(list(devices.keys()) + list(cameras_db.keys()))):
        cam = cameras_db.get(ip, {})
        dev = devices.get(ip, {})
        out.append({
            'ip': ip,
            'rtsp_url': cam.get('rtsp_url'),
            'source': cam.get('source'),  # 'discovered' | 'manual' | 'cleared' | None
            'last_probe': cam.get('last_probe'),
            'set_at': cam.get('set_at'),
            'device_status': dev.get('status'),
            'has_active_game': bool(dev.get('last_score'))
        })
    return jsonify(out)


@app.route('/api/cameras/<ip>')
def api_cameras_detail(ip):
    """Return single camera entry incl. probe log for diagnostics."""
    cam = cameras_db.get(ip)
    if not cam:
        return jsonify({'ip': ip, 'rtsp_url': None, 'probe_log': []})
    return jsonify(cam)


@app.route('/api/cameras/probe', methods=['POST'])
def api_cameras_probe():
    """Re-probe one IP or all known scoreboards."""
    data = request.json or {}
    ip = data.get('ip')
    if ip:
        cam = ensure_camera_for(ip, force_reprobe=True)
        return jsonify({'ok': True, 'camera': cam})
    # Probe-all
    results = []
    for sip in list(devices.keys()):
        cam = ensure_camera_for(sip, force_reprobe=True)
        results.append({'ip': sip, 'rtsp_url': cam.get('rtsp_url'), 'source': cam.get('source')})
    return jsonify({'ok': True, 'results': results})


@app.route('/api/cameras', methods=['POST'])
def api_cameras_set():
    """Manually set RTSP URL for an IP. Empty URL clears it."""
    data = request.json or {}
    ip = (data.get('ip') or '').strip()
    rtsp_url = (data.get('rtsp_url') or '').strip()
    if not ip:
        return jsonify({'ok': False, 'error': 'Missing ip'}), 400
    cam = set_camera_manual(ip, rtsp_url)
    return jsonify({'ok': True, 'camera': cam})


@app.route('/api/cameras/<ip>', methods=['DELETE'])
def api_cameras_delete(ip):
    """Delete a camera entry entirely."""
    if ip in cameras_db:
        del cameras_db[ip]
        save_cameras()
    return jsonify({'ok': True})


@app.route('/api/live/status')
def api_live_status():
    """List all currently active streams."""
    with live_lock:
        out = [_stream_dict_safe(st) for st in live_streams.values()]
    return jsonify({'streams': out, 'count': len(out)})


@app.route('/api/live/start', methods=['POST'])
def api_live_start():
    ip = (request.json or {}).get('ip')
    if not ip:
        return jsonify({'ok': False, 'error': 'Missing ip'}), 400
    return jsonify(start_stream(ip))


@app.route('/api/live/stop', methods=['POST'])
def api_live_stop():
    ip = (request.json or {}).get('ip')
    if not ip:
        return jsonify({'ok': False, 'error': 'Missing ip'}), 400
    return jsonify(stop_stream(ip))


@app.route('/api/live/start_all', methods=['POST'])
def api_live_start_all():
    """Start streams for all eligible scoreboards (mapped + has RTSP)."""
    started, skipped = [], []
    for ip in list(get_active_devices().keys()):
        if ip in live_streams:
            skipped.append({'ip': ip, 'reason': 'already_running'})
            continue
        if ip not in mappings:
            skipped.append({'ip': ip, 'reason': 'unmapped'})
            continue
        if not (cameras_db.get(ip) or {}).get('rtsp_url'):
            skipped.append({'ip': ip, 'reason': 'no_rtsp'})
            continue
        res = start_stream(ip)
        if res.get('ok'):
            started.append(ip)
        else:
            skipped.append({'ip': ip, 'reason': res.get('error')})
    return jsonify({'ok': True, 'started': started, 'skipped': skipped})


@app.route('/api/live/stop_all', methods=['POST'])
def api_live_stop_all():
    ips = list(live_streams.keys())
    for ip in ips:
        stop_stream(ip)
    return jsonify({'ok': True, 'stopped': ips})


@app.route('/api/live_config/<tid>')
def api_live_config_proxy(tid):
    """Proxy for ileague.info live_config — useful for UI to read yt key state."""
    try:
        url = config.get('ileague_api', 'https://ileague.info/api.php')
        r = requests.get(url, params={'action': 'live_config', 'id': tid}, timeout=8)
        return (r.text, r.status_code, {'Content-Type': 'application/json'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/live_config/<tid>', methods=['POST'])
def api_live_config_save(tid):
    """Save livestream config to ileague.info."""
    try:
        url = config.get('ileague_api', 'https://ileague.info/api.php')
        body = dict(request.json or {})
        body['tournament_id'] = tid
        r = requests.post(f'{url}?action=live_config_save', json=body, timeout=8)
        return (r.text, r.status_code, {'Content-Type': 'application/json'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scores')
def api_scores():
    return jsonify(score_log[-50:])


@app.route('/api/autostart', methods=['POST'])
def api_autostart():
    enable = request.json.get('enable', True)
    ok = set_autostart(enable)
    return jsonify({'ok': ok, 'auto_start': config.get('auto_start', False)})


# ============================================================
# SYSTEM TRAY (Windows/Mac)
# ============================================================

def run_tray():
    """Run system tray icon in background."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print('⚠️  pystray/Pillow not installed — running without system tray')
        print('   Install: pip install pystray Pillow')
        return

    def create_icon():
        """Create a simple icon for the tray."""
        # Create a 64x64 blue circle icon
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(30, 136, 229, 255))
        draw.text((20, 18), 'iL', fill=(255, 255, 255, 255))
        return img

    def open_dashboard(icon, item):
        webbrowser.open('http://localhost:5050')

    def show_status(icon, item):
        active = len(get_active_devices())
        total = len(devices)
        club = config.get('club_name', '?')
        # Update tooltip
        icon.title = f'iLeague Hub — {club}\n{active} bảng điểm active / {total} total'

    def quit_app(icon, item):
        agent_status['running'] = False
        icon.stop()
        os._exit(0)

    def toggle_autostart(icon, item):
        current = config.get('auto_start', False)
        set_autostart(not current)

    def open_pro_page(icon, item):
        webbrowser.open('https://ileague.info/p')

    def pro_label():
        if pro_status['active'] is None:
            return '⏳ Pro: kiểm tra...'
        if pro_status['active'] and not pro_status['in_grace']:
            plan = (pro_status['plan'] or '').upper()
            return f"⭐ Pro {plan} · còn {pro_status['days_left']}d"
        if pro_status['in_grace']:
            return '⏰ Pro hết hạn — gia hạn'
        return '🔒 Free · Click để nâng cấp Pro'

    def pro_clickable():
        # Click to open upgrade page when not active; disabled when active.
        return not (pro_status['active'] and not pro_status['in_grace'])

    club = config.get('club_name', 'iLeague Hub')
    menu = pystray.Menu(
        pystray.MenuItem(f'iLeague Hub v{VERSION}', None, enabled=False),
        pystray.MenuItem(f'CLB: {club}', None, enabled=False),
        pystray.MenuItem(lambda item: pro_label(), open_pro_page, enabled=lambda item: pro_clickable()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Mở Dashboard', open_dashboard, default=True),
        pystray.MenuItem('Cập nhật Status', show_status),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            'Tự khởi động cùng Windows',
            toggle_autostart,
            checked=lambda item: config.get('auto_start', False)
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Thoát', quit_app)
    )

    icon = pystray.Icon('ileague_hub', create_icon(), f'iLeague Hub — {club}', menu)

    # Tooltip refresher: keeps Pro days_left visible in tray hover
    def refresh_tooltip_loop():
        while True:
            try:
                active = len(get_active_devices())
                tip = f'iLeague Hub — {club}\n{active} bảng điểm active\n{pro_label()}'
                icon.title = tip
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=refresh_tooltip_loop, daemon=True).start()
    icon.run()


# ============================================================
# MAIN
# ============================================================

def main():
    background = '--background' in sys.argv

    # Restore persistent state
    load_cameras()

    print('=' * 50)
    print(f'  iLeague Hub Agent v{VERSION}')
    print(f'  Dashboard: http://localhost:5050')
    print(f'  CLB: {config.get("club_name", "(chưa setup)")}')
    print(f'  Email: {config.get("email", "(chưa setup)")}')
    print(f'  Cameras: {len(cameras_db)} cached')
    print('=' * 50)

    # Start scan thread
    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()
    print('🔍 Scanning network for scoreboards...')

    # Start ffmpeg watchdog (handles score file refresh + crash recovery)
    threading.Thread(target=watchdog_loop, daemon=True).start()

    # Start Pro status refresh loop (every 5 min). First check is immediate so
    # the tray/dashboard show the right state on first hover.
    fetch_pro_status()
    threading.Thread(target=pro_status_loop, daemon=True).start()

    # Start system tray in background thread
    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()

    # Open browser (unless running in background mode)
    if not background and config.get('setup_done'):
        threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5050')).start()
    elif not background:
        threading.Timer(1.5, lambda: webbrowser.open('http://localhost:5050/setup')).start()

    # Start Flask (main thread)
    app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
