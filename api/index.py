"""
api/index.py — Vercel serverless entry point.

Vercel's @vercel/python runtime auto-detects a module-level WSGI callable
named `app` and serves it. We re-export the Flask app from the project root.

⚠️  IMPORTANT — read api/README_VERCEL.md
This entry point only serves the dashboard UI and the lightweight JSON
endpoints (`/`, `/status`). The automation engine itself (Playwright +
long-lived background threads) CANNOT run inside a Vercel function:
serverless invocations are stateless, have a read-only filesystem, time out
after a few seconds, and cannot install/launch a Chromium browser binary.

Pressing "Start" in a Vercel-hosted UI will fail at runtime. The engine must
run on a persistent container host (Railway / Fly.io / Render / VPS). See the
README for the recommended split-deployment architecture.
"""
import os
import sys

# Make the project root importable (app.py lives one level up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  -> Flask WSGI app, auto-served by Vercel

# Vercel looks for a callable named `app` (or `handler`). `app` is exported above.
