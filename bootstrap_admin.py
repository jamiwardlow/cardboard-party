"""
Run this once to add yourself as the first global admin.

Usage:
  python bootstrap_admin.py your@gmail.com "Your Name"

This adds a pending entry by email. The next time you sign into
Cardboard Party with that Google account, it will be resolved to your
real Google ID automatically.
"""

import sys
from db import add_admin

if len(sys.argv) < 3:
    print("Usage: python bootstrap_admin.py your@gmail.com \"Your Name\"")
    sys.exit(1)

email = sys.argv[1].strip().lower()
name  = sys.argv[2].strip()

add_admin(google_id=f'pending:{email}', email=email, name=name)
print(f"✓ Added {name} ({email}) as a pending admin.")
print(f"  Sign into Cardboard Party with this Google account to activate it.")
