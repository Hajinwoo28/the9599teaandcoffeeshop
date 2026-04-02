import os
import re
import uuid
import socket
import threading
import json
import io
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

# Pandas is required to import legacy Excel/CSV notebooks
try:
    import pandas as pd
except ImportError:
    pd = None

app = Flask(__name__)

# Tells Flask it is behind a secure proxy to preserve Cookies
app.wsgi_app = ProxyFix(
    app.wsgi_app, 
    x_proto=1, 
    x_host=1
)

# ==========================================
# 1. ADVANCED SECURITY CONFIGURATION
# ==========================================

app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-9599-key')

# Google OAuth Client ID for Social Login
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com')

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

if os.environ.get('RENDER') or os.environ.get('DYNO'):
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

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "100 per hour"]
)

@app.after_request
def add_header(response):
    """Prevent the browser from caching API data."""
    if 'api' in request.path:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response

# ==========================================
# 2. CLOUD & LOCAL CONFIGURATION
# ==========================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, 'milktea_system.db')

database_url = os.environ.get('DATABASE_URL', f'sqlite:///{DEFAULT_DB_PATH}')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_ph_time():
    return datetime.utcnow() + timedelta(hours=8)

# ==========================================
# STORE SCHEDULE (permanent, no token needed)
# Mon–Fri: 10:00 AM – 7:00 PM
# Sat–Sun: 3:00 PM – 8:00 PM
# Access blocked 1 hour before closing.
# ==========================================

STORE_SCHEDULE = {
    0: (10, 0, 19, 0),   # Monday
    1: (10, 0, 19, 0),   # Tuesday
    2: (10, 0, 19, 0),   # Wednesday
    3: (10, 0, 19, 0),   # Thursday
    4: (10, 0, 19, 0),   # Friday
    5: (10, 0, 19, 0),   # Saturday
    6: (10, 0, 19, 0),   # Sunday
}

def get_schedule_for_day(dow):
    """Read schedule for a day from DB, falling back to STORE_SCHEDULE defaults."""
    try:
        entry = StoreScheduleEntry.query.filter_by(day_of_week=dow).first()
        if entry:
            return (entry.open_hour, entry.open_minute, entry.close_hour, entry.close_minute)
    except Exception:
        pass
    return STORE_SCHEDULE.get(dow, (10, 0, 19, 0))

def get_store_status():
    """
    Returns a dict:
      open (bool)        – store is currently open and accepting orders
      open_time (str)    – e.g. "10:00 AM"
      close_time (str)   – e.g. "7:00 PM"  (last-order cutoff = 1h before actual close)
      next_open (str)    – human-readable next opening, e.g. "Saturday at 3:00 PM"
      closing_soon (bool)– within 1 hour of cutoff
    """
    now = get_ph_time()
    dow = now.weekday()          # 0=Mon … 6=Sun
    oh, om, ch, cm = get_schedule_for_day(dow)

    open_dt  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    cutoff   = close_dt - timedelta(hours=1)   # last order 1h before closing

    is_open       = open_dt <= now < cutoff
    closing_soon  = cutoff <= now < close_dt

    def fmt(h, m):
        suffix = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"

    open_str  = fmt(oh, om)
    close_str = fmt(ch, cm)
    cutoff_str = fmt(cutoff.hour, cutoff.minute)

    # Find next opening (always tomorrow relative to today)
    day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    next_dow = (dow + 1) % 7
    noh, nom, _, _ = get_schedule_for_day(next_dow)
    next_open_str = f"{day_names[next_dow]} at {fmt(noh, nom)}"

    return {
        "open": is_open,
        "closing_soon": closing_soon,
        "open_time": open_str,
        "close_time": close_str,
        "cutoff_time": cutoff_str,
        "next_open": next_open_str,
        "day": day_names[dow],
    }

def log_audit(action, details=""):
    try:
        new_log = AuditLog(action=action, details=details)
        db.session.add(new_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Audit Log Failed: {str(e)}")

# ==========================================
# 3. DATABASE MODELS
# ==========================================

class Reservation(db.Model):
    __tablename__ = 'reservations'
    id = db.Column(db.Integer, primary_key=True)
    reservation_code = db.Column(db.String(8), unique=True, nullable=False, default=lambda: str(uuid.uuid4())[:8].upper())
    patron_name = db.Column(db.String(100), nullable=False)
    patron_email = db.Column(db.String(120), nullable=False)
    total_investment = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Waiting Confirmation')
    pickup_time = db.Column(db.String(50), nullable=False)
    order_source = db.Column(db.String(30), default='Online') 
    created_at = db.Column(db.DateTime, default=get_ph_time)
    infusions = db.relationship('Infusion', backref='reservation', lazy=True, cascade="all, delete-orphan")

class Infusion(db.Model):
    __tablename__ = 'infusions'
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservations.id'), nullable=False)
    foundation = db.Column(db.String(100), nullable=False)
    sweetener = db.Column(db.String(100), nullable=False, default='100% Sugar')
    ice_level = db.Column(db.String(50), nullable=False, default='Normal Ice')
    pearls = db.Column(db.String(100), nullable=False, default='Walk-In')
    cup_size = db.Column(db.String(20), nullable=False, default='16 oz')
    addons = db.Column(db.String(200), nullable=False, default='')
    item_total = db.Column(db.Float, nullable=False, default=0.0)

class MenuItem(db.Model):
    __tablename__ = 'menu_items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    letter = db.Column(db.String(2), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    is_out_of_stock = db.Column(db.Boolean, nullable=False, default=False)

class Ingredient(db.Model):
    __tablename__ = 'ingredients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    unit = db.Column(db.String(20), nullable=False)
    stock = db.Column(db.Float, nullable=False, default=0.0)
    category = db.Column(db.String(50), nullable=False, default='Other')

class RecipeItem(db.Model):
    __tablename__ = 'recipe_items'
    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id', ondelete='CASCADE'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredients.id', ondelete='CASCADE'), nullable=False)
    quantity_required = db.Column(db.Float, nullable=False)

class Expense(db.Model):
    __tablename__ = 'expenses'
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=get_ph_time)

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=get_ph_time)

class CustomerLog(db.Model):
    __tablename__ = 'customer_logs'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    gmail = db.Column(db.String(120), nullable=True, default='')
    phone = db.Column(db.String(30), nullable=True, default='')
    order_source = db.Column(db.String(30), nullable=True, default='Online')
    order_total = db.Column(db.Float, nullable=True, default=0.0)
    items = db.Column(db.Text, nullable=True, default='')
    pickup_time = db.Column(db.String(50), nullable=True, default='')
    created_at = db.Column(db.DateTime, default=get_ph_time)

class PermissionRequest(db.Model):
    __tablename__ = 'permission_requests'
    id = db.Column(db.Integer, primary_key=True)
    request_code = db.Column(db.String(20), unique=True, nullable=False)
    customer_name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255), nullable=False, default='')
    message = db.Column(db.String(500), nullable=False, default='')
    granted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=get_ph_time)

class SystemState(db.Model):
    __tablename__ = 'system_state'
    id = db.Column(db.Integer, primary_key=True)
    active_session_id = db.Column(db.String(100))
    last_ping = db.Column(db.DateTime)

class ClosedDay(db.Model):
    __tablename__ = 'closed_days'
    id = db.Column(db.Integer, primary_key=True)
    date_str = db.Column(db.String(10), unique=True, nullable=False)  # 'YYYY-MM-DD'

class StoreScheduleEntry(db.Model):
    __tablename__ = 'store_schedule'
    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.Integer, unique=True, nullable=False)  # 0=Mon … 6=Sun
    open_hour = db.Column(db.Integer, nullable=False, default=10)
    open_minute = db.Column(db.Integer, nullable=False, default=0)
    close_hour = db.Column(db.Integer, nullable=False, default=19)
    close_minute = db.Column(db.Integer, nullable=False, default=0)

# ==========================================
# 4. FRONTEND HTML TEMPLATES
# ==========================================

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/jpeg" href="/static/images/9599.jpg">
<title>Admin Login | 9599 Tea &amp; Coffee</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
<style>
:root{
  --brown:#7B4F2E; --brown-dark:#3D2410; --brown-mid:#A0724A;
  --cream:#F0EDE4; --cream-dark:#E2DAC8; --tan:#C4A882;
  --red:#C0392B; --green:#27AE60;
  --text:#2A1505; --muted:#8D6E55;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Nunito',sans-serif;-webkit-tap-highlight-color:transparent;}
body{background:linear-gradient(160deg,var(--brown-dark) 0%,var(--brown) 60%,var(--brown-mid) 100%);display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px;}
.wrap{width:100%;max-width:380px;}
.logo-area{text-align:center;margin-bottom:26px;}
.logo-img-wrap{width:76px;height:76px;border-radius:50%;background:var(--cream);border:4px solid var(--tan);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,0.25);}
.logo-img-wrap img{width:100%;height:100%;object-fit:cover;}
.logo-img-wrap .logo-fallback{font-size:2rem;}
.logo-area h1{font-family:'Playfair Display',serif;font-size:1.5rem;font-weight:900;color:var(--cream);letter-spacing:0.5px;}
.logo-area p{font-size:0.72rem;color:var(--tan);font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-top:3px;}
.card{background:var(--cream);border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.3);padding:30px 26px;}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--brown-dark);color:var(--tan);font-size:0.66rem;font-weight:800;padding:4px 12px;border-radius:20px;margin-bottom:14px;letter-spacing:1px;text-transform:uppercase;}
h2{font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:900;color:var(--text);margin-bottom:3px;}
.sub{font-size:0.82rem;color:var(--muted);font-weight:600;margin-bottom:20px;}
.lbl{font-size:0.68rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:7px;display:block;}
.inp{width:100%;padding:13px;border:2px solid var(--cream-dark);border-radius:12px;font-size:1.55rem;text-align:center;letter-spacing:10px;margin-bottom:16px;outline:none;font-weight:900;color:var(--brown-dark);background:#fff;font-family:'Nunito',sans-serif;transition:border-color 0.2s;}
.inp:focus{border-color:var(--brown);}
.btn{width:100%;background:linear-gradient(135deg,var(--brown) 0%,var(--brown-dark) 100%);color:var(--cream);border:none;padding:13px;border-radius:12px;font-weight:800;font-size:0.95rem;cursor:pointer;display:flex;justify-content:center;align-items:center;gap:10px;font-family:'Nunito',sans-serif;box-shadow:0 4px 16px rgba(61,36,16,0.35);transition:opacity 0.2s;}
.btn:hover{opacity:0.9;}
.err{background:#FFF0F0;color:var(--red);padding:10px 14px;border-radius:10px;font-size:0.82rem;font-weight:700;margin-bottom:16px;border:1.5px solid #F5C6C6;display:flex;align-items:center;gap:8px;}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo-area">
    <div class="logo-img-wrap">
      <img src="/static/images/9599.jpg" alt="9599" onerror="this.style.display='none';this.nextElementSibling.style.display='block';">
      <span class="logo-fallback" style="display:none;">☕</span>
    </div>
    <h1>9599 Tea &amp; Coffee</h1>
    <p>Parne Na! — Management System</p>
  </div>
  <div class="card">
    <div class="pill"><i class="fas fa-shield-alt"></i> Admin Panel</div>
    <h2>Admin Access</h2>
    <p class="sub">Enter the 5-digit master PIN to continue</p>
    {% if error %}<div class="err"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>{% endif %}
    <form method="POST">
      <label class="lbl">Master PIN (5 digits)</label>
      <input type="password" name="pin" class="inp" placeholder="•••••" required autofocus maxlength="5" minlength="5" pattern="[0-9]{5}" inputmode="numeric" autocomplete="one-time-code" title="Enter exactly 5 digits">
      <button type="submit" class="btn"><i class="fas fa-lock"></i> Login Securely</button>
    </form>
  </div>
</div>
</body>
</html>
"""


EMPLOYEE_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/jpeg" href="/static/images/9599.jpg">
<title>Employee Login | 9599 Tea &amp; Coffee</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
<style>
:root{
  --teal:#0D7A6A; --teal-dark:#094F44; --teal-mid:#12937E;
  --cream:#F0F5F4; --cream-dark:#D0E4E0; --gold:#C8A84B;
  --red:#C0392B; --green:#27AE60;
  --text:#0A2925; --muted:#557570;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Nunito',sans-serif;-webkit-tap-highlight-color:transparent;}
body{background:linear-gradient(160deg,var(--teal-dark) 0%,var(--teal) 60%,var(--teal-mid) 100%);display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px;}
.wrap{width:100%;max-width:380px;}
.logo-area{text-align:center;margin-bottom:26px;}
.logo-img-wrap{width:76px;height:76px;border-radius:50%;background:var(--cream);border:4px solid var(--gold);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,0.25);}
.logo-img-wrap img{width:100%;height:100%;object-fit:cover;}
.logo-img-wrap .logo-fallback{font-size:2rem;}
.logo-area h1{font-family:'Playfair Display',serif;font-size:1.5rem;font-weight:900;color:#fff;letter-spacing:0.5px;}
.logo-area p{font-size:0.72rem;color:var(--gold);font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-top:3px;}
.card{background:var(--cream);border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.3);padding:30px 26px;}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--teal-dark);color:var(--gold);font-size:0.66rem;font-weight:800;padding:4px 12px;border-radius:20px;margin-bottom:14px;letter-spacing:1px;text-transform:uppercase;}
h2{font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:900;color:var(--text);margin-bottom:3px;}
.sub{font-size:0.82rem;color:var(--muted);font-weight:600;margin-bottom:20px;}
.lbl{font-size:0.68rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:7px;display:block;}
.inp{width:100%;padding:13px;border:2px solid var(--cream-dark);border-radius:12px;font-size:1.55rem;text-align:center;letter-spacing:10px;margin-bottom:16px;outline:none;font-weight:900;color:var(--teal-dark);background:#fff;font-family:'Nunito',sans-serif;transition:border-color 0.2s;}
.inp:focus{border-color:var(--teal);}
.btn{width:100%;background:linear-gradient(135deg,var(--teal) 0%,var(--teal-dark) 100%);color:#fff;border:none;padding:13px;border-radius:12px;font-weight:800;font-size:0.95rem;cursor:pointer;display:flex;justify-content:center;align-items:center;gap:10px;font-family:'Nunito',sans-serif;box-shadow:0 4px 16px rgba(9,79,68,0.35);transition:opacity 0.2s;}
.btn:hover{opacity:0.9;}
.err{background:#FFF0F0;color:var(--red);padding:10px 14px;border-radius:10px;font-size:0.82rem;font-weight:700;margin-bottom:16px;border:1.5px solid #F5C6C6;display:flex;align-items:center;gap:8px;}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo-area">
    <div class="logo-img-wrap">
      <img src="/static/images/9599.jpg" alt="9599" onerror="this.style.display='none';this.nextElementSibling.style.display='block';">
      <span class="logo-fallback" style="display:none;">☕</span>
    </div>
    <h1>9599 Tea &amp; Coffee</h1>
    <p>Parne Na! — Employee Station</p>
  </div>
  <div class="card">
    <div class="pill"><i class="fas fa-user-tie"></i> Staff Access</div>
    <h2>Employee Access</h2>
    <p class="sub">Enter the 5-digit master PIN to continue</p>
    {% if error %}<div class="err"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>{% endif %}
    <form method="POST">
      <label class="lbl">Master PIN (5 digits)</label>
      <input type="password" name="pin" class="inp" placeholder="•••••" required autofocus maxlength="5" minlength="5" pattern="[0-9]{5}" inputmode="numeric" autocomplete="one-time-code" title="Enter exactly 5 digits">
      <button type="submit" class="btn"><i class="fas fa-sign-in-alt"></i> Login</button>
    </form>
  </div>
</div>
</body>
</html>
"""


EMPLOYEE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="icon" type="image/jpeg" href="/static/images/9599.jpg">
<title>Employee Station | 9599 Tea &amp; Coffee</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
<style>
:root{
  --teal:#0D7A6A; --teal-dark:#094F44; --teal-mid:#12937E; --teal-light:#E6F4F2;
  --gold:#C8A84B; --gold-light:#F7F0DC;
  --bg:#F0F5F4; --card:#FFFFFF; --border:#D0E4E0;
  --text:#0A2925; --muted:#557570;
  --red:#D32F2F; --green:#2E7D32; --orange:#E65100; --blue:#1565C0;
  --shadow:0 4px 20px rgba(13,122,106,0.1);
  --radius:14px; --nav-h:68px; --topbar-h:62px;
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Nunito',sans-serif;-webkit-tap-highlight-color:transparent;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);display:flex;flex-direction:column;}

#toast-container{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none;min-width:200px;}
.toast{background:var(--teal-dark);color:#fff;padding:10px 20px;border-radius:20px;font-weight:700;font-size:0.84rem;box-shadow:0 4px 16px rgba(13,122,106,0.3);text-align:center;}
.toast.error{background:var(--red);}
.toast.success{background:var(--green);}

.topbar{height:var(--topbar-h);background:var(--teal-dark);border-bottom:3px solid var(--teal-mid);display:flex;align-items:center;justify-content:space-between;padding:0 16px;flex-shrink:0;box-shadow:0 2px 12px rgba(9,79,68,0.3);z-index:100;}
.topbar-left{display:flex;align-items:center;gap:10px;}
.logo-circle{width:38px;height:38px;border-radius:50%;border:2px solid var(--gold);overflow:hidden;flex-shrink:0;background:#fff;display:flex;align-items:center;justify-content:center;}
.logo-circle img{width:100%;height:100%;object-fit:cover;}
.brand{font-family:'Playfair Display',serif;font-size:1rem;font-weight:900;color:#fff;line-height:1.1;}
.brand-sub{font-size:0.6rem;color:var(--gold);font-weight:700;letter-spacing:1.5px;text-transform:uppercase;}
.emp-pill{background:rgba(200,168,75,0.2);border:1px solid var(--gold);color:var(--gold);font-size:0.62rem;font-weight:800;padding:3px 10px;border-radius:20px;letter-spacing:0.5px;display:inline-flex;align-items:center;gap:4px;}
.topbar-right{display:flex;align-items:center;gap:10px;flex-shrink:0;}
.clock-chip{background:rgba(255,255,255,0.1);border:1px solid rgba(200,168,75,0.4);color:var(--gold);padding:5px 13px;border-radius:20px;font-size:0.8rem;font-weight:800;white-space:nowrap;}
.logout-btn{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.2);color:rgba(255,255,255,0.7);padding:5px 12px;border-radius:20px;font-size:0.72rem;font-weight:800;cursor:pointer;text-decoration:none;display:flex;align-items:center;gap:5px;}
.logout-btn:hover{background:rgba(255,255,255,0.15);}

.bottom-nav{height:var(--nav-h);background:var(--teal-dark);border-top:2px solid var(--teal-mid);display:flex;align-items:stretch;flex-shrink:0;z-index:100;}
.nav-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border:none;background:transparent;color:rgba(255,255,255,0.5);cursor:pointer;font-family:'Nunito',sans-serif;font-size:0.62rem;font-weight:800;letter-spacing:0.4px;text-transform:uppercase;padding:8px 4px;transition:color 0.15s,background 0.15s;}
.nav-btn i{font-size:1.2rem;}
.nav-btn.active{color:var(--gold);background:rgba(200,168,75,0.1);}
.nav-btn:hover:not(.active){color:rgba(255,255,255,0.8);}
.nav-badge{position:absolute;top:-2px;right:-4px;background:var(--red);color:#fff;border-radius:50%;min-width:16px;height:16px;padding:0 3px;font-size:0.58rem;font-weight:900;display:none;align-items:center;justify-content:center;border:2px solid var(--teal-dark);}
.nav-icon-wrap{position:relative;display:inline-block;}

.screens{flex:1;overflow:hidden;position:relative;}
.screen{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--bg);display:none;padding:0 0 calc(var(--nav-h)+12px);}
.screen.active{display:block;}

.page-header{padding:20px 16px 14px;background:var(--card);border-bottom:1.5px solid var(--border);position:sticky;top:0;z-index:50;}
.page-header h2{font-family:'Playfair Display',serif;font-size:1.25rem;font-weight:900;color:var(--teal-dark);display:flex;align-items:center;gap:9px;}
.page-header p{font-size:0.76rem;color:var(--muted);margin-top:3px;font-weight:600;}

.section{padding:14px 14px 0;}
.card{background:var(--card);border-radius:var(--radius);border:1.5px solid var(--border);box-shadow:var(--shadow);padding:16px;margin-bottom:14px;}
.card-title{font-size:0.8rem;font-weight:900;color:var(--teal-dark);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:12px;display:flex;align-items:center;gap:7px;}

.stat-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;}
.stat-chip{background:var(--card);border:1.5px solid var(--border);border-radius:12px;padding:12px 10px;text-align:center;box-shadow:var(--shadow);}
.stat-num{font-family:'Playfair Display',serif;font-size:1.6rem;font-weight:900;color:var(--teal-dark);line-height:1;}
.stat-lbl{font-size:0.63rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;margin-top:3px;}

.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;font-size:0.68rem;font-weight:800;white-space:nowrap;}
.badge-waiting{background:rgba(230,81,0,0.1);color:var(--orange);}
.badge-preparing{background:rgba(21,101,192,0.1);color:var(--blue);}
.badge-ready{background:rgba(200,168,75,0.15);color:#7A600A;}
.badge-completed{background:rgba(46,125,50,0.1);color:var(--green);}
.badge-online{background:rgba(13,122,106,0.1);color:var(--teal);}
.badge-walkin{background:rgba(21,101,192,0.1);color:var(--blue);}

/* ── Status dropdown pill ── */
.status-select{appearance:none;-webkit-appearance:none;border:none;outline:none;cursor:pointer;font-family:'Nunito',sans-serif;font-weight:800;font-size:0.72rem;padding:5px 28px 5px 12px;border-radius:20px;color:#fff;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 8px center;background-size:12px;min-width:110px;}
.status-select:focus{outline:2px solid rgba(255,255,255,0.4);outline-offset:1px;}
.status-select option{color:#111;background:#fff;font-weight:700;}
.status-select.sel-waiting{background-color:#E65100;}
.status-select.sel-preparing{background-color:#1565C0;}
.status-select.sel-ready{background-color:#F9A825;color:#3E2723;}
.status-select.sel-ready{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%233E2723' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");}
.status-select.sel-completed{background-color:#2E7D32;}
.status-select.sel-cancelled{background-color:#B71C1C;}

.order-list{display:flex;flex-direction:column;gap:10px;}
.order-card{background:var(--card);border:1.5px solid var(--border);border-radius:12px;padding:13px 14px;box-shadow:0 2px 8px rgba(13,122,106,0.07);}
.order-card.online-order{border-left:4px solid var(--teal);}
.order-card.walkin-order{border-left:4px solid var(--blue);}
.order-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;}
.order-code{font-family:'Playfair Display',serif;font-size:1rem;font-weight:900;color:var(--teal-dark);}
.order-name{font-size:0.82rem;font-weight:700;color:var(--text);margin-top:1px;}
.order-meta{font-size:0.72rem;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;}
.order-items{margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);}
.order-item-row{font-size:0.79rem;font-weight:600;color:var(--text);display:flex;justify-content:space-between;margin-bottom:3px;}
.order-item-mods{font-size:0.69rem;color:var(--muted);margin-bottom:4px;padding-left:8px;}
.order-total{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:8px;border-top:1.5px solid var(--border);}
.order-total-lbl{font-size:0.75rem;font-weight:800;color:var(--muted);text-transform:uppercase;}
.order-total-amt{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:900;color:var(--teal-dark);}
.order-actions{display:flex;gap:7px;margin-top:10px;flex-wrap:wrap;}
.btn-status{flex:1;min-width:100px;padding:9px 12px;border-radius:10px;border:none;font-family:'Nunito',sans-serif;font-weight:800;font-size:0.78rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:5px;transition:opacity 0.15s;}
.btn-preparing{background:var(--blue);color:#fff;}
.btn-ready{background:var(--gold);color:#0A2925;}
.btn-complete{background:var(--green);color:#fff;}
.btn-print{background:var(--teal-light);color:var(--teal-dark);border:1.5px solid var(--border);}
.btn-status:active{opacity:0.8;}

.filter-row{display:flex;gap:6px;overflow-x:auto;padding:12px 14px 2px;scrollbar-width:none;}
.filter-row::-webkit-scrollbar{display:none;}
.filter-tab{flex-shrink:0;padding:6px 14px;border-radius:20px;border:1.5px solid var(--border);background:var(--card);font-size:0.74rem;font-weight:800;color:var(--muted);cursor:pointer;white-space:nowrap;transition:all 0.15s;}
.filter-tab.active{background:var(--teal-dark);color:#fff;border-color:var(--teal-dark);}

.pos-layout{display:flex;height:calc(100vh - var(--topbar-h) - var(--nav-h));overflow:hidden;}
.pos-menu-area{flex:1;overflow-y:auto;padding:12px;background:var(--bg);}
.pos-sidebar{width:300px;flex-shrink:0;background:var(--card);border-left:1.5px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
@media(max-width:680px){.pos-layout{flex-direction:column;height:auto;overflow:visible;}.pos-menu-area{overflow-y:auto;max-height:40vh;padding-bottom:10px;}.pos-sidebar{width:100%;flex-shrink:0;border-left:none;border-top:2px solid var(--border);}}

.pos-cat-tabs{display:flex;gap:5px;overflow-x:auto;padding:0 0 10px;scrollbar-width:none;margin-bottom:4px;}
.pos-cat-tabs::-webkit-scrollbar{display:none;}
.cat-tab{flex-shrink:0;padding:6px 14px;border-radius:20px;border:1.5px solid var(--border);background:var(--card);font-size:0.73rem;font-weight:800;color:var(--muted);cursor:pointer;transition:all 0.15s;}
.cat-tab.active{background:var(--teal-dark);color:#fff;border-color:var(--teal-dark);}

.menu-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:9px;}
.menu-card{position:relative;background:var(--card);border:1.5px solid var(--border);border-radius:12px;padding:0 0 10px;cursor:pointer;transition:all 0.15s;text-align:center;box-shadow:0 2px 8px rgba(13,122,106,0.06);overflow:hidden;}
.menu-bs-tag{position:absolute;top:6px;left:6px;right:auto;z-index:2;background:rgba(200,168,75,0.95);color:#0A2925;font-size:0.58rem;font-weight:900;padding:3px 8px;border-radius:10px;letter-spacing:0.3px;}
.menu-card:active{transform:scale(0.97);opacity:0.85;}
.menu-card.oos{opacity:0.45;cursor:not-allowed;}
.menu-preview{width:100%;height:90px;object-fit:cover;display:block;border-radius:10px 10px 0 0;background:var(--teal-light);}
.menu-letter{width:100%;height:90px;border-radius:10px 10px 0 0;background:var(--teal-light);display:flex;align-items:center;justify-content:center;font-family:'Playfair Display',serif;font-size:2rem;font-weight:900;color:var(--teal-dark);}
.menu-card-body{padding:8px 8px 0;}
.menu-name{font-size:0.73rem;font-weight:800;color:var(--text);line-height:1.25;margin-bottom:4px;}
.menu-price{font-size:0.78rem;font-weight:900;color:var(--teal-mid);}
.menu-oos-tag{background:rgba(211,47,47,0.1);color:var(--red);font-size:0.62rem;font-weight:800;padding:2px 7px;border-radius:10px;margin-top:4px;display:inline-block;}

.cart-header{padding:12px 14px;border-bottom:1.5px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
.cart-title{font-size:0.85rem;font-weight:900;color:var(--teal-dark);display:flex;align-items:center;gap:6px;}
.cart-clear{background:none;border:none;color:var(--red);font-size:0.72rem;font-weight:800;cursor:pointer;padding:3px 7px;border-radius:6px;}
.cart-clear:hover{background:rgba(211,47,47,0.08);}
.cart-items{flex:1;overflow-y:auto;padding:10px 12px;}
.cart-item{display:flex;align-items:flex-start;gap:8px;padding:9px 0;border-bottom:1px solid var(--border);}
.cart-item:last-child{border-bottom:none;}
.cart-item-info{flex:1;min-width:0;}
.cart-item-name{font-size:0.79rem;font-weight:800;color:var(--text);line-height:1.3;}
.cart-item-mods{font-size:0.67rem;color:var(--muted);margin-top:1px;line-height:1.4;}
.cart-item-price{font-size:0.82rem;font-weight:900;color:var(--teal-dark);white-space:nowrap;margin-top:1px;}
.cart-item-del{background:none;border:none;color:var(--muted);cursor:pointer;padding:3px;border-radius:6px;font-size:0.9rem;flex-shrink:0;margin-top:1px;}
.cart-item-del:hover{color:var(--red);background:rgba(211,47,47,0.08);}
.cart-empty{text-align:center;padding:30px 14px;color:var(--muted);font-size:0.8rem;font-weight:600;}
.cart-empty i{font-size:2rem;display:block;margin-bottom:8px;opacity:0.3;}

.cart-footer{padding:12px 14px;border-top:1.5px solid var(--border);flex-shrink:0;}
.cart-total-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
.cart-total-lbl{font-size:0.75rem;font-weight:800;color:var(--muted);text-transform:uppercase;}
.cart-total-amt{font-family:'Playfair Display',serif;font-size:1.4rem;font-weight:900;color:var(--teal-dark);}
.name-inp{width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:10px;font-family:'Nunito',sans-serif;font-size:0.84rem;font-weight:700;outline:none;color:var(--text);margin-bottom:8px;background:#fff;}
.name-inp:focus{border-color:var(--teal);}
.btn-checkout{width:100%;padding:13px;border-radius:12px;border:none;background:var(--teal-dark);color:#fff;font-family:'Nunito',sans-serif;font-size:0.92rem;font-weight:900;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;letter-spacing:0.3px;transition:opacity 0.15s;}
.btn-checkout:active{opacity:0.85;}
.btn-checkout:disabled{opacity:0.5;cursor:not-allowed;}

/* ── TABLE LAYOUT (Live Orders screen) ── */
.live-section{padding:12px 14px 0;}
.live-card{background:var(--card);border:1.5px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:14px;overflow:hidden;}
.live-card-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1.5px solid var(--border);}
.live-card-title{font-size:0.82rem;font-weight:900;color:var(--teal-dark);display:flex;align-items:center;gap:7px;text-transform:uppercase;letter-spacing:0.6px;}
.live-card-title.perm-title{color:var(--red);}
.btn-refresh{background:var(--red);color:#fff;border:none;padding:6px 16px;border-radius:8px;font-family:'Nunito',sans-serif;font-size:0.76rem;font-weight:900;cursor:pointer;letter-spacing:0.3px;}
.btn-refresh:active{opacity:0.85;}
.live-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
.live-table{width:100%;border-collapse:collapse;font-size:0.78rem;}
.live-table th{padding:9px 12px;text-align:left;font-size:0.67rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;border-bottom:1.5px solid var(--border);white-space:nowrap;background:#FAFCFB;}
.live-table td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle;color:var(--text);font-weight:600;}
.live-table tr:last-child td{border-bottom:none;}
.live-table tr:hover td{background:rgba(13,122,106,0.03);}
.live-table .empty-row td{text-align:center;padding:20px;color:var(--muted);font-size:0.82rem;font-style:italic;}
.live-table .order-num{font-family:'Playfair Display',serif;font-weight:900;color:var(--teal-dark);font-size:0.85rem;white-space:nowrap;}
.live-table .items-cell{max-width:160px;font-size:0.73rem;color:var(--muted);line-height:1.4;}
.tbl-actions{display:flex;gap:5px;flex-wrap:wrap;}
.tbl-btn{padding:5px 11px;border-radius:8px;border:none;font-family:'Nunito',sans-serif;font-weight:800;font-size:0.71rem;cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:4px;transition:opacity 0.15s;}
.tbl-btn:active{opacity:0.8;}
.tbl-btn.preparing{background:var(--blue);color:#fff;}
.tbl-btn.ready{background:var(--gold);color:#0A2925;}
.tbl-btn.complete{background:var(--green);color:#fff;}
.tbl-btn.print{background:var(--teal-light);color:var(--teal-dark);border:1.5px solid var(--border);}
.tbl-btn.grant{background:rgba(46,125,50,0.12);color:var(--green);border:1.5px solid rgba(46,125,50,0.25);}
.perm-row-card{background:rgba(211,47,47,0.04);}

.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:flex-end;justify-content:center;z-index:9000;}
.modal-overlay.show{display:flex;}
.modal-sheet{background:#fff;width:100%;max-width:480px;border-radius:22px 22px 0 0;padding:20px 20px 30px;max-height:90vh;overflow-y:auto;animation:slideUp 0.25s ease;}
@keyframes slideUp{from{transform:translateY(50px);opacity:0;}to{transform:translateY(0);opacity:1;}}
.modal-handle{width:40px;height:4px;background:var(--border);border-radius:3px;margin:0 auto 16px;}
.modal-title{font-family:'Playfair Display',serif;font-size:1.15rem;font-weight:900;color:var(--teal-dark);margin-bottom:14px;}
.modal-label{font-size:0.72rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:6px;display:block;}
.option-row{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;}
.opt-btn{padding:6px 14px;border:1.5px solid var(--border);border-radius:20px;background:#fff;font-size:0.76rem;font-weight:800;color:var(--muted);cursor:pointer;transition:all 0.15s;font-family:'Nunito',sans-serif;}
.opt-btn.selected{background:var(--teal-dark);color:#fff;border-color:var(--teal-dark);}
.modal-actions{display:flex;gap:8px;margin-top:16px;}
.btn-modal-cancel{flex:1;padding:13px;border-radius:12px;border:1.5px solid var(--border);background:#fff;color:var(--muted);font-family:'Nunito',sans-serif;font-weight:800;font-size:0.88rem;cursor:pointer;}
.btn-modal-add{flex:2;padding:13px;border-radius:12px;border:none;background:var(--teal-dark);color:#fff;font-family:'Nunito',sans-serif;font-weight:900;font-size:0.92rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;}

.success-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);display:none;align-items:center;justify-content:center;z-index:9500;padding:20px;}
.success-overlay.show{display:flex;}
.success-card{background:#fff;border-radius:20px;padding:30px 24px;max-width:360px;width:100%;text-align:center;box-shadow:0 20px 50px rgba(0,0,0,0.15);}
.success-icon{width:64px;height:64px;border-radius:50%;background:rgba(46,125,50,0.1);border:2px solid rgba(46,125,50,0.3);display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:1.8rem;color:var(--green);}
.success-code{font-family:'Playfair Display',serif;font-size:2rem;font-weight:900;color:var(--teal-dark);letter-spacing:2px;margin:8px 0;}
.btn-done{width:100%;padding:13px;border-radius:12px;border:none;background:var(--teal-dark);color:#fff;font-family:'Nunito',sans-serif;font-weight:900;font-size:0.92rem;cursor:pointer;margin-top:14px;}
.btn-done-print{width:100%;padding:12px;border-radius:12px;border:1.5px solid var(--border);background:#fff;color:var(--teal-dark);font-family:'Nunito',sans-serif;font-weight:800;font-size:0.88rem;cursor:pointer;margin-top:8px;display:flex;align-items:center;justify-content:center;gap:6px;}
</style>
</head>
<body>
<div id="toast-container"></div>
<audio id="emp-audio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>

<header class="topbar">
  <div class="topbar-left">
    <div class="logo-circle">
      <img src="/static/images/9599.jpg" alt="9599" onerror="this.style.display='none';">
    </div>
    <div>
      <div class="brand">9599 Tea &amp; Coffee</div>
      <div class="brand-sub">Employee Station</div>
    </div>
    <div class="emp-pill" style="margin-left:4px;"><i class="fas fa-user-tie"></i> Staff</div>
  </div>
  <div class="topbar-right">
    <div id="clock" class="clock-chip">00:00 AM</div>
    <a href="/employee/logout" class="logout-btn"><i class="fas fa-sign-out-alt"></i> Logout</a>
  </div>
</header>

<div class="screens">

  <!-- ONLINE POS -->
  <div id="s-online" class="screen active">
    <div class="page-header">
      <h2><i class="fas fa-wifi"></i> Live Orders</h2>
      <p>Refreshes every 5 seconds — tap a status button to update</p>
    </div>

    <!-- Stats -->
    <div class="section">
      <div class="stat-row">
        <div class="stat-chip"><div class="stat-num" id="stat-waiting">0</div><div class="stat-lbl">Waiting</div></div>
        <div class="stat-chip"><div class="stat-num" id="stat-preparing">0</div><div class="stat-lbl">Preparing</div></div>
        <div class="stat-chip"><div class="stat-num" id="stat-ready">0</div><div class="stat-lbl">Ready</div></div>
      </div>
    </div>

    <!-- Permission Requests -->
    <div class="live-section">
      <div class="live-card">
        <div class="live-card-header">
          <span class="live-card-title perm-title"><i class="fas fa-hand-paper"></i> Permission Requests</span>
          <button class="btn-refresh" onclick="fetchPermReqs()"><i class="fas fa-sync-alt"></i> Refresh</button>
        </div>
        <div class="live-table-wrap">
          <table class="live-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Code</th>
                <th>Customer</th>
                <th>Message</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="perm-tbody">
              <tr class="empty-row"><td colspan="5">No pending requests</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Live Orders Table -->
    <div class="live-section">
      <div class="live-card">
        <div class="live-card-header">
          <span class="live-card-title"><i class="fas fa-receipt"></i> Active Orders</span>
          <div style="display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;">
            <button class="tbl-btn print active-filter" id="ftab-all" onclick="setFilter('All',this)" style="background:var(--teal-dark);color:#fff;border-color:var(--teal-dark);">All</button>
            <button class="tbl-btn print" id="ftab-waiting" onclick="setFilter('Waiting Confirmation',this)">⏳ Waiting</button>
            <button class="tbl-btn print" id="ftab-preparing" onclick="setFilter('Preparing',this)">🔥 Preparing</button>
            <button class="tbl-btn print" id="ftab-ready" onclick="setFilter('Ready for Pickup',this)">✅ Ready</button>
            <button class="tbl-btn print" id="ftab-done" onclick="setFilter('Completed',this)">🏁 Done</button>
          </div>
        </div>
        <div class="live-table-wrap">
          <table class="live-table">
            <thead>
              <tr>
                <th>Order #</th>
                <th>Source</th>
                <th>Name</th>
                <th>Time</th>
                <th>Total</th>
                <th>Items</th>
                <th>Status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="orders-tbody">
              <tr class="empty-row"><td colspan="8">No active orders</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

  <!-- WALK-IN POS -->
  <div id="s-pos" class="screen">
    <div class="page-header">
      <h2><i class="fas fa-cash-register"></i> Walk-In POS</h2>
      <p>Tap items to add to cart, then checkout</p>
    </div>
    <div class="pos-layout">
      <div class="pos-menu-area">
        <div class="pos-cat-tabs" id="cat-tabs"></div>
        <div class="menu-grid" id="menu-grid">
          <div style="grid-column:1/-1;text-align:center;padding:30px;color:var(--muted);font-weight:600;"><i class="fas fa-spinner fa-spin"></i> Loading menu…</div>
        </div>
      </div>
      <div class="pos-sidebar">
        <div class="cart-header">
          <div class="cart-title"><i class="fas fa-shopping-bag"></i> Cart <span id="cart-count" style="background:var(--teal-light);color:var(--teal-dark);border-radius:20px;padding:1px 8px;font-size:0.68rem;">0</span></div>
          <button class="cart-clear" onclick="clearCart()"><i class="fas fa-trash-alt"></i> Clear</button>
        </div>
        <div class="cart-items" id="cart-items">
          <div class="cart-empty"><i class="fas fa-shopping-bag"></i>Cart is empty.<br>Tap items to add.</div>
        </div>
        <div class="cart-footer">
          <div class="cart-total-row">
            <div class="cart-total-lbl">Total</div>
            <div class="cart-total-amt" id="cart-total">₱0.00</div>
          </div>
          <input type="text" class="name-inp" id="customer-name" placeholder="Customer name (optional)">
          <button class="btn-checkout" onclick="checkout()" id="checkout-btn" disabled>
            <i class="fas fa-check-circle"></i> Checkout
          </button>
        </div>
      </div>
    </div>
  </div>

</div>

<nav class="bottom-nav">
  <button class="nav-btn active" id="nav-online" onclick="goScreen('online')">
    <div class="nav-icon-wrap"><i class="fas fa-wifi"></i><span class="nav-badge" id="nav-badge" style="display:none;"></span></div>
    Online POS
  </button>
  <button class="nav-btn" id="nav-pos" onclick="goScreen('pos')">
    <i class="fas fa-cash-register"></i>
    Walk-In POS
  </button>
</nav>

<!-- CUSTOMISE MODAL -->
<div class="modal-overlay" id="customize-modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title" id="modal-item-name">Customize</div>
    <span class="modal-label">Size</span>
    <div class="option-row" id="opt-size">
      <button class="opt-btn selected" onclick="selOpt('size','16 oz',this)">16 oz</button>
      <button class="opt-btn" onclick="selOpt('size','22 oz',this)">22 oz</button>
    </div>
    <span class="modal-label">Sugar Level</span>
    <div class="option-row" id="opt-sugar">
      <button class="opt-btn" onclick="selOpt('sugar','0%',this)">0%</button>
      <button class="opt-btn" onclick="selOpt('sugar','25%',this)">25%</button>
      <button class="opt-btn" onclick="selOpt('sugar','50%',this)">50%</button>
      <button class="opt-btn" onclick="selOpt('sugar','75%',this)">75%</button>
      <button class="opt-btn selected" onclick="selOpt('sugar','100%',this)">100%</button>
    </div>
    <span class="modal-label">Ice Level</span>
    <div class="option-row" id="opt-ice">
      <button class="opt-btn" onclick="selOpt('ice','No Ice',this)">No Ice</button>
      <button class="opt-btn" onclick="selOpt('ice','Less Ice',this)">Less Ice</button>
      <button class="opt-btn selected" onclick="selOpt('ice','Normal Ice',this)">Normal Ice</button>
      <button class="opt-btn" onclick="selOpt('ice','Extra Ice',this)">Extra Ice</button>
    </div>
    <span class="modal-label">Add-ons <span style="color:var(--teal);font-size:0.65rem;">(+₱10 each)</span></span>
    <div class="option-row" id="opt-addons">
      <button class="opt-btn" onclick="toggleAddon('Pearls',this)">🧋 Pearls</button>
      <button class="opt-btn" onclick="toggleAddon('Nata',this)">🟡 Nata</button>
      <button class="opt-btn" onclick="toggleAddon('Coffee Jelly',this)">☕ Coffee Jelly</button>
    </div>
    <div class="modal-actions">
      <button class="btn-modal-cancel" onclick="closeCustomizeModal()">Cancel</button>
      <button class="btn-modal-add" onclick="addToCart()"><i class="fas fa-plus"></i> Add to Cart</button>
    </div>
  </div>
</div>

<!-- SUCCESS MODAL -->
<div class="success-overlay" id="success-modal">
  <div class="success-card">
    <div class="success-icon"><i class="fas fa-check"></i></div>
    <div style="font-size:0.8rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1px;">Order Placed!</div>
    <div class="success-code" id="success-code">—</div>
    <div style="font-size:0.82rem;color:var(--muted);font-weight:600;" id="success-name"></div>
    <div style="font-size:1rem;font-weight:900;color:var(--teal-dark);margin-top:6px;" id="success-total"></div>
    <button class="btn-done-print" onclick="printLastReceipt()"><i class="fas fa-print"></i> Print Receipt</button>
    <button class="btn-done" onclick="closeSuccessModal()">New Order</button>
  </div>
</div>

<script>
function escapeHTML(s){const d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML;}
function onImgErr(el){el.style.display='none';if(el.nextElementSibling)el.nextElementSibling.style.display='flex';}
function showToast(msg,type='info'){const c=document.getElementById('toast-container');const t=document.createElement('div');t.className=`toast ${type}`;t.innerHTML=msg;c.appendChild(t);setTimeout(()=>t.remove(),3200);}

(function tickClock(){const el=document.getElementById('clock');if(el){const now=new Date();const h=now.getHours(),m=now.getMinutes(),s=now.getSeconds();const h12=h%12||12;const ap=h<12?'AM':'PM';el.textContent=`${h12}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')} ${ap}`;}setTimeout(tickClock,1000);})();

function goScreen(id){
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('s-'+id).classList.add('active');
  document.getElementById('nav-'+id).classList.add('active');
  if(id==='online') fetchOrders();
  if(id==='pos') loadMenu();
}

/* ── ONLINE POS ── */
let allOrders=[], activeFilter='All', knownPermCodes=new Set(), knownOrderIds=new Set();

function setFilter(f,btn){
  activeFilter=f;
  document.querySelectorAll('#ftab-all,#ftab-waiting,#ftab-preparing,#ftab-ready,#ftab-done').forEach(b=>{
    b.style.background='';b.style.color='';b.style.borderColor='';
  });
  if(btn){btn.style.background='var(--teal-dark)';btn.style.color='#fff';btn.style.borderColor='var(--teal-dark)';}
  renderOrders();
}

async function fetchOrders(){
  try{
    const r=await fetch('/api/orders');
    if(!r.ok) return;
    const data=await r.json();
    allOrders=data.orders||[];
    const hasNewOrder=knownOrderIds.size>0&&allOrders.some(o=>!knownOrderIds.has(o.id));
    if(hasNewOrder) document.getElementById('emp-audio').play().catch(()=>{});
    allOrders.forEach(o=>knownOrderIds.add(o.id));
    renderOrders();
    updateStats();
  }catch(e){}
}

function updateStats(){
  document.getElementById('stat-waiting').textContent=allOrders.filter(o=>o.status==='Waiting Confirmation').length;
  document.getElementById('stat-preparing').textContent=allOrders.filter(o=>o.status==='Preparing').length;
  document.getElementById('stat-ready').textContent=allOrders.filter(o=>o.status==='Ready for Pickup').length;
  const pending=allOrders.filter(o=>o.status==='Waiting Confirmation').length;
  const badge=document.getElementById('nav-badge');
  if(pending>0){badge.textContent=pending;badge.style.display='flex';}else{badge.style.display='none';}
}

function renderOrders(){
  const tbody=document.getElementById('orders-tbody');
  let orders=allOrders;
  if(activeFilter==='Online') orders=orders.filter(o=>o.source==='Online');
  else if(activeFilter==='Walk-In') orders=orders.filter(o=>o.source!=='Online');
  else if(activeFilter!=='All') orders=orders.filter(o=>o.status===activeFilter);
  if(!orders.length){
    tbody.innerHTML='<tr class="empty-row"><td colspan="8">No active orders</td></tr>';
    return;
  }
  const statusClass={'Waiting Confirmation':'sel-waiting','Preparing':'sel-preparing','Ready for Pickup':'sel-ready','Completed':'sel-completed','Cancelled':'sel-cancelled'};
  tbody.innerHTML=orders.map(o=>{
    const isOnline=o.source==='Online';
    const sourceBadge=isOnline
      ?'<span class="badge badge-online" style="font-size:0.67rem;">🌐 Online</span>'
      :'<span class="badge badge-walkin" style="font-size:0.67rem;">🚶 Walk-In</span>';
    const itemsSummary=o.items.map(i=>escapeHTML(i.foundation+(i.size&&i.size!=='16 oz'?' ('+i.size+')':''))).join(', ');
    const cls=statusClass[o.status]||'sel-waiting';
    const sel=`<select class="status-select ${cls}" onchange="updateStatus(${o.id},this.value,this)">
      <option value="Waiting Confirmation" ${o.status==='Waiting Confirmation'?'selected':''}>⏳ Waiting</option>
      <option value="Preparing" ${o.status==='Preparing'?'selected':''}>🔥 Preparing</option>
      <option value="Ready for Pickup" ${o.status==='Ready for Pickup'?'selected':''}>✅ Ready</option>
      <option value="Completed" ${o.status==='Completed'?'selected':''}>🏁 Completed</option>
      <option value="Cancelled" ${o.status==='Cancelled'?'selected':''}>❌ Cancelled</option>
    </select>`;
    return `<tr>
      <td><span class="order-num">#${escapeHTML(o.code)}</span></td>
      <td>${sourceBadge}</td>
      <td style="font-weight:700;white-space:nowrap;">${escapeHTML(o.name)}</td>
      <td style="white-space:nowrap;font-size:0.74rem;color:var(--muted);">${escapeHTML(o.pickup_time||'Walk-In')}</td>
      <td style="white-space:nowrap;font-family:'Playfair Display',serif;font-weight:900;color:var(--teal-dark);">₱${Number(o.total).toFixed(2)}</td>
      <td class="items-cell">${itemsSummary}</td>
      <td>${sel}</td>
      <td><div class="tbl-actions"><button class="tbl-btn print" onclick="printOrderReceipt(${o.id})"><i class="fas fa-print"></i></button></div></td>
    </tr>`;
  }).join('');
}

async function updateStatus(orderId,status,selectEl){
  const statusClass={'Waiting Confirmation':'sel-waiting','Preparing':'sel-preparing','Ready for Pickup':'sel-ready','Completed':'sel-completed','Cancelled':'sel-cancelled'};
  if(selectEl){
    selectEl.className='status-select '+(statusClass[status]||'sel-waiting');
  }
  try{
    const r=await fetch(`/api/orders/${orderId}/status`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
    if(r.ok){showToast(`Status: ${status}`,'success');fetchOrders();}
    else showToast('Update failed','error');
  }catch(e){showToast('Network error','error');}
}

/* ── PERMISSION REQUESTS (employee view) ── */
async function fetchPermReqs(){
  const tbody=document.getElementById('perm-tbody');
  try{
    const r=await fetch('/api/permission_requests');
    if(!r.ok){tbody.innerHTML='<tr class="empty-row"><td colspan="5">Could not load requests</td></tr>';return;}
    const data=await r.json();
    if(!data.length){tbody.innerHTML='<tr class="empty-row"><td colspan="5">No pending requests</td></tr>';return;}
    // Sound alert for new codes
    data.forEach(p=>{if(!knownPermCodes.has(p.code)){document.getElementById('emp-audio').play().catch(()=>{});knownPermCodes.add(p.code);}});
    const badge=document.getElementById('nav-badge');
    const permCount=data.length;
    const orderPending=allOrders.filter(o=>o.status==='Waiting Confirmation').length;
    const total=permCount+orderPending;
    if(total>0){badge.textContent=total;badge.style.display='flex';}
    tbody.innerHTML=data.map(p=>`<tr class="perm-row-card">
      <td style="white-space:nowrap;font-size:0.74rem;color:var(--muted);">${escapeHTML(p.time)}</td>
      <td><span style="font-family:'Playfair Display',serif;font-weight:900;color:var(--teal-dark);font-size:0.82rem;">${escapeHTML(p.code)}</span></td>
      <td style="font-weight:700;">${escapeHTML(p.name)}</td>
      <td style="font-size:0.74rem;color:var(--muted);max-width:160px;">${escapeHTML(p.message||'—')}</td>
      <td><button class="tbl-btn grant" onclick="grantPerm(${p.id},'${escapeHTML(p.name)}','${escapeHTML(p.code)}')"><i class="fas fa-check-circle"></i> Grant</button></td>
    </tr>`).join('');
  }catch(e){tbody.innerHTML='<tr class="empty-row"><td colspan="5">Network error</td></tr>';}
}

async function grantPerm(id,name,code){
  try{
    const r=await fetch(`/api/permission_requests/${id}/grant`,{method:'POST'});
    if(r.ok){showToast(`✅ Granted for ${name}`,'success');knownPermCodes.delete(code);fetchPermReqs();}
    else showToast('Grant failed','error');
  }catch(e){showToast('Network error','error');}
}

function printOrderReceipt(orderId){
  const o=allOrders.find(x=>x.id===orderId);
  if(!o) return;
  openReceiptWindow({code:o.code,name:o.name,pickup:o.pickup_time,total:o.total,items:o.items.map(i=>({foundation:i.foundation,size:i.size,price:o.total/o.items.length,addons:i.addons,sweetener:i.sweetener,ice:i.ice}))});
}

/* ── WALK-IN POS ── */
let menuItems=[], cart=[], currentItem=null, currentOpts={size:'16 oz',sugar:'100%',ice:'Normal Ice',addons:[]};
const ADD_ON_PRICE=10;
const SIZE_SURCHARGE={'16 oz':0,'22 oz':10};

async function loadMenu(){
  if(menuItems.length) return;
  try{
    const r=await fetch('/api/menu');
    const data=await r.json();
    menuItems=data;
    buildCatTabs();
    renderMenuGrid('All');
  }catch(e){document.getElementById('menu-grid').innerHTML='<div style="grid-column:1/-1;text-align:center;padding:30px;color:var(--muted);">Could not load menu</div>';}
}

function buildCatTabs(){
  const cats=['All','Best Sellers',...new Set(menuItems.map(m=>m.category))];
  const tabs=document.getElementById('cat-tabs');
  tabs.innerHTML=cats.map((c,i)=>`<button class="cat-tab${i===0?' active':''}" onclick="selectCat(${JSON.stringify(c)},this)">${c==='Best Sellers'?'⭐ ':''}${escapeHTML(c)}</button>`).join('');
}

function selectCat(cat,btn){
  document.querySelectorAll('.cat-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderMenuGrid(cat);
}

const CATEGORY_PREVIEW_KEYWORDS={
  'Milktea':'milk+tea+boba',
  'Coffee':'iced+coffee+drink',
  'Milk Series':'strawberry+milk+drink',
  'Matcha Series':'matcha+latte+drink',
  'Fruit Soda':'fruit+soda+drink',
  'Frappe':'frappe+blended+drink',
  'Snacks':'snack+food+fries',
};
const POS_EMOJI_MAP={
  'Taro Milktea':{badge:'bestseller'},'Okinawa Milktea':{badge:'bestseller'},'Biscoff Milktea':{badge:'bestseller'},'Caramel Macchiato':{badge:'bestseller'}
};
function posCardStyle(item){
  if(POS_EMOJI_MAP[item.name]) return POS_EMOJI_MAP[item.name];
  return {badge:'none'};
}
function getPreviewUrl(item){
  const slug=encodeURIComponent(item.name.toLowerCase().replace(/\s+/g,'+'));
  const fallbackKw=CATEGORY_PREVIEW_KEYWORDS[item.category]||'bubble+tea+drink';
  return 'https://source.unsplash.com/160x90/?'+slug+','+fallbackKw;
}

function renderMenuGrid(cat){
  const grid=document.getElementById('menu-grid');
  let items;
  if(cat==='All') items=menuItems;
  else if(cat==='Best Sellers') items=menuItems.filter(m=>!m.is_out_of_stock&&posCardStyle(m).badge==='bestseller');
  else items=menuItems.filter(m=>m.category===cat);
  if(!items.length){grid.innerHTML='<div style="grid-column:1/-1;text-align:center;padding:30px;color:var(--muted);">No items</div>';return;}
  grid.innerHTML=items.map(function(m){
    const imgUrl=getPreviewUrl(m);
    const bs=posCardStyle(m).badge==='bestseller'?'<div class="menu-bs-tag">\u2B50 Best Seller</div>':'';
    const oosClass=m.is_out_of_stock?' oos':'';
    const clickHandler=m.is_out_of_stock?'':'openCustomize('+m.id+')';
    const oosTag=m.is_out_of_stock?'<div class="menu-oos-tag">Out of Stock</div>':'';
    const imgTag='<img class="menu-preview" src="'+imgUrl+'" alt="'+escapeHTML(m.name)+'" onerror="onImgErr(this)">';
    const letterTag='<div class="menu-letter" style="display:none;">'+escapeHTML(m.letter||'?')+'</div>';
    return '<div class="menu-card'+oosClass+'" onclick="'+clickHandler+'">'+
      imgTag+letterTag+bs+
      '<div class="menu-card-body">'+
        '<div class="menu-name">'+escapeHTML(m.name)+'</div>'+
        '<div class="menu-price">\u20B1'+Number(m.price).toFixed(0)+'</div>'+
        oosTag+
      '</div>'+
    '</div>';
  }).join('');
}
function openCustomize(menuId){
  currentItem=menuItems.find(m=>m.id===menuId);
  if(!currentItem) return;
  currentOpts={size:'16 oz',sugar:'100%',ice:'Normal Ice',addons:[]};
  document.getElementById('modal-item-name').textContent=currentItem.name;
  ['opt-size','opt-sugar','opt-ice'].forEach(id=>document.querySelectorAll(`#${id} .opt-btn`).forEach(b=>b.classList.remove('selected')));
  document.querySelectorAll('#opt-addons .opt-btn').forEach(b=>b.classList.remove('selected'));
  const sizeBtn=document.querySelector('#opt-size .opt-btn');if(sizeBtn)sizeBtn.classList.add('selected');
  document.querySelectorAll('#opt-sugar .opt-btn').forEach(b=>{if(b.textContent.trim()==='100%')b.classList.add('selected');});
  document.querySelectorAll('#opt-ice .opt-btn').forEach(b=>{if(b.textContent.includes('Normal'))b.classList.add('selected');});
  document.getElementById('customize-modal').classList.add('show');
}

function selOpt(key,val,btn){
  currentOpts[key]=val;
  const row=btn.closest('.option-row');
  row.querySelectorAll('.opt-btn').forEach(b=>b.classList.remove('selected'));
  btn.classList.add('selected');
}

function toggleAddon(name,btn){
  const idx=currentOpts.addons.indexOf(name);
  if(idx>=0){currentOpts.addons.splice(idx,1);btn.classList.remove('selected');}
  else{currentOpts.addons.push(name);btn.classList.add('selected');}
}

function closeCustomizeModal(){document.getElementById('customize-modal').classList.remove('show');}

function addToCart(){
  if(!currentItem) return;
  const sizeSur=SIZE_SURCHARGE[currentOpts.size]||0;
  const addonSur=currentOpts.addons.length*ADD_ON_PRICE;
  const price=currentItem.price+sizeSur+addonSur;
  cart.push({id:Date.now(),menuId:currentItem.id,foundation:currentItem.name,size:currentOpts.size,sugar:currentOpts.sugar,ice:currentOpts.ice,addons:currentOpts.addons.join(', '),price});
  closeCustomizeModal();
  renderCart();
  showToast(`${currentItem.name} added!`,'success');
}

function renderCart(){
  const el=document.getElementById('cart-items');
  const count=document.getElementById('cart-count');
  const totalEl=document.getElementById('cart-total');
  const btn=document.getElementById('checkout-btn');
  count.textContent=cart.length;
  if(!cart.length){
    el.innerHTML='<div class="cart-empty"><i class="fas fa-shopping-bag"></i>Cart is empty.<br>Tap items to add.</div>';
    totalEl.textContent='₱0.00';btn.disabled=true;return;
  }
  const total=cart.reduce((s,i)=>s+i.price,0);
  el.innerHTML=cart.map(i=>{
    const mods=[i.sugar,i.ice].filter(v=>v&&v!=='N/A').join(', ');
    const addons=i.addons?` • ${i.addons}`:'';
    return `<div class="cart-item">
      <div class="cart-item-info">
        <div class="cart-item-name">${escapeHTML(i.foundation)} ${i.size&&i.size!=='16 oz'?'('+escapeHTML(i.size)+')':''}</div>
        ${mods||addons?`<div class="cart-item-mods">${escapeHTML(mods+addons)}</div>`:''}
        <div class="cart-item-price">₱${i.price.toFixed(2)}</div>
      </div>
      <button class="cart-item-del" onclick="removeFromCart(${i.id})"><i class="fas fa-times"></i></button>
    </div>`;
  }).join('');
  totalEl.textContent='₱'+total.toFixed(2);
  btn.disabled=false;
}

function removeFromCart(id){cart=cart.filter(i=>i.id!==id);renderCart();}
function clearCart(){cart=[];renderCart();}

async function checkout(){
  if(!cart.length) return;
  const btn=document.getElementById('checkout-btn');
  btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i> Processing…';
  const name=document.getElementById('customer-name').value.trim()||'Walk-In';
  const total=cart.reduce((s,i)=>s+i.price,0);
  const payload={customer_name:name,total,items:cart.map(i=>({foundation:i.foundation,size:i.size,sugar:i.sugar,ice:i.ice,addons:i.addons,price:i.price}))};
  try{
    const r=await fetch('/api/admin/manual_order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await r.json();
    if(r.ok){
      window._lastWalkIn={code:data.reservation_code,name,total,items:cart.slice()};
      document.getElementById('success-code').textContent='#'+data.reservation_code;
      document.getElementById('success-name').textContent=name;
      document.getElementById('success-total').textContent='₱'+total.toFixed(2);
      document.getElementById('success-modal').classList.add('show');
      cart=[];renderCart();
      document.getElementById('customer-name').value='';
    }else{
      showToast(data.error||'Order failed','error');
      btn.disabled=false;btn.innerHTML='<i class="fas fa-check-circle"></i> Checkout';
    }
  }catch(e){
    showToast('Network error','error');
    btn.disabled=false;btn.innerHTML='<i class="fas fa-check-circle"></i> Checkout';
  }
}

function closeSuccessModal(){
  document.getElementById('success-modal').classList.remove('show');
  document.getElementById('checkout-btn').innerHTML='<i class="fas fa-check-circle"></i> Checkout';
}

function printLastReceipt(){
  const r=window._lastWalkIn;if(!r) return;
  openReceiptWindow({code:r.code,name:r.name,pickup:'Walk-In',total:r.total,items:r.items.map(i=>({foundation:i.foundation,size:i.size,price:i.price,addons:i.addons,sweetener:i.sugar,ice:i.ice}))});
}

function openReceiptWindow(r){
  const now=new Date();
  const dateStr=now.toLocaleDateString('en-PH',{day:'numeric',month:'short',year:'numeric'});
  const timeStr=now.toLocaleTimeString('en-PH',{hour:'numeric',minute:'2-digit',hour12:true});
  const itemMap={};
  r.items.forEach(i=>{
    const key=i.foundation+(i.size&&i.size!=='16 oz'?' ('+i.size+')':'');
    if(!itemMap[key]) itemMap[key]={name:key,qty:0,amount:0};
    itemMap[key].qty+=1;itemMap[key].amount+=i.price;
  });
  const rows=Object.values(itemMap).map(item=>{
    const up=(item.amount/item.qty).toFixed(2);
    return`<tr><td style="padding:6px 8px 2px 8px;" colspan="2">${item.name}</td></tr><tr><td style="padding:2px 8px 8px 8px;color:#555;">${item.qty} &times; &#8369;${up}</td><td style="padding:2px 8px 8px 8px;text-align:right;font-weight:bold;">&#8369;${item.amount.toFixed(2)}</td></tr>`;
  }).join('');
  const totalQty=Object.values(itemMap).reduce((s,i)=>s+i.qty,0);
  const html=`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Receipt — ${r.code}</title>
  <style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:'Courier New',Courier,monospace;font-size:16px;color:#111;background:white;padding:32px 24px;max-width:560px;margin:0 auto;}
  .center{text-align:center;}.header-section{display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:10px;}
  .logo-img{width:80px;height:80px;border-radius:50%;object-fit:cover;border:2px solid #0D7A6A;margin-bottom:8px;}.shop-name{font-size:1.5rem;font-weight:bold;margin-bottom:2px;}
  .shop-tagline{font-size:0.85rem;color:#0D7A6A;letter-spacing:2px;text-transform:uppercase;margin-bottom:2px;}.shop-meta{font-size:0.82rem;color:#444;margin-top:2px;}
  .divider-solid{border:none;border-top:1px solid #333;margin:10px 0;}.divider-dash{border:none;border-top:1px dashed #999;margin:10px 0;}
  table{width:100%;border-collapse:collapse;}th{text-align:left;padding:6px 8px;border-bottom:1px solid #333;font-weight:bold;}th.right{text-align:right;}
  .total-section td{padding:6px 8px;font-weight:bold;}.footer{text-align:center;font-size:0.9rem;margin-top:18px;color:#333;}.footer .est{font-size:0.78rem;color:#888;margin-top:4px;}
  @media print{@page{margin:10mm;size:A5;}body{padding:0;}}<\/style><\/head>
  <body><div class="header-section"><img src="/static/images/9599.jpg" class="logo-img" onerror="this.style.display='none'">
  <div class="shop-name">9599 Tea &amp; Coffee<\/div><div class="shop-tagline">Parne Na!<\/div>
  <div class="shop-meta">&#128205; Brgy. Poblacion, San Antonio, Quezon, Philippines<\/div>
  <div class="shop-meta">BIR TIN: 322-845-268-00000<\/div><\/div>
  <hr class="divider-solid"><div class="center" style="font-size:1rem;font-weight:bold;letter-spacing:1px;">OFFICIAL RECEIPT<\/div>
  <div class="center" style="font-size:0.88rem;color:#555;margin-top:4px;">Date: ${dateStr} &nbsp;|&nbsp; Time: ${timeStr}<\/div>
  <hr class="divider-solid"><div style="font-size:1rem;margin-bottom:4px;"><b>Order #:<\/b> ${r.code}<\/div>
  <div style="font-size:1rem;margin-bottom:4px;"><b>Customer:<\/b> ${r.name}<\/div>
  <div style="font-size:1rem;margin-bottom:6px;"><b>Pick-up:<\/b> ${r.pickup||'Walk-In'}<\/div>
  <hr class="divider-solid"><table><thead><tr><th>Item<\/th><th class="right">Amount<\/th><\/tr><\/thead><tbody>${rows}<\/tbody><\/table>
  <hr class="divider-dash"><table class="total-section"><tr><td style="text-align:left;">Total Items: ${totalQty}<\/td><td style="text-align:right;">&#8369;${r.total.toFixed(2)}<\/td><\/tr><\/table>
  <hr class="divider-dash"><div class="footer">Thank you for ordering!<br>9599 Tea &amp; Coffee Shop<div class="est">Est. ${new Date().getFullYear()} &nbsp;&middot;&nbsp; This serves as your official receipt.<\/div><\/div>
  <script>window.onload=()=>{setTimeout(()=>{window.print();window.onafterprint=()=>window.close();},300);}<\/script>
  <\/body><\/html>`;
  const w=window.open('','_blank','width=680,height=900');
  if(w){w.document.write(html);w.document.close();}
}

setInterval(()=>{if(document.getElementById('s-online').classList.contains('active')){fetchOrders();fetchPermReqs();}},5000);
fetchOrders();
fetchPermReqs();
</script>
</body>
</html>
"""


STOREFRONT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <link rel="icon" href="/static/images/9599.jpg">
    <title>Order Here | 9599 Tea & Coffee</title>
    
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@400;500;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
    
    <style>
        :root {
            --bg-base: #FAF8F5;
            --text-dark: #2C1A12;
            --text-light: #A38C7D;
            --gold: #CD9A5B;
            --gold-light: #F6F1E9;
            --card-bg: #FFFFFF;
            --border-color: #EBE5DC;
            --badge-bestseller: #D4A373;
            --badge-new: #5C7C5C;
            --danger: #C0392B;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; background-color: var(--bg-base); color: var(--text-dark); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
        h1, h2, h3, .serif-font { font-family: 'Playfair Display', serif; }

        .promo-ticker { background: #1A110D; color: var(--gold); padding: 10px 0; overflow: hidden; font-size: 0.85rem; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; flex-shrink: 0; }
        .ticker-inner { display: inline-block; animation: ticker 25s linear infinite; }
        @keyframes ticker { from { transform: translateX(100vw); } to { transform: translateX(-100%); } }

        header { background: var(--bg-base); padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); flex-shrink: 0; }
        .logo-area { display: flex; align-items: center; gap: 15px; }
        .logo-ring { width: 50px; height: 50px; border-radius: 50%; border: 2px solid var(--gold); display: flex; justify-content: center; align-items: center; }
        .logo-img { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; }
        .logo-text-wrapper { display: flex; flex-direction: column; }
        .logo-title { font-family: 'Playfair Display', serif; font-weight: 900; font-size: 1.3rem; color: var(--text-dark); line-height: 1; }
        .logo-sub { font-family: 'Playfair Display', serif; font-size: 0.7rem; font-weight: 900; letter-spacing: 3px; color: var(--gold); text-transform: uppercase; margin-top: 4px; }

        .main-container { display: flex; flex: 1; overflow: hidden; }
        .menu-area { flex: 1; padding: 25px 30px; overflow-y: auto; background: var(--bg-base); }
        .categories { display: flex; gap: 12px; overflow-x: auto; margin-bottom: 25px; padding-bottom: 10px; scrollbar-width: none; }
        .categories::-webkit-scrollbar { display: none; }
        .cat-btn { padding: 10px 24px; border-radius: 50px; border: 1px solid var(--border-color); background: var(--card-bg); color: var(--text-dark); font-weight: 700; font-size: 0.9rem; cursor: pointer; white-space: nowrap; transition: all 0.2s ease; display: flex; align-items: center; gap: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.02); }
        .cat-btn.active { background: var(--text-dark); color: var(--gold); border-color: var(--text-dark); }

        .search-bar-wrapper { position: relative; margin-bottom: 20px; }
        .search-bar-wrapper i { position: absolute; left: 15px; top: 50%; transform: translateY(-50%); color: var(--text-light); font-size: 0.95rem; pointer-events: none; }
        .menu-search-input { width: 100%; padding: 12px 15px 12px 42px; border: 1.5px solid var(--border-color); border-radius: 50px; font-size: 0.9rem; font-weight: 600; color: var(--text-dark); background: var(--card-bg); outline: none; transition: border-color 0.2s; }
        .menu-search-input:focus { border-color: var(--gold); }

        .menu-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 20px; }
        .card { background: var(--card-bg); border-radius: 16px; overflow: hidden; box-shadow: 0 4px 15px rgba(44, 26, 18, 0.05); cursor: pointer; transition: transform 0.2s ease; border: 1px solid var(--border-color); position: relative; display: flex; flex-direction: column; }
        .card:hover { transform: translateY(-5px); box-shadow: 0 8px 25px rgba(44, 26, 18, 0.1); }
        .card-img-container { height: 140px; position: relative; display: flex; justify-content: center; align-items: center; overflow: hidden;}
        
        .grad-trending   { background: linear-gradient(135deg, #8B5A2B, #C88A3C); }
        .grad-signature  { background: linear-gradient(135deg, #4A3A6B, #6A5A8B); }
        .grad-matcha     { background: linear-gradient(135deg, #3A5A40, #5C7C5C); }
        .grad-fruit      { background: linear-gradient(135deg, #8B2B4A, #C0395A); }
        .grad-snacks     { background: linear-gradient(135deg, #6B4A1A, #A67B2A); }
        .grad-default    { background: linear-gradient(135deg, #4A2B1A, #8B5A2B); }

        .card-emoji { font-size: 4.5rem; filter: drop-shadow(0 10px 15px rgba(0,0,0,0.3)); transition: transform 0.3s ease; z-index: 2; }
        .card-real-img { width: 100%; height: 100%; object-fit: cover; position: absolute; top: 0; left: 0; z-index: 1; }
        .card:hover .card-emoji { transform: scale(1.1) rotate(5deg); }

        .badge-bestseller { position: absolute; top: 12px; left: 12px; background: var(--badge-bestseller); color: var(--text-dark); padding: 4px 10px; border-radius: 20px; font-size: 0.65rem; font-weight: 800; letter-spacing: 0.5px; z-index: 3;}
        .badge-new { position: absolute; top: 12px; left: 12px; background: var(--badge-new); color: white; padding: 4px 10px; border-radius: 20px; font-size: 0.65rem; font-weight: 800; letter-spacing: 0.5px; z-index: 3;}
        .card-price { position: absolute; bottom: 12px; right: 12px; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); color: var(--gold); padding: 5px 12px; border-radius: 20px; font-family: 'Playfair Display', serif; font-weight: 700; font-size: 0.85rem; z-index: 3;}
        .card-body { padding: 15px; }
        .card-title { font-family: 'Playfair Display', serif; font-weight: 900; color: var(--text-dark); font-size: 1.1rem; line-height: 1.2; z-index: 3;}
        .card-cat-tag { margin-top: 4px; font-size: 0.7rem; font-weight: 700; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; }

        .card.sold-out { opacity: 0.5; pointer-events: none; }
        .sold-out-badge { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(255,255,255,0.6); display: flex; justify-content: center; align-items: center; font-weight: 900; color: var(--danger); font-size: 1.2rem; z-index: 10; letter-spacing: 2px; }

        .sidebar { width: 380px; background: var(--bg-base); border-left: 1px solid var(--border-color); display: flex; flex-direction: column; z-index: 50; overflow: hidden; }
        .cart-top-section { padding: 25px 25px 15px; flex-shrink: 0; overflow: visible; }
        .cart-header { display: flex; align-items: center; margin-bottom: 20px; gap: 10px; }
        .cart-title { font-family: 'Playfair Display', serif; font-size: 1.5rem; font-weight: 900; color: var(--text-dark); }
        .cart-count { background: var(--gold); color: var(--text-dark); font-size: 0.8rem; font-weight: 800; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }

        .order-type { display: flex; background: var(--gold-light); border-radius: 12px; padding: 5px; gap: 5px; margin-bottom: 15px; border: 1px solid var(--border-color); }
        .type-btn { flex: 1; padding: 10px; text-align: center; font-weight: 700; font-size: 0.85rem; border-radius: 8px; cursor: pointer; color: var(--text-light); transition: all 0.2s; display: flex; justify-content: center; align-items: center; gap: 8px; }
        .type-btn.active { background: var(--text-dark); color: var(--gold); box-shadow: 0 4px 10px rgba(0,0,0,0.1); }

        .name-input { width: 100%; padding: 14px 16px; border: 1px solid var(--border-color); border-radius: 12px; font-size: 0.95rem; font-weight: 700; outline: none; margin-bottom: 15px; color: var(--text-dark); background: var(--card-bg); font-family: 'DM Sans', sans-serif; box-shadow: 0 2px 5px rgba(0,0,0,0.02); }
        .name-input:focus { border-color: var(--gold); }
        .pickup-label { font-size: 0.75rem; font-weight: 800; color: var(--text-light); margin-bottom: 8px; display: block; text-transform: uppercase; letter-spacing: 1px; }
        .time-wrapper { position: relative; }
        .time-wrapper i { position: absolute; right: 16px; top: 50%; transform: translateY(-50%); color: var(--text-dark); font-size: 1.1rem; pointer-events: none; }

        .cart-content { padding: 0 25px 15px; flex: 1; overflow-y: auto; min-height: 0; }
        .empty-cart { margin: auto 0; text-align: center; padding: 40px 0; }
        .empty-cart-icon { font-size: 3rem; margin-bottom: 15px; opacity: 0.2; }
        .empty-cart p { font-weight: 700; font-size: 1rem; color: var(--text-light); }

        .cart-item { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 12px; padding: 16px; border-radius: 16px; background: var(--card-bg); border: 1px solid var(--border-color); box-shadow: 0 4px 10px rgba(44, 26, 18, 0.03); }
        .cart-item-name { font-family: 'Playfair Display', serif; font-size: 1.1rem; font-weight: 900; color: var(--text-dark); margin-bottom: 4px; }
        .cart-item-sub { font-size: 0.75rem; color: var(--text-light); font-weight: 500; line-height: 1.4; }
        .cart-item-right { display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; height: 100%; }
        .cart-item-price { font-family: 'Playfair Display', serif; font-weight: 900; color: var(--text-dark); font-size: 1.1rem; }
        .cart-item-del { margin-top: 10px; font-size: 0.8rem; color: var(--danger); cursor: pointer; font-weight: 700; }

        .checkout-area { padding: 20px 25px; border-top: 1px solid var(--border-color); background: var(--bg-base); flex-shrink: 0; }
        .total-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .total-label { font-size: 0.9rem; font-weight: 800; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; }
        .total-amount { font-family: 'Playfair Display', serif; font-size: 2rem; font-weight: 900; color: var(--text-dark); }

        .checkout-btn { width: 100%; padding: 18px; border: 2px solid #B8A898; border-radius: 12px; font-size: 1rem; font-weight: 800; letter-spacing: 1px; display: flex; justify-content: center; align-items: center; gap: 10px; color: #8C7B6E; background: #E8DDD4; cursor: not-allowed; transition: all 0.25s ease; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .checkout-btn.active { background: var(--gold); color: var(--text-dark); border-color: var(--gold); cursor: pointer; box-shadow: 0 6px 20px rgba(200, 155, 60, 0.4); }
        .checkout-btn.active:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(200, 155, 60, 0.5); }

        .modal { display: none; position: fixed; z-index: 200; left: 0; top: 0; width: 100%; height: 100%; background: rgba(44, 26, 18, 0.7); backdrop-filter: blur(5px); align-items: center; justify-content: center; }
        .modal-content { background: var(--bg-base); padding: 35px; border-radius: 24px; max-width: 90%; width: 400px; box-shadow: 0 24px 60px rgba(0,0,0,0.3); max-height: 90vh; overflow-y: auto; border: 1px solid var(--border-color); }
        .modal-content h2 { font-family: 'Playfair Display', serif; font-weight: 900; color: var(--text-dark); text-align: center; font-size: 1.8rem; margin-bottom: 25px; }

        .modal-section-label { font-size: 0.75rem; font-weight: 800; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; display: block; }
        .size-btns { display: flex; gap: 12px; margin-bottom: 20px; }
        .size-btn { flex: 1; padding: 15px 10px; border-radius: 12px; border: 2px solid var(--border-color); background: var(--card-bg); color: var(--text-dark); font-weight: 800; cursor: pointer; text-align: center; transition: all 0.2s; font-size: 1rem; }
        .size-btn.selected { background: var(--text-dark); color: var(--gold); border-color: var(--text-dark); }
        .size-btn-price { font-size: 0.8rem; font-weight: 600; opacity: 0.8; display: block; margin-top: 4px; }

        .sel-row { display: flex; gap: 12px; margin-bottom: 20px; }
        .sel-group { flex: 1; }
        .sel-group select { width: 100%; padding: 12px 14px; border: 1px solid var(--border-color); border-radius: 10px; font-weight: 700; color: var(--text-dark); background: var(--card-bg); outline: none; font-size: 0.9rem; margin-top: 5px; }

        .addon-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 5px; }
        .addon-label { display: flex; align-items: center; gap: 10px; padding: 12px; border: 1px solid var(--border-color); border-radius: 10px; cursor: pointer; font-weight: 700; color: var(--text-dark); background: var(--card-bg); font-size: 0.85rem; transition: all 0.2s; }
        .addon-label input { width: 18px; height: 18px; accent-color: var(--gold); }
        .addon-label input[type="checkbox"]:checked + span { color: var(--gold); }
        .addon-label input[type="radio"]:checked + span { color: var(--gold); }

        .modal-actions { display: flex; gap: 12px; margin-top: 30px; }
        .qty-selector { display: flex; align-items: center; justify-content: center; gap: 0; margin: 18px 0 4px; background: var(--gold-light); border-radius: 50px; border: 1.5px solid var(--border-color); width: fit-content; margin-left: auto; margin-right: auto; }
        .qty-selector .qty-label { font-size: 0.72rem; font-weight: 800; color: var(--text-light); text-transform: uppercase; letter-spacing: 1px; text-align: center; margin-bottom: 4px; }
        .qty-btn { width: 38px; height: 38px; border: none; background: transparent; font-size: 1.3rem; font-weight: 900; cursor: pointer; color: var(--text-dark); border-radius: 50px; display: flex; align-items: center; justify-content: center; transition: background 0.15s; }
        .qty-btn:hover { background: var(--border-color); }
        .qty-num { min-width: 36px; text-align: center; font-family: 'Playfair Display', serif; font-size: 1.3rem; font-weight: 900; color: var(--text-dark); }
        .btn-cancel { flex: 1; background: var(--border-color); color: var(--text-dark); border: none; padding: 15px; border-radius: 12px; font-weight: 800; cursor: pointer; }
        .btn-add { flex: 2; background: var(--gold); color: var(--text-dark); border: none; padding: 15px; border-radius: 12px; font-weight: 800; cursor: pointer; font-size: 1rem; }

        .slide-clock-wrapper { background: var(--card-bg); border: 1.5px solid var(--border-color); border-radius: 12px; padding: 14px 16px; margin-bottom: 15px; }
        .slide-clock-display { font-family: 'Playfair Display', serif; font-size: 1.4rem; font-weight: 900; color: var(--gold); text-align: center; margin-bottom: 12px; letter-spacing: 2px; }
        .slide-clock-row { display: flex; align-items: center; justify-content: center; gap: 6px; }
        .slide-clock-col { display: flex; flex-direction: column; align-items: center; gap: 4px; }
        .sc-btn { background: var(--gold-light); border: 1px solid var(--border-color); border-radius: 6px; width: 36px; height: 28px; font-size: 0.8rem; cursor: pointer; font-weight: 800; color: var(--text-dark); transition: background 0.15s; }
        .sc-btn:hover { background: var(--gold); }
        .sc-val { font-size: 1.5rem; font-weight: 900; color: var(--text-dark); min-width: 40px; text-align: center; font-family: 'Playfair Display', serif; }
        .sc-sep { font-size: 1.5rem; font-weight: 900; color: var(--text-dark); align-self: center; padding-bottom: 4px; }

        .admin-slide-clock-wrapper { background: #FDFBF7; border: 1.5px solid #D7CCC8; border-radius: 8px; padding: 12px 14px; margin-bottom: 15px; }
        .admin-slide-clock-label { font-size: 0.75rem; font-weight: 800; color: #8D6E63; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; display: block; }
        .admin-slide-clock-display { font-size: 1.1rem; font-weight: 800; color: #6F4E37; text-align: center; margin-bottom: 10px; font-family: 'Playfair Display', serif; }
        .admin-sc-row { display: flex; align-items: center; justify-content: center; gap: 5px; }
        .admin-sc-col { display: flex; flex-direction: column; align-items: center; gap: 3px; }
        .admin-sc-btn { background: #F5EFE6; border: 1px solid #D7CCC8; border-radius: 5px; width: 32px; height: 24px; font-size: 0.75rem; cursor: pointer; font-weight: 800; color: #3E2723; }
        .admin-sc-btn:hover { background: #D7CCC8; }
        .admin-sc-val { font-size: 1.2rem; font-weight: 900; color: #3E2723; min-width: 36px; text-align: center; font-family: 'Playfair Display', serif; }
        .admin-sc-sep { font-size: 1.2rem; font-weight: 900; color: #3E2723; align-self: center; }
        @media (max-width: 768px) {
            body { height: auto; min-height: 100vh; overflow-y: auto; }
            .main-container { flex-direction: column; height: auto; overflow: visible; }
            .menu-area { flex: none; height: auto; overflow: visible; padding-bottom: 60vh; }
            .menu-grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
            .sidebar { width: 100%; flex: none; height: auto; border-left: none; border-top: 2px solid var(--border-color); position: sticky; bottom: 0; z-index: 100; max-height: min(78vh, 720px); max-height: min(78dvh, 720px); display: flex; flex-direction: column; box-shadow: 0 -4px 20px rgba(44,26,18,0.12); overflow: hidden; min-height: 0; }
            .cart-top-section { flex-shrink: 1; min-height: 0; overflow-y: auto; max-height: min(44vh, 400px); }
            .cart-content { flex: 1 1 auto; overflow-y: auto; min-height: 0; max-height: none; }
            .checkout-area { flex-shrink: 0; position: sticky; bottom: 0; z-index: 6; background: var(--bg-base); padding-top: 14px; padding-bottom: max(16px, env(safe-area-inset-bottom, 0px)); box-shadow: 0 -8px 22px rgba(44,26,18,0.12); }
        }

        #toast-container { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 9999; display: flex; flex-direction: column; gap: 10px; }
        .toast { background-color: #3E2723; color: #fff; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; }
        .toast.error { background-color: #C62828; }
        .toast.success { background-color: #388E3C; }

        .notif-bell { cursor: pointer; position: relative; padding: 10px; border-radius: 50%; background: var(--gold-light); color: var(--gold); border: none; width: 38px; height: 38px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--border-color); transition: background 0.2s; }
        .notif-bell:hover { background: var(--border-color); }
        .notif-badge { position: absolute; top: -4px; right: -4px; background: var(--danger); color: white; border-radius: 50%; min-width: 18px; height: 18px; font-size: 0.65rem; font-weight: 800; display: none; align-items: center; justify-content: center; padding: 0 4px; border: 2px solid #fff; }
        .notif-dropdown { position: absolute; top: 48px; right: 0; background: #fff; border: 1px solid var(--border-color); border-radius: 14px; box-shadow: 0 8px 30px rgba(44,26,18,0.12); width: 300px; z-index: 9999; overflow: hidden; display: none; }
        .notif-dropdown.open { display: block; }
        .notif-dropdown-header { padding: 14px 16px 10px; border-bottom: 1px solid var(--border-color); font-family: 'Playfair Display', serif; font-weight: 900; font-size: 1rem; color: var(--text-dark); }
        .notif-list { max-height: 260px; overflow-y: auto; }
        .notif-item { padding: 12px 16px; border-bottom: 1px solid var(--border-color); font-size: 0.82rem; font-weight: 600; color: var(--text-dark); }
        .notif-item .notif-time { font-size: 0.7rem; color: var(--text-light); margin-top: 2px; }
        .notif-empty { padding: 20px 16px; text-align: center; color: var(--text-light); font-size: 0.82rem; font-weight: 600; }
        .gate-wrapper { display: flex; height: 100vh; width: 100vw; justify-content: center; align-items: center; background: var(--bg-base); padding: 20px; flex-direction: column; }

        /* ── Location Banner ── */
        .location-banner { background: linear-gradient(90deg, #6F4E37, #A67B5B); color: #fff; padding: 10px 20px; display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 0.85rem; font-weight: 700; cursor: pointer; flex-shrink: 0; transition: opacity 0.2s; }
        .location-banner:hover { opacity: 0.92; }
        .location-banner i { font-size: 1rem; color: var(--gold); }
        .location-banner span { letter-spacing: 0.3px; }
        .location-banner .loc-pill { background: rgba(255,255,255,0.2); border-radius: 20px; padding: 3px 12px; font-size: 0.78rem; border: 1px solid rgba(255,255,255,0.3); }

        /* ── Location Modal ── */
        .loc-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none; align-items: flex-end; justify-content: center; z-index: 9999; }
        .loc-modal-overlay.show { display: flex; }
        .loc-modal-sheet { background: #fff; width: 100%; max-width: 520px; border-radius: 24px 24px 0 0; padding: 24px 24px 32px; animation: slideUpLoc 0.3s ease; max-height: 92vh; overflow-y: auto; }
        @keyframes slideUpLoc { from { transform: translateY(60px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        .loc-handle { width: 44px; height: 4px; background: #D7CCC8; border-radius: 4px; margin: 0 auto 18px; }
        .loc-title { font-family: 'Playfair Display', serif; font-size: 1.35rem; font-weight: 900; color: #3E2723; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; }
        .loc-address { font-size: 0.88rem; color: #8D6E63; margin-bottom: 16px; line-height: 1.5; }
        .loc-map-wrap { border-radius: 16px; overflow: hidden; border: 1px solid #EFEBE4; margin-bottom: 16px; height: 240px; }
        .loc-map-wrap iframe { width: 100%; height: 100%; border: none; }
        .loc-info-row { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; font-size: 0.88rem; color: #5D4037; }
        .loc-info-row i { color: var(--gold); font-size: 1rem; margin-top: 2px; flex-shrink: 0; }
        .loc-divider { border: none; border-top: 1px dashed #EFEBE4; margin: 14px 0; }
        .loc-nav-btn { width: 100%; padding: 15px; border-radius: 14px; background: #6F4E37; color: #fff; border: none; font-family: inherit; font-size: 1rem; font-weight: 800; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 10px; letter-spacing: 0.3px; }
        .loc-nav-btn:active { opacity: 0.9; }
        .loc-nav-btn.gmaps { background: #1a73e8; }
        .loc-close-btn { width: 100%; padding: 12px; border-radius: 14px; border: 1.5px solid #D7CCC8; background: #fff; color: #8D6E63; font-family: inherit; font-size: 0.92rem; font-weight: 700; cursor: pointer; }
        .gate-card { background: var(--card-bg); padding: 50px 40px; border-radius: 24px; box-shadow: 0 20px 50px rgba(0,0,0,0.1); width: 100%; max-width: 420px; text-align: center; border: 1px solid var(--border-color); }
    </style>
</head>
<body>

<div id="toast-container"></div>
<audio id="status-audio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>
<audio id="alert-audio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>

{% if not session.get('customer_verified') %}
<!-- SIGN-IN GATEKEEPER -->
<div id="login-gatekeeper" class="gate-wrapper">
    <div class="gate-card">
        <div style="width:70px; height:70px; border-radius:50%; border:2px solid var(--gold); display:flex; justify-content:center; align-items:center; margin: 0 auto 15px;">
            <img src="/static/images/9599.jpg" style="width:60px; height:60px; border-radius:50%; object-fit:cover;" onerror="this.style.display='none'">
        </div>
        <h1 style="color:var(--text-dark); font-family:'Playfair Display',serif; font-size:2rem; line-height:1.1;">9599 Tea & Coffee</h1>
        <p style="font-family:'Playfair Display',serif; color:var(--gold); letter-spacing:3px; font-size:0.8rem; font-weight:900; margin-bottom: 24px;">PARNE NA!</p>

        <!-- MANUAL SIGN-IN FORM -->
        <div id="manual-signin-form">
            <p style="color:var(--text-light); font-weight:500; margin-bottom: 18px; font-size: 0.9rem;">Enter your details to place an order.</p>
            <div style="text-align:left; margin-bottom:12px;">
                <label style="font-size:0.7rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; display:block; margin-bottom:5px;">Full Name *</label>
                <input id="gate-name" type="text" placeholder="e.g. Juan dela Cruz" autocomplete="name"
                    style="width:100%; padding:13px 14px; border:2px solid var(--border-color); border-radius:12px; font-size:0.95rem; font-family:inherit; font-weight:600; color:var(--text-dark); background:#fff; outline:none; transition:border-color 0.2s;"
                    onfocus="this.style.borderColor='var(--gold)'" onblur="this.style.borderColor='var(--border-color)'">
            </div>
            <div style="text-align:left; margin-bottom:12px;">
                <label style="font-size:0.7rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; display:block; margin-bottom:5px;">Email Address *</label>
                <input id="gate-email" type="email" placeholder="e.g. juan@email.com" autocomplete="email"
                    style="width:100%; padding:13px 14px; border:2px solid var(--border-color); border-radius:12px; font-size:0.95rem; font-family:inherit; font-weight:600; color:var(--text-dark); background:#fff; outline:none; transition:border-color 0.2s;"
                    onfocus="this.style.borderColor='var(--gold)'" onblur="this.style.borderColor='var(--border-color)'">
            </div>
            <div style="text-align:left; margin-bottom:20px;">
                <label style="font-size:0.7rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; display:block; margin-bottom:5px;">Phone Number *</label>
                <input id="gate-phone" type="tel" placeholder="e.g. 09XX-XXX-XXXX" autocomplete="tel"
                    style="width:100%; padding:13px 14px; border:2px solid var(--border-color); border-radius:12px; font-size:0.95rem; font-family:inherit; font-weight:600; color:var(--text-dark); background:#fff; outline:none; transition:border-color 0.2s;"
                    onfocus="this.style.borderColor='var(--gold)'" onblur="this.style.borderColor='var(--border-color)'">
            </div>
            <button id="gate-btn" onclick="handleManualSignIn()"
                style="width:100%; padding:15px; border-radius:14px; background:linear-gradient(135deg,#8B5E3C,#5C3317); color:#fff; border:none; font-family:inherit; font-size:1rem; font-weight:800; cursor:pointer; letter-spacing:0.3px; box-shadow:0 4px 16px rgba(92,51,23,0.3); transition:opacity 0.2s; display:flex; align-items:center; justify-content:center; gap:10px;">
                <i class="fas fa-mug-hot"></i> Continue to Order
            </button>
            <div id="gate-error" style="display:none; margin-top:12px; background:#FFF0F0; color:#C0392B; padding:10px 14px; border-radius:10px; font-size:0.82rem; font-weight:700; border:1.5px solid #F5C6C6;">
                <i class="fas fa-exclamation-circle"></i> <span id="gate-error-msg"></span>
            </div>

            {% if google_client_id and google_client_id != 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com' %}
            <!-- Google Sign-In (only shown when a valid Client ID is configured) -->
            <script src="https://accounts.google.com/gsi/client" async defer></script>
            <div style="display:flex; align-items:center; gap:10px; margin:18px 0 14px;">
                <div style="flex:1; height:1px; background:var(--border-color);"></div>
                <span style="font-size:0.72rem; font-weight:700; color:var(--text-light); text-transform:uppercase; letter-spacing:1px;">or</span>
                <div style="flex:1; height:1px; background:var(--border-color);"></div>
            </div>
            <div id="g_id_onload" data-client_id="{{ google_client_id }}" data-context="signin" data-ux_mode="popup" data-callback="handleGoogleLogin" data-auto_prompt="false"></div>
            <div class="g_id_signin" data-type="standard" data-shape="rectangular" data-theme="outline" data-text="continue_with" data-size="large" data-logo_alignment="left" style="display:flex; justify-content:center;"></div>
            {% endif %}
        </div>
    </div>
</div>

<script>
    function showToast(message, type='info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        let icon = type === 'error' ? 'fa-exclamation-circle' : 'fa-check-circle';
        toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
        container.appendChild(toast);
        setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 3200);
    }

    function showGateError(msg) {
        const el = document.getElementById('gate-error');
        document.getElementById('gate-error-msg').textContent = msg;
        el.style.display = 'block';
    }
    function hideGateError() {
        document.getElementById('gate-error').style.display = 'none';
    }

    async function handleManualSignIn() {
        hideGateError();
        const name  = document.getElementById('gate-name').value.trim();
        const email = document.getElementById('gate-email').value.trim();
        const phone = document.getElementById('gate-phone').value.trim();
        if (!name)  { showGateError('Please enter your full name.'); return; }
        if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            showGateError('Please enter a valid email address.'); return;
        }
        const btn = document.getElementById('gate-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Please wait…';
        try {
            const res = await fetch('/api/auth/manual', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, email, phone })
            });
            if (res.ok) { location.reload(); }
            else {
                const d = await res.json();
                showGateError(d.error || 'Something went wrong. Please try again.');
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-mug-hot"></i> Continue to Order';
            }
        } catch (e) {
            showGateError('Connection error. Please check your internet and try again.');
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-mug-hot"></i> Continue to Order';
        }
    }

    // Allow pressing Enter in any field to submit
    ['gate-name','gate-email','gate-phone'].forEach(id => {
        document.getElementById(id).addEventListener('keydown', e => {
            if (e.key === 'Enter') handleManualSignIn();
        });
    });

    async function handleGoogleLogin(response) {
        try {
            const res = await fetch('/api/auth/google', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ token: response.credential }) });
            if (res.ok) location.reload();
            else showToast("Google Authentication Error. Please use the form above.", "error");
        } catch (e) { showToast("Connection Error", "error"); }
    }
</script>

{% else %}
<!-- MAIN STOREFRONT -->

<!-- Promo Ticker -->
<div class="promo-ticker">
    <span class="ticker-inner">
        🔥 Drinks from ₱49 &nbsp;·&nbsp;
        ✨ Customize sugar & ice level &nbsp;·&nbsp;
        🧋 Try our Signature Milk Teas &nbsp;·&nbsp;
        🌿 Matcha lovers — we've got you covered &nbsp;·&nbsp;
        🍟 Pair with crispy snacks &nbsp;·&nbsp;
        🎉 Mix & match — add Nata, Pearl, or Coffee Jelly! &nbsp;&nbsp;&nbsp;&nbsp;
    </span>
</div>

<!-- Location Banner -->
<div class="location-banner" onclick="openLocModal()">
    <i class="fas fa-map-marker-alt"></i>
    <span>Brgy. Poblacion, San Antonio, Quezon</span>
    <span class="loc-pill"><i class="fas fa-directions" style="margin-right:4px;"></i>Get Directions</span>
</div>

<!-- Header -->
<header>
    <div class="logo-area">
        <div class="logo-ring">
            <img src="/static/images/9599.jpg" alt="Logo" class="logo-img" onerror="this.style.display='none'">
        </div>
        <div class="logo-text-wrapper">
            <span class="logo-title">9599 Tea & Coffee</span>
            <span class="logo-sub">Parne Na!</span>
        </div>
    </div>
    <div class="notif-container" style="display:flex; align-items:center; gap:10px; position:relative;">
        <div title="Find Us" onclick="openLocModal()" style="cursor:pointer; width:38px; height:38px; border-radius:50%; background:var(--gold-light); display:flex; align-items:center; justify-content:center; border:1px solid var(--border-color);">
            <i class="fas fa-map-marker-alt" style="color:var(--gold); font-size:16px;"></i>
        </div>
        <button class="notif-bell" id="notif-bell-btn" onclick="toggleNotifDropdown()" title="Order Notifications">
            <i class="fas fa-bell" style="font-size:15px;"></i><span class="notif-badge" id="notif-badge">0</span>
        </button>
        <div class="notif-dropdown" id="notif-dropdown">
            <div class="notif-dropdown-header">🔔 Order Updates</div>
            <div class="notif-list" id="notif-list">
                <div class="notif-empty">No updates yet.<br>We'll notify you when your order status changes.</div>
            </div>
        </div>
    </div>
</header>

<!-- Location Modal -->
<div class="loc-modal-overlay" id="loc-modal" onclick="if(event.target===this)closeLocModal()">
    <div class="loc-modal-sheet">
        <div class="loc-handle"></div>
        <div class="loc-title">
            <i class="fas fa-map-marker-alt" style="color:#E53935; font-size:1.2rem;"></i>
            Find Our Shop
        </div>
        <div class="loc-address">
            Brgy. Poblacion, San Antonio, Quezon, Philippines
        </div>

        <!-- Embedded Google Map -->
        <div class="loc-map-wrap">
            <iframe
                src="https://maps.google.com/maps?q=San+Antonio+Quezon+Philippines&t=&z=15&ie=UTF8&iwloc=&output=embed"
                allowfullscreen="" loading="lazy"
                referrerpolicy="no-referrer-when-downgrade">
            </iframe>
        </div>

        <!-- Info rows -->
        <div class="loc-info-row">
            <i class="fas fa-clock"></i>
            <div>
                <b>Every Day:</b> {{ open_time }} – {{ close_time }}
            </div>
        </div>
        <div class="loc-info-row">
            <i class="fas fa-phone-alt"></i>
            <div>Contact us via our shop page for inquiries.</div>
        </div>

        <hr class="loc-divider">

        <!-- Navigate buttons -->
        <button class="loc-nav-btn gmaps" onclick="openGoogleMaps()">
            <i class="fas fa-directions"></i>
            Navigate with Google Maps
        </button>
        <button class="loc-nav-btn" onclick="openWaze()" style="background:#33CCFF;">
            <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/8/8c/Waze_icon.svg/32px-Waze_icon.svg.png" style="width:20px;height:20px;"> Navigate with Waze
        </button>
        <button class="loc-close-btn" onclick="closeLocModal()">Close</button>
    </div>
</div>

<!-- Main Layout -->
<div class="main-container">
    
    <!-- Menu Area -->
    <div class="menu-area">
        <div class="search-bar-wrapper">
            <i class="fas fa-search"></i>
            <input type="text" class="menu-search-input" id="menu-search" placeholder="Search drinks &amp; snacks…" oninput="searchMenu(this.value)">
        </div>
        <div class="categories" id="categories-container">
            <button class="cat-btn active" onclick="filterMenu('All', this)">☕ All</button>
            <button class="cat-btn" onclick="filterMenu('Best Sellers', this)">⭐ Best Sellers</button>
            <button class="cat-btn" onclick="filterMenu('Milktea', this)">🧋 Milktea</button>
            <button class="cat-btn" onclick="filterMenu('Coffee', this)">☕ Coffee</button>
            <button class="cat-btn" onclick="filterMenu('Matcha Series', this)">🍵 Matcha</button>
            <button class="cat-btn" onclick="filterMenu('Milk Series', this)">🥛 Milk Series</button>
            <button class="cat-btn" onclick="filterMenu('Fruit Soda', this)">🍹 Fruit Soda</button>
            <button class="cat-btn" onclick="filterMenu('Frappe', this)">🥤 Frappe</button>
            <button class="cat-btn" onclick="filterMenu('Snacks', this)">🍟 Snacks</button>
        </div>
        <div class="menu-grid" id="menu-grid">
            <div class="empty-category">Loading our luxurious menu…</div>
        </div>
    </div>

    <!-- Sidebar Cart -->
    <div class="sidebar">
        <div class="cart-top-section">
            <div class="cart-header">
                <div class="cart-title">Your Order</div>
                <div class="cart-count" id="cart-count">0</div>
            </div>

            <div class="order-type">
                <div class="type-btn active" id="btn-dine-in" onclick="setOrderType('Dine-In')"><i class="fas fa-chair"></i> Dine-In</div>
                <div class="type-btn" id="btn-take-out" onclick="setOrderType('Take-Out')"><i class="fas fa-shopping-bag"></i> Take-Out</div>
            </div>

            <input type="text" class="name-input" id="customer-name" placeholder="Your Name *" value="{{ session.get('customer_name', '') }}" oninput="checkCheckoutStatus()">
            <input type="email" class="name-input" id="customer-gmail" placeholder="Email Address *" value="{{ session.get('customer_email', '') }}" oninput="checkCheckoutStatus()">
            <input type="tel" class="name-input" id="customer-phone" placeholder="Phone Number *" value="{{ session.get('customer_phone', '') }}" oninput="checkCheckoutStatus()">

            <label class="pickup-label">Pick-up Time *</label>
            <div class="slide-clock-wrapper" id="pickup-clock-wrapper">
                <div class="slide-clock-row">
                    <div class="slide-clock-col">
                        <button class="sc-btn" onclick="adjustPickupTime('hour', 1)">▲</button>
                        <div class="sc-val" id="sc-hour">12</div>
                        <button class="sc-btn" onclick="adjustPickupTime('hour', -1)">▼</button>
                    </div>
                    <div class="sc-sep">:</div>
                    <div class="slide-clock-col">
                        <button class="sc-btn" onclick="adjustPickupTime('min', 1)">▲</button>
                        <div class="sc-val" id="sc-min">00</div>
                        <button class="sc-btn" onclick="adjustPickupTime('min', -1)">▼</button>
                    </div>
                    <div class="slide-clock-col" style="margin-left:8px;">
                        <button class="sc-btn" onclick="adjustPickupTime('ampm', 1)">▲</button>
                        <div class="sc-val" id="sc-ampm">PM</div>
                        <button class="sc-btn" onclick="adjustPickupTime('ampm', -1)">▼</button>
                    </div>
                </div>
            </div>
            <input type="hidden" id="pickup-time" value="">
        </div>

        <div class="cart-content">
            <div class="empty-cart" id="empty-cart">
                <p>Your cart is empty.</p>
            </div>
            <div id="cart-items"></div>
        </div>

        <div class="checkout-area">
            <div class="total-row">
                <span class="total-label">Total</span>
                <span class="total-amount" id="cart-total">₱0.00</span>
            </div>
            <button class="checkout-btn" id="checkout-btn" onclick="submitOrder()">
                <i class="fas fa-plane"></i> Place My Order
            </button>
        </div>
    </div>
</div>

<!-- Size Modal -->
<div id="size-modal" class="modal">
    <div class="modal-content">
        <h2 id="size-modal-title">Customize</h2>

        <div id="size-row-section">
            <span class="modal-section-label">Choose Size</span>
            <div class="size-btns">
                <button class="size-btn selected" id="btn-size-16" onclick="selectSize('16 oz')">16 oz <span class="size-btn-price" id="size-price-16">₱49</span></button>
                <button class="size-btn" id="btn-size-22" onclick="selectSize('22 oz')">22 oz <span class="size-btn-price" id="size-price-22">₱59</span></button>
            </div>
        </div>

        <div class="sel-row">
            <div class="sel-group" id="sugar-section">
                <span class="modal-section-label">Sugar Level</span>
                <select id="sugar-level-select">
                    <option value="100% Sugar">100% (Normal)</option>
                    <option value="75% Sugar">75% (Less)</option>
                    <option value="50% Sugar">50% (Half)</option>
                    <option value="0% Sugar">0% (No Sugar)</option>
                </select>
            </div>
            <div class="sel-group">
                <span class="modal-section-label">Ice Level</span>
                <select id="ice-level-select">
                    <option value="Normal Ice">Normal Ice</option>
                    <option value="Less Ice">Less Ice</option>
                    <option value="No Ice">No Ice</option>
                </select>
            </div>
        </div>

        <div id="addon-section">
            <span class="modal-section-label">Add-ons</span>
            <div class="addon-grid">
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Nata"> <span>🟡 Nata (+₱10)</span></label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Pearl"> <span>⚫ Pearl (+₱10)</span></label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Coffee Jelly"> <span>☕ Coffee Jelly (+₱10)</span></label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Cloud Foam"> <span>☁️ Cloud Foam (+₱15)</span></label>
            </div>
        </div>

        <div style="text-align:center; margin-top:18px; margin-bottom:4px;">
            <div style="font-size:0.72rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Quantity</div>
            <div class="qty-selector">
                <button class="qty-btn" onclick="changeModalQty('size-qty',-1)">−</button>
                <div class="qty-num" id="size-qty">1</div>
                <button class="qty-btn" onclick="changeModalQty('size-qty',1)">+</button>
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('size-modal').style.display='none'">Cancel</button>
            <button class="btn-add" onclick="confirmAddToCart()">Add to Cart</button>
        </div>
    </div>
</div>

<!-- Fries Flavor Modal -->
<div id="fries-modal" class="modal">
    <div class="modal-content">
        <h2 id="fries-modal-title">Select Flavor</h2>
        <div class="sel-row" style="flex-direction:column; gap:15px; margin-top:20px;">
            <label class="addon-label"><input type="radio" name="fries_flavor" value="Plain" checked> <span>Plain</span></label>
            <label class="addon-label"><input type="radio" name="fries_flavor" value="Cheese"> <span>Cheese</span></label>
            <label class="addon-label"><input type="radio" name="fries_flavor" value="Barbeque"> <span>Barbeque</span></label>
        </div>
        <div style="text-align:center; margin-top:18px; margin-bottom:4px;">
            <div style="font-size:0.72rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px;">Quantity</div>
            <div class="qty-selector">
                <button class="qty-btn" onclick="changeModalQty('fries-qty',-1)">−</button>
                <div class="qty-num" id="fries-qty">1</div>
                <button class="qty-btn" onclick="changeModalQty('fries-qty',1)">+</button>
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('fries-modal').style.display='none'">Cancel</button>
            <button class="btn-add" onclick="confirmFriesToCart()">Add to Cart</button>
        </div>
    </div>
</div>

<!-- Simple Qty Modal (for Frappe, Milk Series, Matcha, Soda, Snacks) -->
<div id="simple-qty-modal" class="modal">
    <div class="modal-content" style="max-width:340px; text-align:center;">
        <h2 id="simple-qty-name" style="font-size:1.3rem; margin-bottom:6px;">Item</h2>
        <div id="simple-qty-price" style="color:var(--gold); font-weight:700; font-size:1rem; margin-bottom:20px;"></div>
        <div style="font-size:0.72rem; font-weight:800; color:var(--text-light); text-transform:uppercase; letter-spacing:1px; margin-bottom:10px;">How many?</div>
        <div class="qty-selector" style="margin-bottom:24px;">
            <button class="qty-btn" onclick="changeModalQty('simple-qty',-1)">−</button>
            <div class="qty-num" id="simple-qty">1</div>
            <button class="qty-btn" onclick="changeModalQty('simple-qty',1)">+</button>
        </div>
        <div class="modal-actions" style="margin-top:0;">
            <button class="btn-cancel" onclick="document.getElementById('simple-qty-modal').style.display='none'">Cancel</button>
            <button class="btn-add" onclick="confirmSimpleQty()">Add to Cart</button>
        </div>
    </div>
</div>

<!-- Success Modal -->
<div id="success-modal" class="modal">
    <div class="modal-content" style="text-align:center; max-width:420px;">
        <div style="font-size:2.5rem; margin-bottom:8px;">✅</div>
        <h2 style="margin-bottom:6px;">Order Placed!</h2>
        <p style="color:var(--text-light); font-weight:600; font-size:0.85rem; margin-bottom:16px;">Show this code to the cashier when picking up your order.</p>
        <div id="display-code" style="font-family:'Playfair Display',serif; font-size:2.5rem; font-weight:900; color:var(--gold); margin-bottom:16px; border:2px dashed var(--gold); padding:15px; border-radius:12px; background:var(--gold-light); letter-spacing:4px;"></div>

        <!-- Receipt preview -->
        <div id="receipt-preview" style="background:#FAFAFA; border:1px solid var(--border-color); border-radius:10px; padding:14px 16px; text-align:left; font-size:0.82rem; margin-bottom:16px; line-height:1.6;">
            <div style="text-align:center; font-family:'Playfair Display',serif; font-weight:900; font-size:1rem; margin-bottom:4px;">9599 Tea & Coffee</div>
            <div style="text-align:center; color:var(--text-light); font-size:0.75rem; margin-bottom:10px;">Official Receipt</div>
            <div id="receipt-details"></div>
            <div style="border-top:1px dashed var(--border-color); margin:10px 0;"></div>
            <div style="display:flex; justify-content:space-between; font-weight:800; font-size:0.9rem;">
                <span>TOTAL</span><span id="receipt-total"></span>
            </div>
            <div style="text-align:center; margin-top:10px; font-size:0.72rem; color:var(--text-light);">Thank you for your order! 🧋</div>
        </div>

        <div style="display:flex; gap:10px;">
            <button class="btn-cancel" style="flex:1;" onclick="closeSuccessAndReset()">Done</button>
        </div>
    </div>
</div>

<!-- Order Limit Modal -->
<div id="perm-modal" class="modal">
    <div class="modal-content" style="text-align:center; max-width:440px;">
        <div style="font-size:2.5rem; margin-bottom:10px;">🛑</div>
        <h2 style="color:var(--danger); margin-bottom:8px;">Order Limit Reached</h2>
        <p style="color:var(--text-light); font-size:0.9rem; font-weight:600; margin-bottom:18px; line-height:1.5;">
            You can only order up to <b style="color:var(--text-dark);">5 items</b> at once.<br>
            To order 5 or more, please request permission from the admin.
        </p>

        <div id="perm-request-code" style="font-family:'Playfair Display',serif; font-size:1.4rem; font-weight:900; color:var(--text-dark); margin-bottom:18px; border:2px dashed var(--border-color); padding:10px 16px; border-radius:12px; background:var(--gold-light); letter-spacing:3px;"></div>

        <div id="perm-form-section">
            <div style="text-align:left; margin-bottom:6px;">
                <label class="pickup-label">Your Name</label>
                <input type="text" id="perm-name-display" class="name-input" readonly style="background:var(--gold-light); cursor:default; margin-bottom:12px;">
            </div>
            <div style="text-align:left; margin-bottom:6px;">
                <label class="pickup-label">Your Address / Location <span style="color:var(--danger);">*</span></label>
                <input type="text" id="perm-address-input" class="name-input" placeholder="e.g. Brgy. Poblacion, San Antonio, Quezon" style="margin-bottom:12px;">
            </div>
            <div style="text-align:left; margin-bottom:12px;">
                <label class="pickup-label">Message to Admin <span style="color:var(--danger);">*</span></label>
                <textarea id="perm-message-input" class="name-input" rows="3" placeholder="e.g. Office bulk order for our team of 8 people." style="resize:none; line-height:1.5;"></textarea>
            </div>

            <div id="perm-send-status" style="font-size:0.85rem; font-weight:700; color:var(--badge-new); margin-bottom:12px; min-height:20px;"></div>

            <div style="display:flex; gap:10px;">
                <button class="btn-cancel" onclick="closePermModal()">Cancel</button>
                <button class="btn-add" id="perm-send-btn" onclick="sendPermissionRequest()"><i class="fas fa-paper-plane"></i> Send Request</button>
                <button class="btn-add" id="perm-place-btn" style="display:none; background:#388E3C;" onclick="submitOrderWithOverride()"><i class="fas fa-check"></i> Place Order</button>
            </div>
        </div>
    </div>
</div>

<script>
    let menuItems = [];
    let cart = [];
    let activeCategory = 'All';
    let pendingItemName = '';
    let pendingSize = '16 oz';
    let pendingPrice = 49;
    let pendingCat = '';
    let orderType = 'Dine-In';

    // ── Persist cart across viewport changes ──────────────────────────────
    function saveCartToSession() {
        try { sessionStorage.setItem('sf_cart', JSON.stringify(cart)); sessionStorage.setItem('sf_orderType', orderType); } catch(e) {}
    }
    function loadCartFromSession() {
        try {
            const c = sessionStorage.getItem('sf_cart');
            const ot = sessionStorage.getItem('sf_orderType');
            if(c) cart = JSON.parse(c);
            if(ot) { orderType = ot; setOrderType(orderType, true); }
        } catch(e) {}
    }

    // ── Notification dropdown ─────────────────────────────────────────────
    let notifMessages = [];
    function toggleNotifDropdown() {
        const dd = document.getElementById('notif-dropdown');
        dd.classList.toggle('open');
        if(dd.classList.contains('open')) {
            // Clear badge when opened
            document.getElementById('notif-badge').style.display = 'none';
        }
    }
    document.addEventListener('click', function(e) {
        const bell = document.getElementById('notif-bell-btn');
        const dd = document.getElementById('notif-dropdown');
        if(dd && bell && !bell.contains(e.target) && !dd.contains(e.target)) dd.classList.remove('open');
    });
    function addNotifMessage(msg) {
        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-PH', {hour:'numeric', minute:'2-digit', hour12:true});
        notifMessages.unshift({msg, time: timeStr});
        const list = document.getElementById('notif-list');
        if(list) list.innerHTML = notifMessages.map(n => `<div class="notif-item">${n.msg}<div class="notif-time">${n.time}</div></div>`).join('');
        const badge = document.getElementById('notif-badge');
        if(badge) { badge.style.display = 'flex'; badge.innerText = notifMessages.length; }
    }

    const STORE_CLOSE_TIME = "{{ close_time }}";

    function escapeHTML(str) { let div = document.createElement('div'); div.innerText = str; return div.innerHTML; }

    const IMAGE_MAP = {
        'Lychee Mogu Soda':            '/static/images/lychee_mogu_soda.jpg',
        'Strawberry Soda':             '/static/images/strawberry_soda.jpg',
        'Blueberry Soda':              '/static/images/blueberry_soda.jpg',
        'Apple Soda':                  '/static/images/green_apple_soda.jpg',
        'Matcha Caramel':              '/static/images/matcha_caramel.jpg',
        'Matcha Frappe':               '/static/images/matcha_frappe.jpg',
        'Matcha Latte':                '/static/images/matcha_latte.jpg',
        'Matcha Strawberry':           '/static/images/matcha_strawberry.jpg',
        'French Fries':                '/static/images/french_fries.jpg',
        'Hash Brown':                  '/static/images/hash_brown.jpg',
        'Onion Rings':                 '/static/images/onion_rings.jpg',
        'Potato Mojos':                '/static/images/potato_mojos.jpg',
        'Blueberry Milk':              '/static/images/blueberry_milk.jpg',
        'Mango Milk':                  '/static/images/mango_milk.jpg',
        'Strawberry Milk':             '/static/images/strawberry_milk.jpg',
        'Ube Milk':                    '/static/images/ube_milk.jpg',
        'Hazelnut':                    '/static/images/hazelnut_milk.jpg',
        'Dark Belgian Choco':          '/static/images/dark_belgian_choco_milktea.jpg',
        'Mango Frappe':                '/static/images/mango_frappe.jpg',
        'Coffee Frappe':               '/static/images/coffee_frappe.jpg',
        'Cookies and Cream Frappe':    '/static/images/cookies_and_cream_frappe.jpg',
        'Mocha Frappe':                '/static/images/mocha_frappe.jpg',
        'Strawberry Frappe':           '/static/images/strawberry_frappe.jpg'
    };

    const EMOJI_MAP = {
        'Taro Milktea':              { em: '🧋', grad: 'grad-default',   badge: 'bestseller' },
        'Okinawa Milktea':           { em: '🧋', grad: 'grad-trending',  badge: 'bestseller' },
        'Wintermelon Milktea':       { em: '🧋', grad: 'grad-default',   badge: 'none' },
        'Cookies and Cream Milktea': { em: '🍪', grad: 'grad-signature', badge: 'none' },
        'Matcha Milktea':            { em: '🍵', grad: 'grad-matcha',    badge: 'none' },
        'Dark Belgian Choco':        { em: '🍫', grad: 'grad-signature', badge: 'none' },
        'Biscoff Milktea':           { em: '☕', grad: 'grad-trending',  badge: 'bestseller' },
        'Mocha':                     { em: '☕', grad: 'grad-trending',  badge: 'none' },
        'Caramel Macchiato':         { em: '🍮', grad: 'grad-trending',  badge: 'bestseller' },
        'Iced Americano':            { em: '🧊', grad: 'grad-default',   badge: 'none' },
        'Cappuccino':                { em: '☕', grad: 'grad-trending',  badge: 'none' },
        'Coffee Jelly Drink':        { em: '☕', grad: 'grad-signature', badge: 'none' },
        'French Vanilla':            { em: '🍦', grad: 'grad-default',   badge: 'none' },
        'Hazelnut':                  { em: '🌰', grad: 'grad-default',   badge: 'none' },
        'French Fries':              { em: '🍟', grad: 'grad-snacks',   badge: 'none' },
        'Hash Brown':                { em: '🟫', grad: 'grad-snacks',   badge: 'none' },
        'Onion Rings':               { em: '🧅', grad: 'grad-snacks',   badge: 'none' },
        'Potato Mojos':              { em: '🥔', grad: 'grad-snacks',   badge: 'none' }
    };

    function getCardStyle(item) {
        if(EMOJI_MAP[item.name]) return EMOJI_MAP[item.name];
        if(item.category === 'Milktea')       return { em: '🧋', grad: 'grad-default',   badge: 'none' };
        if(item.category === 'Coffee')        return { em: '☕', grad: 'grad-trending',  badge: 'none' };
        if(item.category === 'Matcha Series') return { em: '🍵', grad: 'grad-matcha',    badge: 'none' };
        if(item.category === 'Milk Series')   return { em: '🥛', grad: 'grad-default',   badge: 'none' };
        if(item.category === 'Fruit Soda')    return { em: '🍹', grad: 'grad-fruit',     badge: 'none' };
        if(item.category === 'Frappe')        return { em: '🥤', grad: 'grad-signature', badge: 'none' };
        if(item.category === 'Snacks')        return { em: '🍟', grad: 'grad-snacks',    badge: 'none' };
        return { em: '🧋', grad: 'grad-default', badge: 'none' };
    }
    
    document.addEventListener("DOMContentLoaded", () => { 
        loadCartFromSession();
        fetchMenu(); 
        updateCartUI();
        setInterval(pollCustomerOrderStatus, 3000);
        setInterval(fetchMenu, 30000); // Re-sync menu every 30s so stock changes appear automatically
    });

    function showToast(msg, type='info') {
        const t = document.createElement('div');
        t.className = `toast ${type}`;
        t.innerText = msg;
        document.getElementById('toast-container').appendChild(t);
        setTimeout(() => t.remove(), 3000);
    }

    async function fetchMenu() {
        try {
            const res = await fetch('/api/menu');
            menuItems = await res.json();
            const q = document.getElementById('menu-search').value.trim();
            if (q) searchMenu(q); else renderMenu(activeCategory);
        } catch(e) { document.getElementById('menu-grid').innerHTML = '<div style="padding:20px; text-align:center;">Error loading menu.</div>'; }
    }

    function renderMenu(cat) {
        const grid = document.getElementById('menu-grid');
        grid.innerHTML = '';
        let filtered;
        if (cat === 'All') {
            filtered = menuItems;
        } else if (cat === 'Best Sellers') {
            filtered = menuItems.filter(i => getCardStyle(i).badge === 'bestseller');
        } else {
            filtered = menuItems.filter(i => i.category === cat);
        }
        filtered = filtered.filter(i => !i.is_out_of_stock);
        
        if (filtered.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; padding:40px; color:var(--text-light); font-weight:600;">No items available in this category.</div>';
            return;
        }

        filtered.forEach(item => {
            const SIZED_CATS = ['Milktea','Coffee','Milk Series','Matcha Series','Fruit Soda','Frappe'];
            const priceDisplay = SIZED_CATS.includes(item.category) ? `₱${item.price.toFixed(0)} / ₱${(item.price+10).toFixed(0)}` : `₱${item.price.toFixed(0)}`;
            const style = getCardStyle(item);
            let badgeHTML = '';
            if (style.badge === 'bestseller') badgeHTML = '<div class="badge-bestseller">⭐ BEST SELLER</div>';
            else if (style.badge === 'new') badgeHTML = '<div class="badge-new">✨ FAN FAVE</div>';

            let imageContent = `<div class="card-emoji">${style.em}</div>`;
            if (IMAGE_MAP[item.name]) {
                imageContent = `<img src="${IMAGE_MAP[item.name]}" class="card-real-img" onerror="this.style.display='none'">`;
            }

            grid.innerHTML += `
                <div class="card" onclick="addToCart('${escapeHTML(item.name).replace(/'/g,"\\'")}', '${escapeHTML(item.category).replace(/'/g,"\\'")}', ${item.price})">
                    <div class="card-img-container ${style.grad}">
                        ${badgeHTML}
                        ${imageContent}
                        <div class="card-price">${priceDisplay}</div>
                    </div>
                    <div class="card-body">
                        <div class="card-title">${escapeHTML(item.name)}</div>
                        <div class="card-cat-tag">${escapeHTML(item.category)}</div>
                    </div>
                </div>`;
        });
    }

    function filterMenu(cat, btn) {
        document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeCategory = cat;
        document.getElementById('menu-search').value = '';
        renderMenu(cat);
    }

    function searchMenu(query) {
        const q = query.trim().toLowerCase();
        if (!q) { renderMenu(activeCategory); return; }
        const grid = document.getElementById('menu-grid');
        grid.innerHTML = '';
        const filtered = menuItems.filter(i => !i.is_out_of_stock && (i.name.toLowerCase().includes(q) || i.category.toLowerCase().includes(q)));
        if (filtered.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; padding:40px; color:var(--text-light); font-weight:600;">No items found.</div>';
            return;
        }
        filtered.forEach(item => {
            const SIZED_CATS = ['Milktea','Coffee','Milk Series','Matcha Series','Fruit Soda','Frappe'];
            const priceDisplay = SIZED_CATS.includes(item.category) ? `₱${item.price.toFixed(0)} / ₱${(item.price+10).toFixed(0)}` : `₱${item.price.toFixed(0)}`;
            const style = getCardStyle(item);
            let badgeHTML = '';
            if (style.badge === 'bestseller') badgeHTML = '<div class="badge-bestseller">⭐ BEST SELLER</div>';
            else if (style.badge === 'new') badgeHTML = '<div class="badge-new">✨ FAN FAVE</div>';
            let imageContent = `<div class="card-emoji">${style.em}</div>`;
            if (IMAGE_MAP[item.name]) imageContent = `<img src="${IMAGE_MAP[item.name]}" class="card-real-img" onerror="this.style.display='none'">`;
            grid.innerHTML += `
                <div class="card" onclick="addToCart('${escapeHTML(item.name).replace(/'/g,"\\'")}', '${escapeHTML(item.category).replace(/'/g,"\\'")}', ${item.price})">
                    <div class="card-img-container ${style.grad}">
                        ${badgeHTML}
                        ${imageContent}
                        <div class="card-price">${priceDisplay}</div>
                    </div>
                    <div class="card-body">
                        <div class="card-title">${escapeHTML(item.name)}</div>
                        <div class="card-cat-tag">${escapeHTML(item.category)}</div>
                    </div>
                </div>`;
        });
    }

    function setOrderType(type, silent) {
        orderType = type;
        document.getElementById('btn-dine-in').className = type === 'Dine-In' ? 'type-btn active' : 'type-btn';
        document.getElementById('btn-take-out').className = type === 'Take-Out' ? 'type-btn active' : 'type-btn';
        if(!silent) saveCartToSession();
    }

    let sizePrice16 = 49, sizePrice22 = 59;

    function selectSize(size) {
        const price = size === '16 oz' ? sizePrice16 : sizePrice22;
        pendingSize = size; pendingPrice = price;
        document.getElementById('btn-size-16').className = size === '16 oz' ? 'size-btn selected' : 'size-btn';
        document.getElementById('btn-size-22').className = size === '22 oz' ? 'size-btn selected' : 'size-btn';
    }

    function openSizeModal(name, cat, price, showSugar, showAddons) {
        sizePrice16 = price;
        sizePrice22 = price + 10;
        document.getElementById('size-modal-title').innerText = name;
        document.getElementById('size-price-16').innerText = '₱' + sizePrice16;
        document.getElementById('size-price-22').innerText = '₱' + sizePrice22;
        document.getElementById('size-row-section').style.display = '';
        document.getElementById('sugar-section').style.display = showSugar ? '' : 'none';
        document.getElementById('addon-section').style.display = showAddons ? '' : 'none';
        document.querySelectorAll('.addon-checkbox').forEach(cb => cb.checked = false);
        document.getElementById('sugar-level-select').value = '100% Sugar';
        document.getElementById('ice-level-select').value = 'Normal Ice';
        document.getElementById('size-qty').innerText = '1';
        selectSize('16 oz');
        document.getElementById('size-modal').style.display = 'flex';
    }

    function addToCart(name, cat, price) {
        pendingItemName = name;
        pendingCat = cat;
        pendingPrice = price;

        const DRINK_CATS = ['Milktea', 'Coffee', 'Milk Series', 'Matcha Series', 'Fruit Soda', 'Frappe'];

        if (cat === 'Milktea' || cat === 'Coffee') {
            // Size + sugar + ice + add-ons
            openSizeModal(name, cat, price, true, true);
        } else if (cat === 'Milk Series') {
            // Size + sugar + ice + add-ons
            openSizeModal(name, cat, price, true, true);
        } else if (cat === 'Matcha Series') {
            // Size + ice only
            openSizeModal(name, cat, price, false, false);
        } else if (cat === 'Fruit Soda') {
            // Size + ice only
            openSizeModal(name, cat, price, false, false);
        } else if (cat === 'Frappe') {
            // Size + ice only
            openSizeModal(name, cat, price, false, false);
        } else if (name === 'French Fries') {
            document.getElementById('fries-modal-title').innerText = 'French Fries — Choose Flavor';
            document.querySelector('input[name="fries_flavor"][value="Plain"]').checked = true;
            document.getElementById('fries-qty').innerText = '1';
            document.getElementById('fries-modal').style.display = 'flex';
        } else {
            // Snacks etc. — simple qty
            document.getElementById('simple-qty-name').innerText = name;
            document.getElementById('simple-qty-price').innerText = '₱' + price + ' each';
            document.getElementById('simple-qty').innerText = '1';
            document.getElementById('simple-qty-modal').style.display = 'flex';
        }
    }

    function confirmAddToCart() {
        let addons = []; let cost = 0;
        const addonSection = document.getElementById('addon-section');
        if (addonSection.style.display !== 'none') {
            document.querySelectorAll('.addon-checkbox').forEach(cb => {
                if(cb.checked) { addons.push(cb.value); cost += (cb.value==='Cloud Foam'?15:10); }
            });
        }
        const sugarSection = document.getElementById('sugar-section');
        const sugar = sugarSection.style.display !== 'none'
            ? document.getElementById('sugar-level-select').value
            : 'N/A';
        const qty = parseInt(document.getElementById('size-qty').innerText) || 1;
        const item = {
            name: pendingItemName, cat: pendingCat, size: pendingSize,
            sugar: sugar,
            ice: document.getElementById('ice-level-select').value,
            addons, price: pendingPrice + cost
        };
        for(let i = 0; i < qty; i++) cart.push({...item});
        document.getElementById('size-modal').style.display = 'none';
        document.getElementById('size-qty').innerText = '1';
        updateCartUI();
    }

    function changeModalQty(id, delta) {
        const el = document.getElementById(id);
        let v = parseInt(el.innerText) + delta;
        if(v < 1) v = 1; if(v > 20) v = 20;
        el.innerText = v;
    }

    function confirmFriesToCart() {
        const qty = parseInt(document.getElementById('fries-qty').innerText) || 1;
        const flavor = document.querySelector('input[name="fries_flavor"]:checked').value;
        const displayName = `French Fries (${flavor})`;
        for(let i = 0; i < qty; i++) cart.push({name: displayName, cat: pendingCat, size: 'Regular', sugar: 'N/A', ice: 'N/A', addons: [], price: pendingPrice});
        document.getElementById('fries-modal').style.display = 'none';
        document.getElementById('fries-qty').innerText = '1';
        updateCartUI();
    }


    function confirmSimpleQty() {
        const qty = parseInt(document.getElementById('simple-qty').innerText) || 1;
        for(let i = 0; i < qty; i++) {
            cart.push({name: pendingItemName, cat: pendingCat, size: 'Regular', sugar: 'N/A', ice: 'N/A', addons: [], price: pendingPrice});
        }
        document.getElementById('simple-qty-modal').style.display = 'none';
        document.getElementById('simple-qty').innerText = '1';
        updateCartUI();
    }

    function removeFromCart(i) { cart.splice(i,1); saveCartToSession(); updateCartUI(); }

    function updateCartUI() {
        const list = document.getElementById('cart-items');
        const empty = document.getElementById('empty-cart');
        const count = document.getElementById('cart-count');
        let total = 0; list.innerHTML = '';
        
        if(cart.length === 0) {
            empty.style.display = 'block'; count.style.display = 'none';
        } else {
            empty.style.display = 'none'; count.style.display = 'flex'; count.innerText = cart.length;
            cart.forEach((c, i) => {
                total += c.price;
                const SIZED_CATS = ['Milktea','Coffee','Milk Series','Matcha Series','Fruit Soda','Frappe'];
                let subParts = [];
                if (SIZED_CATS.includes(c.cat)) {
                    if (c.size && c.size !== 'Regular') subParts.push(c.size);
                    if (c.sugar && c.sugar !== 'N/A') subParts.push(c.sugar);
                    if (c.ice && c.ice !== 'N/A') subParts.push(c.ice);
                }
                let sub = subParts.join(' · ');
                let adds = c.addons && c.addons.length ? `<br>+ ${c.addons.join(', ')}` : '';
                list.innerHTML += `
                <div class="cart-item">
                    <div>
                        <div class="cart-item-name">${escapeHTML(c.name)}</div>
                        <div class="cart-item-sub">${sub}${adds}</div>
                    </div>
                    <div class="cart-item-right">
                        <div class="cart-item-price">₱${c.price}</div>
                        <div class="cart-item-del" onclick="removeFromCart(${i})"><i class="fas fa-trash-alt"></i> Remove</div>
                    </div>
                </div>`;
            });
        }
        document.getElementById('cart-total').innerText = `₱${total.toFixed(2)}`;
        saveCartToSession();
        checkCheckoutStatus();
    }

    function checkCheckoutStatus() {
        const n = document.getElementById('customer-name').value.trim();
        const g = document.getElementById('customer-gmail').value.trim();
        const ph = document.getElementById('customer-phone').value.trim();
        const p = document.getElementById('pickup-time').value.trim();
        const btn = document.getElementById('checkout-btn');
        if(cart.length > 0 && n && g && ph && p) { btn.className = 'checkout-btn active'; btn.disabled = false; }
        else { btn.className = 'checkout-btn'; btn.disabled = true; }
    }

    // ── Slide Clock ──────────────────────────────────────────────────────
    let scHour = 12, scMin = 0, scAmpm = 'PM';

    function updatePickupClockDisplay() {
        const hStr = String(scHour).padStart(2,'0');
        const mStr = String(scMin).padStart(2,'0');
        const timeStr = `${hStr}:${mStr} ${scAmpm}`;
        document.getElementById('sc-hour').innerText = hStr;
        document.getElementById('sc-min').innerText = mStr;
        document.getElementById('sc-ampm').innerText = scAmpm;
        document.getElementById('pickup-time').value = timeStr;
        checkCheckoutStatus();
    }

    function adjustPickupTime(part, dir) {
        if(part === 'hour') { scHour += dir; if(scHour > 12) scHour = 1; if(scHour < 1) scHour = 12; }
        if(part === 'min') { scMin += dir * 5; if(scMin >= 60) scMin = 0; if(scMin < 0) scMin = 55; }
        if(part === 'ampm') { scAmpm = scAmpm === 'AM' ? 'PM' : 'AM'; }
        updatePickupClockDisplay();
    }

    // Init clock to current time + 15 min
    (function initPickupClock() {
        const now = new Date();
        now.setMinutes(now.getMinutes() + 15);
        scHour = now.getHours() % 12 || 12;
        scMin = Math.round(now.getMinutes() / 5) * 5 % 60;
        scAmpm = now.getHours() >= 12 ? 'PM' : 'AM';
        updatePickupClockDisplay();
    })();

    function parseTimeStr(tStr) {
        try {
            const match = tStr.match(/^(1[0-2]|0?[1-9]):([0-5][0-9]) ?(AM|PM)$/i);
            if(!match) return -1;
            let h = parseInt(match[1], 10);
            let m = parseInt(match[2], 10);
            let mod = match[3].toUpperCase();
            if(mod === 'PM' && h < 12) h += 12;
            if(mod === 'AM' && h === 12) h = 0;
            return h * 60 + m;
        } catch(e) { return -1; }
    }

    /* Permission Logic */
    let permissionGranted = false;
    let permPoll = null;

    function closePermModal() {
        document.getElementById('perm-modal').style.display = 'none';
        document.getElementById('perm-send-status').innerText = '';
        document.getElementById('perm-send-btn').disabled = false;
        document.getElementById('perm-send-btn').innerHTML = '<i class="fas fa-paper-plane"></i> Send Request';
        document.getElementById('perm-send-btn').style.display = 'inline-flex';
        document.getElementById('perm-place-btn').style.display = 'none';
        if(permPoll) { clearInterval(permPoll); permPoll = null; }
    }

    async function sendPermissionRequest() {
        const address = document.getElementById('perm-address-input').value.trim();
        const message = document.getElementById('perm-message-input').value.trim();
        if(!address || !message) {
            document.getElementById('perm-send-status').style.color = 'var(--danger)';
            document.getElementById('perm-send-status').innerText = 'Please fill in your address and message.';
            return;
        }
        const payload = {
            name: document.getElementById('customer-name').value,
            address,
            message,
            code: document.getElementById('perm-request-code').innerText
        };
        const btn = document.getElementById('perm-send-btn');
        const stat = document.getElementById('perm-send-status');
        btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sending...';
        stat.style.color = 'var(--text-light)';
        stat.innerText = '';
        try {
            const res = await fetch('/api/permission_request', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
            if(res.ok) {
                stat.style.color = 'var(--badge-new)';
                stat.innerText = '✅ Request sent! Waiting for admin approval...';
                permPoll = setInterval(()=>checkPermissionStatus(payload.code), 3000);
            } else {
                stat.style.color = 'var(--danger)';
                stat.innerText = 'Error sending request. Please try again.';
                btn.disabled = false; btn.innerHTML = '<i class="fas fa-paper-plane"></i> Send Request';
            }
        } catch(e) {
            stat.style.color = 'var(--danger)';
            stat.innerText = 'Network error. Please try again.';
            btn.disabled = false; btn.innerHTML = '<i class="fas fa-paper-plane"></i> Send Request';
        }
    }

    async function checkPermissionStatus(code) {
        try {
            const res = await fetch(`/api/permission_status?code=${encodeURIComponent(code)}`);
            const data = await res.json();
            if(data.granted) {
                permissionGranted = true;
                clearInterval(permPoll); permPoll = null;
                document.getElementById('perm-send-btn').style.display = 'none';
                document.getElementById('perm-place-btn').style.display = 'inline-flex';
                document.getElementById('perm-send-status').style.color = '#388E3C';
                document.getElementById('perm-send-status').innerText = '✅ Permission Granted! You may now place your order.';
                document.getElementById('alert-audio').play().catch(()=>{});
            }
        } catch(e){}
    }

    async function submitOrder() {
        if(cart.length === 0) return;
        
        let tStr = document.getElementById('pickup-time').value.trim();
        let pTimeMins = parseTimeStr(tStr);
        let closeMins = parseTimeStr(STORE_CLOSE_TIME);

        if(pTimeMins === -1) {
            showToast("Please enter a valid time (e.g. 2:30 PM)", "error");
            return;
        }

        if(pTimeMins > closeMins) {
            showToast("Store is closed at this time.", "error");
            return;
        }

        const totalItems = cart.length;
        if(totalItems >= 5 && !permissionGranted) {
            document.getElementById('alert-audio').play().catch(()=>{});
            document.getElementById('perm-request-code').innerText = "REQ-" + Math.floor(Math.random()*90000 + 10000);
            document.getElementById('perm-name-display').value = document.getElementById('customer-name').value || 'Customer';
            document.getElementById('perm-address-input').value = '';
            document.getElementById('perm-message-input').value = '';
            document.getElementById('perm-send-status').innerText = '';
            document.getElementById('perm-send-btn').disabled = false;
            document.getElementById('perm-send-btn').innerHTML = '<i class="fas fa-paper-plane"></i> Send Request';
            document.getElementById('perm-send-btn').style.display = 'inline-flex';
            document.getElementById('perm-place-btn').style.display = 'none';
            document.getElementById('perm-modal').style.display = 'flex';
            return;
        }

        const btn = document.getElementById('checkout-btn');
        btn.innerHTML = 'Processing...'; btn.disabled = true;
        
        const payload = {
            name: document.getElementById('customer-name').value,
            email: document.getElementById('customer-gmail').value,
            phone: document.getElementById('customer-phone').value,
            pickup_time: tStr,
            total: cart.reduce((s,i)=>s+i.price, 0),
            items: cart.map(i => ({ foundation: i.name, size: i.size, sweetener: i.sugar, ice: i.ice, addons: i.addons.join(', '), pearls: orderType, price: i.price }))
        };
        try {
            const res = await fetch('/reserve', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            const data = await res.json();
            if(res.ok) {
                const code = data.reservation_code;
                document.getElementById('display-code').innerText = code;

                // Build receipt preview
                const name = document.getElementById('customer-name').value;
                const pickup = tStr;
                const total = payload.total;
                let rows = payload.items.map(i => {
                    let detail = i.size && i.size !== 'Regular' ? ` (${i.size})` : '';
                    let mods = [i.sweetener, i.ice].filter(v => v && v !== 'N/A').join(', ');
                    let addons = i.addons ? ` +${i.addons}` : '';
                    return `<div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                        <span>${escapeHTML(i.foundation)}${escapeHTML(detail)}${mods ? '<br><span style="font-size:0.72rem; color:#888;">' + escapeHTML(mods) + escapeHTML(addons) + '</span>' : ''}</span>
                        <span style="font-weight:700; white-space:nowrap; padding-left:8px;">₱${i.price.toFixed(2)}</span>
                    </div>`;
                }).join('');
                document.getElementById('receipt-details').innerHTML = `
                    <div style="margin-bottom:6px;"><b>Code:</b> ${escapeHTML(code)}</div>
                    <div style="margin-bottom:6px;"><b>Name:</b> ${escapeHTML(name)}</div>
                    <div style="margin-bottom:8px;"><b>Pick-up:</b> ${escapeHTML(pickup)}</div>
                    <div style="border-top:1px dashed #ddd; margin-bottom:8px;"></div>
                    ${rows}`;
                document.getElementById('receipt-total').innerText = `₱${total.toFixed(2)}`;

                // Store receipt data for printing
                window._lastReceipt = { code, name, pickup, total, items: payload.items, source: 'Online' };

                document.getElementById('success-modal').style.display = 'flex';
                try { sessionStorage.removeItem('sf_cart'); sessionStorage.removeItem('sf_orderType'); } catch(e) {}
                let orders = JSON.parse(localStorage.getItem('myOrders')) || [];
                orders.push({code, status: 'Waiting Confirmation'});
                localStorage.setItem('myOrders', JSON.stringify(orders));
            } else { showToast("Error: " + data.message, "error"); btn.innerHTML = '<i class="fas fa-plane"></i> Place My Order'; btn.disabled = false; }
        } catch(e) { showToast("Connection Error", "error"); btn.innerHTML = '<i class="fas fa-plane"></i> Place My Order'; btn.disabled = false; }
    }

    async function submitOrderWithOverride() {
        document.getElementById('perm-modal').style.display = 'none';
        if(permPoll) clearInterval(permPoll);
        await submitOrder();
    }

    function closeSuccessAndReset() {
        // Hide the modal without reloading the page (reload would trigger the closed-store page)
        document.getElementById('success-modal').style.display = 'none';
        // Clear cart state
        cart = [];
        try { sessionStorage.removeItem('sf_cart'); sessionStorage.removeItem('sf_orderType'); } catch(e) {}
        // Reset form fields
        ['customer-name','customer-gmail','customer-phone','pickup-time'].forEach(id => {
            const el = document.getElementById(id); if(el) el.value = '';
        });
        // Re-render the cart UI
        updateCartUI();
    }

    function openReceiptWindow(r) {
        const now = new Date();
        const dateStr = now.toLocaleDateString('en-PH', {day:'numeric', month:'short', year:'numeric'});
        const timeStr = now.toLocaleTimeString('en-PH', {hour:'numeric', minute:'2-digit', hour12:true});

        // Aggregate items: group by name+size, count qty, sum amount
        const itemMap = {};
        r.items.forEach(i => {
            const key = i.foundation + (i.size && i.size !== 'Regular' ? ` (${i.size})` : '');
            if(!itemMap[key]) itemMap[key] = { name: key, qty: 0, amount: 0 };
            itemMap[key].qty += 1;
            itemMap[key].amount += i.price;
        });

        let rows = Object.values(itemMap).map(item => {
            const unitPrice = (item.amount / item.qty).toFixed(2);
            return `
            <tr>
                <td style="padding:6px 8px 2px 8px;" colspan="2">${item.name}</td>
            </tr>
            <tr>
                <td style="padding:2px 8px 8px 8px; color:#555;">${item.qty} &times; &#8369;${unitPrice}</td>
                <td style="padding:2px 8px 8px 8px; text-align:right; font-weight:bold;">&#8369;${item.amount.toFixed(2)}</td>
            </tr>`;
        }).join('');

        const totalQty = Object.values(itemMap).reduce((s,i) => s+i.qty, 0);

        const html = `<!DOCTYPE html><html><head><meta charset="UTF-8">
        <title>Receipt — ${r.code}</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Courier New', Courier, monospace; font-size: 16px; color: #111; background: white; padding: 32px 24px; max-width: 560px; margin: 0 auto; }
            .center { text-align: center; }
            .bold { font-weight: bold; }
            .header-section { display: flex; flex-direction: column; align-items: center; text-align: center; margin-bottom: 10px; }
            .logo-img { width: 80px; height: 80px; border-radius: 50%; object-fit: cover; border: 2px solid #6F4E37; margin-bottom: 8px; }
            .shop-name { font-size: 1.5rem; font-weight: bold; margin-bottom: 2px; }
            .shop-tagline { font-size: 0.85rem; color: #6F4E37; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 2px; }
            .shop-meta { font-size: 0.82rem; color: #444; margin-top: 2px; }
            .divider-solid { border: none; border-top: 1px solid #333; margin: 10px 0; }
            .divider-dash { border: none; border-top: 1px dashed #999; margin: 10px 0; }
            table { width: 100%; border-collapse: collapse; }
            th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #333; font-weight: bold; }
            th.right { text-align: right; }
            .total-section td { padding: 6px 8px; font-weight: bold; }
            .footer { text-align: center; font-size: 0.9rem; margin-top: 18px; color: #333; }
            .footer .est { font-size: 0.78rem; color: #888; margin-top: 4px; }
            @media print { @page { margin: 10mm; size: A5; } body { padding: 0; } }
        </style></head>
        <body>
        <div class="header-section">
            <img src="/static/images/9599.jpg" class="logo-img" onerror="this.style.display='none'">
            <div class="shop-name">9599 Tea &amp; Coffee</div>
            <div class="shop-tagline">Parne Na!</div>
            <div class="shop-meta">📍 Brgy. Poblacion, San Antonio, Quezon, Philippines</div>
            <div class="shop-meta">BIR TIN: 322-845-268-00000</div>
        </div>
        <hr class="divider-solid">
        <div class="center" style="font-size:1rem; font-weight:bold; letter-spacing:1px;">OFFICIAL RECEIPT</div>
        <div class="center" style="font-size:0.88rem; color:#555; margin-top:4px;">Date: ${dateStr} &nbsp;|&nbsp; Time: ${timeStr}</div>
        <hr class="divider-solid">
        <div style="font-size:1rem; margin-bottom:4px;"><b>Order #:</b> ${r.code}</div>
        <div style="font-size:1rem; margin-bottom:4px;"><b>Customer:</b> ${r.name}</div>
        <div style="font-size:1rem; margin-bottom:6px;"><b>Pick-up:</b> ${r.pickup || 'Walk-In'}</div>
        <hr class="divider-solid">
        <table>
            <thead><tr><th>Item</th><th class="right">Amount</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <hr class="divider-dash">
        <table class="total-section">
            <tr>
                <td style="text-align:left;">Total Items: ${totalQty}</td>
                <td style="text-align:right;">&#8369;${r.total.toFixed(2)}</td>
            </tr>
        </table>
        <hr class="divider-dash">
        <div class="footer">
            Thank you for ordering!<br>
            9599 Tea &amp; Coffee Shop
            <div class="est">Est. ${new Date().getFullYear()} &nbsp;·&nbsp; This serves as your official receipt.</div>
        </div>
        <script>window.onload=()=>{ setTimeout(()=>{ window.print(); window.onafterprint=()=>window.close(); }, 300); }<\/script>
        </body></html>`;

        const w = window.open('', '_blank', 'width=680,height=900');
        if(w) { w.document.write(html); w.document.close(); }
    }

    function printCustomerReceipt() {
        if(window._lastReceipt) openReceiptWindow(window._lastReceipt);
    }

    async function pollCustomerOrderStatus() {
        let orders = JSON.parse(localStorage.getItem('myOrders')) || [];
        if(orders.length === 0) return;
        const codes = orders.map(o=>o.code).join(',');
        try {
            const res = await fetch(`/api/customer/status?codes=${codes}`);
            const data = await res.json();
            let updated = false;
            data.forEach(srv => {
                const loc = orders.find(o=>o.code === srv.code);
                if(loc && loc.status !== srv.status) {
                    loc.status = srv.status; updated = true;
                    const msg = `Order #${srv.code} is now: ${srv.status}`;
                    showToast(msg, "success");
                    addNotifMessage(msg);
                    document.getElementById('status-audio').play().catch(()=>{});
                }
            });
            if(updated) localStorage.setItem('myOrders', JSON.stringify(orders));
        } catch(e) {}
    }

    // ── Location Modal ──────────────────────────────────────────────────────
    const SHOP_GMAPS_URL = 'https://maps.app.goo.gl/Q1ct25HxyR9S5SLr9';
    const SHOP_LAT = 13.5833;   // San Antonio, Quezon approximate coords
    const SHOP_LNG = 121.3667;

    function openLocModal() {
        document.getElementById('loc-modal').classList.add('show');
        document.body.style.overflow = 'hidden';
    }

    function closeLocModal() {
        document.getElementById('loc-modal').classList.remove('show');
        document.body.style.overflow = '';
    }

    function openGoogleMaps() {
        // Direct link to the exact pin shared by the shop
        window.open('https://maps.app.goo.gl/Q1ct25HxyR9S5SLr9', '_blank');
    }

    function openWaze() {
        // Waze deep link using coordinates
        const wazeUrl = `https://waze.com/ul?ll=${SHOP_LAT},${SHOP_LNG}&navigate=yes`;
        window.open(wazeUrl, '_blank');
    }

    // Close modal on back button (mobile)
    window.addEventListener('popstate', () => closeLocModal());
</script>
{% endif %}
</body>
</html>
"""

# --- Admin Dashboard Template (NEW UI - Connected Live) ---
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link rel="icon" type="image/jpeg" href="/static/images/9599.jpg">
<title>9599 Admin Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
/* ══ LOGO-BASED PALETTE ══════════════════════════════════════════
   #7B4F2E  --brown       primary warm brown (logo text)
   #3D2410  --brown-dark  dark espresso (deepest shade)
   #5A3520  --brown-mid   mid brown
   #A0724A  --brown-light lighter brown
   #F0EDE4  --cream       background (logo circle fill)
   #E2DAC8  --cream-dark  borders / card dividers
   #C4A882  --tan         accent / highlights
   #FDFAF5  --white       card background
   #2A1505  --text        body text
   #8D6E55  --muted       secondary text
   #C0392B  --red         danger/error
   #27AE60  --green       success
   #F57C00  --orange      warning (waiting)
   #1976D2  --blue        info (preparing)
══════════════════════════════════════════════════════════════════ */
:root{
  --brown:#7B4F2E; --brown-dark:#3D2410; --brown-mid:#5A3520; --brown-light:#A0724A;
  --cream:#F0EDE4; --cream-dark:#E2DAC8; --tan:#C4A882;
  --white:#FDFAF5; --text:#2A1505; --muted:#8D6E55;
  --red:#C0392B; --green:#27AE60; --orange:#F57C00; --blue:#1976D2;
  --shadow:0 4px 18px rgba(61,36,16,0.12);
  --radius:14px; --nav-h:64px; --topbar-h:60px;
  --border:rgba(123,79,46,0.15);
}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Nunito',sans-serif;-webkit-tap-highlight-color:transparent;}
html,body{height:100%;overflow:hidden;}
body{background:var(--cream);color:var(--text);display:flex;flex-direction:column;}

/* ── TOAST ── */
#toast-container{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none;min-width:200px;}
.toast{background:var(--brown-dark);color:var(--cream);padding:10px 20px;border-radius:20px;font-weight:700;font-size:0.84rem;box-shadow:0 4px 16px rgba(61,36,16,0.35);text-align:center;}
.toast.error{background:var(--red);}
.toast.success{background:var(--green);}

/* ── TOPBAR ── */
.topbar{height:var(--topbar-h);background:var(--brown-dark);border-bottom:3px solid var(--brown);display:flex;align-items:center;justify-content:space-between;padding:0 16px;flex-shrink:0;box-shadow:0 2px 12px rgba(61,36,16,0.3);z-index:100;}
.topbar-logo{display:flex;align-items:center;gap:10px;}
.logo-circle{width:36px;height:36px;border-radius:50%;border:2px solid var(--tan);overflow:hidden;flex-shrink:0;background:var(--cream);display:flex;align-items:center;justify-content:center;}
.logo-circle img{width:100%;height:100%;object-fit:cover;}
.logo-circle .lf{font-size:1rem;}
.brand{font-family:'Playfair Display',serif;font-size:1rem;font-weight:900;color:var(--cream);line-height:1.1;}
.brand-sub{font-size:0.6rem;color:var(--tan);font-weight:700;letter-spacing:1px;text-transform:uppercase;}
.admin-pill{background:var(--brown);border:1px solid var(--tan);color:var(--tan);font-size:0.62rem;font-weight:800;padding:3px 9px;border-radius:20px;letter-spacing:0.5px;display:inline-flex;align-items:center;gap:4px;}
.topbar-right{display:flex;align-items:center;gap:9px;position:relative;flex-shrink:0;}
.clock-chip{background:rgba(255,255,255,0.1);border:1px solid rgba(196,168,130,0.4);color:var(--tan);padding:5px 12px;border-radius:20px;font-size:0.79rem;font-weight:800;white-space:nowrap;}
.notif-btn{background:rgba(255,255,255,0.1);border:1px solid rgba(196,168,130,0.35);color:var(--tan);width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;cursor:pointer;position:relative;font-size:0.9rem;flex-shrink:0;transition:background 0.15s;}
.notif-btn:hover{background:rgba(196,168,130,0.2);}
.nbadge{position:absolute;top:-4px;right:-4px;background:var(--red);color:#fff;border-radius:50%;min-width:16px;height:16px;padding:0 3px;font-size:0.58rem;font-weight:900;display:none;align-items:center;justify-content:center;border:2px solid var(--brown-dark);}
.notif-panel{display:none;position:absolute;top:48px;right:0;background:var(--white);border:1.5px solid var(--cream-dark);border-radius:var(--radius);width:280px;box-shadow:0 10px 40px rgba(61,36,16,0.2);z-index:500;flex-direction:column;max-height:360px;overflow:hidden;}
.notif-ph{padding:12px 14px;border-bottom:1px solid var(--cream-dark);font-weight:800;display:flex;justify-content:space-between;align-items:center;font-size:0.87rem;color:var(--text);}
.notif-pb{padding:8px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:6px;}
.notif-item{padding:9px 11px;border-radius:10px;background:var(--cream);border:1px solid var(--cream-dark);font-size:0.8rem;}

/* ── SCREENS ── */
.screens{flex:1;overflow:hidden;position:relative;}
.screen{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--cream);display:none;padding:0 0 calc(var(--nav-h)+12px);}
.screen.active{display:block;}
.screen::-webkit-scrollbar{width:3px;}
.screen::-webkit-scrollbar-thumb{background:var(--cream-dark);border-radius:3px;}

/* ── PAGE HEADER ── */
.page-header{background:linear-gradient(135deg,var(--brown-dark) 0%,var(--brown-mid) 60%,var(--brown) 100%);padding:18px 16px 24px;position:relative;overflow:hidden;}
.page-header::after{content:'';position:absolute;bottom:-16px;left:0;right:0;height:32px;background:var(--cream);border-radius:20px 20px 0 0;}
.page-header h2{font-family:'Playfair Display',serif;font-size:1.2rem;font-weight:900;color:var(--cream);display:flex;align-items:center;gap:9px;}
.page-header p{font-size:0.76rem;color:var(--tan);margin-top:3px;}

/* ── BOTTOM NAV ── */
.bottom-nav{height:var(--nav-h);background:var(--brown-dark);border-top:2px solid var(--brown);display:flex;flex-shrink:0;position:relative;z-index:100;}
.nav-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;cursor:pointer;color:var(--tan);font-size:9px;font-weight:800;letter-spacing:0.3px;border:none;background:transparent;transition:color 0.15s;padding:0;opacity:0.65;}
.nav-btn i{font-size:18px;}
.nav-btn.active{color:var(--cream);opacity:1;}
.nav-btn.active i{transform:scale(1.1);}
.nav-center-wrap{flex:1;display:flex;align-items:center;justify-content:center;position:relative;}
.nav-center-btn{width:50px;height:50px;border-radius:50%;background:linear-gradient(135deg,var(--brown) 0%,var(--tan) 100%);display:flex;align-items:center;justify-content:center;color:var(--cream);font-size:20px;border:3px solid var(--brown-dark);box-shadow:0 4px 14px rgba(61,36,16,0.5);cursor:pointer;position:absolute;top:-18px;left:50%;transform:translateX(-50%);}

/* ── CARDS ── */
.card{background:var(--white);border-radius:var(--radius);padding:16px;border:1px solid var(--cream-dark);box-shadow:var(--shadow);}
.card-title{font-size:0.7rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;}

/* ── STAT CARDS ── */
.stat-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:28px 14px 14px;}
.stat-card{background:var(--white);border-radius:12px;padding:13px;box-shadow:var(--shadow);border-top:3px solid;}
.stat-card.s1{border-top-color:var(--brown);}
.stat-card.s2{border-top-color:var(--green);}
.stat-card.s3{border-top-color:var(--red);}
.stat-card .sl{font-size:0.66rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:4px;}
.stat-card .sv{font-family:'Playfair Display',serif;font-size:1.3rem;font-weight:900;line-height:1;}
.stat-card.s1 .sv{color:var(--brown);}
.stat-card.s2 .sv{color:var(--green);}
.stat-card.s3 .sv{color:var(--red);}

/* ── TABLES ── */
.tbl-wrap{overflow:auto;border:1.5px solid var(--cream-dark);border-radius:12px;}
.tbl-wrap::-webkit-scrollbar{height:3px;width:3px;}
.tbl-wrap::-webkit-scrollbar-thumb{background:var(--cream-dark);border-radius:3px;}
.kds-table{width:100%;border-collapse:collapse;min-width:480px;}
.kds-table th,.kds-table td{padding:10px 13px;text-align:left;border-bottom:1px solid var(--cream-dark);font-size:0.81rem;}
.kds-table th{background:var(--cream);color:var(--muted);position:sticky;top:0;font-weight:900;font-size:0.68rem;text-transform:uppercase;letter-spacing:0.5px;}
.kds-table tbody tr:hover{background:#FAF6F0;}
.kds-badge{font-size:0.68rem;padding:2px 8px;border-radius:20px;font-weight:800;}

/* ── BUTTONS ── */
.btn-primary{background:linear-gradient(135deg,var(--brown) 0%,var(--brown-dark) 100%);color:var(--cream);border:none;padding:12px 16px;border-radius:12px;font-weight:800;cursor:pointer;width:100%;margin-bottom:10px;font-family:'Nunito',sans-serif;font-size:0.87rem;box-shadow:0 3px 12px rgba(61,36,16,0.3);transition:opacity 0.2s;}
.btn-primary:hover{opacity:0.9;}
.btn-secondary{background:var(--brown-mid);color:var(--cream);border:none;padding:8px 12px;border-radius:8px;font-weight:700;cursor:pointer;font-family:'Nunito',sans-serif;font-size:0.79rem;transition:background 0.15s;}
.btn-secondary:hover{background:var(--brown);}
.btn-outline{background:transparent;color:var(--brown);border:1.5px solid var(--cream-dark);padding:8px 12px;border-radius:8px;font-weight:700;cursor:pointer;font-family:'Nunito',sans-serif;font-size:0.79rem;transition:all 0.15s;}
.btn-outline:hover{background:var(--cream);border-color:var(--tan);}

/* ── INPUTS ── */
.inp{width:100%;padding:10px 13px;border:1.5px solid var(--cream-dark);border-radius:10px;margin-bottom:12px;font-weight:600;outline:none;font-family:'Nunito',sans-serif;font-size:0.87rem;background:var(--white);color:var(--text);transition:border-color 0.2s;}
.inp:focus{border-color:var(--brown);background:#fff;}

/* ── STATUS BADGES ── */
.error-state{padding:30px;text-align:center;color:var(--red);font-weight:600;font-size:0.84rem;}
.status-badge{padding:4px 10px;border-radius:20px;font-size:0.68rem;font-weight:800;color:#fff;border:none;outline:none;cursor:pointer;font-family:'Nunito',sans-serif;}
.status-waiting{background:var(--orange);}
.status-preparing{background:var(--blue);}
.status-ready{background:var(--green);}
.status-completed{background:#388E3C;}
.status-cancelled{background:var(--red);}

/* ── MODAL ── */
.modal{display:none;position:fixed;z-index:2000;left:0;top:0;width:100%;height:100%;background:rgba(42,21,5,0.7);align-items:flex-end;justify-content:center;padding:0;}
.modal-sheet{background:var(--white);border-radius:20px 20px 0 0;padding:22px 20px;width:100%;max-width:480px;max-height:92vh;overflow-y:auto;animation:slideUp 0.28s ease;}
@keyframes slideUp{from{transform:translateY(30px);opacity:0;}to{transform:translateY(0);opacity:1;}}
.modal-handle{width:38px;height:4px;background:var(--cream-dark);border-radius:4px;margin:0 auto 14px;}
.modal-title{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:900;color:var(--text);margin-bottom:16px;}

/* ── PERMISSION SECTION ── */
#perm-section{background:#FFF8F5;border:1.5px solid rgba(192,57,43,0.2);border-radius:var(--radius);overflow:hidden;margin-bottom:12px;}
.perm-hdr{padding:10px 14px;background:rgba(192,57,43,0.07);display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;}
.perm-title{font-weight:800;color:var(--red);font-size:0.84rem;display:flex;align-items:center;gap:6px;}

/* ── FINANCE ── */
.fin-stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:28px 14px 14px;}
.fin-stat{background:var(--white);border-radius:12px;padding:13px;border:1px solid var(--cream-dark);box-shadow:var(--shadow);}
.fin-stat .fl{font-size:0.66rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;}
.fin-stat .fv{font-family:'Playfair Display',serif;font-size:1.25rem;font-weight:900;}

/* ── SCHEDULE ── */
.schedule-grid{display:flex;flex-direction:column;gap:10px;}
.sched-row{background:var(--white);border-radius:12px;padding:13px 14px;border:1.5px solid var(--cream-dark);display:flex;flex-direction:column;gap:8px;}
.sched-row-top{display:flex;align-items:center;justify-content:space-between;}
.sched-day-name{font-weight:800;color:var(--text);font-size:0.9rem;display:flex;align-items:center;gap:8px;}
.sched-toggle{width:44px;height:24px;border-radius:20px;border:none;cursor:pointer;position:relative;transition:background 0.2s;flex-shrink:0;}
.sched-toggle.on{background:var(--brown);}
.sched-toggle.off{background:var(--cream-dark);}
.sched-toggle::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left 0.2s;box-shadow:0 1px 4px rgba(0,0,0,0.2);}
.sched-toggle.on::after{left:23px;}
.sched-times{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.sched-time-group label{font-size:0.66rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;display:block;margin-bottom:4px;}
.sched-time-input{width:100%;padding:8px 10px;border:1.5px solid var(--cream-dark);border-radius:8px;font-family:'Nunito',sans-serif;font-size:0.84rem;font-weight:700;color:var(--text);background:var(--cream);outline:none;}
.sched-time-input:focus{border-color:var(--brown);}
.sched-times.disabled{opacity:0.4;pointer-events:none;}
.sched-closed-badge{font-size:0.7rem;font-weight:800;color:var(--red);background:rgba(192,57,43,0.1);padding:2px 9px;border-radius:20px;}

/* ── POS CART ── */
.qo-menu-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;}
.qo-menu-item{background:var(--white);border-radius:12px;padding:11px 10px;border:1.5px solid var(--cream-dark);cursor:pointer;text-align:center;transition:border-color 0.15s;}
.qo-menu-item:hover,.qo-menu-item:active{border-color:var(--brown);background:var(--cream);}
.qo-mi-name{font-size:0.78rem;font-weight:800;color:var(--text);line-height:1.25;margin-bottom:3px;}
.qo-mi-price{font-family:'Playfair Display',serif;font-size:0.95rem;font-weight:900;color:var(--brown);}
.qo-cart-item{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--cream-dark);}
.qo-ci-name{font-size:0.82rem;font-weight:800;color:var(--text);}
.qo-ci-det{font-size:0.7rem;color:var(--muted);margin-top:1px;}
.qo-qty{display:flex;align-items:center;gap:6px;}
.qo-qty button{width:22px;height:22px;border-radius:7px;border:none;cursor:pointer;font-size:13px;font-weight:900;display:flex;align-items:center;justify-content:center;font-family:'Nunito',sans-serif;}
.qm{background:var(--cream-dark);color:var(--brown-dark);}
.qp{background:var(--brown);color:var(--cream);}
.qo-qty span{font-size:0.84rem;font-weight:900;min-width:16px;text-align:center;color:var(--text);}
.qo-ci-price{font-family:'Playfair Display',serif;font-size:0.95rem;font-weight:900;color:var(--brown);}

/* ── OPTION BUTTONS (size/sugar/ice) ── */
.option-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:12px;}
.opt-btn{padding:9px;border-radius:10px;border:1.5px solid var(--cream-dark);background:var(--white);cursor:pointer;font-family:'Nunito',sans-serif;font-size:0.79rem;font-weight:800;color:var(--text);text-align:center;transition:all 0.15s;}
.opt-btn.sel{border-color:var(--brown);background:var(--cream);color:var(--brown-dark);}
.opt-section-label{font-size:0.66rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:10px 0 7px;}

/* ── SECTION SPACING ── */
.section{margin:0 14px 12px;}
.screen-inner{padding:28px 0 0;}

/* ── FINANCE TABS ── */
.fin-tab-bar{display:flex;gap:0;margin:0 14px 14px;background:var(--cream-dark);border-radius:12px;padding:4px;}
.fin-tab{flex:1;border:none;background:transparent;color:var(--muted);font-family:'Nunito',sans-serif;font-size:0.78rem;font-weight:800;padding:9px 6px;border-radius:9px;cursor:pointer;transition:all 0.18s;letter-spacing:0.2px;}
.fin-tab.active{background:var(--brown-dark);color:var(--cream);box-shadow:0 2px 8px rgba(61,36,16,0.25);}
.fin-tabpane{display:none;}
.fin-tabpane.active{display:block;}

/* ── INVENTORY TABS ── */
.inv-tab-bar{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;scrollbar-width:none;}
.inv-tab-bar::-webkit-scrollbar{display:none;}
.inv-tab{border:1.5px solid var(--cream-dark);background:var(--white);color:var(--muted);font-family:'Nunito',sans-serif;font-size:0.75rem;font-weight:800;padding:7px 13px;border-radius:20px;cursor:pointer;white-space:nowrap;transition:all 0.18s;flex-shrink:0;}
.inv-tab:hover{border-color:var(--tan);color:var(--brown);}
.inv-tab.active{background:var(--brown-dark);border-color:var(--brown-dark);color:var(--cream);box-shadow:0 2px 8px rgba(61,36,16,0.25);}

/* ── CHART PERIOD PILLS ── */
.period-pills{display:flex;gap:6px;flex-wrap:wrap;}
.period-pill{background:var(--cream);border:1.5px solid var(--cream-dark);color:var(--muted);font-size:0.72rem;font-weight:800;padding:5px 13px;border-radius:20px;cursor:pointer;font-family:'Nunito',sans-serif;transition:all 0.15s;letter-spacing:0.3px;}
.period-pill.active{background:var(--brown-dark);border-color:var(--brown-dark);color:var(--cream);}

/* ── BEST-SELLER BARS ── */
.bs-row{display:flex;align-items:center;gap:10px;margin-bottom:10px;}
.bs-rank{width:22px;height:22px;border-radius:50%;background:var(--cream-dark);color:var(--muted);font-size:0.65rem;font-weight:900;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.bs-rank.gold{background:#F9A825;color:#fff;}
.bs-rank.silver{background:#90A4AE;color:#fff;}
.bs-rank.bronze{background:#A1887F;color:#fff;}
.bs-bar-wrap{flex:1;min-width:0;}
.bs-name{font-size:0.78rem;font-weight:800;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px;}
.bs-bar-track{height:7px;background:var(--cream-dark);border-radius:4px;overflow:hidden;}
.bs-bar-fill{height:100%;background:linear-gradient(90deg,var(--brown) 0%,var(--tan) 100%);border-radius:4px;transition:width 0.5s ease;}
.bs-count{font-size:0.72rem;font-weight:900;color:var(--brown);flex-shrink:0;min-width:28px;text-align:right;}

/* ── LOW STOCK ALERTS ── */
.stock-alert{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;margin-bottom:7px;border:1.5px solid;}
.stock-alert.critical{background:rgba(192,57,43,0.07);border-color:rgba(192,57,43,0.25);}
.stock-alert.low{background:rgba(245,124,0,0.07);border-color:rgba(245,124,0,0.25);}
.stock-alert.medium{background:rgba(25,118,210,0.06);border-color:rgba(25,118,210,0.2);}
.sa-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
.critical .sa-dot{background:var(--red);}
.low .sa-dot{background:var(--orange);}
.medium .sa-dot{background:var(--blue);}
.sa-name{flex:1;font-size:0.79rem;font-weight:800;color:var(--text);}
.sa-val{font-size:0.72rem;font-weight:700;color:var(--muted);}

/* ── ORDER HISTORY SEARCH ── */
.oh-search-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;}
.oh-search-inp{flex:1;min-width:140px;padding:9px 13px;border:1.5px solid var(--cream-dark);border-radius:10px;font-family:'Nunito',sans-serif;font-size:0.84rem;font-weight:600;outline:none;color:var(--text);background:var(--white);}
.oh-search-inp:focus{border-color:var(--brown);}
.oh-filter-sel{padding:9px 12px;border:1.5px solid var(--cream-dark);border-radius:10px;font-family:'Nunito',sans-serif;font-size:0.8rem;font-weight:700;color:var(--text);background:var(--white);outline:none;cursor:pointer;}
</style>
</head>
<body>
<div id="toast-container"></div>
<audio id="admin-audio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>

<!-- ══ TOPBAR ══ -->
<header class="topbar">
  <div class="topbar-logo">
    <div class="logo-circle">
      <img src="/static/images/9599.jpg" alt="9599" onerror="this.style.display='none';this.nextElementSibling.style.display='block';">
      <span class="lf" style="display:none;">☕</span>
    </div>
    <div>
      <div class="brand">9599 Tea &amp; Coffee</div>
      <div class="brand-sub">Parne Na!</div>
    </div>
    <div class="admin-pill" style="margin-left:6px;"><i class="fas fa-shield-alt"></i> Admin</div>
  </div>
  <div class="topbar-right">
    <div id="clock" class="clock-chip">00:00 PM</div>
    <div class="notif-btn" onclick="toggleNotif()">
      <i class="fas fa-bell"></i>
      <span class="nbadge" id="nbadge" style="display:none;"></span>
    </div>
    <div class="notif-panel" id="notif-panel">
      <div class="notif-ph">
        <span>Notifications</span>
        <button style="background:none;border:none;color:var(--red);cursor:pointer;font-weight:800;font-size:0.76rem;" onclick="clearNotifs()">Clear all</button>
      </div>
      <div class="notif-pb" id="notif-body">
        <div style="text-align:center;color:var(--muted);padding:18px;font-size:0.81rem;font-weight:600;">No notifications</div>
      </div>
    </div>
  </div>
</header>

<!-- ══ SCREENS ══ -->
<div class="screens">

  <!-- INVENTORY -->
  <div id="s-inventory" class="screen active">
    <div class="page-header">
      <h2><i class="fas fa-boxes"></i> Inventory</h2>
      <p>Stock levels by category — tap a tab to filter</p>
    </div>
    <div class="screen-inner">

      <!-- Category Tabs -->
      <div style="padding:0 14px;margin-bottom:14px;">
        <div class="inv-tab-bar" id="inv-tab-bar">
          <button class="inv-tab active" onclick="invTab('All',this)">All</button>
          <button class="inv-tab" onclick="invTab('Teas &amp; Bases',this)">🍵 Teas</button>
          <button class="inv-tab" onclick="invTab('Syrups &amp; Flavors',this)">🍯 Syrups</button>
          <button class="inv-tab" onclick="invTab('Dairy',this)">🥛 Dairy</button>
          <button class="inv-tab" onclick="invTab('Add-ons',this)">🧋 Add-ons</button>
          <button class="inv-tab" onclick="invTab('Snacks',this)">🍟 Snacks</button>
          <button class="inv-tab" onclick="invTab('Consumables &amp; Packaging',this)">📦 Packaging</button>
        </div>
      </div>

      <!-- Stock Table -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;" id="inv-tab-label">All Items</span>
          <div style="display:flex;gap:8px;align-items:center;">
            <span id="inv-count" style="font-size:0.72rem;font-weight:700;color:var(--muted);"></span>
            <button class="btn-secondary" onclick="fetchInventory()"><i class="fas fa-sync-alt"></i> Refresh</button>
          </div>
        </div>
        <div class="tbl-wrap">
          <table class="kds-table" style="min-width:300px;">
            <thead><tr><th>Ingredient</th><th>Unit</th><th>Stock</th><th>Status</th></tr></thead>
            <tbody id="inv-tbody"></tbody>
          </table>
        </div>
        <button class="btn-primary" style="margin-top:14px;" onclick="saveInventory()"><i class="fas fa-save"></i> Save Changes</button>
      </div>

      <!-- Low Stock Summary -->
      <div class="section card" id="inv-low-stock-card" style="display:none;">
        <div class="card-title">⚠️ Low / Out of Stock in this Category</div>
        <div id="inv-low-list"></div>
      </div>

    </div>
  </div>

  <!-- FINANCE -->
  <div id="s-finance" class="screen">
    <div class="page-header">
      <h2><i class="fas fa-chart-line"></i> Finance</h2>
      <p>Revenue, reports &amp; order history</p>
    </div>
    <div class="fin-stats">
      <div class="fin-stat" style="grid-column:1/-1;border-top:3px solid var(--brown);">
        <div class="fl">System Total Today</div>
        <div class="fv" id="sys-total" style="color:var(--brown);">₱0.00</div>
      </div>
      <div class="fin-stat" style="border-top:3px solid var(--green);">
        <div class="fl">Net Profit</div>
        <div class="fv" id="net-profit" style="color:var(--green);">₱0.00</div>
      </div>
      <div class="fin-stat" style="border-top:3px solid var(--red);">
        <div class="fl">Expenses</div>
        <div class="fv" id="exp-total" style="color:var(--red);">₱0.00</div>
      </div>
    </div>

    <!-- Tab Bar -->
    <div class="fin-tab-bar" style="margin-top:14px;">
      <button class="fin-tab active" id="ftab-today" onclick="finTab('today',this)"><i class="fas fa-sun" style="margin-right:5px;font-size:0.7rem;"></i>Today</button>
      <button class="fin-tab" id="ftab-reports" onclick="finTab('reports',this)"><i class="fas fa-chart-bar" style="margin-right:5px;font-size:0.7rem;"></i>Reports</button>
      <button class="fin-tab" id="ftab-history" onclick="finTab('history',this)"><i class="fas fa-history" style="margin-right:5px;font-size:0.7rem;"></i>Orders</button>
    </div>

    <!-- ── TODAY TAB ── -->
    <div id="fin-today" class="fin-tabpane active">
      <div class="section card">
        <div class="card-title">Log Expense</div>
        <input type="text" class="inp" id="exp-desc" placeholder="Description (e.g. Ice, Packaging)">
        <input type="number" class="inp" id="exp-amount" placeholder="Amount (₱)">
        <button class="btn-primary" onclick="addExpense()"><i class="fas fa-plus"></i> Record Expense</button>
      </div>
      <div class="section card">
        <div class="card-title">Expense Log</div>
        <div id="exp-list"><div style="color:var(--muted);font-size:0.82rem;font-weight:600;">No expenses today.</div></div>
      </div>
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;">Customer Records</span>
          <button class="btn-secondary" onclick="fetchCustomerLogs()"><i class="fas fa-sync-alt"></i></button>
        </div>
        <div class="tbl-wrap">
          <table class="kds-table">
            <thead><tr><th>Date</th><th>Name</th><th>Gmail</th><th>Phone</th><th>Order</th><th>Pick-up</th><th>Source</th><th>Total</th></tr></thead>
            <tbody id="cust-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ── REPORTS TAB ── -->
    <div id="fin-reports" class="fin-tabpane">
      <!-- Sales Chart -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
          <span class="card-title" style="margin:0;">Sales Overview</span>
          <div class="period-pills">
            <button class="period-pill active" id="pp-7" onclick="loadSalesChart(7,this)">7 Days</button>
            <button class="period-pill" id="pp-30" onclick="loadSalesChart(30,this)">30 Days</button>
          </div>
        </div>
        <div id="chart-loading" style="text-align:center;padding:30px;color:var(--muted);font-size:0.82rem;font-weight:600;display:none;"><i class="fas fa-spinner fa-spin"></i> Loading chart…</div>
        <div style="position:relative;height:200px;">
          <canvas id="sales-chart"></canvas>
        </div>
        <div id="chart-summary" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px;"></div>
      </div>

      <!-- Best-sellers -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
          <span class="card-title" style="margin:0;">Best-Sellers</span>
          <div class="period-pills">
            <button class="period-pill active" id="bsp-today" onclick="loadBestSellers('today',this)">Today</button>
            <button class="period-pill" id="bsp-week" onclick="loadBestSellers('week',this)">This Week</button>
            <button class="period-pill" id="bsp-all" onclick="loadBestSellers('all',this)">All Time</button>
          </div>
        </div>
        <div id="bestsellers-list"><div style="color:var(--muted);font-size:0.82rem;font-weight:600;padding:8px 0;">Loading…</div></div>
      </div>

      <!-- Low Stock Alerts -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;">⚠️ Stock Alerts</span>
          <button class="btn-secondary" style="font-size:0.72rem;padding:5px 10px;" onclick="loadLowStock()"><i class="fas fa-sync-alt"></i></button>
        </div>
        <div id="low-stock-list"><div style="color:var(--muted);font-size:0.82rem;font-weight:600;">Loading…</div></div>
      </div>
    </div>

    <!-- ── ORDER HISTORY TAB ── -->
    <div id="fin-history" class="fin-tabpane">
      <div class="section card">
        <div class="card-title">Order History</div>
        <div class="oh-search-row">
          <input type="text" class="oh-search-inp" id="oh-search" placeholder="🔍  Search by name or code…" oninput="ohDebounce()">
          <select class="oh-filter-sel" id="oh-status" onchange="loadOrderHistory(1)">
            <option value="">All Status</option>
            <option value="Waiting Confirmation">Waiting</option>
            <option value="Preparing Order">Preparing</option>
            <option value="Ready for Pick-up">Ready</option>
            <option value="Completed">Completed</option>
            <option value="Cancelled">Cancelled</option>
          </select>
        </div>
        <div class="tbl-wrap">
          <table class="kds-table" style="min-width:420px;">
            <thead><tr><th>Date</th><th>Code</th><th>Customer</th><th>Total</th><th>Status</th></tr></thead>
            <tbody id="oh-tbody"><tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted);">Loading…</td></tr></tbody>
          </table>
        </div>
        <!-- Pagination -->
        <div id="oh-pagination" style="display:flex;align-items:center;justify-content:space-between;margin-top:14px;gap:8px;flex-wrap:wrap;">
          <button id="oh-prev" onclick="ohChangePage(-1)" style="display:flex;align-items:center;gap:5px;background:var(--white);border:1.5px solid var(--cream-dark);color:var(--brown-dark);padding:8px 14px;border-radius:9px;font-weight:800;font-size:0.78rem;cursor:pointer;font-family:'Nunito',sans-serif;">
            <i class="fas fa-chevron-left" style="font-size:0.7rem;"></i> Prev
          </button>
          <span id="oh-page-info" style="font-size:0.75rem;font-weight:700;color:var(--muted);"></span>
          <button id="oh-next" onclick="ohChangePage(1)" style="display:flex;align-items:center;gap:5px;background:var(--white);border:1.5px solid var(--cream-dark);color:var(--brown-dark);padding:8px 14px;border-radius:9px;font-weight:800;font-size:0.78rem;cursor:pointer;font-family:'Nunito',sans-serif;">
            Next <i class="fas fa-chevron-right" style="font-size:0.7rem;"></i>
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- SETTINGS -->
  <div id="s-settings" class="screen">
    <div class="page-header">
      <h2><i class="fas fa-sliders-h"></i> Settings</h2>
      <p>Store link, schedule, menu &amp; system</p>
    </div>
    <div class="screen-inner">

      <!-- Store Link -->
      <div class="section card">
        <div class="card-title">Store Link Generator</div>
        <div style="background:var(--cream);border:1.5px solid var(--cream-dark);border-radius:12px;padding:13px;">
          <p style="font-size:0.79rem;color:var(--muted);line-height:1.65;margin-bottom:11px;">Generate a <b style="color:var(--brown-dark);">permanent</b> ordering link for customers. The system uses the schedule below to open/close automatically.</p>
          <input type="password" class="inp" id="store-pin" placeholder="Enter Master PIN" style="background:var(--white);">
          <button class="btn-primary" onclick="generateLink()" style="margin-bottom:9px;"><i class="fas fa-link"></i> Generate Permanent Link</button>
          <div style="position:relative;">
            <input type="text" class="inp" id="posLink" style="background:var(--white);padding-right:74px;font-size:0.74rem;color:var(--brown-dark);margin-bottom:0;" readonly placeholder="Link will appear here…">
            <div style="position:absolute;right:6px;top:50%;transform:translateY(-50%);display:flex;gap:4px;">
              <button onclick="copyLink()" style="background:var(--brown);border:none;border-radius:6px;color:var(--cream);width:30px;height:30px;cursor:pointer;display:flex;align-items:center;justify-content:center;"><i class="fas fa-copy" style="font-size:11px;"></i></button>
              <button onclick="openLink()" style="background:#1a73e8;border:none;border-radius:6px;color:#fff;width:30px;height:30px;cursor:pointer;display:flex;align-items:center;justify-content:center;"><i class="fas fa-external-link-alt" style="font-size:11px;"></i></button>
            </div>
          </div>
        </div>
        <!-- Calendar Widget -->
        <div style="margin-top:14px;background:var(--white);border:1.5px solid var(--cream-dark);border-radius:12px;overflow:hidden;">
          <div style="background:var(--brown-dark);color:var(--cream);padding:10px 14px;display:flex;align-items:center;justify-content:space-between;">
            <button onclick="calPrev()" style="background:none;border:none;color:var(--tan);cursor:pointer;font-size:1rem;padding:2px 6px;"><i class="fas fa-chevron-left"></i></button>
            <span id="cal-title" style="font-family:'Playfair Display',serif;font-weight:900;font-size:0.95rem;letter-spacing:0.5px;">APRIL 2025</span>
            <button onclick="calNext()" style="background:none;border:none;color:var(--tan);cursor:pointer;font-size:1rem;padding:2px 6px;"><i class="fas fa-chevron-right"></i></button>
          </div>
          <div style="padding:8px 6px;">
            <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:1px;margin-bottom:4px;">
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">SUN</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">MON</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">TUE</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">WED</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">THU</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">FRI</div>
              <div style="text-align:center;font-size:0.62rem;font-weight:900;color:var(--muted);padding:4px 0;">SAT</div>
            </div>
            <div id="cal-grid" style="display:grid;grid-template-columns:repeat(7,1fr);gap:1px;"></div>
          </div>
        </div>
      </div>

      <!-- Store Schedule -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;">Store Schedule</span>
          <button class="btn-primary" style="width:auto;margin-bottom:0;padding:8px 16px;" onclick="saveSchedule()"><i class="fas fa-save"></i> Save</button>
        </div>
        <div class="schedule-grid" id="schedule-grid">
          <div style="text-align:center;padding:20px;color:var(--muted);font-size:0.82rem;">Loading schedule…</div>
        </div>
        <div id="sched-status" style="margin-top:10px;font-size:0.81rem;font-weight:700;min-height:16px;"></div>
      </div>

      <!-- Menu Management -->
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;">Menu Management</span>
          <button class="btn-secondary" onclick="openMenuModal()"><i class="fas fa-plus"></i> Add Item</button>
        </div>
        <div class="tbl-wrap">
          <table class="kds-table">
            <thead><tr><th>Name</th><th>Category</th><th>Price</th><th>Stock</th><th>Actions</th></tr></thead>
            <tbody id="menu-tbody"></tbody>
          </table>
        </div>
      </div>

      <!-- Backup -->
      <div class="section card">
        <div class="card-title">Backup &amp; Recovery</div>
        <p style="font-size:0.79rem;color:var(--muted);margin-bottom:13px;line-height:1.65;">Download or restore a full backup of all orders, menu, inventory and customer data.</p>
        <div style="display:flex;flex-direction:column;gap:9px;">
          <button onclick="downloadBackup()" class="btn-primary" style="margin-bottom:0;"><i class="fas fa-download"></i> Download Backup</button>
          <label style="width:100%;padding:12px;background:var(--brown-mid);color:var(--cream);border-radius:12px;font-weight:800;font-size:0.87rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;font-family:'Nunito',sans-serif;">
            <i class="fas fa-upload"></i> Restore from Backup
            <input type="file" id="restore-file" accept=".json" style="display:none;" onchange="restoreBackup(this)">
          </label>
        </div>
        <div id="backup-status" style="margin-top:10px;font-size:0.81rem;font-weight:700;min-height:16px;"></div>
      </div>

      <!-- Lock / Reload -->
      <div class="section" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px;">
        <button class="btn-outline" onclick="location.reload()"><i class="fas fa-sync-alt"></i> Reload</button>
        <button class="btn-outline" style="color:var(--red);border-color:rgba(192,57,43,0.3);" onclick="location.href='/logout'"><i class="fas fa-lock"></i> Lock Panel</button>
      </div>

    </div>
  </div>

  <!-- AUDIT TRAIL SCREEN -->
  <div id="s-audit" class="screen">
    <div class="page-header">
      <h2><i class="fas fa-shield-alt"></i> Audit Trail</h2>
      <p>Complete log of all admin actions</p>
    </div>
    <div class="screen-inner" style="padding-top:28px;">
      <div class="section card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px;">
          <span class="card-title" style="margin:0;">Activity Log</span>
          <div style="display:flex;align-items:center;gap:8px;">
            <span id="audit-page-info" style="font-size:0.75rem;font-weight:700;color:var(--muted);"></span>
            <button class="btn-secondary" style="padding:6px 10px;font-size:0.75rem;" onclick="fetchAuditLogs()"><i class="fas fa-sync-alt"></i> Refresh</button>
          </div>
        </div>

        <!-- Log entries -->
        <div id="audit-log-list" style="display:flex;flex-direction:column;gap:0;border:1.5px solid var(--cream-dark);border-radius:12px;overflow:hidden;">
          <div style="padding:24px;text-align:center;color:var(--muted);font-size:0.82rem;font-weight:600;">Loading logs…</div>
        </div>

        <!-- Pagination controls -->
        <div id="audit-pagination" style="display:flex;align-items:center;justify-content:space-between;margin-top:16px;gap:8px;">
          <button id="audit-prev-btn" onclick="auditChangePage(-1)" style="display:flex;align-items:center;gap:6px;background:var(--white);border:1.5px solid var(--cream-dark);color:var(--brown-dark);padding:9px 16px;border-radius:10px;font-weight:800;font-size:0.8rem;cursor:pointer;font-family:'Nunito',sans-serif;transition:all 0.15s;" onmouseover="this.style.background='var(--cream)'" onmouseout="this.style.background='var(--white)'">
            <i class="fas fa-chevron-left" style="font-size:0.75rem;"></i> Prev
          </button>
          <div id="audit-page-dots" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:center;"></div>
          <button id="audit-next-btn" onclick="auditChangePage(1)" style="display:flex;align-items:center;gap:6px;background:var(--white);border:1.5px solid var(--cream-dark);color:var(--brown-dark);padding:9px 16px;border-radius:10px;font-weight:800;font-size:0.8rem;cursor:pointer;font-family:'Nunito',sans-serif;transition:all 0.15s;" onmouseover="this.style.background='var(--cream)'" onmouseout="this.style.background='var(--white)'">
            Next <i class="fas fa-chevron-right" style="font-size:0.75rem;"></i>
          </button>
        </div>
      </div>
    </div>
  </div>

</div><!-- /screens -->

<!-- ══ BOTTOM NAV ══ -->
<nav class="bottom-nav">
  <button class="nav-btn active" id="nb-inventory" onclick="goScreen('inventory',this)"><i class="fas fa-boxes"></i>Stock</button>
  <button class="nav-btn" id="nb-audit" onclick="goScreen('audit',this)"><i class="fas fa-shield-alt"></i>Audit</button>
  <button class="nav-btn" id="nb-finance" onclick="goScreen('finance',this)"><i class="fas fa-chart-line"></i>Finance</button>
  <button class="nav-btn" id="nb-settings" onclick="goScreen('settings',this)"><i class="fas fa-sliders-h"></i>Settings</button>
</nav>

<!-- ══ MODALS ══ -->
<div id="qo-modal" class="modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title" id="qo-modal-title">Choose Options</div>
    <div class="opt-section-label">Size</div>
    <div class="option-grid" id="qo-size-opts"></div>
    <div class="opt-section-label">Sugar Level</div>
    <div class="option-grid" id="qo-sugar-opts"></div>
    <div class="opt-section-label">Ice Level</div>
    <div class="option-grid" id="qo-ice-opts"></div>
    <div class="opt-section-label">Add-ons</div>
    <div id="qo-addon-opts" style="display:flex;flex-direction:column;gap:7px;margin-bottom:14px;"></div>
    <button class="btn-primary" onclick="confirmQO()"><i class="fas fa-cart-plus"></i> Add to Cart</button>
    <button class="btn-outline" style="width:100%;margin-top:7px;" onclick="closeModal('qo-modal')">Cancel</button>
  </div>
</div>

<div id="menu-modal" class="modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title" id="menu-modal-title">Add Menu Item</div>
    <form id="menu-form" onsubmit="saveMenuItem(event)">
      <input type="text" id="menu-name" class="inp" placeholder="Item Name" required>
      <input type="number" id="menu-price" class="inp" placeholder="Price (₱)" required>
      <input type="text" id="menu-category" class="inp" placeholder="Category" required>
      <input type="text" id="menu-letter" class="inp" placeholder="Short Code (e.g. T)" required>
      <label style="display:flex;align-items:center;gap:10px;font-size:0.84rem;font-weight:700;color:var(--brown-dark);margin-bottom:14px;cursor:pointer;">
        <input type="checkbox" id="menu-oos" style="width:17px;height:17px;accent-color:var(--brown);">
        Mark as Out of Stock
      </label>
      <div style="display:flex;gap:9px;">
        <button type="button" class="btn-secondary" style="flex:1;padding:11px;" onclick="closeModal('menu-modal')">Cancel</button>
        <button type="submit" class="btn-primary" style="flex:1;margin-bottom:0;">Save Item</button>
      </div>
    </form>
  </div>
</div>

<script>
/* ══ CLOCK & PING ══ */
function updateClock(){const n=new Date();let h=n.getHours()%12||12,m=n.getMinutes().toString().padStart(2,'0'),ap=n.getHours()>=12?'PM':'AM';document.getElementById('clock').innerText=h+':'+m+' '+ap;}
setInterval(updateClock,1000);updateClock();
setInterval(()=>fetch('/api/admin/ping'),30000);

/* ══ TOAST ══ */
function showToast(msg,type='info'){const t=document.createElement('div');t.className='toast '+type;t.innerText=msg;document.getElementById('toast-container').appendChild(t);setTimeout(()=>t.remove(),3000);}

/* ══ API ══ */
async function apiFetch(url,opts={}){const r=await fetch(url,opts);if(r.status===403){location.href='/login';return null;}return r;}

/* ══ MODAL ══ */
function closeModal(id){document.getElementById(id).style.display='none';}
document.querySelectorAll('.modal').forEach(m=>{m.addEventListener('click',e=>{if(e.target===m)m.style.display='none';});});

/* ══ SCREEN NAV ══ */
function goScreen(name,btn){
  document.querySelectorAll('.screen').forEach(s=>{s.classList.remove('active');s.style.display='none';});
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  const scr=document.getElementById('s-'+name);
  scr.classList.add('active');
  scr.style.display='block';
  if(btn)btn.classList.add('active');
  if(name==='inventory')fetchInventory();
  if(name==='finance'){fetchFinance();fetchCustomerLogs();finTab('today',document.getElementById('ftab-today'));}
  if(name==='settings'){fetchSchedule();fetchMenu();fetchClosedDays();}
  if(name==='audit'){auditPage=1;fetchAuditLogs();}
}

/* ══ HELPERS ══ */
function escapeHTML(s){const d=document.createElement('div');d.innerText=s;return d.innerHTML;}
function getStatusClass(s){return({'Waiting Confirmation':'status-waiting','Preparing Order':'status-preparing','Ready for Pick-up':'status-ready','Completed':'status-completed','Cancelled':'status-cancelled'})[s]||'status-waiting';}

/* ══ NOTIFICATIONS ══ */
let adminNotifs=[],lastOrderIds=new Set(),firstLoad=true;
function playBeep(){try{document.getElementById('admin-audio').play().catch(()=>{});}catch(e){}}
function toggleNotif(){const p=document.getElementById('notif-panel');p.style.display=p.style.display==='flex'?'none':'flex';}
function clearNotifs(){adminNotifs=[];renderNotifUI();document.getElementById('notif-panel').style.display='none';}
function addNotif(code,name,total){adminNotifs.unshift({code,name,total,time:new Date().toLocaleTimeString()});if(adminNotifs.length>20)adminNotifs.pop();renderNotifUI();playBeep();showToast(`New order from ${name} (${code})!`,'success');}
function renderNotifUI(){
  const badge=document.getElementById('nbadge'),body=document.getElementById('notif-body');
  if(adminNotifs.length>0){badge.style.display='flex';badge.innerText=adminNotifs.length;
    body.innerHTML=adminNotifs.map(n=>`<div class="notif-item"><b style="color:var(--brown-dark);">Order #${n.code}</b> — ${escapeHTML(n.name)}<br><span style="color:var(--green);font-weight:800;">₱${n.total.toFixed(2)}</span><span style="float:right;color:var(--muted);font-size:0.7rem;">${n.time}</span></div>`).join('');
  }else{badge.style.display='none';body.innerHTML='<div style="text-align:center;color:var(--muted);padding:18px;font-size:0.81rem;font-weight:600;">No notifications</div>';}
}

/* ══ LIVE ORDERS ══ */
async function fetchOrders(){
  const tbody=document.getElementById('kds-tbody');
  try{
    const res=await apiFetch('/api/orders?_t='+Date.now());
    if(!res||!res.ok){tbody.innerHTML='<tr><td colspan="7" class="error-state">⚠️ DB Error</td></tr>';return;}
    const data=await res.json();
    let cur=new Set();data.orders.forEach(o=>cur.add(o.id));
    if(!firstLoad){data.orders.forEach(o=>{if(!lastOrderIds.has(o.id))addNotif(o.code,o.name,o.total);});}
    lastOrderIds=cur;firstLoad=false;tbody.innerHTML='';
    if(!data.orders.length){tbody.innerHTML='<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--muted);font-weight:600;">No active orders</td></tr>';return;}
    data.orders.forEach(o=>{
      let iH=o.items.map(i=>`<div style="margin-bottom:3px;"><b>${escapeHTML(i.foundation)}</b> (${escapeHTML(i.size)})<br><span style="font-size:0.7rem;color:var(--muted);">${escapeHTML(i.sweetener)} | ${escapeHTML(i.ice)}${i.addons?' | +'+escapeHTML(i.addons):''}</span></div>`).join('');
      let items;
      if(o.items.length>=2){const tid='it-'+o.id,lid='il-'+o.id;items=`<button id="${tid}" onclick="toggleItems('${lid}','${tid}')" style="background:var(--cream);border:1.5px solid var(--cream-dark);border-radius:7px;padding:4px 9px;font-size:0.75rem;font-weight:700;color:var(--brown-dark);cursor:pointer;font-family:'Nunito',sans-serif;"><i class="fas fa-chevron-down"></i> ${o.items.length} items</button><div id="${lid}" style="display:none;margin-top:5px;">${iH}</div>`;}
      else items=iH;
      let sel=`<select onchange="updateStatus(${o.id},this.value)" class="status-badge ${getStatusClass(o.status)}" style="padding:4px 7px;border:none;outline:none;font-weight:800;cursor:pointer;border-radius:20px;">
        <option value="Waiting Confirmation" ${o.status==='Waiting Confirmation'?'selected':''} style="color:#000;background:#fff;">Waiting</option>
        <option value="Preparing Order" ${o.status==='Preparing Order'?'selected':''} style="color:#000;background:#fff;">Preparing</option>
        <option value="Ready for Pick-up" ${o.status==='Ready for Pick-up'?'selected':''} style="color:#000;background:#fff;">Ready</option>
        <option value="Completed" ${o.status==='Completed'?'selected':''} style="color:#000;background:#fff;">Completed</option>
        <option value="Cancelled" ${o.status==='Cancelled'?'selected':''} style="color:#000;background:#fff;">Cancelled</option>
      </select>`;
      tbody.innerHTML+=`<tr><td><b style="color:var(--brown-dark);">${escapeHTML(o.code)}</b></td><td>${escapeHTML(o.source)}</td><td><b>${escapeHTML(o.name)}</b></td><td>${escapeHTML(o.pickup_time)}</td><td style="color:var(--brown);font-weight:800;">₱${o.total.toFixed(2)}</td><td>${items}</td><td>${sel}</td></tr>`;
    });
  }catch(e){tbody.innerHTML='<tr><td colspan="7" class="error-state">⚠️ Network Error</td></tr>';}
}
async function updateStatus(id,status){await apiFetch(`/api/orders/${id}/status`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});showToast('Status updated','success');fetchOrders();}
function toggleItems(lid,tid){const l=document.getElementById(lid),b=document.getElementById(tid),h=l.style.display==='none';l.style.display=h?'block':'none';b.querySelector('i').className=h?'fas fa-chevron-up':'fas fa-chevron-down';}

/* ══ PERMISSION REQUESTS ══ */
let knownPermCodes=new Set(),firstPermLoad=true;
async function fetchPermReqs(){
  const tbody=document.getElementById('perm-tbody');
  try{
    const res=await apiFetch('/api/permission_requests');if(!res||!res.ok)return;
    const data=await res.json();
    if(!data.length){tbody.innerHTML='<tr><td colspan="5" style="text-align:center;padding:12px;color:var(--muted);font-size:0.81rem;">No pending requests</td></tr>';}
    else{tbody.innerHTML=data.map(p=>`<tr><td style="font-size:0.75rem;">${escapeHTML(p.time)}</td><td><b style="color:var(--brown-dark);">${escapeHTML(p.code)}</b></td><td><b>${escapeHTML(p.name)}</b><br><span style="font-size:0.7rem;color:var(--muted);">${escapeHTML(p.address||'')}</span></td><td style="font-size:0.8rem;">${escapeHTML(p.message||'')}</td><td><button class="btn-primary" style="padding:5px 11px;margin-bottom:0;width:auto;" onclick="grantPerm(${p.id},'${escapeHTML(p.name)}','${escapeHTML(p.code)}')">✅ Grant</button></td></tr>`).join('');}
    if(!firstPermLoad){data.forEach(p=>{if(!knownPermCodes.has(p.code)){playBeep();showToast(`🔔 Permission request from ${p.name}`,'error');}});}
    data.forEach(p=>knownPermCodes.add(p.code));firstPermLoad=false;
  }catch(e){}
}
async function grantPerm(id,name,code){const r=await apiFetch(`/api/permission_requests/${id}/grant`,{method:'POST'});if(r&&r.ok){showToast(`✅ Granted for ${name}`,'success');knownPermCodes.delete(code);fetchPermReqs();}}

/* ══ POS ══ */
let qoMenu=[],qoCart=[],qoPending=null;
const QO_SIZES=[{label:'16 oz',price:49},{label:'22 oz',price:59}];
const QO_SUGARS=['100% Sugar','75% Sugar','50% Sugar','0% Sugar'];
const QO_ICE=['Normal Ice','Less Ice','No Ice'];
const QO_ADDONS=[{name:'Nata',cost:10},{name:'Pearl',cost:10},{name:'Coffee Jelly',cost:10},{name:'Cloud Foam',cost:15}];
let selSize=QO_SIZES[0],selSugar=QO_SUGARS[0],selIce=QO_ICE[0],selAddons=[];

async function fetchQOMenu(){const r=await apiFetch('/api/menu');if(!r||!r.ok)return;qoMenu=await r.json();renderQOMenu();}
function renderQOMenu(){
  const q=document.getElementById('qo-search').value.toLowerCase();
  const f=qoMenu.filter(m=>!m.is_out_of_stock&&m.name.toLowerCase().includes(q));
  document.getElementById('qo-grid').innerHTML=f.map(m=>`<div class="qo-menu-item" onclick="openQOModal(${m.id})"><div class="qo-mi-name">${escapeHTML(m.name)}</div><div class="qo-mi-price">₱${m.price}</div></div>`).join('');
}
function openQOModal(id){
  qoPending=qoMenu.find(m=>m.id===id);if(!qoPending)return;
  const isSnack=qoPending.category==='Snacks';
  document.getElementById('qo-modal-title').innerText=qoPending.name;
  document.getElementById('qo-size-opts').innerHTML=isSnack?'<p style="color:var(--muted);font-size:0.81rem;font-weight:600;">Fixed portion</p>':QO_SIZES.map((s,i)=>`<button class="opt-btn ${i===0?'sel':''}" onclick="selQOOpt(this,'size',${i})">${s.label} — ₱${s.price}</button>`).join('');
  selSize=QO_SIZES[0];
  document.getElementById('qo-sugar-opts').innerHTML=isSnack?'':QO_SUGARS.map((s,i)=>`<button class="opt-btn ${i===0?'sel':''}" onclick="selQOOpt(this,'sugar',${i})">${s}</button>`).join('');
  selSugar=QO_SUGARS[0];
  document.getElementById('qo-ice-opts').innerHTML=isSnack?'':QO_ICE.map((s,i)=>`<button class="opt-btn ${i===0?'sel':''}" onclick="selQOOpt(this,'ice',${i})">${s}</button>`).join('');
  selIce=QO_ICE[0];selAddons=[];
  document.getElementById('qo-addon-opts').innerHTML=isSnack?'':QO_ADDONS.map((a,i)=>`<label style="display:flex;align-items:center;gap:9px;padding:8px;border:1.5px solid var(--cream-dark);border-radius:9px;cursor:pointer;"><input type="checkbox" class="qo-ac" data-i="${i}" style="width:16px;height:16px;accent-color:var(--brown);"> ${a.name} <span style="color:var(--muted);font-size:0.76rem;">+₱${a.cost}</span></label>`).join('');
  document.getElementById('qo-modal').style.display='flex';
}
function selQOOpt(btn,type,idx){btn.parentElement.querySelectorAll('.opt-btn').forEach(b=>b.classList.remove('sel'));btn.classList.add('sel');if(type==='size')selSize=QO_SIZES[idx];if(type==='sugar')selSugar=QO_SUGARS[idx];if(type==='ice')selIce=QO_ICE[idx];}
function confirmQO(){
  selAddons=[];document.querySelectorAll('.qo-ac:checked').forEach(cb=>{selAddons.push(QO_ADDONS[parseInt(cb.dataset.i)]);});
  const isSnack=qoPending.category==='Snacks';
  const base=isSnack?qoPending.price:selSize.price;
  const total=base+selAddons.reduce((s,a)=>s+a.cost,0);
  const detail=isSnack?'':`${selSize.label} · ${selSugar} · ${selIce}${selAddons.length?' · '+selAddons.map(a=>a.name).join(', '):''}`;
  qoCart.push({id:qoPending.id,name:qoPending.name,category:qoPending.category,price:total,detail,qty:1});
  closeModal('qo-modal');renderQOCart();showToast(qoPending.name+' added','success');
}
function renderQOCart(){
  const el=document.getElementById('qo-items');
  if(!qoCart.length){el.innerHTML='<p style="color:var(--muted);font-size:0.81rem;font-weight:600;padding:8px 0;">Cart is empty</p>';document.getElementById('qo-total').innerText='₱0.00';return;}
  el.innerHTML=qoCart.map((item,i)=>`<div class="qo-cart-item"><div><div class="qo-ci-name">${escapeHTML(item.name)}</div>${item.detail?'<div class="qo-ci-det">'+escapeHTML(item.detail)+'</div>':''}</div><div style="display:flex;align-items:center;gap:8px;"><div class="qo-qty"><button class="qm" onclick="changeQty(${i},-1)">−</button><span>${item.qty}</span><button class="qp" onclick="changeQty(${i},1)">+</button></div><div class="qo-ci-price">₱${(item.price*item.qty).toFixed(2)}</div></div></div>`).join('');
  document.getElementById('qo-total').innerText='₱'+qoCart.reduce((s,i)=>s+i.price*i.qty,0).toFixed(2);
}
function changeQty(i,d){qoCart[i].qty+=d;if(qoCart[i].qty<=0)qoCart.splice(i,1);renderQOCart();}
async function submitQO(){
  const name=document.getElementById('qo-name').value.trim();
  if(!name)return showToast('Enter customer name','error');
  if(!qoCart.length)return showToast('Cart is empty','error');
  const payload={customer_name:name,total:qoCart.reduce((s,i)=>s+i.price*i.qty,0),items:qoCart.map(i=>{const pts=i.detail?i.detail.split(' · '):[];return{foundation:i.name,size:pts[0]||'N/A',sugar:pts[1]||'N/A',ice:pts[2]||'N/A',addons:pts.slice(3).join(', '),price:i.price*i.qty}})};
  try{const r=await apiFetch('/api/admin/manual_order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(r&&r.ok){showToast('Walk-in order saved!','success');qoCart=[];document.getElementById('qo-name').value='';renderQOCart();}else showToast('Error','error');}catch(e){showToast('Error','error');}
}

/* ══ INVENTORY ══ */
let invAllData=[], invActiveTab='All';

async function fetchInventory(){
  const tbody=document.getElementById('inv-tbody');
  tbody.innerHTML='<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--muted);"><i class="fas fa-spinner fa-spin"></i> Loading…</td></tr>';
  try{
    const r=await apiFetch('/api/inventory');
    if(!r||!r.ok){tbody.innerHTML='<tr><td colspan="4" class="error-state">DB Error</td></tr>';return;}
    invAllData=await r.json();
    renderInvTable();
  }catch(e){tbody.innerHTML='<tr><td colspan="4" class="error-state">Network Error</td></tr>';}
}

function invTab(name,btn){
  invActiveTab=name;
  document.querySelectorAll('.inv-tab').forEach(b=>b.classList.remove('active'));
  if(btn)btn.classList.add('active');
  const label=document.getElementById('inv-tab-label');
  if(label)label.innerText=name==='All'?'All Items':name;
  renderInvTable();
}

function renderInvTable(){
  const tbody=document.getElementById('inv-tbody');
  const countEl=document.getElementById('inv-count');
  const lowCard=document.getElementById('inv-low-stock-card');
  const lowList=document.getElementById('inv-low-list');

  const filtered=invActiveTab==='All'?invAllData:invAllData.filter(i=>i.category===invActiveTab);

  if(!filtered.length){
    tbody.innerHTML='<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--muted);font-weight:600;">No items in this category.</td></tr>';
    if(countEl)countEl.innerText='';
    if(lowCard)lowCard.style.display='none';
    return;
  }

  tbody.innerHTML=filtered.map(i=>{
    const threshold=i.unit==='pcs'?50:(i.unit==='ml'?500:200);
    const pct=i.stock/Math.max(threshold*4,1);
    const color=i.stock<=0?'var(--red)':(i.stock<=threshold?'var(--orange)':'var(--green)');
    const statusDot=i.stock<=0
      ?`<span style="background:rgba(192,57,43,0.12);color:var(--red);padding:2px 9px;border-radius:20px;font-size:0.68rem;font-weight:800;">Out</span>`
      :(i.stock<=threshold
        ?`<span style="background:rgba(245,124,0,0.12);color:var(--orange);padding:2px 9px;border-radius:20px;font-size:0.68rem;font-weight:800;">Low</span>`
        :`<span style="background:rgba(39,174,96,0.12);color:var(--green);padding:2px 9px;border-radius:20px;font-size:0.68rem;font-weight:800;">OK</span>`);
    return`<tr>
      <td><b style="font-size:0.83rem;">${escapeHTML(i.name)}</b><br><span style="font-size:0.67rem;color:var(--muted);">${escapeHTML(i.category)}</span></td>
      <td style="color:var(--muted);font-size:0.8rem;">${escapeHTML(i.unit)}</td>
      <td><input type="number" class="inp stock-inp" data-id="${i.id}" value="${i.stock}" min="0" style="width:110px;padding:6px 9px;margin:0;border-color:${color};color:${color};font-weight:800;font-size:0.84rem;"></td>
      <td>${statusDot}</td>
    </tr>`;
  }).join('');

  if(countEl)countEl.innerText=`${filtered.length} item${filtered.length!==1?'s':''}`;

  // Low stock summary panel
  const lowItems=filtered.filter(i=>{
    const threshold=i.unit==='pcs'?50:(i.unit==='ml'?500:200);
    return i.stock<=threshold;
  });
  if(lowItems.length&&lowCard&&lowList){
    lowCard.style.display='block';
    lowList.innerHTML=lowItems.map(i=>{
      const isOut=i.stock<=0;
      const color=isOut?'var(--red)':'var(--orange)';
      return`<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--cream-dark);">
        <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;display:inline-block;"></span>
        <span style="flex:1;font-size:0.81rem;font-weight:800;color:var(--text);">${escapeHTML(i.name)}</span>
        <span style="font-size:0.75rem;font-weight:700;color:${color};">${i.stock} ${escapeHTML(i.unit)}</span>
      </div>`;
    }).join('');
  } else if(lowCard){
    lowCard.style.display='none';
  }
}

async function saveInventory(){
  const payload=Array.from(document.querySelectorAll('.stock-inp')).map(inp=>({id:parseInt(inp.dataset.id),stock:parseFloat(inp.value)||0}));
  if(!payload.length)return showToast('Nothing to save','error');
  try{
    const r=await apiFetch('/api/inventory',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(r&&r.ok){showToast('Inventory saved ✓','success');fetchInventory();}
  }catch(e){showToast('Error saving','error');}
}

/* ══ FINANCE TABS ══ */
function finTab(name,btn){
  document.querySelectorAll('.fin-tabpane').forEach(p=>{p.classList.remove('active');});
  document.querySelectorAll('.fin-tab').forEach(b=>{b.classList.remove('active');});
  document.getElementById('fin-'+name).classList.add('active');
  if(btn)btn.classList.add('active');
  if(name==='reports'){loadSalesChart(7,document.getElementById('pp-7'));loadBestSellers('today',document.getElementById('bsp-today'));loadLowStock();}
  if(name==='history'){ohPage=1;loadOrderHistory(1);}
}

/* ══ FINANCE ══ */
async function fetchFinance(){
  try{const r=await apiFetch('/api/finance/daily');if(!r||!r.ok)return;
  const data=await r.json();const net=data.system_total-data.expenses_total;
  document.getElementById('sys-total').innerText='₱'+data.system_total.toFixed(2);
  document.getElementById('net-profit').innerText='₱'+net.toFixed(2);
  document.getElementById('exp-total').innerText='₱'+data.expenses_total.toFixed(2);
  const el=document.getElementById('exp-list');
  el.innerHTML=data.expenses&&data.expenses.length?data.expenses.map(x=>`<div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--cream-dark);font-size:0.84rem;"><span>${escapeHTML(x.desc)}</span><b style="color:var(--red);">-₱${x.amount.toFixed(2)}</b></div>`).join(''):'<div style="color:var(--muted);font-size:0.81rem;font-weight:600;">No expenses today.</div>';}catch(e){}
}
async function addExpense(){
  const desc=document.getElementById('exp-desc').value.trim(),amount=document.getElementById('exp-amount').value;
  if(!desc||!amount)return showToast('Fill all fields','error');
  try{const r=await apiFetch('/api/expenses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:desc,amount})});if(r&&r.ok){showToast('Expense logged','success');document.getElementById('exp-desc').value='';document.getElementById('exp-amount').value='';fetchFinance();};}catch(e){showToast('Error','error');}
}
async function fetchCustomerLogs(){
  const tbody=document.getElementById('cust-tbody');
  try{const r=await apiFetch('/api/customer_logs');if(!r||!r.ok){tbody.innerHTML='<tr><td colspan="8" class="error-state">DB Error</td></tr>';return;}
  const data=await r.json();
  tbody.innerHTML=data.length?data.map(l=>{
    const dateStr=escapeHTML(l.time.split(' ')[0]);
    const timeStr=escapeHTML(l.time.split(' ').slice(1).join(' '));
    const gmail=l.gmail&&l.gmail!=='Walk-In'?`<a href="mailto:${escapeHTML(l.gmail)}" style="color:var(--brown);font-weight:700;font-size:0.75rem;">${escapeHTML(l.gmail)}</a>`:`<span style="color:var(--muted);font-size:0.75rem;">—</span>`;
    const phone=l.phone&&l.phone!=='Walk-In'?`<span style="font-size:0.78rem;font-weight:700;">${escapeHTML(l.phone)}</span>`:`<span style="color:var(--muted);font-size:0.75rem;">—</span>`;
    const items=l.items?`<span style="font-size:0.72rem;color:var(--text);">${escapeHTML(l.items)}</span>`:`<span style="color:var(--muted);font-size:0.72rem;">—</span>`;
    const pickup=l.pickup_time?`<span style="font-size:0.78rem;font-weight:800;color:var(--brown-dark);">${escapeHTML(l.pickup_time)}</span>`:`<span style="color:var(--muted);font-size:0.75rem;">—</span>`;
    return`<tr>
      <td style="font-size:0.72rem;color:var(--muted);white-space:nowrap;">${dateStr}<br><span style="font-size:0.68rem;">${timeStr}</span></td>
      <td><b style="font-size:0.82rem;">${escapeHTML(l.name)}</b></td>
      <td>${gmail}</td>
      <td>${phone}</td>
      <td style="max-width:160px;">${items}</td>
      <td style="white-space:nowrap;">${pickup}</td>
      <td><span class="kds-badge" style="background:${l.source==='Online'?'var(--brown)':'var(--brown-mid)'};color:var(--cream);">${escapeHTML(l.source)}</span></td>
      <td style="color:var(--brown);font-weight:800;white-space:nowrap;">₱${l.total.toFixed(2)}</td>
    </tr>`;
  }).join(''):'<tr><td colspan="8" style="text-align:center;padding:18px;color:var(--muted);font-weight:600;">No records.</td></tr>';}catch(e){tbody.innerHTML='<tr><td colspan="8" class="error-state">Network Error</td></tr>';}
}

/* ══ SALES CHART ══ */
let salesChartObj=null;
async function loadSalesChart(days,btn){
  document.querySelectorAll('.period-pill').forEach(p=>{if(p.id.startsWith('pp-'))p.classList.remove('active');});
  if(btn)btn.classList.add('active');
  document.getElementById('chart-loading').style.display='block';
  try{
    const r=await apiFetch(`/api/finance/report?mode=${days===30?'monthly':'weekly'}`);
    if(!r||!r.ok)return;
    const data=await r.json();
    document.getElementById('chart-loading').style.display='none';
    const labels=data.map(d=>d.date);
    const revenue=data.map(d=>d.revenue);
    const expenses=data.map(d=>d.expenses);
    const profit=data.map(d=>d.profit);
    const totalRev=revenue.reduce((a,b)=>a+b,0);
    const totalExp=expenses.reduce((a,b)=>a+b,0);
    const totalPro=profit.reduce((a,b)=>a+b,0);
    document.getElementById('chart-summary').innerHTML=`
      <div style="background:var(--cream);border-radius:10px;padding:10px;text-align:center;border:1.5px solid var(--cream-dark);">
        <div style="font-size:0.62rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Revenue</div>
        <div style="font-size:1rem;font-weight:900;color:var(--brown);font-family:'Playfair Display',serif;">₱${totalRev.toFixed(0)}</div>
      </div>
      <div style="background:var(--cream);border-radius:10px;padding:10px;text-align:center;border:1.5px solid var(--cream-dark);">
        <div style="font-size:0.62rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Expenses</div>
        <div style="font-size:1rem;font-weight:900;color:var(--red);font-family:'Playfair Display',serif;">₱${totalExp.toFixed(0)}</div>
      </div>
      <div style="background:var(--cream);border-radius:10px;padding:10px;text-align:center;border:1.5px solid var(--cream-dark);">
        <div style="font-size:0.62rem;font-weight:900;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">Net Profit</div>
        <div style="font-size:1rem;font-weight:900;color:${totalPro>=0?'var(--green)':'var(--red)'};font-family:'Playfair Display',serif;">₱${totalPro.toFixed(0)}</div>
      </div>`;
    const ctx=document.getElementById('sales-chart').getContext('2d');
    if(salesChartObj)salesChartObj.destroy();
    salesChartObj=new Chart(ctx,{type:'bar',data:{labels,datasets:[
      {label:'Revenue',data:revenue,backgroundColor:'rgba(123,79,46,0.75)',borderRadius:5,borderSkipped:false},
      {label:'Expenses',data:expenses,backgroundColor:'rgba(192,57,43,0.6)',borderRadius:5,borderSkipped:false},
      {label:'Profit',data:profit,type:'line',borderColor:'#27AE60',backgroundColor:'rgba(39,174,96,0.1)',borderWidth:2,pointRadius:3,pointBackgroundColor:'#27AE60',tension:0.35,fill:true,yAxisID:'y'}
    ]},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{font:{family:'Nunito',size:10,weight:'700'},color:'#2A1505',boxWidth:12,padding:10}}},scales:{x:{grid:{display:false},ticks:{font:{family:'Nunito',size:9,weight:'700'},color:'#8D6E55',maxRotation:45}},y:{position:'left',grid:{color:'rgba(196,168,130,0.2)'},ticks:{font:{family:'Nunito',size:9},color:'#8D6E55',callback:v=>'\u20B1'+v}}}}});
  }catch(e){document.getElementById('chart-loading').style.display='none';}
}

/* ══ BEST-SELLERS ══ */
async function loadBestSellers(mode,btn){
  document.querySelectorAll('.period-pill').forEach(p=>{if(p.id.startsWith('bsp-'))p.classList.remove('active');});
  if(btn)btn.classList.add('active');
  const el=document.getElementById('bestsellers-list');
  el.innerHTML='<div style="color:var(--muted);font-size:0.8rem;padding:8px 0;"><i class="fas fa-spinner fa-spin"></i> Loading…</div>';
  try{
    const r=await apiFetch(`/api/finance/bestsellers?mode=${mode}`);
    if(!r||!r.ok)return;
    const data=await r.json();
    if(!data.length){el.innerHTML='<div style="color:var(--muted);font-size:0.8rem;font-weight:600;padding:12px 0;text-align:center;">No orders yet for this period.</div>';return;}
    const max=data[0].count||1;
    el.innerHTML=data.map((item,i)=>{
      const rankClass=i===0?'gold':i===1?'silver':i===2?'bronze':'';
      const pct=Math.round((item.count/max)*100);
      return`<div class="bs-row">
        <div class="bs-rank ${rankClass}">${i+1}</div>
        <div class="bs-bar-wrap">
          <div class="bs-name">${escapeHTML(item.name)}</div>
          <div class="bs-bar-track"><div class="bs-bar-fill" style="width:${pct}%"></div></div>
        </div>
        <div class="bs-count">${item.count}</div>
      </div>`;
    }).join('');
  }catch(e){el.innerHTML='<div style="color:var(--red);font-size:0.8rem;">Error loading.</div>';}
}

/* ══ LOW STOCK ══ */
async function loadLowStock(){
  const el=document.getElementById('low-stock-list');
  el.innerHTML='<div style="color:var(--muted);font-size:0.8rem;"><i class="fas fa-spinner fa-spin"></i> Checking stock…</div>';
  try{
    const r=await apiFetch('/api/finance/low_stock');
    if(!r||!r.ok)return;
    const data=await r.json();
    if(!data.length){el.innerHTML='<div style="background:rgba(39,174,96,0.08);border:1.5px solid rgba(39,174,96,0.25);border-radius:10px;padding:13px 14px;display:flex;align-items:center;gap:10px;font-size:0.82rem;font-weight:700;color:var(--green);"><i class="fas fa-check-circle"></i> All ingredients are well-stocked!</div>';return;}
    el.innerHTML=data.map(item=>`
      <div class="stock-alert ${item.level}">
        <div class="sa-dot"></div>
        <div class="sa-name">${escapeHTML(item.name)}</div>
        <div class="sa-val">${item.stock} ${escapeHTML(item.unit)}</div>
      </div>`).join('');
  }catch(e){el.innerHTML='<div style="color:var(--red);font-size:0.8rem;">Error loading.</div>';}
}

/* ══ ORDER HISTORY ══ */
let ohPage=1,ohDebTimer=null;
function ohDebounce(){clearTimeout(ohDebTimer);ohDebTimer=setTimeout(()=>loadOrderHistory(1),380);}
function ohChangePage(dir){ohPage+=dir;if(ohPage<1)ohPage=1;loadOrderHistory(ohPage);}
async function loadOrderHistory(page){
  ohPage=page;
  const q=document.getElementById('oh-search').value.trim();
  const status=document.getElementById('oh-status').value;
  const tbody=document.getElementById('oh-tbody');
  const info=document.getElementById('oh-page-info');
  const prevBtn=document.getElementById('oh-prev');
  const nextBtn=document.getElementById('oh-next');
  tbody.innerHTML='<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted);font-size:0.82rem;font-weight:600;"><i class="fas fa-spinner fa-spin"></i> Loading…</td></tr>';
  try{
    const r=await apiFetch(`/api/orders/history?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}&page=${page}`);
    if(!r||!r.ok)return;
    const data=await r.json();
    const statusColors={'Waiting Confirmation':'var(--orange)','Preparing Order':'var(--blue)','Ready for Pick-up':'var(--green)','Completed':'#388E3C','Cancelled':'var(--red)'};
    const statusLabels={'Waiting Confirmation':'Waiting','Preparing Order':'Preparing','Ready for Pick-up':'Ready','Completed':'Completed','Cancelled':'Cancelled'};
    tbody.innerHTML=data.orders.length?data.orders.map(o=>`<tr>
      <td style="font-size:0.74rem;color:var(--muted);">${escapeHTML(o.date)}</td>
      <td><b style="color:var(--brown-dark);font-size:0.78rem;">${escapeHTML(o.code)}</b></td>
      <td><b style="font-size:0.8rem;">${escapeHTML(o.name)}</b><br><span style="font-size:0.68rem;color:var(--muted);">${escapeHTML(o.items.slice(0,2).join(', '))}${o.items.length>2?' +'+( o.items.length-2)+'…':''}</span></td>
      <td style="color:var(--brown);font-weight:800;font-size:0.82rem;">₱${o.total.toFixed(2)}</td>
      <td><span style="background:${statusColors[o.status]||'var(--muted)'};color:#fff;padding:3px 8px;border-radius:20px;font-size:0.66rem;font-weight:800;white-space:nowrap;">${statusLabels[o.status]||o.status}</span></td>
    </tr>`).join(''):'<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);font-weight:600;">No orders found.</td></tr>';
    const total=data.total,perPage=data.per_page,totalPages=Math.ceil(total/perPage);
    const start=(page-1)*perPage+1;
    const end=Math.min(page*perPage,total);
    if(info)info.innerText=total?`${start}–${end} of ${total} orders`:'No orders';
    if(prevBtn){prevBtn.disabled=page<=1;prevBtn.style.opacity=page<=1?'0.4':'1';}
    if(nextBtn){nextBtn.disabled=page>=totalPages;nextBtn.style.opacity=page>=totalPages?'0.4':'1';}
  }catch(e){tbody.innerHTML='<tr><td colspan="5" class="error-state">Network Error</td></tr>';}
}

/* ══ SCHEDULE ══ */
let scheduleData=[];
const DAY_NAMES=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const DAY_EMOJIS=['🌙','📅','📅','📅','🎉','☀️','☀️'];
async function fetchSchedule(){
  const grid=document.getElementById('schedule-grid');
  try{const r=await apiFetch('/api/schedule');if(!r||!r.ok){grid.innerHTML='<div class="error-state">Error loading schedule</div>';return;}
  scheduleData=await r.json();renderSchedule();}catch(e){grid.innerHTML='<div class="error-state">Network Error</div>';}
}
function renderSchedule(){
  document.getElementById('schedule-grid').innerHTML=scheduleData.map(d=>`
    <div class="sched-row" id="srow-${d.day}">
      <div class="sched-row-top">
        <div class="sched-day-name">${DAY_EMOJIS[d.day]} ${d.name}</div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="sched-closed-badge" id="sched-badge-${d.day}" style="display:${d.is_open?'none':'inline-flex'};">Closed</span>
          <button class="sched-toggle ${d.is_open?'on':'off'}" id="sched-tog-${d.day}" onclick="toggleDay(${d.day})" title="Toggle open/closed"></button>
        </div>
      </div>
      <div class="sched-times ${d.is_open?'':'disabled'}" id="sched-times-${d.day}">
        <div class="sched-time-group"><label>Opens at</label><input type="time" class="sched-time-input" id="sched-open-${d.day}" value="${String(d.open_hour).padStart(2,'0')}:${String(d.open_minute).padStart(2,'0')}"></div>
        <div class="sched-time-group"><label>Closes at</label><input type="time" class="sched-time-input" id="sched-close-${d.day}" value="${String(d.close_hour).padStart(2,'0')}:${String(d.close_minute).padStart(2,'0')}"></div>
      </div>
    </div>`).join('');
}
function toggleDay(dow){
  const tog=document.getElementById('sched-tog-'+dow),badge=document.getElementById('sched-badge-'+dow),times=document.getElementById('sched-times-'+dow);
  const isOn=tog.classList.contains('on');
  tog.classList.toggle('on',!isOn);tog.classList.toggle('off',isOn);
  badge.style.display=isOn?'inline-flex':'none';times.classList.toggle('disabled',isOn);
  const e=scheduleData.find(d=>d.day===dow);if(e)e.is_open=!isOn;
}
async function saveSchedule(){
  const s=document.getElementById('sched-status');s.style.color='var(--muted)';s.innerText='Saving…';
  const payload=scheduleData.map(d=>{
    const isOpen=document.getElementById('sched-tog-'+d.day).classList.contains('on');
    const ov=document.getElementById('sched-open-'+d.day).value.split(':');
    const cv=document.getElementById('sched-close-'+d.day).value.split(':');
    return{day:d.day,is_open:isOpen,open_hour:parseInt(ov[0]),open_minute:parseInt(ov[1]),close_hour:parseInt(cv[0]),close_minute:parseInt(cv[1])};
  });
  try{const r=await apiFetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(r&&r.ok){s.style.color='var(--green)';s.innerText='✅ Schedule saved!';showToast('Schedule saved!','success');setTimeout(()=>s.innerText='',3000);}
  else{s.style.color='var(--red)';s.innerText='❌ Save failed.';}}catch(e){s.style.color='var(--red)';s.innerText='❌ Network error.';}
}

/* ══ STORE LINK ══ */
async function generateLink(){
  const pin=document.getElementById('store-pin').value;if(!pin)return showToast('Enter Master PIN','error');
  try{const r=await apiFetch('/api/generate_link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})});const data=await r.json();if(r&&r.ok){document.getElementById('posLink').value=data.url;showToast('Link generated!','success');}else showToast(data.error||'Error','error');}catch(e){showToast('Error','error');}
}
function copyLink(){const el=document.getElementById('posLink');if(!el.value)return showToast('Generate link first','error');navigator.clipboard.writeText(el.value).then(()=>showToast('Copied!','success')).catch(()=>{el.select();document.execCommand('copy');showToast('Copied!','success');});}
function openLink(){const el=document.getElementById('posLink');if(!el.value)return showToast('Generate link first','error');window.open(el.value,'_blank');}

/* ══ MENU ══ */
let editMenuId=null;
async function fetchMenu(){
  const tbody=document.getElementById('menu-tbody');
  try{const r=await apiFetch('/api/menu');if(!r||!r.ok){tbody.innerHTML='<tr><td colspan="5" class="error-state">DB Error</td></tr>';return;}
  const data=await r.json();
  tbody.innerHTML=data.map(m=>{
    const oos=m.is_out_of_stock;
    const sb=oos?`<span style="background:rgba(192,57,43,0.12);color:var(--red);padding:2px 8px;border-radius:20px;font-size:0.68rem;font-weight:800;">Out of Stock</span>`:`<span style="background:rgba(39,174,96,0.12);color:var(--green);padding:2px 8px;border-radius:20px;font-size:0.68rem;font-weight:800;">In Stock</span>`;
    const tb=`<button class="btn-secondary" style="padding:4px 8px;background:${oos?'var(--green)':'var(--red)'};font-size:0.72rem;" onclick="toggleOOS(${m.id},${!oos})">${oos?'✅':'🚫'}</button>`;
    return`<tr><td><b>${escapeHTML(m.name)}</b></td><td style="color:var(--muted);font-size:0.77rem;">${escapeHTML(m.category)}</td><td style="color:var(--brown);font-weight:800;">₱${m.price}</td><td>${sb}</td><td style="display:flex;gap:5px;flex-wrap:wrap;padding:7px 13px;"><button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="openMenuModal(${m.id},'${escapeHTML(m.name.replace(/'/g,"\\'"))}',${m.price},'${escapeHTML(m.category.replace(/'/g,"\\'"))}','${escapeHTML(m.letter)}',${oos})">Edit</button>${tb}</td></tr>`;
  }).join('');}catch(e){tbody.innerHTML='<tr><td colspan="5" class="error-state">Network Error</td></tr>';}
}
function openMenuModal(id=null,name='',price='',cat='',letter='',oos=false){
  editMenuId=id;document.getElementById('menu-modal-title').innerText=id?'Edit Item':'Add Menu Item';
  document.getElementById('menu-name').value=name;document.getElementById('menu-price').value=price;
  document.getElementById('menu-category').value=cat;document.getElementById('menu-letter').value=letter;
  document.getElementById('menu-oos').checked=oos;document.getElementById('menu-modal').style.display='flex';
}
async function saveMenuItem(e){
  e.preventDefault();
  const payload={name:document.getElementById('menu-name').value.trim(),price:parseFloat(document.getElementById('menu-price').value),category:document.getElementById('menu-category').value,letter:document.getElementById('menu-letter').value,is_out_of_stock:document.getElementById('menu-oos').checked};
  const url=editMenuId?`/api/menu/${editMenuId}`:'/api/menu',method=editMenuId?'PUT':'POST';
  try{const r=await apiFetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(r&&r.ok){showToast('Saved','success');closeModal('menu-modal');fetchMenu();}
  else{const d=await r.json();showToast(d.error||'Error saving item','error');}
  }catch(e){showToast('Error','error');}
}
async function toggleOOS(id,state){
  try{const item=(await(await apiFetch('/api/menu')).json()).find(m=>m.id===id);if(!item)return;const r=await apiFetch(`/api/menu/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...item,is_out_of_stock:state})});if(r&&r.ok){showToast(state?'Marked OOS':'Marked In Stock','success');fetchMenu();};}catch(e){showToast('Error','error');}
}

/* ══ AUDIT (paginated) ══ */
let auditAllLogs=[], auditPage=1;
const AUDIT_PER_PAGE=15;

async function fetchAuditLogs(){
  const list=document.getElementById('audit-log-list');
  if(!list)return;
  list.innerHTML='<div style="padding:24px;text-align:center;color:var(--muted);font-size:0.82rem;font-weight:600;"><i class="fas fa-spinner fa-spin" style="margin-right:6px;"></i>Loading…</div>';
  try{
    const r=await apiFetch('/api/audit_logs');
    if(!r||!r.ok){list.innerHTML='<div style="padding:24px;text-align:center;color:var(--red);font-size:0.82rem;font-weight:600;">Error loading logs</div>';return;}
    auditAllLogs=await r.json();
    renderAuditPage();
  }catch(e){list.innerHTML='<div style="padding:24px;text-align:center;color:var(--red);font-size:0.82rem;font-weight:600;">Network error</div>';}
}

function renderAuditPage(){
  const list=document.getElementById('audit-log-list');
  const info=document.getElementById('audit-page-info');
  const dots=document.getElementById('audit-page-dots');
  const prevBtn=document.getElementById('audit-prev-btn');
  const nextBtn=document.getElementById('audit-next-btn');
  if(!list)return;
  const total=auditAllLogs.length;
  const totalPages=Math.max(1,Math.ceil(total/AUDIT_PER_PAGE));
  if(auditPage>totalPages)auditPage=totalPages;
  if(auditPage<1)auditPage=1;
  const start=(auditPage-1)*AUDIT_PER_PAGE;
  const slice=auditAllLogs.slice(start,start+AUDIT_PER_PAGE);
  if(!total){
    list.innerHTML='<div style="padding:32px;text-align:center;color:var(--muted);font-size:0.83rem;font-weight:600;"><i class="fas fa-shield-alt" style="font-size:1.8rem;display:block;margin-bottom:10px;opacity:0.3;"></i>No audit logs yet.</div>';
  } else {
    list.innerHTML=slice.map((l,i)=>{
      const isEven=i%2===0;
      return`<div style="padding:13px 16px;background:${isEven?'var(--white)':'var(--cream)'};border-bottom:1px solid var(--cream-dark);display:flex;align-items:flex-start;gap:12px;">
        <div style="width:32px;height:32px;border-radius:50%;background:var(--brown-dark);display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;">
          <i class="fas fa-shield-alt" style="font-size:0.7rem;color:var(--tan);"></i>
        </div>
        <div style="flex:1;min-width:0;">
          <div style="font-size:0.8rem;font-weight:800;color:var(--text);line-height:1.35;">${escapeHTML(l.action)}</div>
          ${l.details?`<div style="font-size:0.72rem;color:var(--muted);margin-top:2px;line-height:1.35;">${escapeHTML(l.details)}</div>`:''}
          <div style="font-size:0.65rem;color:var(--tan);margin-top:4px;font-weight:700;">${escapeHTML(l.time)}</div>
        </div>
      </div>`;
    }).join('');
  }
  // Info text
  if(info) info.innerText=total?`${start+1}–${Math.min(start+AUDIT_PER_PAGE,total)} of ${total}`:'';
  // Prev/Next state
  if(prevBtn){prevBtn.disabled=auditPage<=1;prevBtn.style.opacity=auditPage<=1?'0.4':'1';}
  if(nextBtn){nextBtn.disabled=auditPage>=totalPages;nextBtn.style.opacity=auditPage>=totalPages?'0.4':'1';}
  // Dots
  if(dots){
    const maxDots=7;
    let pages=[];
    if(totalPages<=maxDots){for(let i=1;i<=totalPages;i++)pages.push(i);}
    else{
      pages=[1];
      let lo=Math.max(2,auditPage-1),hi=Math.min(totalPages-1,auditPage+1);
      if(lo>2)pages.push('…');
      for(let i=lo;i<=hi;i++)pages.push(i);
      if(hi<totalPages-1)pages.push('…');
      pages.push(totalPages);
    }
    dots.innerHTML=pages.map(p=>{
      if(p==='…')return`<span style="color:var(--muted);font-size:0.8rem;font-weight:700;padding:0 2px;">…</span>`;
      const active=p===auditPage;
      return`<button onclick="auditGoPage(${p})" style="width:32px;height:32px;border-radius:8px;border:1.5px solid ${active?'var(--brown)':'var(--cream-dark)'};background:${active?'var(--brown-dark)':'var(--white)'};color:${active?'var(--cream)':'var(--text)'};font-size:0.78rem;font-weight:800;cursor:pointer;font-family:'Nunito',sans-serif;transition:all 0.15s;">${p}</button>`;
    }).join('');
  }
}

function auditChangePage(dir){auditPage+=dir;renderAuditPage();}
function auditGoPage(p){auditPage=p;renderAuditPage();}

/* ══ BACKUP ══ */
async function downloadBackup(){
  const s=document.getElementById('backup-status');s.style.color='var(--muted)';s.innerText='Preparing…';
  try{const r=await apiFetch('/api/backup');if(!r||!r.ok){s.style.color='var(--red)';s.innerText='Backup failed.';return;}
  const data=await r.json(),blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=`9599_backup_${new Date().toISOString().slice(0,10)}.json`;a.click();URL.revokeObjectURL(url);
  s.style.color='var(--green)';s.innerText='✅ Backup downloaded.';}catch(e){s.style.color='var(--red)';s.innerText='Network error.';}
}
async function restoreBackup(input){
  const s=document.getElementById('backup-status'),file=input.files[0];if(!file)return;
  s.style.color='var(--muted)';s.innerText='Reading file…';
  const reader=new FileReader();
  reader.onload=async(e)=>{try{const payload=JSON.parse(e.target.result);s.innerText='Restoring…';
    const r=await apiFetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(r&&r.ok){s.style.color='var(--green)';s.innerText='✅ Restored! Refreshing…';setTimeout(()=>location.reload(),1500);}
    else{s.style.color='var(--red)';s.innerText='❌ Restore failed.';}
  }catch(err){s.style.color='var(--red)';s.innerText='❌ Invalid JSON.';}};
  reader.readAsText(file);input.value='';
}

/* ══ CALENDAR (CLOSED DAYS) ══ */
let calYear=new Date().getFullYear(), calMonth=new Date().getMonth();
let closedDays=new Set(); // Set of 'YYYY-MM-DD' strings
const MONTH_NAMES=['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];

async function fetchClosedDays(){
  try{
    const r=await apiFetch('/api/closed_days');
    if(r&&r.ok){ const data=await r.json(); closedDays=new Set(data.dates); renderCal(); }
  }catch(e){}
}

function pad2(n){return String(n).padStart(2,'0');}

function renderCal(){
  const today=new Date();
  document.getElementById('cal-title').innerText=MONTH_NAMES[calMonth]+' '+calYear;
  const grid=document.getElementById('cal-grid');
  const first=new Date(calYear,calMonth,1).getDay();
  const days=new Date(calYear,calMonth+1,0).getDate();
  let html='';
  for(let i=0;i<first;i++) html+='<div></div>';
  for(let d=1;d<=days;d++){
    const dateStr=`${calYear}-${pad2(calMonth+1)}-${pad2(d)}`;
    const isToday=d===today.getDate()&&calMonth===today.getMonth()&&calYear===today.getFullYear();
    const isClosed=closedDays.has(dateStr);
    let bg=isClosed?'var(--red)':isToday?'var(--brown)':'transparent';
    let color=(isClosed||isToday)?'var(--white)':'var(--text)';
    let content=isClosed?`<span style="display:block;font-size:0.55rem;line-height:1;">✕</span>${d}`:d;
    html+=`<div onclick="calToggleDay('${dateStr}',${d})" style="text-align:center;padding:4px 2px;font-size:0.78rem;font-weight:800;color:${color};background:${bg};border-radius:6px;cursor:pointer;line-height:1.3;transition:background 0.15s;" title="${isClosed?'Click to reopen':'Click to mark closed'}">${content}</div>`;
  }
  grid.innerHTML=html;
}

async function calToggleDay(dateStr, d){
  const wasClosed=closedDays.has(dateStr);
  // Optimistic UI update
  if(wasClosed){ closedDays.delete(dateStr); } else { closedDays.add(dateStr); }
  renderCal();
  try{
    let r;
    if(wasClosed){
      r=await apiFetch('/api/closed_days',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:dateStr})});
    } else {
      r=await apiFetch('/api/closed_days',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:dateStr})});
    }
    if(r&&r.ok){
      showToast(wasClosed?`✅ ${MONTH_NAMES[calMonth].slice(0,3)} ${d} reopened`:`🔴 ${MONTH_NAMES[calMonth].slice(0,3)} ${d} marked closed`,'success');
    } else {
      // Revert on failure
      if(wasClosed){ closedDays.add(dateStr); } else { closedDays.delete(dateStr); }
      renderCal(); showToast('Error saving','error');
    }
  }catch(e){
    if(wasClosed){ closedDays.add(dateStr); } else { closedDays.delete(dateStr); }
    renderCal(); showToast('Network error','error');
  }
}

function calPrev(){calMonth--;if(calMonth<0){calMonth=11;calYear--;}renderCal();}
function calNext(){calMonth++;if(calMonth>11){calMonth=0;calYear++;}renderCal();}
renderCal();
fetchClosedDays();

/* ══ AUTO-REFRESH & INIT ══ */
setInterval(fetchPermReqs,5000);
setInterval(()=>{if(document.getElementById('s-audit')&&document.getElementById('s-audit').classList.contains('active'))fetchAuditLogs();},30000);
fetchPermReqs();
fetchInventory();
</script>
</body>
</html>
"""


# ==========================================
# 5. REST API ROUTES & FLASK LOGIC
# ==========================================

@app.before_request
def check_admin_session():
    if request.path.startswith('/admin') or request.path.startswith('/api/admin'):
        # Must have a valid PIN-authenticated session
        if not session.get('is_admin') or not session.get('admin_id'):
            if request.path.startswith('/api'):
                return jsonify({"error": "Unauthorized"}), 403
            return redirect(url_for('admin_login'))
        # Block concurrent sessions (only one admin at a time)
        state = SystemState.query.first()
        if state and state.active_session_id:
            if state.last_ping and (datetime.utcnow() - state.last_ping).total_seconds() < 60:
                if state.active_session_id != session.get('admin_id'):
                    if request.path.startswith('/api'):
                        return jsonify({"error": "Unauthorized"}), 403
                    return "<h3 style='font-family:sans-serif; text-align:center; margin-top:50px;'>The employee only can access the admin site.</h3>", 403

@app.route('/')
def storefront():
    token = request.args.get('token')

    # ── Closed / Not Configured pages ───────────────────────────────────
    def closed_page(title, icon, headline, sub, extra=''):
        # icon param kept for compatibility but logo image is always used
        return f"""<!DOCTYPE html><html><head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <link rel="icon" type="image/jpeg" href="/static/images/9599.jpg">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" crossorigin="anonymous">
        <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@400;600;700&display=swap" rel="stylesheet">
        <title>{title} | 9599 Tea & Coffee</title>
        <style>
            *{{box-sizing:border-box;margin:0;padding:0;}}
            body{{font-family:'DM Sans',sans-serif;background:#F5EFE6;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}}
            .card{{background:white;border-radius:20px;padding:50px 40px;max-width:440px;width:100%;text-align:center;box-shadow:0 20px 50px rgba(0,0,0,0.08);border:1px solid #EFEBE4;}}
            .logo-wrap{{margin-bottom:20px;}}
            .logo-wrap img{{width:90px;height:90px;border-radius:50%;object-fit:cover;border:3px solid #D7CCC8;box-shadow:0 4px 16px rgba(111,78,55,0.15);}}
            .logo-wrap .logo-fallback{{width:90px;height:90px;border-radius:50%;background:#F5EFE6;border:3px solid #D7CCC8;display:inline-flex;align-items:center;justify-content:center;font-size:2.8rem;}}
            h2{{font-family:'Playfair Display',serif;font-size:1.8rem;color:#3E2723;margin-bottom:12px;}}
            p{{color:#8D6E63;font-size:0.95rem;line-height:1.6;}}
            .badge{{display:inline-block;background:#F5EFE6;color:#6F4E37;font-weight:700;font-size:0.85rem;padding:8px 18px;border-radius:50px;margin-top:18px;border:1px solid #D7CCC8;}}
        </style></head><body>
        <div class="card">
            <div class="logo-wrap">
                <img src="/static/images/9599.jpg" alt="9599 Tea &amp; Coffee"
                     onerror="this.style.display='none';this.nextElementSibling.style.display='inline-flex';">
                <span class="logo-fallback" style="display:none;">☕</span>
            </div>
            <h2>{headline}</h2>
            <p>{sub}</p>
            {extra}
        </div></body></html>"""

    # ── Validate token (permanent — just proves it's the real shop link) ─
    if not token:
        return closed_page(
            "Not Found", "🔒",
            "Store Link Required",
            "Please use the official ordering link provided by 9599 Tea & Coffee."
        ), 403

    try:
        token_serializer.loads(token, max_age=None)   # no expiry — permanent link
    except BadSignature:
        return closed_page(
            "Invalid Link", "⚠️",
            "Invalid Link",
            "This ordering link is not valid. Please get the correct link from the shop."
        ), 403
    except Exception:
        pass   # SignatureExpired not raised when max_age=None

    # ── Check schedule ───────────────────────────────────────────────────
    status = get_store_status()

    # ── Check if today is a manually marked closed day ───────────────────
    today_str = get_ph_time().strftime('%Y-%m-%d')
    if ClosedDay.query.filter_by(date_str=today_str).first():
        day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        next_open = None
        for delta in range(1, 15):
            check = get_ph_time() + timedelta(days=delta)
            check_str = check.strftime('%Y-%m-%d')
            if not ClosedDay.query.filter_by(date_str=check_str).first():
                next_open = day_names[check.weekday()] + ' at ' + status['open_time']
                break
        return closed_page(
            "Closed Today", "🧋",
            "We're Closed Today",
            "9599 Tea &amp; Coffee is taking a day off. We'll be back soon!",
            f'<div class="badge">Next opening: {next_open or status["next_open"]}</div>'
        ), 403

    if status["closing_soon"]:
        return closed_page(
            "Closing Soon", "🕐",
            "We're Closing Soon!",
            f"Online ordering closes <b>1 hour before closing time</b> to ensure your order can be prepared.<br><br>"
            f"Today's hours: <b>{status['open_time']} – {status['close_time']}</b><br>"
            f"Last order accepted at: <b>{status['cutoff_time']}</b>",
            f'<div class="badge">Next opening: {status["next_open"]}</div>'
        ), 403

    if not status["open"]:
        return closed_page(
            "Closed", "🧋",
            "We're Currently Closed",
            f"9599 Tea & Coffee is not accepting orders right now.<br><br>"
            f"<b>Today's Hours:</b> {status['open_time']} – {status['close_time']}<br><br>"
            f"Online orders close 1 hour before closing time.",
            f'<div class="badge">Next opening: {status["next_open"]}</div>'
        ), 403

    # ── Store is open — serve the storefront ─────────────────────────────
    return render_template_string(
        STOREFRONT_HTML,
        open_time=status["open_time"],
        close_time=status["cutoff_time"],   # cutoff = 1h before actual close
        google_client_id=GOOGLE_CLIENT_ID
    )

@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    data = request.json
    token = data.get('token')
    if not token: return jsonify({"error": "No token provided"}), 400
    try:
        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={token}"
        response = requests.get(verify_url)
        if response.status_code == 200:
            user_info = response.json()
            session['customer_verified'] = True
            session['customer_name'] = user_info.get('name', 'Valued Patron')
            session['customer_email'] = user_info.get('email', 'google_user@local')
            return jsonify({"status": "success"})
        return jsonify({"error": "Invalid Google Token"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/manual', methods=['POST'])
@limiter.limit("30 per minute")
def manual_auth():
    """Manual sign-in: name + email + optional phone. No Google OAuth required."""
    data = request.json or {}
    name  = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip()
    phone = (data.get('phone') or '').strip()
    if not name:
        return jsonify({"error": "Full name is required."}), 400
    if not email or '@' not in email:
        return jsonify({"error": "A valid email address is required."}), 400
    session['customer_verified'] = True
    session['customer_name']     = name
    session['customer_email']    = email
    session['customer_phone']    = phone
    return jsonify({"status": "success"})

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if master_pin_matches(pin):
            state = SystemState.query.first()
            if not state:
                state = SystemState(active_session_id='', last_ping=datetime.min)
                db.session.add(state)
            
            if state.last_ping and (datetime.utcnow() - state.last_ping).total_seconds() < 60:
                if state.active_session_id and state.active_session_id != session.get('admin_id'):
                    return "<h3 style='font-family:sans-serif; text-align:center; margin-top:50px;'>The employee only can access the admin site.</h3>", 403

            session.permanent = True
            session['is_admin'] = True
            session['admin_id'] = str(uuid.uuid4())
            state.active_session_id = session['admin_id']
            state.last_ping = datetime.utcnow()
            db.session.commit()
            
            log_audit("Admin Login", "Successful login to dashboard")
            return redirect(url_for('admin_dashboard'))
        error = "Enter exactly 5 digits." if (pin is None or not re.fullmatch(r'\d{5}', str(pin).strip())) else "Invalid PIN. Access Denied."
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def admin_logout():
    session.pop('is_admin', None)
    admin_id = session.pop('admin_id', None)
    state = SystemState.query.first()
    if state and state.active_session_id == admin_id:
        state.active_session_id = ''
        db.session.commit()
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin_dashboard():
    if not session.get('is_admin'): return redirect(url_for('admin_login'))
    return render_template_string(ADMIN_HTML)

@app.route('/employee/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def employee_login():
    if session.get('is_employee'): return redirect(url_for('employee_dashboard'))
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if master_pin_matches(pin):
            session.permanent = True
            session['is_employee'] = True
            log_audit("Employee Login", "Staff logged in to employee station")
            return redirect(url_for('employee_dashboard'))
        error = "Enter exactly 5 digits." if (pin is None or not re.fullmatch(r'\d{5}', str(pin).strip())) else "Invalid PIN. Access Denied."
    return render_template_string(EMPLOYEE_LOGIN_HTML, error=error)

@app.route('/employee/logout')
def employee_logout():
    session.pop('is_employee', None)
    return redirect(url_for('employee_login'))

@app.route('/employee')
def employee_dashboard():
    if not session.get('is_employee'): return redirect(url_for('employee_login'))
    return render_template_string(EMPLOYEE_HTML)
@app.route('/api/admin/ping')
def admin_ping():
    if session.get('is_admin'):
        state = SystemState.query.first()
        if state and state.active_session_id == session.get('admin_id'):
            state.last_ping = datetime.utcnow()
            db.session.commit()
    return jsonify({"status": "ok"})

@app.route('/api/generate_link', methods=['POST'])
def generate_link():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    pin = data.get('pin')
    if not master_pin_matches(pin):
        err = "PIN must be exactly 5 digits." if (pin is None or not re.fullmatch(r'\d{5}', str(pin).strip())) else "Invalid PIN"
        return jsonify({"error": err}), 401
    # Permanent token — no times embedded, schedule is enforced server-side
    token = token_serializer.dumps({'store': '9599', 'v': 2})
    return jsonify({"url": f"{request.host_url}?token={token}"})

@app.route('/api/schedule', methods=['GET', 'POST'])
def handle_schedule():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    if request.method == 'GET':
        result = []
        for dow in range(7):
            oh, om, ch, cm = get_schedule_for_day(dow)
            result.append({
                "day": dow,
                "name": day_names[dow],
                "is_open": True,
                "open_hour": oh,
                "open_minute": om,
                "close_hour": ch,
                "close_minute": cm
            })
        return jsonify(result)
    elif request.method == 'POST':
        data = request.json
        for d in data:
            dow = d['day']
            if dow not in range(7):
                continue
            oh, om, ch, cm = get_schedule_for_day(dow)
            new_oh = d.get('open_hour', oh)
            new_om = d.get('open_minute', om)
            new_ch = d.get('close_hour', ch)
            new_cm = d.get('close_minute', cm)
            # Update in-memory fallback
            STORE_SCHEDULE[dow] = (new_oh, new_om, new_ch, new_cm)
            # Persist to DB
            entry = StoreScheduleEntry.query.filter_by(day_of_week=dow).first()
            if entry:
                entry.open_hour = new_oh
                entry.open_minute = new_om
                entry.close_hour = new_ch
                entry.close_minute = new_cm
            else:
                db.session.add(StoreScheduleEntry(
                    day_of_week=dow,
                    open_hour=new_oh, open_minute=new_om,
                    close_hour=new_ch, close_minute=new_cm
                ))
        db.session.commit()
        log_audit("Schedule Updated", "Store schedule updated by admin")
        return jsonify({"status": "success"})

@app.route('/api/closed_days', methods=['GET', 'POST', 'DELETE'])
def handle_closed_days():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    if request.method == 'GET':
        days = ClosedDay.query.all()
        return jsonify({"dates": [d.date_str for d in days]})
    elif request.method == 'POST':
        date_str = request.json.get('date', '')
        if not date_str: return jsonify({"error": "Missing date"}), 400
        existing = ClosedDay.query.filter_by(date_str=date_str).first()
        if not existing:
            db.session.add(ClosedDay(date_str=date_str))
            db.session.commit()
            log_audit("Closed Day Added", f"Shop closed on {date_str}")
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        date_str = request.json.get('date', '')
        existing = ClosedDay.query.filter_by(date_str=date_str).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
            log_audit("Closed Day Removed", f"Shop reopened on {date_str}")
        return jsonify({"status": "success"})

@app.route('/api/store/status')
def store_status_api():
    return jsonify(get_store_status())


@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    if request.method == 'GET':
        items = MenuItem.query.order_by(MenuItem.category, MenuItem.name, MenuItem.id.desc()).all()
        # Deduplicate by name — keep the first (highest id) occurrence
        seen_names = set()
        unique_items = []
        for i in items:
            key = i.name.strip().lower()
            if key not in seen_names:
                seen_names.add(key)
                unique_items.append(i)
        return jsonify([{"id": i.id, "name": i.name, "price": i.price, "letter": i.letter, "category": i.category, "stock": 0 if i.is_out_of_stock else 50, "is_out_of_stock": i.is_out_of_stock} for i in unique_items])
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    if request.method == 'POST':
        data = request.json
        existing = MenuItem.query.filter(db.func.lower(MenuItem.name) == data['name'].strip().lower()).first()
        if existing:
            return jsonify({"status": "error", "error": f"'{data['name']}' already exists in the menu."}), 409
        new_item = MenuItem(name=data['name'].strip(), price=float(data['price']), letter=data['letter'][:2].upper(), category=data['category'], is_out_of_stock=bool(data.get('is_out_of_stock', False)))
        db.session.add(new_item)
        db.session.commit()
        return jsonify({"status": "success"})

@app.route('/api/menu/<int:item_id>', methods=['PUT', 'DELETE'])
def handle_menu_item(item_id):
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    item = MenuItem.query.get_or_404(item_id)
    if request.method == 'PUT':
        data = request.json
        item.name = data['name']
        item.price = float(data['price'])
        item.letter = data['letter'][:2].upper()
        item.category = data['category']
        item.is_out_of_stock = bool(data.get('is_out_of_stock', False))
        db.session.commit()
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
        return jsonify({"status": "success"})

@app.route('/api/inventory', methods=['GET', 'PUT'])
def handle_inventory():
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    if request.method == 'GET':
        ings = Ingredient.query.order_by(Ingredient.category, Ingredient.name).all()
        return jsonify([{"id": i.id, "name": i.name, "unit": i.unit, "stock": i.stock, "category": i.category} for i in ings])
    elif request.method == 'PUT':
        for item_data in request.json:
            ing = Ingredient.query.get(item_data['id'])
            if ing: ing.stock = float(item_data['stock'])
        db.session.commit()
        return jsonify({"status": "success"})

@app.route('/reserve', methods=['POST'])
@limiter.limit("5 per minute")
def reserve_blend():
    data = request.json
    try:
        new_res = Reservation(patron_name=data['name'], patron_email=data.get('email',''), total_investment=data['total'], pickup_time=data['pickup_time'], order_source="Online", status="Waiting Confirmation")
        db.session.add(new_res)
        db.session.flush()
        for i in data['items']:
            new_inf = Infusion(reservation_id=new_res.id, foundation=i['foundation'], sweetener=i.get('sweetener','Standard'), ice_level=i.get('ice','Normal'), pearls=i.get('pearls','Take-Out'), cup_size=i.get('size','16 oz'), addons=i.get('addons',''), item_total=i['price'])
            db.session.add(new_inf)
        db.session.commit()
        return jsonify({"status": "success", "reservation_code": new_res.reservation_code}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/permission_request', methods=['POST'])
def permission_request():
    data = request.json
    code = data.get('code', '')
    name = data.get('name', 'Unknown')
    address = data.get('address', '')
    message = data.get('message', '')
    try:
        existing = PermissionRequest.query.filter_by(request_code=code).first()
        if not existing:
            pr = PermissionRequest(request_code=code, customer_name=name, address=address, message=message, granted=False)
            db.session.add(pr)
            db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/permission_status')
def permission_status():
    code = request.args.get('code', '')
    pr = PermissionRequest.query.filter_by(request_code=code).first()
    if pr:
        return jsonify({"granted": pr.granted})
    return jsonify({"granted": False})

@app.route('/api/permission_requests', methods=['GET'])
def get_permission_requests():
    if not session.get('is_admin'): return jsonify([]), 403
    pending = PermissionRequest.query.filter_by(granted=False).order_by(PermissionRequest.created_at.desc()).all()
    return jsonify([{"id": p.id, "code": p.request_code, "name": p.customer_name, "address": p.address, "message": p.message, "time": p.created_at.strftime('%I:%M %p')} for p in pending])

@app.route('/api/permission_requests/<int:req_id>/grant', methods=['POST'])
def grant_permission(req_id):
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    pr = PermissionRequest.query.get_or_404(req_id)
    pr.granted = True
    db.session.commit()
    log_audit("Permission Granted", f"Code: {pr.request_code} for {pr.customer_name}")
    return jsonify({"status": "success"})

@app.route('/api/backup', methods=['GET'])
def backup_data():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    try:
        reservations = Reservation.query.order_by(Reservation.created_at.desc()).all()
        expenses = Expense.query.order_by(Expense.created_at.desc()).all()
        menu_items = MenuItem.query.all()
        ingredients = Ingredient.query.all()
        customers = CustomerLog.query.order_by(CustomerLog.created_at.desc()).all()
        audit = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()

        payload = {
            "backup_version": "1.0",
            "exported_at": get_ph_time().strftime('%Y-%m-%d %H:%M:%S'),
            "reservations": [{"code": r.reservation_code, "name": r.patron_name, "email": r.patron_email,
                "total": r.total_investment, "status": r.status, "pickup_time": r.pickup_time,
                "source": r.order_source, "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "items": [{"foundation": i.foundation, "sweetener": i.sweetener, "ice_level": i.ice_level,
                    "pearls": i.pearls, "cup_size": i.cup_size, "addons": i.addons, "item_total": i.item_total}
                    for i in r.infusions]} for r in reservations],
            "expenses": [{"description": x.description, "amount": x.amount,
                "created_at": x.created_at.strftime('%Y-%m-%d %H:%M:%S')} for x in expenses],
            "menu_items": [{"name": m.name, "price": m.price, "letter": m.letter,
                "category": m.category, "is_out_of_stock": m.is_out_of_stock} for m in menu_items],
            "ingredients": [{"name": i.name, "unit": i.unit, "stock": i.stock} for i in ingredients],
            "customer_logs": [{"name": c.full_name, "gmail": c.gmail, "phone": c.phone,
                "source": c.order_source, "total": c.order_total,
                "created_at": c.created_at.strftime('%Y-%m-%d %H:%M:%S')} for c in customers],
            "audit_logs": [{"action": a.action, "details": a.details,
                "created_at": a.created_at.strftime('%Y-%m-%d %H:%M:%S')} for a in audit],
        }
        log_audit("Backup Downloaded", f"Full backup exported at {payload['exported_at']}")
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/restore', methods=['POST'])
def restore_data():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    if not data or data.get('backup_version') != '1.0':
        return jsonify({"error": "Invalid backup file"}), 400
    try:
        # Restore expenses
        for x in data.get('expenses', []):
            if not Expense.query.filter_by(description=x['description'], amount=x['amount']).first():
                db.session.add(Expense(description=x['description'], amount=x['amount']))

        # Restore menu items (update stock status only — don't overwrite prices set by admin)
        for m in data.get('menu_items', []):
            existing = MenuItem.query.filter_by(name=m['name']).first()
            if not existing:
                db.session.add(MenuItem(name=m['name'], price=m['price'], letter=m['letter'],
                    category=m['category'], is_out_of_stock=m.get('is_out_of_stock', False)))

        # Restore ingredients stock levels
        for i in data.get('ingredients', []):
            existing = Ingredient.query.filter_by(name=i['name']).first()
            if existing:
                existing.stock = i['stock']
            else:
                db.session.add(Ingredient(name=i['name'], unit=i['unit'], stock=i['stock']))

        # Restore customer logs
        for c in data.get('customer_logs', []):
            db.session.add(CustomerLog(full_name=c['name'], gmail=c['gmail'], phone=c['phone'],
                order_source=c['source'], order_total=c['total']))

        db.session.commit()
        log_audit("Backup Restored", f"Data restored from backup v{data.get('backup_version')}")
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/api/customer/status')
def customer_order_status():
    codes = request.args.get('codes', '').split(',')
    orders = Reservation.query.filter(Reservation.reservation_code.in_(codes)).all()
    return jsonify([{'code': o.reservation_code, 'status': o.status} for o in orders])

@app.route('/api/orders/<int:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    if not session.get('is_admin') and not session.get('is_employee'): return jsonify({"status": "error"}), 403
    order = Reservation.query.get_or_404(order_id)
    new_status = request.json.get('status', 'Completed')
    prev_status = order.status
    order.status = new_status
    # Write customer record only once, when order is first marked Completed
    if new_status == 'Completed' and prev_status != 'Completed':
        try:
            items_summary = ', '.join(i.foundation for i in order.infusions)
            clog = CustomerLog(
                full_name=order.patron_name,
                gmail=order.patron_email,
                phone='',
                order_source=order.order_source,
                order_total=order.total_investment,
                items=items_summary,
                pickup_time=order.pickup_time
            )
            db.session.add(clog)
        except Exception:
            pass  # Never block status update due to log failure
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/orders')
def api_orders():
    if not session.get('is_admin') and not session.get('is_employee'): return jsonify({"status": "error"}), 403
    res = Reservation.query.filter(Reservation.order_source != 'Legacy Notebook').order_by(Reservation.created_at.desc()).limit(50).all()
    return jsonify({'orders': [{'id': r.id, 'code': r.reservation_code, 'source': r.order_source, 'name': r.patron_name, 'total': r.total_investment, 'status': r.status, 'pickup_time': r.pickup_time, 'over_limit': len(r.infusions) > 5, 'items': [{'foundation': i.foundation, 'size': i.cup_size, 'addons': i.addons, 'sweetener': i.sweetener, 'ice': i.ice_level} for i in r.infusions]} for r in res]})

@app.route('/api/admin/manual_order', methods=['POST'])
def admin_manual_order():
    if not session.get('is_admin') and not session.get('is_employee'): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    customer_name = data.get('customer_name', 'Walk-In')
    try:
        res = Reservation(patron_name=customer_name, patron_email="walkin@local", total_investment=data['total'], pickup_time="Walk-In", status="Waiting Confirmation", order_source="Manual/Walk-In")
        db.session.add(res)
        db.session.flush()
        for i in data['items']:
            inf = Infusion(reservation_id=res.id, foundation=i['foundation'], cup_size=i.get('size','16 oz'), sweetener=i.get('sugar','N/A'), ice_level=i.get('ice','N/A'), pearls='Walk-In', addons=i.get('addons',''), item_total=i['price'])
            db.session.add(inf)
        db.session.commit()
        return jsonify({"status": "success", "reservation_code": res.reservation_code})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/finance/daily', methods=['GET'])
def daily_finance():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    now = get_ph_time()
    s = now.replace(hour=0, minute=0, second=0)
    e = now.replace(hour=23, minute=59, second=59)
    ords = Reservation.query.filter(Reservation.created_at >= s, Reservation.created_at <= e).all()
    exps = Expense.query.filter(Expense.created_at >= s, Expense.created_at <= e).all()
    return jsonify({ "system_total": sum(o.total_investment for o in ords), "expenses_total": sum(x.amount for x in exps), "expenses": [{"desc": x.description, "amount": x.amount} for x in exps] })

@app.route('/api/finance/report', methods=['GET'])
def finance_report():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    mode = request.args.get('mode', 'weekly')
    days = 30 if mode == 'monthly' else 7
    now = get_ph_time()
    result = []
    for i in range(days - 1, -1, -1):
        day = now - timedelta(days=i)
        s = day.replace(hour=0, minute=0, second=0, microsecond=0)
        e = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        ords = Reservation.query.filter(Reservation.created_at >= s, Reservation.created_at <= e).all()
        exps = Expense.query.filter(Expense.created_at >= s, Expense.created_at <= e).all()
        revenue = sum(o.total_investment for o in ords)
        expenses = sum(x.amount for x in exps)
        result.append({
            "date": day.strftime('%b %d'),
            "revenue": round(revenue, 2),
            "expenses": round(expenses, 2),
            "profit": round(revenue - expenses, 2),
            "orders": len(ords)
        })
    return jsonify(result)

@app.route('/api/finance/bestsellers', methods=['GET'])
def finance_bestsellers():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    mode = request.args.get('mode', 'all')
    now = get_ph_time()
    if mode == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        infusions = Infusion.query.join(Reservation).filter(Reservation.created_at >= start).all()
    elif mode == 'week':
        start = now - timedelta(days=7)
        infusions = Infusion.query.join(Reservation).filter(Reservation.created_at >= start).all()
    else:
        infusions = Infusion.query.all()
    counts = {}
    for inf in infusions:
        counts[inf.foundation] = counts.get(inf.foundation, 0) + 1
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    return jsonify([{"name": k, "count": v} for k, v in top])

@app.route('/api/finance/low_stock', methods=['GET'])
def finance_low_stock():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    ings = Ingredient.query.order_by(Ingredient.stock).all()
    result = []
    for ing in ings:
        threshold = 500 if ing.unit in ('ml', 'grams') else 20
        pct = (ing.stock / max(threshold * 4, 1)) * 100
        if ing.stock <= 0:
            level = 'critical'
        elif ing.stock <= threshold:
            level = 'low'
        elif pct <= 50:
            level = 'medium'
        else:
            continue
        result.append({"name": ing.name, "stock": ing.stock, "unit": ing.unit, "level": level, "threshold": threshold})
    return jsonify(result)

@app.route('/api/orders/history', methods=['GET'])
def order_history():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '')
    page = max(1, int(request.args.get('page', 1)))
    per_page = 20
    query = Reservation.query
    if q:
        query = query.filter(db.or_(
            Reservation.patron_name.ilike(f'%{q}%'),
            Reservation.reservation_code.ilike(f'%{q}%')
        ))
    if status_filter:
        query = query.filter(Reservation.status == status_filter)
    total = query.count()
    orders = query.order_by(Reservation.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "total": total, "page": page, "per_page": per_page,
        "orders": [{
            "id": o.id, "code": o.reservation_code, "name": o.patron_name,
            "total": o.total_investment, "status": o.status, "source": o.order_source,
            "pickup": o.pickup_time,
            "date": o.created_at.strftime('%b %d, %Y'),
            "time": o.created_at.strftime('%I:%M %p'),
            "items": [i.foundation for i in o.infusions]
        } for o in orders]
    })

@app.route('/api/expenses', methods=['POST'])
def add_expense():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    db.session.add(Expense(description=request.json['description'], amount=float(request.json['amount'])))
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/customer_logs', methods=['GET'])
def get_customer_logs():
    if not session.get('is_admin'): return jsonify([]), 403
    try:
        logs = CustomerLog.query.order_by(CustomerLog.created_at.desc()).limit(200).all()
        return jsonify([{
            "id":          l.id,
            "name":        l.full_name or '',
            "gmail":       l.gmail or '',
            "phone":       l.phone or '',
            "source":      l.order_source or 'Online',
            "total":       l.order_total or 0.0,
            "items":       l.items or '',
            "pickup_time": l.pickup_time or '',
            "time":        l.created_at.strftime('%Y-%m-%d %I:%M %p') if l.created_at else '—'
        } for l in logs])
    except Exception as e:
        db.session.rollback()
        print(f"customer_logs error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/audit_logs', methods=['GET'])
def get_audit_logs():
    if not session.get('is_admin'): return jsonify([]), 403
    return jsonify([{"action": l.action, "details": l.details, "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')} for l in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()])

# ==========================================
# 10. SYSTEM INITIALIZATION & SEED DATA
# ==========================================

with app.app_context():
    try:
        db.create_all()

        # Migrate: add is_out_of_stock column for existing databases
        # Checks column existence first — works on both SQLite and PostgreSQL (Render)
        try:
            is_postgres = 'postgresql' in str(db.engine.url)
            col_exists = False
            if is_postgres:
                result = db.session.execute(db.text(
                    """SELECT COUNT(*) FROM information_schema.columns
                       WHERE table_name='menu_items' AND column_name='is_out_of_stock'"""
                )).scalar()
                col_exists = (result > 0)
            else:
                # SQLite: parse PRAGMA
                cols = db.session.execute(db.text("PRAGMA table_info(menu_items)")).fetchall()
                col_exists = any(row[1] == 'is_out_of_stock' for row in cols)
            if not col_exists:
                db.session.execute(db.text(
                    'ALTER TABLE menu_items ADD COLUMN is_out_of_stock BOOLEAN NOT NULL DEFAULT FALSE'
                ))
                db.session.commit()
                print("Migration: added is_out_of_stock column to menu_items")
            else:
                print("Migration: is_out_of_stock already exists, skipped")
        except Exception as migration_err:
            db.session.rollback()
            print(f"Migration warning (non-fatal): {migration_err}")

        # Migrate: add items and pickup_time columns to customer_logs
        try:
            is_postgres = 'postgresql' in str(db.engine.url)
            for col_name, col_def in [
                ("items",       "TEXT NOT NULL DEFAULT ''"),
                ("pickup_time", "VARCHAR(50) NOT NULL DEFAULT ''"),
                ("phone",       "VARCHAR(30) NOT NULL DEFAULT ''"),
            ]:
                col_exists = False
                if is_postgres:
                    result = db.session.execute(db.text(
                        f"SELECT COUNT(*) FROM information_schema.columns "
                        f"WHERE table_name='customer_logs' AND column_name='{col_name}'"
                    )).scalar()
                    col_exists = (result > 0)
                else:
                    cols = db.session.execute(db.text("PRAGMA table_info(customer_logs)")).fetchall()
                    col_exists = any(row[1] == col_name for row in cols)
                if not col_exists:
                    db.session.execute(db.text(
                        f"ALTER TABLE customer_logs ADD COLUMN {col_name} {col_def}"
                    ))
                    db.session.commit()
                    print(f"Migration: added '{col_name}' column to customer_logs")
                else:
                    print(f"Migration: '{col_name}' already exists, skipped")
        except Exception as migration_err2:
            db.session.rollback()
            print(f"Migration warning (non-fatal): {migration_err2}")

        # Migrate: add category column to ingredients
        try:
            is_postgres = 'postgresql' in str(db.engine.url)
            col_exists = False
            if is_postgres:
                result = db.session.execute(db.text(
                    "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='ingredients' AND column_name='category'"
                )).scalar()
                col_exists = (result > 0)
            else:
                cols = db.session.execute(db.text("PRAGMA table_info(ingredients)")).fetchall()
                col_exists = any(row[1] == 'category' for row in cols)
            if not col_exists:
                db.session.execute(db.text("ALTER TABLE ingredients ADD COLUMN category VARCHAR(50) NOT NULL DEFAULT 'Other'"))
                db.session.commit()
                print("Migration: added category column to ingredients")
            else:
                print("Migration: ingredients.category already exists, skipped")
        except Exception as migration_err3:
            db.session.rollback()
            print(f"Migration warning (non-fatal): {migration_err3}")

        # ── 0. Seed store schedule (only if not already in DB) ──────────────
        for dow, (oh, om, ch, cm) in STORE_SCHEDULE.items():
            existing_sched = StoreScheduleEntry.query.filter_by(day_of_week=dow).first()
            if not existing_sched:
                db.session.add(StoreScheduleEntry(
                    day_of_week=dow,
                    open_hour=oh, open_minute=om,
                    close_hour=ch, close_minute=cm
                ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ── 1. Remove stale menu items not in the new menu ──────────────────
        valid_names = [
            'Taro Milktea', 'Okinawa Milktea', 'Wintermelon Milktea',
            'Cookies and Cream Milktea', 'Matcha Milktea', 'Dark Belgian Choco', 'Biscoff Milktea',
            'Mocha', 'Caramel Macchiato', 'Iced Americano', 'Cappuccino',
            'Coffee Jelly Drink', 'French Vanilla', 'Hazelnut',
            'Ube Milk', 'Mango Milk', 'Strawberry Milk', 'Blueberry Milk',
            'Matcha Latte', 'Matcha Caramel', 'Matcha Strawberry',
            'Lychee Mogu Soda', 'Apple Soda', 'Strawberry Soda', 'Blueberry Soda',
            'Cookies and Cream Frappe', 'Mocha Frappe', 'Coffee Frappe',
            'Strawberry Frappe', 'Matcha Frappe', 'Mango Frappe',
            'French Fries', 'Hash Brown', 'Onion Rings', 'Potato Mojos'
        ]
        MenuItem.query.filter(MenuItem.name.notin_(valid_names)).delete(synchronize_session=False)
        db.session.commit()

        # ── 2. Seed ingredients ──────────────────────────────────────────────
        ingredients_data = [
            # Teas & Bases
            ('Assam Black Tea', 'ml', 10000.0, 'Teas & Bases'),
            ('Jasmine Green Tea', 'ml', 10000.0, 'Teas & Bases'),
            ('Frappe Base', 'grams', 2000.0, 'Teas & Bases'),
            ('Soda Water', 'ml', 10000.0, 'Teas & Bases'),
            ('Espresso Shot', 'ml', 2000.0, 'Teas & Bases'),
            # Syrups & Flavors
            ('Brown Sugar Syrup', 'ml', 4000.0, 'Syrups & Flavors'),
            ('Wintermelon Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Okinawa Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Strawberry Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Lychee Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Caramel Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Mocha Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('French Vanilla Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Hazelnut Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Ube Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Blueberry Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Apple Syrup', 'ml', 2000.0, 'Syrups & Flavors'),
            ('Mango Puree', 'grams', 2000.0, 'Syrups & Flavors'),
            ('Taro Paste', 'grams', 1500.0, 'Syrups & Flavors'),
            ('Matcha Powder', 'grams', 1000.0, 'Syrups & Flavors'),
            ('Dark Choco Powder', 'grams', 2000.0, 'Syrups & Flavors'),
            ('Cookies & Cream Powder', 'grams', 2000.0, 'Syrups & Flavors'),
            ('Biscoff Crumbs', 'grams', 1000.0, 'Syrups & Flavors'),
            # Dairy
            ('Fresh Milk', 'ml', 8000.0, 'Dairy'),
            ('Non-Dairy Creamer', 'grams', 5000.0, 'Dairy'),
            # Add-ons
            ('Tapioca Pearls', 'grams', 3000.0, 'Add-ons'),
            ('Nata', 'grams', 3000.0, 'Add-ons'),
            ('Coffee Jelly', 'grams', 3000.0, 'Add-ons'),
            # Snacks
            ('Hash Brown (pcs)', 'pcs', 100.0, 'Snacks'),
            ('French Fries', 'grams', 5000.0, 'Snacks'),
            ('Onion Rings', 'grams', 3000.0, 'Snacks'),
            ('Potato Mojos', 'grams', 3000.0, 'Snacks'),
            ('Cooking Oil', 'ml', 10000.0, 'Snacks'),
            # Consumables & Packaging
            ('Hot Cups 12oz', 'pcs', 200.0, 'Consumables & Packaging'),
            ('Hot Cups 16oz', 'pcs', 200.0, 'Consumables & Packaging'),
            ('Cold Plastic Cups 16oz', 'pcs', 300.0, 'Consumables & Packaging'),
            ('Cold Plastic Cups 24oz', 'pcs', 300.0, 'Consumables & Packaging'),
            ('Flat Lids', 'pcs', 500.0, 'Consumables & Packaging'),
            ('Snack Packaging', 'pcs', 500.0, 'Consumables & Packaging'),
        ]
        for name, unit, stock, category in ingredients_data:
            existing = Ingredient.query.filter_by(name=name).first()
            if not existing:
                db.session.add(Ingredient(name=name, unit=unit, stock=stock, category=category))
            else:
                # Update category for existing ingredients that may not have one
                if existing.category == 'Other' or not existing.category:
                    existing.category = category
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ── 3. Seed menu items ───────────────────────────────────────────────
        menu_data = [
            # Milktea
            ('Taro Milktea', 49.00, 'T', 'Milktea'),
            ('Okinawa Milktea', 49.00, 'O', 'Milktea'),
            ('Wintermelon Milktea', 49.00, 'W', 'Milktea'),
            ('Cookies and Cream Milktea', 49.00, 'C', 'Milktea'),
            ('Matcha Milktea', 49.00, 'M', 'Milktea'),
            ('Dark Belgian Choco', 49.00, 'D', 'Milktea'),
            ('Biscoff Milktea', 49.00, 'B', 'Milktea'),
            # Coffee
            ('Mocha', 49.00, 'M', 'Coffee'),
            ('Caramel Macchiato', 49.00, 'C', 'Coffee'),
            ('Iced Americano', 49.00, 'I', 'Coffee'),
            ('Cappuccino', 49.00, 'C', 'Coffee'),
            ('Coffee Jelly Drink', 49.00, 'C', 'Coffee'),
            ('French Vanilla', 49.00, 'F', 'Coffee'),
            ('Hazelnut', 49.00, 'H', 'Coffee'),
            # Milk Series
            ('Ube Milk', 59.00, 'U', 'Milk Series'),
            ('Mango Milk', 59.00, 'M', 'Milk Series'),
            ('Strawberry Milk', 59.00, 'S', 'Milk Series'),
            ('Blueberry Milk', 59.00, 'B', 'Milk Series'),
            # Matcha Series
            ('Matcha Latte', 59.00, 'ML', 'Matcha Series'),
            ('Matcha Caramel', 59.00, 'MC', 'Matcha Series'),
            ('Matcha Strawberry', 59.00, 'MS', 'Matcha Series'),
            ('Matcha Frappe', 79.00, 'MF', 'Matcha Series'),
            # Fruit Soda
            ('Lychee Mogu Soda', 59.00, 'LM', 'Fruit Soda'),
            ('Apple Soda', 59.00, 'A', 'Fruit Soda'),
            ('Strawberry Soda', 59.00, 'S', 'Fruit Soda'),
            ('Blueberry Soda', 59.00, 'B', 'Fruit Soda'),
            # Frappe
            ('Cookies and Cream Frappe', 79.00, 'CC', 'Frappe'),
            ('Mocha Frappe', 79.00, 'M', 'Frappe'),
            ('Coffee Frappe', 79.00, 'C', 'Frappe'),
            ('Strawberry Frappe', 79.00, 'S', 'Frappe'),
            ('Mango Frappe', 79.00, 'MG', 'Frappe'),
            # Snacks
            ('French Fries', 39.00, 'F', 'Snacks'),
            ('Hash Brown', 29.00, 'H', 'Snacks'),
            ('Onion Rings', 59.00, 'O', 'Snacks'),
            ('Potato Mojos', 59.00, 'P', 'Snacks'),
        ]
        # ── Deduplicate existing menu items by name (case-insensitive, keep highest id) ──
        try:
            all_items = MenuItem.query.order_by(MenuItem.id.asc()).all()
            seen = {}
            to_delete = []
            for item in all_items:
                key = item.name.strip().lower()
                if key in seen:
                    to_delete.append(item)  # older duplicate — delete it
                else:
                    seen[key] = item
            for dup in to_delete:
                db.session.delete(dup)
            db.session.commit()
        except Exception:
            db.session.rollback()

        for name, price, letter, category in menu_data:
            existing_item = MenuItem.query.filter(db.func.lower(MenuItem.name) == name.strip().lower()).first()
            if not existing_item:
                db.session.add(MenuItem(name=name, price=price, letter=letter, category=category))
            else:
                # Always sync category to the canonical seed value
                existing_item.category = category
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ── 4. Seed recipes ──────────────────────────────────────────────────
        recipe_data = [
            # Snacks
            ('Hash Brown', 'Hash Brown (pcs)', 1), ('Hash Brown', 'Snack Packaging', 1), ('Hash Brown', 'Cooking Oil', 20),
            ('French Fries', 'French Fries', 150), ('French Fries', 'Snack Packaging', 1), ('French Fries', 'Cooking Oil', 50),
            ('Onion Rings', 'Onion Rings', 150), ('Onion Rings', 'Snack Packaging', 1), ('Onion Rings', 'Cooking Oil', 50),
            ('Potato Mojos', 'Potato Mojos', 150), ('Potato Mojos', 'Snack Packaging', 1), ('Potato Mojos', 'Cooking Oil', 50),
            # Milktea
            ('Taro Milktea', 'Assam Black Tea', 150), ('Okinawa Milktea', 'Assam Black Tea', 150),
            ('Wintermelon Milktea', 'Assam Black Tea', 150), ('Matcha Milktea', 'Assam Black Tea', 150),
            ('Cookies and Cream Milktea', 'Assam Black Tea', 150), ('Dark Belgian Choco', 'Assam Black Tea', 150),
            ('Biscoff Milktea', 'Assam Black Tea', 150),
            # Coffee
            ('Mocha', 'Espresso Shot', 30), ('Caramel Macchiato', 'Espresso Shot', 30),
            ('Iced Americano', 'Espresso Shot', 30), ('Cappuccino', 'Espresso Shot', 30),
            ('Coffee Jelly Drink', 'Espresso Shot', 30), ('French Vanilla', 'Espresso Shot', 30),
            ('Hazelnut', 'Espresso Shot', 30),
            # Milk Series
            ('Ube Milk', 'Fresh Milk', 200), ('Mango Milk', 'Fresh Milk', 200),
            ('Strawberry Milk', 'Fresh Milk', 200), ('Blueberry Milk', 'Fresh Milk', 200),
            # Matcha Series
            ('Matcha Latte', 'Matcha Powder', 10), ('Matcha Caramel', 'Matcha Powder', 10),
            ('Matcha Strawberry', 'Matcha Powder', 10),
            # Fruit Soda
            ('Lychee Mogu Soda', 'Soda Water', 200), ('Apple Soda', 'Soda Water', 200),
            ('Strawberry Soda', 'Soda Water', 200), ('Blueberry Soda', 'Soda Water', 200),
            # Frappe
            ('Cookies and Cream Frappe', 'Frappe Base', 30), ('Mocha Frappe', 'Frappe Base', 30),
            ('Coffee Frappe', 'Frappe Base', 30), ('Strawberry Frappe', 'Frappe Base', 30),
            ('Matcha Frappe', 'Frappe Base', 30), ('Mango Frappe', 'Frappe Base', 30),
        ]
        for item_name, ing_name, qty in recipe_data:
            m = MenuItem.query.filter_by(name=item_name).first()
            i = Ingredient.query.filter_by(name=ing_name).first()
            if m and i:
                exists = RecipeItem.query.filter_by(menu_item_id=m.id, ingredient_id=i.id).first()
                if not exists:
                    db.session.add(RecipeItem(menu_item_id=m.id, ingredient_id=i.id, quantity_required=qty))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    except Exception as e:
        print(f"DB Init Error: {e}")
        db.session.rollback()

# ==========================================
# 11. APPLICATION RUNNER
# ==========================================

if __name__ == '__main__':
    if os.environ.get('RENDER') or os.environ.get('DYNO'):
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port)
    else:
        import webbrowser
        from threading import Timer

        def open_browser():
            url = 'http://127.0.0.1:5000/login'
            # Try Chrome first; fall back to default browser
            opened = False
            for chrome_name in ('chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
                try:
                    chrome = webbrowser.get(chrome_name)
                    chrome.open_new(url)
                    opened = True
                    break
                except Exception:
                    continue
            if not opened:
                # Windows: try registry path directly
                import subprocess, sys
                if sys.platform == 'win32':
                    try:
                        subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe', url])
                        opened = True
                    except Exception:
                        pass
                if not opened:
                    webbrowser.open_new(url)  # final fallback

        print("==================================================")
        print(" STARTING SYSTEM (DESKTOP APP MODE)")
        print(f" CUSTOMER POS LINK: http://{get_local_ip()}:5000/")
        print(" ADMIN DASHBOARD:   http://127.0.0.1:5000/login")
        print("==================================================")
        
        Timer(1.5, open_browser).start()
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
