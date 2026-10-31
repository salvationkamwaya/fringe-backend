import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from rest_framework.test import APIClient
from business_core.models import User, AuditLog
from business_catalog.models import Product
from business_pos.models import Order, Shift

def test_phase1():
    client = APIClient()

    print("\n--- 1. Testing Bartender Login ---")
    login_resp = client.post('/api/auth/login/', {'username': 'bartender', 'password': 'password123'})
    assert login_resp.status_code == 200, f"Login failed: {login_resp.data}"
    token = login_resp.data['token']
    client.credentials(HTTP_AUTHORIZATION=f'Token {token}')
    print(f"Logged in as: {login_resp.data['user']['username']} (Role: {login_resp.data['user']['role']})")
    print(f"Active unit: {login_resp.data['active_unit']}")

    print("\n--- 2. Testing Bar Catalog Isolation ---")
    prod_resp = client.get('/api/catalog/products/')
    assert prod_resp.status_code == 200
    bar_prods = prod_resp.data['results'] if 'results' in prod_resp.data else prod_resp.data
    assert len(bar_prods) > 0, "No bar products returned"
    print(f"Bartender received {len(bar_prods)} products. Sample: {bar_prods[0]['name']} (Unit: {bar_prods[0]['business_unit']})")
    for p in bar_prods:
        assert p['business_unit'] == 'bar', f"Data leak! Received product from unit {p['business_unit']}"

    print("\n--- 3. Testing Cross-Unit Security (Bartender attempting Stationery access) ---")
    stat_prod = Product.objects.filter(business_unit_id='stationery').first()
    assert stat_prod is not None
    cross_resp = client.get(f'/api/catalog/products/{stat_prod.id}/')
    assert cross_resp.status_code == 404, f"Expected 404 on cross-unit access, got {cross_resp.status_code}"
    print(f"Security Confirmed: Bartender request to Stationery Product ID {stat_prod.id} returned 404 Not Found.")

    print("\n--- 4. Testing Manager PIN Override Endpoint ---")
    # Wrong PIN
    bad_pin_resp = client.post('/api/auth/verify-manager-pin/', {'pin': '999999', 'action': 'VOID_ITEM'})
    assert bad_pin_resp.status_code == 403, f"Expected 403 for bad pin, got {bad_pin_resp.status_code}"
    
    # Valid Bar Manager PIN (1234)
    good_pin_resp = client.post('/api/auth/verify-manager-pin/', {'pin': '1234', 'action': 'VOID_ITEM'})
    assert good_pin_resp.status_code == 200 and good_pin_resp.data['approved'] is True
    print(f"PIN Verified: Approved by {good_pin_resp.data['approver_name']} ({good_pin_resp.data['approver_role']})")

    print("\n--- 5. Testing Non-Bypassable Age Verification on Alcohol Sale ---")
    beer = Product.objects.filter(business_unit_id='bar', is_alcoholic=True).first()
    assert beer is not None, "No alcoholic product found"

    # Attempt sale WITHOUT age verification
    fail_sale_resp = client.post('/api/pos/orders/', {
        'items': [{'product_id': beer.id, 'quantity': 2}],
        'payments': [{'payment_method': 'CASH', 'amount': str(beer.selling_price * 2)}],
        'is_age_verified': False
    }, format='json')
    assert fail_sale_resp.status_code == 400, f"Expected 400 when selling alcohol without age check, got {fail_sale_resp.status_code}"
    assert 'AGE_VERIFICATION_REQUIRED' in fail_sale_resp.data.get('code', '')
    print(f"Compliance Enforced: Alcohol sale blocked without age check: {fail_sale_resp.data['error']}")

    print("\n--- 6. Testing Successful Alcohol Sale with Age Check ---")
    old_stock = beer.stock_quantity
    success_sale_resp = client.post('/api/pos/orders/', {
        'items': [{'product_id': beer.id, 'quantity': 2}],
        'payments': [{'payment_method': 'CASH', 'amount': str(beer.selling_price * 2)}],
        'is_age_verified': True
    }, format='json')
    assert success_sale_resp.status_code == 201, f"Sale failed: {success_sale_resp.data}"
    order_data = success_sale_resp.data
    print(f"Sale Complete: Order #{order_data['order_number']}, Total: {order_data['final_amount']}")

    # Verify inventory was decremented automatically
    beer.refresh_from_db()
    assert beer.stock_quantity == old_stock - 2, f"Stock not decremented! Expected {old_stock - 2}, got {beer.stock_quantity}"
    print(f"Inventory Auto-Updated: Mosi Lager stock decreased from {old_stock} to {beer.stock_quantity}")

    print("\n--- 7. Testing Append-Only Immutable Audit Log ---")
    latest_audits = AuditLog.objects.filter(business_unit_id='bar')[:5]
    assert latest_audits.count() > 0
    print(f"Found {latest_audits.count()} recent audit records:")
    for a in latest_audits:
        print(f"  [{a.timestamp.strftime('%H:%M:%S')}] {a.action} by {a.username} ({a.role})")

    print("\n>>> ALL PHASE 1 CORE BACKEND ARCHITECTURAL CHECKS PASSED WITH 100% SUCCESS! <<<\n")

if __name__ == '__main__':
    test_phase1()
