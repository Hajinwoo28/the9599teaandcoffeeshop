
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
        nullable=False
    )
    pearls = db.Column(
        db.String(100), 
        nullable=False
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
    
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Identity Services Library -->
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    
    <style>
        /* CSS Reset & Defaults */
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
            flex-direction: column; 
            height: 100vh; 
            overflow: hidden; 
        }
        
        /* Header / Logo Styling */
        header { 
            background: white; 
            padding-top: 15px;
            padding-bottom: 15px;
            padding-left: 20px;
            padding-right: 20px;
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 1px solid #EFEBE4; 
            position: relative; 
            flex-shrink: 0; 
        }
        .logo-area { 
            display: flex; 
            align-items: center; 
            gap: 12px; 
            font-weight: 800; 
            font-size: 1.1rem; 
            color: #3E2723; 
            line-height: 1.1; 
        }
        .logo-img { 
            width: 45px; 
            height: 45px; 
            border-radius: 50%; 
            background: #F5EFE6; 
            object-fit: cover; 
            border: 2px solid #6F4E37; 
        }
        
        /* Main Application Container */
        .main-container { 
            display: flex; 
            flex: 1; 
            overflow: hidden; 
            flex-direction: row; 
        }
        .menu-area { 
            flex: 1; 
            padding: 15px; 
            overflow-y: auto; 
        }
        
        /* Categories Navigation */
        .categories { 
            display: flex; 
            gap: 10px; 
            overflow-x: auto; 
            margin-bottom: 20px; 
            padding-bottom: 5px; 
            scrollbar-width: none; 
            -webkit-overflow-scrolling: touch; 
        }
        .categories::-webkit-scrollbar { 
            display: none; 
        }
        .cat-btn { 
            padding-top: 8px;
            padding-bottom: 8px;
            padding-left: 16px;
            padding-right: 16px;
            border-radius: 20px; 
            border: 1px solid #D7CCC8; 
            background: white; 
            color: #5D4037; 
            font-weight: 600; 
            cursor: pointer; 
            white-space: nowrap; 
            font-size: 0.85rem; 
            transition: all 0.2s; 
        }
        .cat-btn.active { 
            background: #6F4E37; 
            color: white; 
            border-color: #6F4E37; 
            box-shadow: 0 4px 6px rgba(111, 78, 55, 0.2); 
        }

        /* Menu Grid System */
        .menu-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); 
            gap: 15px; 
        }
        .card { 
            background: white; 
            border-radius: 12px; 
            overflow: hidden; 
            box-shadow: 0 4px 10px rgba(111, 78, 55, 0.05); 
            cursor: pointer; 
            transition: transform 0.2s; 
            border: 1px solid #EFEBE4; 
            position: relative; 
        }
        .card:active { 
            transform: scale(0.98); 
        }
        .card-img-container { 
            height: 120px; 
            background: linear-gradient(135deg, #D7CCC8 0%, #A1887F 100%); 
            position: relative; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
        }
        .card-img-container span { 
            font-size: 50px; 
            font-weight: 800; 
            color: rgba(255, 255, 255, 0.4); 
        }
        .card-price { 
            position: absolute; 
            bottom: 8px; 
            right: 8px; 
            background-color: #6F4E37; 
            color: white; 
            padding-top: 4px;
            padding-bottom: 4px;
            padding-left: 8px;
            padding-right: 8px;
            border-radius: 6px; 
            font-weight: 700; 
            font-size: 0.8rem; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.2); 
        }
        .card-title { 
            padding: 12px; 
            font-weight: 800; 
            color: #3E2723; 
            font-size: 0.9rem; 
            line-height: 1.2; 
        }
        .empty-category { 
            grid-column: 1 / -1; 
            text-align: center; 
            color: #A1887F; 
            padding: 40px; 
            font-weight: 600; 
        }

        /* Sold Out & Low Stock Badges */
        .card.sold-out { 
            opacity: 0.5; 
            cursor: not-allowed; 
        }
        .card.sold-out:active { 
            transform: none; 
        }
        .sold-out-badge { 
            position: absolute; 
            top: 0; 
            left: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(255,255,255,0.7); 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            font-weight: 900; 
            color: #C62828; 
            font-size: 1.2rem; 
            z-index: 10; 
            letter-spacing: 1px; 
            text-transform: uppercase; 
        }
        .low-stock-badge { 
            position: absolute; 
            top: 8px; 
            left: 8px; 
            background: #F59E0B; 
            color: white; 
            padding-top: 2px;
            padding-bottom: 2px;
            padding-left: 8px;
            padding-right: 8px;
            border-radius: 4px; 
            font-size: 0.65rem; 
            font-weight: 800; 
            z-index: 5; 
            text-transform: uppercase; 
        }

        /* Sidebar Styling (Cart) */
        .sidebar { 
            width: 350px; 
            background: white; 
            border-left: 1px solid #D7CCC8; 
            display: flex; 
            flex-direction: column; 
            z-index: 50; 
        }
        .cart-top-section { 
            padding-top: 15px;
            padding-bottom: 10px;
            padding-left: 20px;
            padding-right: 20px;
            flex-shrink: 0; 
        }
        .cart-header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 15px; 
        }
        .cart-title { 
            font-size: 1.1rem; 
            font-weight: 800; 
            color: #3E2723; 
            display: flex; 
            align-items: center; 
            gap: 10px; 
        }
        .order-type { 
            display: flex; 
            background: #F5EFE6; 
            border-radius: 8px; 
            padding: 4px; 
            gap: 4px; 
            border: 1px solid #EFEBE4; 
            margin-bottom: 15px; 
        }
        .type-btn { 
            flex: 1; 
            padding-top: 6px;
            padding-bottom: 6px;
            padding-left: 12px;
            padding-right: 12px;
            text-align: center; 
            font-weight: 600; 
            font-size: 0.8rem; 
            border-radius: 6px; 
            cursor: pointer; 
            color: #8D6E63; 
            transition: all 0.2s; 
        }
        .type-btn.active { 
            background: #6F4E37; 
            color: white; 
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); 
        }

        /* Input Form Elements */
        .name-input { 
            width: 100%; 
            padding-top: 10px;
            padding-bottom: 10px;
            padding-left: 12px;
            padding-right: 12px;
            border: 1px solid #D7CCC8; 
            border-radius: 8px; 
            font-size: 0.9rem; 
            font-weight: 600; 
            outline: none; 
            margin-bottom: 10px; 
            color: #3E2723; 
            background: #FDFBF7; 
        }
        .name-input:focus { 
            border-color: #6F4E37; 
        }
        .pickup-label { 
            font-size: 0.75rem; 
            font-weight: 700; 
            color: #8D6E63; 
            margin-bottom: 5px; 
            display: block; 
            text-transform: uppercase; 
        }

        /* Cart List Middle Section */
        .cart-content { 
            padding-top: 10px;
            padding-bottom: 10px;
            padding-left: 20px;
            padding-right: 20px;
            flex: 1; 
            display: flex; 
            flex-direction: column; 
            overflow-y: auto; 
            background: white; 
            border-top: 1px solid #EFEBE4; 
        }
        .empty-cart { 
            margin: auto 0; 
            text-align: center; 
            color: #D7CCC8; 
        }
        .empty-cart i { 
            font-size: 3rem; 
            color: #EFEBE4; 
            margin-bottom: 10px; 
        }
        .empty-cart p { 
            font-weight: 800; 
            font-size: 1rem; 
            letter-spacing: 1px; 
            color: #A1887F;
        }
        .cart-items-list { 
            flex: 1; 
            overflow-y: visible; 
            display: none; 
        }
        .cart-item { 
            display: flex; 
            justify-content: space-between; 
            margin-bottom: 10px; 
            padding-bottom: 10px; 
            border-bottom: 1px solid #EFEBE4; 
        }
        .item-details h4 { 
            font-size: 0.9rem; 
            font-weight: 700; 
            color: #3E2723; 
            margin-bottom: 2px; 
        }
        .item-details p { 
            font-size: 0.75rem; 
            color: #C62828; 
            cursor: pointer; 
            display: inline-block; 
            font-weight: 600; 
        }
        .item-price { 
            font-weight: 800; 
            color: #6F4E37; 
            font-size: 0.9rem; 
        }

        /* Checkout Area Bottom Section */
        .checkout-area { 
            padding-top: 15px;
            padding-bottom: 15px;
            padding-left: 20px;
            padding-right: 20px;
            border-top: 1px solid #EFEBE4; 
            background: #FDFBF7; 
            flex-shrink: 0; 
            z-index: 10; 
            box-shadow: 0 -4px 6px rgba(111,78,55,0.05); 
        }
        .total-row { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 10px; 
        }
        .total-label { 
            font-size: 1.2rem; 
            font-weight: 800; 
            color: #3E2723; 
        }
        .total-amount { 
            font-size: 1.5rem; 
            font-weight: 800; 
            color: #6F4E37; 
        }
        .checkout-btn { 
            width: 100%; 
            padding-top: 15px;
            padding-bottom: 15px;
            padding-left: 15px;
            padding-right: 15px;
            border: none; 
            border-radius: 8px; 
            font-size: 0.95rem; 
            font-weight: 800; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            gap: 10px; 
            color: white; 
            background: #D7CCC8; 
            cursor: not-allowed; 
            transition: all 0.2s; 
        }
        .checkout-btn.active { 
            background: #6F4E37; 
            cursor: pointer; 
            box-shadow: 0 4px 12px rgba(111, 78, 55, 0.3); 
        }
        .checkout-btn.active:hover { 
            background: #4A3324; 
        }

        /* Success Modals */
        .modal { 
            display: none; 
            position: fixed; 
            z-index: 100; 
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
            text-align: center; 
            max-width: 90%; 
            width: 350px; 
            border: 2px solid #6F4E37; 
        }
        .modal-content h2 { 
            font-weight: 800; 
            margin-bottom: 10px; 
            color: #3E2723; 
        }
        .order-number { 
            font-size: 2rem; 
            font-weight: 800; 
            color: #6F4E37; 
            margin-top: 20px;
            margin-bottom: 20px;
            letter-spacing: 2px; 
        }
        .modal-btn { 
            background: #6F4E37; 
            color: white; 
            padding-top: 12px;
            padding-bottom: 12px;
            padding-left: 25px;
            padding-right: 25px;
            border: none; 
            border-radius: 6px; 
            font-weight: 700; 
            cursor: pointer; 
            width: 100%; 
        }

        /* Customer Live Order Notifications */
        .notif-container { 
            position: relative; 
            margin-right: 10px; 
            display: flex; 
            align-items: center; 
        }
        .notif-bell { 
            cursor: pointer; 
            position: relative; 
            padding: 5px; 
        }
        .notif-badge { 
            position: absolute; 
            top: -2px; 
            right: -2px; 
            background: #D32F2F; 
            color: white; 
            border-radius: 50%; 
            padding-top: 2px;
            padding-bottom: 2px;
            padding-left: 6px;
            padding-right: 6px;
            font-size: 0.65rem; 
            font-weight: 800; 
            display: none; 
            border: 2px solid white; 
        }
        .notif-dropdown { 
            display: none; 
            position: absolute; 
            top: 45px; 
            right: -10px; 
            background: white; 
            border: 1px solid #D7CCC8; 
            border-radius: 10px; 
            width: 300px; 
            box-shadow: 0 10px 25px rgba(62, 39, 35, 0.15); 
            z-index: 1000; 
            flex-direction: column; 
            overflow: hidden; 
        }
        .notif-header { 
            padding: 15px; 
            border-bottom: 1px solid #EFEBE4; 
            font-weight: 800; 
            font-size: 0.95rem; 
            color: #3E2723; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            background: #F5EFE6; 
        }
        .notif-clear { 
            font-size: 0.75rem; 
            color: #8D6E63; 
            cursor: pointer; 
            font-weight: 600; 
            text-transform: uppercase; 
        }
        .notif-clear:hover { 
            color: #C62828; 
        }
        .notif-list { 
            max-height: 300px; 
            overflow-y: auto; 
            display: flex; 
            flex-direction: column; 
        }
        .notif-item { 
            padding: 12px 15px; 
            border-bottom: 1px solid #FDFBF7; 
            font-size: 0.85rem; 
            color: #3E2723; 
            font-weight: 600; 
            line-height: 1.4; 
            display: flex; 
            align-items: flex-start; 
            gap: 10px; 
        }
        .notif-item i { 
            color: #F59E0B; 
            margin-top: 3px; 
        }
        .notif-item.ready i { 
            color: #388E3C; 
        }
        .notif-item.completed i { 
            color: #6F4E37; 
        }
        .notif-empty { 
            padding: 20px; 
            text-align: center; 
            color: #A1887F; 
            font-size: 0.85rem; 
            font-weight: 600; 
        }

        /* Mobile Adjustments */
        @media (max-width: 768px) { 
            body { 
                height: auto; 
                min-height: 100vh; 
                overflow-y: auto; 
                display: flex; 
                flex-direction: column; 
            }
            header { 
                flex-direction: row; 
                padding: 15px; 
                align-items: center; 
                flex-shrink: 0; 
            }
            .logo-area { 
                font-size: 1rem; 
            }
            .main-container { 
                flex-direction: column; 
                flex: 1; 
                height: auto; 
                overflow: visible; 
            } 
            .menu-area { 
                flex: none; 
                height: auto; 
                padding: 15px; 
                overflow: visible; 
                padding-bottom: 20px; 
            }
            .menu-grid { 
                grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); 
                gap: 12px; 
            }
            .sidebar { 
                width: 100%; 
                flex: none; 
                height: auto; 
                border-left: none; 
                border-top: 2px solid #D7CCC8; 
                display: flex; 
                flex-direction: column; 
            } 
            .cart-content { 
                max-height: 350px; 
                overflow-y: auto; 
            } 
            .notif-dropdown { 
                position: fixed; 
                top: 60px; 
                right: 10px; 
                width: calc(100% - 20px); 
                max-width: 350px; 
            }
        }
    </style>
</head>
<body>

    {% if not session.get('customer_verified') %}
    
    <!-- ============================================== -->
    <!-- GOOGLE LOGIN GATEKEEPER SCREEN                 -->
    <!-- ============================================== -->
    <div id="login-gatekeeper" style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5EFE6; padding: 20px;">
        <div style="background:white; padding:40px; border-radius:15px; box-shadow:0 10px 25px rgba(0,0,0,0.1); width:100%; max-width:400px; text-align:center; border: 2px solid #6F4E37;">
            <i class="fas fa-coffee" style="font-size: 3rem; color: #6F4E37; margin-bottom: 20px;"></i>
            <h2 style="color:#3E2723; margin-bottom:10px;">Welcome to 9599</h2>
            <p style="color:#8D6E63; font-size:0.9rem; margin-bottom:25px;">Please sign in to browse our menu and place an order securely.</p>
            
            {% if google_client_id == 'YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com' %}
            <div style="background:#FFEBEE; color:#C62828; padding:15px; border-radius:8px; border:1px solid #FFCDD2; margin-bottom:20px; font-size:0.85rem; text-align:left;">
                <strong>⚠️ Admin Setup Required:</strong><br><br>
                Google Login is currently disabled. You must generate an OAuth Client ID from the Google Cloud Console and add it as <b>GOOGLE_CLIENT_ID</b> in your Render Environment Variables.
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
                 data-type="standard"
                 data-shape="rectangular"
                 data-theme="outline"
                 data-text="continue_with"
                 data-size="large"
                 data-logo_alignment="left"
                 style="display: flex; justify-content: center; margin-top: 20px;">
            </div>
            
            <div id="login-spinner" style="display:none; margin-top:20px; color:#6F4E37; font-weight:700;">
                <i class="fas fa-spinner fa-spin"></i> Authenticating...
            </div>
        </div>
    </div>

    <script>
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

                if(res.ok) {
                    location.reload();
                } else {
                    const data = await res.json();
                    alert("Authentication Error: " + (data.error || "Please try again."));
                    document.querySelector('.g_id_signin').style.display = 'flex';
                    document.getElementById('login-spinner').style.display = 'none';
                }
            } catch (e) {
                alert("Connection error. Please check your internet.");
                document.querySelector('.g_id_signin').style.display = 'flex';
                document.getElementById('login-spinner').style.display = 'none';
            }
        }
    </script>

    {% else %}
    <!-- ============================================== -->
    <!-- ORIGINAL MENU CONTENT (Customer is Verified)   -->
    <!-- ============================================== -->
    <header>
        <div class="logo-area">
            <img src="/static/images/9599.jpg" alt="Logo" class="logo-img" onerror="this.style.display='none'">
            <div style="display:flex; flex-direction: column;">
                <span>9599 Tea & Coffee</span>
                <span style="font-size: 0.6rem; font-weight: 700; letter-spacing: 1.5px; color: #A67B5B;">PARNE NA!</span>
                <span id="store-hours-display" style="font-size: 0.65rem; font-weight: 600; color: #8D6E63; display: none;"></span>
            </div>
        </div>

        <div class="notif-container">
            <div class="notif-bell" onclick="toggleNotif()">
                <i class="fas fa-bell" style="font-size: 1.4rem; color: #6F4E37;"></i>
                <span class="notif-badge" id="notif-badge">0</span>
            </div>
            <div class="notif-dropdown" id="notif-dropdown">
                <div class="notif-header">
                    Notifications
                    <span class="notif-clear" onclick="clearNotifs()">Clear</span>
                </div>
                <div class="notif-list" id="notif-list">
                    <div class="notif-empty">No new notifications.</div>
                </div>
            </div>
        </div>
    </header>

    <div class="main-container">
        
        <!-- Left Side: Drinks Menu -->
        <div class="menu-area">
            <div class="categories" id="categories-container">
                <button class="cat-btn active" onclick="filterMenu('All', this)">All</button>
            </div>
            <div class="menu-grid" id="menu-grid">
                <div class="empty-category">Loading Menu...</div>
            </div>
        </div>

        <!-- Right Side: Sidebar / Cart -->
        <div class="sidebar">
            <div class="cart-top-section">
                <div class="cart-header">
                    <div class="cart-title">
                        <i class="fas fa-shopping-cart" style="color:#A67B5B;"></i> Your Cart
                    </div>
                </div>
                
                <div class="order-type">
                    <div class="type-btn active" id="btn-dine-in" onclick="setOrderType('Dine-In')">Dine-In</div>
                    <div class="type-btn" id="btn-take-out" onclick="setOrderType('Take-Out')">Take-Out</div>
                </div>
                
                <input type="text" class="name-input" id="customer-name" placeholder="Enter Your Name" oninput="checkCheckoutStatus()">
                
                <label class="pickup-label" for="pickup-time">
                    Expected Pick-up Time (Required) <span style="color:#C62828;">*</span>
                </label>
                <input type="time" class="name-input" id="pickup-time" oninput="checkCheckoutStatus()" required style="margin-bottom: 0;">
            </div>

            <div class="cart-content">
                <div class="cart-items-header" id="cart-items-header" style="display: none; font-size: 0.8rem; font-weight: 800; color: #8D6E63; margin-bottom: 10px; text-transform: uppercase;">
                    Selected Orders
                </div>
                
                <div class="empty-cart" id="empty-cart" style="margin-top: 10px;">
                    <i class="fas fa-shopping-basket"></i>
                    <p>CART IS EMPTY</p>
                </div>
                
                <div class="cart-items-list" id="cart-items"></div>
            </div>

            <div class="checkout-area">
                <div class="total-row">
                    <div class="total-label">Total</div>
                    <div class="total-amount" id="cart-total">₱0.00</div>
                </div>
                <button class="checkout-btn" id="checkout-btn" onclick="submitOrder()">
                    <i class="fas fa-paper-plane"></i> PLACE ORDER
                </button>
            </div>
        </div>
    </div>

    <!-- Success Modal -->
    <div id="success-modal" class="modal">
        <div class="modal-content">
            <h2>Order Sent!</h2>
            <p style="color: #64748B;">Please wait for your name to be called.</p>
            <div class="order-number" id="display-code">XXXXXX</div>
            <button class="modal-btn" onclick="closeModal()">Done</button>
        </div>
    </div>

    <!-- Customer JavaScript Logic -->
    <script>
        function escapeHTML(str) { 
            let div = document.createElement('div'); 
            div.innerText = str; 
            return div.innerHTML; 
        }

        document.addEventListener("DOMContentLoaded", () => {
            // Auto-fill Name from verified session
            const savedName = "{{ session.get('customer_name', '') }}";
            const nameInput = document.getElementById('customer-name');
            
            if(savedName && nameInput) {
                nameInput.value = savedName;
                nameInput.readOnly = true; 
                nameInput.style.backgroundColor = "#F5EFE6";
            }

            const params = new URLSearchParams(window.location.search);
            
            // Check if Link is Expired based on URL Parameter Time
            if (params.has('exp')) {
                const expTime = parseInt(params.get('exp'));
                if (Date.now() > expTime) {
                    document.body.innerHTML = `
                        <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5EFE6; flex-direction:column; text-align:center; padding: 20px;">
                            <i class="fas fa-lock" style="font-size:4rem; color:#D7CCC8; margin-bottom:20px;"></i>
                            <h2 style="color:#3E2723;">Ordering Link Expired</h2>
                            <p style="color:#8D6E63;">Please ask the staff for a new ordering link.</p>
                        </div>`;
                    return; 
                }
            }
            
            // Display store hours if provided by admin
            const hoursDisplay = document.getElementById('store-hours-display');
            if (params.has('open') && params.has('close') && hoursDisplay) {
                hoursDisplay.innerText = `(${params.get('open')} - ${params.get('close')})`;
                hoursDisplay.style.display = 'block';
            }
            
            fetchMenu();
            updateNotifUI(); 
        });

        // Notifications State Management
        let notifications = JSON.parse(localStorage.getItem('notifications')) ||[];
        let unreadNotifs = parseInt(localStorage.getItem('unreadNotifs')) || 0;

        function updateNotifUI() {
            const badge = document.getElementById('notif-badge');
            const list = document.getElementById('notif-list');
            
            if (unreadNotifs > 0) { 
                badge.innerText = unreadNotifs; 
                badge.style.display = 'block'; 
            } else { 
                badge.style.display = 'none'; 
            }
            
            if (notifications.length > 0) {
                list.innerHTML = notifications.map(n => {
                    let iconClass = 'fa-info-circle'; 
                    let itemClass = '';
                    
                    if (n.status === 'Ready for Pick-up') { 
                        iconClass = 'fa-check-circle'; 
                        itemClass = 'ready'; 
                    }
                    if (n.status === 'Completed') { 
                        iconClass = 'fa-flag-checkered'; 
                        itemClass = 'completed'; 
                    }
                    
                    return `
                        <div class="notif-item ${itemClass}">
                            <i class="fas ${iconClass}"></i> 
                            <div>
                                Order <strong>#${escapeHTML(n.code)}</strong> is now:<br>
                                <span style="color:#8D6E63;">${escapeHTML(n.status)}</span>
                            </div>
                        </div>`;
                }).join('');
            } else {
                list.innerHTML = '<div class="notif-empty">No new notifications.</div>';
            }
        }

        function toggleNotif() {
            const dropdown = document.getElementById('notif-dropdown');
            if (dropdown.style.display === 'none' || dropdown.style.display === '') {
                dropdown.style.display = 'flex'; 
                unreadNotifs = 0; 
                localStorage.setItem('unreadNotifs', 0); 
                updateNotifUI();
            } else { 
                dropdown.style.display = 'none'; 
            }
        }

        function clearNotifs() {
            notifications =[]; 
            unreadNotifs = 0;
            localStorage.setItem('notifications', JSON.stringify(notifications));
            localStorage.setItem('unreadNotifs', 0); 
            updateNotifUI();
        }

        // Live Order Polling
        async function pollCustomerOrderStatus() {
            let myOrders = JSON.parse(localStorage.getItem('myOrders')) ||[];
            if (myOrders.length === 0) {
                return;
            }
            
            let codes = myOrders.map(o => encodeURIComponent(o.code)).join(',');
            
            try {
                const res = await fetch(`/api/customer/status?codes=${codes}&_t=${new Date().getTime()}`);
                if (!res.ok) {
                    return;
                }
                
                const data = await res.json();
                let updated = false;
                
                data.forEach(serverOrder => {
                    let localOrder = myOrders.find(o => o.code === serverOrder.code);
                    if (localOrder && localOrder.status !== serverOrder.status) {
                        localOrder.status = serverOrder.status; 
                        updated = true;
                        notifications.unshift({ 
                            code: serverOrder.code, 
                            status: serverOrder.status 
                        });
                        unreadNotifs += 1;
                    }
                });
                
                if (updated) {
                    if (notifications.length > 10) {
                        notifications = notifications.slice(0, 10);
                    }
                    localStorage.setItem('myOrders', JSON.stringify(myOrders));
                    localStorage.setItem('notifications', JSON.stringify(notifications));
                    localStorage.setItem('unreadNotifs', unreadNotifs);
                    updateNotifUI();
                }
            } catch (e) { 
                console.error("Polling error", e); 
            }
        }
        
        setInterval(pollCustomerOrderStatus, 3000);

        // Core Cart Logic
        let menuItems =[]; 
        let cart =[]; 
        let orderType = 'Dine-In';

        async function fetchMenu() {
            try {
                const response = await fetch('/api/menu?_t=' + new Date().getTime());
                menuItems = await response.json();
                renderCategories(); 
                renderMenu('All');
            } catch(e) { 
                document.getElementById('menu-grid').innerHTML = '<div class="empty-category">Failed to load menu.</div>'; 
            }
        }

        function renderCategories() {
            const categories =[...new Set(menuItems.map(item => item.category))];
            const catContainer = document.getElementById('categories-container');
            
            catContainer.innerHTML = `<button class="cat-btn active" onclick="filterMenu('All', this)">All</button>`;
            
            categories.forEach(cat => { 
                catContainer.innerHTML += `<button class="cat-btn" onclick="filterMenu('${escapeHTML(cat)}', this)">${escapeHTML(cat)}</button>`; 
            });
        }

        function renderMenu(categoryFilter) {
            const grid = document.getElementById('menu-grid'); 
            grid.innerHTML = '';
            
            const filteredItems = categoryFilter === 'All' ? menuItems : menuItems.filter(item => item.category === categoryFilter);
            
            if (filteredItems.length === 0) { 
                grid.innerHTML = '<div class="empty-category">No items found.</div>'; 
                return; 
            }
            
            filteredItems.forEach(item => {
                let isSoldOut = item.stock <= 0;
                let cardClass = isSoldOut ? 'card sold-out' : 'card';
                let onClick = isSoldOut ? '' : `onclick="addToCart('${item.name.replace(/'/g, "\\'")}', ${item.price})"`;
                let badges = '';
                
                if (isSoldOut) {
                    badges = '<div class="sold-out-badge">SOLD OUT</div>';
                } else if (item.stock <= 5) {
                    badges = `<div class="low-stock-badge">Only ${item.stock} left</div>`;
                }
                
                grid.innerHTML += `
                    <div class="${cardClass}" ${onClick}>
                        ${badges}
                        <div class="card-img-container">
                            <span>${escapeHTML(item.letter)}</span>
                            <div class="card-price">₱${item.price.toFixed(2)}</div>
                        </div>
                        <div class="card-title">${escapeHTML(item.name)}</div>
                    </div>
                `;
            });
        }

        function filterMenu(categoryName, btnElement) {
            document.querySelectorAll('.cat-btn').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active'); 
            renderMenu(categoryName);
        }

        function setOrderType(type) {
            orderType = type;
            document.getElementById('btn-dine-in').className = type === 'Dine-In' ? 'type-btn active' : 'type-btn';
            document.getElementById('btn-take-out').className = type === 'Take-Out' ? 'type-btn active' : 'type-btn';
        }

        function addToCart(name, price) {
            const item = menuItems.find(i => i.name === name);
            if (item && item.stock > 0) { 
                cart.push({ 
                    name: name, 
                    price: price 
                }); 
                updateCartUI(); 
            } else { 
                alert("Sorry, this item is out of stock!"); 
            }
        }

        function updateCartUI() {
            const emptyCart = document.getElementById('empty-cart');
            const cartItemsList = document.getElementById('cart-items');
            const totalEl = document.getElementById('cart-total');
            const itemsHeader = document.getElementById('cart-items-header');
            
            if (cart.length > 0) {
                emptyCart.style.display = 'none'; 
                cartItemsList.style.display = 'block';
                if(itemsHeader) {
                    itemsHeader.style.display = 'block';
                }
                
                cartItemsList.innerHTML = ''; 
                let total = 0;
                
                cart.forEach((item, index) => {
                    total += item.price;
                    cartItemsList.innerHTML += `
                        <div class="cart-item">
                            <div class="item-details">
                                <h4>${escapeHTML(item.name)}</h4>
                                <p onclick="removeFromCart(${index})">
                                    <i class="fas fa-trash"></i> Remove
                                </p>
                            </div>
                            <div class="item-price">₱${item.price.toFixed(2)}</div>
                        </div>
                    `;
                });
                totalEl.innerText = `₱${total.toFixed(2)}`;
            } else {
                emptyCart.style.display = 'block'; 
                cartItemsList.style.display = 'none';
                if(itemsHeader) {
                    itemsHeader.style.display = 'none'; 
                }
                totalEl.innerText = '₱0.00';
            }
            checkCheckoutStatus();
        }

        function removeFromCart(index) { 
            cart.splice(index, 1); 
            updateCartUI(); 
        }

        function checkCheckoutStatus() {
            const btn = document.getElementById('checkout-btn');
            const name = document.getElementById('customer-name').value.trim();
            const pickupTime = document.getElementById('pickup-time').value; 
            
            if (cart.length > 0 && name !== '' && pickupTime !== '') {
                btn.className = 'checkout-btn active';
            } else {
                btn.className = 'checkout-btn';
            }
        }

        async function submitOrder() {
            const btn = document.getElementById('checkout-btn');
            const name = document.getElementById('customer-name').value.trim();
            const pickupTime = document.getElementById('pickup-time').value;
            
            if (cart.length === 0 || name === '' || pickupTime === '') {
                return;
            }

            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> PROCESSING...'; 
            btn.className = 'checkout-btn'; 
            
            const total = cart.reduce((sum, item) => sum + item.price, 0);
            
            const mappedItems = cart.map(item => ({ 
                foundation: item.name, 
                sweetener: 'Standard', 
                pearls: orderType, 
                price: item.price 
            }));
            
            const payload = { 
                name: name, 
                email: "{{ session.get('customer_email', 'google_user@9599.local') }}", 
                total: total, 
                items: mappedItems, 
                pickup_time: pickupTime 
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
                    
                    let myOrders = JSON.parse(localStorage.getItem('myOrders')) ||[];
                    myOrders.push({ 
                        code: result.reservation_code, 
                        status: 'Preparing Order' 
                    });
                    
                    if (myOrders.length > 5) {
                        myOrders = myOrders.slice(-5);
                    }
                    localStorage.setItem('myOrders', JSON.stringify(myOrders));
                    
                    cart =[]; 
                    document.getElementById('pickup-time').value = ''; 
                    updateCartUI(); 
                    fetchMenu(); 
                } else {
                    if(response.status === 429) {
                        alert("⚠️ " + (result.message || "Please wait a moment before ordering again."));
                    } else {
                        alert("Order Error: " + (result.message || "Failed to process order."));
                    }
                    fetchMenu(); 
                }
            } catch (error) { 
                alert("Connection error. Please check your internet."); 
            } finally { 
                btn.innerHTML = '<i class="fas fa-paper-plane"></i> PLACE ORDER'; 
                checkCheckoutStatus(); 
            }
        }

        function closeModal() { 
            document.getElementById('success-modal').style.display = 'none'; 
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
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#3E2723">
    
    <link rel="apple-touch-icon" href="/static/images/9599.jpg">
    <link rel="icon" href="/static/images/9599.jpg">
    
    <title>9599 Admin POS</title>
    
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
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
        
        /* ============================================== */
        /* FEATURE 1: MANUAL ORDER (WALK-IN POS) STYLES   */
        /* ============================================== */
        .quick-order-layout { 
            display: grid; 
            grid-template-columns: 1fr 350px; 
            gap: 20px; 
            height: 100%; 
        }
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

        /* Printable Receipt Modal Styles */
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
                <div class="nav-item" onclick="switchTab('quick-order', 'Manual POS (Notebook Killer)', this)">
                    <i class="fas fa-cash-register"></i> Manual POS
                </div>
                <div class="nav-item" onclick="switchTab('inventory', 'Inventory', this)">
                    <i class="fas fa-boxes"></i> Inventory
                </div>
                <div class="nav-item" onclick="switchTab('finance', 'Finance & Reports', this)">
                    <i class="fas fa-chart-line"></i> Finance
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
                    <div class="qo-menu-grid" id="qo-menu-grid">
                        <!-- Populated by JS -->
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

                        <div class="settings-card" style="flex-shrink:0;">
                            <div class="card-title"><i class="fas fa-history"></i> Data Migration (Legacy Notebook)</div>
                            <p class="desc-text">Upload your past Excel/CSV records to inject legacy sales into the system. Requires columns: Date, Item Name, Quantity, Price, Payment Type.</p>
                            <input type="file" id="legacy-csv" accept=".csv" style="margin-bottom: 15px; padding:10px; border:1px dashed #D7CCC8; width:100%;">
                            <button class="btn-blue" onclick="uploadLegacyCSV()">Import CSV Records</button>
                        </div>
                    </div>

                    <div class="settings-card">
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

                        <div style="margin-top:20px; border-top:1px solid #EFEBE4; padding-top:15px; flex:1; overflow-y:auto;">
                            <h4 style="color:#5D4037; margin-bottom:10px; font-size:0.9rem;">Today's Logged Expenses</h4>
                            <div id="expense-list" style="font-size:0.85rem; color:#8D6E63;">
                                <!-- Populated by JS -->
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- ============================================== -->
            <!-- SETTINGS & MENU                                -->
            <!-- ============================================== -->
            <div id="tab-settings" class="tab-pane">
                <div class="settings-grid-layout">
                    
                    <div style="display: flex; flex-direction: column; min-height: 0; overflow-y: auto; padding-right: 5px;">
                        
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
                <p>Total: <span id="receipt-total-display"></span></p>
                <p>Thank You!</p>
                <button onclick="closeReceiptModal()" style="margin-top:20px; padding:10px; width:100%; background:#6F4E37; color:white; border:none; cursor:pointer;">Close</button>
            </div>
        </div>
    </div>

    <!-- Admin JavaScript Logic -->
    <script>
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
        // NAVIGATION (TABS)
        // ----------------------------------------
        function switchTab(tabId, title, btnElement) {
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

        async function fetchLiveOrders() {
            try {
                const response = await fetch('/api/orders?_t=' + new Date().getTime());
                const data = await response.json();
                const tbody = document.getElementById('kds-table-body');
                tbody.innerHTML = '';
                
                if (data.orders.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 40px; color:#A1887F;">No active orders right now.</td></tr>';
                    return;
                }

                data.orders.forEach(order => {
                    let itemsHtml = order.items.map(i => {
                        return `<div style="margin-bottom:4px;"><strong>${escapeHTML(i.foundation)}</strong> <span class="kds-badge" style="background:#EFEBE4; color:#5D4037;">${escapeHTML(i.pearls)}</span></div>`;
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
                fetchLiveOrders();
            } catch (error) {
                console.error("Error updating order status:", error);
            }
        }
        
        setInterval(fetchLiveOrders, 3000);
        fetchLiveOrders();

        // ----------------------------------------
        // FEATURE 1: MANUAL ORDER (WALK-IN POS)
        // ----------------------------------------
        let quickCart =[];
        
        async function fetchQuickOrderMenu() {
            try {
                const res = await fetch('/api/menu?_t=' + new Date().getTime());
                const items = await res.json();
                const grid = document.getElementById('qo-menu-grid');
                grid.innerHTML = '';
                
                items.forEach(i => {
                    grid.innerHTML += `
                        <div class="qo-card" onclick="addToQuickCart('${escapeHTML(i.name.replace(/'/g, "\\'"))}', ${i.price})">
                            <h4>${escapeHTML(i.name)}</h4>
                            <p>₱${i.price.toFixed(2)}</p>
                        </div>
                    `;
                });
            } catch(e) {
                console.error("Error fetching menu for Quick Order.", e);
            }
        }

        function addToQuickCart(name, price) {
            quickCart.push({ 
                name: name, 
                price: price 
            });
            updateQuickCartUI();
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
                    list.innerHTML += `
                        <div class="qo-cart-item">
                            <div>
                                <div style="font-weight:700; color:#3E2723; font-size:0.9rem;">${escapeHTML(item.name)}</div>
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
                return alert("Cart is empty");
            }
            
            const total = quickCart.reduce((sum, item) => sum + item.price, 0);
            
            const payload = { 
                items: quickCart.map(i => ({ 
                    foundation: i.name, 
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
                        receiptBody += `<div style="display:flex; justify-content:space-between;"><span>1x ${escapeHTML(item.name)}</span><span>${item.price.toFixed(2)}</span></div>`;
                    });
                    
                    document.getElementById('receipt-body-content').innerHTML = receiptBody;
                    document.getElementById('receipt-modal').style.display = 'flex';
                    
                    // Clear state
                    quickCart =[];
                    updateQuickCartUI();
                    fetchLiveOrders();
                } else {
                    const data = await res.json();
                    alert("Error: " + data.message);
                }
            } catch(e) { 
                alert("Connection Error. Check the server."); 
            }
        }

        function closeReceiptModal() {
            document.getElementById('receipt-modal').style.display = 'none';
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
                    alert("Inventory updated successfully!"); 
                    fetchAdminInventory(); 
                } else { 
                    alert("Error saving inventory."); 
                }
            } catch(e) { 
                alert("Connection error."); 
            }
        }

        // ----------------------------------------
        // FEATURE 2: FINANCE & RECONCILIATION
        // ----------------------------------------
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
        }

        async function addExpense() {
            const desc = document.getElementById('exp-desc').value;
            const amt = document.getElementById('exp-amount').value;
            
            if(!desc || !amt) {
                return alert("Fill in description and amount.");
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
                    fetchDailyFinances();
                } else {
                    alert("Error saving expense.");
                }
            } catch(e) { 
                alert("Error saving expense. Connection dropped."); 
            }
        }

        async function uploadLegacyCSV() {
            const fileInput = document.getElementById('legacy-csv');
            if(!fileInput.files.length) {
                return alert("Select a CSV file first.");
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
                    alert("Success: " + result.message);
                } else {
                    alert("Import Error: " + (result.error || "Check file format"));
                }
            } catch(e) { 
                alert("Connection Error. Are you online?"); 
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
                
                closeMenuModal(); 
                fetchAdminMenu();
            } catch (error) {
                console.error("Error saving menu item", error);
            }
        }

        async function deleteMenu(id) {
            if (confirm("Delete this menu item?")) {
                try {
                    await fetch(`/api/menu/${id}`, { method: 'DELETE' }); 
                    fetchAdminMenu();
                } catch (error) {
                    console.error("Error deleting menu item", error);
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
                return alert("⚠️ Configure Opening Time, Closing Time, and PIN.");
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
                    
                    alert('Settings validated! Link Generated.');
                } else { 
                    alert("Error: " + data.error); 
                }
            } catch (e) { 
                alert("Error connecting to server."); 
            }
        }

        function copyLink() {
            var copyText = document.getElementById("posLink");
            
            if (copyText.value === "Pending Configuration..." || copyText.value === "") {
                return alert("⚠️ Generate link first.");
            }
            
            copyText.select(); 
            copyText.setSelectionRange(0, 99999);
            navigator.clipboard.writeText(copyText.value); 
            alert("Link Copied!");
        }

        function downloadBackup() { 
            window.location.href = "/api/backup"; 
        }

        async function restoreBackup() {
            const fileInput = document.getElementById('backup-file');
            
            if (!fileInput.files.length) {
                return alert("⚠️ Select a backup file (.json).");
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
                    alert("Error: " + data.message); 
                }
            } catch (e) { 
                alert("Connection error."); 
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
        
    return render_template_string(STOREFRONT_HTML, open_time=open_time, close_time=close_time)


@app.route('/api/otp/bypass', methods=['POST'])
def bypass_otp():
    """
    Temporary endpoint requested to bypass SMS verification completely.
    Accepts just the user's name and grants immediate verified access.
    """
    data = request.json
    name = str(data.get('name', '')).strip()
    
    if not name:
        return jsonify({"error": "Please enter your name."}), 400
        
    session['customer_verified'] = True
    session['customer_phone'] = 'Bypassed'
    session['customer_name'] = name
    
    return jsonify({"status": "verified"})


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    """
    Administrative gateway. Validates the master PIN hash before allowing access
    to the sensitive POS operations.
    """
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        
        if check_password_hash(ADMIN_PIN_HASH, pin):
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
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
    Requires the admin PIN to re-authenticate the action.
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
    
    return jsonify({"url": f"{request.host_url}?token={token}"})


@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    """
    GET: Returns the complete menu to either the customer or admin frontend.
         Also calculates available stock for each drink based dynamically on its recipe.
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
        return jsonify({"status": "success"})
        
    elif request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
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
            return jsonify({"status": "success"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/reserve', methods=['POST'])
@limiter.limit("5 per minute")
def reserve_blend():
    """
    Customer Endpoint. Validates stock in real-time, deducts inventory immediately, 
    and inserts the order into the active KDS queue under the 'Online' source.
    Applies a cooldown to prevent spamming.
    """
    data = request.json
    
    try:
        # --- NEW: COOLDOWN & ANTI-SPAM CHECK ---
        customer_name = data.get('name', '').strip()
        
        # 1. Check for Active "Preparing" Order under the same name
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
            
        # 2. Check 5-Minute Cooldown under the same name
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
        # ---------------------------------------

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
                sweetener=item['sweetener'], 
                pearls=item['pearls'], 
                item_total=item['price']
            )
            db.session.add(new_infusion)
            
        db.session.commit()
        
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
    Given a list of comma-separated order codes, returns their real-time statuses
    so the customer storefront can notify the user when their drink is ready.
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
    order.status = request.json.get('status', 'Completed')
    
    db.session.commit()
    
    return jsonify({"status": "success"})


@app.route('/api/orders')
def api_orders():
    """
    Admin Only. Returns the Live Kitchen Display System (KDS) array.
    Excludes purely historical 'Legacy Notebook' data so the screen doesn't clutter.
    """
    if not session.get('is_admin'): 
        return jsonify({"status": "error"}), 403
    
    all_reservations = Reservation.query.filter(
        Reservation.order_source != 'Legacy Notebook'
    ).order_by(Reservation.created_at.desc()).limit(50).all()
    
    orders_data =[]
    
    for res in all_reservations:
        items =[{'foundation': i.foundation, 'pearls': i.pearls} for i in res.infusions]
        
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
# 6. FEATURE: MANUAL ORDER (NOTEBOOK KILLER)
# ==========================================

@app.route('/api/admin/manual_order', methods=['POST'])
def admin_manual_order():
    """
    Admin Only. The "Notebook Killer".
    Allows staff to tap orders via the POS without a customer QR code.
    Records the order with a strict 'Manual/Notebook' source to distinguish from online.
    Deducts inventory immediately and responds with a printable receipt directive.
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
                sweetener="Standard", 
                pearls="Take-Out", 
                item_total=item['price']
            )
            db.session.add(inf)

            menu_item = MenuItem.query.filter_by(name=item['foundation']).first()
            if menu_item and menu_item.recipe:
                for r in menu_item.recipe: 
                    r.ingredient.stock -= r.quantity_required

        db.session.commit()
        
        return jsonify({
            "status": "success", 
            "print_receipt": True
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"System Error: {str(e)}"}), 500


# ==========================================
# 7. FEATURE: EXPENSE & RECONCILIATION
# ==========================================

@app.route('/api/finance/daily', methods=['GET'])
def daily_finance():
    """
    Admin Only. The "Petty Cash Ledger" brain.
    Calculates the exact monetary health of the shop for the current day.
    Retrieves total valid sales minus the logged expenses to yield Actual Profit.
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
        
        return jsonify({"status": "success"})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ==========================================
# 8. FEATURE: LEGACY IMPORT
# ==========================================

@app.route('/api/import_legacy', methods=['POST'])
def import_legacy():
    """
    Admin Only. Massive bulk ingest logic.
    Uses Python's pandas library to read an older CSV file (the "Legacy Notebook")
    and safely inject those historical rows into the digital system without 
    triggering inventory deductions or active live KDS notifications.
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
                    item_total=price
                )
                db.session.add(inf)
                
            imported_count += 1
            
        db.session.commit()
        
        return jsonify({
            "status": "success", 
            "message": f"Successfully injected {imported_count} historical records!"
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error parsing file: {str(e)}"}), 500


# ==========================================
# 9. SYSTEM BACKUP AND RESTORE
# ==========================================

@app.route('/api/backup', methods=['GET'])
def export_backup():
    """
    Admin Only. Dumps the entire PostgreSQL relational structure into a flat JSON payload.
    Crucial for safety and server migration.
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
                "pearls": i.pearls, 
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
        
        # Clear database dependencies properly with cascading
        db.session.execute(db.text('TRUNCATE TABLE expenses, infusions, reservations, recipe_items, menu_items, ingredients RESTART IDENTITY CASCADE;'))
        
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
            db.session.add(Infusion(**i))
            
        for e in data.get('expenses',[]):
            e['created_at'] = datetime.fromisoformat(e['created_at'])
            db.session.add(Expense(**e))
            
        db.session.commit()
        
        # Reset sequences for Postgres integrity
        db.session.execute(db.text("SELECT setval('ingredients_id_seq', coalesce((SELECT MAX(id)+1 FROM ingredients), 1), false)"))
        db.session.execute(db.text("SELECT setval('menu_items_id_seq', coalesce((SELECT MAX(id)+1 FROM menu_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('recipe_items_id_seq', coalesce((SELECT MAX(id)+1 FROM recipe_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('reservations_id_seq', coalesce((SELECT MAX(id)+1 FROM reservations), 1), false)"))
        db.session.execute(db.text("SELECT setval('infusions_id_seq', coalesce((SELECT MAX(id)+1 FROM infusions), 1), false)"))
        db.session.execute(db.text("SELECT setval('expenses_id_seq', coalesce((SELECT MAX(id)+1 FROM expenses), 1), false)"))
        
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
        
        # ---------------------------------------------------------
        # AUTO-MIGRATE DATABASE SCHEMAS
        # ---------------------------------------------------------
        try:
            db.session.execute(db.text("ALTER TABLE reservations ADD COLUMN order_source VARCHAR(30) DEFAULT 'Online'"))
            db.session.commit()
        except Exception:
            db.session.rollback() 
            
        try:
            db.session.execute(db.text("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY, 
                    description VARCHAR(200) NOT NULL, 
                    amount FLOAT NOT NULL, 
                    created_at TIMESTAMP
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()
            
        # ---------------------------------------------------------
        
        # Only populate if database is brand new 
        if Ingredient.query.count() == 0:
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
                {'name': 'Plastic Cups & Lids', 'unit': 'pcs', 'stock': 500.0}
            ]
            
            for i in ingredients_data: 
                db.session.add(Ingredient(**i))
                
            db.session.commit()

        if MenuItem.query.count() == 0:
            menu_data =[
                {
                    "name": "The Midnight Velvet", 
                    "price": 118.00, 
                    "letter": "M", 
                    "category": "Signature Series"
                },
                {
                    "name": "The Jade Garden", 
                    "price": 122.00, 
                    "letter": "J", 
                    "category": "Signature Series"
                },
                {
                    "name": "Classic Pearl Milk Tea", 
                    "price": 95.00, 
                    "letter": "C", 
                    "category": "Classic Milk Tea"
                },
                {
                    "name": "Taro Symphony", 
                    "price": 105.00, 
                    "letter": "T", 
                    "category": "Matcha & Taro"
                },
                {
                    "name": "Brown Sugar Deerioca", 
                    "price": 120.00, 
                    "letter": "B", 
                    "category": "Signature Series"
                },
                {
                    "name": "Wintermelon Frost", 
                    "price": 90.00, 
                    "letter": "W", 
                    "category": "Fruit Infusions"
                },
                {
                    "name": "Matcha Espresso", 
                    "price": 130.00, 
                    "letter": "M", 
                    "category": "Matcha & Taro"
                },
                {
                    "name": "Matcha Strawberry", 
                    "price": 125.00, 
                    "letter": "M", 
                    "category": "Matcha & Taro"
                },
                {
                    "name": "Strawberry Lychee", 
                    "price": 110.00, 
                    "letter": "S", 
                    "category": "Fruit Infusions"
                }
            ]
            
            for m_data in menu_data: 
                db.session.add(MenuItem(**m_data))
                
            db.session.commit()

            def add_recipe(item_name, ingredient_name, qty):
                """
                Helper function to generate the complex many-to-many relationship
                between Menus and Ingredients effortlessly during seeding.
                """
                item = MenuItem.query.filter_by(name=item_name).first()
                ing = Ingredient.query.filter_by(name=ingredient_name).first()
                
                if item and ing: 
                    db.session.add(RecipeItem(
                        menu_item_id=item.id, 
                        ingredient_id=ing.id, 
                        quantity_required=qty
                    ))

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

            add_recipe("Strawberry Lychee", "Jasmine Green Tea", 200)
            add_recipe("Strawberry Lychee", "Strawberry Syrup", 20)
            add_recipe("Strawberry Lychee", "Lychee Syrup", 20)
            add_recipe("Strawberry Lychee", "Plastic Cups & Lids", 1)

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