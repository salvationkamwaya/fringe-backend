#!/usr/bin/env python
"""
Standalone execution script to create or update the Business Owner in Fringe Business System.

Usage Examples:
  # Default interactive or environment-based:
  python backend/scripts/create_owner.py

  # Non-interactive with custom credentials:
  python backend/scripts/create_owner.py --username mitu --email mitu@fringe.local --password MySecurePass2026! --pin 9999 --non-interactive
"""
import os
import sys

# Ensure backend root is on sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

try:
    import django
    django.setup()
    from django.core.management import call_command
except ImportError as exc:
    raise ImportError(
        "Couldn't import Django. Are you sure it's installed and "
        "available on your PYTHONPATH environment variable? Did you "
        "forget to activate a virtual environment?"
    ) from exc


def main():
    # Pass all CLI arguments directly to the create_owner management command
    args = sys.argv[1:]
    call_command('create_owner', *args)


if __name__ == '__main__':
    main()
