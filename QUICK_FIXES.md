# Quick Reliability Fixes — Apply These First

**Tea & Coffee Shop POS | Group 5**

---

## 1. Fix Most Critical Bug: Always Rollback on Error

Add to `app.py` around line 450 where functions use `db.session.commit()`:

```python
# PATCH: Ensure all endpoints rollback on failure
def ensure_rollback(func):
    """Decorator: Auto-rollback db.session on exception."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            db.session.rollback()
            raise
    return wrapper
```

Then wrap all POST/PUT/DELETE endpoints:
```python
@app.route('/api/menu', methods=['POST'])
@ensure_rollback
def add_menu_item():
    # existing code
```

---

## 2. Fix Race Condition in Inventory

In the `/reserve` endpoint (around line 5000), change:

```python
# BEFORE (Racy)
for item in items:
    ingredient = Ingredient.query.filter_by(name=item['name']).first()
    ingredient.stock -= item['qty']
    db.session.add(ingredient)
db.session.commit()

# AFTER (Safe - use pessimistic lock)
ingredients = db.session.query(Ingredient).filter(
    Ingredient.name.in_([i['name'] for i in items])
).with_for_update().all()

ing_map = {ing.name: ing for ing in ingredients}
for item in items:
    ing = ing_map[item['name']]
    if ing.stock < item['qty']:
        raise ValueError(f"Insufficient stock for {ing.name}")
    ing.stock -= item['qty']
db.session.commit()
```

---

## 3. Add Input Validation to Critical Endpoints

```python
# Import at top
from reliability_utils import Validator, ValidationError

# In any endpoint processing user input:
@app.route('/api/expenses', methods=['POST'])
def add_expense():
    data = request.json or {}
    
    # Validate before using
    try:
        desc = Validator.validate_string(data.get('description', ''), 'Description', max_len=200)
        amount = Validator.validate_float(data.get('amount', 0), 'Amount', min_val=0.01)
    except ValidationError as e:
        return jsonify({"error": e.message}), 400
    
    # Rest of function...
    new_expense = Expense(description=desc, amount=amount)
    db.session.add(new_expense)
    db.session.commit()
    return jsonify({"status": "success"})
```

---

## 4. Add Retry Logic to External APIs

```python
from reliability_utils import retry_on_exception, RetryConfig
import requests

retry_config = RetryConfig(max_attempts=3, initial_delay=1.0)

@retry_on_exception(config=retry_config, exceptions=(requests.RequestException,))
def verify_hcaptcha(token: str):
    resp = requests.post(
        HCAPTCHA_VERIFY_URL,
        data={'secret': HCAPTCHA_SECRET_KEY, 'response': token},
        timeout=8,
    )
    resp.raise_for_status()
    return resp.json()
```

---

## 5. Fix Audit Logging to Never Fail

```python
from reliability_utils import app_logger, LogLevel

def log_audit(action, details="", ip=None):
    """Guaranteed audit logging."""
    try:
        new_log = AuditLog(action=action, details=details[:255], ip_address=ip or '')
        db.session.add(new_log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # Log to file even if DB fails
        app_logger.log(LogLevel.ERROR, f"Audit log failed: {action}", {"error": str(e)})
```

---

## 6. Add Health Check Endpoint

```python
from reliability_utils import system_health, HealthStatus

# After db.create_all()
system_health.register_check("database", lambda: db.session.execute(db.text("SELECT 1")))
system_health.register_check("hcaptcha", lambda: None if HCAPTCHA_SECRET_KEY else (_ for _ in ()).throw(Exception("Missing HCAPTCHA")))

@app.route('/health', methods=['GET'])
def health():
    status, checks = system_health.get_status()
    return jsonify({"status": status.value, "checks": checks}), 200 if status == HealthStatus.HEALTHY else 503
```

---

## 7. Enable Connection Pooling

After line ~380 where `app.config['SQLALCHEMY_DATABASE_URI']` is set, add:

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

---

## Priority Order (Do These First)

1. **CRITICAL** - Add rollback to all DB operations (prevents data corruption)
2. **CRITICAL** - Fix race condition in inventory (prevents overselling)
3. **HIGH** - Add input validation (prevents crashes)
4. **HIGH** - Add health checks (early failure detection)
5. **MEDIUM** - Connection pooling (handles traffic spikes)
6. **MEDIUM** - Retry logic (handles network hiccups)

---

## Testing Each Fix

```bash
# After each change, run:

# 1. Test rollback
curl -X POST http://localhost:5000/api/expenses \
  -H "Content-Type: application/json" \
  -d '{"description":"Test"}'  # Missing amount - should error gracefully

# 2. Test inventory lock
# Make 2 concurrent orders for same item

# 3. Test health check
curl http://localhost:5000/health

# 4. Run test suite
pytest test.py -v
```

---

## Estimated Time Impact

- **Rollback Fix**: 5 minutes
- **Inventory Lock**: 10 minutes  
- **Input Validation**: 15 minutes per endpoint
- **Health Check**: 5 minutes
- **Connection Pooling**: 2 minutes
- **Testing**: 30 minutes

**Total: ~1 hour for all critical fixes**

---

## Files to Reference

- `reliability_utils.py` - Helper functions
- `RELIABILITY_IMPROVEMENTS.md` - Full guide with examples
- System logs: `system.log`

Apply these in order, test each change, then deploy.
