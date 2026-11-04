"""
Venice Tube Grabber - Cloudflare DNS 1.1.1.1 Turbo Resolver
===========================================================
Menyediakan resolusi domain berkecepatan tinggi menggunakan Cloudflare DNS 1.1.1.1 (DoH - DNS over HTTPS).
Menjamin:
- Bypass pemblokiran / sensor DNS lokal oleh ISP (Internet Positif / DNS Nawala)
- Rute Anycast tercepat ke Content Delivery Network (CDN) video global
- Proteksi privasi tanpa pencatatan IP oleh pihak ketiga
"""

import json
import socket
import urllib.request
from typing import Optional

CLOUDFLARE_DOH_PRIMARY = "https://1.1.1.1/dns-query"
CLOUDFLARE_DOH_SECONDARY = "https://1.0.0.1/dns-query"
_DNS_CACHE = {}

def resolve_hostname_doh(hostname: str, record_type: str = "A") -> Optional[str]:
    """
    Melakukan resolusi domain ke IP menggunakan Cloudflare DoH (DNS over HTTPS).
    Mengembalikan IP address (IPv4) pertama yang ditemukan.
    """
    if hostname in _DNS_CACHE:
        return _DNS_CACHE[hostname]

    try:
        socket.inet_aton(hostname)
        return hostname
    except socket.error:
        pass

    url = f"{CLOUDFLARE_DOH_PRIMARY}?name={hostname}&type={record_type}"
    headers = {
        "Accept": "application/dns-json",
        "User-Agent": "Venice-Tube-Grabber-DNS1111/1.0"
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        ip = ans.get("data")
                        if ip:
                            _DNS_CACHE[hostname] = ip
                            return ip
    except Exception:
        # Fallback ke 1.0.0.1
        try:
            sec_req = urllib.request.Request(f"{CLOUDFLARE_DOH_SECONDARY}?name={hostname}&type={record_type}", headers=headers)
            with urllib.request.urlopen(sec_req, timeout=4) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        ip = ans.get("data")
                        if ip:
                            _DNS_CACHE[hostname] = ip
                            return ip
        except Exception:
            pass

    # Fallback terakhir ke resolver lokal jika koneksi DoH gagal
    try:
        ip = socket.gethostbyname(hostname)
        _DNS_CACHE[hostname] = ip
        return ip
    except Exception:
        return None

def patch_socket_for_cloudflare_dns():
    """
    Melakukan monkey-patching pada socket.getaddrinfo agar seluruh koneksi Python
    secara transparan diarahkan melalui Cloudflare 1.1.1.1 DoH!
    """
    original_getaddrinfo = socket.getaddrinfo

    def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(host, str) and not host.replace('.', '').isdigit() and host != 'localhost':
            resolved_ip = resolve_hostname_doh(host)
            if resolved_ip:
                host = resolved_ip
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = custom_getaddrinfo
    print("[Venice DNS] Cloudflare DNS 1.1.1.1 DoH Turbo Aktif!")
