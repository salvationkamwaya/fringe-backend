from decimal import Decimal
from django.core.management.base import BaseCommand
from business_core.models import BusinessUnit, User
from business_catalog.models import Category, Product


class Command(BaseCommand):
    help = 'Seeds initial business units, roles, staff accounts, bar and stationery products.'

    def handle(self, *args, **options):
        self.stdout.write("Starting database seeding for Fringe Business System...")

        # 1. Create Business Units
        bar_unit, _ = BusinessUnit.objects.get_or_create(
            id='bar',
            defaults={
                'name': 'Fringe Lounge & Bar',
                'code': 'BAR',
                'currency': 'ZMW',
                'settings': {
                    'receipt_header': 'Fringe Lounge & Bar\nPlot 104, Great East Road, Lusaka\nTel: +260 971 000 111',
                    'receipt_footer': 'Thank you for drinking responsibly!\nNo sale of alcohol to persons under 18.',
                    'min_age_check': 18,
                    'allow_credit': False
                }
            }
        )

        stat_unit, _ = BusinessUnit.objects.get_or_create(
            id='stationery',
            defaults={
                'name': 'Fringe Stationery & Retail',
                'code': 'STAT',
                'currency': 'ZMW',
                'settings': {
                    'receipt_header': 'Fringe Stationery & Printing Services\nPlot 104, Great East Road, Lusaka\nTel: +260 971 000 222',
                    'receipt_footer': 'Quality stationery for office & school.\nAll returns require original receipt.',
                    'allow_credit': True,
                    'default_credit_limit': 1500.00
                }
            }
        )
        self.stdout.write(self.style.SUCCESS("[OK] Business Units seeded: Bar and Stationery"))

        # 2. Seed Users
        # Owner (Dr. Mitu)
        owner, created = User.objects.get_or_create(
            username='owner',
            defaults={
                'email': 'mitu@fringebusiness.com',
                'first_name': 'Dr.',
                'last_name': 'Mitu',
                'role': User.ROLE_OWNER,
                'is_staff': True,
                'is_superuser': True,
            }
        )
        owner.set_password('password123')
        owner.set_pin('9999')
        owner.save()

        # Bar Manager (Alex)
        bar_mgr, created = User.objects.get_or_create(
            username='bar_manager',
            defaults={
                'email': 'alex@fringe-bar.com',
                'first_name': 'Alex',
                'last_name': 'Banda',
                'role': User.ROLE_BAR_MANAGER,
                'business_unit': bar_unit,
                'is_staff': True,
            }
        )
        bar_mgr.set_password('password123')
        bar_mgr.set_pin('1234')
        bar_mgr.save()

        # Bartender (John)
        bartender, created = User.objects.get_or_create(
            username='bartender',
            defaults={
                'email': 'john@fringe-bar.com',
                'first_name': 'John',
                'last_name': 'Mwila',
                'role': User.ROLE_BARTENDER,
                'business_unit': bar_unit,
            }
        )
        bartender.set_password('password123')
        bartender.set_pin('0000')
        bartender.save()

        # Stationery Manager (Sarah)
        stat_mgr, created = User.objects.get_or_create(
            username='stationery_manager',
            defaults={
                'email': 'sarah@fringe-stationery.com',
                'first_name': 'Sarah',
                'last_name': 'Tembo',
                'role': User.ROLE_STATIONERY_MANAGER,
                'business_unit': stat_unit,
                'is_staff': True,
            }
        )
        stat_mgr.set_password('password123')
        stat_mgr.set_pin('5678')
        stat_mgr.save()

        # Stationery Cashier (Grace)
        stat_cashier, created = User.objects.get_or_create(
            username='stationery_cashier',
            defaults={
                'email': 'grace@fringe-stationery.com',
                'first_name': 'Grace',
                'last_name': 'Phiri',
                'role': User.ROLE_STATIONERY_CASHIER,
                'business_unit': stat_unit,
            }
        )
        stat_cashier.set_password('password123')
        stat_cashier.set_pin('1111')
        stat_cashier.save()

        # Stock Clerk (Peter)
        stock_clerk, created = User.objects.get_or_create(
            username='stock_clerk',
            defaults={
                'email': 'peter@fringebusiness.com',
                'first_name': 'Peter',
                'last_name': 'Zulu',
                'role': User.ROLE_STOCK_CLERK,
                'business_unit': bar_unit,
            }
        )
        stock_clerk.set_password('password123')
        stock_clerk.assigned_units.set([bar_unit, stat_unit])
        stock_clerk.save()

        self.stdout.write(self.style.SUCCESS("[OK] Staff Accounts seeded: Owner, Bar Mgr, Bartender, Stat Mgr, Stat Cashier, Stock Clerk"))

        # 3. Seed Bar Categories and Products
        cat_beers, _ = Category.objects.get_or_create(
            business_unit=bar_unit, slug='beers',
            defaults={'name': 'Beers & Ciders', 'icon': 'beer', 'display_order': 1}
        )
        cat_spirits, _ = Category.objects.get_or_create(
            business_unit=bar_unit, slug='spirits',
            defaults={'name': 'Spirits & Whiskeys', 'icon': 'glass-whiskey', 'display_order': 2}
        )
        cat_cocktails, _ = Category.objects.get_or_create(
            business_unit=bar_unit, slug='cocktails',
            defaults={'name': 'Signature Cocktails', 'icon': 'cocktail', 'display_order': 3}
        )
        cat_softs, _ = Category.objects.get_or_create(
            business_unit=bar_unit, slug='soft-drinks',
            defaults={'name': 'Soft Drinks & Sodas', 'icon': 'cup-soda', 'display_order': 4}
        )
        cat_water, _ = Category.objects.get_or_create(
            business_unit=bar_unit, slug='water-juices',
            defaults={'name': 'Water & Energy Drinks', 'icon': 'droplets', 'display_order': 5}
        )

        bar_products = [
            {
                'name': 'Mosi Lager 375ml',
                'category': cat_beers,
                'sku': 'BAR-MOSI-01',
                'barcode': '6001001001',
                'selling_price': Decimal('35.00'),
                'cost_price': Decimal('22.00'),
                'stock_quantity': Decimal('120.00'),
                'is_alcoholic': True,
                'volume_ml': 375,
            },
            {
                'name': 'Castle Lite 330ml',
                'category': cat_beers,
                'sku': 'BAR-CASTLE-01',
                'barcode': '6001001002',
                'selling_price': Decimal('38.00'),
                'cost_price': Decimal('24.00'),
                'stock_quantity': Decimal('90.00'),
                'is_alcoholic': True,
                'volume_ml': 330,
            },
            {
                'name': 'Jameson Irish Whiskey (Tot)',
                'category': cat_spirits,
                'sku': 'BAR-JAMO-TOT',
                'barcode': '6001001003',
                'selling_price': Decimal('65.00'),
                'cost_price': Decimal('35.00'),
                'stock_quantity': Decimal('45.00'),
                'is_alcoholic': True,
                'volume_ml': 50,
            },
            {
                'name': 'Johnnie Walker Black Label (Tot)',
                'category': cat_spirits,
                'sku': 'BAR-JW-BLACK',
                'barcode': '6001001004',
                'selling_price': Decimal('85.00'),
                'cost_price': Decimal('45.00'),
                'stock_quantity': Decimal('35.00'),
                'is_alcoholic': True,
                'volume_ml': 50,
            },
            {
                'name': 'Classic Mojito',
                'category': cat_cocktails,
                'sku': 'BAR-MOJITO-01',
                'barcode': '6001001005',
                'selling_price': Decimal('75.00'),
                'cost_price': Decimal('28.00'),
                'stock_quantity': Decimal('60.00'),
                'is_alcoholic': True,
                'volume_ml': 250,
            },
            {
                'name': 'Coca Cola Can 330ml',
                'category': cat_softs,
                'sku': 'BAR-COKE-330',
                'barcode': '6001001006',
                'selling_price': Decimal('18.00'),
                'cost_price': Decimal('10.00'),
                'stock_quantity': Decimal('150.00'),
                'is_alcoholic': False,
                'volume_ml': 330,
            },
            {
                'name': 'Sprite Can 330ml',
                'category': cat_softs,
                'sku': 'BAR-SPRITE-330',
                'barcode': '6001001007',
                'selling_price': Decimal('18.00'),
                'cost_price': Decimal('10.00'),
                'stock_quantity': Decimal('110.00'),
                'is_alcoholic': False,
                'volume_ml': 330,
            },
            {
                'name': 'Red Bull Energy Drink 250ml',
                'category': cat_water,
                'sku': 'BAR-REDBULL-01',
                'barcode': '6001001008',
                'selling_price': Decimal('45.00'),
                'cost_price': Decimal('28.00'),
                'stock_quantity': Decimal('65.00'),
                'is_alcoholic': False,
                'volume_ml': 250,
            },
            {
                'name': 'Natural Mineral Water 500ml',
                'category': cat_water,
                'sku': 'BAR-WATER-500',
                'barcode': '6001001009',
                'selling_price': Decimal('12.00'),
                'cost_price': Decimal('6.00'),
                'stock_quantity': Decimal('200.00'),
                'is_alcoholic': False,
                'volume_ml': 500,
            },
        ]

        for p_data in bar_products:
            Product.objects.update_or_create(
                business_unit=bar_unit,
                sku=p_data['sku'],
                defaults={
                    'name': p_data['name'],
                    'category': p_data['category'],
                    'barcode': p_data['barcode'],
                    'selling_price': p_data['selling_price'],
                    'cost_price': p_data['cost_price'],
                    'stock_quantity': p_data['stock_quantity'],
                    'reorder_threshold': Decimal('10.00'),
                    'is_alcoholic': p_data['is_alcoholic'],
                    'volume_ml': p_data['volume_ml'],
                    'is_active': True,
                }
            )
        self.stdout.write(self.style.SUCCESS(f"[OK] Seeded {len(bar_products)} Bar Products (Beers, Spirits, Soft Drinks)"))

        # 4. Seed Stationery Categories and Products
        cat_paper, _ = Category.objects.get_or_create(
            business_unit=stat_unit, slug='paper',
            defaults={'name': 'Paper & Reams', 'icon': 'file-text', 'display_order': 1}
        )
        cat_pens, _ = Category.objects.get_or_create(
            business_unit=stat_unit, slug='writing',
            defaults={'name': 'Pens & Markers', 'icon': 'pen-tool', 'display_order': 2}
        )
        cat_books, _ = Category.objects.get_or_create(
            business_unit=stat_unit, slug='notebooks',
            defaults={'name': 'Books & Notepads', 'icon': 'book', 'display_order': 3}
        )
        cat_office, _ = Category.objects.get_or_create(
            business_unit=stat_unit, slug='office-desk',
            defaults={'name': 'Office Accessories', 'icon': 'briefcase', 'display_order': 4}
        )

        stat_products = [
            {
                'name': 'Rotatrim A4 80gsm Copy Paper Ream',
                'category': cat_paper,
                'sku': 'STAT-ROTA-A4',
                'barcode': '7001001001',
                'selling_price': Decimal('185.00'),
                'cost_price': Decimal('135.00'),
                'stock_quantity': Decimal('50.00'),
                'is_pack': True,
                'pack_unit_ratio': 500,
                'pack_name': 'Ream (500 sheets)',
                'unit_name': 'Sheet',
                'allow_split_sale': False,
            },
            {
                'name': 'Bic Medium Blue Ballpoint Pens (Box of 50)',
                'category': cat_pens,
                'sku': 'STAT-BIC-BLUE-BOX',
                'barcode': '7001001002',
                'selling_price': Decimal('220.00'),
                'cost_price': Decimal('150.00'),
                'stock_quantity': Decimal('35.00'),
                'is_pack': True,
                'pack_unit_ratio': 50,
                'pack_name': 'Box of 50',
                'unit_name': 'Single Pen',
                'allow_split_sale': True,
                'split_unit_price': Decimal('5.00'),
            },
            {
                'name': 'Bic Medium Black Ballpoint Pens (Box of 50)',
                'category': cat_pens,
                'sku': 'STAT-BIC-BLACK-BOX',
                'barcode': '7001001003',
                'selling_price': Decimal('220.00'),
                'cost_price': Decimal('150.00'),
                'stock_quantity': Decimal('30.00'),
                'is_pack': True,
                'pack_unit_ratio': 50,
                'pack_name': 'Box of 50',
                'unit_name': 'Single Pen',
                'allow_split_sale': True,
                'split_unit_price': Decimal('5.00'),
            },
            {
                'name': 'Oxford Hardcover A4 192-Page Notebook',
                'category': cat_books,
                'sku': 'STAT-OXFORD-A4',
                'barcode': '7001001004',
                'selling_price': Decimal('65.00'),
                'cost_price': Decimal('40.00'),
                'stock_quantity': Decimal('75.00'),
                'is_pack': False,
            },
            {
                'name': 'A5 Exercise Book 72-Page (Pack of 10)',
                'category': cat_books,
                'sku': 'STAT-EXBOOK-A5-PK',
                'barcode': '7001001005',
                'selling_price': Decimal('95.00'),
                'cost_price': Decimal('60.00'),
                'stock_quantity': Decimal('60.00'),
                'is_pack': True,
                'pack_unit_ratio': 10,
                'pack_name': 'Pack of 10',
                'unit_name': 'Single Book',
                'allow_split_sale': True,
                'split_unit_price': Decimal('11.00'),
            },
            {
                'name': 'Heavy Duty Metal Desktop Stapler',
                'category': cat_office,
                'sku': 'STAT-STAPLER-HD',
                'barcode': '7001001006',
                'selling_price': Decimal('145.00'),
                'cost_price': Decimal('95.00'),
                'stock_quantity': Decimal('25.00'),
                'is_pack': False,
            },
            {
                'name': 'Kangaroo 26/6 Staples Box (5000 staples)',
                'category': cat_office,
                'sku': 'STAT-STAPLES-266',
                'barcode': '7001001007',
                'selling_price': Decimal('35.00'),
                'cost_price': Decimal('20.00'),
                'stock_quantity': Decimal('90.00'),
                'is_pack': True,
                'pack_unit_ratio': 5000,
                'pack_name': 'Box (5000)',
                'unit_name': 'Strip',
                'allow_split_sale': False,
            },
            {
                'name': 'Bostik Clear Glue Stick 36g',
                'category': cat_office,
                'sku': 'STAT-GLUE-36G',
                'barcode': '7001001008',
                'selling_price': Decimal('28.00'),
                'cost_price': Decimal('16.00'),
                'stock_quantity': Decimal('80.00'),
                'is_pack': False,
            },
        ]

        for p_data in stat_products:
            Product.objects.update_or_create(
                business_unit=stat_unit,
                sku=p_data['sku'],
                defaults={
                    'name': p_data['name'],
                    'category': p_data['category'],
                    'barcode': p_data['barcode'],
                    'selling_price': p_data['selling_price'],
                    'cost_price': p_data['cost_price'],
                    'stock_quantity': p_data['stock_quantity'],
                    'reorder_threshold': Decimal('10.00'),
                    'is_alcoholic': False,
                    'is_pack': p_data.get('is_pack', False),
                    'pack_unit_ratio': p_data.get('pack_unit_ratio', 1),
                    'pack_name': p_data.get('pack_name', 'Pack'),
                    'unit_name': p_data.get('unit_name', 'Piece'),
                    'allow_split_sale': p_data.get('allow_split_sale', False),
                    'split_unit_price': p_data.get('split_unit_price'),
                    'is_active': True,
                }
            )
        self.stdout.write(self.style.SUCCESS(f"[OK] Seeded {len(stat_products)} Stationery Products (Reams, Packs, Books)"))
        self.stdout.write(self.style.SUCCESS("All initial database records seeded successfully!"))
