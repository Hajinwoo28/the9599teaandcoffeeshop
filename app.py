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
    pearls = db.Column(db.String(100), nullable=False)
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

class Ingredient(db.Model):
    __tablename__ = 'ingredients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    unit = db.Column(db.String(20), nullable=False)
    stock = db.Column(db.Float, nullable=False, default=0.0)

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

        @media (max-width: 768px) {
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
    <div class="notif-container">
        <div class="notif-bell" onclick="alert('Notification center ready.')">
            <i class="fas fa-bell"></i><span class="notif-badge" id="notif-badge">0</span>
        </div>
    </div>
</header>

<!-- Main Layout -->
<div class="main-container">
    
    <!-- Menu Area -->
    <div class="menu-area">
        <div class="categories" id="categories-container">
            <button class="cat-btn active" onclick="filterMenu('All', this)">☕ All</button>
            <button class="cat-btn" onclick="filterMenu('Trending Now', this)">🔥 Trending</button>
            <button class="cat-btn" onclick="filterMenu('Signature Series', this)">⭐ Signature</button>
            <button class="cat-btn" onclick="filterMenu('Matcha & Taro', this)">🌿 Matcha & Taro</button>
            <button class="cat-btn" onclick="filterMenu('Fruit Soda', this)">🍹 Fruit Soda</button>
            <button class="cat-btn" onclick="filterMenu('Fruit Infusions', this)">🍓 Fruit Infusions</button>
            <button class="cat-btn" onclick="filterMenu('Milk Series', this)">🥛 Milk Series</button>
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

            <input type="text" class="name-input" id="customer-name" placeholder="Your Name" value="{{ session.get('customer_name', '') }}" oninput="checkCheckoutStatus()">

            <label class="pickup-label">Pick-up Time *</label>
            <div class="time-wrapper">
                <input type="text" class="name-input" id="pickup-time" placeholder="e.g. 2:30 PM" oninput="checkCheckoutStatus()">
                <i class="far fa-clock"></i>
            </div>
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
    <div class="modal-content" style="text-align:center;">
        <h2 style="margin-bottom:10px;">Order Placed!</h2>
        <p style="color:var(--text-light); font-weight:600; font-size:0.9rem;">Your order is being prepared. Show this code to the cashier.</p>
        <div id="display-code" style="font-family:'Playfair Display',serif; font-size: 2.5rem; font-weight: 900; color:var(--gold); margin: 20px 0; border: 2px dashed var(--gold); padding: 15px; border-radius: 12px; background: var(--gold-light); letter-spacing: 4px;"></div>
        <button class="btn-add" style="width:100%; padding:18px;" onclick="location.reload()">Done, Thanks!</button>
    </div>
</div>

<!-- Order Limit Modal -->
<div id="perm-modal" class="modal">
    <div class="modal-content" style="text-align:center;">
        <h2 style="color:var(--danger);">Limit Reached</h2>
        <p style="color:var(--text-light); font-size:0.9rem; font-weight:600; margin-bottom:15px;">You can only order up to 5 milktea drinks at once. Ask the admin to approve this bulk order.</p>
        <div id="perm-request-code" style="font-family:'Playfair Display',serif; font-size: 1.5rem; font-weight: 900; color:var(--text-dark); margin: 15px 0; border: 2px dashed var(--border-color); padding: 10px; border-radius: 12px;"></div>
        
        <input type="text" id="perm-address-input" class="name-input" placeholder="Your Address / Location">
        <textarea id="perm-message-input" class="name-input" rows="2" placeholder="Message to admin (e.g. Office order)"></textarea>
        
        <div id="perm-send-status" style="font-size:0.85rem; font-weight:700; color:var(--badge-new); margin-bottom:10px;"></div>

        <div style="display:flex; gap:10px;">
            <button class="btn-cancel" onclick="document.getElementById('perm-modal').style.display='none'">Cancel</button>
            <button class="btn-add" id="perm-send-btn" onclick="sendPermissionRequest()">Send Request</button>
            <button class="btn-add" id="perm-place-btn" style="display:none;" onclick="submitOrderWithOverride()">Place Order</button>
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
        'Green Apple Soda': '/static/images/green_apple_soda.jpg',
        'Blueberry Soda': '/static/images/blueberry_soda.jpg',
        'Lychee Mogu Soda': '/static/images/lychee_mogu_soda.jpg',
        'Strawberry Soda': '/static/images/strawberry_soda.jpg',
        'Matcha Caramel': '/static/images/matcha_caramel.jpg',
        'Matcha Frappe': '/static/images/matcha_frappe.jpg',
        'Matcha Latte': '/static/images/matcha_latte.jpg',
        'Blueberry Milk': '/static/images/blueberry_milk.jpg',
        'Hazelnut Milk': '/static/images/hazelnut_milk.jpg',
        'Mango Milk': '/static/images/mango_milk.jpg',
        'Strawberry Milk': '/static/images/strawberry_milk.jpg',
        'Ube Milk': '/static/images/ube_milk.jpg'
    };

    const EMOJI_MAP = {
        'Dirty Matcha': { em: '🔥', grad: 'grad-trending', badge: 'bestseller' },
        'Biscoff Frappe': { em: '☕', grad: 'grad-trending', badge: 'bestseller' },
        'Midnight Velvet': { em: '⭐', grad: 'grad-signature', badge: 'bestseller' },
        'Taro Symphony': { em: '🌿', grad: 'grad-matcha', badge: 'new' },
        'Strawberry Lychee': { em: '🍓', grad: 'grad-fruit', badge: 'none' },
        'French Fries': { em: '🍟', grad: 'grad-snacks', badge: 'none' }
    };

    function getCardStyle(item) {
        if(EMOJI_MAP[item.name]) return EMOJI_MAP[item.name];
        if(item.category.includes('Matcha')) return { em: '🍵', grad: 'grad-matcha', badge: 'none' };
        if(item.category.includes('Fruit Soda') || item.category.includes('Fruit Infusions')) return { em: '🍓', grad: 'grad-fruit', badge: 'none' };
        if(item.category.includes('Signature')) return { em: '✨', grad: 'grad-signature', badge: 'none' };
        if(item.category.includes('Snack')) return { em: '🍟', grad: 'grad-snacks', badge: 'none' };
        if(item.category.includes('Milk')) return { em: '🥛', grad: 'grad-default', badge: 'none' };
        return { em: '🧋', grad: 'grad-default', badge: 'none' };
    }
    
    document.addEventListener("DOMContentLoaded", () => { 
        fetchMenu(); 
        setInterval(pollCustomerOrderStatus, 3000); 
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
            renderMenu('All');
        } catch(e) { document.getElementById('menu-grid').innerHTML = '<div style="padding:20px; text-align:center;">Error loading menu.</div>'; }
    }

    function renderMenu(cat) {
        const grid = document.getElementById('menu-grid');
        grid.innerHTML = '';
        let filtered = cat === 'All' ? menuItems : menuItems.filter(i => i.category === cat);
        
        filtered.forEach(item => {
            const isSoldOut = item.stock <= 0;
            const priceDisplay = ['Snacks','Frappes','Fruit Soda','Milk Series'].includes(item.category) ? `₱${item.price.toFixed(0)}` : '₱49 / ₱59';
            const style = getCardStyle(item);
            
            let badgeHTML = '';
            if (isSoldOut) badgeHTML = '<div class="sold-out-badge">SOLD OUT</div>';
            else if (style.badge === 'bestseller') badgeHTML = '<div class="badge-bestseller">⭐ BEST SELLER</div>';
            else if (style.badge === 'new') badgeHTML = '<div class="badge-new">✨ FAN FAVE</div>';

            let imageContent = `<div class="card-emoji">${style.em}</div>`;
            if (IMAGE_MAP[item.name]) {
                imageContent = `<img src="${IMAGE_MAP[item.name]}" class="card-real-img" onerror="this.style.display='none'">`;
            }

            grid.innerHTML += `
                <div class="card ${isSoldOut?'sold-out':''}" onclick="${isSoldOut?'':`addToCart('${escapeHTML(item.name).replace(/'/g,"\\'")}', '${escapeHTML(item.category).replace(/'/g,"\\'")}', ${item.price})`}">
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
        renderMenu(cat);
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

        if (name === 'French Fries') {
            document.getElementById('fries-modal').style.display = 'flex';
        } else if (['Snacks','Frappes','Fruit Soda','Milk Series'].includes(cat)) {
            cart.push({name, cat, size:'Regular', sugar:'N/A', ice:'N/A', addons:[], price});
            updateCartUI();
        } else {
            document.getElementById('size-modal-title').innerText = name;
            selectSize('16 oz', 49);
            document.querySelectorAll('.addon-checkbox').forEach(cb => cb.checked = false);
            document.getElementById('size-modal').style.display = 'flex';
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
        const p = document.getElementById('pickup-time').value.trim();
        const btn = document.getElementById('checkout-btn');
        if(cart.length > 0 && n && p) { btn.className = 'checkout-btn active'; btn.disabled = false; }
        else { btn.className = 'checkout-btn'; btn.disabled = true; }
    }

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

    async function sendPermissionRequest() {
        const payload = {
            name: document.getElementById('customer-name').value,
            address: document.getElementById('perm-address-input').value,
            message: document.getElementById('perm-message-input').value,
            code: document.getElementById('perm-request-code').innerText
        };
        const btn = document.getElementById('perm-send-btn');
        const stat = document.getElementById('perm-send-status');
        btn.disabled = true; btn.innerText = "Sending...";
        try {
            const res = await fetch('/api/permission_request', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
            if(res.ok) { stat.innerText = "Sent! Waiting for approval..."; permPoll = setInterval(()=>checkPermissionStatus(payload.name), 3000); }
            else stat.innerText = "Error sending request.";
        } catch(e) { stat.innerText = "Network Error."; btn.disabled = false; btn.innerText = "Send Request"; }
    }

    async function checkPermissionStatus(name) {
        try {
            const res = await fetch(`/api/permission_status?name=${encodeURIComponent(name)}`);
            const data = await res.json();
            if(data.granted) {
                permissionGranted = true;
                document.getElementById('perm-send-btn').style.display = 'none';
                document.getElementById('perm-place-btn').style.display = 'block';
                document.getElementById('perm-send-status').innerText = "✅ Permission Granted! You can order now.";
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

        const milkteas = cart.filter(c => !['Snacks'].includes(c.cat)).length;
        if(milkteas > 5 && !permissionGranted) {
            document.getElementById('alert-audio').play().catch(()=>{});
            document.getElementById('perm-request-code').innerText = "REQ-" + Math.floor(Math.random()*10000);
            document.getElementById('perm-modal').style.display = 'flex';
            return;
        }

        const btn = document.getElementById('checkout-btn');
        btn.innerHTML = 'Processing...'; btn.disabled = true;
        
        const payload = {
            name: document.getElementById('customer-name').value,
            email: "{{ session.get('customer_email', 'local@9599') }}",
            pickup_time: tStr,
            total: cart.reduce((s,i)=>s+i.price, 0),
            items: cart.map(i => ({ foundation: i.name, size: i.size, sweetener: i.sugar, ice: i.ice, addons: i.addons.join(', '), pearls: orderType, price: i.price }))
        };
        try {
            const res = await fetch('/reserve', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            const data = await res.json();
            if(res.ok) {
                document.getElementById('display-code').innerText = data.reservation_code;
                document.getElementById('success-modal').style.display = 'flex';
                let orders = JSON.parse(localStorage.getItem('myOrders')) || [];
                orders.push({code: data.reservation_code, status: 'Waiting Confirmation'});
                localStorage.setItem('myOrders', JSON.stringify(orders));
            } else { showToast("Error: " + data.message, "error"); btn.innerHTML = 'Place Order'; btn.disabled = false; }
        } catch(e) { showToast("Connection Error", "error"); btn.innerHTML = 'Place Order'; btn.disabled = false; }
    }

    async function submitOrderWithOverride() {
        document.getElementById('perm-modal').style.display = 'none';
        if(permPoll) clearInterval(permPoll);
        await submitOrder();
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
        .settings-grid-layout { display: grid; grid-template-columns: 340px 1fr; gap: 20px; height: 100%; }
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
            <div class="finance-grid">
                <div style="display:flex; flex-direction:column; overflow-y:auto;">
                    <div class="settings-card">
                        <div class="card-title">Daily Close-Out</div>
                        <div style="display:flex; justify-content:space-between; margin-bottom:10px;"><b>System Total</b> <span id="sys-total">₱0.00</span></div>
                        <div style="display:flex; justify-content:space-between; margin-bottom:10px; color:#C62828;"><b>Expenses</b> <span id="expense-total">- ₱0.00</span></div>
                        <div style="display:flex; justify-content:space-between; font-size:1.2rem; font-weight:800; margin-top:10px; border-top:1px dashed #D7CCC8; padding-top:10px;"><b>Net Profit</b> <span id="cash-drawer">₱0.00</span></div>
                        <button class="btn-dark" style="margin-top:15px;" onclick="fetchDailyFinances()">Refresh Data</button>
                    </div>
                    <div class="settings-card">
                        <div class="card-title">Top Sellers</div>
                        <div id="best-sellers-list"></div>
                    </div>
                </div>
                <div style="display:flex; flex-direction:column; overflow-y:auto;">
                    <div class="settings-card">
                        <div class="card-title">Log Expense</div>
                        <input type="text" id="exp-desc" class="input-pin" placeholder="Description (e.g. Ice)">
                        <input type="number" id="exp-amount" class="input-pin" placeholder="Amount (₱)">
                        <button class="btn-blue" onclick="addExpense()">Record Expense</button>
                    </div>
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
                <div style="display:flex; flex-direction:column; overflow-y:auto;">
                    <div class="settings-card">
                        <div class="card-title">Store Status & Link</div>
                        <input type="time" id="store-open" class="input-pin">
                        <input type="time" id="store-close" class="input-pin">
                        <input type="password" id="store-pin" class="input-pin" placeholder="Master PIN">
                        <button class="btn-blue" onclick="saveConfigurations()">Generate Customer Link</button>
                        <input type="text" id="posLink" class="input-pin" style="background:#F5EFE6; margin-top:10px;" readonly>
                    </div>
                </div>
                <div class="settings-card">
                    <div style="display:flex; justify-content:space-between;">
                        <div class="card-title">Menu Management</div>
                        <button class="btn-dark" onclick="openMenuModal()">Add Item</button>
                    </div>
                    <div class="table-responsive">
                        <table class="kds-table">
                            <thead><tr><th>Name</th><th>Category</th><th>Price</th><th>Action</th></tr></thead>
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
        <select id="qo-sugar" class="input-pin" style="padding:10px;"><option value="100% Sugar">100% Sugar</option><option value="50% Sugar">50% Sugar</option></select>
        <select id="qo-ice" class="input-pin" style="padding:10px;"><option value="Normal Ice">Normal Ice</option><option value="Less Ice">Less Ice</option></select>
        <div style="display:flex; gap:10px; margin-top:20px;">
            <button class="btn-dark" style="flex:1;" onclick="document.getElementById('qo-size-modal').style.display='none'">Cancel</button>
            <button class="btn-blue" style="flex:2; margin-bottom:0;" onclick="confirmQuickCart()">Add</button>
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
        if(id==='finance') fetchDailyFinances();
        if(id==='audit') fetchAuditLogs();
        if(id==='settings') fetchAdminMenu();
    }

    function escapeHTML(str) { 
        let div = document.createElement('div'); 
        div.innerText = str; 
        return div.innerHTML; 
    }

    const ADMIN_IMAGE_MAP = {
        'Green Apple Soda': '/static/images/green_apple_soda.jpg',
        'Blueberry Soda': '/static/images/blueberry_soda.jpg',
        'Lychee Mogu Soda': '/static/images/lychee_mogu_soda.jpg',
        'Strawberry Soda': '/static/images/strawberry_soda.jpg',
        'Matcha Caramel': '/static/images/matcha_caramel.jpg',
        'Matcha Frappe': '/static/images/matcha_frappe.jpg',
        'Matcha Latte': '/static/images/matcha_latte.jpg',
        'Blueberry Milk': '/static/images/blueberry_milk.jpg',
        'Hazelnut Milk': '/static/images/hazelnut_milk.jpg',
        'Mango Milk': '/static/images/mango_milk.jpg',
        'Strawberry Milk': '/static/images/strawberry_milk.jpg',
        'Ube Milk': '/static/images/ube_milk.jpg'
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
            body.innerHTML = adminNotifs.map(n => `
                <div class="notif-item">
                    <b>Order #${n.code}</b> from ${n.name}<br>
                    Amount: ₱${n.total.toFixed(2)} <span style="float:right; color:#A38C7D; font-size:0.75rem;">${n.time}</span>
                </div>
            `).join('');
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
                let items = o.items.map(i => `
                    <div style="display:flex; align-items:center; margin-bottom:5px;">
                        ${getAdminPhotoHtml(i.foundation.replace(/ \(.+\)$/, ''))}
                        <div><b>${escapeHTML(i.foundation)}</b> (${escapeHTML(i.size)})<br><span style="font-size:0.75rem; color:#8D6E63;">${escapeHTML(i.sweetener)} | ${escapeHTML(i.ice)}${i.addons ? ' | + ' + escapeHTML(i.addons) : ''}</span></div>
                    </div>
                `).join('');
                
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
        grid.innerHTML = items.map(i => `
            <div style="background:white; border:1px solid #D7CCC8; padding:15px; border-radius:10px; cursor:pointer;" onclick="addQO('${escapeHTML(i.name.replace(/'/g, "\\'"))}', '${escapeHTML(i.category.replace(/'/g, "\\'"))}', ${i.price})">
                <div style="font-weight:bold;">${escapeHTML(i.name)}</div>
                <div style="font-size:0.8rem; color:#A67B5B;">₱${i.price}</div>
            </div>
        `).join('');
    }

    function filterQuickOrderMenu() {
        const q = document.getElementById('qo-search').value.toLowerCase();
        renderQOMenu(adminMenu.filter(i => i.name.toLowerCase().includes(q)));
    }

    let qoCart = [];
    let qoPending = {};
    function addQO(name, cat, price) {
        if(name === 'French Fries') {
            qoPending = {name, price};
            document.getElementById('qo-fries-modal').style.display = 'flex';
        } else if(['Snacks','Frappes','Fruit Soda','Milk Series'].includes(cat)) {
            qoCart.push({name, size:'Regular', price});
            renderQOCart();
        } else {
            qoPending = {name};
            document.getElementById('qo-size-modal-title').innerText = name;
            selectQOSize('16 oz', 49);
            document.getElementById('qo-size-modal').style.display = 'flex';
        }
    }

    function confirmQOFries() {
        let flavor = document.querySelector('input[name="qo_fries_flavor"]:checked').value;
        qoCart.push({name: `${qoPending.name} (${flavor})`, size: 'Regular', price: qoPending.price});
        document.getElementById('qo-fries-modal').style.display = 'none';
        renderQOCart();
    }

    function selectQOSize(size, price) {
        qoPending.size = size; qoPending.price = price;
        document.getElementById('btn-qo-16').style.background = size==='16 oz' ? '#6F4E37' : 'white';
        document.getElementById('btn-qo-16').style.color = size==='16 oz' ? 'white' : '#3E2723';
        document.getElementById('btn-qo-22').style.background = size==='22 oz' ? '#388E3C' : 'white';
        document.getElementById('btn-qo-22').style.color = size==='22 oz' ? 'white' : '#3E2723';
    }

    function confirmQuickCart() {
        qoCart.push({
            name: qoPending.name, size: qoPending.size, price: qoPending.price,
            sugar: document.getElementById('qo-sugar').value, ice: document.getElementById('qo-ice').value
        });
        document.getElementById('qo-size-modal').style.display = 'none';
        renderQOCart();
    }

    function renderQOCart() {
        const list = document.getElementById('qo-cart-items');
        let t = 0; list.innerHTML = '';
        qoCart.forEach((c, i) => {
            t += c.price;
            let sub = c.size==='Regular' ? '' : ` (${c.size})`;
            list.innerHTML += `<div style="display:flex; justify-content:space-between; margin-bottom:10px;">
                <div><b>${escapeHTML(c.name)}</b>${escapeHTML(sub)}</div>
                <div>₱${c.price} <i class="fas fa-times" style="color:red; cursor:pointer;" onclick="qoCart.splice(${i},1); renderQOCart();"></i></div>
            </div>`;
        });
        document.getElementById('qo-total-display').innerText = `₱${t.toFixed(2)}`;
    }

    async function submitQuickOrder() {
        if(qoCart.length===0) return showToast("Cart empty", "error");
        const payload = { items: qoCart.map(c=>({foundation:c.name, size:c.size, price:c.price})), total: qoCart.reduce((s,c)=>s+c.price,0) };
        try {
            const res = await apiFetch('/api/admin/manual_order', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) { showToast("Saved!", "success"); qoCart=[]; renderQOCart(); fetchLiveOrders(); }
        } catch(e) { showToast("Error saving order", "error"); }
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
            tbody.innerHTML = data.map(m => `<tr><td><b>${escapeHTML(m.name)}</b></td><td>${escapeHTML(m.category)}</td><td>₱${m.price}</td><td><button class="btn-dark" style="padding:5px 10px;" onclick="openMenuModal(${m.id}, '${escapeHTML(m.name.replace(/'/g,"\\'"))}', ${m.price}, '${escapeHTML(m.category.replace(/'/g,"\\'"))}', '${escapeHTML(m.letter)}')">Edit</button></td></tr>`).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="4" class="error-state">Network Error</td></tr>'; }
    }

    function openMenuModal(id=null, name='', price='', cat='', letter='') {
        editMenuId = id;
        document.getElementById('menu-modal-title').innerText = id ? 'Edit Item' : 'Add Item';
        document.getElementById('menu-name').value = name;
        document.getElementById('menu-price').value = price;
        document.getElementById('menu-category').value = cat;
        document.getElementById('menu-letter').value = letter;
        document.getElementById('menu-modal').style.display = 'flex';
    }

    async function saveMenuItem(e) {
        e.preventDefault();
        const payload = { name: document.getElementById('menu-name').value, price: parseFloat(document.getElementById('menu-price').value), category: document.getElementById('menu-category').value, letter: document.getElementById('menu-letter').value };
        const url = editMenuId ? `/api/menu/${editMenuId}` : '/api/menu';
        const method = editMenuId ? 'PUT' : 'POST';
        try {
            const res = await apiFetch(url, { method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) { showToast("Saved", "success"); document.getElementById('menu-modal').style.display='none'; fetchAdminMenu(); }
        } catch(e) { showToast("Error", "error"); }
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
        const payload = { open: document.getElementById('store-open').value, close: document.getElementById('store-close').value, pin: document.getElementById('store-pin').value };
        try {
            const res = await apiFetch('/api/generate_link', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            const data = await res.json();
            if(res && res.ok) { document.getElementById('posLink').value = data.url; showToast("Link Generated", "success"); }
            else showToast(data.error, "error");
        } catch(e) { showToast("Error", "error"); }
    }

    setInterval(() => { if(document.getElementById('tab-kds').classList.contains('active')) fetchLiveOrders(); }, 5000);
    fetchLiveOrders();
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
    
    blocked_html = """
    <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5EFE6; flex-direction:column; text-align:center; padding: 20px; font-family: 'DM Sans', sans-serif;">
        <i class="fas fa-store-slash" style="font-size:4rem; color:#D7CCC8; margin-bottom:20px;"></i>
        <h2 style="color:#3E2723; margin-bottom: 10px;">Store Not Configured</h2>
        <p style="color:#8D6E63;">You cannot access this site directly. Please scan the official ordering QR code provided by the cashier.</p>
    </div>
    """
    
    if not token:
        return blocked_html, 403
        
    try:
        data = token_serializer.loads(token) 
        open_time = data.get('open', '06:00 AM')
        close_time = data.get('close', '07:00 PM')
    except (SignatureExpired, BadSignature):
        return blocked_html, 403
        
    return render_template_string(STOREFRONT_HTML, open_time=open_time, close_time=close_time, google_client_id=GOOGLE_CLIENT_ID)

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
    token = token_serializer.dumps({'open': data['open'], 'close': data['close']})
    return jsonify({"url": f"{request.host_url}?token={token}"})

@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    if request.method == 'GET':
        items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
        return jsonify([{"id": i.id, "name": i.name, "price": i.price, "letter": i.letter, "category": i.category, "stock": 50} for i in items])
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    if request.method == 'POST':
        data = request.json
        new_item = MenuItem(name=data['name'], price=float(data['price']), letter=data['letter'][:2].upper(), category=data['category'])
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
        new_res = Reservation(patron_name=data['name'], patron_email=data['email'], total_investment=data['total'], pickup_time=data['pickup_time'], order_source="Online", status="Waiting Confirmation")
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
    try:
        res = Reservation(patron_name="Walk-In", patron_email="local", total_investment=data['total'], pickup_time="Walk-In", status="Completed", order_source="Manual/Notebook")
        db.session.add(res)
        db.session.flush()
        for i in data['items']:
            inf = Infusion(reservation_id=res.id, foundation=i['foundation'], cup_size=i.get('size','16 oz'), item_total=i['price'])
            db.session.add(inf)
        db.session.commit()
        return jsonify({"status": "success"})
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
        ingredients_data =[{'name': 'Fresh Milk', 'unit': 'ml', 'stock': 8000.0}, {'name': 'Tapioca Pearls', 'unit': 'grams', 'stock': 3000.0}]
        for i_data in ingredients_data:
            if not Ingredient.query.filter_by(name=i_data['name']).first(): db.session.add(Ingredient(**i_data))
        
        menu_data =[
            {"name": "Dirty Matcha", "price": 49.00, "letter": "D", "category": "Trending Now"},
            {"name": "Biscoff Frappe", "price": 84.00, "letter": "B", "category": "Trending Now"},
            {"name": "Midnight Velvet", "price": 49.00, "letter": "M", "category": "Signature Series"},
            {"name": "Taro Symphony", "price": 49.00, "letter": "T", "category": "Matcha & Taro"},
            {"name": "Strawberry Lychee", "price": 49.00, "letter": "S", "category": "Fruit Infusions"},
            {"name": "French Fries", "price": 39.00, "letter": "F", "category": "Snacks"},
            {"name": "Green Apple Soda", "price": 49.00, "letter": "G", "category": "Fruit Soda"},
            {"name": "Blueberry Soda", "price": 49.00, "letter": "B", "category": "Fruit Soda"},
            {"name": "Lychee Mogu Soda", "price": 49.00, "letter": "L", "category": "Fruit Soda"},
            {"name": "Strawberry Soda", "price": 49.00, "letter": "S", "category": "Fruit Soda"},
            {"name": "Matcha Caramel", "price": 59.00, "letter": "M", "category": "Matcha & Taro"},
            {"name": "Matcha Frappe", "price": 84.00, "letter": "M", "category": "Matcha & Taro"},
            {"name": "Matcha Latte", "price": 59.00, "letter": "M", "category": "Matcha & Taro"},
            {"name": "Blueberry Milk", "price": 59.00, "letter": "B", "category": "Milk Series"},
            {"name": "Hazelnut Milk", "price": 59.00, "letter": "H", "category": "Milk Series"},
            {"name": "Mango Milk", "price": 59.00, "letter": "M", "category": "Milk Series"},
            {"name": "Strawberry Milk", "price": 59.00, "letter": "S", "category": "Milk Series"},
            {"name": "Ube Milk", "price": 59.00, "letter": "U", "category": "Milk Series"}
        ]
        for m_data in menu_data:
            if not MenuItem.query.filter_by(name=m_data['name']).first(): db.session.add(MenuItem(**m_data))
        
        db.session.commit()
    except Exception as e:
        print(f"DB Init Error: {e}")

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
            webbrowser.open_new('http://127.0.0.1:5000/login')

        print("==================================================")
        print(" STARTING SYSTEM (DESKTOP APP MODE)")
        print(f" CUSTOMER POS LINK: http://{get_local_ip()}:5000/")
        print(" ADMIN DASHBOARD:   http://127.0.0.1:5000/login")
        print("==================================================")
        
        Timer(1.5, open_browser).start()
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)