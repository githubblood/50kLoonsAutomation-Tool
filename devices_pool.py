"""
devices_pool.py
───────────────
Pre-defined pool of real-world Android device fingerprints.

Each entry mirrors a physical device so the browser context looks
authentic to server-side fingerprinting checks.  The pool is used by
`utils.device_manager` when the sheet row doesn't specify a custom device.
"""

DEVICE_POOL: list[dict] = [
    # ── Google Pixel Series ──────────────────────────────────────────
    {
        "model": "Pixel 7",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Pixel 7 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 892},
        "device_scale_factor": 3.5,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Pixel 8",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 932},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Pixel 8 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 8 Pro) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 448, "height": 998},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Pixel 9",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 9) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 924},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },

    # ── Samsung Galaxy S Series ──────────────────────────────────────
    {
        "model": "Galaxy S23",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy S23 Ultra",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 824},
        "device_scale_factor": 3.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy S24",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S921B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Galaxy S24 Ultra",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S928B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 824},
        "device_scale_factor": 3.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },

    # ── Samsung Galaxy A Series ──────────────────────────────────────
    {
        "model": "Galaxy A54",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A546B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy A15",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A156B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 854},
        "device_scale_factor": 2.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── OnePlus ──────────────────────────────────────────────────────
    {
        "model": "OnePlus 12",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; CPH2583) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 919},
        "device_scale_factor": 3.5,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Xiaomi / Redmi ───────────────────────────────────────────────
    {
        "model": "Redmi Note 13 Pro",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; 2312DRA50G) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 393, "height": 873},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Xiaomi 14",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; 2311DRK48C) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 393, "height": 851},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Nothing / Motorola ───────────────────────────────────────────
    {
        "model": "Nothing Phone (2)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; A065) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Moto G84",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; motorola edge 40) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.6422.165 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Samsung Galaxy — Samsung Browser ────────────────────────────
    {
        "model": "Galaxy S24 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; SM-S921B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "15",
    },
    {
        "model": "Galaxy S23 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-S911B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },
    {
        "model": "Galaxy A54 (Samsung Browser)",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-A546B) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/23.0 Chrome/115.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "android_version": "14",
    },

    # ── Apple iPhone — Safari (iOS) ──────────────────────────────────
    {
        "model": "iPhone 16 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 402, "height": 874},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 16",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/129.0.6668.46 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 15 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 393, "height": 852},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 15",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/124.0.6367.82 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 14 Pro",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 393, "height": 852},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 14",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/121.0.6167.66 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 13",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/16.6 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
    {
        "model": "iPhone 13 mini",
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 375, "height": 812},
        "device_scale_factor": 3.0,
        "is_mobile": True,
        "has_touch": True,
        "os": "ios",
    },
]
