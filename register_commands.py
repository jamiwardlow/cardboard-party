"""
Register (bulk-overwrite) the bot's slash commands with Discord.

Run this once per environment whenever the command set changes (e.g. a new
subcommand). It reads DISCORD_BOT_TOKEN and DISCORD_APP_ID the same way the app
does (env var first, then this project's Secret Manager via GOOGLE_CLOUD_PROJECT)
and PUTs the full command list to the global application-commands endpoint, which
replaces whatever was registered before.

    # Staging (secrets in the staging project's Secret Manager):
    GOOGLE_CLOUD_PROJECT=cardboard-party-staging python register_commands.py

    # Prod:
    GOOGLE_CLOUD_PROJECT=cardboard-party python register_commands.py

Global commands can take a few minutes to propagate to clients.
"""

import sys
import requests
from gcp_secrets import get_secret

API = 'https://discord.com/api/v10'

# Option types we use: 1 = SUB_COMMAND, 6 = USER.
COMMANDS = [{
    'name': 'cparty',
    'description': 'Cardboard Party tournaments',
    'type': 1,  # CHAT_INPUT
    'options': [
        {'type': 1, 'name': 'register',
         'description': 'Register yourself for an event'},
        {'type': 1, 'name': 'report',
         'description': 'Report the result of your match'},
        {'type': 1, 'name': 'standings',
         'description': "Show an event's standings"},
        {'type': 1, 'name': 'link',
         'description': "Post this event's pairings in this channel each round"},
        {'type': 1, 'name': 'announce',
         'description': 'Post an event here with a one-tap Register button'},
        {'type': 1, 'name': 'invite',
         'description': 'DM someone an invitation to register for an event',
         'options': [
             {'type': 6, 'name': 'user',
              'description': 'Who to invite (must be a member of this server)',
              'required': True},
         ]},
        {'type': 1, 'name': 'help',
         'description': 'List the bot commands and what they do'},
    ],
}]


def main():
    token = get_secret('DISCORD_BOT_TOKEN')
    app_id = get_secret('DISCORD_APP_ID')
    if not (token and app_id):
        sys.exit('Missing DISCORD_BOT_TOKEN / DISCORD_APP_ID — set them in the '
                 'environment or point GOOGLE_CLOUD_PROJECT at the right project.')
    r = requests.put(f'{API}/applications/{app_id}/commands',
                     headers={'Authorization': f'Bot {token}'},
                     json=COMMANDS, timeout=10)
    if not r.ok:
        sys.exit(f'Command registration failed {r.status_code}: {r.text[:500]}')
    names = ', '.join(c['name'] for c in r.json())
    print(f'Registered {len(r.json())} command(s): {names}')


if __name__ == '__main__':
    main()
