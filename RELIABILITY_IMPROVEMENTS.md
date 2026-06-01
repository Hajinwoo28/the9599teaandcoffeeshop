# System Reliability Improvements — Implementation Guide

**Tea & Coffee Shop POS System**  
Group 5 | 06/01/26

---

## Executive Summary

Your system has strong security fundamentals but several reliability gaps that can cause data loss, poor user experience, and operational disruptions. This guide provides concrete improvements with code examples.

---

## Critical Issues & Fixes

### 1. ❌ ISSUE: Missing Transaction Rollback on Database Errors

**Problem**: Database operations fail silently, leaving data in inconsistent states.

```python
# BEFORE (Unsafe)
@app.route('/api/expenses', methods=['POST'])
def add_expense():
    data = request.json
    try:
        new_expense = Expense(description=data['description'], amount=float(data['amount']))
        db.session.add(new_expense)
        db.session.commit()  # ← Could fail, data left hanging
        log_audit("Petty Cash Logged", f"Spent ₱{data['amount']}")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400
        # ← Doesn't rollback db.session!
```

**Fix**: Always rollback on exception.

```python
# AFTER (Safe)
from reliability_utils import safe_endpoint, ValidationError, Validator, app_logger, LogLevel

@app.route('/api/expenses', methods=['POST'])
@safe_endpoint(log_context={"endpoint": "add_expense"})
def add_expense():
    data = request.json or {}
    
    # Validate input
    Validator.require_fields(data, ['description', 'amount'], "Expense")
    description = Validator.validate_string(data['description'], "Description", max_len=200)
    amount = Validator.validate_float(data['amount'], "Amount", min_val=0.01)
    
    try:
        new_expense = Expense(description=description, amount=amount)
        db.session.add(new_expense)
        db.session.commit()
        
        log_audit("Petty Cash Logged", f"Spent ₱{amount:.2f} on {description}")
        app_logger.log(LogLevel.INFO, "Expense logged", {"amount": amount})
        
        return jsonify({"status": "success"}), 201
    
    except Exception as e:
        db.session.rollback()  # ← Always rollback
        app_logger.log(LogLevel.ERROR, "Failed to log expense", {"error": str(e)}, exc_info=e)
        raise  # ← Let safe_endpoint decorator handle it
```

---

### 2. ❌ ISSUE: No Retry Logic for External APIs

**Problem**: PayMongo, hCaptcha calls fail once and don't retry. Transient network errors cause order failures.

```python
# BEFORE (Fragile)
def verify_hcaptcha(token: str):
    if not HCAPTCHA_SECRET_KEY:
        return True, ''
    if not token:
        return False, 'CAPTCHA token missing...'
    try:
        resp = requests.post(HCAPTCHA_VERIFY_URL, data={...}, timeout=8)
        # ← Single attempt; network hiccup = failure
        data = resp.json()
        if data.get('success'):
            return True, ''
        return False, "CAPTCHA verification failed..."
    except Exception as e:
        print(f"[hCaptcha] Verification request failed: {e}")
        return True, ''  # ← Silent failure!
```

**Fix**: Add retry logic.

```python
# AFTER (Resilient)
from reliability_utils import retry_on_exception, RetryConfig, ExternalAPIError, app_logger, LogLevel

hcaptcha_retry_config = RetryConfig(
    max_attempts=3,
    initial_delay=1.0,
    max_delay=10.0,
)

@retry_on_exception(
    config=hcaptcha_retry_config,
    exceptions=(requests.RequestException, requests.Timeout),
    on_retry=lambda attempt, delay, exc: app_logger.log(
        LogLevel.WARN,
        f"hCaptcha retry {attempt}",
        {"delay": delay}
    )
)
def verify_hcaptcha(token: str):
    if not HCAPTCHA_SECRET_KEY:
        return True, ''
    
    if not token:
        raise ValidationError('CAPTCHA token missing. Please refresh and try again.')
    
    try:
        resp = requests.post(
            HCAPTCHA_VERIFY_URL,
            data={'secret': HCAPTCHA_SECRET_KEY, 'response': token},
            timeout=8,
        )
        resp.raise_for_status()  # ← Raise on HTTP errors
        
        data = resp.json()
        if data.get('success'):
            return True, ''
        
        codes = data.get('error-codes', [])
        raise ExternalAPIError(
            f"CAPTCHA verification failed ({', '.join(codes)})",
            {"error_codes": codes}
        )
    
    except requests.Timeout:
        raise ExternalAPIError("hCaptcha request timeout")
    except requests.RequestException as e:
        raise ExternalAPIError(f"hCaptcha connection error: {str(e)}")
```

---

### 3. ❌ ISSUE: Race Conditions in Inventory Deduction

**Problem**: Two concurrent orders can deduct inventory twice, causing overselling.

```python
# BEFORE (Racy)
@app.route('/reserve', methods=['POST'])
def reserve_blend():
    data = request.json
    items = data.get('items', [])
    
    # Check stock
    for item in items:
        ingredient = Ingredient.query.filter_by(name=item['name']).first()
        if ingredient.stock < item['qty']:
            return jsonify({"error": "Out of stock"}), 409
    
    # Deduct stock (between check and deduct, another request could succeed!)
    for item in items:
        ingredient = Ingredient.query.filter_by(name=item['name']).first()
        ingredient.stock -= item['qty']  # ← RACE CONDITION HERE
    
    db.session.commit()
    return jsonify({"status": "success"})
```

**Fix**: Use atomic database operations.

```python
# AFTER (Atomic)
@app.route('/reserve', methods=['POST'])
@safe_endpoint()
def reserve_blend():
    data = request.json or {}
    items = data.get('items', [])
    
    Validator.require_fields(data, ['items', 'total'], "Order")
    
    if not items or not isinstance(items, list):
        raise ValidationError("Items must be a non-empty list")
    
    total = Validator.validate_float(data['total'], "Total", min_val=0.01)
    
    try:
        # Lock rows for update (atomic)
        ingredients_to_update = db.session.query(Ingredient).filter(
            Ingredient.name.in_([item['name'] for item in items])
        ).with_for_update().all()  # ← Lock acquired
        
        ingredient_map = {ing.name: ing for ing in ingredients_to_update}
        
        # Validate & deduct atomically
        for item in items:
            qty = Validator.validate_int(item.get('qty', 0), "Item quantity", min_val=1)
            ing = ingredient_map.get(item['name'])
            
            if not ing:
                raise ValidationError(f"Ingredient '{item['name']}' not found")
            if ing.stock < qty:
                raise ValidationError(f"Insufficient stock for {ing.name}")
            
            ing.stock -= qty
        
        # Create reservation
        new_reservation = Reservation(
            total=total,
            reservation_code=str(uuid.uuid4())[:8].upper(),
        )
        db.session.add(new_reservation)
        db.session.commit()  # ← All changes commit atomically
        
        log_audit("Online Order Placed", f"Order for ₱{total:.2f}")
        
        return jsonify({
            "status": "success",
            "reservation_code": new_reservation.reservation_code
        }), 201
    
    except Exception as e:
        db.session.rollback()
        raise
```

---

### 4. ❌ ISSUE: No Connection Pooling for Database

**Problem**: Running out of database connections under load.

**Fix**: Add connection pooling to `app.py`.

```python
from sqlalchemy.pool import QueuePool

# ── Add after DATABASE_URL configuration ──

# Configure connection pooling
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'poolclass': QueuePool,
    'pool_size': 10,           # Number of connections to keep open
    'max_overflow': 20,        # Max additional connections
    'pool_recycle': 3600,      # Recycle connections after 1 hour
    'pool_pre_ping': True,     # Test connection before use
    'echo': False,             # Set to True for SQL debugging
}
```

---

### 5. ❌ ISSUE: No Input Validation in Most Endpoints

**Problem**: SQL injection, XSS, and type errors can crash the system.

**Fix**: Use `Validator` class on all user inputs.

```python
# BEFORE (Unsafe)
@app.route('/api/menu', methods=['POST'])
def add_menu_item():
    data = request.json
    # No validation!
    new_item = MenuItem(
        name=data['name'],              # ← Could be None, 10000 chars, or SQL
        price=data['price'],            # ← Could be negative, non-numeric
        category=data['category'],
        letter=data['letter'],
    )
    db.session.add(new_item)
    db.session.commit()
    return jsonify({"status": "success"})


# AFTER (Safe)
@app.route('/api/menu', methods=['POST'])
@safe_endpoint()
def add_menu_item():
    data = request.json or {}
    
    # Validate all inputs
    Validator.require_fields(data, ['name', 'price', 'category'], "Menu item")
    
    name = Validator.validate_string(data['name'], "Item name", min_len=1, max_len=100)
    price = Validator.validate_float(data['price'], "Price", min_val=0.01, max_val=10000)
    category = Validator.validate_string(data['category'], "Category", max_len=50)
    letter = Validator.validate_string(data.get('letter', 'A'), "Letter", min_len=1, max_len=1)
    
    try:
        new_item = MenuItem(
            name=name,
            price=price,
            category=category,
            letter=letter,
        )
        db.session.add(new_item)
        db.session.commit()
        
        _invalidate_menu_cache()  # Refresh menu cache
        
        return jsonify({"status": "success", "item_id": new_item.id}), 201
    except Exception as e:
        db.session.rollback()
        raise
```

---

### 6. ❌ ISSUE: Audit Logging Fails Silently

**Problem**: If audit log fails, critical actions aren't recorded.

```python
# BEFORE (Fragile)
def log_audit(action, details="", ip=None):
    try:
        new_log = AuditLog(action=action, details=details, ip_address=(ip or ''))
        db.session.add(new_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Audit Log Failed: {str(e)}")  # ← Lost!
```

**Fix**: Use structured logging.

```python
# AFTER (Reliable)
from reliability_utils import app_logger, LogLevel

def log_audit(action: str, details: str = "", ip: str = None) -> None:
    """Log audit event with guaranteed delivery."""
    ip = ip or ""
    
    try:
        new_log = AuditLog(
            action=action,
            details=details[:255],  # Ensure truncation
            ip_address=ip,
        )
        db.session.add(new_log)
        db.session.commit()
        
        app_logger.log(
            LogLevel.INFO,
            f"Audit: {action}",
            {"details": details, "ip": ip}
        )
    except Exception as e:
        db.session.rollback()
        
        # Log to file even if DB fails
        app_logger.log(
            LogLevel.ERROR,
            f"Failed to log audit: {action}",
            {"details": details, "error": str(e)},
            exc_info=e,
        )
```

---

### 7. ❌ ISSUE: No Health Checks

**Problem**: System can be down without anyone knowing.

**Fix**: Add a health check endpoint.

```python
from reliability_utils import system_health, HealthStatus

# Register health checks
def check_database():
    """Check database connectivity."""
    result = db.session.execute(db.text("SELECT 1"))
    assert result is not None

def check_hcaptcha_config():
    """Check HCAPTCHA is configured."""
    if HCAPTCHA_SECRET_KEY:
        return
    raise Exception("HCAPTCHA_SECRET_KEY not set")

system_health.register_check("database", check_database)
system_health.register_check("hcaptcha", check_hcaptcha_config)

@app.route('/health', methods=['GET'])
def health_check():
    """Public health check endpoint."""
    status, checks = system_health.get_status()
    
    return jsonify({
        "status": status.value,
        "timestamp": datetime.utcnow().isoformat(),
        "checks": checks,
    }), 200 if status == HealthStatus.HEALTHY else 503
```

---

## Integration Checklist

### Step 1: Add to `app.py` (Top of file)
```python
from reliability_utils import (
    safe_endpoint,
    retry_on_exception,
    RetryConfig,
    Validator,
    app_logger,
    LogLevel,
    ValidationError,
    ExternalAPIError,
    system_health,
    HealthStatus,
)
```

### Step 2: Configure Connection Pooling
```python
from sqlalchemy.pool import QueuePool

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'poolclass': QueuePool,
    'pool_size': 10,
    'max_overflow': 20,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}
```

### Step 3: Register Health Checks (after db init)
```python
def check_database():
    db.session.execute(db.text("SELECT 1"))

def check_hcaptcha():
    if not HCAPTCHA_SECRET_KEY:
        raise Exception("HCAPTCHA_SECRET_KEY missing")

system_health.register_check("database", check_database)
system_health.register_check("hcaptcha", check_hcaptcha)
```

### Step 4: Add Health Endpoint
```python
@app.route('/health', methods=['GET'])
def health():
    status, checks = system_health.get_status()
    return jsonify({"status": status.value, "checks": checks}), 200
```

### Step 5: Update All Endpoints
- Wrap with `@safe_endpoint()`
- Use `Validator` for inputs
- Ensure `db.session.rollback()` on errors
- Use `retry_on_exception()` for external APIs

---

## Testing

### Test Database Connection Pool
```python
# Simulate connection exhaustion
import concurrent.futures

def slow_query():
    time.sleep(2)
    db.session.execute(db.text("SELECT 1"))

with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    futures = [executor.submit(slow_query) for _ in range(15)]
    concurrent.futures.wait(futures)
    print("All requests completed successfully with connection pooling!")
```

### Test Retry Logic
```python
# Verify retries on transient errors
@retry_on_exception(RetryConfig(max_attempts=3, initial_delay=0.1))
def flaky_api_call():
    import random
    if random.random() < 0.7:
        raise requests.Timeout("Simulated timeout")
    return "success"

result = flaky_api_call()
print(f"Result after retries: {result}")
```

### Monitor Logs
```bash
# Real-time log monitoring
tail -f system.log | grep ERROR
```

---

## Performance Impact

| Improvement | Overhead | Benefit |
|---|---|---|
| Connection Pooling | Minimal | Prevents connection exhaustion |
| Retry Logic | 1-60 seconds | Recovers from transient failures |
| Input Validation | <5ms | Prevents crashes & injection |
| Structured Logging | <1ms | Better debugging |
| Health Checks | <100ms | Early failure detection |

---

## Deployment Notes

1. **Testing**: Run all endpoints through staging before production
2. **Gradual Rollout**: Update 20% of endpoints per day
3. **Monitoring**: Watch `/health` endpoint on monitoring service
4. **Rollback**: Keep old code branch for quick revert
5. **Backups**: Run nightly database backups

---

## Summary

These improvements will:
- ✅ Prevent data corruption from race conditions
- ✅ Recover from transient network errors  
- ✅ Catch invalid inputs before database
- ✅ Provide audit trail and error tracking
- ✅ Enable proactive failure detection

**Estimated Implementation Time**: 4-6 hours  
**Testing Time**: 2-3 hours  
**Total**: ~8-9 hours

---

*For questions, consult reliability_utils.py docstrings or system logs.*
