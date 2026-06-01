"""
POS System Reliability Utilities — Error Handling, Retries, Validation, Logging
Group 5 | 06/01/26
"""

import time
import logging
import functools
import traceback
from typing import Callable, Any, TypeVar, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import json

# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING
# ══════════════════════════════════════════════════════════════════════════════

class LogLevel(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class StructuredLogger:
    """Provides structured logging with context and metrics."""
    
    def __init__(self, name: str, log_file: str = "system.log"):
        self.name = name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        
        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        )
        console.setFormatter(formatter)
        self.logger.addHandler(console)
        
        # File handler
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except Exception as e:
            print(f"[WARNING] Could not open log file {log_file}: {e}")
    
    def log(self, level: LogLevel, message: str, context: dict = None, exc_info=None):
        """Log with structured context."""
        context = context or {}
        extra = json.dumps(context) if context else ""
        full_msg = f"{message} | {extra}" if extra else message
        
        if level == LogLevel.DEBUG:
            self.logger.debug(full_msg, exc_info=exc_info)
        elif level == LogLevel.INFO:
            self.logger.info(full_msg, exc_info=exc_info)
        elif level == LogLevel.WARN:
            self.logger.warning(full_msg, exc_info=exc_info)
        elif level == LogLevel.ERROR:
            self.logger.error(full_msg, exc_info=exc_info)
        elif level == LogLevel.CRITICAL:
            self.logger.critical(full_msg, exc_info=exc_info)


app_logger = StructuredLogger("9599_POS")


# ══════════════════════════════════════════════════════════════════════════════
# RETRY LOGIC WITH EXPONENTIAL BACKOFF
# ══════════════════════════════════════════════════════════════════════════════

class RetryConfig:
    """Configuration for retry behavior."""
    
    def __init__(
        self,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number (0-indexed)."""
        delay = self.initial_delay * (self.exponential_base ** attempt)
        delay = min(delay, self.max_delay)
        
        if self.jitter:
            import random
            delay *= (0.5 + random.random())
        
        return delay


F = TypeVar('F', bound=Callable[..., Any])


def retry_on_exception(
    config: RetryConfig = None,
    exceptions: Tuple = (Exception,),
    on_retry: Callable = None,
) -> Callable[[F], F]:
    """
    Decorator: Retry function with exponential backoff.
    
    Args:
        config: RetryConfig instance (uses defaults if None)
        exceptions: Tuple of exception types to catch and retry
        on_retry: Optional callback(attempt, delay, exception)
    """
    config = config or RetryConfig()
    
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(config.max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < config.max_attempts - 1:
                        delay = config.get_delay(attempt)
                        if on_retry:
                            on_retry(attempt + 1, delay, e)
                        app_logger.log(
                            LogLevel.WARN,
                            f"Retry {attempt + 1}/{config.max_attempts} for {func.__name__}",
                            {"delay": delay, "error": str(e)},
                        )
                        time.sleep(delay)
            
            # All retries exhausted
            app_logger.log(
                LogLevel.ERROR,
                f"All {config.max_attempts} attempts failed for {func.__name__}",
                {"error": str(last_exc)},
                exc_info=last_exc,
            )
            raise last_exc
        
        return wrapper  # type: ignore
    
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING DECORATOR
# ══════════════════════════════════════════════════════════════════════════════

class AppError(Exception):
    """Base application error with HTTP status code."""
    
    def __init__(self, message: str, status_code: int = 500, context: dict = None):
        self.message = message
        self.status_code = status_code
        self.context = context or {}
        super().__init__(message)


class ValidationError(AppError):
    """Input validation error."""
    def __init__(self, message: str, context: dict = None):
        super().__init__(message, 400, context)


class DatabaseError(AppError):
    """Database operation error."""
    def __init__(self, message: str, context: dict = None):
        super().__init__(message, 500, context)


class ExternalAPIError(AppError):
    """External API call error."""
    def __init__(self, message: str, context: dict = None):
        super().__init__(message, 502, context)


def safe_endpoint(
    log_context: dict = None,
    default_error: str = "Internal server error",
):
    """
    Decorator: Wrap Flask endpoints with error handling & logging.
    Returns JSON error responses with proper status codes.
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            context = log_context or {}
            start_time = time.time()
            
            try:
                app_logger.log(
                    LogLevel.DEBUG,
                    f"Endpoint START: {func.__name__}",
                    context,
                )
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                
                app_logger.log(
                    LogLevel.DEBUG,
                    f"Endpoint SUCCESS: {func.__name__}",
                    {**context, "duration_ms": duration * 1000},
                )
                return result
            
            except AppError as e:
                duration = time.time() - start_time
                app_logger.log(
                    LogLevel.WARN,
                    f"Endpoint APP_ERROR: {func.__name__} - {e.message}",
                    {**context, **e.context, "duration_ms": duration * 1000},
                )
                # Import here to avoid circular imports
                from flask import jsonify
                return jsonify({"error": e.message, **e.context}), e.status_code
            
            except Exception as e:
                duration = time.time() - start_time
                error_id = str(int(time.time() * 1000000))
                app_logger.log(
                    LogLevel.ERROR,
                    f"Endpoint EXCEPTION: {func.__name__} [ID: {error_id}]",
                    {**context, "error": str(e), "duration_ms": duration * 1000},
                    exc_info=e,
                )
                # Import here to avoid circular imports
                from flask import jsonify
                return (
                    jsonify({
                        "error": default_error,
                        "error_id": error_id,
                    }),
                    500,
                )
        
        return wrapper  # type: ignore
    
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# INPUT VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class Validator:
    """Utilities for input validation."""
    
    @staticmethod
    def require_fields(data: dict, required: list, name: str = "Request") -> dict:
        """Validate that all required fields are present and non-empty."""
        missing = [f for f in required if not data.get(f)]
        if missing:
            raise ValidationError(
                f"{name} missing required fields: {', '.join(missing)}",
                {"missing_fields": missing}
            )
        return data
    
    @staticmethod
    def validate_int(value: Any, name: str, min_val: int = None, max_val: int = None) -> int:
        """Validate and parse integer."""
        try:
            num = int(value)
            if min_val is not None and num < min_val:
                raise ValidationError(
                    f"{name} must be >= {min_val}",
                    {"field": name, "value": num, "min": min_val}
                )
            if max_val is not None and num > max_val:
                raise ValidationError(
                    f"{name} must be <= {max_val}",
                    {"field": name, "value": num, "max": max_val}
                )
            return num
        except (ValueError, TypeError):
            raise ValidationError(
                f"{name} must be a valid integer",
                {"field": name, "value": value}
            )
    
    @staticmethod
    def validate_float(value: Any, name: str, min_val: float = None, max_val: float = None) -> float:
        """Validate and parse float."""
        try:
            num = float(value)
            if min_val is not None and num < min_val:
                raise ValidationError(
                    f"{name} must be >= {min_val}",
                    {"field": name, "value": num}
                )
            if max_val is not None and num > max_val:
                raise ValidationError(
                    f"{name} must be <= {max_val}",
                    {"field": name, "value": num}
                )
            return num
        except (ValueError, TypeError):
            raise ValidationError(
                f"{name} must be a valid number",
                {"field": name, "value": value}
            )
    
    @staticmethod
    def validate_string(value: Any, name: str, min_len: int = 1, max_len: int = None) -> str:
        """Validate and parse string."""
        if not isinstance(value, str):
            raise ValidationError(
                f"{name} must be a string",
                {"field": name, "type": type(value).__name__}
            )
        s = value.strip()
        if len(s) < min_len:
            raise ValidationError(
                f"{name} must be at least {min_len} characters",
                {"field": name, "length": len(s)}
            )
        if max_len and len(s) > max_len:
            raise ValidationError(
                f"{name} must be at most {max_len} characters",
                {"field": name, "length": len(s), "max": max_len}
            )
        return s
    
    @staticmethod
    def validate_choice(value: Any, name: str, choices: list) -> Any:
        """Validate value is in allowed choices."""
        if value not in choices:
            raise ValidationError(
                f"{name} must be one of: {', '.join(map(str, choices))}",
                {"field": name, "value": value, "choices": choices}
            )
        return value


# ══════════════════════════════════════════════════════════════════════════════
# TRANSACTION SAFETY
# ══════════════════════════════════════════════════════════════════════════════

def safe_transaction(func: F) -> F:
    """
    Decorator: Wrap function with database transaction safety.
    Auto-commits on success, auto-rollsback on exception.
    """
    @functools.wraps(func)
    def wrapper(*args, db_session=None, **kwargs):
        if db_session is None:
            # Import here to avoid circular dependency
            from flask_sqlalchemy import SQLAlchemy
            raise ValueError("safe_transaction requires db_session kwarg")
        
        try:
            result = func(*args, **kwargs)
            db_session.commit()
            return result
        except Exception as e:
            db_session.rollback()
            app_logger.log(
                LogLevel.ERROR,
                f"Transaction rolled back for {func.__name__}",
                {"error": str(e)},
                exc_info=e,
            )
            raise
    
    return wrapper  # type: ignore


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECKS & MONITORING
# ══════════════════════════════════════════════════════════════════════════════

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class SystemHealth:
    """Track system health and dependencies."""
    
    def __init__(self):
        self.checks = {}
        self.last_check = None
    
    def register_check(self, name: str, check_fn: Callable) -> None:
        """Register a health check function."""
        self.checks[name] = check_fn
    
    def get_status(self) -> Tuple[HealthStatus, dict]:
        """Run all checks and return overall status."""
        results = {}
        all_ok = True
        
        for name, check_fn in self.checks.items():
            try:
                check_fn()
                results[name] = "ok"
            except Exception as e:
                results[name] = str(e)
                all_ok = False
        
        self.last_check = datetime.now()
        
        if all_ok:
            status = HealthStatus.HEALTHY
        else:
            status = HealthStatus.DEGRADED
        
        return status, results


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class RateLimitExceeded(AppError):
    """Rate limit exceeded error."""
    def __init__(self, retry_after: int = 60):
        super().__init__(
            f"Too many requests. Retry after {retry_after}s",
            429,
            {"retry_after": retry_after}
        )


# ══════════════════════════════════════════════════════════════════════════════
# GRACEFUL DEGRADATION
# ══════════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """Simple circuit breaker for external service calls."""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half_open
    
    def record_failure(self) -> None:
        """Record a failure."""
        self.failures += 1
        self.last_failure_time = datetime.now()
        
        if self.failures >= self.failure_threshold:
            self.state = "open"
            app_logger.log(
                LogLevel.WARN,
                "Circuit breaker opened",
                {"failures": self.failures}
            )
    
    def record_success(self) -> None:
        """Record a success."""
        self.failures = 0
        self.state = "closed"
    
    def is_available(self) -> bool:
        """Check if service is available."""
        if self.state == "closed":
            return True
        
        if self.state == "open":
            elapsed = (datetime.now() - self.last_failure_time).total_seconds()
            if elapsed > self.timeout:
                self.state = "half_open"
                app_logger.log(LogLevel.INFO, "Circuit breaker half-open")
                return True
            return False
        
        return True  # half_open


# Initialize global health status
system_health = SystemHealth()
