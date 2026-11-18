"""
Phase 2 Comprehensive Automated API Test Suite
Stationery Cashier & Stationery Manager Workflows
- Authentication (stationery_cashier Grace, stationery_manager Sarah)
- Pack vs. Unit selling and fractional stock deduction
- Customer credit registration, credit limits, manager PIN override
- Debt repayment, customer statements, and drawer reconciliation
- Debtors Aging Report (0-30, 31-60, 61-90, 90+ days)
- Supplier payables & Purchase Orders
- Cross-unit isolation checks (Bar vs Stationery)
"""
import sys
import os
import uuid
from decimal import Decimal

import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from rest_framework.test import APIClient
from business_core.models import User, BusinessUnit, AuditLog
from business_catalog.models import Product, Category
from business_pos.models import (
    Shift, Customer, Order, OrderItem, Payment,
    CustomerCreditTransaction, Supplier, PurchaseOrder, SupplierPayment
)


def run_tests():
    print("=" * 60)
    print("STARTING PHASE 2 AUTOMATED TEST SUITE: STATIONERY & CREDIT")
    print("=" * 60)

    client = APIClient()

    # 1. AUTHENTICATE GRACE (STATIONERY CASHIER) & SARAH (STATIONERY MANAGER)
    print("\n[TEST 1] Logging in stationery_cashier and stationery_manager...")
    res_grace = client.post('/api/auth/login/', {'username': 'stationery_cashier', 'password': 'password123'}, format='json')
    assert res_grace.status_code == 200, f"Grace login failed: {res_grace.data}"
    grace_token = res_grace.data['token']
    print(f" [OK] Grace logged in. Role: {res_grace.data['user']['role']}, Unit: {res_grace.data['active_unit']}")

    res_sarah = client.post('/api/auth/login/', {'username': 'stationery_manager', 'password': 'password123'}, format='json')
    assert res_sarah.status_code == 200, f"Sarah login failed: {res_sarah.data}"
    sarah_token = res_sarah.data['token']
    print(f" [OK] Sarah logged in. Role: {res_sarah.data['user']['role']}, Unit: {res_sarah.data['active_unit']}")

    # Setup client as Grace
    client.credentials(HTTP_AUTHORIZATION=f'Token {grace_token}')
    client.defaults['HTTP_X_BUSINESS_UNIT'] = 'stationery'

    # 2. START A SHIFT FOR GRACE
    print("\n[TEST 2] Starting shift for Grace with opening float TSH 250.00...")
    res_shift = client.post('/api/pos/shifts/start_shift/', {'opening_float': '250.00'}, format='json')
    if res_shift.status_code == 400 and 'active open shift' in str(res_shift.data.get('error', '')):
        shift_data = res_shift.data['shift']
    else:
        assert res_shift.status_code in (200, 201), f"Shift start failed: {res_shift.data}"
        shift_data = res_shift.data.get('shift', res_shift.data)
    shift_id = shift_data['id']
    print(f" [OK] Shift #{shift_id} started/active. Opening Float: TSH {shift_data['opening_float']}")

    # 3. UNIT VS. PACK PRICING & FRACTIONAL STOCK DEDUCTION
    print("\n[TEST 3] Testing Unit vs. Pack pricing and fractional stock deduction...")
    # Find a pack product, e.g. "Ballpoint Pens (Box of 50)"
    pen_product = Product.objects.filter(business_unit_id='stationery', is_pack=True, allow_split_sale=True).first()
    assert pen_product is not None, "Pack product with split sale not found!"
    initial_stock = pen_product.stock_quantity
    ratio = Decimal(pen_product.pack_unit_ratio)
    pack_price = pen_product.selling_price
    unit_price = pen_product.split_unit_price or (pen_product.selling_price / Decimal(pen_product.pack_unit_ratio))
    print(f" Product: '{pen_product.name}' | Pack Price: TSH {pack_price} | Unit Price: TSH {unit_price} | Ratio: {ratio} | Stock: {initial_stock}")

    # 3a. Sell 2 individual pens (units)
    order_data_unit = {
        'order_type': 'DIRECT',
        'items': [
            {
                'product_id': pen_product.id,
                'quantity': 2,
                'is_pack_sale': False
            }
        ],
        'payments': [
            {
                'payment_method': 'CASH',
                'amount': str(unit_price * 2),
                'amount_tendered': '50.00'
            }
        ]
    }
    res_order_unit = client.post('/api/pos/orders/', order_data_unit, format='json')
    assert res_order_unit.status_code == 201, f"Unit order failed: {res_order_unit.data}"
    expected_unit_total = unit_price * 2
    assert Decimal(res_order_unit.data['final_amount']) == expected_unit_total, f"Expected {expected_unit_total}, got {res_order_unit.data['final_amount']}"
    
    # Verify stock deduction: 2 units = 2 / 50 = 0.04 boxes deducted
    pen_product.refresh_from_db()
    expected_stock_after_unit = initial_stock - (Decimal('2') / ratio)
    assert pen_product.stock_quantity == expected_stock_after_unit, f"Stock after unit sale mismatch: expected {expected_stock_after_unit}, got {pen_product.stock_quantity}"
    print(f" [OK] Unit sale: 2 units @ TSH {unit_price} = TSH {expected_unit_total}. Stock correctly reduced by 2/{ratio} -> {pen_product.stock_quantity}")

    # 3b. Sell 1 whole pack (box)
    order_data_pack = {
        'order_type': 'DIRECT',
        'items': [
            {
                'product_id': pen_product.id,
                'quantity': 1,
                'is_pack_sale': True
            }
        ],
        'payments': [
            {
                'payment_method': 'CASH',
                'amount': str(pack_price),
                'amount_tendered': str(pack_price)
            }
        ]
    }
    res_order_pack = client.post('/api/pos/orders/', order_data_pack, format='json')
    assert res_order_pack.status_code == 201, f"Pack order failed: {res_order_pack.data}"
    pen_product.refresh_from_db()
    expected_stock_after_pack = expected_stock_after_unit - Decimal('1.0')
    assert pen_product.stock_quantity == expected_stock_after_pack, f"Stock after pack sale mismatch: expected {expected_stock_after_pack}, got {pen_product.stock_quantity}"
    print(f" [OK] Pack sale: 1 pack @ TSH {pack_price}. Stock correctly reduced by 1.0 -> {pen_product.stock_quantity}")

    # 4. CUSTOMER REGISTRATION & CREDIT LIMITS
    print("\n[TEST 4] Testing Customer Registration & Credit Limits...")
    cust_name = f"Apex Academy {uuid.uuid4().hex[:4].upper()}"
    res_cust = client.post('/api/pos/customers/', {
        'name': cust_name,
        'contact_person': 'Principal Banda',
        'phone': '0977889900',
        'email': 'principal@apex.edu.zm',
        'credit_limit': str(pack_price * 5),
        'notes': 'School account - 30 days credit'
    }, format='json')
    assert res_cust.status_code == 201, f"Customer registration failed: {res_cust.data}"
    customer_id = res_cust.data['id']
    print(f" [OK] Customer registered: '{res_cust.data['name']}' | Credit Limit: TSH {res_cust.data['credit_limit']}")

    # 4a. Credit sale within limit
    res_credit_sale_1 = client.post('/api/pos/orders/', {
        'customer_id': customer_id,
        'order_type': 'DIRECT',
        'items': [
            {'product_id': pen_product.id, 'quantity': 2, 'is_pack_sale': True} # 2 * pack_price
        ],
        'payments': [
            {'payment_method': 'CREDIT', 'amount': str(pack_price * 2)}
        ]
    }, format='json')
    assert res_credit_sale_1.status_code == 201, f"Credit sale 1 failed: {res_credit_sale_1.data}"
    cust = Customer.objects.get(id=customer_id)
    expected_debt_1 = pack_price * 2
    assert cust.current_balance == expected_debt_1, f"Customer balance mismatch: expected {expected_debt_1}, got {cust.current_balance}"
    print(f" [OK] Credit sale within limit: TSH {expected_debt_1} charged. Customer balance: TSH {cust.current_balance}, Available: TSH {cust.available_credit}")

    # 4b. Credit sale exceeding credit limit WITHOUT manager PIN -> MUST BE BLOCKED
    excess_qty = 5 # 5 * pack_price (750,000) exceeds available credit (450,000) within stock (18+)
    res_credit_blocked = client.post('/api/pos/orders/', {
        'customer_id': customer_id,
        'order_type': 'DIRECT',
        'items': [
            {'product_id': pen_product.id, 'quantity': excess_qty, 'is_pack_sale': True}
        ],
        'payments': [
            {'payment_method': 'CREDIT', 'amount': str(pack_price * excess_qty)}
        ]
    }, format='json')
    assert res_credit_blocked.status_code == 403, f"Expected 403 when credit exceeded, got {res_credit_blocked.status_code}: {res_credit_blocked.data}"
    print(f" [OK] Blocked: Exceeding credit limit without manager PIN correctly rejected (403): {res_credit_blocked.data['error']}")

    # 4c. Credit sale exceeding credit limit WITH manager PIN (Sarah PIN 5678) -> ALLOWED
    res_credit_approved = client.post('/api/pos/orders/', {
        'customer_id': customer_id,
        'order_type': 'DIRECT',
        'manager_pin': '5678',
        'items': [
            {'product_id': pen_product.id, 'quantity': excess_qty, 'is_pack_sale': True}
        ],
        'payments': [
            {'payment_method': 'CREDIT', 'amount': str(pack_price * excess_qty)}
        ]
    }, format='json')
    assert res_credit_approved.status_code == 201, f"Credit override failed: {res_credit_approved.data}"
    cust.refresh_from_db()
    total_debt_expected = expected_debt_1 + (pack_price * excess_qty)
    assert cust.current_balance == total_debt_expected, f"Balance after override mismatch: expected {total_debt_expected}, got {cust.current_balance}"
    print(f" [OK] Approved: Manager PIN 5678 authorized credit override. New balance: TSH {cust.current_balance}")

    # 5. CUSTOMER DEBT REPAYMENT & DRAWER RECONCILIATION
    print("\n[TEST 5] Testing Debt Repayment & Drawer Update...")
    payment_amt = Decimal('300.00')
    shift_before = Shift.objects.get(id=shift_id)
    recovered_before = shift_before.total_debt_recovered

    res_repay = client.post(f'/api/pos/customers/{customer_id}/record_payment/', {
        'amount': str(payment_amt),
        'payment_method': 'CASH',
        'reference': 'RCPT-0041',
        'notes': 'Part payment by Principal Banda'
    }, format='json')
    assert res_repay.status_code == 200, f"Repayment failed: {res_repay.data}"
    cust.refresh_from_db()
    assert cust.current_balance == total_debt_expected - payment_amt
    print(f" [OK] Repaid TSH {payment_amt}. Customer balance now: TSH {cust.current_balance}")

    # Check shift total_debt_recovered
    shift = Shift.objects.get(id=shift_id)
    assert shift.total_debt_recovered == recovered_before + payment_amt, f"Shift debt recovered mismatch: {shift.total_debt_recovered}"
    print(f" [OK] Shift #{shift_id} total_debt_recovered accurately updated by TSH {payment_amt} -> TSH {shift.total_debt_recovered}")

    # Check customer statement
    res_stmt = client.get(f'/api/pos/customers/{customer_id}/statement/')
    assert res_stmt.status_code == 200
    tx_count = len(res_stmt.data['transactions'])
    assert tx_count == 3, f"Expected 3 transactions (2 sales + 1 repayment), got {tx_count}"
    print(f" [OK] Customer Statement verified: {tx_count} transactions recorded.")

    # 6. DEBTORS AGING REPORT
    print("\n[TEST 6] Testing Debtors Aging Report...")
    # Switch client to Sarah (Stationery Manager)
    client.credentials(HTTP_AUTHORIZATION=f'Token {sarah_token}')
    res_aging = client.get('/api/pos/debtors-aging/')
    assert res_aging.status_code == 200, f"Aging report failed: {res_aging.data}"
    assert len(res_aging.data['debtors']) >= 1
    summary = res_aging.data['summary']
    print(f" [OK] Aging report generated. Total Current (0-30 days): TSH {summary['total_current']} | Grand Total: TSH {summary['grand_total_debt']}")

    # 7. SUPPLIERS & PURCHASE ORDERS (PAYABLES)
    print("\n[TEST 7] Testing Suppliers & Purchase Orders...")
    res_sup = client.post('/api/pos/suppliers/', {
        'name': 'Lusaka Office Supplies Ltd',
        'contact_person': 'Mrs. Chanda',
        'phone': '0966112233',
        'email': 'sales@lusakaoffice.zm'
    }, format='json')
    assert res_sup.status_code == 201, f"Supplier create failed: {res_sup.data}"
    supplier_id = res_sup.data['id']
    print(f" [OK] Supplier registered: '{res_sup.data['name']}'")

    # Create Purchase Order
    po_cost = Decimal('2500.00')
    res_po = client.post('/api/pos/purchase-orders/', {
        'supplier': supplier_id,
        'invoice_number': 'INV-99882',
        'total_cost': str(po_cost),
        'notes': 'Batch order for reams and pens'
    }, format='json')
    assert res_po.status_code == 201, f"PO create failed: {res_po.data}"
    po_id = res_po.data['id']
    
    # Check supplier balance payable
    sup = Supplier.objects.get(id=supplier_id)
    assert sup.balance_payable == po_cost, f"Supplier balance mismatch: {sup.balance_payable}"
    print(f" [OK] Purchase Order #{res_po.data['order_number']} created. Supplier payable: TSH {sup.balance_payable}")

    # Make partial payment to supplier
    pay_supplier_amt = Decimal('1000.00')
    res_pay_sup = client.post(f'/api/pos/purchase-orders/{po_id}/record_payment/', {
        'amount': str(pay_supplier_amt),
        'payment_method': 'BANK_TRANSFER',
        'reference': 'EFT-7721'
    }, format='json')
    assert res_pay_sup.status_code == 200, f"PO payment failed: {res_pay_sup.data}"
    sup.refresh_from_db()
    assert sup.balance_payable == po_cost - pay_supplier_amt
    po = PurchaseOrder.objects.get(id=po_id)
    assert po.status == 'PARTIALLY_PAID'
    print(f" [OK] Paid TSH {pay_supplier_amt} to supplier. Supplier payable remaining: TSH {sup.balance_payable}, PO status: {po.status}")

    # 8. STATIONERY REPORTS (END-OF-DAY)
    print("\n[TEST 8] Testing Stationery End-of-Day Report...")
    res_stat_rep = client.get('/api/pos/reports/stationery/')
    assert res_stat_rep.status_code == 200, f"Stationery report failed: {res_stat_rep.data}"
    eod = res_stat_rep.data['end_of_day']
    print(f" [OK] End-of-Day Summary:")
    print(f"      Total Revenue: TSH {eod['total_revenue']}")
    print(f"      Cash Sales: TSH {eod['cash_sales']}")
    print(f"      Credit Sales: TSH {eod['credit_sales']}")
    print(f"      Debt Recovered: TSH {eod['debt_recovered']}")
    print(f"      Expected Cash Drawer: TSH {eod['expected_cash_drawer']}")
    print(f"      Total Debtors Balance: TSH {eod['total_debtors_balance']}")

    # 9. STRICT CROSS-UNIT ISOLATION
    print("\n[TEST 9] Verifying strict cross-unit isolation...")
    # Grace (Stationery) should not be able to list Bar tabs
    client.credentials(HTTP_AUTHORIZATION=f'Token {grace_token}')
    client.defaults['HTTP_X_BUSINESS_UNIT'] = 'stationery'
    res_bar_tabs = client.get('/api/pos/tabs/')
    assert len(res_bar_tabs.data) == 0, f"Stationery cashier should see 0 bar tabs, saw {len(res_bar_tabs.data)}"
    print(f" [OK] Isolation verified: Stationery cashier cannot see Bar tabs (returned 0).")

    # Grace cannot access bar reports (must return 404)
    res_bar_rep = client.get('/api/pos/reports/bar/')
    assert res_bar_rep.status_code == 404, f"Stationery staff should get 404 on Bar reports, got {res_bar_rep.status_code}"
    print(f" [OK] Isolation verified: Stationery cashier accessing Bar reports gets HTTP 404.")

    # Bar cashier bartender should not be able to access customer list
    res_bartender = client.post('/api/auth/login/', {'username': 'bartender', 'password': 'password123'}, format='json')
    bartender_token = res_bartender.data['token']
    client.credentials(HTTP_AUTHORIZATION=f'Token {bartender_token}')
    client.defaults['HTTP_X_BUSINESS_UNIT'] = 'bar'
    res_bartender_cust = client.get('/api/pos/customers/')
    assert len(res_bartender_cust.data) == 0, f"Bar cashier should see 0 stationery customers, saw {len(res_bartender_cust.data)}"
    print(f" [OK] Isolation verified: Bar cashier cannot see Stationery customer directory (returned 0).")

    # Bar cashier cannot access Stationery reports
    res_bartender_stat_rep = client.get('/api/pos/reports/stationery/')
    assert res_bartender_stat_rep.status_code == 404, f"Bar cashier accessing Stationery reports must get HTTP 404, got {res_bartender_stat_rep.status_code}"
    print(f" [OK] Isolation verified: Bar cashier accessing Stationery reports gets HTTP 404.")

    print("\n" + "=" * 60)
    print("ALL PHASE 2 BACKEND TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 60)


if __name__ == '__main__':
    run_tests()
