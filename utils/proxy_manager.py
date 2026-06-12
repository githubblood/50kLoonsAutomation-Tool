"""
utils/proxy_manager.py
──────────────────────
Proxy rotation engine.

Supports proxy sources (configured via PROXY_SOURCE env var):
  0. "none"     → no proxy; use the machine's current IP
  1. "file"     → reads from proxies.txt (one proxy per line)
  2. "env"      → reads comma-separated list from PROXY_LIST env var
  3. "rotating" → uses a single rotating-gateway URL (e.g. Bright Data)

For modes 1 & 2 the manager cycles through the list, picking a fresh
proxy for every row.  For mode 3 the same URL is returned each time
(the gateway itself rotates the exit IP).
"""

from __future__ import annotations

import itertools
import os
import random
from pathlib import Path
from urllib.parse import unquote

import structlog

log = structlog.get_logger(__name__)

# Froxy carrier code mapping — injected into the password field of mobile URLs.
# Format: http://user:mobile;country;CARRIER;state;city@host:port
_FROXY_CARRIER_MAP: dict[str, str] = {
    "AT&T":         "att",
    "Verizon":      "verizon",
    "T-Mobile":     "tmobile",
    "Boost Mobile": "boost",
    "US Cellular":  "uscellular",
}


class ProxyManager:
    """Thread-safe* proxy rotator.  (*sequential usage assumed.)"""

    def __init__(self) -> None:
        source = os.getenv("PROXY_SOURCE", "file").strip().lower()
        self._proxies: list[str] = []
        self._mode: str = source
        self._proxy_carriers: dict[str, str | None] = {}
        self._current_carrier: str | None = None

        if source in ("none", "direct", "off", "disabled"):
            log.info("proxy.disabled", msg="Running on current machine IP (no proxy)")
        elif source == "file":
            self._load_from_file()
        elif source == "env":
            self._load_from_env()
        elif source == "rotating":
            self._load_rotating()
        else:
            log.warning("proxy.unknown_source", source=source, fallback="none")

        if self._proxies:
            # Shuffle once so runs don't always start with the same proxy
            random.shuffle(self._proxies)
            self._cycle = itertools.cycle(self._proxies)
            log.info("proxy.loaded", mode=source, count=len(self._proxies))
        else:
            self._cycle = None
            log.warning("proxy.none_loaded", mode=source)

    # ── loaders ──────────────────────────────────────────────────────

    def _load_from_file(self) -> None:
        """Read proxies.txt – one proxy URL per line, ignoring blanks/comments."""
        proxy_file = Path("proxies.txt")
        if not proxy_file.exists():
            log.warning("proxy.file_missing", path=str(proxy_file))
            return
        with proxy_file.open() as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    self._proxies.append(line)

    def _load_from_env(self) -> None:
        """Parse comma-separated PROXY_LIST env var."""
        raw = os.getenv("PROXY_LIST", "")
        for proxy in raw.split(","):
            proxy = proxy.strip()
            if proxy:
                self._proxies.append(proxy)

    def _load_rotating(self) -> None:
        """Use rotating-gateway endpoint(s).

        Loads WiFi and/or mobile URLs as-is.  Froxy rotates the exit IP
        automatically on every connection — no carrier injection needed
        (injecting carrier codes breaks HTTPS CONNECT tunnelling for most
        account types).
        """
        force_type = os.getenv("ROTATING_PROXY_FORCE_TYPE", "").strip().lower()
        allowed_type = force_type if force_type in {"wifi", "mobile"} else ""

        url = os.getenv("ROTATING_PROXY_URL", "").strip()
        if url and (not allowed_type or f":{allowed_type};" in url):
            self._proxies.append(url)
            self._proxy_carriers[url] = None

        mobile_url = os.getenv("ROTATING_PROXY_MOBILE_URL", "").strip()
        if mobile_url and (not allowed_type or f":{allowed_type};" in mobile_url):
            self._proxies.append(mobile_url)
            self._proxy_carriers[mobile_url] = None

        if allowed_type:
            log.info("proxy.rotating_force_type", force_type=allowed_type, count=len(self._proxies))

    # ── public API ───────────────────────────────────────────────────

    def next_proxy(self) -> str | None:
        """Return the next proxy URL, or ``None`` if no proxies are configured."""
        if self._cycle is None:
            return None
        proxy = next(self._cycle)
        self._current_carrier = self._proxy_carriers.get(proxy)
        log.debug("proxy.selected", proxy=self._mask(proxy), carrier=self._current_carrier)
        return proxy

    @property
    def current_carrier(self) -> str | None:
        """Carrier name for the proxy returned by the last next_proxy() call."""
        return self._current_carrier

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

    @property
    def total(self) -> int:
        return len(self._proxies)

    @staticmethod
    def proxy_type(proxy_url: str | None) -> str:
        """Return proxy type hint from URL (wifi/mobile/direct/unknown)."""
        if not proxy_url:
            return "direct"
        if ":mobile;" in proxy_url:
            return "mobile"
        if ":wifi;" in proxy_url:
            return "wifi"
        return "unknown"

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _inject_carrier(mobile_url: str, carrier_code: str) -> str:
        """Inject a Froxy carrier code into the password field of a mobile proxy URL.

        Transforms: http://user:mobile;us;;;@host:port
        Into:       http://user:mobile;us;att;;@host:port
        """
        if "://" not in mobile_url or "@" not in mobile_url:
            return mobile_url
        scheme, rest = mobile_url.split("://", 1)
        creds, host_port = rest.rsplit("@", 1)
        if ":" not in creds:
            return mobile_url
        username, password = creds.split(":", 1)
        parts = password.split(";")
        if len(parts) >= 3:
            parts[2] = carrier_code
        return f"{scheme}://{username}:{';'.join(parts)}@{host_port}"

    @staticmethod
    def _mask(url: str) -> str:
        """Mask credentials in proxy URL for safe logging."""
        if "@" in url:
            scheme_rest = url.split("://", 1)
            if len(scheme_rest) == 2:
                scheme, rest = scheme_rest
                creds, host = rest.rsplit("@", 1)
                return f"{scheme}://****:****@{host}"
        return url

    @staticmethod
    def to_playwright_proxy(proxy_url: str) -> dict:
        """
        Convert a proxy URL string into the dict format Playwright expects:
            {"server": "...", "username": "...", "password": "..."}
        
        Accepted formats:
            protocol://host:port
            protocol://user:pass@host:port
        """
        result: dict[str, str] = {}

        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"

        scheme, rest = proxy_url.split("://", 1)

        if "@" in rest:
            creds, host_port = rest.rsplit("@", 1)
            if ":" in creds:
                username, password = creds.split(":", 1)
                result["username"] = unquote(username)
                result["password"] = unquote(password)
            result["server"] = f"{scheme}://{host_port}"
        else:
            result["server"] = f"{scheme}://{rest}"

        return result
