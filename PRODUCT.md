# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Volunteer/community organizers running Swiss-system tournaments and leagues for
trading-card-game events (primarily Magic: The Gathering), for a specific local
community (the East Bay MTG scene) that already coordinates over Discord
alongside in-person play. Players in that community are the other user group:
they register, get paired, and report results, mostly without ever opening the
web app.

## Product Purpose

Cardboard Party runs the administrative core of a Swiss tournament or league —
registration, waitlisting, pairing, result reporting, standings, and playoff
brackets — so a volunteer organizer doesn't have to do it by hand or in a
spreadsheet. Success is an organizer running a full event end-to-end (and
players participating in it) without friction, data loss, or needing a second
tool.

## Positioning

Discord-native workflow: players can register, receive pairings, and report
results entirely through Discord slash commands and DMs, without visiting the
web app. The web app is the organizer's control surface and the durable record
of the event; the bot is how most players actually touch the product. This is
the thing a generic bracket tool (Challonge, a spreadsheet) or a storefront
platform (EventLink) doesn't do.

## Operating Context

In-person Swiss events (one-day, multi-week league, or draft) run by a
volunteer organizer, typically at a local game store or similar community
venue. Organizers work from the web app (pairing rounds, editing results,
managing the waitlist); players interact primarily via the Discord bot
(`/cparty`), with the web app as a secondary/optional surface for viewing
standings, managing their profile, or uploading a decklist.

## Capabilities and Constraints

- Swiss pairing/standings engine (`swiss.py`), one-day/league/draft event
  types, single-elimination playoff brackets.
- Waitlist with manual promotion; table assignments (auto or fixed seating).
- Decklist upload + validation (Scryfall / Premodern legality, Moxfield
  import); print-policy and proxy tagging for organizers.
- Google and Discord OAuth login; Discord role assignment per event; avatar
  upload.
- Admin / owner / co-organizer permission model; players report only their
  own match results.
- No optimistic locking on the event document — concurrent writers can
  clobber each other's changes (a known, accepted limitation, not a bug to
  silently paper over in the interface).
- No payment or commerce features; this is not a storefront/POS tool.

## Brand Commitments

- The "Cardboard Party" name is fixed.
- Discord-first identity must stay visible and prominent: the bot, Discord
  login, and the "Join us on Discord" invite are core to the product's
  identity, not an add-on to de-emphasize.
- Free / non-commercial framing must be preserved — no paid tiers, ads, or
  monetization-driven UI pressure (upsells, gated features, etc.).

## Evidence on Hand

- `about.html` credits creation to Jami "with generous input from the East
  Bay Magic: The Gathering community" — the product's origin is a specific
  real community, not a generic audience.
- A live Discord community exists (invite link on the About page); no
  testimonials, case studies, press, or usage metrics exist to reference —
  future work must not invent any.

## Product Principles

1. Discord is a first-class surface, not an afterthought — the core loop
   (register, get paired, report a result, see standings) must keep working
   for a player who never opens the website.
2. Free and non-commercial by design — no monetization pressure should ever
   shape an interface decision.
3. Organizer trust over cleverness — this holds the live record of a real
   in-person event; the interface must never make players or organizers
   doubt whether a result or pairing was actually saved.
4. Built for how this community already runs events, not generalized for
   an unknown audience of tournament organizers.
5. Low friction for players — the web surface should inform and assist, never
   gate participation that Discord already handles.
