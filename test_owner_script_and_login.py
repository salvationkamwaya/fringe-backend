import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core.management import call_command
from rest_framework.test import APIClient
from business_core.models import User, AuditLog

def test_owner_script_and_dual_login():
    print("=" * 70)
    print("TESTING SCRIPT-BASED OWNER CREATION & USERNAME/EMAIL DUAL AUTHENTICATION")
    print("=" * 70)

    # 1. Run script / management command to create or update owner
    print("\n--- 1. Executing create_owner Script via call_command ---")
    test_username = 'drmitu'
    test_email = 'dr.mitu@fringe.local'
    test_password = 'OwnerSecurePassword2026!'
    test_pin = '7788'

    call_command(
        'create_owner',
        username=test_username,
        email=test_email,
        password=test_password,
        pin=test_pin,
        first_name='Dr.',
        last_name='Mitu',
        non_interactive=True
    )

    owner = User.objects.filter(username=test_username).first()
    assert owner is not None, "Owner user was not found in database!"
    assert owner.email == test_email, f"Expected email {test_email}, got {owner.email}"
    assert owner.role == User.ROLE_OWNER, f"Expected role {User.ROLE_OWNER}, got {owner.role}"
    assert owner.business_unit is None, "Owner should have business_unit=None for global access"
    assert owner.is_superuser is True, "Owner should be a superuser"
    assert owner.check_pin(test_pin) is True, "PIN hash check failed"
    print(f"[OK] Database record confirmed for {owner.username} ({owner.email}), Role: {owner.role}")

    # Verify audit log was recorded
    audit_log = AuditLog.objects.filter(entity_type='User', entity_id=owner.id, action__in=['STAFF_CREATED', 'STAFF_ROLE_ASSIGNED']).last()
    assert audit_log is not None, "Audit log was not recorded for owner creation"
    print(f"[OK] Audit log confirmed: Action: {audit_log.action} | User: {audit_log.username}")

    client = APIClient()

    # 2. Login via USERNAME
    print("\n--- 2. Testing Login Using USERNAME ('drmitu') ---")
    resp_username = client.post('/api/auth/login/', {
        'username': test_username,
        'password': test_password
    }, format='json')
    assert resp_username.status_code == 200, f"Login via username failed: {resp_username.data}"
    assert resp_username.data['user']['username'] == test_username
    assert resp_username.data['user']['role'] == 'owner'
    token_username = resp_username.data['token']
    print(f"[OK] Success! Logged in with USERNAME. User: {resp_username.data['user']['username']}, Role: {resp_username.data['user']['role']}")

    # 3. Login via EMAIL
    print("\n--- 3. Testing Login Using EMAIL ('dr.mitu@fringe.local') ---")
    resp_email = client.post('/api/auth/login/', {
        'username': test_email,  # Sent in the username/login field
        'password': test_password
    }, format='json')
    assert resp_email.status_code == 200, f"Login via email failed: {resp_email.data}"
    assert resp_email.data['user']['username'] == test_username
    assert resp_email.data['user']['email'] == test_email
    assert resp_email.data['user']['role'] == 'owner'
    token_email = resp_email.data['token']
    print(f"[OK] Success! Logged in with EMAIL. User: {resp_email.data['user']['username']} ({resp_email.data['user']['email']})")

    # 4. Login via UPPERCASE EMAIL (Case Insensitivity)
    print("\n--- 4. Testing Case-Insensitive Email Login ('DR.MITU@FRINGE.LOCAL') ---")
    resp_upper = client.post('/api/auth/login/', {
        'username': test_email.upper(),
        'password': test_password
    }, format='json')
    assert resp_upper.status_code == 200, f"Login via uppercase email failed: {resp_upper.data}"
    print(f"[OK] Success! Case-insensitive email authentication verified.")

    # 5. Login with invalid password
    print("\n--- 5. Testing Wrong Password Rejection ---")
    resp_bad = client.post('/api/auth/login/', {
        'username': test_email,
        'password': 'WrongPassword123'
    }, format='json')
    assert resp_bad.status_code == 401, f"Expected 401 for bad password, got {resp_bad.status_code}"
    print(f"[OK] Blocked correctly: {resp_bad.data['error']}")

    # 6. Verify Owner Privileges with token from email login
    print("\n--- 6. Verifying Owner Has Full Cross-Unit Access with Email-Auth Token ---")
    client.credentials(HTTP_AUTHORIZATION=f'Token {token_email}')
    users_resp = client.get('/api/core/users/')
    assert users_resp.status_code == 200, f"Owner failed to access staff management: {users_resp.data}"
    print(f"[OK] Owner authenticated via email can access staff management (/api/core/users/).")

    # Re-normalize default owner credentials for seamless development experience
    call_command(
        'create_owner',
        username='owner',
        email='mitu@fringebusiness.com',
        password='password123',
        pin='9999',
        first_name='Dr.',
        last_name='Mitu',
        non_interactive=True
    )
    print("\n[OK] Default owner restored: username='owner', email='mitu@fringebusiness.com', password='password123'")

    print("\n" + "=" * 70)
    print("ALL SCRIPT AND DUAL USERNAME/EMAIL AUTHENTICATION TESTS PASSED 100%!")
    print("=" * 70)

if __name__ == '__main__':
    test_owner_script_and_dual_login()
