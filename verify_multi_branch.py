import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient
from business_core.models import Branch, User, BusinessUnit, AuditLog
from business_catalog.models import Category, Product
from business_pos.models import Shift, Order, OrderItem, Payment, BarTab

def run_tests():
    print("=" * 70)
    print("STARTING MULTI-BRANCH VERIFICATION TESTS")
    print("=" * 70)

    # 1. Ensure Bar Business Unit exists
    bar_unit, _ = BusinessUnit.objects.get_or_create(
        id='bar',
        defaults={'name': 'Bar & Lounge', 'code': 'BAR', 'currency': 'TSH'}
    )

    # 2. Setup or retrieve Business Owner
    owner, _ = User.objects.get_or_create(
        username='owner_test_multi',
        defaults={
            'role': User.ROLE_OWNER,
            'is_active': True,
            'email': 'owner_multi@fringe.local'
        }
    )
    owner.set_password('OwnerStrong#2026')
    owner.save()

    client = APIClient()
    client.force_authenticate(user=owner)

    # TEST A: Dynamic Branch Creation by Owner (No hardcoded branches)
    print("\n[TEST A] Dynamic Branch Creation by Business Owner...")
    branch_a_code = f"OYB{int(timezone.now().timestamp()) % 1000}"
    branch_b_code = f"CCB{int(timezone.now().timestamp()) % 1000}"

    res_a = client.post('/api/core/branches/', {
        'name': 'Oysterbay Luxury Lounge',
        'code': branch_a_code,
        'address': 'Plot 42 Haile Selassie Rd, Masaki',
        'phone': '+255 712 000 001',
        'business_unit': 'bar',
        'is_active': True
    }, format='json')
    assert res_a.status_code == 201, f"Failed to create Branch A: {res_a.data}"
    branch_a_id = res_a.data['id']
    print(f"  ✓ Branch A created dynamically: {res_a.data['name']} (ID: {branch_a_id}, Code: {branch_a_code})")

    res_b = client.post('/api/core/branches/', {
        'name': 'City Center Lounge',
        'code': branch_b_code,
        'address': 'Samora Avenue, CBD',
        'phone': '+255 712 000 002',
        'business_unit': 'bar',
        'is_active': True
    }, format='json')
    assert res_b.status_code == 201, f"Failed to create Branch B: {res_b.data}"
    branch_b_id = res_b.data['id']
    print(f"  ✓ Branch B created dynamically: {res_b.data['name']} (ID: {branch_b_id}, Code: {branch_b_code})")

    # TEST B: Completely Separate Drink Catalogs per Branch
    print("\n[TEST B] Completely Separate Drink Catalogs per Branch...")
    # Create category and drink in Branch A
    cat_a = Category.objects.create(
        business_unit=bar_unit,
        branch_id=branch_a_id,
        name='Lagres OYB',
        slug=f'lagers-oyb-{branch_a_id}'
    )
    drink_a = Product.objects.create(
        business_unit=bar_unit,
        branch_id=branch_a_id,
        category=cat_a,
        name='Serengeti Premium Lager OYB',
        sku=f'SKU-OYB-{branch_a_id}',
        selling_price=Decimal('4500.00'),
        cost_price=Decimal('3000.00'),
        stock_quantity=Decimal('100'),
        is_alcoholic=True
    )

    # Create category and drink in Branch B
    cat_b = Category.objects.create(
        business_unit=bar_unit,
        branch_id=branch_b_id,
        name='Lagres CCB',
        slug=f'lagers-ccb-{branch_b_id}'
    )
    drink_b = Product.objects.create(
        business_unit=bar_unit,
        branch_id=branch_b_id,
        category=cat_b,
        name='Kilimanjaro Lager CCB',
        sku=f'SKU-CCB-{branch_b_id}',
        selling_price=Decimal('5000.00'),
        cost_price=Decimal('3200.00'),
        stock_quantity=Decimal('80'),
        is_alcoholic=True
    )

    # Query catalog with X-Branch-Id: branch_a_id
    client.defaults['HTTP_X_BRANCH_ID'] = str(branch_a_id)
    res_cat_a = client.get('/api/catalog/products/')
    assert res_cat_a.status_code == 200
    prods_in_a = [p['id'] for p in res_cat_a.data]
    assert drink_a.id in prods_in_a, "Branch A drink missing in Branch A catalog"
    assert drink_b.id not in prods_in_a, "Branch B drink leaked into Branch A catalog!"
    print(f"  ✓ Branch A catalog contains drink '{drink_a.name}' and zero drinks from Branch B.")

    # Query catalog with X-Branch-Id: branch_b_id
    client.defaults['HTTP_X_BRANCH_ID'] = str(branch_b_id)
    res_cat_b = client.get('/api/catalog/products/')
    assert res_cat_b.status_code == 200
    prods_in_b = [p['id'] for p in res_cat_b.data]
    assert drink_b.id in prods_in_b, "Branch B drink missing in Branch B catalog"
    assert drink_a.id not in prods_in_b, "Branch A drink leaked into Branch B catalog!"
    print(f"  ✓ Branch B catalog contains drink '{drink_b.name}' and zero drinks from Branch A.")

    # TEST C: Permanent Staff Assignment & Non-Roaming Isolation
    print("\n[TEST C] Permanent Staff Assignment & Anti-Roaming Tenant Isolation...")
    bartender_a, _ = User.objects.get_or_create(
        username=f'bartender_oyb_{branch_a_id}',
        defaults={
            'role': User.ROLE_BARTENDER,
            'business_unit': bar_unit,
            'branch_id': branch_a_id,
            'is_active': True,
            'first_name': 'Baraka',
            'last_name': 'OYB'
        }
    )
    bartender_a.set_password('StaffStrong#2026')
    bartender_a.save()

    bartender_b, _ = User.objects.get_or_create(
        username=f'bartender_ccb_{branch_b_id}',
        defaults={
            'role': User.ROLE_BARTENDER,
            'business_unit': bar_unit,
            'branch_id': branch_b_id,
            'is_active': True,
            'first_name': 'Neema',
            'last_name': 'CCB'
        }
    )
    bartender_b.set_password('StaffStrong#2026')
    bartender_b.save()

    # Authenticate as Bartender A
    b_client = APIClient()
    b_client.force_authenticate(user=bartender_a)

    # 1. Bartender A accessing own branch products succeeds
    res = b_client.get('/api/catalog/products/')
    assert res.status_code == 200
    assert len(res.data) == 1 and res.data[0]['id'] == drink_a.id
    print("  ✓ Bartender A cleanly restricted to Branch A drink catalog.")

    # 2. Bartender A attempts cross-branch spoofing (X-Branch-Id: branch_b_id) -> MUST 404
    b_client.defaults['HTTP_X_BRANCH_ID'] = str(branch_b_id)
    res_spoof = b_client.get('/api/catalog/products/')
    assert res_spoof.status_code == 404, f"Security Breach: Cross-branch access returned {res_spoof.status_code} instead of 404"
    print("  ✓ Security Guard Active: Bartender A cross-branch spoofing blocked with 404 Not Found.")

    # TEST D: Orders & Shift Branch Code Prefix Enforcement
    print("\n[TEST D] Orders Prefix & Branch Isolation...")
    # Clear spoof header
    b_client.defaults.pop('HTTP_X_BRANCH_ID', None)

    # Bartender A starts a shift
    res_shift = b_client.post('/api/pos/shifts/start_shift/', {'opening_float': '50000.00'})
    assert res_shift.status_code == 201, f"Failed to start shift: {res_shift.data}"
    shift_a_id = res_shift.data['id']
    assert res_shift.data['branch'] == branch_a_id

    # Bartender A places an order
    res_order = b_client.post('/api/pos/orders/', {
        'items': [{'product_id': drink_a.id, 'quantity': 2, 'is_pack_sale': True}],
        'payments': [{'payment_method': 'CASH', 'amount': '10000.00'}],
        'shift_id': shift_a_id,
        'is_age_verified': True
    }, format='json')
    assert res_order.status_code == 201, f"Failed to create order: {res_order.data}"
    order_a = res_order.data
    assert order_a['order_number'].startswith(branch_a_code), f"Order number '{order_a['order_number']}' does not start with branch prefix '{branch_a_code}'"
    print(f"  ✓ Order created successfully with Branch Prefix: {order_a['order_number']}")

    # TEST E: Dynamic Staff Reassignment by Business Owner
    print("\n[TEST E] Dynamic Staff Reassignment by Owner...")
    client.defaults.pop('HTTP_X_BRANCH_ID', None)
    res_reassign = client.patch(f'/api/core/users/{bartender_a.id}/', {
        'branch': branch_b_id
    }, format='json')
    assert res_reassign.status_code == 200, f"Failed to reassign: {res_reassign.data}"
    bartender_a.refresh_from_db()
    assert bartender_a.branch_id == branch_b_id, "Bartender branch was not updated"
    print(f"  ✓ Owner dynamically moved Bartender A to Branch B ({res_reassign.data['branch_name']}).")

    # Verify audit log recorded STAFF_BRANCH_REASSIGNED
    audit = AuditLog.objects.filter(action='STAFF_BRANCH_REASSIGNED', entity_id=bartender_a.id).last()
    assert audit is not None, "Audit log missing for branch reassignment!"
    print(f"  ✓ Immutable Audit Log entry recorded: {audit.action} (User: {audit.username})")

    # TEST F: Owner Branch Performance Reporting Breakdown
    print("\n[TEST F] Owner Multi-Branch Reporting Breakdown...")
    client.defaults['HTTP_X_BRANCH_ID'] = 'all'
    res_rep = client.get('/api/pos/reports/bar/')
    assert res_rep.status_code == 200
    assert 'branch_breakdown' in res_rep.data, "branch_breakdown missing from owner report"
    breakdown = res_rep.data['branch_breakdown']
    assert len(breakdown) >= 2, f"Expected at least 2 branches in breakdown, got {len(breakdown)}"
    print(f"  ✓ Owner Consolidated Bar Report returned breakdown for {len(breakdown)} branches.")

    print("\n" + "=" * 70)
    print("ALL MULTI-BRANCH VERIFICATION TESTS PASSED SUCCESSFULLY! (100% GREEN)")
    print("=" * 70)

if __name__ == '__main__':
    run_tests()
