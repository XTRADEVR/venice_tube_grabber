"""
Venice Tube Grabber - 5GB Ultra Streaming Downloader Engine
==========================================================
Engine unduhan chunked berkinerja tinggi yang dirancang khusus untuk file besar (hingga 5GB):
- Chunked streaming langsung ke disk tanpa membebani RAM (bebas MemoryError)
- HTTP Range Header Resume (melanjutkan file yang terputus)
- Retry otomatis dengan Exponential Backoff
- Penghitungan kecepatan riil (MB/s) & sisa waktu (ETA)
- Penyimpanan sementara di .incoming/ lalu verifikasi ke folder final
- Pencatatan anti-duplikasi ke .downloaded.txt
- Konversi / remuxing menggunakan FFmpeg
"""

import os
import sys
import time
import shutil
import urllib.request
import urllib.error
import urllib.parse
import subprocess
from pathlib import Path

# Pastikan sys.path menunjuk ke folder modul
sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import Callable, Optional, Dict, Any

from config import (
    APP, BASE, CFG_FILE, INCOMING, ARCHIVE, UA, FFMPEG,
    MEDIA_EXT, BAD, IS_ANDROID, clean_filename, load_settings,
    add_to_archive, is_already_downloaded
)
from dns_resolver import resolve_hostname_doh

class DownloadTask:
    def __init__(self, task_id: str, url: str, custom_filename: Optional[str] = None):
        self.task_id = task_id
        self.url = url
        self.custom_filename = custom_filename
        self.filename = ""
        self.total_bytes = 0
        self.downloaded_bytes = 0
        self.speed_bps = 0.0
        self.eta_seconds = 0
        self.percentage = 0.0
        self.status = "queued"  # queued, probing, downloading, paused, cancelled, completed, remuxing, failed
        self.error_message = ""
        self.resumable = False
        self.incoming_path: Optional[Path] = None
        self.final_path: Optional[Path] = None
        self.is_paused = False
        self.is_cancelled = False
        self.format_remux: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "url": self.url,
            "filename": self.filename,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "speed_mbps": round(self.speed_bps / (1024 * 1024), 2),
            "eta_seconds": self.eta_seconds,
            "percentage": round(self.percentage, 2),
            "status": self.status,
            "resumable": self.resumable,
            "error_message": self.error_message
        }

class VeniceDownloader:
    def __init__(self):
        self.settings = load_settings()
        self.chunk_size = self.settings.get("chunk_size_bytes", 4 * 1024 * 1024)
        self.max_retries = self.settings.get("max_retries", 5)
        self.backoff_factor = self.settings.get("backoff_factor", 1.5)

    def probe_url(self, task: DownloadTask) -> bool:
        """
        Memeriksa URL sebelum mengunduh:
        - Mendapatkan Content-Length (ukuran file hingga 5GB)
        - Mengecek dukungan HTTP Range (Resume)
        - Mengekstrak nama file dan membersihkan dengan regex BAD
        """
        task.status = "probing"
        try:
            parsed = urllib.parse.urlparse(task.url)
            headers = {"User-Agent": UA, "Accept": "*/*", "Connection": "keep-alive"}

            # Kirim request HEAD
            req = urllib.request.Request(task.url, headers=headers, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=self.settings.get("connect_timeout", 15)) as resp:
                    h_dict = dict(resp.headers)
            except urllib.error.HTTPError:
                # Fallback ke Range 0-0 jika server memblokir HEAD
                req_fb = urllib.request.Request(task.url, headers={**headers, "Range": "bytes=0-0"})
                with urllib.request.urlopen(req_fb, timeout=15) as resp:
                    h_dict = dict(resp.headers)

            # Ekstraksi Content-Length
            cl = h_dict.get("Content-Length") or h_dict.get("content-length")
            if cl and cl.isdigit():
                task.total_bytes = int(cl)

            # Cek dukungan HTTP Range untuk resume
            ar = h_dict.get("Accept-Ranges") or h_dict.get("accept-ranges")
            task.resumable = (ar == "bytes" or "content-range" in h_dict)

            # Ekstraksi nama file
            if task.custom_filename:
                raw_name = task.custom_filename
            else:
                cd = h_dict.get("Content-Disposition") or h_dict.get("content-disposition", "")
                if "filename=" in cd:
                    raw_name = cd.split("filename=")[-1].strip('"\' ')
                else:
                    path_name = os.path.basename(parsed.path)
                    raw_name = path_name if path_name else f"video_{int(time.time())}.mp4"

            # Terapkan pembersihan regex BAD
            task.filename = clean_filename(raw_name)
            if not any(task.filename.endswith(ext) for cat in MEDIA_EXT.values() for ext in cat):
                task.filename += ".mp4"

            task.incoming_path = INCOMING / f"{task.filename}.part"
            dl_dir = Path(self.settings.get("download_dir", BASE / "downloads"))
            dl_dir.mkdir(parents=True, exist_ok=True)
            task.final_path = dl_dir / task.filename

            return True
        except Exception as e:
            task.status = "failed"
            task.error_message = f"Gagal memeriksa URL: {str(e)}"
            return False

    def download_file(self, task: DownloadTask, progress_callback: Optional[Callable[[DownloadTask], None]] = None) -> bool:
        """
        Mengunduh berkas dengan chunked streaming, resume HTTP Range, dan exponential backoff.
        """
        if not self.probe_url(task):
            return False

        # Cek file parsial di .incoming untuk RESUME
        existing_bytes = 0
        if task.incoming_path and task.incoming_path.exists():
            existing_bytes = task.incoming_path.stat().st_size
            if task.total_bytes > 0 and existing_bytes == task.total_bytes:
                return self._finalize_download(task, progress_callback)
            elif task.resumable and existing_bytes > 0:
                print(f"[Venice Tube] Melanjutkan download dari byte ke-{existing_bytes}...")
                task.downloaded_bytes = existing_bytes

        retries = 0
        task.status = "downloading"
        last_calc_time = time.time()
        bytes_at_last_calc = existing_bytes

        while retries <= self.max_retries and not task.is_cancelled:
            try:
                headers = {"User-Agent": UA, "Accept": "*/*", "Connection": "keep-alive"}

                # Kirim Range header jika resume didukung
                if task.resumable and task.downloaded_bytes > 0:
                    headers["Range"] = f"bytes={task.downloaded_bytes}-"
                    file_mode = "ab"
                else:
                    file_mode = "wb"
                    task.downloaded_bytes = 0

                req = urllib.request.Request(task.url, headers=headers)

                with urllib.request.urlopen(req, timeout=self.settings.get("read_timeout", 60)) as resp:
                    if resp.status not in (200, 206):
                        raise Exception(f"Server mengembalikan HTTP {resp.status}")

                    with open(task.incoming_path, file_mode) as f:
                        while not task.is_cancelled:
                            if task.is_paused:
                                task.status = "paused"
                                if progress_callback: progress_callback(task)
                                time.sleep(0.5)
                                continue

                            # Baca chunk buffer (misal 4MB) agar konsumsi RAM tetap rendah
                            chunk = resp.read(self.chunk_size)
                            if not chunk:
                                break

                            f.write(chunk)
                            task.downloaded_bytes += len(chunk)

                            now = time.time()
                            duration = now - last_calc_time
                            if duration >= 0.5:
                                bytes_diff = task.downloaded_bytes - bytes_at_last_calc
                                task.speed_bps = bytes_diff / duration
                                if task.total_bytes > 0:
                                    task.percentage = (task.downloaded_bytes / task.total_bytes) * 100.0
                                    rem_bytes = max(0, task.total_bytes - task.downloaded_bytes)
                                    task.eta_seconds = int(rem_bytes / task.speed_bps) if task.speed_bps > 0 else 0

                                last_calc_time = now
                                bytes_at_last_calc = task.downloaded_bytes
                                if progress_callback: progress_callback(task)

                if not task.is_cancelled and not task.is_paused:
                    return self._finalize_download(task, progress_callback)

            except Exception as e:
                retries += 1
                if task.is_cancelled:
                    task.status = "cancelled"
                    return False

                if retries > self.max_retries:
                    task.status = "failed"
                    task.error_message = f"Download gagal setelah {self.max_retries}x retries. Error: {e}"
                    if progress_callback: progress_callback(task)
                    return False

                wait_sec = self.backoff_factor ** retries
                print(f"[Venice Retry] Koneksi interupsi: {e}. Menunggu {wait_sec:.1f}s (Percobaan {retries}/{self.max_retries})...")
                time.sleep(wait_sec)

        return False

    def _finalize_download(self, task: DownloadTask, progress_callback=None) -> bool:
        """
        Pindahkan file dari .incoming/ ke target akhir, remux via FFmpeg jika dipilih,
        dan catat ke arsip .downloaded.txt.
        """
        try:
            if task.format_remux and FFMPEG:
                task.status = "remuxing"
                if progress_callback: progress_callback(task)

                remuxed_name = f"{Path(task.filename).stem}.{task.format_remux}"
                remuxed_path = task.final_path.parent / remuxed_name

                cmd = [
                    FFMPEG, "-y", "-i", str(task.incoming_path),
                    "-c", "copy" if task.format_remux in ("mp4", "mkv") else "aac",
                    str(remuxed_path)
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                task.incoming_path.unlink(missing_ok=True)
                task.final_path = remuxed_path
                task.filename = remuxed_name
            else:
                shutil.move(str(task.incoming_path), str(task.final_path))

            task.status = "completed"
            task.percentage = 100.0
            task.speed_bps = 0.0
            task.eta_seconds = 0

            final_size = task.final_path.stat().st_size if task.final_path.exists() else task.downloaded_bytes
            add_to_archive(task.filename, task.url, final_size)

            if progress_callback: progress_callback(task)
            print(f"[Venice Tube] Selesai diunduh: {task.filename}")
            return True
        except Exception as e:
            task.status = "failed"
            task.error_message = f"Gagal finalisasi: {e}"
            if progress_callback: progress_callback(task)
            return False
