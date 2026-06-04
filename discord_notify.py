"""
Discord webhook notifications for Cardboard Party.

Sends a message to a configured Discord channel when a new round is paired.
The message includes the full pairings list and current standings.

To set up:
  1. In your Discord server, go to the channel you want notifications in
  2. Click Edit Channel → Integrations → Webhooks → New Webhook
  3. Copy the webhook URL
  4. Paste it into the Admin → Settings page in Cardboard Party
"""

import re
import requests

# Discord webhook URLs only. Validating the host is also our SSRF guard: the
# server POSTs to these URLs, and organisers (not just admins) now supply them,
# so we must never let an arbitrary URL through to requests.post().
_WEBHOOK_RE = re.compile(
    r'^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$'
)

def is_valid_webhook(url: str) -> bool:
    return bool(url) and bool(_WEBHOOK_RE.match(url.strip()))


def post_test(webhook_url: str) -> bool:
    """Send a simple test message. Returns True on success."""
    if not is_valid_webhook(webhook_url):
        return False
    try:
        resp = requests.post(
            webhook_url,
            json={"content": "✅ Cardboard Party notifications are working!"},
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord test error: {e}")
        return False


def post_round(webhook_url: str, event: dict, round_num: int,
               pairings: list, standings: list):
    """Post a new round notification to Discord."""
    if not is_valid_webhook(webhook_url):
        return

    event_name = event.get('name', 'Event')
    event_url  = f"https://cardboard-party.wl.r.appspot.com/events/{event['id']}"

    # Build pairings text
    player_map = {p['id']: p['name'] for p in event.get('players', [])}
    pairing_lines = []
    for m in pairings:
        if m.get('is_bye'):
            pairing_lines.append(f"• {player_map.get(m['player1_id'], '?')} — *bye*")
        else:
            p1 = player_map.get(m['player1_id'], '?')
            p2 = player_map.get(m['player2_id'], '?')
            pairing_lines.append(f"• {p1}  vs  {p2}")

    # Build standings text (top 8 or all if fewer)
    standing_lines = []
    for i, s in enumerate(standings[:8]):
        standing_lines.append(f"{i+1}. {s['name']} — {s['points']} pts")
    if len(standings) > 8:
        standing_lines.append(f"*…and {len(standings) - 8} more*")

    # Compose the Discord embed
    embed = {
        "title": f"🃏 {event_name} — Round {round_num} Pairings",
        "url":   event_url,
        "color": 0x185fa5,
        "fields": [
            {
                "name":   "Pairings",
                "value":  "\n".join(pairing_lines) or "No pairings",
                "inline": False,
            },
            {
                "name":   "Standings",
                "value":  "\n".join(standing_lines) or "No standings yet",
                "inline": False,
            },
        ],
        "footer": {"text": "Cardboard Party"},
    }

    try:
        resp = requests.post(
            webhook_url,
            json={"embeds": [embed]},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        # Don't let a Discord failure break the pairing response
        print(f"Discord webhook error: {e}")
