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
