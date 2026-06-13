"""
Round labelling for Discord posts.

Historically this module also sent round notifications via Discord webhooks, but
that was retired in favour of the bot (see discord_api.py / routes/discord.py).
Only the shared round-label helper remains, used when the bot posts pairings.
"""


def _round_label(round_num: int, pairings: list) -> str:
    """Human label for a round: a Swiss round number, or the playoff bracket
    stage (Finals/Semifinals/Quarterfinals/Top N) when these are bracket matches."""
    if pairings and pairings[0].get('stage') == 'bracket':
        return {1: 'Finals', 2: 'Semifinals', 4: 'Quarterfinals'}.get(
            len(pairings), f"Top {len(pairings) * 2}")
    return f"Round {round_num}"
