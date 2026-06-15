"""
Round labelling for Discord posts.

Historically this module also sent round notifications via Discord webhooks, but
that was retired in favour of the bot (see discord_api.py / routes/discord.py).
Only the shared round-label helper remains, used when the bot posts pairings.
"""


def fmt_time(hhmm: str) -> str:
    """Format a 'HH:MM' 24-hour time string as a friendly 12-hour time
    (e.g. '19:00' → '7:00 PM'). Returns '' for blank or malformed input. Lives
    here (a dependency-free module) so the web app and the bot share one
    implementation."""
    try:
        h_str, m_str = (hhmm or '').strip().split(':')
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return ''
    if not (0 <= h < 24 and 0 <= m < 60):
        return ''
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _round_label(round_num: int, pairings: list) -> str:
    """Human label for a round: a Swiss round number, or the playoff bracket
    stage (Finals/Semifinals/Quarterfinals/Top N) when these are bracket matches."""
    if pairings and pairings[0].get('stage') == 'bracket':
        return {1: 'Finals', 2: 'Semifinals', 4: 'Quarterfinals'}.get(
            len(pairings), f"Top {len(pairings) * 2}")
    return f"Round {round_num}"
