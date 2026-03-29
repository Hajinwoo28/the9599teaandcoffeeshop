import os
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

RAW_PIN = os.environ.get('ADMIN_PIN', '12345')
ADMIN_PIN_HASH = generate_password_hash(RAW_PIN)

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
    5: (15, 0, 20, 0),   # Saturday
    6: (15, 0, 20, 0),   # Sunday
}

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
    oh, om, ch, cm = STORE_SCHEDULE[dow]

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

    # Find next opening
    day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    for delta in range(1, 8):
        next_dow = (dow + delta) % 7
        noh, nom, _, _ = STORE_SCHEDULE[next_dow]
        next_open_str = f"{day_names[next_dow]} at {fmt(noh, nom)}"
        break

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
    gmail = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False, default='')
    order_source = db.Column(db.String(30), nullable=False, default='Online')
    order_total = db.Column(db.Float, nullable=False, default=0.0)
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

# ==========================================
# 4. FRONTEND HTML TEMPLATES
# ==========================================

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login | 9599 Tea & Coffee</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@400;500;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'DM Sans', sans-serif; }
        body { background-color: #F5EFE6; display: flex; justify-content: center; align-items: center; height: 100vh; padding: 20px; }
        .login-box { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 10px 25px rgba(111, 78, 55, 0.1); text-align: center; width: 100%; max-width: 400px; border: 1px solid #EFEBE4; }
        .login-box h2 { color: #3E2723; margin-bottom: 5px; font-weight: 800; font-family: 'Playfair Display', serif; }
        .login-box p { color: #8D6E63; font-size: 0.9rem; margin-bottom: 25px; }
        .input-pin { width: 100%; padding: 15px; border: 2px solid #D7CCC8; border-radius: 8px; font-size: 1.5rem; text-align: center; letter-spacing: 8px; margin-bottom: 20px; outline: none; font-weight: 800; color: #3E2723; background: #FDFBF7; }
        .input-pin:focus { border-color: #6F4E37; }
        .btn-login { width: 100%; background: #6F4E37; color: white; border: none; padding: 15px; border-radius: 8px; font-weight: 700; font-size: 1rem; cursor: pointer; display: flex; justify-content: center; align-items: center; gap: 10px; }
        .btn-login:hover { background: #4A3324; }
        .error { background: #FFEBEE; color: #C62828; padding: 10px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; margin-bottom: 20px; border: 1px solid #FFCDD2; }
    </style>
</head>
<body>
    <div class="login-box">
        <i class="fas fa-coffee" style="font-size: 3rem; color: #A67B5B; margin-bottom: 15px;"></i>
        <h2>Admin Access</h2>
        <p>Enter master PIN for 9599 Store System</p>
        {% if error %}<div class="error"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="pin" class="input-pin" placeholder="•••••" required autofocus>
            <button type="submit" class="btn-login">Login Securely</button>
        </form>
    </div>
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
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    
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

        .sidebar { width: 380px; background: var(--bg-base); border-left: 1px solid var(--border-color); display: flex; flex-direction: column; z-index: 50; }
        .cart-top-section { padding: 25px 25px 15px; flex-shrink: 0; }
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

        .cart-content { padding: 0 25px 15px; flex: 1; overflow-y: auto; }
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

        .checkout-btn { width: 100%; padding: 18px; border: none; border-radius: 12px; font-size: 1rem; font-weight: 800; letter-spacing: 1px; display: flex; justify-content: center; align-items: center; gap: 10px; color: var(--text-dark); background: var(--border-color); cursor: not-allowed; transition: all 0.25s ease; font-family: 'DM Sans', sans-serif; text-transform: uppercase; }
        .checkout-btn.active { background: var(--gold); cursor: pointer; box-shadow: 0 6px 20px rgba(200, 155, 60, 0.4); }
        .checkout-btn.active:hover { transform: translateY(-2px); }

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
            body { height: auto; min-height: 100vh; overflow-y: auto; }
            .main-container { flex-direction: column; height: auto; overflow: visible; }
            .menu-area { flex: none; height: auto; overflow: visible; padding-bottom: 20px; }
            .menu-grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
            .sidebar { width: 100%; flex: none; height: auto; border-left: none; border-top: 2px solid var(--border-color); }
        }

        #toast-container { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 9999; display: flex; flex-direction: column; gap: 10px; }
        .toast { background-color: #3E2723; color: #fff; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; }
        .toast.error { background-color: #C62828; }
        .toast.success { background-color: #388E3C; }

        .notif-bell { cursor: pointer; position: relative; padding: 10px; border-radius: 50%; background: var(--gold-light); color: var(--gold); border: none; }
        .notif-badge { position: absolute; top: -2px; right: -2px; background: var(--danger); color: white; border-radius: 50%; padding: 2px 6px; font-size: 0.7rem; font-weight: 800; display: none; }
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
<!-- GOOGLE LOGIN GATEKEEPER -->
<div id="login-gatekeeper" class="gate-wrapper">
    <div class="gate-card">
        <div style="width:70px; height:70px; border-radius:50%; border:2px solid var(--gold); display:flex; justify-content:center; align-items:center; margin: 0 auto 15px;">
            <img src="/static/images/9599.jpg" style="width:60px; height:60px; border-radius:50%; object-fit:cover;" onerror="this.style.display='none'">
        </div>
        <h1 style="color:var(--text-dark); font-family:'Playfair Display',serif; font-size:2rem; line-height:1.1;">9599 Tea & Coffee</h1>
        <p style="font-family:'Playfair Display',serif; color:var(--gold); letter-spacing:3px; font-size:0.8rem; font-weight:900; margin-bottom: 30px;">PARNE NA!</p>
        <p style="color:var(--text-light); font-weight:500; margin-bottom: 25px; font-size: 0.95rem;">Sign in to place your order.</p>

        <div id="g_id_onload" data-client_id="{{ google_client_id }}" data-context="signin" data-ux_mode="popup" data-callback="handleGoogleLogin" data-auto_prompt="false"></div>
        <div class="g_id_signin" data-type="standard" data-shape="rectangular" data-theme="outline" data-text="continue_with" data-size="large" data-logo_alignment="left" style="display:flex; justify-content:center;"></div>
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
    
    async function handleGoogleLogin(response) {
        try {
            const res = await fetch('/api/auth/google', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ token: response.credential }) });
            if (res.ok) location.reload();
            else showToast("Authentication Error", "error");
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
    <div class="notif-container" style="display:flex; align-items:center; gap:10px;">
        <div title="Find Us" onclick="openLocModal()" style="cursor:pointer; width:38px; height:38px; border-radius:50%; background:var(--gold-light); display:flex; align-items:center; justify-content:center; border:1px solid var(--border-color);">
            <i class="fas fa-map-marker-alt" style="color:var(--gold); font-size:16px;"></i>
        </div>
        <div class="notif-bell" onclick="alert('Notification center ready.')">
            <i class="fas fa-bell"></i><span class="notif-badge" id="notif-badge">0</span>
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
                <b>Mon – Fri:</b> 10:00 AM – 7:00 PM<br>
                <b>Sat – Sun:</b> 3:00 PM – 8:00 PM
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
            <input type="email" class="name-input" id="customer-gmail" placeholder="Gmail Address *" value="{{ session.get('customer_email', '') if session.get('customer_email', '').endswith('@gmail.com') else '' }}" oninput="checkCheckoutStatus()">
            <input type="tel" class="name-input" id="customer-phone" placeholder="Phone Number *" oninput="checkCheckoutStatus()">

            <label class="pickup-label">Pick-up Time *</label>
            <div class="slide-clock-wrapper" id="pickup-clock-wrapper">
                <div class="slide-clock-display" id="pickup-clock-display">Select Time</div>
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
        
        <span class="modal-section-label">Choose Size</span>
        <div class="size-btns">
            <button class="size-btn selected" id="btn-size-16" onclick="selectSize('16 oz', 49)">16 oz <span class="size-btn-price">₱49</span></button>
            <button class="size-btn" id="btn-size-22" onclick="selectSize('22 oz', 59)">22 oz <span class="size-btn-price">₱59</span></button>
        </div>

        <div class="sel-row">
            <div class="sel-group">
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

        <span class="modal-section-label">Add-ons</span>
        <div class="addon-grid">
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Nata"> <span>🟡 Nata (+₱10)</span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Pearl"> <span>⚫ Pearl (+₱10)</span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Coffee Jelly"> <span>☕ Coffee Jelly (+₱10)</span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Cloud Foam"> <span>☁️ Cloud Foam (+₱15)</span></label>
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
        <div class="modal-actions">
            <button class="btn-cancel" onclick="document.getElementById('fries-modal').style.display='none'">Cancel</button>
            <button class="btn-add" onclick="confirmFriesToCart()">Add to Cart</button>
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
            <button class="btn-cancel" style="flex:1;" onclick="location.reload()">Done</button>
            <button class="btn-add" style="flex:1;" onclick="printCustomerReceipt()"><i class="fas fa-print"></i> Print Receipt</button>
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
        'French Fries (Plain)':        '/static/images/french_fries.jpg',
        'French Fries (Cheese)':       '/static/images/french_fries.jpg',
        'French Fries (BBQ)':          '/static/images/french_fries.jpg',
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
        'French Fries (Plain)':      { em: '🍟', grad: 'grad-snacks',   badge: 'none' },
        'French Fries (Cheese)':     { em: '🍟', grad: 'grad-snacks',   badge: 'none' },
        'French Fries (BBQ)':        { em: '🍟', grad: 'grad-snacks',   badge: 'none' },
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
        fetchMenu(); 
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
        let filtered = cat === 'All' ? menuItems : menuItems.filter(i => i.category === cat);
        filtered = filtered.filter(i => !i.is_out_of_stock);
        
        if (filtered.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; padding:40px; color:var(--text-light); font-weight:600;">No items available in this category.</div>';
            return;
        }

        filtered.forEach(item => {
            const priceDisplay = ['Milktea','Coffee'].includes(item.category) ? '₱49 / ₱59' : `₱${item.price.toFixed(0)}`;
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
            const priceDisplay = ['Milktea','Coffee'].includes(item.category) ? '₱49 / ₱59' : `₱${item.price.toFixed(0)}`;
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

    function setOrderType(type) {
        orderType = type;
        document.getElementById('btn-dine-in').className = type === 'Dine-In' ? 'type-btn active' : 'type-btn';
        document.getElementById('btn-take-out').className = type === 'Take-Out' ? 'type-btn active' : 'type-btn';
    }

    function selectSize(size, price) {
        pendingSize = size; pendingPrice = price;
        document.getElementById('btn-size-16').className = size === '16 oz' ? 'size-btn selected' : 'size-btn';
        document.getElementById('btn-size-22').className = size === '22 oz' ? 'size-btn selected' : 'size-btn';
    }

    function addToCart(name, cat, price) {
        pendingItemName = name;
        pendingCat = cat;
        pendingPrice = price;

        // Only Milktea and Coffee have size options (16oz ₱49 / 22oz ₱59)
        if (['Milktea', 'Coffee'].includes(cat)) {
            document.getElementById('size-modal-title').innerText = name;
            selectSize('16 oz', price);
            document.querySelectorAll('.addon-checkbox').forEach(cb => cb.checked = false);
            document.getElementById('size-modal').style.display = 'flex';
        } else {
            // All other categories (Snacks, Frappe, Milk Series, Matcha Series, Fruit Soda) — fixed price, add directly
            cart.push({name, cat, size:'Regular', sugar:'N/A', ice:'N/A', addons:[], price});
            updateCartUI();
        }
    }

    function confirmAddToCart() {
        let addons = []; let cost = 0;
        document.querySelectorAll('.addon-checkbox').forEach(cb => {
            if(cb.checked) { addons.push(cb.value); cost += (cb.value==='Cloud Foam'?15:10); }
        });
        cart.push({
            name: pendingItemName, cat: pendingCat, size: pendingSize,
            sugar: document.getElementById('sugar-level-select').value,
            ice: document.getElementById('ice-level-select').value,
            addons, price: pendingPrice + cost
        });
        document.getElementById('size-modal').style.display = 'none';
        updateCartUI();
    }

    function confirmFriesToCart() {
        let flavor = document.querySelector('input[name="fries_flavor"]:checked').value;
        let displayName = `${pendingItemName} (${flavor})`;
        cart.push({name: displayName, cat: pendingCat, size: 'Regular', sugar: 'N/A', ice: 'N/A', addons: [], price: pendingPrice});
        document.getElementById('fries-modal').style.display = 'none';
        updateCartUI();
    }

    function removeFromCart(i) { cart.splice(i,1); updateCartUI(); }

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
                let sub = c.size==='Regular' ? '' : `${c.size} · ${c.sugar} · ${c.ice}`;
                let adds = c.addons.length ? `<br>+ ${c.addons.join(', ')}` : '';
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
        document.getElementById('pickup-clock-display').innerText = timeStr;
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
                let orders = JSON.parse(localStorage.getItem('myOrders')) || [];
                orders.push({code, status: 'Waiting Confirmation'});
                localStorage.setItem('myOrders', JSON.stringify(orders));
            } else { showToast("Error: " + data.message, "error"); btn.innerHTML = 'Place Order'; btn.disabled = false; }
        } catch(e) { showToast("Connection Error", "error"); btn.innerHTML = 'Place Order'; btn.disabled = false; }
    }

    async function submitOrderWithOverride() {
        document.getElementById('perm-modal').style.display = 'none';
        if(permPoll) clearInterval(permPoll);
        await submitOrder();
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
            <div class="shop-meta">BIR TIN: 000-000-000-000</div>
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
                    showToast(`Order #${srv.code} is now: ${srv.status}`, "success");
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
    <title>9599 Admin POS</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@400;500;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'DM Sans', sans-serif; }
        body { background-color: #F5EFE6; color: #3E2723; display: flex; height: 100vh; overflow: hidden; }
        #toast-container { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 9999; display: flex; flex-direction: column; gap: 10px; }
        .toast { background-color: #3E2723; color: #fff; padding: 12px 24px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; }
        .toast.error { background-color: #C62828; }
        .toast.success { background-color: #388E3C; }
        .sidebar { width: 220px; background: white; border-right: 1px solid #EFEBE4; display: flex; flex-direction: column; z-index: 100; }
        .sidebar-header { padding: 30px 25px; font-weight: 800; font-size: 1.1rem; color: #3E2723; }
        .nav-links { flex: 1; display: flex; flex-direction: column; }
        .nav-item { padding: 15px 25px; color: #8D6E63; font-weight: 600; font-size: 0.95rem; display: flex; align-items: center; gap: 12px; cursor: pointer; border-left: 4px solid transparent; }
        .nav-item:hover { background: #FDFBF7; color: #6F4E37; }
        .nav-item.active { background: #F5EFE6; color: #3E2723; border-left: 4px solid #6F4E37; font-weight: 700; }
        .sidebar-footer { padding: 25px; border-top: 1px solid #EFEBE4; display: flex; flex-direction: column; gap: 10px; }
        .btn-reload { width: 100%; background: #F5EFE6; color: #5D4037; border: none; padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; }
        .btn-logout { width: 100%; background: #FFEBEE; color: #C62828; border: 1px solid #FFCDD2; padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; }
        .main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        .topbar { background: white; padding: 20px 40px; border-bottom: 1px solid #EFEBE4; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
        .page-title { font-size: 1.4rem; font-weight: 800; color: #0F172A; }
        .content-body { padding: 25px; overflow: hidden; flex: 1; display: flex; flex-direction: column; }
        .tab-pane { display: none; flex: 1; flex-direction: column; min-height: 0; overflow: hidden; }
        .tab-pane.active { display: flex; }
        .settings-card { background: white; border-radius: 12px; padding: 25px; border: 1px solid #EFEBE4; display: flex; flex-direction: column; min-height: 0; margin-bottom: 20px; }
        .card-title { font-size: 1rem; font-weight: 800; color: #3E2723; margin-bottom: 15px; }
        .table-responsive { flex: 1; overflow-y: auto; border: 1px solid #EFEBE4; border-radius: 8px; }
        .kds-table { width: 100%; border-collapse: collapse; }
        .kds-table th, .kds-table td { padding: 15px 20px; text-align: left; border-bottom: 1px solid #EFEBE4; font-size: 0.85rem; }
        .kds-table th { background: #F5EFE6; color: #5D4037; position: sticky; top: 0; }
        .kds-badge { font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
        .btn-blue { background: #6F4E37; color: white; border: none; padding: 12px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; width: 100%; margin-bottom: 10px; }
        .btn-dark { background: #3E2723; color: white; border: none; padding: 10px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; }
        .input-group label { display: block; font-size: 0.75rem; font-weight: 800; color: #8D6E63; margin-bottom: 8px; }
        .input-pin { width: 100%; padding: 12px; border: 1px solid #D7CCC8; border-radius: 8px; margin-bottom: 15px; font-weight: 600; outline: none; }
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(62,39,35,0.6); align-items: center; justify-content: center; }
        .modal-content { background: white; padding: 30px; border-radius: 12px; width: 400px; border: 2px solid #6F4E37; }
        .error-state { padding: 40px; text-align: center; color: #C62828; font-weight: 600; }
        
        /* Layouts */
        .settings-grid-layout { display: grid; grid-template-columns: 340px 1fr; gap: 20px; height: 100%; overflow: hidden; }
        .quick-order-layout { display: grid; grid-template-columns: 1fr 350px; gap: 20px; height: 100%; }
        .finance-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; height: 100%; }

        /* Notification Bells & Status Badges */
        .notif-bell { cursor: pointer; position: relative; padding: 10px; border-radius: 50%; background: #F5EFE6; color: #6F4E37; border: none; font-size: 1.2rem; }
        .notif-badge { position: absolute; top: -5px; right: -5px; background: #C62828; color: white; border-radius: 50%; padding: 2px 6px; font-size: 0.7rem; font-weight: 800; display: none; }
        .notif-panel { display: none; position: absolute; top: 60px; right: 40px; background: white; border: 1px solid #EFEBE4; border-radius: 12px; width: 300px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); z-index: 1000; flex-direction: column; max-height: 400px; }
        .notif-panel-header { padding: 15px; border-bottom: 1px solid #EFEBE4; font-weight: 800; display: flex; justify-content: space-between; align-items: center; }
        .notif-panel-body { padding: 10px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 10px; }
        .notif-item { padding: 10px; border-radius: 8px; background: #FDFBF7; border: 1px solid #EFEBE4; font-size: 0.85rem; }
        .notif-item b { color: #3E2723; }
        .status-badge { padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; color: white; }
        .status-waiting { background: #F57C00; }
        .status-preparing { background: #1976D2; }
        .status-ready { background: #388E3C; }
        .status-completed { background: #4CAF50; }
        .status-cancelled { background: #D32F2F; }
        .item-thumb { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; margin-right: 10px; border: 1px solid #EFEBE4; }
    </style>
</head>
<body>

<div id="toast-container"></div>
<audio id="admin-audio" src="https://assets.mixkit.co/active_storage/sfx/2869/2869-preview.mp3" preload="auto"></audio>

<aside class="sidebar">
    <div>
        <div class="sidebar-header">Admin Mode</div>
        <nav class="nav-links">
            <div class="nav-item active" onclick="switchTab('kds', 'Live Orders', this)"><i class="fas fa-clipboard-list"></i> Orders</div>
            <div class="nav-item" onclick="switchTab('quick-order', 'Manual POS', this)"><i class="fas fa-cash-register"></i> Manual POS</div>
            <div class="nav-item" onclick="switchTab('inventory', 'Inventory', this)"><i class="fas fa-boxes"></i> Inventory</div>
            <div class="nav-item" onclick="switchTab('finance', 'Finance & Reports', this)"><i class="fas fa-chart-line"></i> Finance</div>
            <div class="nav-item" onclick="switchTab('audit', 'Audit Trail', this)"><i class="fas fa-list-ol"></i> Audit Trail</div>
            <div class="nav-item" onclick="switchTab('settings', 'Settings & Menu', this)"><i class="fas fa-sliders-h"></i> Settings</div>
        </nav>
    </div>
    <div class="sidebar-footer">
        <button class="btn-reload" onclick="location.reload()">Reload UI</button>
        <button class="btn-logout" onclick="location.href='/logout'">Lock</button>
    </div>
</aside>

<main class="main-content">
    <header class="topbar">
        <div class="page-title" id="page-title">Live Orders</div>
        <div style="display:flex; align-items:center; gap:15px; position:relative;">
            <a href="/" target="_blank" class="btn-dark" style="text-decoration:none;">Customer POS</a>
            <div id="clock" style="font-weight:bold; background:#F5EFE6; padding:8px 15px; border-radius:20px;">00:00 PM</div>
            <div class="notif-bell" onclick="toggleAdminNotif()">
                <i class="fas fa-bell"></i><span class="notif-badge" id="admin-notif-badge">0</span>
            </div>
            <div class="notif-panel" id="admin-notif-panel">
                <div class="notif-panel-header">
                    <span>Notifications</span>
                    <button style="background:none; border:none; color:#C62828; cursor:pointer; font-weight:700;" onclick="clearAdminNotifs()">Clear</button>
                </div>
                <div class="notif-panel-body" id="admin-notif-body">
                    <div style="text-align:center; color:#8D6E63; padding:20px;">No new notifications</div>
                </div>
            </div>
        </div>
    </header>

    <div class="content-body">
        
        <!-- LIVE ORDERS -->
        <div id="tab-kds" class="tab-pane active">
            <!-- Pending Permission Requests -->
            <div id="perm-requests-section" style="margin-bottom:16px; background:#FFF8F8; border:1.5px solid #FFCDD2; border-radius:10px; overflow:hidden;">
                <div style="padding:12px 20px; background:#FFEBEE; display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:800; color:#C62828; font-size:0.95rem;"><i class="fas fa-hand-paper"></i> Pending Permission Requests</span>
                    <button class="btn-dark" style="padding:5px 12px; background:#C62828; font-size:0.8rem;" onclick="fetchPermissionRequests()">Refresh</button>
                </div>
                <div style="overflow-x:auto;">
                    <table class="kds-table" style="background:white;">
                        <thead><tr><th>Time</th><th>Code</th><th>Customer</th><th>Message</th><th>Action</th></tr></thead>
                        <tbody id="perm-requests-body">
                            <tr><td colspan="5" style="text-align:center; padding:16px; color:#A67B5B;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
            <!-- Orders Table -->
            <div class="table-responsive">
                <table class="kds-table">
                    <thead><tr><th>Order #</th><th>Source</th><th>Name</th><th>Time</th><th>Total</th><th>Items</th><th>Status</th></tr></thead>
                    <tbody id="kds-table-body"></tbody>
                </table>
            </div>
        </div>

        <!-- MANUAL POS -->
        <div id="tab-quick-order" class="tab-pane">
            <div class="quick-order-layout">
                <div style="display:flex; flex-direction:column; overflow:hidden;">
                    <input type="text" id="qo-search" class="input-pin" placeholder="Search menu..." onkeyup="filterQuickOrderMenu()">
                    <div id="qo-menu-grid" style="display:grid; grid-template-columns:1fr 1fr; gap:10px; overflow-y:auto; padding-right:10px;"></div>
                </div>
                <div class="settings-card" style="margin:0;">
                    <div class="card-title">Walk-In Cart</div>
                    <input type="text" id="qo-customer-name" class="input-pin" placeholder="Customer Name *" style="margin-bottom:12px;">
                    <div id="qo-cart-items" style="flex:1; overflow-y:auto; border-bottom:1px solid #EFEBE4; margin-bottom:15px; padding-bottom:15px;"></div>
                    <div style="font-size:1.2rem; font-weight:800; margin-bottom:15px; display:flex; justify-content:space-between;"><span>Total</span><span id="qo-total-display">₱0.00</span></div>
                    <button class="btn-blue" onclick="submitQuickOrder()">Save Walk-In Order</button>
                </div>
            </div>
        </div>

        <!-- INVENTORY -->
        <div id="tab-inventory" class="tab-pane">
            <div class="settings-card" style="height:100%;">
                <div class="card-title">Raw Ingredients</div>
                <div class="table-responsive">
                    <table class="kds-table">
                        <thead><tr><th>Ingredient</th><th>Unit</th><th>Stock</th></tr></thead>
                        <tbody id="admin-inventory-list"></tbody>
                    </table>
                </div>
                <button class="btn-blue" style="margin-top:20px;" onclick="saveInventory()">Save Inventory</button>
            </div>
        </div>

        <!-- FINANCE -->
        <div id="tab-finance" class="tab-pane">
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; height:50%; min-height:0; margin-bottom:20px;">
                <div style="display:flex; flex-direction:column; overflow-y:auto; gap:20px;">
                    <div class="settings-card" style="margin:0;">
                        <div class="card-title">Daily Close-Out</div>
                        <div style="display:flex; justify-content:space-between; margin-bottom:10px;"><b>System Total</b> <span id="sys-total">₱0.00</span></div>
                        <div style="display:flex; justify-content:space-between; margin-bottom:10px; color:#C62828;"><b>Expenses</b> <span id="expense-total">- ₱0.00</span></div>
                        <div style="display:flex; justify-content:space-between; font-size:1.2rem; font-weight:800; margin-top:10px; border-top:1px dashed #D7CCC8; padding-top:10px;"><b>Net Profit</b> <span id="cash-drawer">₱0.00</span></div>
                        <button class="btn-dark" style="margin-top:15px;" onclick="fetchDailyFinances()">Refresh Data</button>
                    </div>
                    <div class="settings-card" style="margin:0;">
                        <div class="card-title">Top Sellers</div>
                        <div id="best-sellers-list"></div>
                    </div>
                </div>
                <div style="display:flex; flex-direction:column; overflow-y:auto;">
                    <div class="settings-card" style="margin:0;">
                        <div class="card-title">Log Expense</div>
                        <input type="text" id="exp-desc" class="input-pin" placeholder="Description (e.g. Ice)">
                        <input type="number" id="exp-amount" class="input-pin" placeholder="Amount (₱)">
                        <button class="btn-blue" onclick="addExpense()">Record Expense</button>
                    </div>
                </div>
            </div>
            <div class="settings-card" style="flex:1; min-height:0; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                    <div class="card-title" style="margin:0;">Customer Records</div>
                    <button class="btn-dark" onclick="fetchCustomerLogs()">Refresh</button>
                </div>
                <div class="table-responsive" style="flex:1;">
                    <table class="kds-table">
                        <thead><tr><th>Date & Time</th><th>Name</th><th>Gmail</th><th>Phone</th><th>Source</th><th>Total</th></tr></thead>
                        <tbody id="customer-logs-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- AUDIT -->
        <div id="tab-audit" class="tab-pane">
            <div class="settings-card" style="height:100%;">
                <div class="card-title">System Audit Logs</div>
                <div class="table-responsive">
                    <table class="kds-table">
                        <thead><tr><th>Time</th><th>Action</th><th>Details</th></tr></thead>
                        <tbody id="audit-table-body"></tbody>
                    </table>
                </div>
                <button class="btn-dark" style="margin-top:20px;" onclick="fetchAuditLogs()">Refresh Logs</button>
            </div>
        </div>

        <!-- SETTINGS -->
        <div id="tab-settings" class="tab-pane">
            <div class="settings-grid-layout">
                <div style="display:flex; flex-direction:column; overflow-y:auto; gap:16px; height:100%; padding-bottom:20px; padding-right:4px;">

                    <!-- ── Store Schedule Card ── -->
                    <div class="settings-card" style="padding:20px; margin-bottom:0; flex-shrink:0;">
                        <div class="card-title" style="margin-bottom:14px;">Store Link &amp; Schedule</div>

                        <!-- Schedule Block -->
                        <div style="background:#FFF8F2; border:1px solid #EFEBE4; border-radius:12px; padding:16px; margin-bottom:16px;">
                            <div style="display:flex; align-items:center; gap:8px; margin-bottom:10px;">
                                <span style="font-size:1.1rem;">📅</span>
                                <span style="font-weight:800; font-size:0.95rem; color:#3E2723;">Store Schedule</span>
                            </div>
                            <div style="display:flex; flex-direction:column; gap:5px; font-size:0.88rem; color:#5D4037;">
                                <div><b>Mon – Fri:</b> &nbsp;10:00 AM – 7:00 PM</div>
                                <div><b>Sat – Sun:</b> &nbsp;3:00 PM – 8:00 PM</div>
                            </div>
                            <div style="margin-top:8px; font-size:0.78rem; color:#A1887F; border-top:1px dashed #D7CCC8; padding-top:8px;">
                                Online orders close <b style="color:#6F4E37;">1 hour before closing</b>.
                            </div>
                            <div id="store-live-status" style="margin-top:10px; font-size:0.88rem; font-weight:700; padding:8px 12px; border-radius:8px; background:#FFF3E0;"></div>
                        </div>

                        <!-- Permanent Link Generator -->
                        <div style="background:#F5EFE6; border:1px solid #EFEBE4; border-radius:12px; padding:16px; margin-bottom:0;">
                            <div style="font-size:0.82rem; color:#6F4E37; line-height:1.6; margin-bottom:12px;">
                                Generate a <b>permanent</b> ordering link. Pin it once on your shop page — the system automatically opens and closes based on the schedule above. No need to regenerate daily.
                            </div>
                            <input type="password" id="store-pin" class="input-pin" placeholder="Enter Master PIN to generate link" style="margin-bottom:10px;">
                            <button class="btn-blue" onclick="saveConfigurations()" style="width:100%; margin-bottom:10px;">
                                <i class="fas fa-link"></i> Generate Permanent Link
                            </button>
                            <div style="position:relative;">
                                <input type="text" id="posLink" class="input-pin" style="background:#fff; padding-right:80px; font-size:0.78rem; color:#6F4E37;" readonly placeholder="Your permanent link will appear here…">
                                <div style="position:absolute; right:6px; top:50%; transform:translateY(-50%); display:flex; gap:4px;">
                                    <button onclick="copyPosLink()" title="Copy link" style="background:#6F4E37; border:none; border-radius:6px; color:#fff; width:32px; height:32px; cursor:pointer; display:flex; align-items:center; justify-content:center;">
                                        <i class="fas fa-copy" style="font-size:12px;"></i>
                                    </button>
                                    <button onclick="openInChrome()" title="Open customer site in Chrome" style="background:#1a73e8; border:none; border-radius:6px; color:#fff; width:32px; height:32px; cursor:pointer; display:flex; align-items:center; justify-content:center;">
                                        <i class="fas fa-external-link-alt" style="font-size:12px;"></i>
                                    </button>
                                </div>
                            </div>
                            <div style="margin-top:8px; font-size:0.75rem; color:#A1887F; display:flex; align-items:center; gap:5px;">
                                <i class="fas fa-info-circle"></i>
                                &nbsp;Click <b style="color:#1a73e8;">&#x2197;</b> to open the customer site directly in Chrome.
                            </div>
                        </div>
                    </div>

                    <!-- ── Backup & Recovery Card ── -->
                    <div class="settings-card" style="padding:20px; margin-bottom:0; flex-shrink:0;">
                        <div class="card-title" style="margin-bottom:6px;">Backup &amp; Recovery</div>
                        <p style="font-size:0.85rem; color:#8D6E63; margin-bottom:18px; line-height:1.6;">
                            Download a full backup of all orders, customers, expenses, and menu data as a JSON file. To restore, upload a previously downloaded backup file.
                        </p>
                        <div style="display:flex; flex-direction:column; gap:10px;">
                            <button onclick="downloadBackup()" style="width:100%; padding:13px 16px; background:#6F4E37; color:#fff; border:none; border-radius:10px; font-weight:700; font-size:0.92rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; font-family:inherit;">
                                <i class="fas fa-download"></i> Download Backup
                            </button>
                            <label style="width:100%; padding:13px 16px; background:#3E2723; color:#fff; border:none; border-radius:10px; font-weight:700; font-size:0.92rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; font-family:inherit; box-sizing:border-box;">
                                <i class="fas fa-upload"></i> Restore from Backup
                                <input type="file" id="restore-file" accept=".json" style="display:none;" onchange="restoreBackup(this)">
                            </label>
                        </div>
                        <div id="backup-status" style="margin-top:12px; font-size:0.85rem; font-weight:700; min-height:20px;"></div>
                    </div>

                </div>
                <div class="settings-card" style="overflow:hidden; display:flex; flex-direction:column; margin-bottom:0;">
                    <div style="display:flex; justify-content:space-between;">
                        <div class="card-title">Menu Management</div>
                        <button class="btn-dark" onclick="openMenuModal()">Add Item</button>
                    </div>
                    <div class="table-responsive" style="flex:1; overflow-y:auto;">
                        <table class="kds-table">
                            <thead><tr><th>Name</th><th>Category</th><th>Price</th><th>Stock</th><th>Action</th></tr></thead>
                            <tbody id="admin-menu-list"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
</main>

<div id="menu-modal" class="modal">
    <div class="modal-content">
        <h2 id="menu-modal-title" style="margin-bottom:20px;">Add Menu Item</h2>
        <form id="menu-form" onsubmit="saveMenuItem(event)">
            <input type="text" id="menu-name" class="input-pin" placeholder="Item Name" required>
            <input type="number" id="menu-price" class="input-pin" placeholder="Price" required>
            <input type="text" id="menu-category" class="input-pin" placeholder="Category" required>
            <input type="text" id="menu-letter" class="input-pin" placeholder="Short Letter" required>
            <label style="display:flex; align-items:center; gap:10px; font-size:0.85rem; font-weight:700; color:#5D4037; margin-bottom:15px; cursor:pointer;">
                <input type="checkbox" id="menu-out-of-stock" style="width:18px; height:18px; cursor:pointer;">
                Mark as Out of Stock
            </label>
            <div style="display:flex; gap:10px;">
                <button type="button" class="btn-dark" style="flex:1;" onclick="document.getElementById('menu-modal').style.display='none'">Cancel</button>
                <button type="submit" class="btn-blue" style="flex:1; margin-bottom:0;">Save</button>
            </div>
        </form>
    </div>
</div>

<div id="qo-size-modal" class="modal">
    <div class="modal-content">
        <h2 id="qo-size-modal-title" style="margin-bottom:20px; text-align:center;">Options</h2>
        <div style="display:flex; gap:10px; margin-bottom:15px;">
            <button id="btn-qo-16" style="flex:1; padding:10px; border:2px solid #6F4E37; background:#6F4E37; color:white; border-radius:8px; font-weight:700;" onclick="selectQOSize('16 oz', 49)">16 oz<br>₱49</button>
            <button id="btn-qo-22" style="flex:1; padding:10px; border:2px solid #D7CCC8; background:white; color:#3E2723; border-radius:8px; font-weight:700;" onclick="selectQOSize('22 oz', 59)">22 oz<br>₱59</button>
        </div>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; display:block;">Sugar Level</label>
        <select id="qo-sugar" class="input-pin" style="padding:10px; margin-bottom:12px;">
            <option value="100% Sugar">100% Sugar (Normal)</option>
            <option value="75% Sugar">75% Sugar (Less)</option>
            <option value="50% Sugar">50% Sugar (Half)</option>
            <option value="0% Sugar">0% Sugar (No Sugar)</option>
        </select>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; display:block;">Ice Level</label>
        <select id="qo-ice" class="input-pin" style="padding:10px; margin-bottom:12px;">
            <option value="Normal Ice">Normal Ice</option>
            <option value="Less Ice">Less Ice</option>
            <option value="No Ice">No Ice</option>
        </select>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; display:block;">Add-ons</label>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:16px;">
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-addon-cb" value="Nata" data-cost="10"> 🟡 Nata (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-addon-cb" value="Pearl" data-cost="10"> ⚫ Pearl (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-addon-cb" value="Coffee Jelly" data-cost="10"> ☕ Coffee Jelly (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-addon-cb" value="Cloud Foam" data-cost="15"> ☁️ Cloud Foam (+₱15)</label>
        </div>
        <div style="display:flex; gap:10px;">
            <button class="btn-dark" style="flex:1;" onclick="document.getElementById('qo-size-modal').style.display='none'">Cancel</button>
            <button class="btn-blue" style="flex:2; margin-bottom:0;" onclick="confirmQuickCart()">Add to Cart</button>
        </div>
    </div>
</div>

<!-- Manual POS Custom Modal for fixed-price items (no size picker) -->
<div id="qo-custom-modal" class="modal">
    <div class="modal-content">
        <h2 id="qo-custom-modal-title" style="margin-bottom:20px; text-align:center;">Customize</h2>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; display:block;">Sugar Level</label>
        <select id="qo-custom-sugar" class="input-pin" style="padding:10px; margin-bottom:12px;">
            <option value="100% Sugar">100% Sugar (Normal)</option>
            <option value="75% Sugar">75% Sugar (Less)</option>
            <option value="50% Sugar">50% Sugar (Half)</option>
            <option value="0% Sugar">0% Sugar (No Sugar)</option>
            <option value="N/A">N/A (Not Applicable)</option>
        </select>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; display:block;">Ice Level</label>
        <select id="qo-custom-ice" class="input-pin" style="padding:10px; margin-bottom:12px;">
            <option value="Normal Ice">Normal Ice</option>
            <option value="Less Ice">Less Ice</option>
            <option value="No Ice">No Ice</option>
            <option value="N/A">N/A (Not Applicable)</option>
        </select>
        <label style="font-size:0.75rem; font-weight:800; color:#8D6E63; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; display:block;">Add-ons</label>
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:16px;">
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-custom-addon-cb" value="Nata" data-cost="10"> 🟡 Nata (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-custom-addon-cb" value="Pearl" data-cost="10"> ⚫ Pearl (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-custom-addon-cb" value="Coffee Jelly" data-cost="10"> ☕ Coffee Jelly (+₱10)</label>
            <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; padding:8px; border:1px solid #D7CCC8; border-radius:8px; cursor:pointer;"><input type="checkbox" class="qo-custom-addon-cb" value="Cloud Foam" data-cost="15"> ☁️ Cloud Foam (+₱15)</label>
        </div>
        <div style="display:flex; gap:10px;">
            <button class="btn-dark" style="flex:1;" onclick="document.getElementById('qo-custom-modal').style.display='none'">Cancel</button>
            <button class="btn-blue" style="flex:2; margin-bottom:0;" onclick="confirmCustomCart()">Add to Cart</button>
        </div>
    </div>
</div>

<!-- Admin Fries Modal for Manual POS -->
<div id="qo-fries-modal" class="modal">
    <div class="modal-content">
        <h2 id="qo-fries-modal-title" style="margin-bottom:20px; text-align:center;">Select Flavor</h2>
        <div class="sel-row" style="flex-direction:column; gap:15px;">
            <label class="input-group" style="font-size:1rem; cursor:pointer;"><input type="radio" name="qo_fries_flavor" value="Plain" checked> Plain</label>
            <label class="input-group" style="font-size:1rem; cursor:pointer;"><input type="radio" name="qo_fries_flavor" value="Cheese"> Cheese</label>
            <label class="input-group" style="font-size:1rem; cursor:pointer;"><input type="radio" name="qo_fries_flavor" value="Barbeque"> Barbeque</label>
        </div>
        <div style="display:flex; gap:10px; margin-top:20px;">
            <button class="btn-dark" style="flex:1;" onclick="document.getElementById('qo-fries-modal').style.display='none'">Cancel</button>
            <button class="btn-blue" style="flex:2; margin-bottom:0;" onclick="confirmQOFries()">Add</button>
        </div>
    </div>
</div>

<script>
    function updateTime() {
        const now = new Date();
        let hours = now.getHours() % 12 || 12;
        let minutes = now.getMinutes().toString().padStart(2, '0');
        let ampm = now.getHours() >= 12 ? 'PM' : 'AM';
        document.getElementById('clock').innerText = hours + ':' + minutes + ' ' + ampm;
    }
    setInterval(updateTime, 1000);
    updateTime();

    function pingSession() { fetch('/api/admin/ping'); }
    setInterval(pingSession, 30000); // Send heartbeat

    function showToast(msg, type='info') {
        const t = document.createElement('div');
        t.className = `toast ${type}`;
        t.innerText = msg;
        document.getElementById('toast-container').appendChild(t);
        setTimeout(() => t.remove(), 3000);
    }

    async function apiFetch(url, options={}) {
        const res = await fetch(url, options);
        if(res.status === 403) { window.location.href = '/login'; return null; }
        return res;
    }

    function switchTab(id, title, btn) {
        document.querySelectorAll('.tab-pane').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
        document.getElementById('tab-'+id).classList.add('active');
        btn.classList.add('active');
        document.getElementById('page-title').innerText = title;

        if(id==='kds') fetchLiveOrders();
        if(id==='quick-order') fetchQuickOrderMenu();
        if(id==='inventory') fetchAdminInventory();
        if(id==='finance') { fetchDailyFinances(); fetchCustomerLogs(); }
        if(id==='audit') fetchAuditLogs();
        if(id==='settings') fetchAdminMenu();
    }

    function escapeHTML(str) { 
        let div = document.createElement('div'); 
        div.innerText = str; 
        return div.innerHTML; 
    }

    const ADMIN_IMAGE_MAP = {
        'Lychee Mogu Soda':            '/static/images/lychee_mogu_soda.jpg',
        'Strawberry Soda':             '/static/images/strawberry_soda.jpg',
        'Blueberry Soda':              '/static/images/blueberry_soda.jpg',
        'Apple Soda':                  '/static/images/green_apple_soda.jpg',
        'Matcha Caramel':              '/static/images/matcha_caramel.jpg',
        'Matcha Frappe':               '/static/images/matcha_frappe.jpg',
        'Matcha Latte':                '/static/images/matcha_latte.jpg',
        'Matcha Strawberry':           '/static/images/matcha_strawberry.jpg',
        'French Fries (Plain)':        '/static/images/french_fries.jpg',
        'French Fries (Cheese)':       '/static/images/french_fries.jpg',
        'French Fries (BBQ)':          '/static/images/french_fries.jpg',
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

    const ADMIN_EMOJI_MAP = {
        'Dirty Matcha': '🍵', 'Biscoff Frappe': '☕', 'Midnight Velvet': '⭐', 'Taro Symphony': '🌿', 'Strawberry Lychee': '🍓', 'French Fries': '🍟', 'default': '🧋'
    };
    
    function getAdminPhotoHtml(name) {
        if (ADMIN_IMAGE_MAP[name]) {
            return `<img src="${ADMIN_IMAGE_MAP[name]}" class="item-thumb" onerror="this.style.display='none'">`;
        }
        let em = ADMIN_EMOJI_MAP[name] || ADMIN_EMOJI_MAP['default'];
        return `<div class="item-thumb" style="display:flex; justify-content:center; align-items:center; font-size:1.5rem; background:#F5EFE6;">${em}</div>`;
    }

    function getStatusClass(status) {
        switch(status) {
            case 'Waiting Confirmation': return 'status-waiting';
            case 'Preparing Order': return 'status-preparing';
            case 'Ready for Pick-up': return 'status-ready';
            case 'Completed': return 'status-completed';
            case 'Cancelled': return 'status-cancelled';
            default: return 'status-waiting';
        }
    }

    let adminNotifs = [];
    let lastKnownOrderIds = new Set();
    let firstLoad = true;

    function playNotificationSound() {
        try {
            document.getElementById('admin-audio').play().catch(()=>{});
        } catch(e){}
    }

    function toggleAdminNotif() {
        const panel = document.getElementById('admin-notif-panel');
        panel.style.display = panel.style.display === 'flex' ? 'none' : 'flex';
    }

    function clearAdminNotifs() {
        adminNotifs = [];
        updateAdminNotifUI();
    }

    function addAdminNotif(orderCode, name, total) {
        adminNotifs.unshift({code: orderCode, name: name, total: total, time: new Date().toLocaleTimeString()});
        if(adminNotifs.length > 20) adminNotifs.pop();
        updateAdminNotifUI();
        playNotificationSound();
        showToast(`New order from ${name} (${orderCode})!`, "success");
    }

    function updateAdminNotifUI() {
        const badge = document.getElementById('admin-notif-badge');
        const body = document.getElementById('admin-notif-body');
        if(adminNotifs.length > 0) {
            badge.style.display = 'block';
            badge.innerText = adminNotifs.length;
            body.innerHTML = adminNotifs.map(n => {
                if(n.type === 'perm') {
                    return `<div class="notif-item" style="border-left:4px solid #C62828;">
                        <b style="color:#C62828;">🔔 Permission Request</b><br>
                        <b>${escapeHTML(n.name)}</b> — ${escapeHTML(n.code)}<br>
                        <span style="font-size:0.8rem; color:#8D6E63;">${escapeHTML(n.address)}</span><br>
                        <span style="font-size:0.8rem;">${escapeHTML(n.message)}</span>
                        <span style="float:right; color:#A38C7D; font-size:0.75rem;">${n.time}</span>
                    </div>`;
                }
                return `<div class="notif-item">
                    <b>Order #${n.code}</b> from ${escapeHTML(n.name)}<br>
                    Amount: ₱${n.total.toFixed(2)} <span style="float:right; color:#A38C7D; font-size:0.75rem;">${n.time}</span>
                </div>`;
            }).join('');
        } else {
            badge.style.display = 'none';
            badge.innerText = '0';
            body.innerHTML = '<div style="text-align:center; color:#8D6E63; padding:20px;">No new notifications</div>';
        }
    }

    async function fetchLiveOrders() {
        const tbody = document.getElementById('kds-table-body');
        try {
            const res = await apiFetch('/api/orders?_t='+Date.now());
            if(!res || !res.ok) {
                tbody.innerHTML = '<tr><td colspan="7" class="error-state">⚠️ Database Connection Error.</td></tr>';
                return;
            }
            const data = await res.json();
            
            let currentIds = new Set();
            data.orders.forEach(o => currentIds.add(o.id));

            if (!firstLoad) {
                data.orders.forEach(o => {
                    if(!lastKnownOrderIds.has(o.id)) {
                        addAdminNotif(o.code, o.name, o.total);
                    }
                });
            }
            lastKnownOrderIds = currentIds;
            firstLoad = false;

            tbody.innerHTML = '';
            if(data.orders.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:30px; color:#A67B5B;">No active orders.</td></tr>';
                return;
            }
            data.orders.forEach(o => {
                let itemsHTML = o.items.map(i => `
                    <div style="margin-bottom:5px;">
                        <b>${escapeHTML(i.foundation)}</b> (${escapeHTML(i.size)})<br><span style="font-size:0.75rem; color:#8D6E63;">${escapeHTML(i.sweetener)} | ${escapeHTML(i.ice)}${i.addons ? ' | + ' + escapeHTML(i.addons) : ''}</span>
                    </div>
                `).join('');

                let items;
                if (o.items.length >= 2) {
                    const toggleId = `items-toggle-${o.id}`;
                    const listId = `items-list-${o.id}`;
                    items = `
                        <div>
                            <button id="${toggleId}" onclick="toggleOrderItems('${listId}', '${toggleId}')"
                                style="background:#F5EFE6; border:1px solid #D7CCC8; border-radius:6px; padding:5px 10px; font-size:0.8rem; font-weight:700; color:#5D4037; cursor:pointer; display:flex; align-items:center; gap:6px;">
                                <i class="fas fa-chevron-down"></i> ${o.items.length} items
                            </button>
                            <div id="${listId}" style="display:none; margin-top:8px;">${itemsHTML}</div>
                        </div>`;
                } else {
                    items = itemsHTML;
                }

                let sel = `<select onchange="updateOrderStatus(${o.id}, this.value)" class="status-badge ${getStatusClass(o.status)}" style="padding:6px; border:none; outline:none; font-weight:bold; cursor:pointer;">
                    <option value="Waiting Confirmation" ${o.status==='Waiting Confirmation'?'selected':''} style="color:black;background:white;">Waiting</option>
                    <option value="Preparing Order" ${o.status==='Preparing Order'?'selected':''} style="color:black;background:white;">Preparing</option>
                    <option value="Ready for Pick-up" ${o.status==='Ready for Pick-up'?'selected':''} style="color:black;background:white;">Ready</option>
                    <option value="Completed" ${o.status==='Completed'?'selected':''} style="color:black;background:white;">Completed</option>
                    <option value="Cancelled" ${o.status==='Cancelled'?'selected':''} style="color:black;background:white;">Cancelled</option>
                </select>`;

                tbody.innerHTML += `<tr><td>${escapeHTML(o.code)}</td><td>${escapeHTML(o.source)}</td><td><b>${escapeHTML(o.name)}</b></td><td>${escapeHTML(o.pickup_time)}</td><td>₱${o.total.toFixed(2)}</td><td>${items}</td><td>${sel}</td></tr>`;
            });
        } catch(e) { tbody.innerHTML = '<tr><td colspan="7" class="error-state">⚠️ Network Error. Retrying...</td></tr>'; }
    }

    async function updateOrderStatus(id, status) {
        await apiFetch(`/api/orders/${id}/status`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status}) });
        showToast('Status Updated', 'success');
        fetchLiveOrders();
    }

    function toggleOrderItems(listId, toggleId) {
        const list = document.getElementById(listId);
        const btn = document.getElementById(toggleId);
        const isHidden = list.style.display === 'none';
        list.style.display = isHidden ? 'block' : 'none';
        btn.querySelector('i').className = isHidden ? 'fas fa-chevron-up' : 'fas fa-chevron-down';
    }

    let adminMenu = [];
    async function fetchQuickOrderMenu() {
        const grid = document.getElementById('qo-menu-grid');
        try {
            const res = await apiFetch('/api/menu');
            if(!res || !res.ok) { grid.innerHTML = '<div class="error-state">Database Error</div>'; return; }
            adminMenu = await res.json();
            renderQOMenu(adminMenu);
        } catch(e) { grid.innerHTML = '<div class="error-state">Network Error</div>'; }
    }

    function renderQOMenu(items) {
        const grid = document.getElementById('qo-menu-grid');
        const available = items.filter(i => !i.is_out_of_stock);
        if(available.length === 0) {
            grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; padding:30px; color:#A67B5B; font-weight:600;">No items available.</div>';
            return;
        }
        grid.innerHTML = available.map(i => {
            const imgSrc = ADMIN_IMAGE_MAP[i.name];
            const thumb = imgSrc
                ? `<img src="${imgSrc}" style="width:100%; height:80px; object-fit:cover; border-radius:6px; margin-bottom:8px; display:block;" onerror="this.style.display='none'">`
                : `<div style="width:100%; height:80px; background:#F5EFE6; border-radius:6px; margin-bottom:8px; display:flex; align-items:center; justify-content:center; font-size:2rem;">🧋</div>`;
            return `<div style="background:white; border:1px solid #D7CCC8; padding:10px; border-radius:10px; cursor:pointer;" onclick="addQO('${escapeHTML(i.name.replace(/'/g, "\\'"))}', '${escapeHTML(i.category.replace(/'/g, "\\'"))}', ${i.price})">
                ${thumb}
                <div style="font-weight:700; font-size:0.9rem; line-height:1.2;">${escapeHTML(i.name)}</div>
                <div style="font-size:0.8rem; color:#A67B5B; margin-top:2px;">₱${i.price}</div>
            </div>`;
        }).join('');
    }

    function filterQuickOrderMenu() {
        const q = document.getElementById('qo-search').value.toLowerCase();
        renderQOMenu(adminMenu.filter(i => !i.is_out_of_stock && i.name.toLowerCase().includes(q)));
    }

    let qoCart = [];
    let qoPending = {};
    function addQO(name, cat, price) {
        qoPending = {name, cat, price};
        if (['Milktea', 'Coffee'].includes(cat)) {
            document.getElementById('qo-size-modal-title').innerText = name;
            selectQOSize('16 oz', price);
            document.querySelectorAll('.qo-addon-cb').forEach(cb => cb.checked = false);
            document.getElementById('qo-sugar').value = '100% Sugar';
            document.getElementById('qo-ice').value = 'Normal Ice';
            document.getElementById('qo-size-modal').style.display = 'flex';
        } else {
            // Fixed-price items — open custom modal (sugar, ice, add-ons, no size)
            document.getElementById('qo-custom-modal-title').innerText = name;
            document.querySelectorAll('.qo-custom-addon-cb').forEach(cb => cb.checked = false);
            const isSnack = cat === 'Snacks';
            document.getElementById('qo-custom-sugar').value = isSnack ? 'N/A' : '100% Sugar';
            document.getElementById('qo-custom-ice').value = isSnack ? 'N/A' : 'Normal Ice';
            document.getElementById('qo-custom-modal').style.display = 'flex';
        }
    }

    function confirmQuickCart() {
        let addons = []; let addonCost = 0;
        document.querySelectorAll('.qo-addon-cb').forEach(cb => {
            if(cb.checked) { addons.push(cb.value); addonCost += parseInt(cb.dataset.cost); }
        });
        qoCart.push({
            name: qoPending.name, size: qoPending.size, price: qoPending.price + addonCost,
            sugar: document.getElementById('qo-sugar').value,
            ice: document.getElementById('qo-ice').value,
            addons: addons.join(', ')
        });
        document.getElementById('qo-size-modal').style.display = 'none';
        renderQOCart();
    }

    function confirmCustomCart() {
        let addons = []; let addonCost = 0;
        document.querySelectorAll('.qo-custom-addon-cb').forEach(cb => {
            if(cb.checked) { addons.push(cb.value); addonCost += parseInt(cb.dataset.cost); }
        });
        qoCart.push({
            name: qoPending.name, size: 'Regular', price: qoPending.price + addonCost,
            sugar: document.getElementById('qo-custom-sugar').value,
            ice: document.getElementById('qo-custom-ice').value,
            addons: addons.join(', ')
        });
        document.getElementById('qo-custom-modal').style.display = 'none';
        renderQOCart();
    }

    function selectQOSize(size, price) {
        qoPending.size = size; qoPending.price = price;
        document.getElementById('btn-qo-16').style.background = size==='16 oz' ? '#6F4E37' : 'white';
        document.getElementById('btn-qo-16').style.color = size==='16 oz' ? 'white' : '#3E2723';
        document.getElementById('btn-qo-22').style.background = size==='22 oz' ? '#388E3C' : 'white';
        document.getElementById('btn-qo-22').style.color = size==='22 oz' ? 'white' : '#3E2723';
    }

    function renderQOCart() {
        const list = document.getElementById('qo-cart-items');
        let t = 0; list.innerHTML = '';
        qoCart.forEach((c, i) => {
            t += c.price;
            let sub = c.size && c.size !== 'Regular' ? `${c.size}` : '';
            let details = [c.sugar, c.ice].filter(v => v && v !== 'N/A').join(' · ');
            let addons = c.addons ? `+${c.addons}` : '';
            let subLine = [sub, details, addons].filter(Boolean).join(' | ');
            list.innerHTML += `<div style="display:flex; justify-content:space-between; margin-bottom:10px; padding-bottom:10px; border-bottom:1px solid #EFEBE4;">
                <div>
                    <div style="font-weight:700;">${escapeHTML(c.name)}</div>
                    ${subLine ? `<div style="font-size:0.75rem; color:#8D6E63; margin-top:2px;">${escapeHTML(subLine)}</div>` : ''}
                </div>
                <div style="display:flex; align-items:center; gap:10px;">
                    <span style="font-weight:700;">₱${c.price}</span>
                    <i class="fas fa-times" style="color:#C62828; cursor:pointer;" onclick="qoCart.splice(${i},1); renderQOCart();"></i>
                </div>
            </div>`;
        });
        document.getElementById('qo-total-display').innerText = `₱${t.toFixed(2)}`;
    }

    async function submitQuickOrder() {
        if(qoCart.length===0) return showToast("Cart empty", "error");
        const customerName = document.getElementById('qo-customer-name').value.trim();
        if(!customerName) return showToast("Please enter customer name", "error");
        const payload = {
            customer_name: customerName,
            items: qoCart.map(c => ({foundation: c.name, size: c.size || 'Regular', price: c.price, sugar: c.sugar || 'N/A', ice: c.ice || 'N/A', addons: c.addons || ''})),
            total: qoCart.reduce((s,c) => s+c.price, 0)
        };
        try {
            const res = await apiFetch('/api/admin/manual_order', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) {
                const data = await res.json();
                showToast("Saved!", "success");

                // Offer receipt print
                const receiptData = {
                    code: data.reservation_code || 'WALK-IN',
                    name: customerName,
                    pickup: 'Walk-In',
                    source: 'Manual/Walk-In',
                    total: payload.total,
                    items: payload.items.map(i => ({foundation: i.foundation, size: i.size, sweetener: i.sugar, ice: i.ice, addons: i.addons, price: i.price}))
                };
                // Auto-print receipt immediately
                printAdminReceipt(receiptData);

                qoCart=[]; document.getElementById('qo-customer-name').value=''; renderQOCart(); fetchLiveOrders();
            }
        } catch(e) { showToast("Error saving order", "error"); }
    }

    function printAdminReceipt(o) {
        const now = new Date();
        const dateStr = now.toLocaleDateString('en-PH', {day:'numeric', month:'short', year:'numeric'});
        const timeStr = now.toLocaleTimeString('en-PH', {hour:'numeric', minute:'2-digit', hour12:true});

        // Aggregate items by name, counting qty
        const itemMap = {};
        o.items.forEach(i => {
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
        <title>Receipt — ${o.code}</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Courier New', Courier, monospace; font-size: 16px; color: #111; background: white; padding: 32px 24px; max-width: 560px; margin: 0 auto; }
            .center { text-align: center; }
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
            <div class="shop-meta">BIR TIN: 000-000-000-000</div>
        </div>
        <hr class="divider-solid">
        <div class="center" style="font-size:1rem; font-weight:bold; letter-spacing:1px;">OFFICIAL RECEIPT</div>
        <div class="center" style="font-size:0.88rem; color:#555; margin-top:4px;">Date: ${dateStr} &nbsp;|&nbsp; Time: ${timeStr}</div>
        <hr class="divider-solid">
        <div style="font-size:1rem; margin-bottom:4px;"><b>Order #:</b> ${o.code}</div>
        <div style="font-size:1rem; margin-bottom:4px;"><b>Customer:</b> ${o.name}</div>
        <div style="font-size:1rem; margin-bottom:6px;"><b>Type:</b> ${o.source || 'Walk-In'}</div>
        <hr class="divider-solid">
        <table>
            <thead><tr><th>Item</th><th class="right">Amount</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <hr class="divider-dash">
        <table class="total-section">
            <tr>
                <td style="text-align:left;">Total Items: ${totalQty}</td>
                <td style="text-align:right;">&#8369;${o.total.toFixed(2)}</td>
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

    async function fetchAdminInventory() {
        const tbody = document.getElementById('admin-inventory-list');
        try {
            const res = await apiFetch('/api/inventory');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            tbody.innerHTML = data.map(i => `<tr><td><b>${escapeHTML(i.name)}</b></td><td>${escapeHTML(i.unit)}</td><td><input type="number" class="input-pin stock-input" data-id="${i.id}" value="${i.stock}" style="width:100px; padding:5px; margin:0;"></td></tr>`).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Network Error</td></tr>'; }
    }

    async function saveInventory() {
        const payload = Array.from(document.querySelectorAll('.stock-input')).map(input => ({ id: parseInt(input.getAttribute('data-id')), stock: parseFloat(input.value) }));
        try {
            const res = await apiFetch('/api/inventory', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) showToast("Inventory Saved", "success");
        } catch(e) { showToast("Error", "error"); }
    }

    async function fetchDailyFinances() {
        try {
            const res = await apiFetch('/api/finance/daily');
            if(!res || !res.ok) { document.getElementById('sys-total').innerText = "DB Error"; return; }
            const data = await res.json();
            document.getElementById('sys-total').innerText = `₱${data.system_total.toFixed(2)}`;
            document.getElementById('expense-total').innerText = `- ₱${data.expenses_total.toFixed(2)}`;
            document.getElementById('cash-drawer').innerText = `₱${(data.system_total - data.expenses_total).toFixed(2)}`;
        } catch(e) { document.getElementById('sys-total').innerText = "Net Error"; }
    }

    async function fetchCustomerLogs() {
        const tbody = document.getElementById('customer-logs-body');
        try {
            const res = await apiFetch('/api/customer_logs');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="6" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            if(data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:20px; color:#A67B5B;">No customer records yet.</td></tr>';
                return;
            }
            tbody.innerHTML = data.map(l => `
                <tr>
                    <td style="font-size:0.8rem;">${escapeHTML(l.time)}</td>
                    <td><b>${escapeHTML(l.name)}</b></td>
                    <td>${escapeHTML(l.gmail)}</td>
                    <td>${escapeHTML(l.phone)}</td>
                    <td><span class="kds-badge" style="background:${l.source==='Online'?'#1976D2':'#388E3C'}; color:white;">${escapeHTML(l.source)}</span></td>
                    <td><b>₱${l.total.toFixed(2)}</b></td>
                </tr>`).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="6" class="error-state">Network Error</td></tr>'; }
    }

    async function fetchAuditLogs() {
        const tbody = document.getElementById('audit-table-body');
        try {
            const res = await apiFetch('/api/audit_logs');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            tbody.innerHTML = data.map(l => `<tr><td style="font-size:0.8rem;">${escapeHTML(l.time)}</td><td><b>${escapeHTML(l.action)}</b></td><td>${escapeHTML(l.details)}</td></tr>`).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Network Error</td></tr>'; }
    }

    let editMenuId = null;
    async function fetchAdminMenu() {
        const tbody = document.getElementById('admin-menu-list');
        try {
            const res = await apiFetch('/api/menu');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="4" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            tbody.innerHTML = data.map(m => {
                const oos = m.is_out_of_stock;
                const stockBadge = oos
                    ? `<span style="background:#FFEBEE; color:#C62828; padding:3px 8px; border-radius:4px; font-size:0.75rem; font-weight:700;">Out of Stock</span>`
                    : `<span style="background:#E8F5E9; color:#2E7D32; padding:3px 8px; border-radius:4px; font-size:0.75rem; font-weight:700;">In Stock</span>`;
                const toggleBtn = `<button class="btn-dark" style="padding:5px 10px; background:${oos ? '#388E3C' : '#C62828'};" onclick="toggleOutOfStock(${m.id}, ${!oos})">${oos ? '✅ Mark In Stock' : '🚫 Mark Out of Stock'}</button>`;
                return `<tr>
                    <td><b>${escapeHTML(m.name)}</b></td>
                    <td>${escapeHTML(m.category)}</td>
                    <td>₱${m.price}</td>
                    <td>${stockBadge}</td>
                    <td style="display:flex; gap:6px; flex-wrap:wrap;">
                        <button class="btn-dark" style="padding:5px 10px;" onclick="openMenuModal(${m.id}, '${escapeHTML(m.name.replace(/'/g,"\\'"))}', ${m.price}, '${escapeHTML(m.category.replace(/'/g,"\\'"))}', '${escapeHTML(m.letter)}', ${oos})">Edit</button>
                        ${toggleBtn}
                    </td>
                </tr>`;
            }).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="4" class="error-state">Network Error</td></tr>'; }
    }

    function openMenuModal(id=null, name='', price='', cat='', letter='', outOfStock=false) {
        editMenuId = id;
        document.getElementById('menu-modal-title').innerText = id ? 'Edit Item' : 'Add Item';
        document.getElementById('menu-name').value = name;
        document.getElementById('menu-price').value = price;
        document.getElementById('menu-category').value = cat;
        document.getElementById('menu-letter').value = letter;
        document.getElementById('menu-out-of-stock').checked = outOfStock;
        document.getElementById('menu-modal').style.display = 'flex';
    }

    async function saveMenuItem(e) {
        e.preventDefault();
        const payload = { name: document.getElementById('menu-name').value, price: parseFloat(document.getElementById('menu-price').value), category: document.getElementById('menu-category').value, letter: document.getElementById('menu-letter').value, is_out_of_stock: document.getElementById('menu-out-of-stock').checked };
        const url = editMenuId ? `/api/menu/${editMenuId}` : '/api/menu';
        const method = editMenuId ? 'PUT' : 'POST';
        try {
            const res = await apiFetch(url, { method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) { showToast("Saved", "success"); document.getElementById('menu-modal').style.display='none'; fetchAdminMenu(); }
        } catch(e) { showToast("Error", "error"); }
    }

    async function toggleOutOfStock(id, newState) {
        try {
            const item = (await (await apiFetch('/api/menu')).json()).find(m => m.id === id);
            if (!item) return;
            const res = await apiFetch(`/api/menu/${id}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name: item.name, price: item.price, category: item.category, letter: item.letter, is_out_of_stock: newState })
            });
            if (res && res.ok) {
                showToast(newState ? "Marked as Out of Stock" : "Marked as In Stock", "success");
                fetchAdminMenu();
            }
        } catch(e) { showToast("Error updating stock status", "error"); }
    }

    async function addExpense() {
        const desc = document.getElementById('exp-desc').value;
        const amount = document.getElementById('exp-amount').value;
        if(!desc || !amount) return showToast("Fill all fields", "error");
        try {
            const res = await apiFetch('/api/expenses', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({description:desc, amount}) });
            if(res && res.ok) { showToast("Expense logged", "success"); fetchDailyFinances(); }
        } catch(e) {}
    }

    async function saveConfigurations() {
        const pin = document.getElementById('store-pin').value;
        if(!pin) { showToast("Enter your Master PIN", "error"); return; }
        const payload = { pin };
        try {
            const res = await apiFetch('/api/generate_link', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            const data = await res.json();
            if(res && res.ok) {
                document.getElementById('posLink').value = data.url;
                showToast("Permanent link generated! Pin this on your shop page.", "success");
            } else { showToast(data.error || "Error", "error"); }
        } catch(e) { showToast("Error", "error"); }
    }

    function copyPosLink() {
        const el = document.getElementById('posLink');
        if(!el.value) { showToast("Generate a link first", "error"); return; }
        navigator.clipboard.writeText(el.value)
            .then(() => showToast("Link copied to clipboard!", "success"))
            .catch(() => { el.select(); document.execCommand('copy'); showToast("Link copied!", "success"); });
    }

    function openInChrome() {
        const el = document.getElementById('posLink');
        if(!el.value) { showToast("Generate a link first", "error"); return; }
        const url = el.value;
        // Try to force Chrome using intent:// on Android, or chrome:// protocol handler on desktop
        const ua = navigator.userAgent.toLowerCase();
        if (/android/.test(ua)) {
            // Android: use Chrome intent
            window.location.href = 'intent://' + url.replace(/^https?:\/\//, '') + '#Intent;scheme=http;package=com.android.chrome;end';
        } else if (/iphone|ipad|ipod/.test(ua)) {
            // iOS: use googlechrome:// scheme
            const chromeUrl = url.replace(/^http:\/\//, 'googlechrome://').replace(/^https:\/\//, 'googlechromes://');
            window.location.href = chromeUrl;
            // Fallback to Safari after short delay if Chrome not installed
            setTimeout(() => { window.open(url, '_blank'); }, 1500);
        } else {
            // Desktop: just open in new tab (user sets Chrome as default, or copy and paste)
            window.open(url, '_blank');
            showToast("Link opened! Set Chrome as your default browser for best results.", "success");
        }
    }

    async function fetchStoreStatus() {
        try {
            const res = await fetch('/api/store/status');
            if(!res.ok) return;
            const s = await res.json();
            const el = document.getElementById('store-live-status');
            if(!el) return;
            if(s.open) {
                el.style.background = '#E8F5E9';
                el.innerHTML = `<span style="color:#2E7D32;">🟢 Store is <b>OPEN</b></span> &nbsp;— Orders close at <b>${s.cutoff_time}</b>`;
            } else if(s.closing_soon) {
                el.style.background = '#FFF3E0';
                el.innerHTML = `<span style="color:#E65100;">🟠 <b>Closing Soon</b></span> &nbsp;— No new orders after <b>${s.cutoff_time}</b>`;
            } else {
                el.style.background = '#FFEBEE';
                el.innerHTML = `<span style="color:#C62828;">🔴 Store is <b>CLOSED</b></span> &nbsp;— Next: <b>${s.next_open}</b>`;
            }
        } catch(e) {}
    }

    // Stub — slide clock elements removed from UI, kept as no-ops for safety
    function updateStoreClockDisplay(which) {}
    function adjustStoreClock(which, part, dir) {}

    setInterval(() => { if(document.getElementById('tab-kds').classList.contains('active')) fetchLiveOrders(); }, 5000);
    setInterval(fetchPermissionRequests, 5000);
    setInterval(fetchStoreStatus, 30000);
    fetchLiveOrders();
    fetchPermissionRequests();
    fetchStoreStatus();
    let knownPermCodes = new Set();
    let firstPermLoad = true;

    async function fetchPermissionRequests() {
        try {
            const res = await apiFetch('/api/permission_requests');
            if(!res || !res.ok) return;
            const data = await res.json();
            const tbody = document.getElementById('perm-requests-body');
            if(tbody) {
                if(data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:20px; color:#A67B5B;">No pending permission requests.</td></tr>';
                } else {
                    tbody.innerHTML = data.map(p => `
                        <tr id="perm-row-${p.id}">
                            <td style="font-size:0.8rem;">${escapeHTML(p.time)}</td>
                            <td><b>${escapeHTML(p.code)}</b></td>
                            <td><b>${escapeHTML(p.name)}</b><br><span style="font-size:0.75rem; color:#8D6E63;">${escapeHTML(p.address)}</span></td>
                            <td style="font-size:0.85rem;">${escapeHTML(p.message)}</td>
                            <td><button class="btn-blue" style="padding:6px 14px; margin-bottom:0;" onclick="grantPermission(${p.id}, '${escapeHTML(p.name)}', '${escapeHTML(p.code)}')">✅ Grant</button></td>
                        </tr>`).join('');
                }
            }
            // Notify admin of new requests
            if(!firstPermLoad) {
                data.forEach(p => {
                    if(!knownPermCodes.has(p.code)) {
                        playNotificationSound();
                        showToast(`🔔 Permission request from ${p.name} (${p.code})`, "error");
                        addAdminPermNotif(p);
                    }
                });
            }
            data.forEach(p => knownPermCodes.add(p.code));
            firstPermLoad = false;
        } catch(e) {}
    }

    function addAdminPermNotif(p) {
        adminNotifs.unshift({type:'perm', code: p.code, name: p.name, address: p.address, message: p.message, time: p.time});
        if(adminNotifs.length > 30) adminNotifs.pop();
        updateAdminNotifUI();
    }

    async function grantPermission(id, name, code) {
        try {
            const res = await apiFetch(`/api/permission_requests/${id}/grant`, {method:'POST'});
            if(res && res.ok) {
                showToast(`✅ Permission granted for ${name}`, "success");
                knownPermCodes.delete(code);
                fetchPermissionRequests();
            }
        } catch(e) { showToast("Error granting permission", "error"); }
    }

    // ── Backup & Recovery ─────────────────────────────────────────────────
    async function downloadBackup() {
        const status = document.getElementById('backup-status');
        status.style.color = '#8D6E63';
        status.innerText = 'Preparing backup...';
        try {
            const res = await apiFetch('/api/backup');
            if(!res || !res.ok) { status.style.color='#C62828'; status.innerText='Backup failed.'; return; }
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            const now = new Date();
            a.href = url;
            a.download = `9599_backup_${now.toISOString().slice(0,10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
            status.style.color = '#388E3C';
            status.innerText = '✅ Backup downloaded successfully.';
        } catch(e) { status.style.color='#C62828'; status.innerText='Network error during backup.'; }
    }

    async function restoreBackup(input) {
        const status = document.getElementById('backup-status');
        const file = input.files[0];
        if(!file) return;
        status.style.color = '#8D6E63';
        status.innerText = 'Reading backup file...';
        const reader = new FileReader();
        reader.onload = async (e) => {
            try {
                const payload = JSON.parse(e.target.result);
                status.innerText = 'Restoring... please wait.';
                const res = await apiFetch('/api/restore', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                if(res && res.ok) {
                    status.style.color = '#388E3C';
                    status.innerText = '✅ Restore complete! Refreshing...';
                    setTimeout(() => location.reload(), 1500);
                } else {
                    status.style.color = '#C62828';
                    status.innerText = '❌ Restore failed. Invalid backup file.';
                }
            } catch(err) {
                status.style.color = '#C62828';
                status.innerText = '❌ Invalid JSON file.';
            }
        };
        reader.readAsText(file);
        input.value = '';
    }
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
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
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
            f"<b>Mon – Fri:</b> 10:00 AM – 7:00 PM<br>"
            f"<b>Sat – Sun:</b> 3:00 PM – 8:00 PM<br><br>"
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

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if check_password_hash(ADMIN_PIN_HASH, pin):
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
        error = "Invalid PIN. Access Denied."
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
    if not check_password_hash(ADMIN_PIN_HASH, pin): return jsonify({"error": "Invalid PIN"}), 401
    # Permanent token — no times embedded, schedule is enforced server-side
    token = token_serializer.dumps({'store': '9599', 'v': 2})
    return jsonify({"url": f"{request.host_url}?token={token}"})

@app.route('/api/store/status')
def store_status_api():
    return jsonify(get_store_status())

@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    if request.method == 'GET':
        items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
        return jsonify([{"id": i.id, "name": i.name, "price": i.price, "letter": i.letter, "category": i.category, "stock": 0 if i.is_out_of_stock else 50, "is_out_of_stock": i.is_out_of_stock} for i in items])
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    if request.method == 'POST':
        data = request.json
        new_item = MenuItem(name=data['name'], price=float(data['price']), letter=data['letter'][:2].upper(), category=data['category'], is_out_of_stock=bool(data.get('is_out_of_stock', False)))
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
        ings = Ingredient.query.order_by(Ingredient.name).all()
        return jsonify([{"id": i.id, "name": i.name, "unit": i.unit, "stock": i.stock} for i in ings])
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
        # Log customer info separately
        clog = CustomerLog(full_name=data['name'], gmail=data.get('email',''), phone=data.get('phone',''), order_source='Online', order_total=data['total'])
        db.session.add(clog)
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
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    order = Reservation.query.get_or_404(order_id)
    order.status = request.json.get('status', 'Completed')
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/orders')
def api_orders():
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    res = Reservation.query.filter(Reservation.order_source != 'Legacy Notebook').order_by(Reservation.created_at.desc()).limit(50).all()
    return jsonify({'orders': [{'id': r.id, 'code': r.reservation_code, 'source': r.order_source, 'name': r.patron_name, 'total': r.total_investment, 'status': r.status, 'pickup_time': r.pickup_time, 'over_limit': len(r.infusions) > 5, 'items': [{'foundation': i.foundation, 'size': i.cup_size, 'addons': i.addons, 'sweetener': i.sweetener, 'ice': i.ice_level} for i in r.infusions]} for r in res]})

@app.route('/api/admin/manual_order', methods=['POST'])
def admin_manual_order():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    customer_name = data.get('customer_name', 'Walk-In')
    try:
        res = Reservation(patron_name=customer_name, patron_email="walkin@local", total_investment=data['total'], pickup_time="Walk-In", status="Waiting Confirmation", order_source="Manual/Walk-In")
        db.session.add(res)
        db.session.flush()
        for i in data['items']:
            inf = Infusion(reservation_id=res.id, foundation=i['foundation'], cup_size=i.get('size','16 oz'), sweetener=i.get('sugar','N/A'), ice_level=i.get('ice','N/A'), pearls='Walk-In', addons=i.get('addons',''), item_total=i['price'])
            db.session.add(inf)
        # Log customer info
        clog = CustomerLog(full_name=customer_name, gmail='Walk-In', phone='Walk-In', order_source='Manual/Walk-In', order_total=data['total'])
        db.session.add(clog)
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

@app.route('/api/expenses', methods=['POST'])
def add_expense():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    db.session.add(Expense(description=request.json['description'], amount=float(request.json['amount'])))
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/customer_logs', methods=['GET'])
def get_customer_logs():
    if not session.get('is_admin'): return jsonify([]), 403
    logs = CustomerLog.query.order_by(CustomerLog.created_at.desc()).limit(200).all()
    return jsonify([{"id": l.id, "name": l.full_name, "gmail": l.gmail, "phone": l.phone, "source": l.order_source, "total": l.order_total, "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')} for l in logs])

@app.route('/api/audit_logs', methods=['GET'])
def get_audit_logs():
    if not session.get('is_admin'): return jsonify([]), 403
    return jsonify([{"action": l.action, "details": l.details, "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')} for l in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()])

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
            'French Fries (Plain)', 'French Fries (Cheese)', 'French Fries (BBQ)',
            'Hash Brown', 'Onion Rings', 'Potato Mojos'
        ]
        MenuItem.query.filter(MenuItem.name.notin_(valid_names)).delete(synchronize_session=False)
        db.session.commit()

        # ── 2. Seed ingredients ──────────────────────────────────────────────
        ingredients_data = [
            ('Assam Black Tea', 'ml', 10000.0), ('Jasmine Green Tea', 'ml', 10000.0),
            ('Fresh Milk', 'ml', 8000.0), ('Non-Dairy Creamer', 'grams', 5000.0),
            ('Tapioca Pearls', 'grams', 3000.0), ('Brown Sugar Syrup', 'ml', 4000.0),
            ('Wintermelon Syrup', 'ml', 2000.0), ('Okinawa Syrup', 'ml', 2000.0),
            ('Cookies & Cream Powder', 'grams', 2000.0), ('Matcha Powder', 'grams', 1000.0),
            ('Dark Choco Powder', 'grams', 2000.0), ('Taro Paste', 'grams', 1500.0),
            ('Strawberry Syrup', 'ml', 2000.0), ('Lychee Syrup', 'ml', 2000.0),
            ('Plastic Cups & Lids', 'pcs', 500.0), ('Hash Brown (pcs)', 'pcs', 100.0),
            ('French Fries', 'grams', 5000.0), ('Onion Rings', 'grams', 3000.0),
            ('Potato Mojos', 'grams', 3000.0), ('Snack Packaging', 'pcs', 500.0),
            ('Cooking Oil', 'ml', 10000.0), ('Caramel Syrup', 'ml', 2000.0),
            ('Frappe Base', 'grams', 2000.0), ('Nata', 'grams', 3000.0),
            ('Coffee Jelly', 'grams', 3000.0), ('Espresso Shot', 'ml', 2000.0),
            ('Biscoff Crumbs', 'grams', 1000.0), ('Mocha Syrup', 'ml', 2000.0),
            ('French Vanilla Syrup', 'ml', 2000.0), ('Hazelnut Syrup', 'ml', 2000.0),
            ('Ube Syrup', 'ml', 2000.0), ('Mango Puree', 'grams', 2000.0),
            ('Blueberry Syrup', 'ml', 2000.0), ('Apple Syrup', 'ml', 2000.0),
            ('Soda Water', 'ml', 10000.0),
        ]
        for name, unit, stock in ingredients_data:
            existing = Ingredient.query.filter_by(name=name).first()
            if not existing:
                db.session.add(Ingredient(name=name, unit=unit, stock=stock))
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
            ('Matcha Frappe', 79.00, 'MF', 'Frappe'),
            ('Mango Frappe', 79.00, 'MG', 'Frappe'),
            # Snacks
            ('French Fries (Plain)', 39.00, 'F', 'Snacks'),
            ('French Fries (Cheese)', 39.00, 'F', 'Snacks'),
            ('French Fries (BBQ)', 39.00, 'F', 'Snacks'),
            ('Hash Brown', 29.00, 'H', 'Snacks'),
            ('Onion Rings', 59.00, 'O', 'Snacks'),
            ('Potato Mojos', 59.00, 'P', 'Snacks'),
        ]
        for name, price, letter, category in menu_data:
            if not MenuItem.query.filter_by(name=name).first():
                db.session.add(MenuItem(name=name, price=price, letter=letter, category=category))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ── 4. Seed recipes ──────────────────────────────────────────────────
        recipe_data = [
            # Snacks
            ('Hash Brown', 'Hash Brown (pcs)', 1), ('Hash Brown', 'Snack Packaging', 1), ('Hash Brown', 'Cooking Oil', 20),
            ('French Fries (Plain)', 'French Fries', 150), ('French Fries (Plain)', 'Snack Packaging', 1), ('French Fries (Plain)', 'Cooking Oil', 50),
            ('French Fries (Cheese)', 'French Fries', 150), ('French Fries (Cheese)', 'Snack Packaging', 1), ('French Fries (Cheese)', 'Cooking Oil', 50),
            ('French Fries (BBQ)', 'French Fries', 150), ('French Fries (BBQ)', 'Snack Packaging', 1), ('French Fries (BBQ)', 'Cooking Oil', 50),
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