"""TEST-ONLY shim: makes DNS work on VMs with intercepted port-53.

NOT shipped with the package. Patches socket.getaddrinfo to resolve via
DNS-over-HTTPS (dns.google), so `requests` and plain socket calls work here.
On normal machines DNS works fine and this shim is unnecessary.

Usage: python -m doh_shim <script>  OR  python -c "import doh_shim; ..."
"""

import json
import socket
import urllib.request

_orig_getaddrinfo = socket.getaddrinfo
_cache: dict[str, list] = {}


def _doh_resolve(host: str):
    if host in _cache:
        return _cache[host]
    # Never resolve dns.google through ourselves.
    if host == "dns.google":
        return [(1, "8.8.8.8"), (1, "8.8.4.4")]
    try:
        import subprocess
        for qtype in (1, 28):  # A, AAAA
            out = subprocess.run(
                ["curl", "-s", "--max-time", "10",
                 f"https://dns.google/resolve?name={host}&type={qtype}"],
                capture_output=True, timeout=15, text=True)
            if out.returncode != 0 or not out.stdout.strip():
                continue
            data = json.loads(out.stdout)
            for ans in data.get("Answer", []):
                if ans["type"] in (1, 28):
                    _cache.setdefault(host, []).append((ans["type"], ans["data"]))
    except Exception:
        pass
    return _cache.get(host, [])


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host is None:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    try:
        # Fast path: literal IP.
        import ipaddress
        ipaddress.ip_address(host)
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except ValueError:
        pass
    resolved = _doh_resolve(host)
    if not resolved:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    out = []
    for qtype, addr in resolved:
        fam = socket.AF_INET if qtype == 1 else socket.AF_INET6
        if family not in (0, fam):
            continue
        sockaddr = (addr, port) if fam == socket.AF_INET else (addr, port, 0, 0)
        out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
    if not out:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    return out


socket.getaddrinfo = _patched_getaddrinfo
# requests' vendored urllib3 calls socket.getaddrinfo too — covered.
