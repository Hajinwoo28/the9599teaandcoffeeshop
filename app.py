
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
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

if os.environ.get('RENDER'):
    app.config['SESSION_COOKIE_SECURE'] = True 

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
    """
    Prevent the browser from caching API data.
    Ensures that the POS system always displays the most up-to-date
    inventory and order statuses.
    """
    if 'api' in request.path:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response

# ==========================================
# 2. CLOUD & LOCAL CONFIGURATION
# ==========================================

database_url = os.environ.get('DATABASE_URL', 'postgresql://postgres:12345@localhost/milktea_system')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

def get_local_ip():
    """
    Attempts to retrieve the local IP address of the machine running the server.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_ph_time():
    """
    Adjusts the UTC time to Philippine Standard Time (UTC+8).
    """
    return datetime.utcnow() + timedelta(hours=8)

def log_audit(action, details=""):
    """
    TRANSPARENCY FEATURE: Core logging function.
    Records every critical action into the database for the owner to review.
    """
    try:
        new_log = AuditLog(
            action=action,
            details=details
        )
        db.session.add(new_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Audit Log Failed: {str(e)}")

# ==========================================
# 3. DATABASE MODELS
# ==========================================

class Reservation(db.Model):
    """
    Represents a customer's total order (reservation).
    """
    __tablename__ = 'reservations'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    reservation_code = db.Column(
        db.String(8), 
        unique=True, 
        nullable=False, 
        default=lambda: str(uuid.uuid4())[:8].upper()
    )
    patron_name = db.Column(
        db.String(100), 
        nullable=False
    )
    patron_email = db.Column(
        db.String(120), 
        nullable=False
    )
    total_investment = db.Column(
        db.Float, 
        nullable=False
    )
    status = db.Column(
        db.String(50), 
        default='Preparing Order'
    )
    pickup_time = db.Column(
        db.String(50), 
        nullable=False
    )
    order_source = db.Column(
        db.String(30), 
        default='Online'
    ) 
    created_at = db.Column(
        db.DateTime, 
        default=get_ph_time
    )
    infusions = db.relationship(
        'Infusion', 
        backref='reservation', 
        lazy=True, 
        cascade="all, delete-orphan"
    )

class Infusion(db.Model):
    """
    Represents an individual drink (item) within a customer's reservation.
    Includes the cup_size, sugar level, ice level, and addons.
    """
    __tablename__ = 'infusions'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    reservation_id = db.Column(
        db.Integer, 
        db.ForeignKey('reservations.id'), 
        nullable=False
    )
    foundation = db.Column(
        db.String(100), 
        nullable=False
    )
    sweetener = db.Column(
        db.String(100), 
        nullable=False,
        default='100% Sugar'
    )
    ice_level = db.Column(
        db.String(50), 
        nullable=False,
        default='Normal Ice'
    )
    pearls = db.Column(
        db.String(100), 
        nullable=False
    )
    cup_size = db.Column(
        db.String(20), 
        nullable=False, 
        default='16 oz'
    )
    addons = db.Column(
        db.String(200), 
        nullable=False, 
        default=''
    )
    item_total = db.Column(
        db.Float, 
        nullable=False, 
        default=0.0
    )

class MenuItem(db.Model):
    """
    Represents a drink available on the menu.
    """
    __tablename__ = 'menu_items'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    name = db.Column(
        db.String(100), 
        nullable=False
    )
    price = db.Column(
        db.Float, 
        nullable=False
    )
    letter = db.Column(
        db.String(2), 
        nullable=False
    )
    category = db.Column(
        db.String(50), 
        nullable=False
    )

class Ingredient(db.Model):
    """
    Represents raw materials in the shop (e.g., Milk, Tea, Cups).
    """
    __tablename__ = 'ingredients'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    name = db.Column(
        db.String(100), 
        nullable=False, 
        unique=True
    )
    unit = db.Column(
        db.String(20), 
        nullable=False
    )
    stock = db.Column(
        db.Float, 
        nullable=False, 
        default=0.0
    )

class RecipeItem(db.Model):
    """
    Maps an ingredient to a specific menu item.
    """
    __tablename__ = 'recipe_items'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    menu_item_id = db.Column(
        db.Integer, 
        db.ForeignKey('menu_items.id', ondelete='CASCADE'), 
        nullable=False
    )
    ingredient_id = db.Column(
        db.Integer, 
        db.ForeignKey('ingredients.id', ondelete='CASCADE'), 
        nullable=False
    )
    quantity_required = db.Column(
        db.Float, 
        nullable=False
    )
    ingredient = db.relationship(
        'Ingredient'
    )
    menu_item = db.relationship(
        'MenuItem', 
        backref=db.backref('recipe', cascade="all, delete-orphan")
    )

class OTPVerification(db.Model):
    """
    Stores temporary OTP codes sent to customers for verification.
    """
    __tablename__ = 'otp_verifications'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    phone = db.Column(
        db.String(20), 
        nullable=False
    )
    code = db.Column(
        db.String(6), 
        nullable=False
    )
    expires_at = db.Column(
        db.DateTime, 
        nullable=False
    )
    is_verified = db.Column(
        db.Boolean, 
        default=False
    )

class Expense(db.Model):
    """
    The Petty Cash Ledger. Tracks money leaving the drawer for supplies.
    """
    __tablename__ = 'expenses'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    description = db.Column(
        db.String(200), 
        nullable=False
    )
    amount = db.Column(
        db.Float, 
        nullable=False
    )
    created_at = db.Column(
        db.DateTime, 
        default=get_ph_time
    )

class AuditLog(db.Model):
    """
    TRANSPARENCY FEATURE: Stores an immutable record of staff and system actions.
    """
    __tablename__ = 'audit_logs'
    
    id = db.Column(
        db.Integer, 
        primary_key=True
    )
    action = db.Column(
        db.String(100), 
        nullable=False
    )
    details = db.Column(
        db.String(255), 
        nullable=True
    )
    created_at = db.Column(
        db.DateTime, 
        default=get_ph_time
    )

# ==========================================
# 4. FRONTEND HTML TEMPLATES
# ==========================================

# --- Admin Login Template ---
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#3E2723">
    
    <link rel="icon" href="/static/images/9599.jpg">
    
    <title>Admin Login | 9599 Tea & Coffee</title>
    
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        * { 
            box-sizing: border-box; 
            margin: 0; 
            padding: 0; 
            font-family: 'Poppins', sans-serif; 
        }
        
        body { 
            background-color: #F5EFE6; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            height: 100vh; 
            padding: 20px; 
        }
        
        .login-box { 
            background: white; 
            padding-top: 40px; 
            padding-bottom: 40px;
            padding-left: 40px;
            padding-right: 40px;
            border-radius: 12px; 
            box-shadow: 0 10px 25px rgba(111, 78, 55, 0.1); 
            text-align: center; 
            width: 100%; 
            max-width: 400px; 
            border: 1px solid #EFEBE4; 
        }
        
        .login-box h2 { 
            color: #3E2723; 
            margin-bottom: 5px; 
            font-weight: 800; 
        }
        
        .login-box p { 
            color: #8D6E63; 
            font-size: 0.9rem; 
            margin-bottom: 25px; 
        }
        
        .input-pin { 
            width: 100%; 
            padding: 15px; 
            border: 2px solid #D7CCC8; 
            border-radius: 8px; 
            font-size: 1.5rem; 
            text-align: center; 
            letter-spacing: 8px; 
            margin-bottom: 20px; 
            outline: none; 
            font-weight: 800; 
            color: #3E2723; 
            transition: border-color 0.2s; 
            background: #FDFBF7; 
        }
        
        .input-pin:focus { 
            border-color: #6F4E37; 
        }
        
        .btn-login { 
            width: 100%; 
            background: #6F4E37; 
            color: white; 
            border: none; 
            padding-top: 15px;
            padding-bottom: 15px;
            padding-left: 15px;
            padding-right: 15px;
            border-radius: 8px; 
            font-weight: 700; 
            font-size: 1rem; 
            cursor: pointer; 
            transition: 0.2s; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            gap: 10px; 
        }
        
        .btn-login:hover { 
            background: #4A3324; 
        }
        
        .error { 
            background: #FFEBEE; 
            color: #C62828; 
            padding: 10px; 
            border-radius: 8px; 
            font-size: 0.85rem; 
            font-weight: 600; 
            margin-bottom: 20px; 
            border: 1px solid #FFCDD2; 
        }
        
        @media (max-width: 480px) {
            .login-box { 
                padding: 25px; 
                max-width: 100%; 
            }
        }
    </style>
</head>
<body>
    <div class="login-box">
        <i class="fas fa-coffee" style="font-size: 3rem; color: #A67B5B; margin-bottom: 15px;"></i>
        <h2>Admin Access</h2>
        <p>Enter master PIN for 9599 Store System</p>
        
        {% if error %}
        <div class="error">
            <i class="fas fa-exclamation-circle"></i> {{ error }}
        </div>
        {% endif %}
        
        <form method="POST">
            <input type="password" name="pin" class="input-pin" placeholder="•••••" required autofocus>
            <button type="submit" class="btn-login">Login Securely</button>
        </form>
    </div>
</body>
</html>
"""

# --- Storefront Template (CUSTOMER SITE / POS ONLY) ---
STOREFRONT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <link rel="icon" href="/static/images/9599.jpg">
    <title>Order Here | 9599 Tea & Coffee Shop</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    <style>
        :root {
            --espresso: #1E0F08;
            --coffee: #6B3A2A;
            --roast: #8C4A30;
            --caramel: #C8922A;
            --toffee: #A67B5B;
            --cream: #FDF6EC;
            --latte: #F4EAD8;
            --milk: #FDFBF7;
            --foam: #EEE5D8;
            --sage: #5C7C5C;
            --berry: #9B3A5A;
            --gold: #D4A843;
            --red-alert: #C0392B;
            --green-ready: #2E7D32;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'DM Sans', sans-serif;
            background-color: var(--cream);
            color: var(--espresso);
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }
        h1,h2,h3 { font-family: 'Playfair Display', serif; }

        /* ── TOAST ── */
        #toast-container {
            position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
            z-index: 9999; display: flex; flex-direction: column; gap: 10px; align-items: center;
        }
        .toast {
            background: var(--espresso); color: #fff;
            padding: 12px 22px; border-radius: 50px;
            font-weight: 600; font-size: 0.88rem;
            box-shadow: 0 6px 20px rgba(0,0,0,0.25);
            animation: slideUp 0.3s ease forwards;
            display: flex; align-items: center; gap: 10px;
        }
        .toast.error  { background: var(--red-alert); }
        .toast.success { background: var(--green-ready); }
        .toast.fade-out { animation: fadeOut 0.3s ease forwards; }
        @keyframes slideUp   { from { opacity:0; transform:translateY(20px);} to { opacity:1; transform:translateY(0);} }
        @keyframes fadeOut   { from { opacity:1; transform:translateY(0);} to { opacity:0; transform:translateY(20px);} }

        /* ── PROMO TICKER ── */
        .promo-ticker {
            background: var(--espresso); color: var(--caramel);
            padding: 8px 0; overflow: hidden; font-size: 0.82rem;
            font-weight: 600; letter-spacing: 0.3px; white-space: nowrap;
            flex-shrink: 0;
        }
        .ticker-inner { display: inline-block; animation: ticker 28s linear infinite; }
        @keyframes ticker { from { transform: translateX(100vw); } to { transform: translateX(-100%); } }

        /* ── HEADER ── */
        header {
            background: var(--milk);
            padding: 12px 20px;
            display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid var(--foam);
            flex-shrink: 0;
            box-shadow: 0 2px 12px rgba(30,15,8,0.06);
        }
        .logo-area {
            display: flex; align-items: center; gap: 12px;
            font-family: 'Playfair Display', serif;
            font-weight: 900; font-size: 1.1rem; color: var(--espresso); line-height: 1.1;
        }
        .logo-img {
            width: 44px; height: 44px; border-radius: 50%;
            object-fit: cover; border: 2px solid var(--caramel);
        }
        .logo-sub { font-family: 'DM Sans', sans-serif; font-size: 0.6rem; font-weight: 700; letter-spacing: 2.5px; color: var(--caramel); text-transform: uppercase; }
        .logo-hours { font-family: 'DM Sans', sans-serif; font-size: 0.62rem; font-weight: 500; color: var(--toffee); display: none; }

        /* ── MAIN LAYOUT ── */
        .main-container { display: flex; flex: 1; overflow: hidden; }
        .menu-area { flex: 1; padding: 16px 18px; overflow-y: auto; }

        /* ── CATEGORY TABS ── */
        .categories {
            display: flex; gap: 8px; overflow-x: auto; margin-bottom: 18px;
            padding-bottom: 6px; scrollbar-width: none; -webkit-overflow-scrolling: touch;
        }
        .categories::-webkit-scrollbar { display: none; }
        .cat-btn {
            padding: 8px 18px; border-radius: 50px;
            border: 1.5px solid var(--foam);
            background: var(--milk); color: var(--coffee);
            font-family: 'DM Sans', sans-serif;
            font-weight: 600; font-size: 0.82rem;
            cursor: pointer; white-space: nowrap;
            transition: all 0.2s ease;
            display: flex; align-items: center; gap: 6px;
        }
        .cat-btn:hover { border-color: var(--caramel); color: var(--caramel); }
        .cat-btn.active {
            background: var(--espresso); color: var(--caramel);
            border-color: var(--espresso);
            box-shadow: 0 4px 12px rgba(30,15,8,0.2);
        }

        /* ── MENU GRID ── */
        .menu-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
            gap: 14px;
        }

        /* ── DRINK CARD ── */
        .card {
            background: var(--milk); border-radius: 16px; overflow: hidden;
            box-shadow: 0 3px 14px rgba(30,15,8,0.06);
            cursor: pointer; transition: transform 0.2s ease, box-shadow 0.2s ease;
            border: 1px solid var(--foam); position: relative;
        }
        .card:hover { transform: translateY(-4px); box-shadow: 0 8px 24px rgba(30,15,8,0.12); }
        .card:active { transform: scale(0.97); }

        .card-img-container {
            height: 128px; position: relative;
            display: flex; justify-content: center; align-items: center;
            overflow: hidden; background: #2E1808;
        }
        .card-photo {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            object-fit: cover; object-position: center;
            transition: transform 0.35s ease;
        }
        .card:hover .card-photo { transform: scale(1.06); }
        .card-photo-overlay {
            position: absolute; bottom: 0; left: 0; right: 0; height: 55%;
            background: linear-gradient(to top, rgba(20,8,4,0.72) 0%, transparent 100%);
            z-index: 1;
        }
        /* Fallback gradient if image fails to load */
        .card-img-container.cat-trending   { background: linear-gradient(145deg, #4A1C10, #C8922A); }
        .card-img-container.cat-signature  { background: linear-gradient(145deg, #2D1B45, #7B5EA7); }
        .card-img-container.cat-classic    { background: linear-gradient(145deg, #3B2010, #9B6A3A); }
        .card-img-container.cat-matcha     { background: linear-gradient(145deg, #1A3020, #5C7C5C); }
        .card-img-container.cat-fruit      { background: linear-gradient(145deg, #4A1028, #C0395A); }
        .card-img-container.cat-taro       { background: linear-gradient(145deg, #2E1A45, #8B6AB5); }
        .card-img-container.cat-snacks     { background: linear-gradient(145deg, #3B2B08, #C87F20); }
        .card-img-container.cat-default    { background: linear-gradient(145deg, #2E1808, #8C4A30); }

        .card-price {
            position: absolute; bottom: 8px; right: 8px;
            background: rgba(0,0,0,0.55); backdrop-filter: blur(4px);
            color: var(--caramel); padding: 4px 9px; border-radius: 20px;
            font-weight: 700; font-size: 0.78rem; letter-spacing: 0.3px;
        }
        .card-body { padding: 11px 12px 13px; }
        .card-title {
            font-weight: 700; color: var(--espresso);
            font-size: 0.88rem; line-height: 1.25;
        }
        .card-cat-tag {
            margin-top: 5px; font-size: 0.68rem; font-weight: 600;
            color: var(--toffee); text-transform: uppercase; letter-spacing: 0.8px;
        }

        /* ── BADGES ── */
        .badge-bestseller {
            position: absolute; top: 8px; left: 8px;
            background: var(--caramel); color: var(--espresso);
            padding: 3px 8px; border-radius: 20px;
            font-size: 0.62rem; font-weight: 800; letter-spacing: 0.5px;
            text-transform: uppercase; z-index: 5;
        }
        .badge-new {
            position: absolute; top: 8px; left: 8px;
            background: var(--sage); color: #fff;
            padding: 3px 8px; border-radius: 20px;
            font-size: 0.62rem; font-weight: 800; letter-spacing: 0.5px;
            text-transform: uppercase; z-index: 5;
        }
        .low-stock-badge {
            position: absolute; top: 8px; left: 8px;
            background: #E65100; color: #fff;
            padding: 3px 8px; border-radius: 20px;
            font-size: 0.62rem; font-weight: 800; z-index: 5;
        }
        .card.sold-out { opacity: 0.45; cursor: not-allowed; }
        .card.sold-out:hover { transform: none; box-shadow: 0 3px 14px rgba(30,15,8,0.06); }
        .sold-out-badge {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(255,255,255,0.55); backdrop-filter: blur(2px);
            display: flex; justify-content: center; align-items: center;
            font-weight: 900; color: var(--red-alert);
            font-size: 1rem; z-index: 10; letter-spacing: 2px; text-transform: uppercase;
        }
        .empty-category {
            grid-column: 1 / -1; text-align: center;
            color: var(--toffee); padding: 50px; font-weight: 600;
        }

        /* ── CART SIDEBAR ── */
        .sidebar {
            width: 340px; background: var(--milk);
            border-left: 1px solid var(--foam);
            display: flex; flex-direction: column; z-index: 50;
        }
        .cart-top-section {
            padding: 16px 18px 12px; flex-shrink: 0;
        }
        .cart-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 14px;
        }
        .cart-title {
            font-family: 'Playfair Display', serif;
            font-size: 1.15rem; font-weight: 900; color: var(--espresso);
            display: flex; align-items: center; gap: 9px;
        }
        .cart-title i { color: var(--caramel); font-size: 1rem; }
        .cart-count {
            background: var(--caramel); color: var(--espresso);
            font-family: 'DM Sans', sans-serif;
            font-size: 0.72rem; font-weight: 800;
            padding: 2px 8px; border-radius: 20px; display: none;
        }

        .order-type {
            display: flex; background: var(--latte);
            border-radius: 10px; padding: 4px; gap: 4px;
            border: 1px solid var(--foam); margin-bottom: 12px;
        }
        .type-btn {
            flex: 1; padding: 7px 10px; text-align: center;
            font-weight: 600; font-size: 0.82rem;
            border-radius: 8px; cursor: pointer;
            color: var(--toffee); transition: all 0.2s;
        }
        .type-btn.active {
            background: var(--espresso); color: var(--caramel);
            box-shadow: 0 2px 6px rgba(30,15,8,0.2);
        }

        .name-input {
            width: 100%; padding: 10px 13px;
            border: 1.5px solid var(--foam);
            border-radius: 10px; font-size: 0.88rem;
            font-weight: 500; outline: none;
            margin-bottom: 10px; color: var(--espresso);
            background: var(--cream); font-family: 'DM Sans', sans-serif;
            transition: border-color 0.2s;
        }
        .name-input:focus { border-color: var(--caramel); background: #fff; }

        .pickup-label {
            font-size: 0.72rem; font-weight: 700; color: var(--toffee);
            margin-bottom: 5px; display: block; text-transform: uppercase; letter-spacing: 0.8px;
        }

        /* ── CART CONTENT ── */
        .cart-content {
            padding: 10px 18px; flex: 1;
            display: flex; flex-direction: column; overflow-y: auto;
            background: var(--milk); border-top: 1px solid var(--foam);
        }
        .empty-cart { margin: auto 0; text-align: center; padding: 20px 0; }
        .empty-cart-icon { font-size: 2.8rem; margin-bottom: 10px; opacity: 0.3; }
        .empty-cart p { font-weight: 700; font-size: 0.9rem; color: var(--toffee); }
        .empty-cart small { font-size: 0.78rem; color: #B0967E; font-weight: 500; }

        .cart-items-list { flex: 1; overflow-y: visible; display: none; }
        .cart-item {
            display: flex; justify-content: space-between; align-items: flex-start;
            margin-bottom: 10px; padding: 10px;
            border-radius: 10px; background: var(--cream);
            border: 1px solid var(--foam); gap: 8px;
        }
        .item-details h4 { font-size: 0.88rem; font-weight: 700; color: var(--espresso); margin-bottom: 2px; }
        .item-meta { font-size: 0.7rem; color: var(--toffee); font-weight: 500; }
        .item-remove {
            font-size: 0.72rem; color: var(--red-alert);
            cursor: pointer; display: inline-flex; align-items: center; gap: 4px;
            font-weight: 600; margin-top: 4px; background: none; border: none; padding: 0;
        }
        .item-price { font-weight: 800; color: var(--coffee); font-size: 0.9rem; white-space: nowrap; }

        /* ── CHECKOUT AREA ── */
        .checkout-area {
            padding: 14px 18px; border-top: 1px solid var(--foam);
            background: var(--milk); flex-shrink: 0;
            box-shadow: 0 -4px 12px rgba(30,15,8,0.04);
        }
        .total-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .total-label { font-size: 0.88rem; font-weight: 700; color: var(--toffee); text-transform: uppercase; letter-spacing: 0.5px; }
        .total-amount { font-family: 'Playfair Display', serif; font-size: 1.55rem; font-weight: 900; color: var(--espresso); }

        .checkout-btn {
            width: 100%; padding: 15px; border: none; border-radius: 12px;
            font-size: 0.92rem; font-weight: 700; letter-spacing: 0.5px;
            display: flex; justify-content: center; align-items: center; gap: 10px;
            color: var(--espresso); background: var(--foam);
            cursor: not-allowed; transition: all 0.25s ease;
            font-family: 'DM Sans', sans-serif; text-transform: uppercase;
        }
        .checkout-btn.active {
            background: linear-gradient(135deg, var(--caramel) 0%, #E0A030 100%);
            cursor: pointer; color: var(--espresso);
            box-shadow: 0 6px 20px rgba(200,146,42,0.4);
        }
        .checkout-btn.active:hover { box-shadow: 0 8px 26px rgba(200,146,42,0.55); transform: translateY(-1px); }

        /* ── MODALS ── */
        .modal {
            display: none; position: fixed; z-index: 200; left: 0; top: 0;
            width: 100%; height: 100%;
            background: rgba(30,15,8,0.6); backdrop-filter: blur(4px);
            align-items: center; justify-content: center;
        }
        .modal-content {
            background: var(--milk); padding: 30px; border-radius: 20px;
            max-width: 92%; width: 370px;
            border: 1px solid var(--foam);
            box-shadow: 0 24px 60px rgba(30,15,8,0.25);
            max-height: 92vh; overflow-y: auto;
        }
        .modal-content h2 {
            font-family: 'Playfair Display', serif;
            font-weight: 900; margin-bottom: 8px;
            color: var(--espresso); text-align: center;
        }

        /* SUCCESS MODAL */
        .success-icon { font-size: 3.5rem; text-align: center; margin-bottom: 10px; }
        .order-number {
            font-family: 'Playfair Display', serif;
            font-size: 2.2rem; font-weight: 900;
            color: var(--caramel); margin: 16px 0;
            letter-spacing: 4px; text-align: center;
            background: var(--latte); border-radius: 12px; padding: 14px;
            border: 2px dashed var(--caramel);
        }
        .modal-btn {
            background: linear-gradient(135deg, var(--caramel), #E0A030);
            color: var(--espresso); padding: 13px 25px;
            border: none; border-radius: 10px; font-weight: 700;
            cursor: pointer; width: 100%; font-size: 0.95rem;
            font-family: 'DM Sans', sans-serif; margin-top: 8px;
        }

        /* SIZE MODAL */
        .modal-section-label {
            font-size: 0.72rem; font-weight: 700; color: var(--toffee);
            text-transform: uppercase; letter-spacing: 1px;
            margin-bottom: 8px; display: block;
        }
        .size-btns { display: flex; gap: 10px; margin-bottom: 18px; }
        .size-btn {
            flex: 1; padding: 14px 10px; border-radius: 12px;
            border: 2px solid var(--foam); background: var(--cream);
            color: var(--espresso); font-weight: 700; cursor: pointer;
            text-align: center; font-family: 'DM Sans', sans-serif;
            transition: all 0.2s; font-size: 0.92rem;
        }
        .size-btn.selected { background: var(--espresso); color: var(--caramel); border-color: var(--espresso); }
        .size-btn-price { font-size: 0.75rem; font-weight: 600; opacity: 0.8; }

        .sel-row { display: flex; gap: 10px; margin-bottom: 18px; }
        .sel-group { flex: 1; }
        .sel-group select {
            width: 100%; padding: 10px 12px;
            border: 1.5px solid var(--foam); border-radius: 10px;
            font-family: 'DM Sans', sans-serif; font-weight: 600;
            color: var(--espresso); background: var(--cream);
            outline: none; font-size: 0.85rem; margin-top: 6px;
        }
        .sel-group select:focus { border-color: var(--caramel); }

        .addon-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }
        .addon-label {
            display: flex; align-items: center; gap: 8px;
            padding: 10px 12px; border: 1.5px solid var(--foam);
            border-radius: 10px; cursor: pointer; font-weight: 600;
            color: var(--espresso); background: var(--cream);
            font-size: 0.82rem; transition: all 0.2s;
        }
        .addon-label:hover { border-color: var(--caramel); }
        .addon-label input[type="checkbox"]:checked + span { color: var(--coffee); }
        .addon-label input { width: 16px; height: 16px; accent-color: var(--caramel); }

        .modal-actions { display: flex; gap: 10px; margin-top: 22px; }
        .btn-cancel {
            flex: 1; background: var(--latte); color: var(--coffee); border: none;
            padding: 13px; border-radius: 10px; font-weight: 700; cursor: pointer;
            font-family: 'DM Sans', sans-serif;
        }
        .btn-add {
            flex: 2; background: linear-gradient(135deg, var(--caramel), #E0A030);
            color: var(--espresso); border: none;
            padding: 13px; border-radius: 10px; font-weight: 700; cursor: pointer;
            font-family: 'DM Sans', sans-serif; font-size: 0.95rem;
        }

        /* ── NOTIFICATIONS ── */
        .notif-container { position: relative; margin-right: 4px; display: flex; align-items: center; }
        .notif-bell { cursor: pointer; position: relative; padding: 8px; border-radius: 50%; background: var(--latte); border: 1px solid var(--foam); }
        .notif-bell:hover { background: var(--foam); }
        .notif-bell i { font-size: 1.1rem; color: var(--coffee); }
        .notif-badge {
            position: absolute; top: -2px; right: -2px;
            background: var(--red-alert); color: white; border-radius: 50%;
            padding: 2px 5px; font-size: 0.6rem; font-weight: 800; display: none;
            border: 2px solid white;
        }
        .notif-dropdown {
            display: none; position: absolute; top: 50px; right: -10px;
            background: var(--milk); border: 1px solid var(--foam); border-radius: 14px;
            width: 300px; box-shadow: 0 12px 30px rgba(30,15,8,0.15); z-index: 1000;
            flex-direction: column; overflow: hidden;
        }
        .notif-header {
            padding: 14px 16px; border-bottom: 1px solid var(--foam);
            font-weight: 700; font-size: 0.92rem; color: var(--espresso);
            display: flex; justify-content: space-between; align-items: center;
            background: var(--latte);
        }
        .notif-clear { font-size: 0.72rem; color: var(--toffee); cursor: pointer; font-weight: 600; }
        .notif-clear:hover { color: var(--red-alert); }
        .notif-list { max-height: 280px; overflow-y: auto; display: flex; flex-direction: column; }
        .notif-item {
            padding: 12px 16px; border-bottom: 1px solid var(--foam);
            font-size: 0.83rem; color: var(--espresso); font-weight: 600;
            line-height: 1.4; display: flex; align-items: flex-start; gap: 10px;
        }
        .notif-item i { color: var(--gold); margin-top: 2px; }
        .notif-item.ready i { color: var(--green-ready); }
        .notif-item.completed i { color: var(--coffee); }
        .notif-empty { padding: 20px; text-align: center; color: var(--toffee); font-size: 0.83rem; }

        /* ── RESPONSIVE ── */
        @media (max-width: 768px) {
            body { height: auto; min-height: 100vh; overflow-y: auto; }
            .main-container { flex-direction: column; height: auto; overflow: visible; }
            .menu-area { flex: none; height: auto; overflow: visible; padding-bottom: 20px; }
            .menu-grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
            .sidebar { width: 100%; flex: none; height: auto; border-left: none; border-top: 2px solid var(--foam); }
            .cart-content { max-height: 320px; }
            .notif-dropdown { position: fixed; top: 62px; right: 10px; width: calc(100% - 20px); max-width: 340px; }
        }

        /* ── GOOGLE LOGIN GATE ── */
        .gate-wrapper {
            display: flex; height: 100vh; width: 100vw;
            justify-content: center; align-items: center;
            background: linear-gradient(160deg, var(--espresso) 0%, #3A1A0A 100%);
            padding: 20px; flex-direction: column;
        }
        .gate-card {
            background: var(--milk); padding: 44px 40px; border-radius: 24px;
            box-shadow: 0 30px 80px rgba(0,0,0,0.4);
            width: 100%; max-width: 420px; text-align: center;
        }
        .gate-logo { font-size: 3rem; margin-bottom: 6px; }
        .gate-title { font-family: 'Playfair Display', serif; font-size: 1.9rem; font-weight: 900; color: var(--espresso); margin-bottom: 6px; }
        .gate-subtitle { font-size: 0.88rem; color: var(--toffee); font-weight: 500; margin-bottom: 26px; line-height: 1.5; }
        .gate-divider { display: flex; align-items: center; gap: 12px; margin: 20px 0; }
        .gate-divider::before, .gate-divider::after { content: ''; flex: 1; height: 1px; background: var(--foam); }
        .gate-divider span { font-size: 0.75rem; color: var(--toffee); font-weight: 600; letter-spacing: 1px; }
        .gate-features { display: flex; justify-content: center; gap: 20px; margin-bottom: 24px; }
        .gate-feature { display: flex; flex-direction: column; align-items: center; gap: 5px; }
        .gate-feature span:first-child { font-size: 1.5rem; }
        .gate-feature span:last-child { font-size: 0.7rem; font-weight: 600; color: var(--toffee); }

        /* Cart items header */
        .cart-section-label {
            font-size: 0.7rem; font-weight: 800; color: var(--toffee);
            text-transform: uppercase; letter-spacing: 1px;
            margin-bottom: 10px; display: none;
        }
    </style>
</head>
<body>

<div id="toast-container"></div>

{% if not session.get('customer_verified') %}
<!-- ======================================= -->
<!-- GOOGLE LOGIN GATEKEEPER                 -->
<!-- ======================================= -->
<div id="login-gatekeeper" class="gate-wrapper">
    <div class="gate-card">
        <div class="gate-logo">☕</div>
        <h1 class="gate-title">9599 Tea & Coffee</h1>
        <p class="gate-subtitle">Craft drinks made with love.<br>Sign in to explore our menu & place your order.</p>

        <div class="gate-features">
            <div class="gate-feature"><span>🧋</span><span>Milk Teas</span></div>
            <div class="gate-feature"><span>🌿</span><span>Matcha</span></div>
            <div class="gate-feature"><span>🍓</span><span>Frappes</span></div>
            <div class="gate-feature"><span>🍟</span><span>Snacks</span></div>
        </div>

        {% if google_client_id == 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com' or google_client_id == '' %}
        <div style="background:#FFF3E0; color:#E65100; padding:14px; border-radius:10px; border:1px solid #FFE0B2; margin-bottom:18px; font-size:0.83rem; text-align:left;">
            <strong>⚠️ Admin Setup Required:</strong><br><br>
            Google Login is disabled. Configure <b>GOOGLE_CLIENT_ID</b> in your Render Environment Variables.
        </div>
        {% endif %}

        <div id="g_id_onload"
             data-client_id="{{ google_client_id }}"
             data-context="signin"
             data-ux_mode="popup"
             data-callback="handleGoogleLogin"
             data-auto_prompt="false">
        </div>
        <div class="g_id_signin"
             data-type="standard" data-shape="rectangular"
             data-theme="outline" data-text="continue_with"
             data-size="large" data-logo_alignment="left"
             style="display:flex; justify-content:center; margin-top:6px;">
        </div>
        <div id="login-spinner" style="display:none; margin-top:18px; color:var(--coffee); font-weight:700;">
            <i class="fas fa-spinner fa-spin"></i> Authenticating…
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

    document.addEventListener("DOMContentLoaded", () => {
        var ua = navigator.userAgent || navigator.vendor || window.opera;
        if ((ua.indexOf("FBAN") > -1) || (ua.indexOf("FBAV") > -1) || (ua.indexOf("Messenger") > -1) || (ua.indexOf("Instagram") > -1)) {
            const warningDiv = document.createElement("div");
            warningDiv.style = "background:#FFF3E0;color:#E65100;padding:14px;margin-bottom:16px;border-radius:10px;border:1px solid #FFE0B2;font-size:0.82rem;text-align:left;";
            warningDiv.innerHTML = "<strong>⚠️ Facebook/Messenger Browser Detected:</strong><br><br>Google Login may not work here. Tap the <b>3 dots (⋮)</b> and choose <b>'Open in Chrome'</b>.";
            document.getElementById("login-gatekeeper").querySelector(".gate-card").prepend(warningDiv);
        }
    });

    async function handleGoogleLogin(response) {
        const token = response.credential;
        document.querySelector('.g_id_signin').style.display = 'none';
        document.getElementById('login-spinner').style.display = 'block';
        try {
            const res = await fetch('/api/auth/google', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ token: token })
            });
            if (res.ok) {
                location.reload();
            } else {
                const data = await res.json();
                showToast("Authentication Error: " + (data.error || "Please try again."), "error");
                document.querySelector('.g_id_signin').style.display = 'flex';
                document.getElementById('login-spinner').style.display = 'none';
            }
        } catch (e) {
            showToast("Connection error. Please check your internet.", "error");
            document.querySelector('.g_id_signin').style.display = 'flex';
            document.getElementById('login-spinner').style.display = 'none';
        }
    }
</script>

{% else %}
<!-- ======================================= -->
<!-- MAIN STOREFRONT (Verified Customer)     -->
<!-- ======================================= -->

<!-- Promo Ticker -->
<div class="promo-ticker">
    <span class="ticker-inner">
        🔥 Drinks starting at ₱49 &nbsp;·&nbsp;
        ✨ Customize your sugar & ice level &nbsp;·&nbsp;
        🧋 Try our Signature Milk Teas &nbsp;·&nbsp;
        🌿 Matcha lovers — we've got you covered &nbsp;·&nbsp;
        🍟 Pair your drink with our crispy snacks &nbsp;·&nbsp;
        🎉 Mix & match — add Nata, Pearl, or Coffee Jelly! &nbsp;·&nbsp;
        ☕ Crafted fresh for every order &nbsp;&nbsp;&nbsp;&nbsp;
    </span>
</div>

<!-- Header -->
<header>
    <div class="logo-area">
        <img src="/static/images/9599.jpg" alt="9599 Logo" class="logo-img" onerror="this.style.display='none'">
        <div style="display:flex; flex-direction:column;">
            <span>9599 Tea &amp; Coffee</span>
            <span class="logo-sub">Parne Na!</span>
            <span class="logo-hours" id="store-hours-display"></span>
        </div>
    </div>
    <div class="notif-container">
        <div class="notif-bell" onclick="toggleNotif()">
            <i class="fas fa-bell"></i>
            <span class="notif-badge" id="notif-badge">0</span>
        </div>
        <div class="notif-dropdown" id="notif-dropdown">
            <div class="notif-header">
                🔔 Notifications
                <span class="notif-clear" onclick="clearNotifs()">Clear All</span>
            </div>
            <div class="notif-list" id="notif-list">
                <div class="notif-empty">No new notifications.</div>
            </div>
        </div>
    </div>
</header>

<!-- Main Layout -->
<div class="main-container">

    <!-- LEFT: Menu -->
    <div class="menu-area">
        <div class="categories" id="categories-container">
            <button class="cat-btn active" onclick="filterMenu('All', this)">🍵 All</button>
        </div>
        <div class="menu-grid" id="menu-grid">
            <div class="empty-category">
                <i class="fas fa-spinner fa-spin" style="font-size:1.4rem; color:var(--caramel);"></i>
                <p style="margin-top:10px;">Loading our menu…</p>
            </div>
        </div>
    </div>

    <!-- RIGHT: Cart Sidebar -->
    <div class="sidebar">
        <div class="cart-top-section">
            <div class="cart-header">
                <div class="cart-title">
                    <i class="fas fa-shopping-bag"></i>
                    Your Order
                    <span class="cart-count" id="cart-count">0</span>
                </div>
            </div>

            <div class="order-type">
                <div class="type-btn active" id="btn-dine-in" onclick="setOrderType('Dine-In')">🪑 Dine-In</div>
                <div class="type-btn" id="btn-take-out" onclick="setOrderType('Take-Out')">🥡 Take-Out</div>
            </div>

            <input type="text" class="name-input" id="customer-name" placeholder="Your name" oninput="checkCheckoutStatus()">

            <label class="pickup-label" for="pickup-time">
                Pick-up Time <span style="color:var(--red-alert);">*</span>
            </label>
            <input type="time" class="name-input" id="pickup-time" oninput="checkCheckoutStatus()" required style="margin-bottom:0;">
        </div>

        <div class="cart-content">
            <div class="cart-section-label" id="cart-items-header">Selected Items</div>

            <div class="empty-cart" id="empty-cart">
                <div class="empty-cart-icon">🧋</div>
                <p>Your cart is empty</p>
                <small>Tap any item to add it</small>
            </div>

            <div class="cart-items-list" id="cart-items"></div>
        </div>

        <div class="checkout-area">
            <div class="total-row">
                <span class="total-label">Total</span>
                <span class="total-amount" id="cart-total">₱0.00</span>
            </div>
            <button class="checkout-btn" id="checkout-btn" onclick="submitOrder()">
                <i class="fas fa-paper-plane"></i> Place My Order
            </button>
        </div>
    </div>
</div><!-- /main-container -->

<!-- ── SUCCESS MODAL ── -->
<div id="success-modal" class="modal">
    <div class="modal-content" style="text-align:center;">
        <div class="success-icon">🎉</div>
        <h2>Order Placed!</h2>
        <p style="color:var(--toffee); font-size:0.88rem; margin-top:6px;">Your order is being prepared. We'll call your name!</p>
        <div class="order-number" id="display-code">XXXXXX</div>
        <p style="font-size:0.78rem; color:var(--toffee); margin-bottom:4px;">Show this code when picking up your order.</p>
        <button class="modal-btn" onclick="closeModal()">Done, Thanks! 👋</button>
    </div>
</div>

<!-- ── CUSTOMIZATION MODAL ── -->
<div id="size-modal" class="modal">
    <div class="modal-content">
        <h2 id="size-modal-title" style="text-align:center; margin-bottom:20px;">Customize</h2>

        <span class="modal-section-label">1. Choose Cup Size</span>
        <div class="size-btns">
            <button class="size-btn selected" id="btn-size-16" onclick="selectSize('16 oz', 49)">
                🥤 16 oz<br><span class="size-btn-price">₱49.00</span>
            </button>
            <button class="size-btn" id="btn-size-22" onclick="selectSize('22 oz', 59)">
                🧋 22 oz<br><span class="size-btn-price">₱59.00</span>
            </button>
        </div>

        <div class="sel-row">
            <div class="sel-group">
                <span class="modal-section-label">Sugar Level</span>
                <select id="sugar-level-select">
                    <option value="100% Sugar">100% – Normal</option>
                    <option value="75% Sugar">75% – Less Sweet</option>
                    <option value="50% Sugar">50% – Half</option>
                    <option value="25% Sugar">25% – Slight</option>
                    <option value="0% Sugar">0% – No Sugar</option>
                </select>
            </div>
            <div class="sel-group">
                <span class="modal-section-label">Ice Level</span>
                <select id="ice-level-select">
                    <option value="Normal Ice">Normal Ice</option>
                    <option value="Less Ice">Less Ice</option>
                    <option value="No Ice">No Ice</option>
                    <option value="Extra Ice">Extra Ice</option>
                </select>
            </div>
        </div>

        <span class="modal-section-label">Add-ons (optional)</span>
        <div class="addon-grid">
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Nata"> <span>🟡 Nata <small style="font-size:0.7rem; color:var(--toffee);">+₱10</small></span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Pearl"> <span>⚫ Pearl <small style="font-size:0.7rem; color:var(--toffee);">+₱10</small></span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Coffee Jelly"> <span>☕ Coffee Jelly <small style="font-size:0.7rem; color:var(--toffee);">+₱10</small></span></label>
            <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Cloud Foam"> <span>☁️ Cloud Foam <small style="font-size:0.7rem; color:var(--toffee);">+₱15</small></span></label>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeSizeModal()">Cancel</button>
            <button class="btn-add" onclick="confirmAddToCart()">
                <i class="fas fa-plus"></i> Add to Cart
            </button>
        </div>
    </div>
</div>

<!-- ── JAVASCRIPT ── -->
<script>
    /* ── UTILITIES ── */
    function showToast(message, type='info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        let icon = type === 'error' ? 'fa-exclamation-circle' : 'fa-check-circle';
        toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
        container.appendChild(toast);
        setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 3200);
    }

    function escapeHTML(str) {
        let div = document.createElement('div'); div.innerText = str; return div.innerHTML;
    }

    /* Category → emoji & class mapping */
    const CAT_MAP = {
        'Trending Now':       { emoji: '🔥', cls: 'cat-trending',  badge: 'bestseller' },
        'Signature Series':   { emoji: '⭐', cls: 'cat-signature', badge: 'bestseller' },
        'Classic Milk Tea':   { emoji: '🧋', cls: 'cat-classic',   badge: null },
        'Matcha & Taro':      { emoji: '🍵', cls: 'cat-taro',      badge: 'new' },
        'Matcha Menu':        { emoji: '🌿', cls: 'cat-matcha',    badge: null },
        'Fruit Infusions':    { emoji: '🍓', cls: 'cat-fruit',     badge: null },
        'Snacks':             { emoji: '🍟', cls: 'cat-snacks',    badge: null },
    };

    function getCatInfo(category) {
        return CAT_MAP[category] || { emoji: '☕', cls: 'cat-default', badge: null };
    }

    /* Per-item photo mapping → { src, pos }
       Place these 3 image files in your Flask project at /static/images/:
         menu_snacks.jpg, menu_matcha.jpg, menu_specials.jpg */
    const ITEM_PHOTOS = {
        'Hash Brown':             { src: '/static/images/menu_snacks.jpg',   pos: '50% 14%' },
        'French Fries':           { src: '/static/images/menu_snacks.jpg',   pos: '60% 40%' },
        'Onion Rings':            { src: '/static/images/menu_snacks.jpg',   pos: '45% 65%' },
        'Potato Mojos':           { src: '/static/images/menu_snacks.jpg',   pos: '55% 88%' },
        'Matcha Caramel':         { src: '/static/images/menu_matcha.jpg',   pos: '28% 58%' },
        'Matcha Strawberry':      { src: '/static/images/menu_matcha.jpg',   pos: '75% 55%' },
        'Matcha Frappe':          { src: '/static/images/menu_matcha.jpg',   pos: '18% 80%' },
        'Matcha Latte':           { src: '/static/images/menu_matcha.jpg',   pos: '72% 80%' },
        'Matcha Espresso':        { src: '/static/images/menu_matcha.jpg',   pos: '50% 60%' },
        'Dirty Matcha':           { src: '/static/images/menu_specials.jpg', pos: '50% 38%' },
        'Biscoff Frappe':         { src: '/static/images/menu_specials.jpg', pos: '72% 30%' },
        'Cloud Macchiato':        { src: '/static/images/menu_specials.jpg', pos: '28% 48%' },
        'The Midnight Velvet':    { src: '/static/images/menu_specials.jpg', pos: '22% 40%' },
        'The Jade Garden':        { src: '/static/images/menu_specials.jpg', pos: '50% 55%' },
        'Brown Sugar Deerioca':   { src: '/static/images/menu_specials.jpg', pos: '50% 75%' },
        'Wintermelon Frost':      { src: '/static/images/menu_specials.jpg', pos: '78% 55%' },
        'Strawberry Lychee':      { src: '/static/images/menu_specials.jpg', pos: '22% 55%' },
        'Classic Pearl Milk Tea': { src: '/static/images/menu_specials.jpg', pos: '50% 42%' },
        'Taro Symphony':          { src: '/static/images/menu_matcha.jpg',   pos: '50% 65%' },
    };

    function getItemPhoto(name) { return ITEM_PHOTOS[name] || null; }

    /* ── INIT ── */
    document.addEventListener("DOMContentLoaded", () => {
        const savedName = "{{ session.get('customer_name', '') }}";
        const nameInput = document.getElementById('customer-name');
        if (savedName && nameInput) {
            nameInput.value = savedName;
            nameInput.readOnly = true;
            nameInput.style.backgroundColor = "var(--latte)";
        }

        const params = new URLSearchParams(window.location.search);
        if (params.has('exp')) {
            const expTime = parseInt(params.get('exp'));
            if (Date.now() > expTime) {
                document.body.innerHTML = `
                    <div style="display:flex;height:100vh;width:100vw;justify-content:center;align-items:center;background:var(--cream);flex-direction:column;text-align:center;padding:20px;">
                        <div style="font-size:3rem;margin-bottom:16px;">🔒</div>
                        <h2 style="color:var(--espresso);font-family:'Playfair Display',serif;">Link Expired</h2>
                        <p style="color:var(--toffee);margin-top:8px;">Please ask the staff for a new ordering link.</p>
                    </div>`;
                return;
            }
        }

        const hoursDisplay = document.getElementById('store-hours-display');
        if (params.has('open') && params.has('close') && hoursDisplay) {
            hoursDisplay.innerText = `${params.get('open')} – ${params.get('close')}`;
            hoursDisplay.style.display = 'block';
        }

        fetchMenu();
        updateNotifUI();
    });

    /* ── NOTIFICATIONS ── */
    let notifications = JSON.parse(localStorage.getItem('notifications')) || [];
    let unreadNotifs  = parseInt(localStorage.getItem('unreadNotifs'))   || 0;

    function updateNotifUI() {
        const badge = document.getElementById('notif-badge');
        const list  = document.getElementById('notif-list');
        if (unreadNotifs > 0) { badge.innerText = unreadNotifs; badge.style.display = 'block'; }
        else { badge.style.display = 'none'; }

        if (notifications.length > 0) {
            list.innerHTML = notifications.map(n => {
                let iconClass = 'fa-info-circle', itemClass = '';
                if (n.status === 'Ready for Pick-up') { iconClass = 'fa-check-circle'; itemClass = 'ready'; }
                if (n.status === 'Completed')         { iconClass = 'fa-flag-checkered'; itemClass = 'completed'; }
                return `<div class="notif-item ${itemClass}"><i class="fas ${iconClass}"></i>
                    <div>Order <strong>#${escapeHTML(n.code)}</strong> is now:<br>
                    <span style="color:var(--toffee);">${escapeHTML(n.status)}</span></div></div>`;
            }).join('');
        } else {
            list.innerHTML = '<div class="notif-empty">No new notifications.</div>';
        }
    }

    function toggleNotif() {
        const dropdown = document.getElementById('notif-dropdown');
        if (dropdown.style.display === 'none' || dropdown.style.display === '') {
            dropdown.style.display = 'flex'; unreadNotifs = 0;
            localStorage.setItem('unreadNotifs', 0); updateNotifUI();
        } else { dropdown.style.display = 'none'; }
    }

    function clearNotifs() {
        notifications = []; unreadNotifs = 0;
        localStorage.setItem('notifications', JSON.stringify(notifications));
        localStorage.setItem('unreadNotifs', 0); updateNotifUI();
    }

    async function pollCustomerOrderStatus() {
        let myOrders = JSON.parse(localStorage.getItem('myOrders')) || [];
        if (myOrders.length === 0) return;
        let codes = myOrders.map(o => encodeURIComponent(o.code)).join(',');
        try {
            const res = await fetch(`/api/customer/status?codes=${codes}&_t=${new Date().getTime()}`);
            if (!res.ok) return;
            const data = await res.json();
            let updated = false;
            data.forEach(serverOrder => {
                let localOrder = myOrders.find(o => o.code === serverOrder.code);
                if (localOrder && localOrder.status !== serverOrder.status) {
                    localOrder.status = serverOrder.status; updated = true;
                    showToast(`Order #${serverOrder.code} is now ${serverOrder.status}`, 'success');
                    notifications.unshift({ code: serverOrder.code, status: serverOrder.status });
                    unreadNotifs += 1;
                }
            });
            if (updated) {
                if (notifications.length > 10) notifications = notifications.slice(0, 10);
                localStorage.setItem('myOrders', JSON.stringify(myOrders));
                localStorage.setItem('notifications', JSON.stringify(notifications));
                localStorage.setItem('unreadNotifs', unreadNotifs);
                updateNotifUI();
            }
        } catch (e) { console.error("Polling error", e); }
    }
    setInterval(pollCustomerOrderStatus, 3000);

    /* ── MENU STATE ── */
    let menuItems = [];
    let cart      = [];
    let orderType = 'Dine-In';
    let pendingItemName = "";
    let pendingSize     = '16 oz';
    let pendingPrice    = 49;

    /* ── FETCH & RENDER MENU ── */
    async function fetchMenu() {
        try {
            const response = await fetch('/api/menu?_t=' + new Date().getTime());
            menuItems = await response.json();
            renderCategories();
            renderMenu('All');
        } catch(e) {
            document.getElementById('menu-grid').innerHTML =
                '<div class="empty-category"><i class="fas fa-wifi-slash"></i><p style="margin-top:8px;">Failed to load menu.</p></div>';
        }
    }

    function renderCategories() {
        const categories = [...new Set(menuItems.map(item => item.category))];
        const catContainer = document.getElementById('categories-container');
        catContainer.innerHTML = `<button class="cat-btn active" onclick="filterMenu('All', this)">🍵 All</button>`;
        categories.forEach(cat => {
            const info = getCatInfo(cat);
            catContainer.innerHTML += `<button class="cat-btn" onclick="filterMenu('${escapeHTML(cat)}', this)">${info.emoji} ${escapeHTML(cat)}</button>`;
        });
    }

    function renderMenu(categoryFilter) {
        const grid = document.getElementById('menu-grid');
        grid.innerHTML = '';
        const filteredItems = categoryFilter === 'All' ? menuItems : menuItems.filter(item => item.category === categoryFilter);
        if (filteredItems.length === 0) {
            grid.innerHTML = '<div class="empty-category">No items in this category.</div>';
            return;
        }
        filteredItems.forEach((item, index) => {
            const isSoldOut = item.stock <= 0;
            const info = getCatInfo(item.category);
            const onClick = isSoldOut ? '' : `onclick="addToCart('${item.name.replace(/'/g, "\\'")}', '${item.category.replace(/'/g, "\\'")}', ${item.price})"`;
            const cardClass = isSoldOut ? 'card sold-out' : 'card';
            const priceDisplay = item.category === 'Snacks' ? `₱${item.price.toFixed(0)}` : '₱49 / ₱59';

            let badgeHTML = '';
            if (isSoldOut) {
                badgeHTML = '<div class="sold-out-badge">SOLD OUT</div>';
            } else if (item.stock <= 5) {
                badgeHTML = `<div class="low-stock-badge">⚡ Only ${item.stock} left!</div>`;
            } else if (info.badge === 'bestseller') {
                badgeHTML = '<div class="badge-bestseller">⭐ Best Seller</div>';
            } else if (info.badge === 'new') {
                badgeHTML = '<div class="badge-new">✨ Fan Fave</div>';
            }

            const photo = getItemPhoto(item.name);
            const photoHTML = photo
                ? `<img class="card-photo" src="${photo.src}" alt="${escapeHTML(item.name)}"
                        style="object-position:${photo.pos};"
                        onerror="this.style.display='none'">`
                : `<img class="card-photo" src="/static/images/menu_specials.jpg" alt="${escapeHTML(item.name)}"
                        onerror="this.style.display='none'">`;

            grid.innerHTML += `
                <div class="${cardClass}" ${onClick} style="animation-delay:${index * 0.04}s">
                    <div class="card-img-container ${info.cls}">
                        ${photoHTML}
                        <div class="card-photo-overlay"></div>
                        ${badgeHTML}
                        <div class="card-price" style="z-index:2;">${priceDisplay}</div>
                    </div>
                    <div class="card-body">
                        <div class="card-title">${escapeHTML(item.name)}</div>
                        <div class="card-cat-tag">${escapeHTML(item.category)}</div>
                    </div>
                </div>
            `;
        });
    }

    function filterMenu(categoryName, btnElement) {
        document.querySelectorAll('.cat-btn').forEach(btn => btn.classList.remove('active'));
        btnElement.classList.add('active');
        renderMenu(categoryName);
    }

    /* ── ORDER TYPE ── */
    function setOrderType(type) {
        orderType = type;
        document.getElementById('btn-dine-in').className = type === 'Dine-In' ? 'type-btn active' : 'type-btn';
        document.getElementById('btn-take-out').className = type === 'Take-Out' ? 'type-btn active' : 'type-btn';
    }

    /* ── SIZE SELECTION ── */
    function selectSize(size, price) {
        pendingSize  = size;
        pendingPrice = price;
        document.getElementById('btn-size-16').className = size === '16 oz' ? 'size-btn selected' : 'size-btn';
        document.getElementById('btn-size-22').className = size === '22 oz' ? 'size-btn selected' : 'size-btn';
    }

    /* ── ADD TO CART ── */
    function addToCart(name, category, basePrice) {
        const item = menuItems.find(i => i.name === name);
        if (item && item.stock > 0) {
            if (category === 'Snacks') {
                cart.push({ name, size: 'Regular', addons: [], sugar: 'N/A', ice: 'N/A', price: basePrice });
                updateCartUI();
                showToast(`${name} added to cart! 🍟`, 'success');
            } else {
                pendingItemName = name;
                document.getElementById('size-modal-title').innerText = name;
                selectSize('16 oz', 49);
                document.getElementById('sugar-level-select').value = "100% Sugar";
                document.getElementById('ice-level-select').value   = "Normal Ice";
                document.querySelectorAll('.addon-checkbox').forEach(cb => cb.checked = false);
                document.getElementById('size-modal').style.display = 'flex';
            }
        } else {
            showToast("Sorry, this item is out of stock!", "error");
        }
    }

    function closeSizeModal() {
        document.getElementById('size-modal').style.display = 'none';
        pendingItemName = "";
    }

    function confirmAddToCart() {
        let addons = [], addonCost = 0;
        document.querySelectorAll('.addon-checkbox').forEach(cb => {
            if (cb.checked) { addons.push(cb.value); addonCost += (cb.value === 'Cloud Foam' ? 15 : 10); }
        });
        cart.push({
            name: pendingItemName, size: pendingSize,
            sugar: document.getElementById('sugar-level-select').value,
            ice: document.getElementById('ice-level-select').value,
            addons, price: pendingPrice + addonCost
        });
        updateCartUI();
        closeSizeModal();
        showToast(`${pendingItemName || 'Item'} added! 🧋`, 'success');
    }

    /* ── CART UI ── */
    function updateCartUI() {
        const emptyCart    = document.getElementById('empty-cart');
        const cartItemsList = document.getElementById('cart-items');
        const totalEl      = document.getElementById('cart-total');
        const itemsHeader  = document.getElementById('cart-items-header');
        const cartCount    = document.getElementById('cart-count');

        if (cart.length > 0) {
            emptyCart.style.display     = 'none';
            cartItemsList.style.display = 'block';
            if (itemsHeader)  { itemsHeader.style.display = 'block'; }
            if (cartCount)    { cartCount.style.display = 'inline'; cartCount.innerText = cart.length; }

            cartItemsList.innerHTML = '';
            let total = 0;
            cart.forEach((item, index) => {
                total += item.price;
                const sizeLabel  = item.size === 'Regular' ? '' : ` · ${escapeHTML(item.size)}`;
                const addonLabel = item.addons && item.addons.length > 0 ? ` · ${escapeHTML(item.addons.join(', '))}` : '';
                const tempMeta   = item.size === 'Regular' ? '' : `${escapeHTML(item.sugar)} · ${escapeHTML(item.ice)}`;

                cartItemsList.innerHTML += `
                    <div class="cart-item">
                        <div class="item-details">
                            <h4>${escapeHTML(item.name)}<span style="color:var(--toffee); font-weight:500;">${sizeLabel}</span></h4>
                            <div class="item-meta">${tempMeta}${addonLabel}</div>
                            <button class="item-remove" onclick="removeFromCart(${index})">
                                <i class="fas fa-trash-alt"></i> Remove
                            </button>
                        </div>
                        <div class="item-price">₱${item.price.toFixed(2)}</div>
                    </div>
                `;
            });
            totalEl.innerText = `₱${total.toFixed(2)}`;
        } else {
            emptyCart.style.display     = 'flex';
            emptyCart.style.flexDirection = 'column';
            emptyCart.style.alignItems  = 'center';
            cartItemsList.style.display = 'none';
            if (itemsHeader) { itemsHeader.style.display = 'none'; }
            if (cartCount)   { cartCount.style.display = 'none'; }
            totalEl.innerText = '₱0.00';
        }
        checkCheckoutStatus();
    }

    function removeFromCart(index) { cart.splice(index, 1); updateCartUI(); }

    function checkCheckoutStatus() {
        const btn        = document.getElementById('checkout-btn');
        const name       = document.getElementById('customer-name').value.trim();
        const pickupTime = document.getElementById('pickup-time').value;
        btn.className    = (cart.length > 0 && name !== '' && pickupTime !== '') ? 'checkout-btn active' : 'checkout-btn';
    }

    /* ── SUBMIT ORDER ── */
    async function submitOrder() {
        const btn        = document.getElementById('checkout-btn');
        const name       = document.getElementById('customer-name').value.trim();
        const pickupTime = document.getElementById('pickup-time').value;
        if (cart.length === 0 || name === '' || pickupTime === '') return;

        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing…';
        btn.className = 'checkout-btn';

        const total       = cart.reduce((sum, item) => sum + item.price, 0);
        const mappedItems = cart.map(item => ({
            foundation: item.name, size: item.size,
            addons: item.addons ? item.addons.join(', ') : '',
            sweetener: item.sugar || 'Standard',
            ice: item.ice || 'Normal Ice',
            pearls: orderType, price: item.price
        }));
        const payload = {
            name, email: "{{ session.get('customer_email', 'google_user@9599.local') }}",
            total, items: mappedItems, pickup_time: pickupTime
        };

        try {
            const response = await fetch('/reserve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await response.json();
            if (response.ok) {
                document.getElementById('display-code').innerText = result.reservation_code;
                document.getElementById('success-modal').style.display = 'flex';
                let myOrders = JSON.parse(localStorage.getItem('myOrders')) || [];
                myOrders.push({ code: result.reservation_code, status: 'Preparing Order' });
                if (myOrders.length > 5) myOrders = myOrders.slice(-5);
                localStorage.setItem('myOrders', JSON.stringify(myOrders));
                cart = [];
                document.getElementById('pickup-time').value = '';
                updateCartUI(); fetchMenu();
            } else {
                if (response.status === 429) {
                    showToast("⚠️ " + (result.message || "Please wait before ordering again."), "error");
                } else {
                    showToast("Order Error: " + (result.message || "Failed to process order."), "error");
                }
                fetchMenu();
            }
        } catch (error) {
            showToast("Connection error. Please check your internet.", "error");
        } finally {
            btn.innerHTML = '<i class="fas fa-paper-plane"></i> Place My Order';
            checkCheckoutStatus();
        }
    }

    function closeModal() { document.getElementById('success-modal').style.display = 'none'; }
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
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#3E2723">
    
    <link rel="apple-touch-icon" href="/static/images/9599.jpg">
    <link rel="icon" href="/static/images/9599.jpg">
    
    <title>9599 Admin POS</title>
    
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Chart.js for Professional Financial Analytics -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

    <style>
        /* CSS Reset */
        * { 
            box-sizing: border-box; 
            margin: 0; 
            padding: 0; 
            font-family: 'Poppins', sans-serif; 
        }
        body { 
            background-color: #F5EFE6; 
            color: #3E2723; 
            display: flex; 
            height: 100vh; 
            overflow: hidden; 
        }
        
        /* Toast Notifications (Professional UI) */
        #toast-container {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            display: flex;
            flex-direction: column;
            gap: 10px;
            align-items: center;
        }
        .toast {
            background-color: #3E2723;
            color: #fff;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.9rem;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            animation: slideUp 0.3s ease forwards;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .toast.error { background-color: #C62828; }
        .toast.success { background-color: #388E3C; }
        .toast.fade-out { animation: fadeOut 0.3s ease forwards; }
        @keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes fadeOut { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(20px); } }

        /* Sidebar Navigation */
        .sidebar { 
            width: 220px; 
            background: white; 
            border-right: 1px solid #EFEBE4; 
            display: flex; 
            flex-direction: column; 
            justify-content: space-between; 
            flex-shrink: 0; 
            z-index: 100; 
        }
        .sidebar-header { 
            padding-top: 30px;
            padding-bottom: 30px;
            padding-left: 25px;
            padding-right: 25px;
            font-weight: 800; 
            font-size: 1.1rem; 
            color: #3E2723; 
        }
        .nav-links { 
            flex: 1; 
            display: flex; 
            flex-direction: column; 
        }
        .nav-item { 
            padding-top: 15px;
            padding-bottom: 15px;
            padding-left: 25px;
            padding-right: 25px;
            color: #8D6E63; 
            text-decoration: none; 
            font-weight: 600; 
            font-size: 0.95rem; 
            display: flex; 
            align-items: center; 
            gap: 12px; 
            cursor: pointer; 
            transition: all 0.2s; 
            border-left: 4px solid transparent; 
        }
        .nav-item:hover { 
            background: #FDFBF7; 
            color: #6F4E37; 
        }
        .nav-item.active { 
            background: #F5EFE6; 
            color: #3E2723; 
            border-left: 4px solid #6F4E37; 
            font-weight: 700; 
        }
        .nav-item i { 
            font-size: 1.1rem; 
            width: 20px; 
            text-align: center; 
        }
        .sidebar-footer { 
            padding-top: 25px;
            padding-bottom: 25px;
            padding-left: 25px;
            padding-right: 25px;
            border-top: 1px solid #EFEBE4; 
            display: flex; 
            flex-direction: column; 
            gap: 10px; 
        }
        .btn-reload { 
            width: 100%; 
            background: #F5EFE6; 
            color: #5D4037; 
            border: none; 
            padding-top: 12px;
            padding-bottom: 12px;
            padding-left: 12px;
            padding-right: 12px;
            border-radius: 8px; 
            font-weight: 700; 
            cursor: pointer; 
            transition: background 0.2s; 
        }
        .btn-reload:hover { 
            background: #D7CCC8; 
        }
        .btn-logout { 
            width: 100%; 
            background: #FFEBEE; 
            color: #C62828; 
            border: 1px solid #FFCDD2; 
            padding-top: 12px;
            padding-bottom: 12px;
            padding-left: 12px;
            padding-right: 12px;
            border-radius: 8px; 
            font-weight: 700; 
            cursor: pointer; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            gap: 8px; 
        }
        
        /* Main Layout */
        .main-content { 
            flex: 1; 
            display: flex; 
            flex-direction: column; 
            overflow: hidden; 
        }
        .topbar { 
            background: white; 
            padding-top: 20px;
            padding-bottom: 20px;
            padding-left: 40px;
            padding-right: 40px;
            border-bottom: 1px solid #EFEBE4; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            flex-shrink: 0; 
            z-index: 90; 
        }
        .page-title { 
            font-size: 1.4rem; 
            font-weight: 800; 
            color: #0F172A; 
        }
        .topbar-actions { 
            display: flex; 
            align-items: center; 
            gap: 20px; 
        }
        .btn-action { 
            background: white; 
            border: 1px solid #D7CCC8; 
            padding-top: 8px;
            padding-bottom: 8px;
            padding-left: 16px;
            padding-right: 16px;
            border-radius: 20px; 
            color: #5D4037; 
            font-weight: 600; 
            font-size: 0.85rem; 
            display: flex; 
            align-items: center; 
            gap: 8px; 
            cursor: pointer; 
            text-decoration: none; 
        }
        .time-badge { 
            background: #F5EFE6; 
            color: #3E2723; 
            font-weight: 800; 
            padding-top: 8px;
            padding-bottom: 8px;
            padding-left: 16px;
            padding-right: 16px;
            border-radius: 20px; 
            font-size: 0.9rem; 
            letter-spacing: 0.5px; 
        }
        .content-body { 
            padding: 25px; 
            overflow: hidden; 
            flex: 1; 
            display: flex; 
            flex-direction: column; 
        }
        
        /* Tab System */
        .tab-pane { 
            display: none; 
            height: 100%; 
            flex-direction: column; 
            min-height: 0; 
        }
        .tab-pane.active { 
            display: flex; 
            animation: fadeIn 0.3s ease; 
        }
        @keyframes fadeIn { 
            from { opacity: 0; transform: translateY(5px); } 
            to { opacity: 1; transform: translateY(0); } 
        }

        /* Generic Admin Components */
        .settings-grid-layout { 
            display: grid; 
            grid-template-columns: 340px 1fr; 
            gap: 20px; 
            height: 100%; 
            min-height: 0; 
        }
        .settings-card { 
            background: white; 
            border-radius: 12px; 
            padding: 25px; 
            border: 1px solid #EFEBE4; 
            box-shadow: 0 4px 6px rgba(111, 78, 55, 0.05); 
            display: flex; 
            flex-direction: column; 
            min-height: 0; 
            margin-bottom: 20px; 
        }
        .card-title { 
            font-size: 1rem; 
            font-weight: 800; 
            color: #3E2723; 
            text-transform: uppercase; 
            margin-bottom: 15px; 
            flex-shrink: 0; 
        }
        .desc-text { 
            color: #8D6E63; 
            font-size: 0.85rem; 
            line-height: 1.5; 
            margin-bottom: 20px; 
            flex-shrink: 0; 
        }
        .table-responsive { 
            flex: 1; 
            overflow-x: auto; 
            overflow-y: auto; 
            border: 1px solid #EFEBE4; 
            border-radius: 8px; 
        }
        
        /* Input Overrides */
        .input-group label { 
            display: block; 
            font-size: 0.75rem; 
            font-weight: 800; 
            color: #8D6E63; 
            margin-bottom: 8px; 
            text-transform: uppercase; 
        }
        .input-with-icon input, .input-pin { 
            width: 100%; 
            padding: 12px 15px; 
            border: 1px solid #D7CCC8; 
            border-radius: 8px; 
            font-weight: 600; 
            color: #3E2723; 
            outline: none; 
            background: #FDFBF7; 
            margin-bottom: 15px;
        }
        .btn-blue { 
            width: 100%; 
            background: #6F4E37; 
            color: white; 
            border: none; 
            padding: 16px; 
            border-radius: 8px; 
            font-weight: 700; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            gap: 10px; 
            margin-bottom: 20px; 
        }
        .btn-dark { 
            background: #3E2723; 
            color: white; 
            border: none; 
            padding: 12px 16px; 
            border-radius: 8px; 
            font-weight: 700; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            gap: 6px; 
        }

        /* Kitchen Display System Table */
        .kds-table { 
            width: 100%; 
            border-collapse: collapse; 
            background: white; 
        }
        .kds-table th, .kds-table td { 
            padding: 15px 20px; 
            text-align: left; 
            border-bottom: 1px solid #EFEBE4; 
        }
        .kds-table th { 
            background: #F5EFE6; 
            color: #5D4037; 
            font-weight: 800; 
            font-size: 0.8rem; 
            position: sticky; 
            top: 0; 
            z-index:10;
        }
        .kds-badge { 
            font-size: 0.75rem; 
            padding-top: 2px;
            padding-bottom: 2px;
            padding-left: 8px;
            padding-right: 8px;
            border-radius: 4px; 
            font-weight: 600; 
            display: inline-block; 
            margin-bottom: 2px;
        }

        /* Modals and Link Settings */
        .link-row { 
            display: flex; 
            width: 100%; 
            gap: 8px; 
            margin-bottom: 10px; 
            flex-shrink: 0; 
            box-sizing: border-box; 
        }
        .link-input { 
            flex: 1; 
            min-width: 0; 
            background: #F5EFE6; 
            border: 1px solid #D7CCC8; 
            padding: 12px; 
            border-radius: 8px; 
            color: #8D6E63; 
            font-family: monospace; 
            font-size: 0.85rem; 
            outline: none; 
            white-space: nowrap; 
        }
        .modal { 
            display: none; 
            position: fixed; 
            z-index: 1000; 
            left: 0; 
            top: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(62, 39, 35, 0.6); 
            align-items: center; 
            justify-content: center; 
        }
        .modal-content { 
            background: white; 
            padding: 30px; 
            border-radius: 12px; 
            width: 400px; 
            border: 2px solid #6F4E37; 
        }
        .btn-add-item { 
            background: #A67B5B; 
            color: white; 
            border: none; 
            padding: 10px 20px; 
            border-radius: 8px; 
            font-weight: 800; 
            cursor: pointer; 
            display: flex; 
            align-items: center; 
            gap: 8px; 
        }
        .action-btn { 
            border: none; 
            padding: 8px 12px; 
            border-radius: 6px; 
            cursor: pointer; 
            font-size: 0.85rem; 
            font-weight: 700;
        }

        /* No Scrollbar Utility */
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
        
        /* ============================================== */
        /* FEATURE 1: MANUAL ORDER (WALK-IN POS) STYLES   */
        /* ============================================== */
        .quick-order-layout { 
            display: grid; 
            grid-template-columns: 1fr 350px; 
            gap: 20px; 
            height: 100%; 
        }
        
        /* New Search Bar for Professional POS */
        .qo-search-bar {
            width: 100%;
            padding: 15px;
            border: 2px solid #D7CCC8;
            border-radius: 8px;
            font-size: 1rem;
            margin-bottom: 15px;
            font-family: 'Poppins', sans-serif;
            outline: none;
        }
        .qo-search-bar:focus { border-color: #6F4E37; }

        .qo-menu-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); 
            gap: 15px; 
            overflow-y: auto; 
            padding-right: 10px; 
            align-content: start;
        }
        .qo-card { 
            background: white; 
            border: 1px solid #D7CCC8; 
            border-radius: 12px; 
            padding: 15px; 
            text-align: center; 
            cursor: pointer; 
            transition: transform 0.1s; 
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        }
        .qo-card:active { 
            transform: scale(0.95); 
            background: #F5EFE6;
        }
        .qo-card h4 { 
            color: #3E2723; 
            font-size: 0.9rem; 
            margin-bottom: 5px;
        }
        .qo-card p { 
            color: #A67B5B; 
            font-weight: 800; 
            font-size: 1rem;
        }
        
        .qo-cart { 
            background: white; 
            border: 1px solid #D7CCC8; 
            border-radius: 12px; 
            display: flex; 
            flex-direction: column; 
            overflow: hidden; 
        }
        .qo-cart-header { 
            background: #6F4E37; 
            color: white; 
            padding: 15px; 
            font-weight: 800; 
            text-align: center; 
        }
        .qo-cart-items { 
            flex: 1; 
            overflow-y: auto; 
            padding: 15px; 
        }
        .qo-cart-item { 
            display: flex; 
            justify-content: space-between; 
            margin-bottom: 15px; 
            border-bottom: 1px dashed #EFEBE4; 
            padding-bottom: 10px;
        }
        .qo-cart-footer { 
            padding: 20px; 
            background: #FDFBF7; 
            border-top: 1px solid #D7CCC8; 
        }
        .qo-total-row { 
            display: flex; 
            justify-content: space-between; 
            font-size: 1.2rem; 
            font-weight: 800; 
            margin-bottom: 15px; 
            color: #3E2723;
        }
        .btn-qo-checkout { 
            width: 100%; 
            background: #388E3C; 
            color: white; 
            padding: 15px; 
            border-radius: 8px; 
            font-weight: 800; 
            font-size: 1.1rem; 
            border: none; 
            cursor: pointer; 
        }

        /* Printable Receipt Modal Styles - THERMAL PRINTER READY */
        .receipt-modal { 
            background: white; 
            width: 300px; 
            padding: 20px; 
            border-radius: 0; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5); 
            font-family: 'Courier New', Courier, monospace; 
            color: black; 
        }
        .receipt-header { 
            text-align: center; 
            margin-bottom: 15px; 
            border-bottom: 1px dashed black; 
            padding-bottom: 10px; 
        }
        .receipt-body { 
            font-size: 0.85rem; 
            margin-bottom: 15px; 
            border-bottom: 1px dashed black; 
            padding-bottom: 10px; 
        }
        .receipt-footer { 
            text-align: center; 
            font-size: 0.8rem; 
            font-weight: bold; 
        }
        
        /* Print Media Query for Thermal Printer formatting */
        @media print {
            body * { visibility: hidden; }
            #receipt-modal, #receipt-modal * { visibility: visible; }
            #receipt-modal { 
                position: absolute; 
                left: 0; 
                top: 0; 
                width: 100%; 
                margin: 0; 
                padding: 0; 
                background: none; 
            }
            .receipt-modal { 
                box-shadow: none; 
                width: 100%; 
            }
            .btn-dark, .btn-blue, button { display: none !important; }
        }

        /* Checkbox styling */
        .addon-label {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            border: 1px solid #D7CCC8;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            color: #3E2723;
            background: #FDFBF7;
        }
        .addon-checkbox {
            transform: scale(1.3);
        }

        /* ============================================== */
        /* FEATURE 2: FINANCE & EXPENSES STYLES           */
        /* ============================================== */
        .finance-grid { 
            display: grid; 
            grid-template-columns: 1fr 1fr; 
            gap: 20px; 
            height: 100%;
        }
        .fin-box { 
            background: #FDFBF7; 
            border: 1px solid #D7CCC8; 
            padding: 20px; 
            border-radius: 12px; 
            margin-bottom: 20px;
        }
        .fin-stat { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 15px; 
            font-size: 1.1rem; 
            font-weight: 600; 
            color: #5D4037;
        }
        .fin-stat span.val { 
            font-weight: 800; 
            color: #3E2723; 
            font-size: 1.2rem; 
        }
        .fin-stat input { 
            width: 120px; 
            text-align: right; 
            font-size: 1.1rem; 
            padding: 8px; 
            border-radius: 6px; 
            border: 1px solid #A67B5B; 
            font-weight: 800;
        }
        .fin-total { 
            font-size: 1.5rem; 
            font-weight: 800; 
            color: #388E3C; 
            margin-top: 15px; 
            border-top: 2px dashed #D7CCC8; 
            padding-top: 15px; 
            display: flex; 
            justify-content: space-between;
        }
    </style>
</head>
<body>

    <div id="toast-container"></div>

    <aside class="sidebar">
        <div>
            <div class="sidebar-header">
                Admin Mode<br>
                <small style="font-size: 0.7rem; color: #A67B5B;">9599 TEA & COFFEE</small>
            </div>
            <nav class="nav-links">
                <div class="nav-item active" onclick="switchTab('kds', 'Live Orders', this)">
                    <i class="fas fa-clipboard-list"></i> Orders
                </div>
                <div class="nav-item" onclick="switchTab('quick-order', 'Manual POS', this)">
                    <i class="fas fa-cash-register"></i> Manual POS
                </div>
                <div class="nav-item" onclick="switchTab('inventory', 'Inventory', this)">
                    <i class="fas fa-boxes"></i> Inventory
                </div>
                <div class="nav-item" onclick="switchTab('finance', 'Finance & Reports', this)">
                    <i class="fas fa-chart-line"></i> Finance
                </div>
                <div class="nav-item" onclick="switchTab('audit', 'Audit Trail', this)">
                    <i class="fas fa-list-ol"></i> Audit Trail
                </div>
                <div class="nav-item" onclick="switchTab('settings', 'Settings & Menu', this)">
                    <i class="fas fa-sliders-h"></i> Settings
                </div>
            </nav>
        </div>
        <div class="sidebar-footer">
            <button class="btn-reload" onclick="location.reload()">Reload UI</button>
            <button class="btn-logout" onclick="location.href='/logout'">
                <i class="fas fa-sign-out-alt"></i> Lock
            </button>
        </div>
    </aside>

    <main class="main-content">
        <header class="topbar">
            <div class="page-title" id="page-title">Live Orders</div>
            <div class="topbar-actions">
                <a href="/" target="_blank" class="btn-action">
                    <i class="fas fa-external-link-square-alt"></i> Customer POS
                </a>
                <div class="time-badge" id="clock">00:00 PM</div>
            </div>
        </header>

        <div class="content-body">
            
            <!-- ============================================== -->
            <!-- LIVE ORDERS (KDS)                              -->
            <!-- ============================================== -->
            <div id="tab-kds" class="tab-pane active">
                <div class="table-responsive">
                    <table class="kds-table">
                        <thead>
                            <tr>
                                <th>Order #</th>
                                <th>Source</th>
                                <th>Customer Name</th>
                                <th>Total</th>
                                <th>Items Ordered</th>
                                <th>Status Manager</th>
                            </tr>
                        </thead>
                        <tbody id="kds-table-body"></tbody>
                    </table>
                </div>
            </div>

            <!-- ============================================== -->
            <!-- FEATURE 1: MANUAL ORDER (WALK-IN POS)          -->
            <!-- ============================================== -->
            <div id="tab-quick-order" class="tab-pane">
                <div class="quick-order-layout">
                    <div style="display:flex; flex-direction:column; overflow:hidden;">
                        <!-- Custom Search Filter -->
                        <input type="text" id="qo-search" class="qo-search-bar" placeholder="🔍 Search for a drink or snack..." onkeyup="filterQuickOrderMenu()">
                        
                        <div class="qo-menu-grid" id="qo-menu-grid">
                            <!-- Populated by JS -->
                        </div>
                    </div>
                    
                    <div class="qo-cart">
                        <div class="qo-cart-header">Manual Entry Cart</div>
                        <div class="qo-cart-items" id="qo-cart-items">
                            <div style="text-align:center; color:#A67B5B; margin-top:20px;">Cart is empty.</div>
                        </div>
                        <div class="qo-cart-footer">
                            <div class="qo-total-row">
                                <span>Total</span>
                                <span id="qo-total-display">₱0.00</span>
                            </div>
                            <button class="btn-qo-checkout" onclick="submitQuickOrder()">Save Manual Order</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ============================================== -->
            <!-- INVENTORY                                      -->
            <!-- ============================================== -->
            <div id="tab-inventory" class="tab-pane">
                <div class="settings-card" style="height: 100%;">
                    <div class="card-title">Raw Ingredient Inventory</div>
                    <p class="desc-text">Update raw materials here. If a required ingredient hits 0, drinks relying on it become "Sold Out".</p>
                    <div class="table-responsive" style="margin-top: 0;">
                        <table class="kds-table">
                            <thead>
                                <tr>
                                    <th>Ingredient Name</th>
                                    <th>Unit</th>
                                    <th style="width: 150px; text-align: center;">Stock Remaining</th>
                                </tr>
                            </thead>
                            <tbody id="admin-inventory-list">
                                <!-- Populated via JS -->
                            </tbody>
                        </table>
                    </div>
                    <button class="btn-blue" style="margin-top: 20px; flex-shrink: 0;" onclick="saveInventory()">
                        <i class="fas fa-save"></i> Save Inventory Updates
                    </button>
                </div>
            </div>

            <!-- ============================================== -->
            <!-- FEATURE 2: FINANCE & EXPENSES                  -->
            <!-- ============================================== -->
            <div id="tab-finance" class="tab-pane">
                <div class="finance-grid">
                    
                    <div style="display:flex; flex-direction:column; min-height:0; overflow-y:auto;">
                        
                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-calculator"></i> Daily Close-Out Reconciliation</div>
                            <p class="desc-text">Reconcile the system's verified digital orders with any manual notebook walk-ins and petty cash expenses.</p>
                            
                            <div class="fin-box">
                                <div class="fin-stat">
                                    System Total (Online + Manual) 
                                    <span class="val" id="sys-total" data-value="0">₱0.00</span>
                                </div>
                                <div class="fin-stat">
                                    Manual Notebook Total
                                    <input type="number" id="notebook-total" placeholder="0.00" oninput="calculateReconciliation()">
                                </div>
                                <div class="fin-stat" style="color:#C62828;">
                                    Minus: Total Expenses 
                                    <span class="val" style="color:#C62828;" id="expense-total" data-value="0">- ₱0.00</span>
                                </div>
                                
                                <div class="fin-total">
                                    Actual Profit (Cash in Drawer)
                                    <span id="cash-drawer">₱0.00</span>
                                </div>
                            </div>
                            <button class="btn-dark" onclick="fetchDailyFinances()">
                                <i class="fas fa-sync"></i> Refresh Finance Data
                            </button>
                        </div>

                        <!-- Professional Chart.js Analytics Container -->
                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-chart-pie"></i> Profit Analytics</div>
                            <canvas id="financeChart" width="400" height="200"></canvas>
                        </div>
                    </div>

                    <div style="display:flex; flex-direction:column; min-height:0; overflow-y:auto;">
                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-receipt"></i> Petty Cash / Expense Tracker</div>
                            <p class="desc-text">Log daily expenses like buying ice, extra milk, or supplies here. It auto-deducts from the Close-Out.</p>
                            
                            <div class="input-group">
                                <label>Expense Description</label>
                                <input type="text" id="exp-desc" class="input-pin" style="text-align:left; letter-spacing:0; font-size:1rem;" placeholder="e.g. Bought 5kg Ice">
                            </div>
                            <div class="input-group">
                                <label>Amount (₱)</label>
                                <input type="number" id="exp-amount" class="input-pin" style="text-align:left; letter-spacing:0; font-size:1rem;" placeholder="0.00">
                            </div>
                            <button class="btn-blue" style="background:#A67B5B;" onclick="addExpense()">Record Expense</button>

                            <div style="margin-top:20px; border-top:1px solid #EFEBE4; padding-top:15px;">
                                <h4 style="color:#5D4037; margin-bottom:10px; font-size:0.9rem;">Today's Logged Expenses</h4>
                                <div id="expense-list" style="font-size:0.85rem; color:#8D6E63;">
                                    <!-- Populated by JS -->
                                </div>
                            </div>
                        </div>
                        
                        <!-- Data-Driven Menu Trends -->
                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-fire"></i> Data-Driven Menu Trends</div>
                            <p class="desc-text">Analyze what your customers love most to make smart inventory decisions.</p>
                            <canvas id="menuTrendsChart" width="400" height="200" style="margin-bottom: 20px;"></canvas>
                            <div id="best-sellers-list" style="font-size:0.85rem; color:#8D6E63;">
                                <!-- JS populated -->
                            </div>
                        </div>

                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-history"></i> Legacy Data Migration</div>
                            <p class="desc-text">Upload your past Excel/CSV records to inject legacy sales into the system.</p>
                            <input type="file" id="legacy-csv" accept=".csv" style="margin-bottom: 15px; padding:10px; border:1px dashed #D7CCC8; width:100%;">
                            <button class="btn-dark" onclick="uploadLegacyCSV()">Import CSV Records</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ============================================== -->
            <!-- FEATURE 3: AUDIT TRAIL (TRANSPARENCY)          -->
            <!-- ============================================== -->
            <div id="tab-audit" class="tab-pane">
                <div class="settings-card" style="height: 100%;">
                    <div class="card-title">System Audit Trail</div>
                    <p class="desc-text">Maximum Transparency: Tracks every order, expense, and stock change in real-time to prevent shrinkage and ensure accountability.</p>
                    <div class="table-responsive" style="margin-top: 0;">
                        <table class="kds-table">
                            <thead>
                                <tr>
                                    <th>Timestamp</th>
                                    <th>Action</th>
                                    <th>Details</th>
                                </tr>
                            </thead>
                            <tbody id="audit-table-body">
                                <!-- Populated via JS -->
                            </tbody>
                        </table>
                    </div>
                    <button class="btn-dark" style="margin-top: 20px; flex-shrink: 0;" onclick="fetchAuditLogs()">
                        <i class="fas fa-sync"></i> Refresh Audit Logs
                    </button>
                </div>
            </div>
            
            <!-- ============================================== -->
            <!-- SETTINGS & MENU                                -->
            <!-- ============================================== -->
            <div id="tab-settings" class="tab-pane">
                <div class="settings-grid-layout">
                    
                    <div class="no-scrollbar" style="display: flex; flex-direction: column; min-height: 0; overflow-y: auto; padding-right: 5px;">
                        
                        <!-- Link Gen -->
                        <div class="settings-card" style="flex-shrink: 0; border-left: 6px solid #6F4E37;">
                            <div class="card-title">Store Configuration & Link</div>
                            <p class="desc-text">Configure your store's hours and PIN to enable ordering. Customers cannot order until you generate a valid link.</p>
                            
                            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:15px;">
                                <div class="input-group">
                                    <label>Opening Time</label>
                                    <input type="time" id="store-open" class="input-pin" style="margin-bottom:0; font-size:1rem; letter-spacing:0;">
                                </div>
                                <div class="input-group">
                                    <label>Closing Time</label>
                                    <input type="time" id="store-close" class="input-pin" style="margin-bottom:0; font-size:1rem; letter-spacing:0;">
                                </div>
                            </div>
                            
                            <div class="input-group">
                                <label>Store Access PIN</label>
                                <input type="password" id="store-pin" class="input-pin" placeholder="Enter PIN">
                            </div>
                            
                            <button class="btn-blue" onclick="saveConfigurations()">
                                <i class="fas fa-bolt"></i> Validate & Generate Link
                            </button>
                            
                            <div class="link-row">
                                <input type="text" class="link-input" id="posLink" value="Pending Configuration..." readonly>
                                <button class="btn-dark" onclick="copyLink()"><i class="far fa-copy"></i> Copy</button>
                            </div>
                            <div style="text-align:center; font-size:0.8rem; font-family:monospace; color:#A1887F;" id="expiration-text">
                                • Expires: Pending Generation
                            </div>
                        </div>

                        <!-- Backup / Restore -->
                        <div class="settings-card" style="flex-shrink: 0; border-left: 6px solid #A67B5B;">
                            <div class="card-title"><i class="fas fa-database"></i> Backup & Recovery</div>
                            <p class="desc-text">Download a complete backup of your system (Inventory, Menu, Orders). You can use this file to restore the system later.</p>
                            
                            <button class="btn-blue" onclick="downloadBackup()" style="margin-bottom: 15px; background: #A67B5B;">
                                <i class="fas fa-download"></i> Download System Backup
                            </button>
                            
                            <div style="border-top: 1px solid #EFEBE4; margin: 15px 0;"></div>
                            
                            <p class="desc-text" style="margin-bottom: 10px; font-weight:600;">Restore from Backup</p>
                            <input type="file" id="backup-file" accept=".json" style="width:100%; padding: 10px; border: 1px dashed #D7CCC8; margin-bottom: 15px;">
                            
                            <button class="btn-dark" style="width: 100%;" onclick="restoreBackup()">
                                <i class="fas fa-upload"></i> Upload & Restore System
                            </button>
                        </div>
                    </div>

                    <!-- Menu List -->
                    <div class="settings-card">
                        <div style="display:flex; justify-content:space-between; align-items:center; flex-shrink: 0; margin-bottom: 15px; border-bottom: 2px solid #F5EFE6; padding-bottom: 15px;">
                            <div class="card-title" style="margin-bottom:0;">Menu Management</div>
                            <button class="btn-add-item" onclick="openMenuModal()">
                                <i class="fas fa-plus-circle"></i> Add New Item
                            </button>
                        </div>
                        <div class="table-responsive">
                            <table class="kds-table" style="font-size: 0.85rem;">
                                <thead>
                                    <tr>
                                        <th>Item Name</th>
                                        <th>Category</th>
                                        <th>Price</th>
                                        <th style="text-align: right;">Actions</th>
                                    </tr>
                                </thead>
                                <tbody id="admin-menu-list">
                                    <!-- Populated via JS -->
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <!-- Modal for Menu Add/Edit -->
    <div id="menu-modal" class="modal">
        <div class="modal-content">
            <h2 style="font-weight: 800; font-size: 1.2rem; margin-bottom: 20px; color: #3E2723;" id="menu-modal-title">Add Menu Item</h2>
            <form id="menu-form" onsubmit="saveMenuItem(event)">
                <div class="input-group">
                    <label>Item Name</label>
                    <input type="text" id="menu-name" class="input-pin" style="font-size:1rem; letter-spacing:0;" required>
                </div>
                <div class="input-group">
                    <label>Price (₱)</label>
                    <input type="number" step="0.01" id="menu-price" class="input-pin" style="font-size:1rem; letter-spacing:0;" required>
                </div>
                <div class="input-group">
                    <label>Category</label>
                    <input type="text" id="menu-category" class="input-pin" style="font-size:1rem; letter-spacing:0;" required>
                </div>
                <div class="input-group">
                    <label>Display Letter (1-2 Chars)</label>
                    <input type="text" id="menu-letter" class="input-pin" maxlength="2" style="font-size:1rem; letter-spacing:0;">
                </div>
                <div style="display:flex; gap:10px; margin-top:10px;">
                    <button type="button" class="btn-dark" style="flex:1; background:#EFEBE4; color:#3E2723;" onclick="closeMenuModal()">Cancel</button>
                    <button type="submit" class="btn-blue" style="flex:1; margin-bottom:0;">Save Item</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Size Selection Modal for Quick Order -->
    <div id="qo-size-modal" class="modal">
        <div class="modal-content" style="text-align:left;">
            <h2 id="qo-size-modal-title" style="text-align:center; margin-bottom: 20px;">Select Options</h2>
            
            <label style="font-weight:800; color:#8D6E63; font-size:0.8rem; text-transform:uppercase;">1. Choose Size</label>
            <div style="display:flex; gap:10px; margin-top:5px; margin-bottom:20px;">
                <button id="btn-size-16" style="flex:1; padding: 15px; font-size: 1rem; border:2px solid #6F4E37; background:#6F4E37; color:white; border-radius:8px; font-weight:700; cursor:pointer;" onclick="selectSize('16 oz', 49)">16 oz<br>₱49.00</button>
                <button id="btn-size-22" style="flex:1; padding: 15px; font-size: 1rem; border:2px solid #D7CCC8; background:white; color:#3E2723; border-radius:8px; font-weight:700; cursor:pointer;" onclick="selectSize('22 oz', 59)">22 oz<br>₱59.00</button>
            </div>

            <div style="display:flex; gap:10px; margin-bottom:20px;">
                <div style="flex:1;">
                    <label style="font-weight:800; color:#8D6E63; font-size:0.8rem; text-transform:uppercase;">Sugar Level</label>
                    <select id="qo-sugar-level-select" class="name-input" style="margin-top:5px; margin-bottom:0;">
                        <option value="100% Sugar">100% (Normal)</option>
                        <option value="75% Sugar">75% (Less)</option>
                        <option value="50% Sugar">50% (Half)</option>
                        <option value="25% Sugar">25% (Slight)</option>
                        <option value="0% Sugar">0% (No Sugar)</option>
                    </select>
                </div>
                <div style="flex:1;">
                    <label style="font-weight:800; color:#8D6E63; font-size:0.8rem; text-transform:uppercase;">Ice Level</label>
                    <select id="qo-ice-level-select" class="name-input" style="margin-top:5px; margin-bottom:0;">
                        <option value="Normal Ice">Normal Ice</option>
                        <option value="Less Ice">Less Ice</option>
                        <option value="No Ice">No Ice</option>
                        <option value="Extra Ice">Extra Ice</option>
                    </select>
                </div>
            </div>

            <label style="font-weight:800; color:#8D6E63; font-size:0.8rem; text-transform:uppercase;">Add-ons</label>
            <div style="display:flex; flex-direction:column; gap:10px; margin-top:5px;">
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Nata"> Nata (+₱10)</label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Pearl"> Pearl (+₱10)</label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Coffee Jelly"> Coffee Jelly (+₱10)</label>
                <label class="addon-label"><input type="checkbox" class="addon-checkbox" value="Cloud Foam"> Cloud Foam (+₱15)</label>
            </div>

            <div style="display:flex; gap:10px; margin-top:25px;">
                <button class="btn-dark" style="flex:1; width:auto; margin-top:0;" onclick="closeQOSizeModal()">Cancel</button>
                <button class="btn-blue" style="flex:2; margin-bottom:0;" onclick="confirmQuickCart()">Add to Cart</button>
            </div>
        </div>
    </div>

    <!-- Printable Receipt Modal for Manual POS -->
    <div id="receipt-modal" class="modal">
        <div class="receipt-modal">
            <div class="receipt-header">
                <h3>9599 Tea & Coffee</h3>
                <p>Official Receipt</p>
                <p id="receipt-date"></p>
            </div>
            <div class="receipt-body" id="receipt-body-content">
                <!-- Items populated by JS -->
            </div>
            <div class="receipt-footer">
                <p style="font-size:1.2rem; font-weight:800; border-top:1px dashed black; padding-top:10px;">Total: <span id="receipt-total-display"></span></p>
                <p style="margin-top:15px; font-weight:normal;">Thank You!</p>
                <div style="display:flex; gap:10px; margin-top:20px;">
                    <button onclick="closeReceiptModal()" style="flex:1; padding:10px; background:#D7CCC8; color:#3E2723; border:none; cursor:pointer;">Close</button>
                    <button onclick="printReceipt()" style="flex:2; padding:10px; background:#6F4E37; color:white; border:none; cursor:pointer; font-weight:bold;">🖨️ Print Receipt</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Admin JavaScript Logic -->
    <script>
        // Custom Professional Toast Notifications
        function showToast(message, type='info') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            let icon = type === 'error' ? 'fa-exclamation-circle' : 'fa-check-circle';
            toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
            container.appendChild(toast);
            setTimeout(() => { 
                toast.classList.add('fade-out'); 
                setTimeout(() => toast.remove(), 300); 
            }, 3000);
        }

        function escapeHTML(str) { 
            let div = document.createElement('div'); 
            div.innerText = str; 
            return div.innerHTML; 
        }
        
        function updateTime() {
            const now = new Date();
            let hours = now.getHours() % 12 || 12;
            let minutes = now.getMinutes() < 10 ? '0' + now.getMinutes() : now.getMinutes();
            let ampm = now.getHours() >= 12 ? 'PM' : 'AM';
            document.getElementById('clock').innerText = hours + ':' + minutes + ' ' + ampm;
        }
        
        setInterval(updateTime, 1000); 
        updateTime();

        // ----------------------------------------
        // AUDIO NOTIFICATION SYSTEM
        // ----------------------------------------
        let audioCtx = null;
        
        function initAudio() {
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }
        }
        
        // Browsers block audio until user interaction. We listen to clicks to wake up the audio engine.
        document.addEventListener('click', initAudio, { once: true });
        document.addEventListener('touchstart', initAudio, { once: true });

        function playNotificationSound() {
            try {
                if (!audioCtx) initAudio();
                if (audioCtx && audioCtx.state === 'suspended') return;
                
                function playTone(freq, time, duration) {
                    const osc = audioCtx.createOscillator();
                    const gain = audioCtx.createGain();
                    osc.type = 'sine';
                    osc.frequency.setValueAtTime(freq, audioCtx.currentTime + time);
                    gain.gain.setValueAtTime(0.5, audioCtx.currentTime + time);
                    gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + time + duration);
                    osc.connect(gain);
                    gain.connect(audioCtx.destination);
                    osc.start(audioCtx.currentTime + time);
                    osc.stop(audioCtx.currentTime + time + duration);
                }
                
                // Plays a "Ding-Dong" sound
                playTone(880, 0, 0.3);     // A5 Note
                playTone(659.25, 0.2, 0.5); // E5 Note
            } catch (e) {
                console.error("Audio playback error:", e);
            }
        }

        // ----------------------------------------
        // NAVIGATION (TABS)
        // ----------------------------------------
        function switchTab(tabId, title, btnElement) {
            initAudio(); // Wake up audio context if clicking navigation
            
            document.querySelectorAll('.tab-pane').forEach(tab => {
                tab.classList.remove('active');
            });
            
            document.querySelectorAll('.nav-item').forEach(btn => {
                btn.classList.remove('active');
            });
            
            document.getElementById('tab-' + tabId).classList.add('active');
            btnElement.classList.add('active');
            document.getElementById('page-title').innerText = title;
            
            if (tabId === 'kds') {
                fetchLiveOrders();
            }
            if (tabId === 'quick-order') {
                fetchQuickOrderMenu();
            }
            if (tabId === 'inventory') {
                fetchAdminInventory();
            }
            if (tabId === 'finance') {
                fetchDailyFinances();
            }
            if (tabId === 'audit') {
                fetchAuditLogs();
            }
            if (tabId === 'settings') {
                fetchAdminMenu(); 
            }
        }

        // ----------------------------------------
        // KDS / LIVE ORDERS
        // ----------------------------------------
        function getStatusClass(status) {
            if (status === 'Preparing Order') {
                return 'background: #FFF3E0; color: #E65100; border: 1px solid #FFE0B2;';
            }
            if (status === 'Completed') {
                return 'background: #F5EFE6; color: #8D6E63; border: 1px solid #D7CCC8;';
            }
            return 'background: #E8F5E9; color: #2E7D32; border: 1px solid #C8E6C9;';
        }

        let knownOrderIds = new Set();
        let isInitialLoad = true;

        async function fetchLiveOrders() {
            try {
                const response = await fetch('/api/orders?_t=' + new Date().getTime());
                const data = await response.json();
                const tbody = document.getElementById('kds-table-body');
                tbody.innerHTML = '';
                
                if (data.orders.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 40px; color:#A1887F; font-weight:600;">No active orders right now. Waiting for customers...</td></tr>';
                    isInitialLoad = false;
                    return;
                }

                let hasNewOrder = false;

                data.orders.forEach(order => {
                    // Check if this is a newly received order that we haven't seen before
                    if (!isInitialLoad && !knownOrderIds.has(order.id) && order.status === 'Preparing Order') {
                        hasNewOrder = true;
                    }
                    // Memorize the order ID
                    knownOrderIds.add(order.id);

                    let itemsHtml = order.items.map(i => {
                        let sizeDisplay = i.size === 'Regular' ? '' : ` (${escapeHTML(i.size)})`;
                        let addonDisplay = i.addons ? `<br><span style="color:#D97706; font-size:0.7rem;">+ ${escapeHTML(i.addons)}</span>` : '';
                        let sugarIceDisplay = i.size === 'Regular' ? '' : `<br><span style="color:#8D6E63; font-size:0.65rem;">${escapeHTML(i.sweetener)} | ${escapeHTML(i.ice)}</span>`;
                        return `<div style="margin-bottom:8px; border-bottom:1px dashed #EFEBE4; padding-bottom:4px;"><strong>${escapeHTML(i.foundation)}${sizeDisplay}</strong>${sugarIceDisplay}${addonDisplay} <span class="kds-badge" style="background:#EFEBE4; color:#5D4037; float:right;">${escapeHTML(i.pearls)}</span></div>`;
                    }).join('');
                    
                    let sourceBadge = '';
                    if (order.source === 'Manual/Notebook') {
                        sourceBadge = `<span class="kds-badge" style="background:#3E2723; color:white;">Manual POS</span>`;
                    } else {
                        sourceBadge = `<span class="kds-badge" style="background:#388E3C; color:white;">Online QR</span>`;
                    }
                    
                    let selectHtml = `
                        <select style="padding:8px 12px; border-radius:20px; font-weight:bold; font-family:'Poppins'; font-size:0.8rem; text-transform:uppercase; outline:none; ${getStatusClass(order.status)}" onchange="updateOrderStatus(${order.id}, this.value)">
                            <option value="Preparing Order" ${order.status === 'Preparing Order' ? 'selected' : ''}>Preparing</option>
                            <option value="Completed" ${order.status === 'Completed' ? 'selected' : ''}>Completed</option>
                        </select>
                    `;
                    
                    tbody.innerHTML += `
                        <tr>
                            <td style="font-family:monospace; font-weight:700; font-size:1.1rem; color:#5D4037;">${escapeHTML(order.code)}</td>
                            <td>${sourceBadge}</td>
                            <td style="font-weight:800; color:#3E2723;">${escapeHTML(order.name)}</td>
                            <td style="font-weight:700; color:#D97706;">₱${order.total.toFixed(2)}</td>
                            <td>${itemsHtml}</td>
                            <td>${selectHtml}</td>
                        </tr>
                    `;
                });
                
                // Trigger audio if new order is found
                if (hasNewOrder) {
                    playNotificationSound();
                    showToast("🔔 New Order Received!", "success");
                }
                
                isInitialLoad = false;

            } catch (error) { 
                console.error(error); 
            }
        }

        async function updateOrderStatus(orderId, newStatus) {
            try {
                await fetch(`/api/orders/${orderId}/status`, {
                    method: 'PUT', 
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status: newStatus })
                });
                showToast(`Order status updated to ${newStatus}`, 'success');
                fetchLiveOrders();
            } catch (error) {
                showToast("Error updating order status", 'error');
            }
        }
        
        setInterval(fetchLiveOrders, 3000);
        fetchLiveOrders();

        // ----------------------------------------
        // FEATURE 1: MANUAL ORDER (WALK-IN POS)
        // ----------------------------------------
        let quickCart =[];
        let pendingQOItemName = "";
        let pendingSize = '16 oz';
        let pendingPrice = 49;
        let adminMenuItems =[]; // cache for search
        
        async function fetchQuickOrderMenu() {
            try {
                const res = await fetch('/api/menu?_t=' + new Date().getTime());
                adminMenuItems = await res.json();
                renderQuickOrderMenu(adminMenuItems);
            } catch(e) {
                console.error("Error fetching menu for Quick Order.", e);
            }
        }
        
        function filterQuickOrderMenu() {
            const query = document.getElementById('qo-search').value.toLowerCase();
            const filtered = adminMenuItems.filter(i => i.name.toLowerCase().includes(query) || i.category.toLowerCase().includes(query));
            renderQuickOrderMenu(filtered);
        }
        
        function renderQuickOrderMenu(items) {
            const grid = document.getElementById('qo-menu-grid');
            grid.innerHTML = '';
            
            if (items.length === 0) {
                grid.innerHTML = '<div style="grid-column:1/-1; text-align:center; color:#A1887F;">No items found.</div>';
                return;
            }
            
            items.forEach(i => {
                let priceDisplay = i.category === 'Snacks' ? `₱${i.price.toFixed(2)}` : '₱49 / ₱59';
                let onClick = `onclick="addToQuickCart('${escapeHTML(i.name.replace(/'/g, "\\'"))}', '${escapeHTML(i.category.replace(/'/g, "\\'"))}', ${i.price})"`;
                
                grid.innerHTML += `
                    <div class="qo-card" ${onClick}>
                        <h4>${escapeHTML(i.name)}</h4>
                        <p>${priceDisplay}</p>
                    </div>
                `;
            });
        }

        function selectSize(size, price) {
            pendingSize = size;
            pendingPrice = price;
            
            document.getElementById('btn-size-16').style.background = size === '16 oz' ? '#6F4E37' : 'white';
            document.getElementById('btn-size-16').style.color = size === '16 oz' ? 'white' : '#3E2723';
            document.getElementById('btn-size-16').style.borderColor = size === '16 oz' ? '#6F4E37' : '#D7CCC8';
            
            document.getElementById('btn-size-22').style.background = size === '22 oz' ? '#388E3C' : 'white';
            document.getElementById('btn-size-22').style.color = size === '22 oz' ? 'white' : '#3E2723';
            document.getElementById('btn-size-22').style.borderColor = size === '22 oz' ? '#388E3C' : '#D7CCC8';
        }

        function addToQuickCart(name, category, basePrice) {
            if (category === 'Snacks') {
                quickCart.push({ 
                    name: name, 
                    size: 'Regular',
                    addons:[],
                    sugar: 'N/A',
                    ice: 'N/A',
                    price: basePrice 
                });
                updateQuickCartUI();
            } else {
                pendingQOItemName = name;
                document.getElementById('qo-size-modal-title').innerText = name;
                
                selectSize('16 oz', 49);
                document.getElementById('qo-sugar-level-select').value = "100% Sugar";
                document.getElementById('qo-ice-level-select').value = "Normal Ice";
                document.querySelectorAll('.addon-checkbox').forEach(cb => cb.checked = false);
                
                document.getElementById('qo-size-modal').style.display = 'flex';
            }
        }

        function closeQOSizeModal() {
            document.getElementById('qo-size-modal').style.display = 'none';
            pendingQOItemName = "";
        }

        function confirmQuickCart() {
            let addons =[];
            let addonCost = 0;
            
            document.querySelectorAll('#qo-size-modal .addon-checkbox').forEach(cb => {
                if(cb.checked) {
                    addons.push(cb.value);
                    addonCost += (cb.value === 'Cloud Foam' ? 15 : 10);
                }
            });

            quickCart.push({ 
                name: pendingQOItemName, 
                size: pendingSize,
                sugar: document.getElementById('qo-sugar-level-select').value,
                ice: document.getElementById('qo-ice-level-select').value,
                addons: addons,
                price: pendingPrice + addonCost 
            });
            
            updateQuickCartUI();
            closeQOSizeModal();
        }

        function removeQuickCart(index) {
            quickCart.splice(index, 1);
            updateQuickCartUI();
        }

        function updateQuickCartUI() {
            const list = document.getElementById('qo-cart-items');
            let total = 0;
            list.innerHTML = '';
            
            if (quickCart.length === 0) {
                list.innerHTML = '<div style="text-align:center; color:#A67B5B; margin-top:20px;">Cart is empty.</div>';
            } else {
                quickCart.forEach((item, index) => {
                    total += item.price;
                    let sizeLabel = item.size === 'Regular' ? '' : ` <span style="font-size:0.7rem; color:#6F4E37;">(${escapeHTML(item.size)})</span>`;
                    let addonLabel = item.addons && item.addons.length > 0 ? `<br><span style="font-size:0.7rem; color:#A67B5B;">+ ${escapeHTML(item.addons.join(', '))}</span>` : '';
                    let tempDetails = item.size === 'Regular' ? '' : `<br><span style="font-size:0.65rem; color:#8D6E63;">${escapeHTML(item.sugar)} | ${escapeHTML(item.ice)}</span>`;
                    
                    list.innerHTML += `
                        <div class="qo-cart-item">
                            <div>
                                <div style="font-weight:700; color:#3E2723; font-size:0.9rem;">${escapeHTML(item.name)}${sizeLabel}${tempDetails}${addonLabel}</div>
                                <div style="font-size:0.75rem; color:#C62828; cursor:pointer;" onclick="removeQuickCart(${index})">Remove</div>
                            </div>
                            <div style="font-weight:800; color:#A67B5B;">₱${item.price.toFixed(2)}</div>
                        </div>
                    `;
                });
            }
            document.getElementById('qo-total-display').innerText = '₱' + total.toFixed(2);
        }

        async function submitQuickOrder() {
            if(quickCart.length === 0) {
                return showToast("Cart is empty", "error");
            }
            
            const total = quickCart.reduce((sum, item) => sum + item.price, 0);
            
            const payload = { 
                items: quickCart.map(i => ({ 
                    foundation: i.name, 
                    size: i.size,
                    addons: i.addons ? i.addons.join(', ') : '',
                    sweetener: i.sugar || 'Standard',
                    ice: i.ice || 'Normal Ice',
                    price: i.price 
                })), 
                total: total 
            };
            
            try {
                const res = await fetch('/api/admin/manual_order', {
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                if(res.ok) {
                    const data = await res.json();
                    
                    // Show Receipt
                    document.getElementById('receipt-date').innerText = new Date().toLocaleString();
                    document.getElementById('receipt-total-display').innerText = "₱" + total.toFixed(2);
                    
                    let receiptBody = "";
                    quickCart.forEach(item => {
                        let sizeLabel = item.size === 'Regular' ? '' : ` (${escapeHTML(item.size)})`;
                        let addonLabel = item.addons && item.addons.length > 0 ? `<br><small>+ ${escapeHTML(item.addons.join(', '))}</small>` : '';
                        let tempLabel = item.size === 'Regular' ? '' : `<br><small>${escapeHTML(item.sugar)} | ${escapeHTML(item.ice)}</small>`;
                        
                        receiptBody += `<div style="margin-bottom:10px;"><div style="display:flex; justify-content:space-between; font-weight:bold;"><span>1x ${escapeHTML(item.name)}${sizeLabel}</span><span>${item.price.toFixed(2)}</span></div>${tempLabel}${addonLabel}</div>`;
                    });
                    
                    document.getElementById('receipt-body-content').innerHTML = receiptBody;
                    document.getElementById('receipt-modal').style.display = 'flex';
                    
                    // Clear state
                    quickCart =[];
                    updateQuickCartUI();
                    fetchLiveOrders();
                    showToast("Walk-In Order Saved!", "success");
                } else {
                    const data = await res.json();
                    showToast("Error: " + data.message, "error");
                }
            } catch(e) { 
                showToast("Connection Error. Check the server.", "error"); 
            }
        }

        function closeReceiptModal() {
            document.getElementById('receipt-modal').style.display = 'none';
        }
        
        function printReceipt() {
            window.print();
        }

        // ----------------------------------------
        // INVENTORY
        // ----------------------------------------
        async function fetchAdminInventory() {
            try {
                const res = await fetch('/api/inventory?_t=' + new Date().getTime());
                const ings = await res.json();
                const tbodyInv = document.getElementById('admin-inventory-list');
                tbodyInv.innerHTML = '';
                
                ings.forEach(i => {
                    tbodyInv.innerHTML += `
                        <tr>
                            <td style="font-weight:700; color:#3E2723;">${escapeHTML(i.name)}</td>
                            <td style="color:#8D6E63; font-weight: 600;">${escapeHTML(i.unit)}</td>
                            <td style="text-align: center;">
                                <input type="number" class="input-pin stock-input" style="padding: 8px; font-size: 1rem; letter-spacing: 1px; margin: 0; width: 120px;" data-id="${i.id}" value="${i.stock}" min="0" step="0.1">
                            </td>
                        </tr>
                    `;
                });
            } catch (error) { 
                console.error(error); 
            }
        }

        async function saveInventory() {
            const inputs = document.querySelectorAll('.stock-input');
            const payload = Array.from(inputs).map(input => ({
                id: parseInt(input.getAttribute('data-id')),
                stock: parseFloat(input.value)
            }));
            
            try {
                const res = await fetch('/api/inventory', {
                    method: 'PUT', 
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                if(res.ok) { 
                    showToast("Inventory updated successfully!", "success"); 
                    fetchAdminInventory(); 
                } else { 
                    showToast("Error saving inventory.", "error"); 
                }
            } catch(e) { 
                showToast("Connection error.", "error"); 
            }
        }

        // ----------------------------------------
        // FEATURE 2: FINANCE & RECONCILIATION
        // ----------------------------------------
        let financeChartInstance = null;
        let menuTrendsChartInstance = null;
        
        async function fetchDailyFinances() {
            try {
                const res = await fetch('/api/finance/daily');
                const data = await res.json();
                
                document.getElementById('sys-total').innerText = '₱' + data.system_total.toFixed(2);
                document.getElementById('sys-total').dataset.value = data.system_total;
                
                document.getElementById('expense-total').innerText = '- ₱' + data.expenses_total.toFixed(2);
                document.getElementById('expense-total').dataset.value = data.expenses_total;

                const list = document.getElementById('expense-list');
                if(data.expenses.length === 0) {
                    list.innerHTML = '<div style="text-align:center;">No expenses logged today.</div>';
                } else {
                    let htmlString = '';
                    data.expenses.forEach(e => {
                        htmlString += `
                            <div style="display:flex; justify-content:space-between; margin-bottom:5px; padding-bottom:5px; border-bottom:1px dashed #EFEBE4;">
                                <span>${escapeHTML(e.desc)}</span>
                                <span style="font-weight:700; color:#C62828;">₱${e.amount.toFixed(2)}</span>
                            </div>
                        `;
                    });
                    list.innerHTML = htmlString;
                }
                
                calculateReconciliation();
                fetchMenuTrends(); // NEW: Call the menu trends analyzer
                
            } catch(e) { 
                console.error("Finance fetch error", e); 
            }
        }

        function calculateReconciliation() {
            const sysTotal = parseFloat(document.getElementById('sys-total').dataset.value || 0);
            const noteTotal = parseFloat(document.getElementById('notebook-total').value || 0);
            const expTotal = parseFloat(document.getElementById('expense-total').dataset.value || 0);
            
            const final = (sysTotal + noteTotal) - expTotal;
            document.getElementById('cash-drawer').innerText = '₱' + final.toFixed(2);
            
            // Update chart dynamically if manual notebook changes
            updateChart(sysTotal + noteTotal, expTotal);
        }

        function updateChart(totalRevenue, totalExpenses) {
            const ctx = document.getElementById('financeChart').getContext('2d');
            const netProfit = totalRevenue - totalExpenses;
            
            if(financeChartInstance) {
                financeChartInstance.destroy();
            }
            
            financeChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels:['Gross Revenue', 'Expenses', 'Net Profit'],
                    datasets:[{
                        label: 'Daily Financials (₱)',
                        data:[totalRevenue, totalExpenses, netProfit],
                        backgroundColor:[
                            'rgba(56, 142, 60, 0.7)', // Green
                            'rgba(198, 40, 40, 0.7)', // Red
                            'rgba(111, 78, 55, 0.7)'  // Brown
                        ],
                        borderColor:[
                            'rgba(56, 142, 60, 1)',
                            'rgba(198, 40, 40, 1)',
                            'rgba(111, 78, 55, 1)'
                        ],
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    scales: {
                        y: { beginAtZero: true }
                    }
                }
            });
        }
        
        // NEW: Data-Driven Menu Trends
        async function fetchMenuTrends() {
            try {
                const res = await fetch('/api/finance/menu_trends');
                const data = await res.json();
                
                const labels = data.map(d => d.name);
                const counts = data.map(d => d.sold_count);
                
                // Update Top 10 List
                const list = document.getElementById('best-sellers-list');
                if(data.length === 0) {
                    list.innerHTML = '<div style="text-align:center;">No sales data yet.</div>';
                } else {
                    let htmlString = '';
                    data.forEach((item, index) => {
                        let rank = index + 1;
                        htmlString += `
                            <div style="display:flex; justify-content:space-between; margin-bottom:8px; padding-bottom:8px; border-bottom:1px dashed #EFEBE4;">
                                <span><strong>#${rank}</strong> ${escapeHTML(item.name)}</span>
                                <span style="font-weight:700; color:#388E3C;">${item.sold_count} sold (₱${item.total_revenue.toFixed(2)})</span>
                            </div>
                        `;
                    });
                    list.innerHTML = htmlString;
                }
                
                // Update Doughnut Chart
                const ctx = document.getElementById('menuTrendsChart').getContext('2d');
                if(menuTrendsChartInstance) {
                    menuTrendsChartInstance.destroy();
                }
                menuTrendsChartInstance = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        labels: labels,
                        datasets:[{
                            data: counts,
                            backgroundColor:[
                                '#6F4E37', '#A67B5B', '#D7CCC8', '#388E3C', '#F59E0B', 
                                '#C62828', '#1E3A8A', '#4A148C', '#047857', '#B45309'
                            ]
                        }]
                    },
                    options: { responsive: true, maintainAspectRatio: false }
                });
                
            } catch(e) {
                console.error("Error fetching menu trends", e);
            }
        }

        async function addExpense() {
            const desc = document.getElementById('exp-desc').value;
            const amt = document.getElementById('exp-amount').value;
            
            if(!desc || !amt) {
                return showToast("Fill in description and amount.", "error");
            }
            
            try {
                const res = await fetch('/api/expenses', {
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ description: desc, amount: amt })
                });
                
                if (res.ok) {
                    document.getElementById('exp-desc').value = '';
                    document.getElementById('exp-amount').value = '';
                    showToast("Expense logged securely.", "success");
                    fetchDailyFinances();
                } else {
                    showToast("Error saving expense.", "error");
                }
            } catch(e) { 
                showToast("Error saving expense. Connection dropped.", "error"); 
            }
        }

        async function uploadLegacyCSV() {
            const fileInput = document.getElementById('legacy-csv');
            if(!fileInput.files.length) {
                return showToast("Select a CSV file first.", "error");
            }
            
            const formData = new FormData();
            formData.append("file", fileInput.files[0]);
            
            try {
                const res = await fetch('/api/import_legacy', { 
                    method: 'POST', 
                    body: formData 
                });
                
                const result = await res.json();
                
                if(res.ok) {
                    showToast("Success: " + result.message, "success");
                } else {
                    showToast("Import Error: " + (result.error || "Check file format"), "error");
                }
            } catch(e) { 
                showToast("Connection Error. Are you online?", "error"); 
            }
        }

        // ----------------------------------------
        // AUDIT TRAIL
        // ----------------------------------------
        async function fetchAuditLogs() {
            try {
                const res = await fetch('/api/audit_logs?_t=' + new Date().getTime());
                const logs = await res.json();
                const tbody = document.getElementById('audit-table-body');
                tbody.innerHTML = '';
                
                if (logs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;">No audit logs available.</td></tr>';
                    return;
                }
                
                logs.forEach(log => {
                    tbody.innerHTML += `
                        <tr>
                            <td style="color:#8D6E63; font-size:0.8rem;">${escapeHTML(log.time)}</td>
                            <td style="font-weight:700; color:#3E2723;">${escapeHTML(log.action)}</td>
                            <td style="color:#5D4037;">${escapeHTML(log.details)}</td>
                        </tr>
                    `;
                });
            } catch(e) {
                console.error("Error fetching audit logs", e);
            }
        }

        // ----------------------------------------
        // SETTINGS & MENU MANAGEMENT
        // ----------------------------------------
        let editingItemId = null;
        
        async function fetchAdminMenu() {
            try {
                const res = await fetch('/api/menu?_t=' + new Date().getTime());
                const items = await res.json();
                const tbodyMenu = document.getElementById('admin-menu-list');
                tbodyMenu.innerHTML = '';
                
                if (items.length === 0) { 
                    tbodyMenu.innerHTML = '<tr><td colspan="4" style="text-align:center;">No items in menu.</td></tr>'; 
                    return; 
                }
                
                items.forEach(i => {
                    tbodyMenu.innerHTML += `
                        <tr>
                            <td style="font-weight:700; color:#3E2723;">
                                <span class="kds-badge" style="background:#EFEBE4; color:#5D4037; margin-right:5px;">
                                    ${escapeHTML(i.letter)}
                                </span> 
                                ${escapeHTML(i.name)}
                            </td>
                            <td style="color:#8D6E63;">${escapeHTML(i.category)}</td>
                            <td style="color:#D97706; font-weight:700;">₱${i.price.toFixed(2)}</td>
                            <td style="text-align: right; white-space: nowrap;">
                                <button class="action-btn" style="background:#EFEBE4; color:#6F4E37; margin-right:5px;" onclick="editMenu(${i.id}, '${escapeHTML(i.name.replace(/'/g, "\\'"))}', ${i.price}, '${escapeHTML(i.category.replace(/'/g, "\\'"))}', '${escapeHTML(i.letter)}')">
                                    <i class="fas fa-edit"></i> Edit
                                </button>
                                <button class="action-btn" style="background:#FFEBEE; color:#C62828;" onclick="deleteMenu(${i.id})">
                                    <i class="fas fa-trash-alt"></i>
                                </button>
                            </td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error("Error loading menu", error);
            }
        }

        function openMenuModal() {
            editingItemId = null; 
            document.getElementById('menu-form').reset();
            document.getElementById('menu-modal-title').innerText = "Add Menu Item";
            document.getElementById('menu-modal').style.display = 'flex';
        }

        function closeMenuModal() { 
            document.getElementById('menu-modal').style.display = 'none'; 
        }

        function editMenu(id, name, price, category, letter) {
            editingItemId = id;
            document.getElementById('menu-name').value = name; 
            document.getElementById('menu-price').value = price;
            document.getElementById('menu-category').value = category; 
            document.getElementById('menu-letter').value = letter;
            
            document.getElementById('menu-modal-title').innerText = "Edit Menu Item";
            document.getElementById('menu-modal').style.display = 'flex';
        }

        async function saveMenuItem(e) {
            e.preventDefault();
            const payload = {
                name: document.getElementById('menu-name').value, 
                price: parseFloat(document.getElementById('menu-price').value),
                category: document.getElementById('menu-category').value, 
                letter: document.getElementById('menu-letter').value
            };
            
            const method = editingItemId ? 'PUT' : 'POST';
            const url = editingItemId ? `/api/menu/${editingItemId}` : '/api/menu';
            
            try {
                await fetch(url, { 
                    method: method, 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify(payload) 
                });
                
                showToast("Menu Item Saved!", "success");
                closeMenuModal(); 
                fetchAdminMenu();
            } catch (error) {
                showToast("Error saving menu item", "error");
            }
        }

        async function deleteMenu(id) {
            if (confirm("Delete this menu item?")) {
                try {
                    await fetch(`/api/menu/${id}`, { method: 'DELETE' }); 
                    showToast("Menu Item Deleted", "success");
                    fetchAdminMenu();
                } catch (error) {
                    showToast("Error deleting menu item", "error");
                }
            }
        }

        function formatTimeTo12Hr(time24) {
            let[h, m] = time24.split(':'); 
            h = parseInt(h);
            let ampm = h >= 12 ? 'PM' : 'AM'; 
            h = h % 12 || 12;
            return `${h < 10 ? '0'+h : h}:${m} ${ampm}`;
        }

        async function saveConfigurations() {
            const openTimeVal = document.getElementById('store-open').value;
            const closeTimeVal = document.getElementById('store-close').value;
            const pin = document.getElementById('store-pin').value;
            
            if (!openTimeVal || !closeTimeVal || !pin) {
                return showToast("⚠️ Configure Opening Time, Closing Time, and PIN.", "error");
            }
            
            try {
                const res = await fetch('/api/generate_link', {
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        open: formatTimeTo12Hr(openTimeVal), 
                        close: formatTimeTo12Hr(closeTimeVal), 
                        pin: pin 
                    }) 
                });
                
                const data = await res.json();
                
                if (res.ok) {
                    document.getElementById('posLink').value = data.url;
                    
                    const[hours, minutes] = closeTimeVal.split(':');
                    const expiryDate = new Date(); 
                    expiryDate.setHours(parseInt(hours), parseInt(minutes), 0, 0);
                    
                    if (expiryDate < new Date()) {
                        expiryDate.setDate(expiryDate.getDate() + 1);
                    }
                    
                    document.getElementById('expiration-text').innerHTML = `
                        <span style="color:#C62828; font-weight:800;">• Expires: ${expiryDate.toLocaleString()}</span>
                    `;
                    
                    showToast('Settings validated! Link Generated.', "success");
                } else { 
                    showToast("Error: " + data.error, "error"); 
                }
            } catch (e) { 
                showToast("Error connecting to server.", "error"); 
            }
        }

        function copyLink() {
            var copyText = document.getElementById("posLink");
            
            if (copyText.value === "Pending Configuration..." || copyText.value === "") {
                return showToast("⚠️ Generate link first.", "error");
            }
            
            copyText.select(); 
            copyText.setSelectionRange(0, 99999);
            navigator.clipboard.writeText(copyText.value); 
            showToast("Link Copied!", "success");
        }

        function downloadBackup() { 
            window.location.href = "/api/backup"; 
        }

        async function restoreBackup() {
            const fileInput = document.getElementById('backup-file');
            
            if (!fileInput.files.length) {
                return showToast("⚠️ Select a backup file (.json).", "error");
            }
            
            if (!confirm("⚠️ WARNING: This overwrites EVERYTHING with the backup data. Proceed?")) {
                return;
            }
            
            const formData = new FormData(); 
            formData.append("file", fileInput.files[0]);
            
            try {
                const res = await fetch('/api/restore', { 
                    method: 'POST', 
                    body: formData 
                });
                
                if (res.ok) { 
                    alert("✅ System restored!"); 
                    location.reload(); 
                } else { 
                    const data = await res.json(); 
                    showToast("Error: " + data.message, "error"); 
                }
            } catch (e) { 
                showToast("Connection error.", "error"); 
            }
        }
    </script>
</body>
</html>
"""

# ==========================================
# 5. REST API ROUTES & FLASK LOGIC
# ==========================================

@app.route('/')
def storefront():
    """
    Renders the customer-facing digital menu.
    Requires a valid cryptographic token generated by the admin to ensure
    the store is actually open and accepting orders.
    """
    token = request.args.get('token')
    
    blocked_html = """
    <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5EFE6; flex-direction:column; text-align:center; padding: 20px; font-family: 'Poppins', sans-serif;">
        <i class="fas fa-store-slash" style="font-size:4rem; color:#D7CCC8; margin-bottom:20px;"></i>
        <h2 style="color:#3E2723; margin-bottom: 10px;">Store Not Configured</h2>
        <p style="color:#8D6E63;">You cannot access this site directly. Please scan the official ordering QR code provided by the cashier.</p>
    </div>
    """
    
    expired_html = """
    <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5EFE6; flex-direction:column; text-align:center; padding: 20px; font-family: 'Poppins', sans-serif;">
        <i class="fas fa-lock" style="font-size:4rem; color:#D7CCC8; margin-bottom:20px;"></i>
        <h2 style="color:#3E2723; margin-bottom: 10px;">Ordering Link Expired</h2>
        <p style="color:#8D6E63;">This link has expired for security reasons. Please ask the staff for a new ordering link.</p>
    </div>
    """

    if not token:
        return blocked_html, 403
        
    try:
        data = token_serializer.loads(token) 
        open_time = data.get('open', '06:00 AM')
        close_time = data.get('close', '07:00 PM')
        
        current_ph_time = get_ph_time()
        
        try:
            closing_dt = datetime.strptime(close_time, '%I:%M %p')
            expiry_dt = current_ph_time.replace(
                hour=closing_dt.hour, 
                minute=closing_dt.minute, 
                second=0, 
                microsecond=0
            )
            
            if current_ph_time > expiry_dt:
                return expired_html, 403
        except Exception as e:
            token_serializer.loads(token, max_age=3600)
            
    except (SignatureExpired, BadSignature):
        return expired_html, 403
        
    return render_template_string(
        STOREFRONT_HTML, 
        open_time=open_time, 
        close_time=close_time,
        google_client_id=GOOGLE_CLIENT_ID
    )

@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    """
    Handles Google OAuth 2.0 Sign-In.
    Verifies the JWT token securely directly with Google's endpoints.
    """
    data = request.json
    token = data.get('token')
    
    if not token:
        return jsonify({"error": "No token provided"}), 400
        
    try:
        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={token}"
        response = requests.get(verify_url)
        
        if response.status_code == 200:
            user_info = response.json()
            
            if user_info.get('aud') != GOOGLE_CLIENT_ID and GOOGLE_CLIENT_ID != 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com':
                return jsonify({"error": "Invalid Client ID Verification"}), 401
                
            session['customer_verified'] = True
            session['customer_name'] = user_info.get('name', 'Valued Patron')
            session['customer_email'] = user_info.get('email', 'google_user@9599.local')
            
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "Invalid Google Token"}), 401
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    """
    Administrative gateway. Validates the master PIN hash.
    """
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        
        if check_password_hash(ADMIN_PIN_HASH, pin):
            session['is_admin'] = True
            log_audit("Admin Login", "Successful login to dashboard")
            return redirect(url_for('admin_dashboard'))
        else:
            log_audit("Admin Login Failed", "Invalid PIN attempt")
            error = "Invalid PIN. Access Denied."
            
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def admin_logout():
    """
    Destroys the administrative session cookie.
    """
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin_dashboard():
    """
    Renders the Single-Page Application (SPA) for the Staff Dashboard.
    """
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))
    return render_template_string(ADMIN_HTML)

@app.route('/api/generate_link', methods=['POST'])
def generate_link():
    """
    Generates a secure, time-limited cryptographic URL that customers must use to order.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.json
    pin = data.get('pin')
    
    if not check_password_hash(ADMIN_PIN_HASH, pin):
        return jsonify({"error": "Invalid Store Access PIN"}), 401
    
    token = token_serializer.dumps({
        'open': data['open'], 
        'close': data['close']
    })
    
    log_audit("Store Open", f"Generated ordering link until {data['close']}")
    
    return jsonify({"url": f"{request.host_url}?token={token}"})

@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    """
    GET: Returns the complete menu to either the customer or admin frontend.
    POST: Admin only. Adds a new drink to the database.
    """
    if request.method == 'GET':
        items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
        menu_data =[]
        
        for i in items:
            available_portions = float('inf')
            
            if not i.recipe:
                available_portions = 50 
            else:
                for r in i.recipe:
                    if r.quantity_required > 0:
                        portions = r.ingredient.stock // r.quantity_required
                        if portions < available_portions: 
                            available_portions = portions
                            
            if available_portions == float('inf'): 
                available_portions = 0
                
            menu_data.append({
                "id": i.id, 
                "name": i.name, 
                "price": i.price, 
                "letter": i.letter, 
                "category": i.category, 
                "stock": int(max(0, available_portions))
            })
            
        return jsonify(menu_data)
    
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    if request.method == 'POST':
        data = request.json
        letter = data.get('letter', '').strip()
        
        if not letter: 
            letter = data['name'][0].upper() 
            
        new_item = MenuItem(
            name=data['name'], 
            price=float(data['price']), 
            letter=letter[:2].upper(), 
            category=data['category']
        )
        
        db.session.add(new_item)
        db.session.commit()
        
        log_audit("Menu Updated", f"Added item: {data['name']}")
        return jsonify({"status": "success"})

@app.route('/api/menu/<int:item_id>', methods=['PUT', 'DELETE'])
def handle_menu_item(item_id):
    """
    Admin only. Update (PUT) or delete (DELETE) a specific menu item.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    item = MenuItem.query.get_or_404(item_id)
    
    if request.method == 'PUT':
        data = request.json
        letter = data.get('letter', '').strip()
        
        if not letter: 
            letter = data['name'][0].upper()
            
        item.name = data['name']
        item.price = float(data['price'])
        item.letter = letter[:2].upper()
        item.category = data['category']
        
        db.session.commit()
        log_audit("Menu Updated", f"Edited item ID: {item_id}")
        return jsonify({"status": "success"})
        
    elif request.method == 'DELETE':
        item_name = item.name
        db.session.delete(item)
        db.session.commit()
        log_audit("Menu Updated", f"Deleted item: {item_name}")
        return jsonify({"status": "success"})

@app.route('/api/inventory', methods=['GET', 'PUT'])
def handle_inventory():
    """
    GET: Admin only. Retrieves current raw material stock levels.
    PUT: Admin only. Allows bulk updating of inventory numbers.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    if request.method == 'GET':
        ings = Ingredient.query.order_by(Ingredient.name).all()
        return jsonify([{
            "id": i.id, 
            "name": i.name, 
            "unit": i.unit, 
            "stock": i.stock
        } for i in ings])
        
    elif request.method == 'PUT':
        data = request.json
        try:
            for item_data in data:
                ing = Ingredient.query.get(item_data['id'])
                if ing: 
                    ing.stock = float(item_data['stock'])
            db.session.commit()
            log_audit("Inventory Adjusted", "Staff manually updated raw material stocks")
            return jsonify({"status": "success"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/reserve', methods=['POST'])
@limiter.limit("5 per minute")
def reserve_blend():
    """
    Customer Endpoint. Validates stock in real-time, deducts inventory immediately.
    """
    data = request.json
    
    try:
        customer_name = data.get('name', '').strip()
        
        active_order = Reservation.query.filter(
            Reservation.patron_name == customer_name,
            Reservation.status == 'Preparing Order',
            Reservation.order_source == 'Online'
        ).first()
        
        if active_order:
            return jsonify({
                "status": "error", 
                "message": "You currently have an active order being prepared. Please wait until it is completed."
            }), 429
            
        cooldown_time = get_ph_time() - timedelta(minutes=5)
        recent_order = Reservation.query.filter(
            Reservation.patron_name == customer_name,
            Reservation.created_at >= cooldown_time,
            Reservation.order_source == 'Online'
        ).first()
        
        if recent_order:
            return jsonify({
                "status": "error", 
                "message": "Order cooldown active. Please wait 5 minutes between placing new orders."
            }), 429

        for item in data['items']:
            menu_item = MenuItem.query.filter_by(name=item['foundation']).first()
            if menu_item:
                if menu_item.recipe:
                    for r in menu_item.recipe:
                        if r.ingredient.stock < r.quantity_required:
                            db.session.rollback()
                            return jsonify({
                                "status": "error", 
                                "message": f"Sorry, '{menu_item.name}' is out of stock! Missing {r.ingredient.name}."
                            }), 400
                    
                    for r in menu_item.recipe: 
                        r.ingredient.stock -= r.quantity_required
                        
                addons = item.get('addons', '')
                if 'Nata' in addons:
                    ing = Ingredient.query.filter_by(name='Nata').first()
                    if ing: ing.stock -= 30
                if 'Pearl' in addons:
                    ing = Ingredient.query.filter_by(name='Tapioca Pearls').first()
                    if ing: ing.stock -= 30
                if 'Coffee Jelly' in addons:
                    ing = Ingredient.query.filter_by(name='Coffee Jelly').first()
                    if ing: ing.stock -= 30
                if 'Cloud Foam' in addons:
                    ing = Ingredient.query.filter_by(name='Cloud Foam').first()
                    if ing: ing.stock -= 30
            else:
                db.session.rollback()
                return jsonify({
                    "status": "error", 
                    "message": f"Item '{item['foundation']}' no longer exists."
                }), 400

        raw_pickup_time = data.get('pickup_time', '').strip()
        formatted_pickup_time = 'ASAP' 
        
        if raw_pickup_time:
            try:
                pt_obj = datetime.strptime(raw_pickup_time, '%H:%M')
                formatted_pickup_time = pt_obj.strftime('%I:%M %p')
            except ValueError:
                formatted_pickup_time = raw_pickup_time

        new_reservation = Reservation(
            patron_name=customer_name, 
            patron_email=data['email'],
            total_investment=data['total'], 
            pickup_time=formatted_pickup_time,
            order_source="Online"
        )
        db.session.add(new_reservation)
        db.session.flush() 
        
        for item in data['items']:
            new_infusion = Infusion(
                reservation_id=new_reservation.id, 
                foundation=item['foundation'],
                sweetener=item.get('sweetener', 'Standard'), 
                pearls=item.get('pearls', 'Standard'), 
                cup_size=item.get('size', '16 oz'),
                addons=item.get('addons', ''),
                item_total=item['price']
            )
            db.session.add(new_infusion)
            
        db.session.commit()
        
        log_audit("Online Order Placed", f"Order #{new_reservation.reservation_code} for ₱{data['total']}")
        
        return jsonify({
            "status": "success", 
            "reservation_code": new_reservation.reservation_code
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"System Error: {str(e)}"}), 500

@app.route('/api/customer/status')
def customer_order_status():
    """
    Customer Polling Endpoint.
    """
    codes = request.args.get('codes', '')
    if not codes: 
        return jsonify([])
        
    code_list = codes.split(',')
    orders = Reservation.query.filter(Reservation.reservation_code.in_(code_list)).all()
    
    results =[{'code': o.reservation_code, 'status': o.status} for o in orders]
    return jsonify(results)

@app.route('/api/orders/<int:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    """
    Admin Only. Modifies the state of an active order (e.g., Prepared -> Completed).
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    order = Reservation.query.get_or_404(order_id)
    new_status = request.json.get('status', 'Completed')
    order.status = new_status
    
    db.session.commit()
    
    log_audit("Order Status Update", f"Order #{order.reservation_code} marked as {new_status}")
    return jsonify({"status": "success"})

@app.route('/api/orders')
def api_orders():
    """
    Admin Only. Returns the Live Kitchen Display System (KDS) array.
    Excludes purely historical 'Legacy Notebook' data.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
    
    all_reservations = Reservation.query.filter(
        Reservation.order_source != 'Legacy Notebook'
    ).order_by(Reservation.created_at.desc()).limit(50).all()
    
    orders_data =[]
    
    for res in all_reservations:
        items =[{'foundation': i.foundation, 'pearls': i.pearls, 'size': i.cup_size, 'addons': i.addons, 'sweetener': i.sweetener, 'ice': i.ice_level} for i in res.infusions]
        
        orders_data.append({
            'id': res.id, 
            'code': res.reservation_code, 
            'source': res.order_source,
            'name': res.patron_name, 
            'total': res.total_investment,
            'status': res.status, 
            'items': items
        })
        
    return jsonify({'orders': orders_data})

# ==========================================
# 6. FEATURE: MANUAL ORDER 
# ==========================================

@app.route('/api/admin/manual_order', methods=['POST'])
def admin_manual_order():
    """
    Admin Only. Walk-In POS functionality.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.json
    
    try:
        res = Reservation(
            patron_name="Walk-In Customer", 
            patron_email="walkin@9599.local",
            total_investment=data['total'], 
            pickup_time="Walk-In",
            status="Completed", 
            order_source="Manual/Notebook"
        )
        db.session.add(res)
        db.session.flush()

        for item in data['items']:
            inf = Infusion(
                reservation_id=res.id, 
                foundation=item['foundation'],
                sweetener=item.get('sweetener', '100% Sugar'), 
                ice_level=item.get('ice', 'Normal Ice'),
                pearls="Take-Out", 
                cup_size=item.get('size', '16 oz'),
                addons=item.get('addons', ''),
                item_total=item['price']
            )
            db.session.add(inf)

            menu_item = MenuItem.query.filter_by(name=item['foundation']).first()
            if menu_item and menu_item.recipe:
                for r in menu_item.recipe: 
                    r.ingredient.stock -= r.quantity_required
                    
            addons = item.get('addons', '')
            if 'Nata' in addons:
                ing = Ingredient.query.filter_by(name='Nata').first()
                if ing: ing.stock -= 30
            if 'Pearl' in addons:
                ing = Ingredient.query.filter_by(name='Tapioca Pearls').first()
                if ing: ing.stock -= 30
            if 'Coffee Jelly' in addons:
                ing = Ingredient.query.filter_by(name='Coffee Jelly').first()
                if ing: ing.stock -= 30
            if 'Cloud Foam' in addons:
                ing = Ingredient.query.filter_by(name='Cloud Foam').first()
                if ing: ing.stock -= 30

        db.session.commit()
        
        log_audit("Manual POS Order", f"Staff placed order for ₱{data['total']}")
        
        return jsonify({
            "status": "success", 
            "print_receipt": True
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# 7. FEATURE: EXPENSE & RECONCILIATION
# ==========================================

@app.route('/api/finance/daily', methods=['GET'])
def daily_finance():
    """
    Admin Only. The "Petty Cash Ledger" brain.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
    
    now = get_ph_time()
    
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    today_orders = Reservation.query.filter(
        Reservation.created_at >= start_of_day, 
        Reservation.created_at <= end_of_day,
        Reservation.order_source != 'Legacy Notebook'
    ).all()
    
    system_total = sum(o.total_investment for o in today_orders)
    
    today_expenses = Expense.query.filter(
        Expense.created_at >= start_of_day, 
        Expense.created_at <= end_of_day
    ).all()
    
    expenses_total = sum(e.amount for e in today_expenses)
    
    return jsonify({
        "system_total": system_total, 
        "expenses_total": expenses_total,
        "expenses":[
            {
                "id": e.id, 
                "desc": e.description, 
                "amount": e.amount
            } 
            for e in today_expenses
        ]
    })

@app.route('/api/finance/menu_trends', methods=['GET'])
def menu_trends():
    """
    Admin Only. NEW FEATURE: Data-Driven Menu Analytics.
    Analyzes all completed orders to find the Top Best Sellers and their revenue.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
    
    trends = db.session.query(
        Infusion.foundation,
        db.func.count(Infusion.id).label('sold_count'),
        db.func.sum(Infusion.item_total).label('total_revenue')
    ).join(Reservation).filter(
        Reservation.status == 'Completed'
    ).group_by(Infusion.foundation).order_by(db.func.count(Infusion.id).desc()).limit(10).all()
    
    results =[
        {
            "name": t.foundation, 
            "sold_count": t.sold_count, 
            "total_revenue": t.total_revenue
        } for t in trends
    ]
    
    return jsonify(results)

@app.route('/api/expenses', methods=['POST'])
def add_expense():
    """
    Admin Only. Writes an outward cash transaction into the digital ledger.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.json
    
    try:
        new_expense = Expense(
            description=data['description'], 
            amount=float(data['amount'])
        )
        db.session.add(new_expense)
        db.session.commit()
        
        log_audit("Petty Cash Logged", f"Spent ₱{data['amount']} on {data['description']}")
        
        return jsonify({"status": "success"})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

# ==========================================
# 8. FEATURE: LEGACY IMPORT & AUDIT LOGS
# ==========================================

@app.route('/api/import_legacy', methods=['POST'])
def import_legacy():
    """
    Admin Only. Massive bulk ingest logic.
    """
    if not session.get('is_admin'): 
        return jsonify({"error": "Unauthorized"}), 403
        
    if not pd: 
        return jsonify({
            "error": "The 'pandas' library is required to process imports. Please install it on the server."
        }), 500
        
    if 'file' not in request.files: 
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    if not file.filename.endswith('.csv'): 
        return jsonify({"error": "Only CSV files are supported currently."}), 400
        
    try:
        df = pd.read_csv(file)
        imported_count = 0
        
        for index, row in df.iterrows():
            try: 
                dt = pd.to_datetime(row.get('Date', get_ph_time()))
            except: 
                dt = get_ph_time()
            
            qty = int(row.get('Quantity', 1))
            price = float(row.get('Price', 0))
            total = qty * price
            item_name = str(row.get('Item Name', 'Legacy Unnamed Item'))

            res = Reservation(
                patron_name="Legacy Archive", 
                patron_email="legacy@notebook.local",
                total_investment=total, 
                status="Completed", 
                pickup_time="Historical",
                created_at=dt, 
                order_source="Legacy Notebook"
            )
            db.session.add(res)
            db.session.flush()

            for _ in range(qty):
                inf = Infusion(
                    reservation_id=res.id, 
                    foundation=item_name,
                    sweetener="Standard", 
                    pearls="Standard", 
                    cup_size="16 oz",
                    item_total=price
                )
                db.session.add(inf)
                
            imported_count += 1
            
        db.session.commit()
        log_audit("Legacy Import", f"Imported {imported_count} historical records")
        
        return jsonify({
            "status": "success", 
            "message": f"Successfully injected {imported_count} historical records!"
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error parsing file: {str(e)}"}), 500

@app.route('/api/audit_logs', methods=['GET'])
def get_audit_logs():
    """
    Admin Only. Retrieves the system's uneditable audit trail for tracking.
    """
    if not session.get('is_admin'):
        return jsonify([]), 403
        
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()
    results =[]
    
    for l in logs:
        results.append({
            "id": l.id,
            "action": l.action,
            "details": l.details,
            "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')
        })
        
    return jsonify(results)

# ==========================================
# 9. SYSTEM BACKUP AND RESTORE
# ==========================================

@app.route('/api/backup', methods=['GET'])
def export_backup():
    """
    Admin Only. Dumps the entire PostgreSQL relational structure into a flat JSON payload.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    data = {
        "ingredients":[
            {
                "id": i.id, 
                "name": i.name, 
                "unit": i.unit, 
                "stock": i.stock
            } 
            for i in Ingredient.query.all()
        ],
        "menu_items":[
            {
                "id": m.id, 
                "name": m.name, 
                "price": m.price, 
                "letter": m.letter, 
                "category": m.category
            } 
            for m in MenuItem.query.all()
        ],
        "recipe_items":[
            {
                "id": r.id, 
                "menu_item_id": r.menu_item_id, 
                "ingredient_id": r.ingredient_id, 
                "quantity_required": r.quantity_required
            } 
            for r in RecipeItem.query.all()
        ],
        "reservations":[
            {
                "id": r.id, 
                "reservation_code": r.reservation_code, 
                "patron_name": r.patron_name, 
                "patron_email": r.patron_email, 
                "total_investment": r.total_investment, 
                "status": r.status, 
                "pickup_time": r.pickup_time, 
                "order_source": r.order_source, 
                "created_at": r.created_at.isoformat()
            } 
            for r in Reservation.query.all()
        ],
        "infusions":[
            {
                "id": i.id, 
                "reservation_id": i.reservation_id, 
                "foundation": i.foundation, 
                "sweetener": i.sweetener,
                "ice_level": i.ice_level,
                "pearls": i.pearls, 
                "cup_size": i.cup_size,
                "addons": i.addons,
                "item_total": i.item_total
            } 
            for i in Infusion.query.all()
        ],
        "expenses":[
            {
                "id": e.id, 
                "description": e.description, 
                "amount": e.amount, 
                "created_at": e.created_at.isoformat()
            } 
            for e in Expense.query.all()
        ],
        "audit_logs":[
            {
                "id": a.id,
                "action": a.action,
                "details": a.details,
                "created_at": a.created_at.isoformat()
            }
            for a in AuditLog.query.all()
        ]
    }
    
    return Response(
        json.dumps(data, indent=4), 
        mimetype='application/json', 
        headers={
            'Content-Disposition': f'attachment;filename=9599_backup_{datetime.now().strftime("%Y%m%d")}.json'
        }
    )

@app.route('/api/restore', methods=['POST'])
def import_backup():
    """
    Admin Only. Takes a flat JSON file previously downloaded via the backup routine,
    aggressively truncates the existing databases, and reconstitutes the entire state.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
        
    if 'file' not in request.files: 
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
        
    file = request.files['file']
    
    if file.filename == '': 
        return jsonify({"status": "error", "message": "No file selected"}), 400
        
    try:
        data = json.load(file)
        
        db.session.execute(db.text('TRUNCATE TABLE audit_logs, expenses, infusions, reservations, recipe_items, menu_items, ingredients RESTART IDENTITY CASCADE;'))
        
        for i in data.get('ingredients',[]): 
            db.session.add(Ingredient(**i))
            
        for m in data.get('menu_items',[]): 
            db.session.add(MenuItem(**m))
            
        db.session.commit()
        
        for r in data.get('recipe_items',[]): 
            db.session.add(RecipeItem(**r))
            
        for r in data.get('reservations',[]):
            r['created_at'] = datetime.fromisoformat(r['created_at'])
            db.session.add(Reservation(**r))
            
        for i in data.get('infusions',[]): 
            if 'cup_size' not in i:
                i['cup_size'] = '16 oz'
            if 'addons' not in i:
                i['addons'] = ''
            if 'ice_level' not in i:
                i['ice_level'] = 'Normal Ice'
            db.session.add(Infusion(**i))
            
        for e in data.get('expenses',[]):
            e['created_at'] = datetime.fromisoformat(e['created_at'])
            db.session.add(Expense(**e))
            
        for a in data.get('audit_logs',[]):
            a['created_at'] = datetime.fromisoformat(a['created_at'])
            db.session.add(AuditLog(**a))
            
        db.session.commit()
        
        db.session.execute(db.text("SELECT setval('ingredients_id_seq', coalesce((SELECT MAX(id)+1 FROM ingredients), 1), false)"))
        db.session.execute(db.text("SELECT setval('menu_items_id_seq', coalesce((SELECT MAX(id)+1 FROM menu_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('recipe_items_id_seq', coalesce((SELECT MAX(id)+1 FROM recipe_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('reservations_id_seq', coalesce((SELECT MAX(id)+1 FROM reservations), 1), false)"))
        db.session.execute(db.text("SELECT setval('infusions_id_seq', coalesce((SELECT MAX(id)+1 FROM infusions), 1), false)"))
        db.session.execute(db.text("SELECT setval('expenses_id_seq', coalesce((SELECT MAX(id)+1 FROM expenses), 1), false)"))
        db.session.execute(db.text("SELECT setval('audit_logs_id_seq', coalesce((SELECT MAX(id)+1 FROM audit_logs), 1), false)"))
        
        db.session.commit()
        
        return jsonify({
            "status": "success", 
            "message": "System successfully restored!"
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# 10. SYSTEM INITIALIZATION & SEED DATA
# ==========================================

with app.app_context():
    try:
        db.create_all()
        
        # Schema Updates
        try:
            db.session.execute(db.text("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS order_source VARCHAR(30) DEFAULT 'Online'"))
            db.session.execute(db.text("ALTER TABLE infusions ADD COLUMN IF NOT EXISTS cup_size VARCHAR(20) DEFAULT '16 oz'"))
            db.session.execute(db.text("ALTER TABLE infusions ADD COLUMN IF NOT EXISTS addons VARCHAR(200) DEFAULT ''"))
            db.session.execute(db.text("ALTER TABLE infusions ADD COLUMN IF NOT EXISTS ice_level VARCHAR(50) DEFAULT 'Normal Ice'"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            
        try:
            db.session.execute(db.text("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY, description VARCHAR(200) NOT NULL, amount FLOAT NOT NULL, created_at TIMESTAMP
                )
            """))
            db.session.execute(db.text("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY, action VARCHAR(100) NOT NULL, details VARCHAR(255), created_at TIMESTAMP
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # 1. FORCE CHECK & INJECT MISSING INGREDIENTS
        ingredients_data =[
            {'name': 'Assam Black Tea', 'unit': 'ml', 'stock': 10000.0},
            {'name': 'Jasmine Green Tea', 'unit': 'ml', 'stock': 10000.0},
            {'name': 'Fresh Milk', 'unit': 'ml', 'stock': 8000.0},
            {'name': 'Non-Dairy Creamer', 'unit': 'grams', 'stock': 5000.0},
            {'name': 'Tapioca Pearls', 'unit': 'grams', 'stock': 3000.0},
            {'name': 'Brown Sugar Syrup', 'unit': 'ml', 'stock': 4000.0},
            {'name': 'Wintermelon Syrup', 'unit': 'ml', 'stock': 2000.0},
            {'name': 'Matcha Powder', 'unit': 'grams', 'stock': 1000.0},
            {'name': 'Taro Paste', 'unit': 'grams', 'stock': 1500.0},
            {'name': 'Strawberry Syrup', 'unit': 'ml', 'stock': 2000.0},
            {'name': 'Lychee Syrup', 'unit': 'ml', 'stock': 2000.0},
            {'name': 'Plastic Cups & Lids', 'unit': 'pcs', 'stock': 500.0},
            {'name': 'Hash Brown (pcs)', 'unit': 'pcs', 'stock': 100.0},
            {'name': 'French Fries', 'unit': 'grams', 'stock': 5000.0},
            {'name': 'Onion Rings', 'unit': 'grams', 'stock': 3000.0},
            {'name': 'Potato Mojos', 'unit': 'grams', 'stock': 3000.0},
            {'name': 'Snack Packaging', 'unit': 'pcs', 'stock': 500.0},
            {'name': 'Cooking Oil', 'unit': 'ml', 'stock': 10000.0},
            {'name': 'Caramel Syrup', 'unit': 'ml', 'stock': 2000.0},
            {'name': 'Frappe Base', 'unit': 'grams', 'stock': 2000.0},
            {'name': 'Nata', 'unit': 'grams', 'stock': 3000.0},
            {'name': 'Coffee Jelly', 'unit': 'grams', 'stock': 3000.0},
            {'name': 'Espresso Shot', 'unit': 'ml', 'stock': 2000.0},
            {'name': 'Biscoff Crumbs', 'unit': 'grams', 'stock': 1000.0},
            {'name': 'Cloud Foam', 'unit': 'ml', 'stock': 2000.0}
        ]
        
        for i_data in ingredients_data:
            if not Ingredient.query.filter_by(name=i_data['name']).first():
                db.session.add(Ingredient(**i_data))
        db.session.commit()

        # 2. FORCE CHECK & INJECT MISSING MENU ITEMS (SNACKS)
        menu_data =[
            {"name": "Dirty Matcha", "price": 49.00, "letter": "D", "category": "Trending Now"},
            {"name": "Biscoff Frappe", "price": 49.00, "letter": "B", "category": "Trending Now"},
            {"name": "Cloud Macchiato", "price": 49.00, "letter": "C", "category": "Trending Now"},
            {"name": "The Midnight Velvet", "price": 49.00, "letter": "M", "category": "Signature Series"},
            {"name": "The Jade Garden", "price": 49.00, "letter": "J", "category": "Signature Series"},
            {"name": "Classic Pearl Milk Tea", "price": 49.00, "letter": "C", "category": "Classic Milk Tea"},
            {"name": "Taro Symphony", "price": 49.00, "letter": "T", "category": "Matcha & Taro"},
            {"name": "Brown Sugar Deerioca", "price": 49.00, "letter": "B", "category": "Signature Series"},
            {"name": "Wintermelon Frost", "price": 49.00, "letter": "W", "category": "Fruit Infusions"},
            {"name": "Matcha Espresso", "price": 49.00, "letter": "M", "category": "Matcha Menu"},
            {"name": "Matcha Strawberry", "price": 49.00, "letter": "M", "category": "Matcha Menu"},
            {"name": "Matcha Caramel", "price": 49.00, "letter": "M", "category": "Matcha Menu"},
            {"name": "Matcha Frappe", "price": 49.00, "letter": "M", "category": "Matcha Menu"},
            {"name": "Matcha Latte", "price": 49.00, "letter": "M", "category": "Matcha Menu"},
            {"name": "Strawberry Lychee", "price": 49.00, "letter": "S", "category": "Fruit Infusions"},
            
            # --- SNACKS ARE INJECTED HERE ---
            {"name": "Hash Brown", "price": 29.00, "letter": "H", "category": "Snacks"},
            {"name": "French Fries", "price": 39.00, "letter": "F", "category": "Snacks"},
            {"name": "Onion Rings", "price": 59.00, "letter": "O", "category": "Snacks"},
            {"name": "Potato Mojos", "price": 59.00, "letter": "P", "category": "Snacks"}
        ]
        
        for m_data in menu_data:
            if not MenuItem.query.filter_by(name=m_data['name']).first():
                db.session.add(MenuItem(**m_data))
        db.session.commit()

        # 3. FORCE CHECK & MAP RECIPES
        def add_recipe(item_name, ingredient_name, qty):
            item = MenuItem.query.filter_by(name=item_name).first()
            ing = Ingredient.query.filter_by(name=ingredient_name).first()
            
            if item and ing:
                # Only add the recipe link if it doesn't already exist
                existing_recipe = RecipeItem.query.filter_by(menu_item_id=item.id, ingredient_id=ing.id).first()
                if not existing_recipe:
                    db.session.add(RecipeItem(
                        menu_item_id=item.id, 
                        ingredient_id=ing.id, 
                        quantity_required=qty
                    ))

        # Drinks Recipes
        add_recipe("Dirty Matcha", "Matcha Powder", 10)
        add_recipe("Dirty Matcha", "Espresso Shot", 30)
        add_recipe("Dirty Matcha", "Fresh Milk", 120)
        add_recipe("Dirty Matcha", "Plastic Cups & Lids", 1)
        add_recipe("Biscoff Frappe", "Frappe Base", 30)
        add_recipe("Biscoff Frappe", "Fresh Milk", 100)
        add_recipe("Biscoff Frappe", "Biscoff Crumbs", 20)
        add_recipe("Biscoff Frappe", "Plastic Cups & Lids", 1)
        add_recipe("Cloud Macchiato", "Espresso Shot", 30)
        add_recipe("Cloud Macchiato", "Fresh Milk", 100)
        add_recipe("Cloud Macchiato", "Cloud Foam", 30)
        add_recipe("Cloud Macchiato", "Brown Sugar Syrup", 20)
        add_recipe("Cloud Macchiato", "Plastic Cups & Lids", 1)
        add_recipe("The Midnight Velvet", "Assam Black Tea", 150)
        add_recipe("The Midnight Velvet", "Fresh Milk", 100)
        add_recipe("The Midnight Velvet", "Brown Sugar Syrup", 30)
        add_recipe("The Midnight Velvet", "Tapioca Pearls", 50)
        add_recipe("The Midnight Velvet", "Plastic Cups & Lids", 1)
        add_recipe("The Jade Garden", "Jasmine Green Tea", 150)
        add_recipe("The Jade Garden", "Fresh Milk", 100)
        add_recipe("The Jade Garden", "Tapioca Pearls", 50)
        add_recipe("The Jade Garden", "Plastic Cups & Lids", 1)
        add_recipe("Classic Pearl Milk Tea", "Assam Black Tea", 150)
        add_recipe("Classic Pearl Milk Tea", "Non-Dairy Creamer", 30)
        add_recipe("Classic Pearl Milk Tea", "Brown Sugar Syrup", 30)
        add_recipe("Classic Pearl Milk Tea", "Tapioca Pearls", 50)
        add_recipe("Classic Pearl Milk Tea", "Plastic Cups & Lids", 1)
        add_recipe("Taro Symphony", "Taro Paste", 50)
        add_recipe("Taro Symphony", "Fresh Milk", 150)
        add_recipe("Taro Symphony", "Tapioca Pearls", 50)
        add_recipe("Taro Symphony", "Plastic Cups & Lids", 1)
        add_recipe("Brown Sugar Deerioca", "Fresh Milk", 200)
        add_recipe("Brown Sugar Deerioca", "Brown Sugar Syrup", 50)
        add_recipe("Brown Sugar Deerioca", "Tapioca Pearls", 50)
        add_recipe("Brown Sugar Deerioca", "Plastic Cups & Lids", 1)
        add_recipe("Wintermelon Frost", "Jasmine Green Tea", 200)
        add_recipe("Wintermelon Frost", "Wintermelon Syrup", 40)
        add_recipe("Wintermelon Frost", "Plastic Cups & Lids", 1)
        add_recipe("Matcha Espresso", "Matcha Powder", 10)
        add_recipe("Matcha Espresso", "Fresh Milk", 150)
        add_recipe("Matcha Espresso", "Brown Sugar Syrup", 20)
        add_recipe("Matcha Espresso", "Plastic Cups & Lids", 1)
        add_recipe("Matcha Strawberry", "Matcha Powder", 10)
        add_recipe("Matcha Strawberry", "Fresh Milk", 150)
        add_recipe("Matcha Strawberry", "Strawberry Syrup", 30)
        add_recipe("Matcha Strawberry", "Plastic Cups & Lids", 1)
        add_recipe("Matcha Caramel", "Matcha Powder", 10)
        add_recipe("Matcha Caramel", "Fresh Milk", 150)
        add_recipe("Matcha Caramel", "Caramel Syrup", 30)
        add_recipe("Matcha Caramel", "Plastic Cups & Lids", 1)
        add_recipe("Matcha Frappe", "Matcha Powder", 15)
        add_recipe("Matcha Frappe", "Fresh Milk", 100)
        add_recipe("Matcha Frappe", "Frappe Base", 30)
        add_recipe("Matcha Frappe", "Plastic Cups & Lids", 1)
        add_recipe("Matcha Latte", "Matcha Powder", 10)
        add_recipe("Matcha Latte", "Fresh Milk", 180)
        add_recipe("Matcha Latte", "Brown Sugar Syrup", 20)
        add_recipe("Matcha Latte", "Plastic Cups & Lids", 1)
        add_recipe("Strawberry Lychee", "Jasmine Green Tea", 200)
        add_recipe("Strawberry Lychee", "Strawberry Syrup", 20)
        add_recipe("Strawberry Lychee", "Lychee Syrup", 20)
        add_recipe("Strawberry Lychee", "Plastic Cups & Lids", 1)

        # Snacks Recipes
        add_recipe("Hash Brown", "Hash Brown (pcs)", 1)
        add_recipe("Hash Brown", "Snack Packaging", 1)
        add_recipe("Hash Brown", "Cooking Oil", 20)
        add_recipe("French Fries", "French Fries", 150)
        add_recipe("French Fries", "Snack Packaging", 1)
        add_recipe("French Fries", "Cooking Oil", 50)
        add_recipe("Onion Rings", "Onion Rings", 150)
        add_recipe("Onion Rings", "Snack Packaging", 1)
        add_recipe("Onion Rings", "Cooking Oil", 50)
        add_recipe("Potato Mojos", "Potato Mojos", 150)
        add_recipe("Potato Mojos", "Snack Packaging", 1)
        add_recipe("Potato Mojos", "Cooking Oil", 50)

        db.session.commit()
            
    except Exception as e:
        print(f"Database Initialization Error: {e}")

# ==========================================
# 11. APPLICATION RUNNER
# ==========================================

if __name__ == '__main__':
    if os.environ.get('RENDER') or os.environ.get('DYNO'):
        app.run(host='0.0.0.0', port=5000)
    else:
        try:
            import webview
            
            def start_server():
                """
                Spins up the Flask web server inside a daemon thread.
                """
                app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

            server_thread = threading.Thread(target=start_server)
            server_thread.daemon = True
            server_thread.start()

            print("==================================================")
            print(" STARTING SYSTEM (DESKTOP APP MODE)")
            print(f" CUSTOMER POS LINK: http://{get_local_ip()}:5000/")
            print("==================================================")

            admin_url = "http://localhost:5000/admin"
            
            webview.create_window(
                '9599 Tea & Coffee - Admin POS System', 
                admin_url, 
                width=1300, 
                height=850,
                resizable=True,
                fullscreen=False
            )
            
            webview.start()
            
        except ImportError:
            print("Notice: 'pywebview' not found. Falling back to standard browser mode.")
            app.run(host='0.0.0.0', debug=True, port=5000)