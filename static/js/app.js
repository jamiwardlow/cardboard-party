// thunderdome/static/js/app.js
// Shared utilities — page-specific logic lives in the template <script> blocks.

// Escape user-supplied text before interpolating it into innerHTML. Player
// names, discord handles, and event names are all free-text, so anything built
// with template literals + innerHTML must run untrusted values through this.
function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Format a percentage for display
function pct(val) {
  return (val * 100).toFixed(1) + '%';
}

// Simple date formatter: "2025-01-15" → "Jan 15, 2025"
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

// ── Table assignments ───────────────────────────────────────────────────────
// Organisers type table ranges/labels as free text ("13, 21-24", "1:Feature
// Match"); these convert to/from the structured forms the API stores. Shared by
// the create form (index.html) and the edit modal (event.html).

// "13, 21-24" → sorted unique [13, 21, 22, 23, 24].
function parseTableList(str) {
  const out = new Set();
  (str || '').split(',').forEach(part => {
    part = part.trim();
    const m = part.match(/^(\d+)\s*-\s*(\d+)$/);
    if (m) {
      let a = +m[1], b = +m[2];
      if (a > b) [a, b] = [b, a];
      for (let n = a; n <= b; n++) out.add(n);
    } else if (/^\d+$/.test(part)) {
      out.add(+part);
    }
  });
  return [...out].filter(n => n >= 1 && n <= 999).sort((x, y) => x - y);
}

// [13, 21, 22, 23, 24] → "13, 21-24" (collapse runs for a tidy round-trip).
function formatTableList(arr) {
  const nums = [...(arr || [])].map(Number).filter(n => n >= 1).sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < nums.length; i++) {
    let j = i;
    while (j + 1 < nums.length && nums[j + 1] === nums[j] + 1) j++;
    parts.push(i === j ? `${nums[i]}` : `${nums[i]}-${nums[j]}`);
    i = j;
  }
  return parts.join(', ');
}

// "1:Feature Match, 2:Coverage" → { "1": "Feature Match", "2": "Coverage" }.
function parseTableLabels(str) {
  const out = {};
  (str || '').split(',').forEach(part => {
    const idx = part.indexOf(':');
    if (idx < 0) return;
    const n = part.slice(0, idx).trim();
    const label = part.slice(idx + 1).trim().slice(0, 40);
    if (/^\d+$/.test(n) && +n >= 1 && +n <= 999 && label) out[String(+n)] = label;
  });
  return out;
}

function formatTableLabels(obj) {
  return Object.entries(obj || {})
    .sort((a, b) => +a[0] - +b[0])
    .map(([n, label]) => `${n}:${label}`)
    .join(', ');
}

// Tables available for AUTO assignment: the range minus reserved and labeled
// tables (labeled ones are placed manually, so they're held out of the pool).
function autoTableCount(start, end, excluded, labels) {
  if (!end || end < start) return 0;
  const ex = new Set((excluded || []).map(Number));
  Object.keys(labels || {}).forEach(k => ex.add(+k));
  let c = 0;
  for (let n = start; n <= end; n++) if (!ex.has(n)) c++;
  return c;
}

// Toggle a "*-table-opts" block from its "*-tables" checkbox.
function toggleTableOpts(prefix) {
  const on = document.getElementById(`${prefix}-tables`).checked;
  document.getElementById(`${prefix}-table-opts`).classList.toggle('hidden', !on);
}

// Soft warning when the configured range can't seat the expected players.
function refreshTableWarning(prefix, expectedPlayers) {
  const warnEl = document.getElementById(`${prefix}-table-warn`);
  if (!warnEl) return;
  const start = parseInt(document.getElementById(`${prefix}-table-start`).value) || 1;
  const end   = parseInt(document.getElementById(`${prefix}-table-end`).value) || 0;
  const excl  = parseTableList(document.getElementById(`${prefix}-table-excluded`).value);
  const labels = parseTableLabels(document.getElementById(`${prefix}-table-labels`).value);
  let msg = '';
  if (end && end < start) {
    msg = 'Last table number must be greater than the first.';
  } else if (end && expectedPlayers) {
    const cap = autoTableCount(start, end, excl, labels);
    const need = Math.ceil(expectedPlayers / 2);
    if (cap < need)
      msg = `Only ${cap} assignable table${cap === 1 ? '' : 's'} for up to ${expectedPlayers} players (need ${need}).`;
  }
  warnEl.textContent = msg;
}

// ── Google Maps / location ──────────────────────────────────────────────────
// The Maps API script (base.html, only when a key is configured) calls onGoogleMaps
// when ready. Everything degrades gracefully when no key is present.
window.mapsReady = false;
window.onGoogleMaps = function () {
  window.mapsReady = true;
  document.dispatchEvent(new Event('maps-ready'));
};
function whenMapsReady(cb) {
  if (window.mapsReady) cb();
  else document.addEventListener('maps-ready', cb, { once: true });
}

// Place selections captured from autocomplete, keyed by input id.
const placeData = {};

// Places API (New): a custom, themed suggestion dropdown bound to our own input, so
// the field keeps its styling/prefill and degrades to a plain text input if Maps or
// the new Places library isn't available.
async function attachPlaceAutocomplete(inputId) {
  const input = document.getElementById(inputId);
  if (!input || !window.mapsReady || input.dataset.acAttached) return;
  input.dataset.acAttached = '1';
  let AutocompleteSuggestion, AutocompleteSessionToken;
  try {
    ({ AutocompleteSuggestion, AutocompleteSessionToken } =
       await google.maps.importLibrary('places'));
  } catch (e) { return; }
  if (!AutocompleteSuggestion) return;

  const dd = document.createElement('div');
  dd.className = 'place-suggest';
  dd.style.display = 'none';
  document.body.appendChild(dd);
  let token = new AutocompleteSessionToken();
  let items = [], seq = 0;

  const position = () => {
    const r  = input.getBoundingClientRect();
    const vv = window.visualViewport;
    // getBoundingClientRect() is relative to the layout viewport; position:fixed
    // is relative to the visual viewport. On iOS Safari the two diverge when the
    // virtual keyboard appears, pushing the visual viewport up inside the layout
    // viewport. Subtracting the visual-viewport offsets keeps the dropdown under
    // the input after the keyboard slides in.
    dd.style.left  = `${r.left   - (vv ? vv.offsetLeft : 0)}px`;
    dd.style.top   = `${r.bottom - (vv ? vv.offsetTop  : 0) + 2}px`;
    dd.style.width = `${r.width}px`;
  };
  const hide = () => { dd.style.display = 'none'; };
  const reposition = () => { if (dd.style.display !== 'none') position(); };

  input.addEventListener('input', async () => {
    delete placeData[inputId];                 // typing invalidates a prior selection
    const q = input.value.trim();
    if (q.length < 3) { hide(); return; }
    const mine = ++seq;
    let suggestions = [];
    try {
      ({ suggestions } = await AutocompleteSuggestion.fetchAutocompleteSuggestions(
        { input: q, sessionToken: token }));
    } catch (e) { hide(); return; }
    if (mine !== seq) return;                   // a newer keystroke superseded this one
    items = (suggestions || []).map(s => s.placePrediction).filter(Boolean);
    if (!items.length) { hide(); return; }
    dd.innerHTML = items.map((p, i) =>
      `<div class="place-suggest-item" data-i="${i}">${escapeHtml(String(p.text))}</div>`).join('');
    position(); dd.style.display = '';
  });

  // mousedown (not click) so the pick registers before the input's blur hides the list.
  dd.addEventListener('mousedown', async (e) => {
    const el = e.target.closest('.place-suggest-item');
    if (!el) return;
    e.preventDefault();
    hide();
    try {
      const p = items[+el.dataset.i].toPlace();
      await p.fetchFields({ fields: ['displayName', 'formattedAddress', 'location', 'id'] });
      const text = p.formattedAddress || p.displayName || input.value;
      input.value = text;
      placeData[inputId] = { location: text, lat: p.location.lat(), lng: p.location.lng(),
                             place_id: p.id || '' };
    } catch (err) { /* keep the typed text; coords stay unset */ }
    token = new AutocompleteSessionToken();     // a selection ends the billing session
  });

  input.addEventListener('blur', () => setTimeout(hide, 150));
  // Reposition on scroll (desktop) or visual-viewport change (iOS keyboard).
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', reposition);
    window.visualViewport.addEventListener('scroll', reposition);
  } else {
    window.addEventListener('scroll', reposition, true);
  }
}

// Location + coords to submit for an input. Coords only count when the captured
// place still matches the current text (the user didn't type past the selection).
function placeFields(inputId) {
  const input = document.getElementById(inputId);
  const loc = input ? input.value : '';
  const pd = placeData[inputId];
  return (pd && pd.location === loc)
    ? { location: loc, lat: pd.lat, lng: pd.lng, place_id: pd.place_id }
    : { location: loc, lat: null, lng: null, place_id: '' };
}

// Great-circle distance in miles (for the "events near me" filter).
function haversineMiles(lat1, lng1, lat2, lng2) {
  const toRad = d => d * Math.PI / 180, R = 3958.8;
  const dLat = toRad(lat2 - lat1), dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// Proxy settings reveal with "Proxies allowed"; the count field only when the
// policy is "limited". Shared by the create form ('e') and edit modal ('ee').
function onProxyToggle(prefix) {
  const on = document.getElementById(`${prefix}-allow-proxies`).checked;
  document.getElementById(`${prefix}-proxy-opts`).classList.toggle('hidden', !on);
  const limited = document.getElementById(`${prefix}-proxy-policy`).value === 'limited';
  document.getElementById(`${prefix}-proxy-limit-wrap`).classList.toggle('hidden', !(on && limited));
}

// Esc closes the top-most open modal (New event, Edit event, Enter result,
// Edit pairings, avatar cropper — anything using .modal-backdrop). Per-modal
// open handlers reset their state on reopen, so simply hiding is safe.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const open = document.querySelectorAll('.modal-backdrop:not(.hidden)');
  if (open.length) open[open.length - 1].classList.add('hidden');
});
