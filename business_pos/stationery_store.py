import os
import json
import uuid
import threading
from datetime import datetime
from decimal import Decimal
from django.conf import settings
from django.utils import timezone

DATA_DIR = os.path.join(settings.BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

EXPENSES_FILE = os.path.join(DATA_DIR, 'expenses.json')
QUOTATIONS_FILE = os.path.join(DATA_DIR, 'quotations.json')
RETURNS_FILE = os.path.join(DATA_DIR, 'returns.json')

_lock = threading.Lock()


def _read_json(filepath):
    with _lock:
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []


def _write_json(filepath, data):
    with _lock:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ==========================================
# 1. OPERATING EXPENSES (F9)
# ==========================================
EXPENSE_CATEGORIES = [
    'Rent',
    'Transport & Fuel',
    'Electricity & Utilities',
    'Staff Salaries & Allowances',
    'Repairs & Maintenance',
    'Office Supplies & Packing',
    'Marketing & Advertising',
    'Bank Charges & Taxes',
    'Miscellaneous & Sundry',
]


def list_expenses(unit_id='stationery', start_date=None, end_date=None, category=None, branch_id=None):
    items = _read_json(EXPENSES_FILE)
    filtered = []
    branch_filter = str(branch_id) if (branch_id and str(branch_id) != 'all') else None
    for exp in items:
        if unit_id != 'all' and exp.get('business_unit') != unit_id:
            continue
        if branch_filter and str(exp.get('branch_id') or '') != branch_filter:
            continue
        if category and exp.get('category') != category:
            continue
        exp_date = exp.get('date') or exp.get('created_at', '')[:10]
        if start_date and exp_date < str(start_date)[:10]:
            continue
        if end_date and exp_date > str(end_date)[:10]:
            continue
        filtered.append(exp)

    # Newest first
    filtered.sort(key=lambda x: x.get('date', ''), reverse=True)
    return filtered


def add_expense(data, user, unit_id='stationery', branch_id=None):
    items = _read_json(EXPENSES_FILE)
    expense_id = f"EXP-{timezone.now().strftime('%y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    
    amount = float(Decimal(str(data.get('amount', '0.00'))))
    effective_branch = data.get('branch_id') or branch_id
    if effective_branch and str(effective_branch) == 'all':
        effective_branch = None
    record = {
        'id': expense_id,
        'business_unit': unit_id,
        'branch_id': str(effective_branch) if effective_branch else None,
        'title': data.get('title', '').strip(),
        'category': data.get('category', 'Miscellaneous & Sundry'),
        'amount': amount,
        'date': data.get('date') or timezone.now().strftime('%Y-%m-%d'),
        'paid_to': data.get('paid_to', '').strip(),
        'payment_method': data.get('payment_method', 'CASH').upper(),
        'reference': data.get('reference', '').strip(),
        'notes': data.get('notes', '').strip(),
        'recorded_by': user.username,
        'recorded_by_name': f"{user.first_name} {user.last_name}".strip() or user.username,
        'created_at': timezone.now().isoformat()
    }
    items.insert(0, record)
    _write_json(EXPENSES_FILE, items)
    return record


def delete_expense(expense_id, unit_id='stationery'):
    items = _read_json(EXPENSES_FILE)
    initial_len = len(items)
    items = [x for x in items if not (x.get('id') == expense_id and (unit_id == 'all' or x.get('business_unit') == unit_id))]
    if len(items) != initial_len:
        _write_json(EXPENSES_FILE, items)
        return True
    return False


# ==========================================
# 2. QUOTATIONS / PRICE OFFERS (F5 & 4.4)
# ==========================================
def list_quotations(unit_id='stationery', status=None, customer_id=None):
    items = _read_json(QUOTATIONS_FILE)
    filtered = []
    for q in items:
        if unit_id != 'all' and q.get('business_unit') != unit_id:
            continue
        if status and q.get('status') != status:
            continue
        if customer_id and str(q.get('customer_id')) != str(customer_id):
            continue
        filtered.append(q)
    filtered.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return filtered


def get_quotation(quote_id, unit_id='stationery'):
    items = _read_json(QUOTATIONS_FILE)
    for q in items:
        if q.get('id') == quote_id and (unit_id == 'all' or q.get('business_unit') == unit_id):
            return q
    return None


def create_quotation(data, user, unit_id='stationery'):
    items = _read_json(QUOTATIONS_FILE)
    quote_number = f"QUO-{timezone.now().strftime('%y%m%d%H%M')}-{uuid.uuid4().hex[:4].upper()}"

    lines = data.get('items', [])
    subtotal = sum(float(l.get('subtotal', 0)) for l in lines)
    discount = float(data.get('discount_amount', 0))
    final_amount = max(0.0, subtotal - discount)

    record = {
        'id': quote_number,
        'quote_number': quote_number,
        'business_unit': unit_id,
        'customer_id': data.get('customer_id'),
        'customer_name': data.get('customer_name', 'Walk-in Wholesale Client'),
        'customer_contact': data.get('customer_contact', ''),
        'customer_address': data.get('customer_address', ''),
        'customer_tin': data.get('customer_tin', ''),
        'items': lines,
        'subtotal': subtotal,
        'discount_amount': discount,
        'final_amount': final_amount,
        'total_amount': final_amount,
        'valid_until': data.get('valid_until'),
        'status': 'PENDING',  # 'PENDING' | 'ACCEPTED' | 'CONVERTED' | 'REJECTED' | 'EXPIRED'
        'notes': data.get('notes', ''),
        'created_by': user.username,
        'created_by_name': f"{user.first_name} {user.last_name}".strip() or user.username,
        'created_at': timezone.now().isoformat(),
        'converted_order_number': None,
        'converted_at': None
    }
    items.insert(0, record)
    _write_json(QUOTATIONS_FILE, items)
    return record


def update_quotation_status(quote_id, status, converted_order_number=None, unit_id='stationery'):
    items = _read_json(QUOTATIONS_FILE)
    for q in items:
        if q.get('id') == quote_id and q.get('business_unit') == unit_id:
            q['status'] = status
            if converted_order_number:
                q['converted_order_number'] = converted_order_number
                q['converted_at'] = timezone.now().isoformat()
            _write_json(QUOTATIONS_FILE, items)
            return q
    return None


# ==========================================
# 3. RETURNS & REFUNDS (F8)
# ==========================================
def list_returns(unit_id='stationery'):
    items = _read_json(RETURNS_FILE)
    filtered = [x for x in items if unit_id == 'all' or x.get('business_unit') == unit_id]
    filtered.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return filtered


def create_return(data, user, approver=None, unit_id='stationery'):
    items = _read_json(RETURNS_FILE)
    return_number = f"RET-{timezone.now().strftime('%y%m%d%H%M')}-{uuid.uuid4().hex[:4].upper()}"

    lines = data.get('items', [])
    total_refund = sum(float(l.get('refund_amount', l.get('subtotal', 0))) for l in lines)

    record = {
        'id': return_number,
        'return_number': return_number,
        'business_unit': unit_id,
        'order_number': data.get('order_number', '').strip(),
        'customer_id': data.get('customer_id'),
        'customer_name': data.get('customer_name', 'Walk-in Customer'),
        'items': lines,
        'total_refund': total_refund,
        'refund_method': data.get('refund_method', 'CASH').upper(),  # 'CASH' | 'CREDIT' (reduces debt)
        'disposition': data.get('disposition', 'RETURN_TO_STOCK'),  # 'RETURN_TO_STOCK' | 'WRITE_OFF_DAMAGED'
        'reason': data.get('reason', 'Customer Return'),
        'approved_by': data.get('approved_by_name', user.username),
        'recorded_by': user.username,
        'notes': data.get('notes', ''),
        'created_at': timezone.now().isoformat()
    }
    items.insert(0, record)
    _write_json(RETURNS_FILE, items)
    return record
