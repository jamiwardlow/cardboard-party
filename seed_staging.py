"""
Seed the STAGING Firestore with a few sample events for testing.

Safety: refuses to run unless GOOGLE_CLOUD_PROJECT is the staging project, so it
can never write sample data into production.

    GOOGLE_CLOUD_PROJECT=cardboard-party-staging python seed_staging.py
"""

import os
import sys
import datetime

STAGING_PROJECT = 'cardboard-party-staging'

if os.environ.get('GOOGLE_CLOUD_PROJECT') != STAGING_PROJECT:
    sys.exit(f"Refusing to seed: set GOOGLE_CLOUD_PROJECT={STAGING_PROJECT} first "
             "(this script must never touch production).")

from db import create_event   # imported after the guard

today = datetime.date.today().isoformat()


def _players(names):
    return [{'id': f'{n.lower()}_{i}', 'name': n, 'google_id': None,
             'discord': '', 'dropped': False, 'checked_in': True}
            for i, n in enumerate(names)]


SAMPLE_EVENTS = [
    {
        'name': 'Friday Night Pauper (sample)',
        'advanced': False,
        'game': '', 'event_type': 'One-day', 'format': 'Pauper',
        'description': 'Sample simple event for staging.',
        'date': today, 'num_rounds': 3, 'status': 'setup', 'registration': 'open',
        'players': _players(['Alice', 'Bob', 'Carol', 'Dave']), 'rounds': [],
    },
    {
        'name': 'Modern RCQ (sample)',
        'advanced': True,
        'game': 'Magic: The Gathering', 'event_type': 'One-day', 'format': 'Modern',
        'description': 'Sample advanced event with a top cut.',
        'tags': ['Regional Championship Qualifier'],
        'structure': 'swiss_top_cut', 'planned_cut_size': 8,
        'round_timer_minutes': 50, 'require_check_in': True,
        'date': today, 'num_rounds': 4, 'status': 'setup', 'registration': 'open',
        'players': _players(['Erin', 'Frank', 'Grace', 'Heidi', 'Ivan', 'Judy']),
        'rounds': [],
    },
]


def main():
    for e in SAMPLE_EVENTS:
        e.setdefault('owner_id', 'seed')
        e.setdefault('owner_name', 'Seed Script')
        eid = create_event(e)
        print(f"  created {eid}  {e['name']}")
    print(f"Seeded {len(SAMPLE_EVENTS)} events into {STAGING_PROJECT}.")


if __name__ == '__main__':
    main()
