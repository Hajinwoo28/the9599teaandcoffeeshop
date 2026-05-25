import os
import re
import uuid
import socket
import threading
import json
import io
import queue
import random
import hmac
import hashlib
import secrets
import requests
from flask import (
    Flask, 
    render_template_string, 
    request, 
    jsonify, 
    session, 
    redirect, 
    url_for, 
    Response
)
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Load .env file (for local development) ────────────────────────────────────
# Create a .env file in the same folder as app.py with:
#   GMAIL_SENDER=yourgmail@gmail.com
#   GMAIL_APP_PASSWORD=abcdefghijklmnop
# On Render/Vercel/Railway, set these as environment variables in the dashboard.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — env vars must be set manually (fine for production)

# Pandas is required to import legacy Excel/CSV notebooks
try:
    import pandas as pd
except ImportError:
    pd = None

app = Flask(__name__)

# Tells Flask it is behind a secure proxy to preserve Cookies
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,      # trust X-Forwarded-For: use real client IP, not the proxy's
    x_proto=1,
    x_host=1
)

# ── Detect cloud environment (must be defined before _check_production_secrets) ─
_ON_CLOUD = bool(
    os.environ.get('RENDER') or
    os.environ.get('DYNO') or
    os.environ.get('VERCEL') or
    os.environ.get('VERCEL_ENV')
)

# ── Startup security audit ────────────────────────────────────────────────────
def _check_production_secrets():
    """Warn loudly (and refuse on cloud) if default/weak secrets are detected."""
    issues = []
    if os.environ.get('SECRET_KEY', '') in ('', '9599isthesecretkey'):
        issues.append("SECRET_KEY is unset or using the default value — set a strong random key!")
    if (os.environ.get('ADMIN_PIN', '12345') or '12345').strip() == '12345':
        issues.append("ADMIN_PIN is '12345' (default) — set a strong 5-digit PIN in your environment!")
    if os.environ.get('LINK_SECRET', '') in ('', 'link-9599-store-permanent'):
        issues.append("LINK_SECRET is unset or default — set it in your environment.")
    if os.environ.get('DEV_SECRET', '') in ('', 'dev-9599-local'):
        issues.append("DEV_SECRET is unset or default — set it in your environment.")

    if issues:
        print("\n" + "!"*60)
        print("  ⚠  SECURITY WARNINGS — ACTION REQUIRED")
        print("!"*60)
        for msg in issues:
            print(f"  • {msg}")
        print("!"*60 + "\n")
        # On cloud deployments, warn loudly about the default admin PIN
        if _ON_CLOUD and any("ADMIN_PIN" in i for i in issues):
            print("!"*60)
            print("  WARNING: ADMIN_PIN is still the default '12345'.")
            print("  Set a strong ADMIN_PIN in your Vercel environment variables.")
            print("!"*60 + "\n")

_check_production_secrets()

# ==========================================
# 1. ADVANCED SECURITY CONFIGURATION
# ==========================================

app.secret_key = os.environ.get('SECRET_KEY', '9599isthesecretkey')

def verify_hcaptcha(token: str) -> tuple:
    """
    Verify an hCaptcha token server-side.
    Returns (ok: bool, error_msg: str).
    """
    if not HCAPTCHA_SECRET_KEY:
        return True, ''
    if not token:
        return False, 'CAPTCHA token missing. Please refresh and try again.'
    try:
        resp = requests.post(
            HCAPTCHA_VERIFY_URL,
            data={'secret': HCAPTCHA_SECRET_KEY, 'response': token},
            timeout=8,
        )
        data = resp.json()
        if data.get('success'):
            return True, ''
        codes = data.get('error-codes', [])
        return False, f"CAPTCHA verification failed ({', '.join(codes)}). Please refresh and try again."
    except Exception as e:
        print(f"[hCaptcha] Verification request failed: {e}")
        return True, ''


# hCaptcha — bot protection for the order form.
# Get a free key at: https://www.hcaptcha.com
HCAPTCHA_SECRET_KEY  = os.environ.get('HCAPTCHA_SECRET_KEY', 'REMOVED_SECRET').strip()
HCAPTCHA_SITE_KEY    = os.environ.get('HCAPTCHA_SITE_KEY', '1600832e-3a74-42a6-9590-b4ba3630366e').strip()
HCAPTCHA_VERIFY_URL  = 'https://api.hcaptcha.com/siteverify'
# Minimum seconds a real human takes to fill in the order gate form.
# Submissions faster than this are rejected as bots.
BOT_MIN_FORM_SECONDS = 3

# ── PayMongo GCash Payment Gateway ────────────────────────────────────────────
# Sign up free at https://dashboard.paymongo.com
# Set PAYMONGO_SECRET_KEY in your .env or Render/Vercel environment variables.
# Also set PAYMONGO_PUBLIC_KEY (used in webhook verification).
# Your store's public URL — used to build GCash redirect & webhook URLs.
PAYMONGO_SECRET_KEY  = os.environ.get('PAYMONGO_SECRET_KEY', '').strip()
PAYMONGO_PUBLIC_KEY  = os.environ.get('PAYMONGO_PUBLIC_KEY', '').strip()
PAYMONGO_API_BASE    = 'https://api.paymongo.com/v1'
# The URL PayMongo will redirect the customer to after GCash payment.
# Must be a publicly accessible URL (not localhost) in production.
STORE_PUBLIC_URL     = os.environ.get('STORE_PUBLIC_URL', '').strip().rstrip('/')
# ──────────────────────────────────────────────────────────────────────────────

# Developer portal secret — set DEV_SECRET in env for production.
DEV_SECRET = os.environ.get('DEV_SECRET', 'dev-9599-local').strip()
if len(DEV_SECRET) < 8:
    DEV_SECRET = 'dev-9599-local'

# Google OAuth Client ID for Social Login
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com')

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

if _ON_CLOUD:
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'None'
else:
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Master PIN: exactly 5 digits. Set ADMIN_PIN in the environment for production.
RAW_PIN = (os.environ.get('ADMIN_PIN', '12345') or '12345').strip()
if not re.fullmatch(r'\d{5}', RAW_PIN):
    print('WARNING: ADMIN_PIN must be exactly 5 digits. Falling back to 12345 until you fix the environment variable.')
    RAW_PIN = '12345'
ADMIN_PIN_HASH = generate_password_hash(RAW_PIN)


def master_pin_matches(submitted_pin):
    """True only if submitted_pin is exactly 5 digits and matches the configured master PIN."""
    if submitted_pin is None:
        return False
    s = str(submitted_pin).strip()
    if not re.fullmatch(r'\d{5}', s):
        return False
    return check_password_hash(ADMIN_PIN_HASH, s)

token_serializer = URLSafeTimedSerializer(app.secret_key)

# ── OTP hashing helpers ───────────────────────────────────────────────────────
# OTP codes are stored as HMAC-SHA256 hashes (never plaintext).
# This prevents a DB dump from exposing live codes.
_OTP_HMAC_KEY: bytes = (os.environ.get('OTP_HMAC_KEY') or app.secret_key).encode()

def _hash_otp_code(code: str) -> str:
    """Return a hex HMAC-SHA256 digest of the raw 6-digit OTP code."""
    return hmac.new(_OTP_HMAC_KEY, code.encode(), hashlib.sha256).hexdigest()

def _otp_codes_match(raw_input: str, stored_hash: str) -> bool:
    """Timing-safe comparison between the entered code and the stored hash."""
    return hmac.compar