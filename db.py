from google.cloud import firestore

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


# ── Config (Discord webhook etc.) ─────────────────────────────────────────────

_CONFIG_DOC = 'config/settings'

def get_config() -> dict:
    doc = get_db().document(_CONFIG_DOC).get()
    return doc.to_dict() if doc.exists else {}

def save_config(data: dict):
    get_db().document(_CONFIG_DOC).set(data, merge=True)
