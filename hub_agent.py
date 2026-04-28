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
import sys
import subprocess
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
        'setup_done': False
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
            'status': 'end' if scores.get('is_end') else 'live',
            'email': config.get('email', '')
        }
        resp = requests.post(
            f'{config["ileague_api"]}?action=score_update',
            json=payload, timeout=5
        )
        result = resp.json() if resp.status_code == 200 else {}
        return result.get('updated', False)
    except:
        return False


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
.btn-secondary{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.15)}}
.btn-secondary:hover{{background:rgba(255,255,255,0.1)}}
.btn:disabled{{background:#333;cursor:not-allowed}}
.note{{font-size:10px;color:#555;margin-top:12px}}
.version{{font-size:10px;color:#333;margin-top:8px}}
.google-wrap{{display:flex;justify-content:center;margin:16px 0}}
.divider{{display:flex;align-items:center;gap:10px;margin:16px 0;color:#444;font-size:11px}}
.divider::before,.divider::after{{content:'';flex:1;height:1px;background:rgba(255,255,255,0.08)}}
.user-info{{background:rgba(76,175,80,0.15);border:1px solid rgba(76,175,80,0.3);border-radius:8px;padding:10px 14px;margin:12px 0;text-align:left;font-size:13px;display:none}}
.user-info .name{{color:#66bb6a;font-weight:700}}
.user-info .email{{color:#90caf9;font-size:12px}}
.toggle-link{{color:#64b5f6;font-size:12px;cursor:pointer;text-decoration:underline;display:inline-block;margin-top:14px}}
.toggle-link:hover{{color:#90caf9}}
#step2{{display:none}}
#manualForm{{display:none}}
</style></head><body>
<div class="setup-box">
<h1>iLeague Hub</h1>
<p class="sub">Cài đặt lần đầu — kết nối bảng điểm với iLeague</p>

<div id="step1">
<p style="color:#90caf9;font-size:13px;margin-bottom:12px;font-weight:600">Bước 1: Đăng nhập</p>

<div id="googleForm">
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
<a class="toggle-link" onclick="toggleManual(true)">Hoặc nhập Gmail thủ công →</a>
</div>

<div id="manualForm">
<label>Gmail của bạn</label>
<input id="manualEmail" type="email" placeholder="VD: chubida@gmail.com" required>
<p style="font-size:10px;color:#999;margin-top:6px;text-align:left">Phải là Gmail đã/sẽ dùng để login iLeague trên web</p>
<button class="btn btn-secondary" onclick="useManualEmail()">Tiếp tục</button>
<a class="toggle-link" onclick="toggleManual(false)">← Đăng nhập bằng Google</a>
</div>
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

function toggleManual(show) {{
    document.getElementById('googleForm').style.display = show ? 'none' : 'block';
    document.getElementById('manualForm').style.display = show ? 'block' : 'none';
}}

function onGoogleSignIn(response) {{
    var parts = response.credential.split('.');
    var payload = JSON.parse(atob(parts[1].replace(/-/g,'+').replace(/_/g,'/')));
    _email = payload.email || '';
    _name = payload.name || '';
    showStep2(_email, _name);
}}

function useManualEmail() {{
    var email = document.getElementById('manualEmail').value.trim().toLowerCase();
    if (!email || email.indexOf('@') === -1) {{ alert('Nhập đúng định dạng email'); return; }}
    _email = email;
    _name = email.split('@')[0];
    showStep2(_email, _name);
}}

function showStep2(email, name) {{
    document.getElementById('userName').textContent = name;
    document.getElementById('userEmail').textContent = email;
    document.getElementById('userInfo').style.display = 'block';
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
}}

function doSetup() {{
    var club = document.getElementById('clubName').value.trim();
    if (!club) {{ alert('Nhập tên CLB'); return; }}
    if (!_email) {{ alert('Đăng nhập / nhập email trước'); return; }}

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
        'recent_errors': agent_status['errors'][-5:]
    })


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

    club = config.get('club_name', 'iLeague Hub')
    menu = pystray.Menu(
        pystray.MenuItem(f'iLeague Hub v{VERSION}', None, enabled=False),
        pystray.MenuItem(f'CLB: {club}', None, enabled=False),
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
    icon.run()


# ============================================================
# MAIN
# ============================================================

def main():
    background = '--background' in sys.argv

    print('=' * 50)
    print(f'  iLeague Hub Agent v{VERSION}')
    print(f'  Dashboard: http://localhost:5050')
    print(f'  CLB: {config.get("club_name", "(chưa setup)")}')
    print(f'  Email: {config.get("email", "(chưa setup)")}')
    print('=' * 50)

    # Start scan thread
    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()
    print('🔍 Scanning network for scoreboards...')

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
