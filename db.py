from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

_db = None

def get_db():
    """Returns a Firestore client (lazily initialized)."""
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


# ── Events ─────────────────────────────────────────────────────────────────────

def create_event(data: dict) -> str:
    db = get_db()
    ref = db.collection('events').document()
    stored = {**data, 'id': ref.id}
    if 'rounds' in stored:
        stored['rounds'] = _flatten_rounds(stored['rounds'])
    ref.set(stored)
    return ref.id

def get_event(event_id: str) -> dict | None:
    doc = get_db().collection('events').document(event_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if 'rounds' in data:
        data['rounds'] = _unflatten_rounds(data['rounds'])
    return data

def save_event(event_id: str, data: dict):
    stored = {**data}
    if 'rounds' in stored:
        stored['rounds'] = _flatten_rounds(stored['rounds'])
    get_db().collection('events').document(event_id).set(stored, merge=True)

def delete_event(event_id: str):
    get_db().collection('events').document(event_id).delete()

def set_player_field(event_id: str, player_id: str, field: str, value):
    """Atomically set one field on a single player.

    Read-modify-write of the whole `players` array isn't safe under concurrency:
    two overlapping requests start from the same snapshot, so the later write
    clobbers the earlier one (a lost update — e.g. a "dropped" player left active,
    or a check-in undone). Running it in a transaction serializes them.

    Returns the updated player dict, or None if the event or player is gone.
    """
    db = get_db()
    ref = db.collection('events').document(event_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn):
        snapshot = ref.get(transaction=txn)
        if not snapshot.exists:
            return None
        players = snapshot.to_dict().get('players', [])
        target = next((p for p in players if p['id'] == player_id), None)
        if target is None:
            return None
        target[field] = value
        txn.update(ref, {'players': players})
        return target

    return _apply(transaction)

def set_player_dropped(event_id: str, player_id: str, dropped: bool):
    """Atomically flip a single player's `dropped` flag (see set_player_field)."""
    return set_player_field(event_id, player_id, 'dropped', dropped)

def list_events() -> list[dict]:
    events = []
    for doc in get_db().collection('events').stream():
        data = doc.to_dict()
        if 'rounds' in data:
            data['rounds'] = _unflatten_rounds(data['rounds'])
        events.append(data)
    return events


# ── Admins ─────────────────────────────────────────────────────────────────────
# Stored as a single Firestore document: { admins: [{id, email, name}] }

_ADMIN_DOC = 'config/admins'

def get_admins() -> list[dict]:
    doc = get_db().document(_ADMIN_DOC).get()
    return doc.to_dict().get('admins', []) if doc.exists else []

def is_admin(google_id: str) -> bool:
    return any(a['id'] == google_id for a in get_admins())

def add_admin(google_id: str, email: str, name: str):
    admins = get_admins()
    if any(a['id'] == google_id for a in admins):
        return  # already an admin
    admins.append({'id': google_id, 'email': email, 'name': name})
    get_db().document(_ADMIN_DOC).set({'admins': admins})

def remove_admin(google_id: str):
    admins = [a for a in get_admins() if a['id'] != google_id]
    get_db().document(_ADMIN_DOC).set({'admins': admins})


# ── Firestore nested-array workaround ─────────────────────────────────────────

def _flatten_rounds(rounds: list) -> list:
    flat = []
    for round_num, matches in enumerate(rounds):
        for match in matches:
            flat.append({**match, 'round_num': round_num})
    return flat

def _unflatten_rounds(flat: list) -> list:
    if not flat:
        return []
    num_rounds = max(m['round_num'] for m in flat) + 1
    rounds = [[] for _ in range(num_rounds)]
    for match in flat:
        rn = match['round_num']
        rounds[rn].append({k: v for k, v in match.items() if k != 'round_num'})
    return rounds


# ── Users ──────────────────────────────────────────────────────────────────────
# Stores persistent profile data keyed by Google ID.
# This is separate from the player entries inside each event, which are
# snapshots at registration time. The users collection is the source of
# truth for display name and Discord handle going forward.

def get_user_profile(google_id: str) -> dict:
    doc = get_db().collection('users').document(google_id).get()
    return doc.to_dict() if doc.exists else {}

def save_user_profile(google_id: str, data: dict):
    get_db().collection('users').document(google_id).set(data, merge=True)

def list_users() -> list[dict]:
    """All known user profiles, each including its google_id (the document id)."""
    users = []
    for doc in get_db().collection('users').stream():
        data = doc.to_dict() or {}
        data['google_id'] = doc.id
        users.append(data)
    return users


# ── Event invites (Discord DMs) ──────────────────────────────────────────────
# Each invite a Discord user sends is logged so we can rate-limit senders and
# avoid re-pestering the same recipient about the same event. Recipients can
# opt out entirely (stored by their numeric Discord ID, since a recipient may
# have no account here). Queries use single-field equality filters only, so no
# composite Firestore index is needed; small result sets are filtered in Python.

def record_invite(inviter_id: str, target_id: str, event_id: str, ts: float):
    """Log that `inviter_id` invited `target_id` to `event_id` at unix time `ts`."""
    get_db().collection('invites').add({
        'inviter_id': str(inviter_id), 'target_id': str(target_id),
        'event_id': event_id, 'ts': ts})

def recent_invite_count(inviter_id: str, since_ts: float) -> int:
    """How many invites this sender has logged since `since_ts` (rate limiting)."""
    q = get_db().collection('invites').where(
        filter=FieldFilter('inviter_id', '==', str(inviter_id))).stream()
    return sum(1 for d in q if (d.to_dict().get('ts') or 0) >= since_ts)

def target_invited_since(target_id: str, event_id: str, since_ts: float) -> bool:
    """Whether `target_id` has already been invited to `event_id` since `since_ts`
    (dedupe — by anyone, so the recipient isn't pestered repeatedly)."""
    q = get_db().collection('invites').where(
        filter=FieldFilter('target_id', '==', str(target_id))).stream()
    return any(d.to_dict().get('event_id') == event_id
               and (d.to_dict().get('ts') or 0) >= since_ts for d in q)

def set_invite_optout(discord_id: str, opted_out: bool = True):
    """Record (or clear) a recipient's choice not to receive event invites."""
    ref = get_db().collection('invite_optouts').document(str(discord_id))
    if opted_out:
        ref.set({'opted_out': True})
    else:
        ref.delete()

def is_invite_opted_out(discord_id: str) -> bool:
    return get_db().collection('invite_optouts').document(str(discord_id)).get().exists
