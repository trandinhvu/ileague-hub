# iLeague Hub Agent

Quét bảng điểm Hello/9Score/Arena trong WiFi LAN → đọc tỉ số real-time → push lên iLeague.
Tùy chọn: pull RTSP camera tại CLB → composite overlay tỉ số → livestream YouTube.

## Cài đặt

**Người dùng cuối**: download bản binary tại https://ileague.info/h
- Windows: `iLeagueHub-win.zip` — giải nén → chạy `iLeagueHub.exe`
- macOS: `iLeagueHub-mac.zip` — giải nén → chạy `iLeagueHub.app`

**Lập trình viên**: chạy từ source
```bash
pip3 install -r requirements.txt
python3 hub_agent.py
```

## Setup bảng điểm — KHÔNG cần làm gì

iLeague Hub đọc tỉ số qua **HTTP** (port 8080 mặc định của 3 app scoreboard). 3 app này tự chạy HTTP server expose game data.json. Hub chỉ là HTTP client read-only.

→ **KHÔNG cần** ADB, USB debugging, Wireless debugging, root, OCR, screenshot, hay sửa đổi gì trên bảng điểm.

Yêu cầu duy nhất: bảng điểm và PC chạy Hub **cùng WiFi LAN** (cùng subnet, vd 192.168.1.x).

## Setup camera RTSP (cho tính năng livestream YouTube)

Nếu CLB đã cấu hình camera RTSP trong app bảng điểm (đa số đã có sẵn để hiển thị video bàn) → Hub tự discover URL từ HTTP config của bảng điểm. **Không cần admin làm gì.**

Trường hợp probe không tìm được (path config app version mới chưa biết): admin paste full RTSP URL `rtsp://user:pass@ip:554/...` 1 lần vào UI Hub, lưu local SQLite.

## Chạy

```bash
python3 hub_agent.py        # foreground, mở browser tự động
python3 hub_agent.py --background  # không mở browser
```

Mở dashboard: **http://localhost:5050**

## Build binary

```bash
bash build.sh
```

Output `dist/iLeagueHub.app` (Mac) hoặc `dist/iLeagueHub.exe` (Windows). Kèm ffmpeg bundled.

CI tự build cả Win+Mac qua GitHub Actions: https://github.com/trandinhvu/ileague-hub/actions

## API Endpoints chính

| Endpoint | Method | Mô tả |
|---|---|---|
| `/api/status` | GET | Trạng thái agent + Pro license |
| `/api/devices` | GET | Danh sách bảng điểm active |
| `/api/scan` | POST | Trigger scan WiFi |
| `/api/active_tournament` | GET/POST | Set giải đấu hiện hành (auto-map dùng) |
| `/api/automap` | POST | Auto-map bảng điểm ↔ trận đấu theo cặp tên VĐV |
| `/api/cameras` | GET/POST | List/set camera RTSP per IP |
| `/api/cameras/probe` | POST | Re-probe RTSP từ scoreboard config |
| `/api/live/start` | POST | Go live YouTube stream cho 1 IP |
| `/api/live/start_all` | POST | Go live tất cả bảng điểm đã map |
| `/api/live/status` | GET | Trạng thái streams đang chạy |

## Architecture

```
[Tablet bảng điểm Android]                [Camera RTSP IP]
       :8080 HTTP                              ↓ rtsp://
       (data.json)                             ↓
            ↓                                  ↓
       [iLeague Hub Agent on PC CLB]
            ├─ scan WiFi /24, đọc :8080 mỗi 3s
            ├─ probe scoreboard config tìm rtsp_url
            ├─ auto-map score ↔ match by player names
            └─ ffmpeg pull RTSP + overlay → YouTube RTMP
            ↓
       [ileague.info backend]                [YouTube Live]
       Score updates                          Public stream
```

Tất cả compute video chạy **local trên PC CLB**. Server iLeague chỉ thấy: tỉ số (text) + URL YouTube. Không thấy stream video, RTSP URL, hay password camera.

## License

Internal — Archisketch / iLeague.
