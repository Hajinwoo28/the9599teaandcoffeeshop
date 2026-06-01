def main():
    src = open('app.py', encoding='utf-8').read()

    checks = {
        'reliability_utils imported':    'from reliability_utils import' in src,
        'CircuitBreaker for hCaptcha':   '_hcaptcha_circuit = CircuitBreaker' in src,
        'hCaptcha retry loop (3x)':      'for attempt in range(3)' in src,
        'Connection pooling pre_ping':   'pool_pre_ping' in src,
        'Health checks registered':      'system_health.register_check' in src,
        '/health endpoint defined':      "route('/health'" in src,
        'log_audit safe_details':        'safe_details' in src,
        'log_audit AUDIT DB WRITE':      'AUDIT DB WRITE FAILED' in src,
        'log_audit has rollback':        'db.session.rollback()' in src,
    }

    all_ok = True
    for name, passed in checks.items():
        icon = 'OK     ' if passed else 'MISSING'
        print(f'  [{icon}] {name}')
        if not passed:
            all_ok = False

    print()
    print('All reliability checks passed!' if all_ok else 'Some checks FAILED - review above.')


if __name__ == '__main__':
    main()
