"""
9599 Tea & Coffee — ADMIN PANEL
================================
This file serves the admin-only panel with full access to:
  - Live Orders (KDS)
  - Inventory Management
  - Finance & Reports
  - Audit Trail
  - Settings & Menu Management

Run separately from the employee site (app.py).
Access via: http://localhost:5001/login
"""

import os
import uuid
import socket
import requests
from flask import (
    Flask,
    render_template_string,
    request,
    jsonify,
    session,
    redirect,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ==========================================
# 1. SECURITY CONFIGURATION
# ==========================================

app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-9599-admin-key')

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
    if 'api' in request.path:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
    return response

# ==========================================
# 2. DATABASE CONFIGURATION (shared DB)
# ==========================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, 'milktea_system.db')

database_url = os.environ.get('DATABASE_URL', f'sqlite:///{DEFAULT_DB_PATH}')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

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
# 3. DATABASE MODELS (shared with employee site)
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
# 4. ADMIN HTML TEMPLATES
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
        .admin-badge { background: #3E2723; color: #F5EFE6; font-size: 0.75rem; font-weight: 700; padding: 4px 12px; border-radius: 20px; display: inline-block; margin-bottom: 16px; letter-spacing: 1px; text-transform: uppercase; }
    </style>
</head>
<body>
    <div class="login-box">
        <i class="fas fa-shield-alt" style="font-size: 3rem; color: #6F4E37; margin-bottom: 12px;"></i>
        <div class="admin-badge">Admin Panel</div>
        <h2>Admin Access</h2>
        <p>Enter master PIN for 9599 Store System</p>
        {% if error %}<div class="error"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="pin" class="input-pin" placeholder="•••••" required autofocus>
            <button type="submit" class="btn-login"><i class="fas fa-lock"></i> Login Securely</button>
        </form>
    </div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>9599 Admin Panel</title>
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
        .sidebar { width: 230px; background: #3E2723; border-right: 1px solid #2a1a17; display: flex; flex-direction: column; z-index: 100; }
        .sidebar-header { padding: 28px 25px 20px; }
        .sidebar-header .brand { font-family: 'Playfair Display', serif; font-size: 1.15rem; font-weight: 900; color: #F5EFE6; }
        .sidebar-header .brand-sub { font-size: 0.72rem; color: #A67B5B; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; margin-top: 2px; }
        .admin-pill { background: #6F4E37; color: #F5EFE6; font-size: 0.7rem; font-weight: 800; padding: 3px 10px; border-radius: 20px; display: inline-block; margin-top: 8px; letter-spacing: 1px; }
        .nav-links { flex: 1; display: flex; flex-direction: column; padding-top: 8px; }
        .nav-item { padding: 13px 25px; color: #A67B5B; font-weight: 600; font-size: 0.92rem; display: flex; align-items: center; gap: 12px; cursor: pointer; border-left: 4px solid transparent; transition: all 0.15s; }
        .nav-item:hover { background: rgba(255,255,255,0.06); color: #F5EFE6; }
        .nav-item.active { background: rgba(255,255,255,0.1); color: #F5EFE6; border-left: 4px solid #A67B5B; font-weight: 800; }
        .sidebar-footer { padding: 20px 25px; border-top: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; gap: 8px; }
        .btn-reload { width: 100%; background: rgba(255,255,255,0.08); color: #F5EFE6; border: none; padding: 10px; border-radius: 8px; font-weight: 700; cursor: pointer; font-family: inherit; }
        .btn-logout { width: 100%; background: #FFEBEE; color: #C62828; border: 1px solid #FFCDD2; padding: 10px; border-radius: 8px; font-weight: 700; cursor: pointer; font-family: inherit; }
        .main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        .topbar { background: white; padding: 18px 36px; border-bottom: 1px solid #EFEBE4; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
        .page-title { font-size: 1.35rem; font-weight: 800; color: #0F172A; }
        .content-body { padding: 24px; overflow: hidden; flex: 1; display: flex; flex-direction: column; }
        .tab-pane { display: none; flex: 1; flex-direction: column; min-height: 0; overflow: hidden; }
        .tab-pane.active { display: flex; }
        .settings-card { background: white; border-radius: 12px; padding: 24px; border: 1px solid #EFEBE4; display: flex; flex-direction: column; min-height: 0; margin-bottom: 20px; }
        .card-title { font-size: 1rem; font-weight: 800; color: #3E2723; margin-bottom: 15px; }
        .table-responsive { flex: 1; overflow-y: auto; border: 1px solid #EFEBE4; border-radius: 8px; }
        .kds-table { width: 100%; border-collapse: collapse; }
        .kds-table th, .kds-table td { padding: 14px 18px; text-align: left; border-bottom: 1px solid #EFEBE4; font-size: 0.85rem; }
        .kds-table th { background: #F5EFE6; color: #5D4037; position: sticky; top: 0; font-weight: 800; }
        .kds-badge { font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
        .btn-blue { background: #6F4E37; color: white; border: none; padding: 12px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; width: 100%; margin-bottom: 10px; font-family: inherit; }
        .btn-dark { background: #3E2723; color: white; border: none; padding: 10px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; font-family: inherit; }
        .input-group label { display: block; font-size: 0.75rem; font-weight: 800; color: #8D6E63; margin-bottom: 8px; }
        .input-pin { width: 100%; padding: 12px; border: 1px solid #D7CCC8; border-radius: 8px; margin-bottom: 15px; font-weight: 600; outline: none; font-family: inherit; font-size: 0.9rem; }
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(62,39,35,0.6); align-items: center; justify-content: center; }
        .modal-content { background: white; padding: 30px; border-radius: 12px; width: 420px; border: 2px solid #6F4E37; }
        .error-state { padding: 40px; text-align: center; color: #C62828; font-weight: 600; }
        .settings-grid-layout { display: grid; grid-template-columns: 340px 1fr; gap: 20px; height: 100%; overflow: hidden; }
        .notif-bell { cursor: pointer; position: relative; padding: 10px; border-radius: 50%; background: #F5EFE6; color: #6F4E37; border: none; font-size: 1.2rem; }
        .notif-badge { position: absolute; top: -5px; right: -5px; background: #C62828; color: white; border-radius: 50%; padding: 2px 6px; font-size: 0.7rem; font-weight: 800; display: none; }
        .notif-panel { display: none; position: absolute; top: 60px; right: 36px; background: white; border: 1px solid #EFEBE4; border-radius: 12px; width: 300px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); z-index: 1000; flex-direction: column; max-height: 400px; }
        .notif-panel-header { padding: 15px; border-bottom: 1px solid #EFEBE4; font-weight: 800; display: flex; justify-content: space-between; align-items: center; }
        .notif-panel-body { padding: 10px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 10px; }
        .notif-item { padding: 10px; border-radius: 8px; background: #FDFBF7; border: 1px solid #EFEBE4; font-size: 0.85rem; }
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
        <div class="sidebar-header">
            <div class="brand">9599 Tea & Coffee</div>
            <div class="brand-sub">Management System</div>
            <div class="admin-pill"><i class="fas fa-shield-alt"></i> Admin Panel</div>
        </div>
        <nav class="nav-links">
            <div class="nav-item active" onclick="switchTab('kds', 'Live Orders', this)"><i class="fas fa-clipboard-list"></i> Live Orders</div>
            <div class="nav-item" onclick="switchTab('inventory', 'Inventory', this)"><i class="fas fa-boxes"></i> Inventory</div>
            <div class="nav-item" onclick="switchTab('finance', 'Finance & Reports', this)"><i class="fas fa-chart-line"></i> Finance</div>
            <div class="nav-item" onclick="switchTab('audit', 'Audit Trail', this)"><i class="fas fa-list-ol"></i> Audit Trail</div>
            <div class="nav-item" onclick="switchTab('settings', 'Settings & Menu', this)"><i class="fas fa-sliders-h"></i> Settings</div>
        </nav>
    </div>
    <div class="sidebar-footer">
        <button class="btn-reload" onclick="location.reload()">Reload UI</button>
        <button class="btn-logout" onclick="location.href='/logout'">Lock Panel</button>
    </div>
</aside>

<main class="main-content">
    <header class="topbar">
        <div class="page-title" id="page-title">Live Orders</div>
        <div style="display:flex; align-items:center; gap:15px; position:relative;">
            <div id="clock" style="font-weight:bold; background:#F5EFE6; padding:8px 15px; border-radius:20px; font-size:0.9rem;">00:00 PM</div>
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
            <div class="table-responsive">
                <table class="kds-table">
                    <thead><tr><th>Order #</th><th>Source</th><th>Name</th><th>Time</th><th>Total</th><th>Items</th><th>Status</th></tr></thead>
                    <tbody id="kds-table-body"></tbody>
                </table>
            </div>
        </div>

        <!-- INVENTORY -->
        <div id="tab-inventory" class="tab-pane">
            <div class="settings-card" style="height:100%;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                    <div class="card-title" style="margin:0;">Raw Ingredients Stock</div>
                    <button class="btn-dark" onclick="fetchAdminInventory()">Refresh</button>
                </div>
                <div class="table-responsive">
                    <table class="kds-table">
                        <thead><tr><th>Ingredient</th><th>Unit</th><th>Current Stock</th></tr></thead>
                        <tbody id="admin-inventory-list"></tbody>
                    </table>
                </div>
                <button class="btn-blue" style="margin-top:20px;" onclick="saveInventory()"><i class="fas fa-save"></i> Save Inventory</button>
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
                        <input type="text" id="exp-desc" class="input-pin" placeholder="Description (e.g. Ice, Packaging)">
                        <input type="number" id="exp-amount" class="input-pin" placeholder="Amount (₱)">
                        <button class="btn-blue" onclick="addExpense()"><i class="fas fa-plus"></i> Record Expense</button>
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

        <!-- AUDIT TRAIL -->
        <div id="tab-audit" class="tab-pane">
            <div class="settings-card" style="height:100%;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                    <div class="card-title" style="margin:0;">System Audit Logs</div>
                    <button class="btn-dark" onclick="fetchAuditLogs()">Refresh Logs</button>
                </div>
                <div class="table-responsive">
                    <table class="kds-table">
                        <thead><tr><th>Time</th><th>Action</th><th>Details</th></tr></thead>
                        <tbody id="audit-table-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- SETTINGS -->
        <div id="tab-settings" class="tab-pane">
            <div class="settings-grid-layout">
                <div style="display:flex; flex-direction:column; overflow-y:auto; gap:16px; height:100%; padding-bottom:20px; padding-right:4px;">
                    <div class="settings-card" style="padding:20px; margin-bottom:0; flex-shrink:0;">
                        <div class="card-title" style="margin-bottom:14px;">Store Link Generator</div>
                        <div style="background:#F5EFE6; border:1px solid #EFEBE4; border-radius:12px; padding:16px;">
                            <div style="font-size:0.82rem; color:#6F4E37; line-height:1.6; margin-bottom:12px;">
                                Generate a <b>permanent</b> ordering link for the employee/customer site. The system automatically opens and closes based on the store schedule.
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
                                    <button onclick="openInChrome()" title="Open in new tab" style="background:#1a73e8; border:none; border-radius:6px; color:#fff; width:32px; height:32px; cursor:pointer; display:flex; align-items:center; justify-content:center;">
                                        <i class="fas fa-external-link-alt" style="font-size:12px;"></i>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="settings-card" style="padding:20px; margin-bottom:0; flex-shrink:0;">
                        <div class="card-title" style="margin-bottom:6px;">Backup & Recovery</div>
                        <p style="font-size:0.85rem; color:#8D6E63; margin-bottom:18px; line-height:1.6;">
                            Download a full backup of all orders, customers, expenses, and menu data. Upload a backup file to restore.
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
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                        <div class="card-title" style="margin:0;">Menu Management</div>
                        <button class="btn-dark" onclick="openMenuModal()">+ Add Item</button>
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

<!-- Menu Item Modal -->
<div id="menu-modal" class="modal">
    <div class="modal-content">
        <h2 id="menu-modal-title" style="margin-bottom:20px;">Add Menu Item</h2>
        <form id="menu-form" onsubmit="saveMenuItem(event)">
            <input type="text" id="menu-name" class="input-pin" placeholder="Item Name" required>
            <input type="number" id="menu-price" class="input-pin" placeholder="Price (₱)" required>
            <input type="text" id="menu-category" class="input-pin" placeholder="Category" required>
            <input type="text" id="menu-letter" class="input-pin" placeholder="Short Letter (e.g. T)" required>
            <label style="display:flex; align-items:center; gap:10px; font-size:0.85rem; font-weight:700; color:#5D4037; margin-bottom:15px; cursor:pointer;">
                <input type="checkbox" id="menu-out-of-stock" style="width:18px; height:18px; cursor:pointer;">
                Mark as Out of Stock
            </label>
            <div style="display:flex; gap:10px;">
                <button type="button" class="btn-dark" style="flex:1;" onclick="document.getElementById('menu-modal').style.display='none'">Cancel</button>
                <button type="submit" class="btn-blue" style="flex:1; margin-bottom:0;">Save Item</button>
            </div>
        </form>
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

    function pingSession() { fetch('/admin/api/ping'); }
    setInterval(pingSession, 30000);

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
        try { document.getElementById('admin-audio').play().catch(()=>{}); } catch(e){}
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
        adminNotifs.unshift({code: orderCode, name, total, time: new Date().toLocaleTimeString()});
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
                        <span style="font-size:0.8rem; color:#8D6E63;">${escapeHTML(n.address||'')}</span><br>
                        <span style="font-size:0.8rem;">${escapeHTML(n.message||'')}</span>
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
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="7" class="error-state">⚠️ Database Connection Error.</td></tr>'; return; }
            const data = await res.json();
            let currentIds = new Set();
            data.orders.forEach(o => currentIds.add(o.id));
            if (!firstLoad) {
                data.orders.forEach(o => { if(!lastKnownOrderIds.has(o.id)) addAdminNotif(o.code, o.name, o.total); });
            }
            lastKnownOrderIds = currentIds;
            firstLoad = false;
            tbody.innerHTML = '';
            if(data.orders.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:30px; color:#A67B5B;">No active orders.</td></tr>';
                return;
            }
            data.orders.forEach(o => {
                let itemsHTML = o.items.map(i => `<div style="margin-bottom:5px;"><b>${escapeHTML(i.foundation)}</b> (${escapeHTML(i.size)})<br><span style="font-size:0.75rem; color:#8D6E63;">${escapeHTML(i.sweetener)} | ${escapeHTML(i.ice)}${i.addons ? ' | + ' + escapeHTML(i.addons) : ''}</span></div>`).join('');
                let items;
                if (o.items.length >= 2) {
                    const toggleId = `items-toggle-${o.id}`;
                    const listId = `items-list-${o.id}`;
                    items = `<div><button id="${toggleId}" onclick="toggleOrderItems('${listId}', '${toggleId}')" style="background:#F5EFE6; border:1px solid #D7CCC8; border-radius:6px; padding:5px 10px; font-size:0.8rem; font-weight:700; color:#5D4037; cursor:pointer;"><i class="fas fa-chevron-down"></i> ${o.items.length} items</button><div id="${listId}" style="display:none; margin-top:8px;">${itemsHTML}</div></div>`;
                } else { items = itemsHTML; }
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

    async function fetchAdminInventory() {
        const tbody = document.getElementById('admin-inventory-list');
        try {
            const res = await apiFetch('/api/inventory');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            tbody.innerHTML = data.map(i => {
                const pct = i.stock / 1000;
                const color = i.stock <= 0 ? '#C62828' : (pct < 0.3 ? '#F57C00' : '#388E3C');
                return `<tr>
                    <td><b>${escapeHTML(i.name)}</b></td>
                    <td style="color:#8D6E63;">${escapeHTML(i.unit)}</td>
                    <td>
                        <input type="number" class="input-pin stock-input" data-id="${i.id}" value="${i.stock}"
                            style="width:110px; padding:6px 10px; margin:0; border-color:${color}; color:${color}; font-weight:800;">
                    </td>
                </tr>`;
            }).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Network Error</td></tr>'; }
    }

    async function saveInventory() {
        const payload = Array.from(document.querySelectorAll('.stock-input')).map(input => ({ id: parseInt(input.getAttribute('data-id')), stock: parseFloat(input.value) }));
        try {
            const res = await apiFetch('/api/inventory', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
            if(res && res.ok) showToast("Inventory Saved", "success");
        } catch(e) { showToast("Error saving inventory", "error"); }
    }

    async function fetchDailyFinances() {
        try {
            const res = await apiFetch('/api/finance/daily');
            if(!res || !res.ok) { document.getElementById('sys-total').innerText = "DB Error"; return; }
            const data = await res.json();
            document.getElementById('sys-total').innerText = `₱${data.system_total.toFixed(2)}`;
            document.getElementById('expense-total').innerText = `- ₱${data.expenses_total.toFixed(2)}`;
            document.getElementById('cash-drawer').innerText = `₱${(data.system_total - data.expenses_total).toFixed(2)}`;
            // Top sellers from orders
            const sellerMap = {};
            (data.expenses || []).forEach(x => {
                const key = x.desc;
                if(!sellerMap[key]) sellerMap[key] = 0;
                sellerMap[key] += x.amount;
            });
            const bsList = document.getElementById('best-sellers-list');
            if(data.expenses && data.expenses.length) {
                bsList.innerHTML = data.expenses.map(x => `<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #EFEBE4;font-size:0.88rem;">
                    <span>${escapeHTML(x.desc)}</span><span style="color:#C62828;font-weight:700;">-₱${x.amount.toFixed(2)}</span></div>`).join('');
            } else {
                bsList.innerHTML = '<div style="color:#A67B5B; padding:12px 0; font-size:0.88rem;">No expenses recorded today.</div>';
            }
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
            tbody.innerHTML = data.map(l => `<tr>
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
            if(data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding:20px; color:#A67B5B;">No audit logs yet.</td></tr>';
                return;
            }
            tbody.innerHTML = data.map(l => `<tr>
                <td style="font-size:0.8rem; white-space:nowrap;">${escapeHTML(l.time)}</td>
                <td><b>${escapeHTML(l.action)}</b></td>
                <td style="color:#5D4037;">${escapeHTML(l.details||'')}</td>
            </tr>`).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="3" class="error-state">Network Error</td></tr>'; }
    }

    let editMenuId = null;
    async function fetchAdminMenu() {
        const tbody = document.getElementById('admin-menu-list');
        try {
            const res = await apiFetch('/api/menu');
            if(!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="5" class="error-state">Database Error</td></tr>'; return; }
            const data = await res.json();
            tbody.innerHTML = data.map(m => {
                const oos = m.is_out_of_stock;
                const stockBadge = oos
                    ? `<span style="background:#FFEBEE; color:#C62828; padding:3px 8px; border-radius:4px; font-size:0.75rem; font-weight:700;">Out of Stock</span>`
                    : `<span style="background:#E8F5E9; color:#2E7D32; padding:3px 8px; border-radius:4px; font-size:0.75rem; font-weight:700;">In Stock</span>`;
                const toggleBtn = `<button class="btn-dark" style="padding:5px 10px; background:${oos ? '#388E3C' : '#C62828'};" onclick="toggleOutOfStock(${m.id}, ${!oos})">${oos ? '✅ Mark In Stock' : '🚫 Mark OOS'}</button>`;
                return `<tr>
                    <td><b>${escapeHTML(m.name)}</b></td>
                    <td>${escapeHTML(m.category)}</td>
                    <td>₱${m.price}</td>
                    <td>${stockBadge}</td>
                    <td style="display:flex; gap:6px; flex-wrap:wrap; padding:8px 18px;">
                        <button class="btn-dark" style="padding:5px 10px;" onclick="openMenuModal(${m.id}, '${escapeHTML(m.name.replace(/'/g,\"\\\\'\"  ))}', ${m.price}, '${escapeHTML(m.category.replace(/'/g,\"\\\\'\"  ))}', '${escapeHTML(m.letter)}', ${oos})">Edit</button>
                        ${toggleBtn}
                    </td>
                </tr>`;
            }).join('');
        } catch(e) { tbody.innerHTML = '<tr><td colspan="5" class="error-state">Network Error</td></tr>'; }
    }

    function openMenuModal(id=null, name='', price='', cat='', letter='', outOfStock=false) {
        editMenuId = id;
        document.getElementById('menu-modal-title').innerText = id ? 'Edit Item' : 'Add Menu Item';
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
            const res = await apiFetch(`/api/menu/${id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name: item.name, price: item.price, category: item.category, letter: item.letter, is_out_of_stock: newState }) });
            if (res && res.ok) { showToast(newState ? "Marked Out of Stock" : "Marked In Stock", "success"); fetchAdminMenu(); }
        } catch(e) { showToast("Error updating stock status", "error"); }
    }

    async function addExpense() {
        const desc = document.getElementById('exp-desc').value.trim();
        const amount = document.getElementById('exp-amount').value;
        if(!desc || !amount) return showToast("Fill all fields", "error");
        try {
            const res = await apiFetch('/api/expenses', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({description:desc, amount}) });
            if(res && res.ok) { showToast("Expense logged", "success"); document.getElementById('exp-desc').value=''; document.getElementById('exp-amount').value=''; fetchDailyFinances(); }
        } catch(e) { showToast("Error logging expense", "error"); }
    }

    async function saveConfigurations() {
        const pin = document.getElementById('store-pin').value;
        if(!pin) { showToast("Enter your Master PIN", "error"); return; }
        try {
            const res = await apiFetch('/api/generate_link', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ pin }) });
            const data = await res.json();
            if(res && res.ok) { document.getElementById('posLink').value = data.url; showToast("Permanent link generated!", "success"); }
            else { showToast(data.error || "Error", "error"); }
        } catch(e) { showToast("Error", "error"); }
    }

    function copyPosLink() {
        const el = document.getElementById('posLink');
        if(!el.value) { showToast("Generate a link first", "error"); return; }
        navigator.clipboard.writeText(el.value).then(() => showToast("Link copied!", "success")).catch(() => { el.select(); document.execCommand('copy'); showToast("Link copied!", "success"); });
    }

    function openInChrome() {
        const el = document.getElementById('posLink');
        if(!el.value) { showToast("Generate a link first", "error"); return; }
        window.open(el.value, '_blank');
        showToast("Link opened in new tab!", "success");
    }

    async function downloadBackup() {
        const status = document.getElementById('backup-status');
        status.style.color = '#8D6E63'; status.innerText = 'Preparing backup...';
        try {
            const res = await apiFetch('/api/backup');
            if(!res || !res.ok) { status.style.color='#C62828'; status.innerText='Backup failed.'; return; }
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = `9599_backup_${new Date().toISOString().slice(0,10)}.json`; a.click();
            URL.revokeObjectURL(url);
            status.style.color = '#388E3C'; status.innerText = '✅ Backup downloaded.';
        } catch(e) { status.style.color='#C62828'; status.innerText='Network error.'; }
    }

    async function restoreBackup(input) {
        const status = document.getElementById('backup-status');
        const file = input.files[0]; if(!file) return;
        status.style.color = '#8D6E63'; status.innerText = 'Reading backup file...';
        const reader = new FileReader();
        reader.onload = async (e) => {
            try {
                const payload = JSON.parse(e.target.result);
                status.innerText = 'Restoring... please wait.';
                const res = await apiFetch('/api/restore', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
                if(res && res.ok) { status.style.color = '#388E3C'; status.innerText = '✅ Restore complete! Refreshing...'; setTimeout(() => location.reload(), 1500); }
                else { status.style.color = '#C62828'; status.innerText = '❌ Restore failed.'; }
            } catch(err) { status.style.color = '#C62828'; status.innerText = '❌ Invalid JSON file.'; }
        };
        reader.readAsText(file); input.value = '';
    }

    // Permission requests
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
                    tbody.innerHTML = data.map(p => `<tr id="perm-row-${p.id}">
                        <td style="font-size:0.8rem;">${escapeHTML(p.time)}</td>
                        <td><b>${escapeHTML(p.code)}</b></td>
                        <td><b>${escapeHTML(p.name)}</b><br><span style="font-size:0.75rem; color:#8D6E63;">${escapeHTML(p.address||'')}</span></td>
                        <td style="font-size:0.85rem;">${escapeHTML(p.message||'')}</td>
                        <td><button class="btn-blue" style="padding:6px 14px; margin-bottom:0;" onclick="grantPermission(${p.id}, '${escapeHTML(p.name)}', '${escapeHTML(p.code)}')">✅ Grant</button></td>
                    </tr>`).join('');
                }
            }
            if(!firstPermLoad) {
                data.forEach(p => {
                    if(!knownPermCodes.has(p.code)) {
                        playNotificationSound();
                        showToast(`🔔 Permission request from ${p.name} (${p.code})`, "error");
                    }
                });
            }
            data.forEach(p => knownPermCodes.add(p.code));
            firstPermLoad = false;
        } catch(e) {}
    }

    async function grantPermission(id, name, code) {
        try {
            const res = await apiFetch(`/api/permission_requests/${id}/grant`, {method:'POST'});
            if(res && res.ok) { showToast(`✅ Permission granted for ${name}`, "success"); knownPermCodes.delete(code); fetchPermissionRequests(); }
        } catch(e) { showToast("Error granting permission", "error"); }
    }

    setInterval(() => { if(document.getElementById('tab-kds').classList.contains('active')) fetchLiveOrders(); }, 5000);
    setInterval(fetchPermissionRequests, 5000);
    fetchLiveOrders();
    fetchPermissionRequests();
</script>
</body>
</html>
"""

# ==========================================
# 5. ADMIN AUTH & ROUTE GUARDS
# ==========================================

@app.before_request
def require_admin():
    public = ['/login', '/logout', '/static']
    if any(request.path.startswith(p) for p in public):
        return
    if not session.get('is_admin'):
        if request.path.startswith('/api'):
            return jsonify({"error": "Unauthorized"}), 403
        return redirect(url_for('admin_login'))

# ==========================================
# 6. ROUTES
# ==========================================

@app.route('/')
def index():
    return redirect(url_for('admin_dashboard'))

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def admin_login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if check_password_hash(ADMIN_PIN_HASH, pin):
            session.permanent = True
            session['is_admin'] = True
            session['admin_id'] = str(uuid.uuid4())
            # Update system state
            state = SystemState.query.first()
            if not state:
                state = SystemState(active_session_id='', last_ping=datetime.min)
                db.session.add(state)
            state.active_session_id = session['admin_id']
            state.last_ping = datetime.utcnow()
            db.session.commit()
            log_audit("Admin Login", "Admin panel access granted")
            return redirect(url_for('admin_dashboard'))
        error = "Invalid PIN. Access Denied."
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def admin_logout():
    admin_id = session.pop('admin_id', None)
    session.pop('is_admin', None)
    state = SystemState.query.first()
    if state and state.active_session_id == admin_id:
        state.active_session_id = ''
        db.session.commit()
    log_audit("Admin Logout", "Admin panel session ended")
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin_dashboard():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/api/ping')
def admin_ping():
    if session.get('is_admin'):
        state = SystemState.query.first()
        if state and state.active_session_id == session.get('admin_id'):
            state.last_ping = datetime.utcnow()
            db.session.commit()
    return jsonify({"status": "ok"})

# ── Proxied API endpoints (same DB as employee site) ──

@app.route('/api/orders')
def api_orders():
    res = Reservation.query.filter(Reservation.order_source != 'Legacy Notebook').order_by(Reservation.created_at.desc()).limit(50).all()
    return jsonify({'orders': [{'id': r.id, 'code': r.reservation_code, 'source': r.order_source, 'name': r.patron_name, 'total': r.total_investment, 'status': r.status, 'pickup_time': r.pickup_time, 'items': [{'foundation': i.foundation, 'size': i.cup_size, 'addons': i.addons, 'sweetener': i.sweetener, 'ice': i.ice_level} for i in r.infusions]} for r in res]})

@app.route('/api/orders/<int:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    order = Reservation.query.get_or_404(order_id)
    order.status = request.json.get('status', 'Completed')
    db.session.commit()
    log_audit("Order Status Updated", f"Order #{order.reservation_code} → {order.status}")
    return jsonify({"status": "success"})

@app.route('/api/inventory', methods=['GET', 'PUT'])
def handle_inventory():
    if request.method == 'GET':
        ings = Ingredient.query.order_by(Ingredient.name).all()
        return jsonify([{"id": i.id, "name": i.name, "unit": i.unit, "stock": i.stock} for i in ings])
    for item_data in request.json:
        ing = Ingredient.query.get(item_data['id'])
        if ing:
            ing.stock = float(item_data['stock'])
    db.session.commit()
    log_audit("Inventory Updated", "Stock levels saved by admin")
    return jsonify({"status": "success"})

@app.route('/api/finance/daily', methods=['GET'])
def daily_finance():
    now = get_ph_time()
    s = now.replace(hour=0, minute=0, second=0)
    e = now.replace(hour=23, minute=59, second=59)
    ords = Reservation.query.filter(Reservation.created_at >= s, Reservation.created_at <= e).all()
    exps = Expense.query.filter(Expense.created_at >= s, Expense.created_at <= e).all()
    return jsonify({
        "system_total": sum(o.total_investment for o in ords),
        "expenses_total": sum(x.amount for x in exps),
        "expenses": [{"desc": x.description, "amount": x.amount} for x in exps]
    })

@app.route('/api/expenses', methods=['POST'])
def add_expense():
    db.session.add(Expense(description=request.json['description'], amount=float(request.json['amount'])))
    db.session.commit()
    log_audit("Expense Logged", f"{request.json['description']}: ₱{request.json['amount']}")
    return jsonify({"status": "success"})

@app.route('/api/audit_logs', methods=['GET'])
def get_audit_logs():
    return jsonify([{"action": l.action, "details": l.details, "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')} for l in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(100).all()])

@app.route('/api/customer_logs', methods=['GET'])
def get_customer_logs():
    logs = CustomerLog.query.order_by(CustomerLog.created_at.desc()).limit(200).all()
    return jsonify([{"id": l.id, "name": l.full_name, "gmail": l.gmail, "phone": l.phone, "source": l.order_source, "total": l.order_total, "time": l.created_at.strftime('%Y-%m-%d %I:%M %p')} for l in logs])

@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
    if request.method == 'GET':
        items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
        return jsonify([{"id": i.id, "name": i.name, "price": i.price, "letter": i.letter, "category": i.category, "is_out_of_stock": i.is_out_of_stock} for i in items])
    data = request.json
    new_item = MenuItem(name=data['name'], price=float(data['price']), letter=data['letter'][:2].upper(), category=data['category'], is_out_of_stock=bool(data.get('is_out_of_stock', False)))
    db.session.add(new_item)
    db.session.commit()
    log_audit("Menu Item Added", f"Added: {data['name']}")
    return jsonify({"status": "success"})

@app.route('/api/menu/<int:item_id>', methods=['PUT', 'DELETE'])
def handle_menu_item(item_id):
    item = MenuItem.query.get_or_404(item_id)
    if request.method == 'PUT':
        data = request.json
        item.name = data['name']
        item.price = float(data['price'])
        item.letter = data['letter'][:2].upper()
        item.category = data['category']
        item.is_out_of_stock = bool(data.get('is_out_of_stock', False))
        db.session.commit()
        log_audit("Menu Item Updated", f"Updated: {item.name}")
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
        log_audit("Menu Item Deleted", f"Deleted: {item.name}")
        return jsonify({"status": "success"})

@app.route('/api/permission_requests', methods=['GET'])
def get_permission_requests():
    pending = PermissionRequest.query.filter_by(granted=False).order_by(PermissionRequest.created_at.desc()).all()
    return jsonify([{"id": p.id, "code": p.request_code, "name": p.customer_name, "address": p.address, "message": p.message, "time": p.created_at.strftime('%I:%M %p')} for p in pending])

@app.route('/api/permission_requests/<int:req_id>/grant', methods=['POST'])
def grant_permission(req_id):
    pr = PermissionRequest.query.get_or_404(req_id)
    pr.granted = True
    db.session.commit()
    log_audit("Permission Granted", f"Code: {pr.request_code} for {pr.customer_name}")
    return jsonify({"status": "success"})

@app.route('/api/generate_link', methods=['POST'])
def generate_link():
    data = request.json
    pin = data.get('pin')
    if not check_password_hash(ADMIN_PIN_HASH, pin):
        return jsonify({"error": "Invalid PIN"}), 401
    token = token_serializer.dumps({'store': '9599', 'v': 2})
    log_audit("Store Link Generated", "Permanent customer ordering link created")
    return jsonify({"url": f"{request.host_url}?token={token}"})

@app.route('/api/backup', methods=['GET'])
def backup_data():
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
            "reservations": [{"code": r.reservation_code, "name": r.patron_name, "email": r.patron_email, "total": r.total_investment, "status": r.status, "pickup_time": r.pickup_time, "source": r.order_source, "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S'), "items": [{"foundation": i.foundation, "sweetener": i.sweetener, "ice_level": i.ice_level, "pearls": i.pearls, "cup_size": i.cup_size, "addons": i.addons, "item_total": i.item_total} for i in r.infusions]} for r in reservations],
            "expenses": [{"description": x.description, "amount": x.amount, "created_at": x.created_at.strftime('%Y-%m-%d %H:%M:%S')} for x in expenses],
            "menu_items": [{"name": m.name, "price": m.price, "letter": m.letter, "category": m.category, "is_out_of_stock": m.is_out_of_stock} for m in menu_items],
            "ingredients": [{"name": i.name, "unit": i.unit, "stock": i.stock} for i in ingredients],
            "customer_logs": [{"name": c.full_name, "gmail": c.gmail, "phone": c.phone, "source": c.order_source, "total": c.order_total, "created_at": c.created_at.strftime('%Y-%m-%d %H:%M:%S')} for c in customers],
            "audit_logs": [{"action": a.action, "details": a.details, "created_at": a.created_at.strftime('%Y-%m-%d %H:%M:%S')} for a in audit],
        }
        log_audit("Backup Downloaded", f"Full backup at {payload['exported_at']}")
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/restore', methods=['POST'])
def restore_data():
    data = request.json
    if not data or data.get('backup_version') != '1.0':
        return jsonify({"error": "Invalid backup file"}), 400
    try:
        for x in data.get('expenses', []):
            if not Expense.query.filter_by(description=x['description'], amount=x['amount']).first():
                db.session.add(Expense(description=x['description'], amount=x['amount']))
        for m in data.get('menu_items', []):
            if not MenuItem.query.filter_by(name=m['name']).first():
                db.session.add(MenuItem(name=m['name'], price=m['price'], letter=m['letter'], category=m['category'], is_out_of_stock=m.get('is_out_of_stock', False)))
        for i in data.get('ingredients', []):
            existing = Ingredient.query.filter_by(name=i['name']).first()
            if existing:
                existing.stock = i['stock']
            else:
                db.session.add(Ingredient(name=i['name'], unit=i['unit'], stock=i['stock']))
        for c in data.get('customer_logs', []):
            db.session.add(CustomerLog(full_name=c['name'], gmail=c['gmail'], phone=c['phone'], order_source=c['source'], order_total=c['total']))
        db.session.commit()
        log_audit("Backup Restored", f"Data restored from backup v{data.get('backup_version')}")
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# ==========================================
# 7. DB INITIALIZATION
# ==========================================

with app.app_context():
    try:
        db.create_all()
        print("Admin Panel DB initialized.")
    except Exception as e:
        print(f"DB Init Error: {e}")

# ==========================================
# 8. RUNNER (Port 5001 — separate from employee site)
# ==========================================

if __name__ == '__main__':
    if os.environ.get('RENDER') or os.environ.get('DYNO'):
        port = int(os.environ.get('PORT', 5001))
        app.run(host='0.0.0.0', port=port)
    else:
        import webbrowser
        from threading import Timer

        def open_browser():
            webbrowser.open('http://127.0.0.1:5001/login')

        Timer(1.0, open_browser).start()
        print("\n========================================")
        print("  9599 Tea & Coffee — ADMIN PANEL")
        print("  http://127.0.0.1:5001/login")
        print("========================================\n")
        app.run(host='0.0.0.0', port=5001, debug=False)
