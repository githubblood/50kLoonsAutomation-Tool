"""
utils/stealth.py — Anti-detection & human-like behaviour helpers.

Provides:
  • JavaScript stealth patches injected into every page context
  • Human-like typing with variable inter-key delays
  • Random mouse movements and scroll actions
  • Random delay helpers
"""
from __future__ import annotations
import json
import random
import time
from pathlib import Path
from typing import Any
import structlog
from playwright.sync_api import Page, Locator

log = structlog.get_logger(__name__)

# ── Randomisation pools ─────────────────────────────────────────────

_CARRIERS = [
    "AT&T",
    "Verizon",
    "T-Mobile",
    "Boost Mobile",
    "US Cellular",
]

# All profiles are cellular — matches the mobile proxy being used.
# Each entry: (label shown in logs, navigator.connection.type, effectiveType, downlink Mbps, rtt ms)
_CONNECTION_PROFILES: list[dict] = [
    {"label": "5G mmWave",          "type": "cellular", "effectiveType": "4g", "downlink": 200.0, "rtt": 5},
    {"label": "5G Sub-6GHz",        "type": "cellular", "effectiveType": "4g", "downlink": 80.0,  "rtt": 15},
    {"label": "4G LTE Advanced",    "type": "cellular", "effectiveType": "4g", "downlink": 35.0,  "rtt": 30},
    {"label": "4G LTE",             "type": "cellular", "effectiveType": "3g", "downlink": 18.0,  "rtt": 50},
    {"label": "LTE Weak Signal",    "type": "cellular", "effectiveType": "3g", "downlink": 8.0,   "rtt": 80},
]

# (webgl_vendor, webgl_renderer)
_WEBGL_PROFILES: list[tuple[str, str]] = [
    ("Google Inc. (Qualcomm)", "ANGLE (Qualcomm, Adreno (TM) 730, OpenGL ES 3.2)"),
    ("Google Inc. (Qualcomm)", "ANGLE (Qualcomm, Adreno (TM) 640, OpenGL ES 3.2)"),
    ("Google Inc. (Qualcomm)", "ANGLE (Qualcomm, Adreno (TM) 650, OpenGL ES 3.2)"),
    ("Google Inc. (ARM)",      "ANGLE (ARM, Mali-G715, OpenGL ES 3.2)"),
    ("Google Inc. (ARM)",      "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)"),
    ("Google Inc. (ARM)",      "ANGLE (ARM, Mali-G76, OpenGL ES 3.2)"),
    ("Google Inc. (Samsung)",  "ANGLE (Samsung, Xclipse 940, Vulkan 1.3.255)"),
    ("Google Inc. (Intel)",    "ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL ES 3.2)"),
    ("Apple Inc.",             "Apple GPU"),
]

_STATE_FILE = Path(__file__).resolve().parents[1] / "logs" / "fingerprint_state.json"


def _load_state() -> dict[str, Any]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        # Stealth should never fail hard because state persistence failed.
        pass


def _pick_rotating_value(values: list[Any], state: dict[str, Any], key: str) -> Any:
    """Pick next value in a persisted cycle to avoid immediate repeats across runs."""
    if not values:
        raise ValueError(f"No values supplied for {key}")

    last_idx = state.get(key)
    if not isinstance(last_idx, int) or last_idx < 0 or last_idx >= len(values):
        # Random starting point on first use; deterministic rotation afterwards.
        idx = random.randint(0, len(values) - 1)
    else:
        idx = (last_idx + 1) % len(values)

    state[key] = idx
    return values[idx]

# ── Static stealth patches (always applied) ─────────────────────────

_STATIC_SCRIPTS: list[str] = [
    # 1. Hide webdriver flag
    """Object.defineProperty(navigator, 'webdriver', {get: () => undefined});""",

    # 2. Fake plugins array (mobile Chrome has 0 plugins, which is legit)
    """Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });""",

    # 3. Fake languages
    """Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });""",

    # 4. Permissions query override
    """const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);""",

    # 5. Prevent canvas fingerprint detection
    """const toBlob = HTMLCanvasElement.prototype.toBlob;
    const toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toBlob = function() {
        const shift = { r: Math.floor(Math.random() * 10) - 5,
                        g: Math.floor(Math.random() * 10) - 5,
                        b: Math.floor(Math.random() * 10) - 5 };
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] += shift.r;
                imageData.data[i+1] += shift.g;
                imageData.data[i+2] += shift.b;
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return toBlob.apply(this, arguments);
    };""",
]


def inject_stealth(page: Page, fingerprint: dict | None = None) -> None:
    """Inject stealth + randomised fingerprint patches into a Playwright page.

    Picks a fresh carrier, connection type, and WebGL profile every call so
    each browser session presents a distinct fingerprint to tracking systems.
    """
    fp = fingerprint or {}
    ua: str = fp.get("user_agent", "")
    is_ios = "iPhone" in ua or "iPad" in ua
    is_safari_only = is_ios and "CriOS" not in ua and "Version/" in ua  # pure Safari UA

    # Carrier is chosen by device_manager per attempt and stored in fingerprint["_carrier"].
    # Fallback to random.choice only if called without a pre-built fingerprint.
    carrier = fp.get("_carrier") or random.choice(_CARRIERS)
    # Connection profile cycles through cellular-only profiles; WebGL stays random.
    state = _load_state()
    conn = _pick_rotating_value(_CONNECTION_PROFILES, state, "connection_idx")
    webgl_vendor, webgl_renderer = random.choice(_WEBGL_PROFILES)
    _save_state(state)

    scripts: list[str] = list(_STATIC_SCRIPTS)

    # Chrome runtime — present for Chrome/CriOS/SamsungBrowser, absent for pure Safari
    if not is_safari_only:
        scripts.append("window.chrome = { runtime: {} };")

    # WebGL spoof — randomised per session
    scripts.append(
        f"""const _glGetParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {{
    if (parameter === 37445) return '{webgl_vendor}';
    if (parameter === 37446) return '{webgl_renderer}';
    return _glGetParam.call(this, parameter);
}};""")

    # navigator.connection spoof
    scripts.append(
        f"""(() => {{
    const conn = {{
        label: '{conn["label"]}',
        connectionType: '{conn["label"]}',
        effectiveType: '{conn["effectiveType"]}',
        type: '{conn["type"]}',
        downlink: {conn["downlink"]},
        rtt: {conn["rtt"]},
        saveData: false,
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
    }};
    const navProto = Object.getPrototypeOf(navigator);
    try {{ Object.defineProperty(navProto, 'connection', {{ get: () => conn, configurable: true }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'connection', {{ get: () => conn, configurable: true }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navProto, 'connectionType', {{ get: () => '{conn["label"]}', configurable: true }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'connectionType', {{ get: () => '{conn["label"]}', configurable: true }}); }} catch (e) {{}}
    try {{ window.__fp_connection = conn; }} catch (e) {{}}
    try {{ window.__fp_connection_type = '{conn["label"]}'; }} catch (e) {{}}
}})();""")

    # Carrier/ISP spoof (checked by some advanced JS fingerprinters)
    scripts.append(
        f"""(() => {{
    const navProto = Object.getPrototypeOf(navigator);
    try {{ Object.defineProperty(navProto, 'carrier', {{ get: () => '{carrier}', configurable: true }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'carrier', {{ get: () => '{carrier}', configurable: true }}); }} catch (e) {{}}
    try {{ window.__fp_carrier = '{carrier}'; }} catch (e) {{}}
}})();""")

    # iOS-specific overrides
    if is_ios:
        scripts.append(
            "Object.defineProperty(navigator, 'platform', { get: () => 'iPhone', configurable: true });"
        )
        if is_safari_only:
            scripts.append(
                "try { window.safari = { pushNotification: { permission: () => ({ deviceToken: null, permission: 'denied' }) } }; } catch(e) {}"
            )

    combined = "\n".join(scripts)
    page.add_init_script(combined)
    log.info("stealth.injected", carrier=carrier, connection=conn["label"],
             network_type=conn["type"], webgl=webgl_vendor.split("(")[-1].rstrip(")"), ios=is_ios)


# Keep the old name as an alias for any external callers
STEALTH_SCRIPTS = _STATIC_SCRIPTS


# ── Human-like interactions ──────────────────────────────────────────

def human_type(page: Page, selector: str, text: str, config: dict) -> None:
    """Type text character-by-character with random delays."""
    delays = config.get("delays", {})
    min_d = delays.get("min_typing_delay", 0.04)
    max_d = delays.get("max_typing_delay", 0.12)

    element = page.locator(selector)
    element.click()
    random_pause(0.1, 0.3)

    for char in text:
        element.press_sequentially(char, delay=random.uniform(min_d, max_d) * 1000)
        # Occasional micro-pause (simulates thinking)
        if random.random() < 0.05:
            time.sleep(random.uniform(0.2, 0.6))

    log.debug("stealth.typed", selector=selector, length=len(text))


def human_click(page: Page, selector: str) -> None:
    """Click with slight random offset to avoid pixel-perfect clicks."""
    locator = page.locator(selector)
    box = locator.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        y = box["y"] + box["height"] * random.uniform(0.2, 0.8)
        page.mouse.click(x, y)
    else:
        locator.click()
    log.debug("stealth.clicked", selector=selector)


def random_scroll(page: Page) -> None:
    """Perform a random scroll to mimic natural browsing."""
    distance = random.randint(100, 400)
    direction = random.choice([1, -1])
    page.mouse.wheel(0, distance * direction)
    time.sleep(random.uniform(0.3, 0.8))
    log.debug("stealth.scrolled", distance=distance * direction)


def random_mouse_move(page: Page) -> None:
    """Move mouse to a random viewport position."""
    vp = page.viewport_size or {"width": 412, "height": 915}
    x = random.randint(50, vp["width"] - 50)
    y = random.randint(50, vp["height"] - 50)
    page.mouse.move(x, y, steps=random.randint(5, 15))
    log.debug("stealth.mouse_moved", x=x, y=y)


def random_pause(min_s: float = 0.5, max_s: float = 2.0) -> None:
    """Sleep for a random duration between min_s and max_s seconds."""
    duration = random.uniform(min_s, max_s)
    time.sleep(duration)


def action_delay(config: dict) -> None:
    """Wait a human-like pause between form actions."""
    delays = config.get("delays", {})
    min_d = delays.get("min_action_delay", 0.5)
    max_d = delays.get("max_action_delay", 2.0)
    random_pause(min_d, max_d)
