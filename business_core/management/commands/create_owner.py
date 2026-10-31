import os
import sys
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from business_core.models import User, AuditLog


class Command(BaseCommand):
    help = 'Create or initialize the Business Owner account with full cross-business privileges.'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, help='Owner username (default: env OWNER_USERNAME or "owner")')
        parser.add_argument('--email', type=str, help='Owner email address (default: env OWNER_EMAIL or "mitu@fringebusiness.com")')
        parser.add_argument('--password', type=str, help='Owner login password (min 6 characters)')
        parser.add_argument('--pin', type=str, help='Owner 4-6 digit authorization PIN (default: "9999")')
        parser.add_argument('--first-name', type=str, help='First name (default: "Dr.")')
        parser.add_argument('--last-name', type=str, help='Last name (default: "Mitu")')
        parser.add_argument('--non-interactive', action='store_true', help='Run non-interactively using provided args/defaults')

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== FRINGE BUSINESS SYSTEM: OWNER ACCOUNT INITIALIZATION ==="))

        # Resolve credentials with priority: CLI option > ENV var > Default/Prompt
        username = options.get('username') or os.getenv('OWNER_USERNAME')
        email = options.get('email') or os.getenv('OWNER_EMAIL')
        password = options.get('password') or os.getenv('OWNER_PASSWORD')
        pin = options.get('pin') or os.getenv('OWNER_PIN')
        first_name = options.get('first_name') or os.getenv('OWNER_FIRST_NAME')
        last_name = options.get('last_name') or os.getenv('OWNER_LAST_NAME')
        non_interactive = options.get('non_interactive', False)

        # Fallbacks or interactive prompts
        if not username:
            if non_interactive or not sys.stdin.isatty():
                username = 'owner'
            else:
                input_usr = input("Enter Owner Username [default: owner]: ").strip()
                username = input_usr if input_usr else 'owner'

        if not email:
            if non_interactive or not sys.stdin.isatty():
                email = 'mitu@fringebusiness.com'
            else:
                input_email = input("Enter Owner Email [default: mitu@fringebusiness.com]: ").strip()
                email = input_email if input_email else 'mitu@fringebusiness.com'

        # Validate email
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(f"Invalid email address provided: '{email}'")

        if not first_name:
            first_name = 'Dr.'
        if not last_name:
            last_name = 'Mitu'

        if not password:
            if non_interactive or not sys.stdin.isatty():
                password = 'Fringe@2026!'
            else:
                import getpass
                input_pwd = getpass.getpass("Enter Owner Password (strong, min 8 chars) [default: Fringe@2026!]: ").strip()
                password = input_pwd if input_pwd else 'Fringe@2026!'

        import re
        missing = []
        if len(password) < 8:
            missing.append("at least 8 characters")
        if not re.search(r'[A-Z]', password):
            missing.append("at least one uppercase letter (A-Z)")
        if not re.search(r'[a-z]', password):
            missing.append("at least one lowercase letter (a-z)")
        if not re.search(r'[0-9]', password):
            missing.append("at least one number (0-9)")
        if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]', password):
            missing.append("at least one special character (!@#$%^&*)")
        if missing:
            raise CommandError(f"Password does not meet security standards. Missing: {', '.join(missing)}.")

        if not pin:
            if non_interactive or not sys.stdin.isatty():
                pin = '9999'
            else:
                input_pin = input("Enter Owner 4-6 digit numeric PIN [default: 9999]: ").strip()
                pin = input_pin if input_pin else '9999'

        if not str(pin).isdigit() or len(str(pin)) < 4 or len(str(pin)) > 6:
            raise CommandError("PIN must be a 4 to 6 digit numeric code.")

        # Check existing user by username or email
        existing_user = User.objects.filter(username=username).first() or User.objects.filter(email=email).first()

        if existing_user:
            self.stdout.write(f"Updating existing account: @{existing_user.username} ({existing_user.email})...")
            user = existing_user
            user.username = username
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.role = User.ROLE_OWNER
            user.business_unit = None  # Global cross-unit access
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            user.set_password(password)
            user.set_pin(pin)
            user.save()
            action_desc = "UPDATED"
        else:
            self.stdout.write(f"Creating new Owner account: @{username} ({email})...")
            user = User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=User.ROLE_OWNER,
                business_unit=None,  # Global cross-unit access
                is_staff=True,
                is_superuser=True,
                is_active=True
            )
            user.set_password(password)
            user.set_pin(pin)
            user.save()
            action_desc = "CREATED"

        # Record in Immutable Audit Log
        AuditLog.objects.create(
            user=user,
            username=user.username,
            role=user.role,
            business_unit=None,
            action='STAFF_CREATED' if action_desc == 'CREATED' else 'STAFF_ROLE_ASSIGNED',
            entity_type='User',
            entity_id=user.id,
            after_value={
                'username': user.username,
                'email': user.email,
                'role': user.role,
                'is_owner': True,
                'unit': 'all'
            },
            metadata={'script': 'create_owner', 'action': action_desc}
        )

        self.stdout.write(self.style.SUCCESS(f"\n[OK] Business Owner Successfully {action_desc}!"))
        self.stdout.write("---------------------------------------------------------------")
        self.stdout.write(f"  Name:       {user.first_name} {user.last_name}")
        self.stdout.write(f"  Username:   {user.username}")
        self.stdout.write(f"  Email:      {user.email}")
        self.stdout.write(f"  Role:       {user.get_role_display()} (Full Cross-Unit Isolation Bypass)")
        self.stdout.write(f"  Auth PIN:   Configured ({len(pin)} digits)")
        self.stdout.write("---------------------------------------------------------------")
        self.stdout.write(self.style.NOTICE(">> LOGIN INSTRUCTIONS:"))
        self.stdout.write(f"   You can now log in via the POS interface or API using EITHER:")
        self.stdout.write(f"   1. Username: '{user.username}' with your password")
        self.stdout.write(f"   2. Email:    '{user.email}' with your password\n")
