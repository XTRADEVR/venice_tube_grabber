"""
Venice Tube Grabber - Server Backend Utama (Python)
===================================================
Jalankan: python main.py
"""

import os
import sys
import json
import time
import uuid
import shutil
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse

from config import (
    APP, BASE, CFG_FILE, INCOMING, ARCHIVE, UA, FFMPEG,
    IS_ANDROID, clean_filename, load_settings, save_settings, init_directories
)
from dns_resolver import patch_socket_for_cloudflare_dns, resolve_hostname_doh
from downloader import DownloadTask, VeniceDownloader

DOWNLOAD_TASKS = {}
TASKS_LOCK = threading.Lock()
ACTIVE_DOWNLOADER = VeniceDownloader()

class VeniceRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        static_dir = BASE / "static"
        super().__init__(*args, directory=str(static_dir), **kwargs)

    def _send_json(self, data: dict, code: int = 200):
        b = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path

        if p == "/api/system":
            settings = load_settings()
            self._send_json({
                "app": APP,
                "is_android": IS_ANDROID,
                "storage_path": settings.get("download_dir"),
                "ffmpeg_available": bool(FFMPEG),
                "dns_1111_active": settings.get("use_dns_1111", True)
            })
            return
        elif p == "/api/settings":
            self._send_json(load_settings())
            return
        elif p == "/api/archive":
            lines = []
            if ARCHIVE.exists():
                with open(ARCHIVE, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
            self._send_json({"archive": lines})
            return
        elif p == "/api/tasks":
            with TASKS_LOCK:
                tasks_data = [t.to_dict() for t in DOWNLOAD_TASKS.values()]
            self._send_json({"tasks": tasks_data})
            return

        return super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            try: body = json.loads(self.rfile.read(content_len).decode("utf-8"))
            except Exception: pass

        if p == "/api/probe":
            url = body.get("url", "").strip()
            parsed_url = urllib.parse.urlparse(url)
            resolved_ip = resolve_hostname_doh(parsed_url.hostname) if parsed_url.hostname else None
            temp_task = DownloadTask("probe_temp", url)

            if ACTIVE_DOWNLOADER.probe_url(temp_task):
                self._send_json({
                    "success": True,
                    "url": url,
                    "filename": temp_task.filename,
                    "total_bytes": temp_task.total_bytes,
                    "resumable": temp_task.resumable,
                    "resolved_ip": resolved_ip or "Default",
                    "dns_provider": "Cloudflare 1.1.1.1 DoH Turbo"
                })
            else:
                self._send_json({"success": False, "error": temp_task.error_message}, 400)
            return

        elif p == "/api/download/start":
            url = body.get("url", "").strip()
            task_id = str(uuid.uuid4())[:8]
            task = DownloadTask(task_id, url, body.get("filename"))
            task.format_remux = body.get("remux_format")

            with TASKS_LOCK:
                DOWNLOAD_TASKS[task_id] = task

            threading.Thread(target=ACTIVE_DOWNLOADER.download_file, args=(task,), daemon=True).start()
            self._send_json({"message": "Download dimulai", "task": task.to_dict()})
            return

        elif p.startswith("/api/download/pause/"):
            t_id = p.replace("/api/download/pause/", "")
            with TASKS_LOCK:
                task = DOWNLOAD_TASKS.get(t_id)
            if task:
                task.is_paused = True
                self._send_json({"message": "Dijeda"})
            return

        elif p.startswith("/api/download/resume/"):
            t_id = p.replace("/api/download/resume/", "")
            with TASKS_LOCK:
                task = DOWNLOAD_TASKS.get(t_id)
            if task:
                task.is_paused = False
                task.status = "downloading"
                self._send_json({"message": "Dilanjutkan"})
            return

        elif p.startswith("/api/download/cancel/"):
            t_id = p.replace("/api/download/cancel/", "")
            with TASKS_LOCK:
                task = DOWNLOAD_TASKS.get(t_id)
            if task:
                task.is_cancelled = True
                self._send_json({"message": "Dibatalkan"})
            return

        elif p == "/api/simulate-test-5gb":
            t_id = f"sim_{str(uuid.uuid4())[:6]}"
            sim_task = DownloadTask(t_id, "https://speed.cloudflare.com/test-5gb.bin", "venice_benchmark_5GB_video.mp4")
            sim_task.total_bytes = 5 * 1024 * 1024 * 1024
            sim_task.filename = "venice_benchmark_5GB_video.mp4"
            sim_task.resumable = True
            with TASKS_LOCK:
                DOWNLOAD_TASKS[t_id] = sim_task

            def sim_worker(t):
                t.status = "downloading"
                chunk = 12 * 1024 * 1024
                while t.downloaded_bytes < t.total_bytes and not t.is_cancelled:
                    if t.is_paused: time.sleep(0.5); continue
                    time.sleep(0.1)
                    t.downloaded_bytes = min(t.total_bytes, t.downloaded_bytes + chunk)
                    t.speed_bps = chunk * 10
                    t.percentage = (t.downloaded_bytes / t.total_bytes) * 100
                if not t.is_cancelled and not t.is_paused:
                    t.status = "completed"
                    t.percentage = 100

            threading.Thread(target=sim_worker, args=(sim_task,), daemon=True).start()
            self._send_json({"message": "Simulasi 5GB dimulai", "task": sim_task.to_dict()})
            return

        self._send_json({"error": "Endpoint tidak ditemukan"}, 404)

def start_server(port: int = 8080):
    init_directories()
    patch_socket_for_cloudflare_dns()
    httpd = HTTPServer(("0.0.0.0", port), VeniceRequestHandler)
    print(f"🚀 {APP} Berjalan di http://0.0.0.0:{port}")
    httpd.serve_forever()

if __name__ == "__main__":
    start_server(int(os.environ.get("PORT", 8080)))
