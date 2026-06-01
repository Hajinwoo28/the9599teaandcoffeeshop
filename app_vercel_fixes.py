"""
=============================================================
  9599 Tea & Coffee — app.py VERCEL FIX GUIDE
  Apply each section to your app.py in order.
=============================================================
"""

# ─────────────────────────────────────────────────────────────
# FIX 1: DB PATH — Replace your current DB path with this.
#
# BEFORE (causes 500 crash on Vercel — filesystem is read-only):
#   DB_PATH = "coffeeshop.db"        ← WRONG
#   DB_PATH = "instance/shop.db"     ← WRONG
#   DB_PATH = "database.db"          ← WRONG
#
# AFTER:
import os
DB_PATH = os.path.join("/tmp", "9599_coffeeshop.db")
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 2: LAZY DB INIT — Do NOT call init_db() at module level.
#
# BEFORE (crashes Vercel on cold start):
#   init_db()           ← DELETE THIS (if it's at module top-level)
#   create_tables()     ← DELETE THIS (if it's at module top-level)
#
# AFTER — Use Flask's before_request instead:
from flask import Flask, g
app = Flask(__name__)

DB_INITIALIZED = False

@app.before_request
def ensure_db():
    """Initialize DB on first request, not at import time."""
    global DB_INITIALIZED
    if not DB_INITIALIZED:
        init_db()          # your existing init function — just move it here
        DB_INITIALIZED = True
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 3: SECRET KEY — Must come from env var on Vercel.
#
# BEFORE:
#   app.secret_key = "some_hardcoded_key"   ← Works locally, risky
#
# AFTER:
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-fallback-change-this")
# In Vercel dashboard → Settings → Environment Variables, add: SECRET_KEY = <random_32char>
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 4: APP.RUN — Must be guarded. This is the #1 Vercel crash cause.
#
# BEFORE (causes FUNCTION_INVOCATION_FAILED):
#   app.run(debug=True)        ← at module level, CRASHES Vercel
#   app.run()                  ← even this at module level CRASHES Vercel
#
# AFTER — wrap in the guard at the VERY BOTTOM of app.py:
if __name__ == "__main__":
    app.run(debug=True)        # Only runs locally, Vercel never hits this
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 5: JSON ERROR HANDLERS — Fixes the SyntaxError in browser console.
#
# Add these near the top of app.py, right after app is created:
from flask import jsonify

@app.errorhandler(400)
def bad_request(e):
    return jsonify(error="Bad request", message=str(e)), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify(error="Unauthorized", message=str(e)), 401

@app.errorhandler(403)
def forbidden(e):
    return jsonify(error="Forbidden", message=str(e)), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify(error="Not found", message=str(e)), 404

@app.errorhandler(429)
def too_many_requests(e):
    return jsonify(error="Too many requests", message=str(e)), 429

@app.errorhandler(500)
def server_error(e):
    return jsonify(error="Internal server error", message=str(e)), 500
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 6: FLASK-LIMITER storage URI — if you use flask_limiter,
# the default in-memory storage resets on cold start which is fine,
# but it must NOT try to write to the filesystem.
#
# BEFORE (may crash on Vercel):
#   limiter = Limiter(app, storage_uri="memory://")  ← usually fine
#   limiter = Limiter(app, storage_uri="redis://...")  ← needs Redis env var
#
# SAFE for Vercel (in-memory, resets each cold start):
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"   # ← in-memory is fine for Vercel
)
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FIX 7: HCAPTCHA SECRET — Must come from env var.
#
# In Vercel dashboard → Settings → Environment Variables, add:
#   HCAPTCHA_SECRET  = your-secret-key
#   HCAPTCHA_SITEKEY = your-site-key
#
# In app.py:
HCAPTCHA_SECRET  = os.environ.get("HCAPTCHA_SECRET",  "0x0000000000000000000000000000000000000000")
HCAPTCHA_SITEKEY = os.environ.get("HCAPTCHA_SITEKEY", "10000000-ffff-ffff-ffff-000000000001")
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# FINAL STRUCTURE — Your app.py bottom should look like this:
#
#   ... all your routes ...
#
#   # Error handlers (Fix 5)
#   @app.errorhandler(404) ...
#   @app.errorhandler(500) ...
#
#   if __name__ == "__main__":    # Fix 4 — MUST be the very last thing
#       app.run(debug=True)
#
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# THE api/dev/* ERRORS (404, 401, 500 in browser console)
#
# These come from Vercel's own Toolbar/Speed Insights scripts
# injected into the page — NOT from your Flask routes.
# They are HARMLESS to your app and appear in ALL Vercel projects.
# You can ignore them, or disable Vercel Analytics in:
#   Vercel Dashboard → Your Project → Settings → Analytics → Disable
# ─────────────────────────────────────────────────────────────
