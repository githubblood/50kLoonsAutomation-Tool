"""
utils/device_manager.py — Device fingerprint manager.
Builds a Playwright-compatible fingerprint for each row.
"""
from __future__ import annotations
import random
from typing import Any
import structlog
from devices_pool import DEVICE_POOL
from .stealth import _CARRIERS

# Tracks the last carrier used so we never repeat it on the very next call.
_last_carrier: list[str] = [""]

log = structlog.get_logger(__name__)

_LOCALES = [
    "en-US", "en-GB", "en-CA", "en-AU", "en-IN",
    "es-ES", "es-MX", "fr-FR", "de-DE", "pt-BR",
]
_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "Europe/London", "Europe/Paris",
    "Asia/Tokyo", "Asia/Kolkata", "Australia/Sydney",
]
_COLOR_SCHEMES = ["light", "dark", "no-preference"]


class DeviceManager:
    """Generates a fresh device fingerprint for every row."""

    def __init__(self, config: dict) -> None:
        self._defaults = config.get("device_defaults", {})
        self._col_map = config.get("sheet_columns", {})

    def build_fingerprint(self, row: dict[str, Any]) -> dict[str, Any]:
        """Build a Playwright-compatible context fingerprint from row data."""
        use_custom_col = self._col_map.get("use_custom_device", "Use_Custom_Device")
        use_custom = str(row.get(use_custom_col, "")).strip().lower() == "yes"

        fp = self._build_custom(row) if use_custom else self._build_random()

        # Apply orientation
        ori_col = self._col_map.get("orientation", "Orientation")
        orientation = str(row.get(ori_col, "")).strip().lower()
        if orientation == "random":
            orientation = random.choice(["portrait", "landscape"])
        if orientation == "landscape":
            vp = fp.get("viewport", {})
            fp["viewport"] = {"width": vp.get("height", 915), "height": vp.get("width", 412)}

        log.info("device.fingerprint", model=fp.get("_model", "random"),
                 viewport=fp.get("viewport"), locale=fp.get("locale"))
        return fp

    def _build_custom(self, row: dict[str, Any]) -> dict[str, Any]:
        """Build fingerprint from sheet-specified device columns."""
        model_col = self._col_map.get("device_model", "Device_Model")
        ver_col = self._col_map.get("android_version", "Android_Version")
        model = str(row.get(model_col, "")).strip()
        android_ver = str(row.get(ver_col, "14")).strip()

        match = next((d for d in DEVICE_POOL if d["model"].lower() == model.lower()), None)
        if match:
            fp = self._device_to_fp(match)
            if android_ver and android_ver != match["android_version"]:
                fp["user_agent"] = fp["user_agent"].replace(
                    f"Android {match['android_version']}", f"Android {android_ver}")
        else:
            log.warning("device.unknown_model", model=model)
            fp = self._build_generic(model, android_ver)
        fp["_model"] = model
        return fp

    def _build_random(self) -> dict[str, Any]:
        """Pick a random device and jitter its properties."""
        device = random.choice(DEVICE_POOL)
        fp = self._device_to_fp(device)
        if self._defaults.get("random_viewport", True):
            w_r = self._defaults.get("viewport_width_range", [360, 430])
            h_r = self._defaults.get("viewport_height_range", [640, 932])
            fp["viewport"] = {"width": random.randint(w_r[0], w_r[1]),
                              "height": random.randint(h_r[0], h_r[1])}
        fp["_model"] = device["model"]
        return fp

    def _device_to_fp(self, device: dict) -> dict[str, Any]:
        """Convert a DEVICE_POOL entry into a Playwright context dict."""
        fp: dict[str, Any] = {
            "user_agent": device["user_agent"],
            "viewport": dict(device["viewport"]),
            "device_scale_factor": device.get("device_scale_factor", 2.625),
            "is_mobile": device.get("is_mobile", True),
            "has_touch": device.get("has_touch", True),
        }
        self._add_extras(fp)
        return fp

    def _build_generic(self, model: str, android_ver: str) -> dict[str, Any]:
        """Fallback fingerprint when model isn't in the pool."""
        w_r = self._defaults.get("viewport_width_range", [360, 430])
        h_r = self._defaults.get("viewport_height_range", [640, 932])
        ua = (f"Mozilla/5.0 (Linux; Android {android_ver}; {model}) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.6422.165 Mobile Safari/537.36")
        fp: dict[str, Any] = {
            "user_agent": ua,
            "viewport": {"width": random.randint(w_r[0], w_r[1]),
                         "height": random.randint(h_r[0], h_r[1])},
            "device_scale_factor": round(random.uniform(2.0, 3.5), 1),
            "is_mobile": True, "has_touch": True,
        }
        self._add_extras(fp)
        return fp

    def _add_extras(self, fp: dict[str, Any]) -> None:
        """Add locale, timezone, colour-scheme, and carrier randomisation."""
        if self._defaults.get("random_locale", True):
            fp["locale"] = random.choice(_LOCALES)
        if self._defaults.get("random_timezone", True):
            fp["timezone_id"] = random.choice(_TIMEZONES)
        if self._defaults.get("random_color_scheme", True):
            fp["color_scheme"] = random.choice(_COLOR_SCHEMES)
        # Always pick a carrier different from the previous attempt.
        pool = [c for c in _CARRIERS if c != _last_carrier[0]] or list(_CARRIERS)
        carrier = random.choice(pool)
        _last_carrier[0] = carrier
        fp["_carrier"] = carrier
