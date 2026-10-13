import re
import hashlib
from typing import Literal

DeviceType = Literal["desktop", "mobile"]


def detect_device_type(user_agent: str) -> DeviceType:
    ua = (user_agent or "").lower()
    # Patterns for mobile / tablets
    mobile_patterns = [
        r"mobile",
        r"android",
        r"iphone",
        r"ipad",
        r"ipod",
        r"windows phone",
        r"blackberry",
        r"opera mini",
        r"iemobile",
    ]
    for pattern in mobile_patterns:
        if re.search(pattern, ua):
            return "mobile"
    return "desktop"


def parse_browser_os(user_agent: str) -> str:
    ua = user_agent or ""
    # Detect OS
    os_name = "Unknown OS"
    if "Windows NT 10.0" in ua:
        os_name = "Windows 10/11"
    elif "Windows NT 6.3" in ua:
        os_name = "Windows 8.1"
    elif "Windows NT 6.1" in ua:
        os_name = "Windows 7"
    elif "Macintosh; Intel Mac OS X" in ua:
        os_name = "macOS"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Linux" in ua:
        os_name = "Linux"

    # Detect Browser
    browser = "Browser"
    if "Edg/" in ua:
        browser = "Edge"
    elif "Chrome/" in ua and "Chromium" not in ua and "Edg" not in ua:
        browser = "Chrome"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Zalo" in ua:
        browser = "Zalo Web"

    return f"{browser} ({os_name})"


def generate_device_hash(user_agent: str, ip: str, client_fp: str = "") -> str:
    """Tao ma dinh danh thiet bi du phong dua tren UA + IP subnet."""
    if client_fp and len(client_fp) >= 16:
        return client_fp[:32]
    ip_parts = ip.split(".")
    ip_subnet = ".".join(ip_parts[:3]) if len(ip_parts) == 4 else ip
    raw = f"{user_agent}|{ip_subnet}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]
