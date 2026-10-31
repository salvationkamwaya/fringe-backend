import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from rest_framework.test import APIClient
from business_core.models import User, BusinessUnit, AuditLog
from business_catalog.models import Product
from business_pos.models import Order, Customer

def run_rbac_and_isolation_tests():
    print("=" * 70)
    print("STARTING RBAC AND TENANT DATA ISOLATION VERIFICATION")
    print("=" * 70)

    client = APIClient()

    # -------------------------------------------------------------
    # 1. OWNER PRIVILEGE & STAFF CREATION / ROLE ASSIGNMENT
    # -------------------------------------------------------------
    print("\n--- 1. Testing Owner Authentication ---")
    owner_login = client.post('/api/auth/login/', {'username': 'owner', 'password': 'password123'})
    assert owner_login.status_code == 200, f"Owner login failed: {owner_login.data}"
    owner_token = owner_login.data['token']
    client.credentials(HTTP_AUTHORIZATION=f'Token {owner_token}')
    print(f"Logged in as Owner: {owner_login.data['user']['username']} (Role: {owner_login.data['user']['role']})")

    print("\n--- 2. Owner Listing Users (/api/core/users/) ---")
    users_resp = client.get('/api/core/users/')
    assert users_resp.status_code == 200, f"Owner failed to list users: {users_resp.data}"
    user_list = users_resp.data['results'] if isinstance(users_resp.data, dict) and 'results' in users_resp.data else users_resp.data
    print(f"Owner successfully retrieved {len(user_list)} registered staff accounts.")

    print("\n--- 3. Owner Creating New Staff (Stock Clerk) ---")
    # Clean up test user if exists
    User.objects.filter(username='clerk_john').delete()
    create_payload = {
        'username': 'clerk_john',
        'first_name': 'John',
        'last_name': 'Banda',
        'email': 'john.banda@fringe.local',
        'role': 'stock_clerk',
        'business_unit': 'bar',
        'password': 'StrongPassword2026!',
        'pin': '5678'
    }
    create_resp = client.post('/api/core/users/', create_payload, format='json')
    assert create_resp.status_code == 201, f"Owner failed to create staff: {create_resp.data}"
    new_user_id = create_resp.data['id']
    print(f"Staff Created: {create_resp.data['username']} (ID: {new_user_id}, Role: {create_resp.data['role_display']})")
    assert create_resp.data['has_pin'] is True, "PIN hash was not saved properly"

    print("\n--- 4. Owner Reassigning Staff Role & Unit ---")
    update_payload = {
        'role': 'stationery_cashier',
        'business_unit': 'stationery'
    }
    update_resp = client.patch(f'/api/core/users/{new_user_id}/', update_payload, format='json')
    assert update_resp.status_code == 200, f"Owner failed to update role: {update_resp.data}"
    assert update_resp.data['role'] == 'stationery_cashier'
    assert update_resp.data['business_unit'] == 'stationery'
    print(f"Role Successfully Reassigned: clerk_john is now {update_resp.data['role_display']} in {update_resp.data['business_unit_name']}")

    print("\n--- 5. Owner Setting / Resetting Staff PIN ---")
    pin_resp = client.post(f'/api/core/users/{new_user_id}/set_pin/', {'pin': '4321'}, format='json')
    assert pin_resp.status_code == 200, f"Owner failed to set PIN: {pin_resp.data}"
    clerk = User.objects.get(id=new_user_id)
    assert clerk.check_pin('4321') is True, "New PIN does not match hash"
    print(f"Staff PIN successfully updated and verified via bcrypt hash.")

    print("\n--- 6. Owner Deactivating / Reactivating Staff Account ---")
    toggle_resp = client.post(f'/api/core/users/{new_user_id}/toggle_active/')
    assert toggle_resp.status_code == 200
    assert toggle_resp.data['is_active'] is False, "Staff should now be deactivated"
    print(f"Staff Deactivated: {toggle_resp.data['message']}")

    # Verify deactivated user cannot log in
    deactivated_client = APIClient()
    fail_login = deactivated_client.post('/api/auth/login/', {'username': 'clerk_john', 'password': 'StrongPassword2026!'})
    assert fail_login.status_code in [400, 401], f"Deactivated user should not log in: {fail_login.data}"
    print(f"Confirmed: Suspended staff cannot log in ({fail_login.data.get('error', fail_login.data)}).")

    # Reactivate
    reactivate_resp = client.post(f'/api/core/users/{new_user_id}/toggle_active/')
    assert reactivate_resp.status_code == 200
    assert reactivate_resp.data['is_active'] is True
    print(f"Staff Reactivated: {reactivate_resp.data['message']}")

    print("\n--- 7. Owner Safe Guard: Cannot Deactivate Own Account ---")
    owner_user = User.objects.get(username='owner')
    self_deact_resp = client.post(f'/api/core/users/{owner_user.id}/toggle_active/')
    assert self_deact_resp.status_code == 400, "Owner should not be allowed to deactivate self"
    print(f"Owner protection confirmed: {self_deact_resp.data['error']}")

    # -------------------------------------------------------------
    # 2. NON-OWNER RESTRICTION (RBAC DEFENSE)
    # -------------------------------------------------------------
    print("\n--- 8. Testing Bar Manager Attempting Staff Management (Must Be Blocked 403) ---")
    bm_client = APIClient()
    bm_login = bm_client.post('/api/auth/login/', {'username': 'bar_manager', 'password': 'password123'})
    assert bm_login.status_code == 200, "Bar manager login failed"
    bm_client.credentials(HTTP_AUTHORIZATION=f"Token {bm_login.data['token']}")

    # Bar Manager attempts to list staff
    bm_list_resp = bm_client.get('/api/core/users/')
    assert bm_list_resp.status_code == 403, f"Expected 403 for Bar Manager listing users, got {bm_list_resp.status_code}"
    print(f"Bar Manager List Staff: Blocked with HTTP 403 Forbidden.")

    # Bar Manager attempts to create staff
    bm_create_resp = bm_client.post('/api/core/users/', {
        'username': 'rogue_staff',
        'first_name': 'Rogue',
        'role': 'bartender',
        'business_unit': 'bar',
        'password': 'password123'
    })
    assert bm_create_resp.status_code == 403, f"Expected 403 for Bar Manager creating user, got {bm_create_resp.status_code}"
    print(f"Bar Manager Create Staff: Blocked with HTTP 403 Forbidden.")

    # Bar Manager attempts to modify user roles
    bm_patch_resp = bm_client.patch(f'/api/core/users/{new_user_id}/', {'role': 'bar_manager'})
    assert bm_patch_resp.status_code == 403, f"Expected 403 for Bar Manager modifying role, got {bm_patch_resp.status_code}"
    print(f"Bar Manager Reassign Role: Blocked with HTTP 403 Forbidden.")

    print("\n--- 9. Testing Stationery Manager & Cashier Role Defense ---")
    sm_client = APIClient()
    sm_login = sm_client.post('/api/auth/login/', {'username': 'stationery_manager', 'password': 'password123'})
    assert sm_login.status_code == 200, "Stationery manager login failed"
    sm_client.credentials(HTTP_AUTHORIZATION=f"Token {sm_login.data['token']}")

    sm_create_resp = sm_client.post('/api/core/users/', {'username': 'stat_staff_x', 'password': 'password123'})
    assert sm_create_resp.status_code == 403, f"Expected 403 for Stationery Manager, got {sm_create_resp.status_code}"
    print(f"Stationery Manager Create Staff: Blocked with HTTP 403 Forbidden.")

    bt_client = APIClient()
    bt_login = bt_client.post('/api/auth/login/', {'username': 'bartender', 'password': 'password123'})
    assert bt_login.status_code == 200
    bt_client.credentials(HTTP_AUTHORIZATION=f"Token {bt_login.data['token']}")

    bt_create_resp = bt_client.post('/api/core/users/', {'username': 'bt_friend', 'password': 'password123'})
    assert bt_create_resp.status_code == 403, f"Expected 403 for Bartender, got {bt_create_resp.status_code}"
    print(f"Bartender / Cashier Create Staff: Blocked with HTTP 403 Forbidden.")

    # -------------------------------------------------------------
    # 3. STRICT TENANT DATA ISOLATION (ZERO CROSS-UNIT MIXING)
    # -------------------------------------------------------------
    print("\n--- 10. Bar Staff Attempting to Access Stationery Data (Must 404) ---")
    stat_product = Product.objects.filter(business_unit_id='stationery').first()
    assert stat_product is not None, "Stationery product required for test"

    # Bartender tries to view Stationery Product
    bar_to_stat_prod = bt_client.get(f'/api/catalog/products/{stat_product.id}/')
    assert bar_to_stat_prod.status_code == 404, f"Bar staff should get 404 on Stationery Product, got {bar_to_stat_prod.status_code}"
    print(f"Data Isolation Confirmed: Bar staff GET Stationery Product -> HTTP 404 Not Found.")

    # Bartender tries to view Stationery Customers / Credit Accounts
    stat_customer = Customer.objects.filter(business_unit_id='stationery').first()
    if stat_customer:
        bar_to_stat_cust = bt_client.get(f'/api/pos/customers/{stat_customer.id}/')
        assert bar_to_stat_cust.status_code == 404, f"Bar staff should get 404 on Stationery Customer, got {bar_to_stat_cust.status_code}"
        print(f"Data Isolation Confirmed: Bar staff GET Stationery Customer -> HTTP 404 Not Found.")

    # Bartender tries to access Stationery End of Day report
    bar_to_stat_eod = bt_client.get('/api/pos/reports/stationery_eod/')
    assert bar_to_stat_eod.status_code in [403, 404], f"Bar staff should not access Stationery EOD report, got {bar_to_stat_eod.status_code}"
    print(f"Data Isolation Confirmed: Bar staff GET Stationery EOD Report -> Denied ({bar_to_stat_eod.status_code}).")

    print("\n--- 11. Stationery Staff Attempting to Access Bar Data (Must 404) ---")
    bar_product = Product.objects.filter(business_unit_id='bar').first()
    assert bar_product is not None, "Bar product required for test"

    sc_client = APIClient()
    sc_login = sc_client.post('/api/auth/login/', {'username': 'stationery_cashier', 'password': 'password123'})
    assert sc_login.status_code == 200
    sc_client.credentials(HTTP_AUTHORIZATION=f"Token {sc_login.data['token']}")

    # Stationery Cashier tries to view Bar Product
    stat_to_bar_prod = sc_client.get(f'/api/catalog/products/{bar_product.id}/')
    assert stat_to_bar_prod.status_code == 404, f"Stationery staff should get 404 on Bar Product, got {stat_to_bar_prod.status_code}"
    print(f"Data Isolation Confirmed: Stationery staff GET Bar Product -> HTTP 404 Not Found.")

    # Stationery Cashier tries to access Bar Orders
    bar_order = Order.objects.filter(business_unit_id='bar').first()
    if bar_order:
        stat_to_bar_ord = sc_client.get(f'/api/pos/orders/{bar_order.id}/')
        assert stat_to_bar_ord.status_code == 404, f"Stationery staff should get 404 on Bar Order, got {stat_to_bar_ord.status_code}"
        print(f"Data Isolation Confirmed: Stationery staff GET Bar Order -> HTTP 404 Not Found.")

    # Stationery Cashier tries to access Bar Tabs
    stat_to_bar_tabs = sc_client.get('/api/pos/orders/active_tabs/')
    assert stat_to_bar_tabs.status_code in [403, 404] or len(stat_to_bar_tabs.data) == 0, f"Stationery staff saw Bar Tabs: {stat_to_bar_tabs.data}"
    print(f"Data Isolation Confirmed: Stationery staff cannot view Bar Tabs.")

    # -------------------------------------------------------------
    # 4. AUDIT TRAIL LOGGING OF STAFF ACTIONS
    # -------------------------------------------------------------
    print("\n--- 12. Verifying Immutable Audit Trail ---")
    latest_audits = AuditLog.objects.filter(action__in=['STAFF_CREATED', 'STAFF_ROLE_ASSIGNED', 'STAFF_PIN_SET']).order_by('-timestamp')[:3]
    assert latest_audits.count() >= 3, f"Expected audit records for staff management, found {latest_audits.count()}"
    for log in latest_audits:
        print(f"Audit Log Recorded: [{log.timestamp.strftime('%H:%M:%S')}] Action: {log.action} | User: {log.username} | Entity: {log.entity_type} #{log.entity_id}")

    # Cleanup test user
    User.objects.filter(username='clerk_john').delete()

    print("\n" + "=" * 70)
    print("ALL RBAC AND DATA ISOLATION TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == '__main__':
    run_rbac_and_isolation_tests()
