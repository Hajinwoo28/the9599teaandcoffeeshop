import os
import uuid
import socket
import threading
import json
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)

# --- Security Configuration ---
app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-9599-key')
ADMIN_MASTER_PIN = os.environ.get('ADMIN_PIN', '12345')

# --- Cloud & Local Configuration ---
database_url = os.environ.get('DATABASE_URL', 'postgresql://postgres:12345@localhost/milktea_system')
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

# --- Database Models ---
class Reservation(db.Model):
    __tablename__ = 'reservations'
    id = db.Column(db.Integer, primary_key=True)
    reservation_code = db.Column(db.String(8), unique=True, nullable=False, default=lambda: str(uuid.uuid4())[:8].upper())
    patron_name = db.Column(db.String(100), nullable=False)
    patron_email = db.Column(db.String(120), nullable=False)
    total_investment = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Preparing Order')
    pickup_time = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    infusions = db.relationship('Infusion', backref='reservation', lazy=True)

class Infusion(db.Model):
    __tablename__ = 'infusions'
    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservations.id'), nullable=False)
    foundation = db.Column(db.String(100), nullable=False)
    sweetener = db.Column(db.String(100), nullable=False)
    pearls = db.Column(db.String(100), nullable=False)
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

class RecipeItem(db.Model):
    __tablename__ = 'recipe_items'
    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id', ondelete='CASCADE'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredients.id', ondelete='CASCADE'), nullable=False)
    quantity_required = db.Column(db.Float, nullable=False)

    ingredient = db.relationship('Ingredient')
    menu_item = db.relationship('MenuItem', backref=db.backref('recipe', cascade="all, delete-orphan"))

# --- Admin Login Template ---
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#0F172A">
    <title>Admin Login | 9599 Tea & Coffee</title>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
        body { background-color: #F4F7F6; display: flex; justify-content: center; align-items: center; height: 100vh; padding: 20px; }
        .login-box { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); text-align: center; width: 100%; max-width: 400px; border: 1px solid #E2E8F0; }
        .login-box h2 { color: #0F172A; margin-bottom: 10px; font-weight: 800; }
        .login-box p { color: #64748B; font-size: 0.9rem; margin-bottom: 25px; }
        .input-pin { width: 100%; padding: 15px; border: 2px solid #CBD5E1; border-radius: 8px; font-size: 1.5rem; text-align: center; letter-spacing: 8px; margin-bottom: 20px; outline: none; font-weight: 800; color: #0F172A; transition: border-color 0.2s; }
        .input-pin:focus { border-color: #2563EB; }
        .btn-login { width: 100%; background: #0F172A; color: white; border: none; padding: 15px; border-radius: 8px; font-weight: 700; font-size: 1rem; cursor: pointer; transition: 0.2s; display: flex; justify-content: center; align-items: center; gap: 10px; }
        .btn-login:hover { background: #1E293B; }
        .error { background: #FEF2F2; color: #DC2626; padding: 10px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; margin-bottom: 20px; border: 1px solid #FECACA; }
        @media (max-width: 480px) {
            .login-box { padding: 25px; max-width: 100%; }
        }
    </style>
</head>
<body>
    <div class="login-box">
        <i class="fas fa-lock" style="font-size: 3rem; color: #94A3B8; margin-bottom: 15px;"></i>
        <h2>Admin Access</h2>
        <p>Please enter the master PIN to continue.</p>
        
        {% if error %}
        <div class="error"><i class="fas fa-exclamation-circle"></i> {{ error }}</div>
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
    <title>Order Here | 9599 Tea & Coffee Shop</title>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
        body { background-color: #F5F6F8; color: #1e293b; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
        
        header { background: white; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; position: relative; }
        .logo-area { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 1.1rem; }
        .logo-img { width: 40px; height: 40px; border-radius: 50%; background: #e2e8f0; object-fit: cover; border: 2px solid #f59e0b; }
        
        .main-container { display: flex; flex: 1; overflow: hidden; flex-direction: row; }
        .menu-area { flex: 1; padding: 15px; overflow-y: auto; }
        
        .categories { display: flex; gap: 10px; overflow-x: auto; margin-bottom: 20px; padding-bottom: 5px; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
        .categories::-webkit-scrollbar { display: none; }
        .cat-btn { padding: 8px 16px; border-radius: 20px; border: 1px solid #e2e8f0; background: white; color: #475569; font-weight: 600; cursor: pointer; white-space: nowrap; font-size: 0.85rem; transition: all 0.2s; }
        .cat-btn.active { background: #1e293b; color: white; border-color: #1e293b; }

        .menu-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 15px; }
        .card { background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.02); cursor: pointer; transition: transform 0.2s; border: 1px solid #f1f5f9; position: relative; }
        .card:active { transform: scale(0.98); }
        .card-img-container { height: 120px; background: linear-gradient(135deg, #e2e8f0 0%, #94a3b8 100%); position: relative; display: flex; justify-content: center; align-items: center; }
        .card-img-container span { font-size: 50px; font-weight: 800; color: rgba(255, 255, 255, 0.3); }
        .card-price { position: absolute; bottom: 8px; right: 8px; background-color: #B25022; color: white; padding: 4px 8px; border-radius: 6px; font-weight: 700; font-size: 0.8rem; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
        .card-title { padding: 12px; font-weight: 800; color: #0f172a; font-size: 0.9rem; line-height: 1.2; }
        .empty-category { grid-column: 1 / -1; text-align: center; color: #94a3b8; padding: 40px; font-weight: 600; }

        .card.sold-out { opacity: 0.5; cursor: not-allowed; }
        .card.sold-out:active { transform: none; }
        .sold-out-badge { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(255,255,255,0.7); display: flex; justify-content: center; align-items: center; font-weight: 900; color: #DC2626; font-size: 1.2rem; z-index: 10; letter-spacing: 1px; text-transform: uppercase; }
        .low-stock-badge { position: absolute; top: 8px; left: 8px; background: #F59E0B; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.65rem; font-weight: 800; z-index: 5; text-transform: uppercase; }

        .sidebar { width: 350px; background: white; border-left: 1px solid #e2e8f0; display: flex; flex-direction: column; z-index: 50; }
        
        .cart-content { padding: 20px; flex: 1; display: flex; flex-direction: column; overflow-y: auto; }
        .cart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .cart-title { font-size: 1.1rem; font-weight: 800; color: #1e293b; display: flex; align-items: center; gap: 10px; }
        
        .order-type { display: flex; background: #f1f5f9; border-radius: 8px; padding: 4px; gap: 4px; }
        .type-btn { flex: 1; padding: 8px 12px; text-align: center; font-weight: 600; font-size: 0.8rem; border-radius: 6px; cursor: pointer; color: #64748b; transition: all 0.2s; }
        .type-btn.active { background: white; color: #0f172a; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }

        .name-input { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 0.9rem; font-weight: 600; outline: none; margin-bottom: 15px; }
        .name-input:focus { border-color: #f59e0b; }
        .pickup-label { font-size: 0.75rem; font-weight: 700; color: #64748B; margin-bottom: 5px; display: block; text-transform: uppercase; }

        .empty-cart { margin: auto 0; text-align: center; color: #94a3b8; }
        .empty-cart i { font-size: 3rem; color: #e2e8f0; margin-bottom: 10px; }
        .empty-cart p { font-weight: 800; font-size: 1rem; letter-spacing: 1px; }

        .cart-items-list { flex: 1; overflow-y: auto; display: none; margin-bottom: 15px; }
        .cart-item { display: flex; justify-content: space-between; margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid #f1f5f9; }
        .item-details h4 { font-size: 0.9rem; font-weight: 700; color: #1e293b; }
        .item-details p { font-size: 0.75rem; color: #ef4444; cursor: pointer; margin-top: 5px; font-weight: 600; }
        .item-price { font-weight: 800; color: #B25022; font-size: 0.9rem; }

        .checkout-area { padding: 20px; border-top: 1px solid #e2e8f0; background: white; }
        .total-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .total-label { font-size: 1.2rem; font-weight: 800; color: #0f172a; }
        .total-amount { font-size: 1.5rem; font-weight: 800; color: #854d0e; }
        
        .checkout-btn { width: 100%; padding: 15px; border: none; border-radius: 8px; font-size: 0.95rem; font-weight: 800; display: flex; justify-content: center; align-items: center; gap: 10px; color: white; background: #94a3b8; cursor: not-allowed; transition: all 0.2s; }
        .checkout-btn.active { background: #854d0e; cursor: pointer; }

        .modal { display: none; position: fixed; z-index: 100; left: 0; top: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.6); align-items: center; justify-content: center; }
        .modal-content { background: white; padding: 30px; border-radius: 12px; text-align: center; max-width: 90%; width: 350px; }
        .modal-content h2 { font-weight: 800; margin-bottom: 10px; color: #0f172a; }
        .order-number { font-size: 2rem; font-weight: 800; color: #B25022; margin: 20px 0; letter-spacing: 2px; }
        .modal-btn { background: #1e293b; color: white; padding: 12px 25px; border: none; border-radius: 6px; font-weight: 700; cursor: pointer; width: 100%; }

        .notif-container { position: relative; margin-right: 10px; }
        .notif-bell { cursor: pointer; position: relative; padding: 5px; }
        .notif-badge { position: absolute; top: -2px; right: -2px; background: #ef4444; color: white; border-radius: 50%; padding: 2px 6px; font-size: 0.65rem; font-weight: 800; display: none; border: 2px solid white; }
        .notif-dropdown { display: none; position: absolute; top: 45px; right: -10px; background: white; border: 1px solid #e2e8f0; border-radius: 10px; width: 300px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); z-index: 1000; flex-direction: column; overflow: hidden; }
        .notif-header { padding: 15px; border-bottom: 1px solid #e2e8f0; font-weight: 800; font-size: 0.95rem; color: #0f172a; display: flex; justify-content: space-between; align-items: center; background: #F8FAFC; }
        .notif-clear { font-size: 0.75rem; color: #64748B; cursor: pointer; font-weight: 600; text-transform: uppercase; }
        .notif-clear:hover { color: #ef4444; }
        .notif-list { max-height: 300px; overflow-y: auto; display: flex; flex-direction: column; }
        .notif-item { padding: 12px 15px; border-bottom: 1px solid #f1f5f9; font-size: 0.85rem; color: #1e293b; font-weight: 600; line-height: 1.4; display: flex; align-items: flex-start; gap: 10px; }
        .notif-item i { color: #f59e0b; margin-top: 3px; }
        .notif-item.ready i { color: #10B981; }
        .notif-item.completed i { color: #3B82F6; }
        .notif-empty { padding: 20px; text-align: center; color: #94a3b8; font-size: 0.85rem; font-weight: 600; }

        /* ANDROID/MOBILE RESPONSIVE FIXES */
        @media (max-width: 768px) { 
            header { flex-direction: row; padding: 15px; align-items: center; }
            .logo-area { font-size: 1rem; }
            .main-container { flex-direction: column; } 
            .menu-area { flex: 1; height: 55vh; padding: 10px; }
            .sidebar { width: 100%; height: 45vh; border-left: none; border-top: 1px solid #e2e8f0; box-shadow: 0 -4px 15px rgba(0,0,0,0.05); } 
            .menu-grid { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }
            .card-img-container { height: 100px; }
            .notif-dropdown { position: fixed; top: 60px; right: 10px; width: calc(100% - 20px); max-width: 350px; }
        }
    </style>
</head>
<body>

    <header>
        <div class="logo-area">
            <img src="/static/images/9599.jpg" alt="Logo" class="logo-img" onerror="this.style.display='none'">
            <div style="display:flex; flex-direction: column;">
                <span>9599 Tea & Coffee</span>
                <span id="store-hours-display" style="font-size: 0.7rem; font-weight: 600; color: #64748b; display: none;"></span>
            </div>
        </div>

        <div class="notif-container">
            <div class="notif-bell" onclick="toggleNotif()">
                <i class="fas fa-bell" style="font-size: 1.4rem; color: #475569;"></i>
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
        <div class="menu-area">
            <div class="categories" id="categories-container">
                <button class="cat-btn active" onclick="filterMenu('All', this)">All</button>
            </div>
            <div class="menu-grid" id="menu-grid">
                <div class="empty-category">Loading Menu...</div>
            </div>
        </div>

        <div class="sidebar">
            <div class="cart-content">
                <div class="cart-header">
                    <div class="cart-title"><i class="fas fa-shopping-cart" style="color:#f59e0b;"></i> Your Cart</div>
                    <div class="order-type">
                        <div class="type-btn active" id="btn-dine-in" onclick="setOrderType('Dine-In')">Dine-In</div>
                        <div class="type-btn" id="btn-take-out" onclick="setOrderType('Take-Out')">Take-Out</div>
                    </div>
                </div>

                <input type="text" class="name-input" id="customer-name" placeholder="Enter Your Name" oninput="checkCheckoutStatus()">
                
                <label class="pickup-label" for="pickup-time">Expected Pick-up Time (Required) <span style="color:#ef4444;">*</span></label>
                <input type="time" class="name-input" id="pickup-time" oninput="checkCheckoutStatus()" required>

                <div class="empty-cart" id="empty-cart">
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
            <p>Please wait for your name to be called.</p>
            <div class="order-number" id="display-code">XXXXXX</div>
            <button class="modal-btn" onclick="closeModal()">Done</button>
        </div>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", () => {
            const params = new URLSearchParams(window.location.search);
            // Time Expiration Check dynamically on frontend
            if (params.has('exp')) {
                const expTime = parseInt(params.get('exp'));
                if (Date.now() > expTime) {
                    document.body.innerHTML = `
                        <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5F6F8; flex-direction:column; text-align:center; padding: 20px;">
                            <i class="fas fa-lock" style="font-size:4rem; color:#94a3b8; margin-bottom:20px;"></i>
                            <h2 style="color:#0f172a; font-family:'Poppins', sans-serif;">Ordering Link Expired</h2>
                            <p style="color:#64748b; font-family:'Poppins', sans-serif;">Please ask the staff for a new ordering link.</p>
                        </div>`;
                    return; 
                }
            }
            
            const hoursDisplay = document.getElementById('store-hours-display');
            if (params.has('open') && params.has('close') && hoursDisplay) {
                hoursDisplay.innerText = `(${params.get('open')} - ${params.get('close')})`;
                hoursDisplay.style.display = 'block';
            }
            
            fetchMenu();
            updateNotifUI(); 
        });

        // Notifications
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
                    if (n.status === 'Ready for Pick-up') { iconClass = 'fa-check-circle'; itemClass = 'ready'; }
                    if (n.status === 'Completed') { iconClass = 'fa-flag-checkered'; itemClass = 'completed'; }
                    
                    return `<div class="notif-item ${itemClass}">
                                <i class="fas ${iconClass}"></i> 
                                <div>Order <strong>#${n.code}</strong> is now:<br><span style="color:#64748b;">${n.status}</span></div>
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

        async function pollCustomerOrderStatus() {
            let myOrders = JSON.parse(localStorage.getItem('myOrders')) ||[];
            if (myOrders.length === 0) return;

            let codes = myOrders.map(o => o.code).join(',');
            try {
                const res = await fetch(`/api/customer/status?codes=${codes}`);
                if (!res.ok) return;
                const data = await res.json();
                
                let updated = false;
                data.forEach(serverOrder => {
                    let localOrder = myOrders.find(o => o.code === serverOrder.code);
                    if (localOrder && localOrder.status !== serverOrder.status) {
                        localOrder.status = serverOrder.status;
                        updated = true;
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
            } catch (e) {
                console.error("Polling error", e);
            }
        }
        
        setInterval(pollCustomerOrderStatus, 3000);

        // Storefront Logic
        let menuItems =[];
        let cart =[];
        let orderType = 'Dine-In';

        async function fetchMenu() {
            try {
                const response = await fetch('/api/menu');
                menuItems = await response.json();
                renderCategories();
                renderMenu('All');
            } catch(e) {
                console.error("Error loading menu:", e);
                document.getElementById('menu-grid').innerHTML = '<div class="empty-category">Failed to load menu.</div>';
            }
        }

        function renderCategories() {
            const categories =[...new Set(menuItems.map(item => item.category))];
            const catContainer = document.getElementById('categories-container');
            catContainer.innerHTML = `<button class="cat-btn active" onclick="filterMenu('All', this)">All</button>`;
            categories.forEach(cat => {
                catContainer.innerHTML += `<button class="cat-btn" onclick="filterMenu('${cat}', this)">${cat}</button>`;
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
                            <span>${item.letter}</span>
                            <div class="card-price">₱${item.price.toFixed(2)}</div>
                        </div>
                        <div class="card-title">${item.name}</div>
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
                cart.push({ name: name, price: price });
                updateCartUI();
            } else {
                alert("Sorry, this item is out of stock!");
            }
        }

        function updateCartUI() {
            const emptyCart = document.getElementById('empty-cart');
            const cartItemsList = document.getElementById('cart-items');
            const totalEl = document.getElementById('cart-total');
            
            if (cart.length > 0) {
                emptyCart.style.display = 'none';
                cartItemsList.style.display = 'block';
                cartItemsList.innerHTML = '';
                let total = 0;
                
                cart.forEach((item, index) => {
                    total += item.price;
                    cartItemsList.innerHTML += `
                        <div class="cart-item">
                            <div class="item-details">
                                <h4>${item.name}</h4>
                                <p onclick="removeFromCart(${index})"><i class="fas fa-trash"></i> Remove</p>
                            </div>
                            <div class="item-price">₱${item.price.toFixed(2)}</div>
                        </div>
                    `;
                });
                totalEl.innerText = `₱${total.toFixed(2)}`;
            } else {
                emptyCart.style.display = 'block';
                cartItemsList.style.display = 'none';
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
            
            if (cart.length === 0 || name === '' || pickupTime === '') return;

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
                email: "mobile@9599.local", 
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
                    myOrders.push({ code: result.reservation_code, status: 'Preparing Order' });
                    if (myOrders.length > 5) myOrders = myOrders.slice(-5);
                    localStorage.setItem('myOrders', JSON.stringify(myOrders));

                    cart =[];
                    document.getElementById('customer-name').value = '';
                    document.getElementById('pickup-time').value = ''; 
                    updateCartUI(); 
                    fetchMenu(); 
                } else {
                    alert("Order Error: " + result.message);
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
</body>
</html>
"""

# --- Admin Dashboard Template (NEW UI - Connected Live) ---
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    
    <!-- Meta Tags for Mobile/Tablet App Installation (PWA) -->
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#0F172A">
    <link rel="apple-touch-icon" href="/static/images/9599.jpg">
    
    <title>9599 Admin POS</title>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Poppins', sans-serif; }
        body { background-color: #F4F7F6; color: #1E293B; display: flex; height: 100vh; overflow: hidden; }
        
        .sidebar { width: 250px; background: white; border-right: 1px solid #E2E8F0; display: flex; flex-direction: column; justify-content: space-between; flex-shrink: 0; z-index: 100; }
        .sidebar-header { padding: 30px 25px; font-weight: 800; font-size: 1.1rem; color: #0F172A; }
        .nav-links { flex: 1; display: flex; flex-direction: column; }
        .nav-item { padding: 15px 25px; color: #64748B; text-decoration: none; font-weight: 600; font-size: 0.95rem; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: all 0.2s; border-left: 4px solid transparent; }
        .nav-item:hover { background: #F8FAFC; color: #1E293B; }
        .nav-item.active { background: #F8FAFC; color: #0F172A; border-left: 4px solid #0F172A; font-weight: 700; }
        .nav-item i { font-size: 1.1rem; width: 20px; text-align: center; }
        
        .sidebar-footer { padding: 25px; border-top: 1px solid #E2E8F0; display: flex; flex-direction: column; gap: 10px; }
        .btn-reload { width: 100%; background: #F1F5F9; color: #1E293B; border: none; padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; transition: background 0.2s; }
        .btn-reload:hover { background: #E2E8F0; }
        .btn-logout { width: 100%; background: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; transition: background 0.2s; display: flex; justify-content: center; align-items: center; gap: 8px; }
        .btn-logout:hover { background: #FEE2E2; }

        .main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        .topbar { background: white; padding: 20px 40px; border-bottom: 1px solid #E2E8F0; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; z-index: 90; }
        .page-title { font-size: 1.4rem; font-weight: 800; color: #0F172A; }
        .topbar-actions { display: flex; align-items: center; gap: 20px; }
        .btn-action { background: white; border: 1px solid #E2E8F0; padding: 8px 16px; border-radius: 20px; color: #475569; font-weight: 600; font-size: 0.85rem; display: flex; align-items: center; gap: 8px; cursor: pointer; text-decoration: none; }
        .btn-action:hover { background: #F8FAFC; }
        .icon-bell { color: #64748B; font-size: 1.2rem; cursor: pointer; }
        .time-badge { background: #F1F5F9; color: #0F172A; font-weight: 800; padding: 8px 16px; border-radius: 20px; font-size: 0.9rem; letter-spacing: 0.5px; }

        /* Unified Layout Base */
        .content-body { padding: 30px; overflow: hidden; flex: 1; display: flex; flex-direction: column; }
        .tab-pane { display: none; height: 100%; flex-direction: column; min-height: 0; }
        .tab-pane.active { display: flex; animation: fadeIn 0.3s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

        /* Custom Grid for Settings Tab (Adjusted left column to be slightly smaller) */
        .settings-grid-layout { display: grid; grid-template-columns: 360px 1fr; gap: 25px; height: 100%; min-height: 0; }
        
        .settings-card { background: white; border-radius: 12px; padding: 25px; border: 1px solid #E2E8F0; box-shadow: 0 4px 6px rgba(0,0,0,0.02); display: flex; flex-direction: column; min-height: 0; }
        .card-link { border-left: 6px solid #1E293B; }
        
        .card-title { font-size: 1rem; font-weight: 800; color: #0F172A; text-transform: uppercase; margin-bottom: 15px; letter-spacing: 0.5px; flex-shrink: 0; }
        .desc-text { color: #64748B; font-size: 0.85rem; line-height: 1.5; margin-bottom: 20px; flex-shrink: 0; }
        
        /* Table Internal Scrolling */
        .table-responsive { flex: 1; overflow-x: auto; overflow-y: auto; border: 1px solid #E2E8F0; border-radius: 8px; -webkit-overflow-scrolling: touch; }

        .config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px; flex-shrink: 0; }
        .input-group label { display: block; font-size: 0.75rem; font-weight: 800; color: #64748B; margin-bottom: 8px; text-transform: uppercase; }
        .input-with-icon { position: relative; }
        .input-with-icon input { width: 100%; padding: 12px 15px; border: 1px solid #CBD5E1; border-radius: 8px; font-weight: 600; color: #0F172A; outline: none; }
        .input-pin { width: 100%; padding: 12px 15px; border: 1px solid #E2E8F0; border-radius: 8px; background: #EFF6FF; color: #0F172A; font-weight: 800; font-size: 1.2rem; letter-spacing: 2px; outline: none; margin-bottom: 20px; text-align: center; flex-shrink: 0; }

        .btn-blue { width: 100%; background: #2563EB; color: white; border: none; padding: 16px; border-radius: 8px; font-weight: 700; font-size: 0.95rem; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 10px; transition: background 0.2s; margin-bottom: 20px; flex-shrink: 0; }
        .btn-blue:hover { background: #1D4ED8; }
        
        .btn-reset { width: 100%; background: #FEF2F2; color: #0F172A; border: 1px solid #E2E8F0; padding: 12px; border-radius: 8px; font-weight: 700; font-size: 0.9rem; cursor: pointer; display: flex; justify-content: center; align-items: center; gap: 8px; transition: background 0.2s; }
        .btn-reset:hover { background: #E2E8F0; }

        .link-row { display: flex; gap: 10px; margin-bottom: 10px; flex-shrink: 0; }
        .link-input { flex: 1; background: #F1F5F9; border: 1px solid #E2E8F0; padding: 12px 15px; border-radius: 8px; color: #94A3B8; font-family: monospace; font-size: 0.85rem; outline: none; text-align: center; }
        .btn-dark { background: #0F172A; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 700; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: background 0.2s; }
        .btn-dark:hover { background: #1E293B; }

        .status-text { text-align: center; font-size: 0.8rem; font-weight: 600; font-family: monospace; flex-shrink: 0; }
        .status-muted { color: #94A3B8; }

        /* Action Buttons specific styling for Menu Management */
        .action-btn { background: #F1F5F9; border: none; padding: 8px 12px; border-radius: 6px; font-size: 1rem; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; justify-content: center; }
        .action-btn-edit { color: #2563EB; margin-right: 8px; }
        .action-btn-edit:hover { background: #DBEAFE; }
        .action-btn-delete { color: #DC2626; }
        .action-btn-delete:hover { background: #FEE2E2; }

        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.6); align-items: center; justify-content: center; }
        .modal-content { background: white; padding: 30px; border-radius: 12px; width: 400px; max-width: 95%; margin: 10px; }
        .modal-title { font-weight: 800; font-size: 1.2rem; margin-bottom: 20px; color: #0F172A; }
        .form-control { width: 100%; padding: 10px 15px; border: 1px solid #CBD5E1; border-radius: 6px; font-family: 'Poppins', sans-serif; font-size: 0.9rem; }
        .btn-group { display: flex; gap: 10px; margin-top: 25px; }
        .btn-save { flex: 1; background: #10B981; color: white; border: none; padding: 12px; border-radius: 6px; font-weight: 700; cursor: pointer; }
        .btn-cancel { flex: 1; background: #E2E8F0; color: #475569; border: none; padding: 12px; border-radius: 6px; font-weight: 700; cursor: pointer; }

        .kds-table { width: 100%; border-collapse: collapse; background: white; }
        .kds-table th, .kds-table td { padding: 15px 20px; text-align: left; border-bottom: 1px solid #E2E8F0; white-space: nowrap; }
        .kds-table th { background: #F8FAFC; color: #64748B; font-weight: 700; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; position: sticky; top: 0; z-index: 10; border-bottom: 2px solid #E2E8F0; }
        .kds-code { font-family: monospace; font-weight: 700; color: #475569; font-size: 1.1rem; }
        
        .status-select { font-family: 'Poppins', sans-serif; font-weight: 700; font-size: 0.8rem; padding: 8px 12px; border-radius: 20px; border: 1px solid #E2E8F0; outline: none; cursor: pointer; text-transform: uppercase; width: 100%; text-align: center; }
        .status-preparing { background: #FEF3C7; color: #D97706; border-color: #FDE68A; }
        .status-ready { background: #D1FAE5; color: #059669; border-color: #A7F3D0; }
        .status-completed { background: #F1F5F9; color: #64748B; border-color: #E2E8F0; }

        .infusion-list { list-style: none; padding: 0; margin: 0; font-size: 0.9rem; color: #1E293B; }
        .infusion-list li { margin-bottom: 5px; }
        .kds-badge { background: #E2E8F0; color: #475569; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; margin-left: 8px; font-weight: 600; }
        
        .live-indicator { display: inline-block; width: 10px; height: 10px; background: #10B981; border-radius: 50%; margin-right: 8px; animation: pulse 2s infinite; }
        @keyframes pulse { 0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); } 70% { transform: scale(1); box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); } 100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); } }

        /* Responsive Layout Handling */
        @media (max-width: 1024px) {
            .settings-grid-layout { grid-template-columns: 1fr; height: auto; }
            .content-body { overflow-y: auto; }
            .tab-pane { height: auto; }
        }
        @media (max-width: 768px) {
            body { flex-direction: column; }
            .sidebar { width: 100%; flex-direction: column; border-right: none; border-bottom: 1px solid #E2E8F0; }
            .sidebar-header { padding: 15px; text-align: center; }
            .nav-links { flex-direction: row; overflow-x: auto; padding: 0 10px; }
            .nav-item { flex: 1; flex-direction: column; padding: 10px; font-size: 0.75rem; text-align: center; border-left: none !important; border-bottom: 4px solid transparent; white-space: nowrap; }
            .nav-item i { margin-bottom: 5px; }
            .nav-item.active { border-bottom: 4px solid #0F172A; background: transparent; }
            .sidebar-footer { flex-direction: row; padding: 10px 15px; }
            .btn-reload, .btn-logout { padding: 10px; font-size: 0.8rem; }
            
            .topbar { padding: 15px; flex-direction: column; align-items: flex-start; gap: 15px; }
            .topbar-actions { width: 100%; justify-content: flex-end; }
            
            .content-body { padding: 15px; overflow-y: auto; }
            .settings-grid-layout { grid-template-columns: 1fr; gap: 15px; height: auto; }
            .settings-card { padding: 20px; }
            .config-grid { grid-template-columns: 1fr; gap: 10px; margin-bottom: 10px; }
            
            .table-responsive { border: none; overflow-x: auto; overflow-y: hidden; }
            .kds-table th, .kds-table td { padding: 12px 15px; }
            .tab-pane { height: auto; }
        }
    </style>
</head>
<body>

    <!-- Sidebar (Top Tab Bar on Mobile) -->
    <aside class="sidebar">
        <div>
            <div class="sidebar-header">Admin Mode</div>
            <nav class="nav-links">
                <div class="nav-item" onclick="switchTab('kds', 'Orders', this)">
                    <i class="fas fa-clipboard-list"></i> Orders
                </div>
                <div class="nav-item" onclick="switchTab('inventory', 'Inventory (Ingredients)', this)">
                    <i class="fas fa-boxes"></i> Inventory
                </div>
                <div class="nav-item active" onclick="switchTab('settings', 'Settings & Menu', this)">
                    <i class="fas fa-sliders-h"></i> Settings & Menu
                </div>
            </nav>
        </div>
        <div class="sidebar-footer">
            <button class="btn-logout" onclick="location.href='/logout'"><i class="fas fa-sign-out-alt"></i> Lock / Logout</button>
            <button class="btn-reload" onclick="location.reload()">Reload System</button>
        </div>
    </aside>

    <!-- Main Application Area -->
    <main class="main-content">
        <!-- Header -->
        <header class="topbar">
            <div class="page-title" id="page-title">Settings & Menu</div>
            <div class="topbar-actions">
                <i class="fas fa-bell icon-bell"></i>
                <a href="/" target="_blank" class="btn-action"><i class="fas fa-external-link-square-alt"></i> Customer Site</a>
                <div class="time-badge" id="clock">00:00 PM</div>
            </div>
        </header>

        <!-- Content Body -->
        <div class="content-body">
            
            <!-- TAB 1: Settings & Menu (Side-by-Side Layout) -->
            <div id="tab-settings" class="tab-pane active">
                <div class="settings-grid-layout">
                    
                    <!-- LEFT COLUMN: Configuration & Backup -->
                    <div style="display: flex; flex-direction: column; gap: 25px; min-height: 0; overflow-y: auto; padding-right: 5px;">
                        
                        <!-- Store Configuration Card -->
                        <div class="settings-card card-link" style="flex-shrink: 0;">
                            <div class="card-title">Store Configuration & Link</div>
                            <p class="desc-text">Configure your store's hours and PIN to enable ordering. Customers cannot order until you generate a valid link.</p>
                            
                            <div class="config-grid">
                                <div class="input-group">
                                    <label>Opening Time</label>
                                    <div class="input-with-icon">
                                        <input type="time" id="store-open">
                                    </div>
                                </div>
                                <div class="input-group">
                                    <label>Closing Time</label>
                                    <div class="input-with-icon">
                                        <input type="time" id="store-close">
                                    </div>
                                </div>
                            </div>

                            <div class="input-group">
                                <label>Store Access PIN (Used by Cashier)</label>
                                <input type="password" id="store-pin" class="input-pin" placeholder="Enter PIN">
                            </div>

                            <button class="btn-blue" onclick="saveConfigurations()">
                                <i class="fas fa-bolt"></i> Validate & Generate Link
                            </button>

                            <div class="link-row">
                                <input type="text" class="link-input" id="posLink" value="Pending Configuration..." readonly style="color: #ef4444; font-weight: bold;">
                                <button class="btn-dark" onclick="copyLink()"><i class="far fa-copy"></i> Copy</button>
                            </div>

                            <div class="status-text">
                                <span class="status-muted" id="expiration-text">• Expires: Pending Generation</span>
                            </div>
                        </div>

                        <!-- Backup & Recovery Card -->
                        <div class="settings-card" style="flex-shrink: 0; border-left: 6px solid #10B981;">
                            <div class="card-title"><i class="fas fa-database"></i> Backup & Recovery</div>
                            <p class="desc-text">Download a complete backup of your system. You can use this file to safely restore the system later.</p>
                            
                            <button class="btn-blue" onclick="downloadBackup()" style="margin-bottom: 15px; background: #10B981;">
                                <i class="fas fa-download"></i> Download System Backup
                            </button>
                            
                            <div style="border-top: 1px solid #E2E8F0; margin: 15px 0;"></div>
                            
                            <p class="desc-text" style="margin-bottom: 10px; font-weight:600;">Restore from Backup</p>
                            <input type="file" id="backup-file" accept=".json" style="width:100%; padding: 10px; border: 1px dashed #CBD5E1; border-radius: 8px; margin-bottom: 15px; font-size: 0.85rem;">
                            
                            <button class="btn-reset" onclick="restoreBackup()">
                                <i class="fas fa-upload"></i> Upload & Restore System
                            </button>
                        </div>
                    </div>

                    <!-- RIGHT COLUMN: Menu Management Card -->
                    <div class="settings-card">
                        <div style="display:flex; justify-content:space-between; align-items:center; flex-shrink: 0; margin-bottom: 10px;">
                            <div class="card-title" style="margin-bottom:0;">Menu Management</div>
                            <button class="btn-dark" style="padding: 8px 15px; font-size: 0.8rem;" onclick="openMenuModal()">
                                <i class="fas fa-plus"></i> Add Item
                            </button>
                        </div>
                        
                        <div class="table-responsive">
                            <table class="kds-table" style="font-size: 0.85rem;">
                                <thead>
                                    <tr>
                                        <th>Item Name</th>
                                        <th>Category</th>
                                        <th>Price</th>
                                        <th style="text-align: right; width: 120px;">Actions</th>
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

            <!-- TAB 2: Orders System -->
            <div id="tab-kds" class="tab-pane">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 15px; flex-shrink: 0;">
                    <h2 style="font-size:1.2rem; color:#0F172A; margin:0;"><span class="live-indicator"></span> Live Active Orders</h2>
                    <h3 style="color:#D97706; margin:0;" id="total-revenue-display">Total Sales: ₱0.00</h3>
                </div>
                <div class="table-responsive" style="margin-top: 0;">
                    <table class="kds-table">
                        <thead>
                            <tr>
                                <th>Order #</th>
                                <th>Customer Name</th>
                                <th>Total</th>
                                <th>Items Ordered</th>
                                <th>Order Placed</th>
                                <th>Pick-up Time</th> 
                                <th>Status Manager</th>
                            </tr>
                        </thead>
                        <tbody id="kds-table-body">
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- TAB 3: Inventory UI (Raw Ingredients) -->
            <div id="tab-inventory" class="tab-pane">
                <div class="settings-card" style="height: 100%;">
                    <div class="card-title">Raw Ingredient Inventory</div>
                    <p class="desc-text">Update your raw materials here. The system uses these to calculate if a finished drink can be made. If a required ingredient hits 0, the drinks that rely on it will become "Sold Out".</p>
                    
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

        </div>
    </main>

    <!-- Modal for Menu Add/Edit -->
    <div id="menu-modal" class="modal">
        <div class="modal-content">
            <div class="modal-title" id="menu-modal-title">Add Menu Item</div>
            <form id="menu-form" onsubmit="saveMenuItem(event)">
                <div class="input-group" style="margin-bottom: 15px;">
                    <label>Item Name</label>
                    <input type="text" id="menu-name" class="form-control" required>
                </div>
                <div class="input-group" style="margin-bottom: 15px;">
                    <label>Price (₱)</label>
                    <input type="number" step="0.01" id="menu-price" class="form-control" required>
                </div>
                <div class="input-group" style="margin-bottom: 15px;">
                    <label>Category</label>
                    <input type="text" id="menu-category" class="form-control" placeholder="e.g., Signature Series" required>
                </div>
                <div class="input-group" style="margin-bottom: 15px;">
                    <label>Display Letter (1-2 Chars)</label>
                    <input type="text" id="menu-letter" class="form-control" maxlength="2" placeholder="Leave blank to auto-generate">
                </div>
                <div class="btn-group">
                    <button type="button" class="btn-cancel" onclick="closeMenuModal()">Cancel</button>
                    <button type="submit" class="btn-save">Save Item</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        function updateTime() {
            const now = new Date();
            let hours = getHours(now);
            let minutes = now.getMinutes();
            let ampm = now.getHours() >= 12 ? 'PM' : 'AM';
            minutes = minutes < 10 ? '0' + minutes : minutes;
            document.getElementById('clock').innerText = hours + ':' + minutes + ' ' + ampm;
        }
        function getHours(now) {
            let hours = now.getHours() % 12;
            return hours ? hours : 12;
        }
        setInterval(updateTime, 1000);
        updateTime();

        function getStatusClass(status) {
            if (status === 'Preparing Order') return 'status-preparing';
            if (status === 'Ready for Pick-up') return 'status-ready';
            if (status === 'Completed') return 'status-completed';
            return 'status-preparing';
        }

        async function updateOrderStatus(orderId, newStatus) {
            try {
                const response = await fetch(`/api/orders/${orderId}/status`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status: newStatus })
                });
                if (response.ok) {
                    fetchLiveOrders(); 
                } else {
                    alert("Error updating order status.");
                }
            } catch (error) {
                console.error("Connection error updating status:", error);
            }
        }

        async function fetchLiveOrders() {
            try {
                const response = await fetch('/api/orders');
                const data = await response.json();

                document.getElementById('total-revenue-display').innerText = 'Total Sales: ₱' + data.total_revenue.toFixed(2);
                const tbody = document.getElementById('kds-table-body');
                tbody.innerHTML = '';

                if (data.orders.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding: 40px; color:#94A3B8; font-weight:600;">No active orders right now. Waiting for customers...</td></tr>';
                    return;
                }

                data.orders.forEach(order => {
                    let itemsHtml = '<ul class="infusion-list">';
                    order.items.forEach(item => {
                        itemsHtml += `<li><strong>${item.foundation}</strong> <span class="kds-badge">${item.pearls}</span></li>`;
                    });
                    itemsHtml += '</ul>';

                    const statusClass = getStatusClass(order.status);
                    const statusSelectHtml = `
                        <select class="status-select ${statusClass}" onchange="updateOrderStatus(${order.id}, this.value)">
                            <option value="Preparing Order" ${order.status === 'Preparing Order' ? 'selected' : ''}>Preparing</option>
                            <option value="Ready for Pick-up" ${order.status === 'Ready for Pick-up' ? 'selected' : ''}>Ready</option>
                            <option value="Completed" ${order.status === 'Completed' ? 'selected' : ''}>Completed</option>
                        </select>
                    `;

                    tbody.innerHTML += `
                        <tr>
                            <td class="kds-code">${order.code}</td>
                            <td style="font-weight:800; color:#0F172A;">${order.name}</td>
                            <td style="font-weight:700; color:#D97706;">₱${order.total.toFixed(2)}</td>
                            <td>${itemsHtml}</td>
                            <td style="color:#64748B; font-size:0.9rem;">${order.time}</td>
                            <td style="font-weight:800; color:#2563EB;">${order.pickup_time}</td>
                            <td>${statusSelectHtml}</td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error("Error fetching live orders:", error);
            }
        }
        
        fetchLiveOrders();
        setInterval(fetchLiveOrders, 3000);

        let editingItemId = null;

        async function fetchAdminMenu() {
            try {
                const res = await fetch('/api/menu');
                const items = await res.json();
                const tbodyMenu = document.getElementById('admin-menu-list');
                tbodyMenu.innerHTML = '';
                
                if (items.length === 0) {
                    tbodyMenu.innerHTML = '<tr><td colspan="4" style="text-align:center; color:#94A3B8; padding:20px;">No items in menu.</td></tr>';
                    return;
                }

                items.forEach(i => {
                    tbodyMenu.innerHTML += `
                        <tr>
                            <td style="font-weight:700; color:#0F172A;">
                                <span style="background:#E2E8F0; padding:2px 6px; border-radius:4px; margin-right:5px; font-size:0.75rem;">${i.letter}</span> 
                                ${i.name}
                            </td>
                            <td style="color:#64748B;">${i.category}</td>
                            <td style="color:#D97706; font-weight:700;">₱${i.price.toFixed(2)}</td>
                            <td style="text-align: right; white-space: nowrap;">
                                <button class="action-btn action-btn-edit" onclick="editMenu(${i.id}, '${i.name.replace(/'/g, "\\'")}', ${i.price}, '${i.category.replace(/'/g, "\\'")}', '${i.letter}')" title="Edit"><i class="fas fa-edit"></i></button>
                                <button class="action-btn action-btn-delete" onclick="deleteMenu(${i.id})" title="Delete"><i class="fas fa-trash-alt"></i></button>
                            </td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error("Error fetching menu:", error);
            }
        }

        async function fetchAdminInventory() {
            try {
                const res = await fetch('/api/inventory');
                const ings = await res.json();
                const tbodyInv = document.getElementById('admin-inventory-list');
                tbodyInv.innerHTML = '';
                
                if (ings.length === 0) {
                    tbodyInv.innerHTML = '<tr><td colspan="3" style="text-align:center; color:#94A3B8; padding:20px;">No inventory items.</td></tr>';
                    return;
                }

                ings.forEach(i => {
                    tbodyInv.innerHTML += `
                        <tr>
                            <td style="font-weight:700; color:#0F172A;">${i.name}</td>
                            <td style="color:#64748B; font-weight: 600;">${i.unit}</td>
                            <td style="text-align: center;">
                                <input type="number" class="input-pin stock-input" style="padding: 8px; font-size: 1rem; letter-spacing: 1px; margin: 0; width: 120px;" data-id="${i.id}" value="${i.stock}" min="0" step="0.1">
                            </td>
                        </tr>
                    `;
                });
            } catch (error) {
                console.error("Error fetching inventory:", error);
            }
        }

        fetchAdminMenu();
        fetchAdminInventory();

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
                    alert("Inventory successfully updated!");
                    fetchAdminInventory(); 
                } else {
                    alert("Error saving inventory.");
                }
            } catch(e) { 
                alert("Connection error while saving inventory."); 
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
                alert("Error saving menu item.");
            }
        }

        async function deleteMenu(id) {
            if (confirm("Are you sure you want to delete this menu item?")) {
                try {
                    await fetch(`/api/menu/${id}`, { method: 'DELETE' });
                    fetchAdminMenu();
                } catch (error) {
                    alert("Error deleting item.");
                }
            }
        }

        // --- Backup & Recovery Logic ---
        function downloadBackup() {
            window.location.href = "/api/backup";
        }

        async function restoreBackup() {
            const fileInput = document.getElementById('backup-file');
            if (!fileInput.files.length) {
                alert("⚠️ Please select a .json backup file to upload.");
                return;
            }
            
            if (!confirm("⚠️ CRITICAL WARNING: This will overwrite ALL current Inventory, Menus, Recipes, and Orders with the backup data. Are you absolutely sure you want to proceed?")) {
                return;
            }

            const formData = new FormData();
            formData.append("file", fileInput.files[0]);

            try {
                const res = await fetch('/api/restore', {
                    method: 'POST',
                    body: formData
                });
                const result = await res.json();
                
                if (res.ok) {
                    alert("✅ System successfully restored from backup!");
                    location.reload();
                } else {
                    alert("Error restoring system: " + result.message);
                }
            } catch (e) {
                alert("Connection error while restoring system.");
            }
        }

        function formatTimeTo12Hr(time24) {
            let[h, m] = time24.split(':');
            h = parseInt(h);
            let ampm = h >= 12 ? 'PM' : 'AM';
            h = h % 12 || 12;
            return `${h < 10 ? '0'+h : h}:${m} ${ampm}`;
        }

        function saveConfigurations() {
            const openTimeVal = document.getElementById('store-open').value;
            const closeTimeVal = document.getElementById('store-close').value;
            const pin = document.getElementById('store-pin').value;

            if (!openTimeVal || !closeTimeVal || !pin) {
                alert("⚠️ Required: Please configure the Store Opening Time, Closing Time, and PIN before generating the link.");
                return;
            }

            const now = new Date();
            now.setMinutes(now.getMinutes() + 15);
            document.getElementById('expiration-text').innerHTML = `<span style="color:#10B981;">• Expires: ${now.toLocaleString()}</span>`;

            const openTime = formatTimeTo12Hr(openTimeVal);
            const closeTime = formatTimeTo12Hr(closeTimeVal);

            const params = new URLSearchParams({
                open: openTime,
                close: closeTime,
                pin: btoa(pin), 
                exp: now.getTime()
            });

            let baseUrl = "{{ ordering_link }}";
            if (baseUrl.endsWith('/')) { baseUrl = baseUrl.slice(0, -1); }
            
            const posLinkField = document.getElementById('posLink');
            posLinkField.value = baseUrl + "/?" + params.toString();
            posLinkField.style.color = "#94A3B8";
            posLinkField.style.fontWeight = "normal";

            alert('Settings validated successfully! A new secure Ordering Link has been generated.');
        }

        function copyLink() {
            var copyText = document.getElementById("posLink");
            
            if (copyText.value === "Pending Configuration..." || copyText.value === "") {
                alert("⚠️ Cannot copy! Please configure the store and generate the link first.");
                return;
            }
            
            copyText.select();
            copyText.setSelectionRange(0, 99999);
            navigator.clipboard.writeText(copyText.value);
            alert("Ordering Link Copied Successfully!");
        }

        function switchTab(tabId, title, btnElement) {
            document.querySelectorAll('.tab-pane').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(btn => btn.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            btnElement.classList.add('active');
            document.getElementById('page-title').innerText = title;
            
            if (tabId === 'inventory') fetchAdminInventory();
            if (tabId === 'settings') fetchAdminMenu(); 
        }
    </script>
</body>
</html>
"""

# --- Application Routes ---

@app.route('/')
def storefront():
    # STRICT BACKEND SECURITY: Blocks anyone who goes to the raw website link
    open_time = request.args.get('open')
    close_time = request.args.get('close')
    exp_time = request.args.get('exp')

    if not open_time or not close_time or not exp_time:
        return """
        <div style="display:flex; height:100vh; width:100vw; justify-content:center; align-items:center; background:#F5F6F8; flex-direction:column; text-align:center; padding: 20px; font-family: sans-serif;">
            <h2 style="color:#0f172a; margin-bottom: 10px;">Store Not Configured</h2>
            <p style="color:#64748b;">You cannot access this site directly. Please scan the official ordering QR code provided by the cashier.</p>
        </div>
        """, 403
        
    return render_template_string(STOREFRONT_HTML)

@app.route('/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        pin = request.form.get('pin')
        if pin == ADMIN_MASTER_PIN:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid PIN. Access Denied."
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))
    link = request.host_url  
    return render_template_string(ADMIN_HTML, ordering_link=link)


@app.route('/api/menu', methods=['GET', 'POST'])
def handle_menu():
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
                "id": i.id, "name": i.name, "price": i.price, 
                "letter": i.letter, "category": i.category, 
                "stock": int(max(0, available_portions))
            })
        return jsonify(menu_data)
    
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    
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
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403

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
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    
    if request.method == 'GET':
        ings = Ingredient.query.order_by(Ingredient.name).all()
        return jsonify([{"id": i.id, "name": i.name, "unit": i.unit, "stock": i.stock} for i in ings])
        
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


# --- BACKUP & RECOVERY ENDPOINTS ---
@app.route('/api/backup', methods=['GET'])
def export_backup():
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    
    data = {
        "ingredients":[{"id": i.id, "name": i.name, "unit": i.unit, "stock": i.stock} for i in Ingredient.query.all()],
        "menu_items":[{"id": m.id, "name": m.name, "price": m.price, "letter": m.letter, "category": m.category} for m in MenuItem.query.all()],
        "recipe_items":[{"id": r.id, "menu_item_id": r.menu_item_id, "ingredient_id": r.ingredient_id, "quantity_required": r.quantity_required} for r in RecipeItem.query.all()],
        "reservations":[{"id": r.id, "reservation_code": r.reservation_code, "patron_name": r.patron_name, "patron_email": r.patron_email, "total_investment": r.total_investment, "status": r.status, "pickup_time": r.pickup_time, "created_at": r.created_at.isoformat()} for r in Reservation.query.all()],
        "infusions":[{"id": i.id, "reservation_id": i.reservation_id, "foundation": i.foundation, "sweetener": i.sweetener, "pearls": i.pearls, "item_total": i.item_total} for i in Infusion.query.all()]
    }
    
    return Response(
        json.dumps(data, indent=4),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment;filename=9599_backup_{datetime.now().strftime("%Y%m%d")}.json'}
    )

@app.route('/api/restore', methods=['POST'])
def import_backup():
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No file selected"}), 400
        
    try:
        data = json.load(file)
        
        db.session.execute(db.text('TRUNCATE TABLE infusions, reservations, recipe_items, menu_items, ingredients RESTART IDENTITY CASCADE;'))
        
        for i in data.get('ingredients',[]):
            db.session.add(Ingredient(**i))
            
        for m in data.get('menu_items',[]):
            db.session.add(MenuItem(**m))
            
        db.session.commit()
        
        for r in data.get('recipe_items',[]):
            db.session.add(RecipeItem(**r))
            
        for r in data.get('reservations', []):
            r['created_at'] = datetime.fromisoformat(r['created_at'])
            db.session.add(Reservation(**r))
            
        for i in data.get('infusions',[]):
            db.session.add(Infusion(**i))
        
        db.session.commit()
        
        db.session.execute(db.text("SELECT setval('ingredients_id_seq', coalesce((SELECT MAX(id)+1 FROM ingredients), 1), false)"))
        db.session.execute(db.text("SELECT setval('menu_items_id_seq', coalesce((SELECT MAX(id)+1 FROM menu_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('recipe_items_id_seq', coalesce((SELECT MAX(id)+1 FROM recipe_items), 1), false)"))
        db.session.execute(db.text("SELECT setval('reservations_id_seq', coalesce((SELECT MAX(id)+1 FROM reservations), 1), false)"))
        db.session.execute(db.text("SELECT setval('infusions_id_seq', coalesce((SELECT MAX(id)+1 FROM infusions), 1), false)"))
        db.session.commit()
        
        return jsonify({"status": "success", "message": "System successfully restored!"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/reserve', methods=['POST'])
def reserve_blend():
    data = request.json
    try:
        for item in data['items']:
            menu_item = MenuItem.query.filter_by(name=item['foundation']).first()
            if menu_item:
                if menu_item.recipe:
                    for r in menu_item.recipe:
                        if r.ingredient.stock < r.quantity_required:
                            db.session.rollback()
                            return jsonify({"status": "error", "message": f"Sorry, '{menu_item.name}' is out of stock! Missing {r.ingredient.name}."}), 400
                    
                    for r in menu_item.recipe:
                        r.ingredient.stock -= r.quantity_required
            else:
                db.session.rollback()
                return jsonify({"status": "error", "message": f"Item '{item['foundation']}' no longer exists."}), 400

        raw_pickup_time = data.get('pickup_time', '').strip()
        formatted_pickup_time = 'ASAP' 
        
        if raw_pickup_time:
            try:
                pt_obj = datetime.strptime(raw_pickup_time, '%H:%M')
                formatted_pickup_time = pt_obj.strftime('%I:%M %p')
            except ValueError:
                formatted_pickup_time = raw_pickup_time

        new_reservation = Reservation(
            patron_name=data['name'],
            patron_email=data['email'],
            total_investment=data['total'],
            pickup_time=formatted_pickup_time  
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
            "message": "Order Received.",
            "reservation_code": new_reservation.reservation_code
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/customer/status')
def customer_order_status():
    codes = request.args.get('codes', '')
    if not codes: return jsonify([])
    code_list = codes.split(',')
    orders = Reservation.query.filter(Reservation.reservation_code.in_(code_list)).all()
    results =[{'code': o.reservation_code, 'status': o.status} for o in orders]
    return jsonify(results)

@app.route('/api/orders/<int:order_id>/status', methods=['PUT'])
def update_order_status(order_id):
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    try:
        order = Reservation.query.get_or_404(order_id)
        data = request.json
        new_status = data.get('status')
        if new_status:
            order.status = new_status
            db.session.commit()
            return jsonify({"status": "success", "message": "Order status updated."}), 200
        return jsonify({"status": "error", "message": "No status provided."}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/orders')
def api_orders():
    if not session.get('is_admin'): return jsonify({"status": "error"}), 403
    all_reservations = Reservation.query.order_by(Reservation.created_at.desc()).all()
    revenue = sum(res.total_investment for res in all_reservations)
    orders_data =[]
    for res in all_reservations:
        items =[{'foundation': i.foundation, 'pearls': i.pearls} for i in res.infusions]
        time_str = res.created_at.strftime('%I:%M %p')
        orders_data.append({
            'id': res.id,
            'code': res.reservation_code,
            'name': res.patron_name,
            'total': res.total_investment,
            'status': res.status,
            'time': time_str,
            'pickup_time': res.pickup_time, 
            'items': items
        })
    return jsonify({'total_revenue': revenue, 'orders': orders_data})

# --- Initialization Logic ---
with app.app_context():
    try:
        db.create_all()
        
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
                {"name": "The Midnight Velvet", "price": 118.00, "letter": "M", "category": "Signature Series"},
                {"name": "The Jade Garden", "price": 122.00, "letter": "J", "category": "Signature Series"},
                {"name": "Classic Pearl Milk Tea", "price": 95.00, "letter": "C", "category": "Classic Milk Tea"},
                {"name": "Taro Symphony", "price": 105.00, "letter": "T", "category": "Matcha & Taro"},
                {"name": "Brown Sugar Deerioca", "price": 120.00, "letter": "B", "category": "Signature Series"},
                {"name": "Wintermelon Frost", "price": 90.00, "letter": "W", "category": "Fruit Infusions"},
                {"name": "Matcha Espresso", "price": 130.00, "letter": "M", "category": "Matcha & Taro"},
                {"name": "Matcha Strawberry", "price": 125.00, "letter": "M", "category": "Matcha & Taro"},
                {"name": "Strawberry Lychee", "price": 110.00, "letter": "S", "category": "Fruit Infusions"}
            ]
            
            for m_data in menu_data:
                item = MenuItem(**m_data)
                db.session.add(item)
            db.session.commit()

            def add_recipe(item_name, ingredient_name, qty):
                item = MenuItem.query.filter_by(name=item_name).first()
                ing = Ingredient.query.filter_by(name=ingredient_name).first()
                if item and ing:
                    db.session.add(RecipeItem(menu_item_id=item.id, ingredient_id=ing.id, quantity_required=qty))

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

# --- DESKTOP APP & SERVER EXECUTION ---
if __name__ == '__main__':
    if os.environ.get('RENDER') or os.environ.get('DYNO'):
        app.run(host='0.0.0.0', port=5000)
    else:
        try:
            import webview
            
            def start_server():
                app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

            server_thread = threading.Thread(target=start_server)
            server_thread.daemon = True
            server_thread.start()

            print("========================================")
            print(" STARTING SYSTEM (DESKTOP APP MODE)")
            print(f" CUSTOMER POS LINK: http://{get_local_ip()}:5000/")
            print("========================================")

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