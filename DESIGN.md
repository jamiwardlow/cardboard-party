---
name: Cardboard Party
description: An 8-bit console cartridge world for running Swiss tournaments — earned pixel confetti, never ambient decoration.
colors:
  bg: "#150F24"
  bg2: "#1E1730"
  bg3: "#2A2044"
  ink: "#0B0714"
  border: "#372F52"
  text: "#F5F1FF"
  muted: "#B3A8D9"
  accent: "#B98CFF"
  accent-h: "#7C3AED"
  pink: "#FF6BB5"
  pink-h: "#C71F6B"
  green: "#34D399"
  green-h: "#16A97B"
  danger: "#FF6B6B"
  danger-h: "#C42B2B"
typography:
  display:
    fontFamily: "Silkscreen, Space Grotesk, system-ui, sans-serif"
    fontWeight: 700
    letterSpacing: "0.01em"
  pixel:
    fontFamily: "'Press Start 2P', 'Space Mono', ui-monospace, monospace"
    fontWeight: 400
  body:
    fontFamily: "Space Grotesk, system-ui, sans-serif"
    fontWeight: 400
    lineHeight: 1.6
  mono:
    fontFamily: "'Space Mono', ui-monospace, monospace"
rounded:
  structural: "0px"
  chip: "3px"
spacing:
  page-pad: "1.5rem"
components:
  button-primary:
    backgroundColor: "{colors.accent-h}"
    textColor: "#ffffff"
    rounded: "{rounded.structural}"
    padding: "0.5rem 1rem"
  button-primary-hover:
    backgroundColor: "#6d28d9"
  button-default:
    backgroundColor: "{colors.bg2}"
    textColor: "{colors.text}"
    rounded: "{rounded.structural}"
    padding: "0.5rem 1rem"
  card:
    backgroundColor: "{colors.bg2}"
    textColor: "{colors.text}"
    rounded: "{rounded.structural}"
    padding: "1.1rem 1.25rem"
  chip-status-open:
    backgroundColor: "{colors.green}"
    textColor: "{colors.ink}"
    rounded: "{rounded.chip}"
    padding: "0.15rem 0.65rem"
  chip-status-closed:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.chip}"
    padding: "0.15rem 0.65rem"
---

# Design System: Cardboard Party

## Overview

**Creative North Star: "Cartridge Confetti"**

Cardboard Party is an 8-bit console cartridge, not a SaaS dashboard: flat, saturated cartridge purple/pink/green on a near-black shell, hard 2px ink outlines, zero-blur offset shadows, and rectangular chrome (rounding is reserved for a small chip radius on tags, never on structural panels). It refuses the soft-gradient hero and rounded-pill chrome that every other bracket tool ships. A player should recognize a game they've played, trust the save-file's worth of state behind it, and feel the tournament is a party — not paperwork.

The world's one moment of ambient delight, pixel confetti, is earned rather than decorative: it fires only at three real milestones (self-registration confirmed, a freshly-posted round containing your own match, a newly-decided champion), gated so it never replays for state that already existed before the current page visit, and it steps down to a smaller reduced-motion burst under `prefers-reduced-motion`.

This is a code-led build: there is no approved comp image in the record, only text-only decision cards during direction selection, followed directly by implementation and a finish review (disposition: ship, after one fix round covering shadow-color contrast, a unicode-glyph icon cleanup, pairing "vs" pixel-font unification, and the status-chip filled treatment). The whole app inherits from one shared stylesheet; only the event list (`index.html`) and the event page (`event.html`) received bespoke visual review — the rest of the template set is styled by inheritance from the same tokens and components but was not independently screenshotted.

**Key Characteristics:**
- Flat cartridge-purple world, zero gradients, zero blur
- Two-tier radius: true rectangles for structure, a whisper of rounding only on tags
- Depth carried by a saturated pink offset shadow, not a dark drop shadow
- Two-tier type: pixel/display faces for our own fixed chrome only, clean sans for anything a user typed
- Confetti as an earned, gated signature — never ambient

## Colors

A flat, saturated cartridge palette on a near-black purple shell — no gradients, no soft blends; role separation between a lighter "ink" tone (text/links) and a deeper "fill" tone (solid backgrounds) per hue so white text on a filled surface reliably clears contrast.

### Primary
- **Cartridge Purple** (`#B98CFF` / deep fill `#7C3AED`): the accent hue — links, focus rings, primary buttons, the logo, "your match" highlighting in pairings, active-state chips.

### Secondary
- **Cartridge Pink** (`#FF6BB5` / deep fill `#C71F6B`): the shadow/depth hue (see Elevation) and the waitlist status chip; also the drop-tag and p2-wins highlight in the result modal.

### Tertiary
- **Cartridge Green** (`#34D399` / deep fill `#16A97B`): the "go/confirmed" hue — registration-open chip, checked-in tag, payment link, round-complete tag, decklist-valid messaging.

### Neutral
- **Cartridge Shell** (`#150F24` page background, `#1E1730` panel background, `#2A2044` raised/hover surface): the three-step dark ramp everything sits on.
- **Ink** (`#0B0714`): the hard outline color on every bordered element (buttons, cards, modals, inputs) — never a lighter border in this role.
- **Hairline** (`#372F52`): the quieter 1px divider used inside dense lists and tables, distinct from the 2px ink structural border.
- **Party Text** (`#F5F1FF`): primary text on the dark shell.
- **Muted** (`#B3A8D9`): secondary text, meta lines, placeholders, disabled labels.

### Named Rules
**The Hue-Not-Luminance Rule.** Depth cues never rely on a dark drop shadow against this near-black world — a black-on-black shadow is invisible here, so weight and proximity are always signaled with a saturated hue (pink) instead of a darker tone.

## Typography

**Display Font:** Silkscreen (with Space Grotesk, system-ui fallback)
**Label/Mono Font:** Press Start 2P (with Space Mono, ui-monospace fallback)
**Body Font:** Space Grotesk (with system-ui, sans-serif fallback)

**Character:** A pixel-console face pair reserved strictly for the app's own fixed chrome, paired with a clean geometric sans that carries everything a person actually typed — the contrast between "the machine talking" and "the player talking" is the point.

### Hierarchy
- **Display** (Silkscreen, 700, ~1.2–1.4rem): the nav logo (with a pink text-shadow echoing the shadow system) and page `<h1>` headings that are app-authored, not user-typed (e.g. "Events").
- **Pixel/Label** (Press Start 2P, 400, ~0.6–1.2rem): section headings ("Standings", "Players", "Rounds"), modal dialog titles, round labels and round-status text, the pairing "vs" separator, the standings rank column, score-entry numerals, waitlist position numbers, stat-card values, countdown timer text.
- **Body** (Space Grotesk, 400–700, 0.82–1.05rem, line-height 1.6): all user-authored content — event names, player display names, descriptions, form fields, profile bios — plus general UI copy (buttons, meta text, banners).
- **Mono** (Space Mono / ui-monospace, 0.8–0.9rem): decklist text blocks, inline `<code>`, table-assignment badges, copy-paste helper fields.

### Named Rules
**The Chrome-vs-Content Rule.** The pixel display faces (Silkscreen, Press Start 2P) are reserved exclusively for fixed, app-authored strings the team controls. Anything a user typed — event names, player names, descriptions, form content — stays in the body face (Space Grotesk), even when it sits directly beside pixel-font chrome (e.g. an event's own `<h1>` is bold body type, not display type). This is the load-bearing rule of the whole system: never let pixel type leak onto user content, and never render app chrome in the body face.

## Layout

A centered single-column reading width (`max-width: 1100px`) for the sticky header, main content, and footer, with `--page-pad` (1.5rem, stepping to 1rem then 0.75rem on narrower breakpoints) as the horizontal gutter. The event page uses a masonry-like flow (`#event-flow`) that stacks content blocks in one column on narrow viewports and deals them into two balanced columns on wide ones, ordered by event stage. Card grids (`.card-grid`) are `auto-fill, minmax(240px, 1fr)`, collapsing to a single column under 700px. Breakpoints in use: 700px (nav/card-grid collapse), 480px (table/tap-target adjustments), 380px (button sizing).

## Elevation & Depth

Hard, zero-blur offset shadows — a cartridge sitting slightly proud of the shelf, never a soft glow. Depth is carried by hue contrast (a saturated pink shadow), not by a luminance gap: a near-black shadow proved invisible against this near-black page during the finish review, so the "proud of the shelf" cue had to be recolored rather than darkened. Hover lifts an element (translate up-left + a bigger shadow); pressing/active collapses the shadow to 0 and translates the element down-right, reading as physically pushed in.

### Shadow Vocabulary
- **sm** (`2px 2px 0 var(--pink-h)`): default resting depth for buttons, round cards, banners, stat cards.
- **md** (`4px 4px 0 var(--pink-h)`): default resting depth for the shelf cards (`.card`), hover state for buttons/stat cards, modal-adjacent popovers (calendar menu, autocomplete list, place-suggest).
- **lg** (`6px 6px 0 var(--pink-h)`): the modal's own resting depth, and the card hover state (which jumps straight to the largest shadow to read as "lifted off the shelf").

### Named Rules
**The Hue-Shadow Rule.** Every structural shadow in the system is colored `var(--pink-h)`, never `var(--ink)` or black — ink is reserved for outlines, pink is reserved for depth.

## Shapes

Two-tier radius system. `--radius: 0` governs all structural chrome — cards, buttons, panels, modals, inputs, the event brand image — so it reads as a true rectangular cartridge shell. `--radius-chip: 3px` is reserved for small status tags and chip-like elements only (status bands, tag chips, badges, the checkin/drop tags, scrollbar thumb) — just enough rounding that a chip reads as a "sticker," never enough to soften a structural panel. Borders are uniformly 2px solid `var(--ink)` on structural elements (buttons, cards, inputs, modals) and 1px solid `var(--border)` on internal dividers (table rows, list separators). Avatars and the profile cropper are the one deliberate exception to the rectangular rule — circular, matching their real-world source material (a Google/Discord profile photo).

## Components

### Buttons
- **Shape:** rectangular (`--radius: 0`), 2px ink border, `--shadow-sm` at rest.
- **Primary:** filled `--accent-h` (#7C3AED) background, white text, 700 weight; hover darkens to `#6d28d9`.
- **Default:** `--bg2` background, `--text` color, 600 weight.
- **Danger:** transparent background, `--danger` text/border; hover fills with a translucent danger tint.
- **Hover / Focus / Active:** hover translates the button up-left 1px and steps the shadow from sm to md; active/press translates it down-right 2px and collapses the shadow to 0, reading as pressed into the cartridge slot. `:focus-visible` gets a 2px accent outline everywhere in the system, not just on buttons.
- **Size variant:** `.btn-sm` uses a thinner 1px/2px shadow pair scaled to its smaller footprint.

### Chips
- **Style:** status bands (`.reg-open`, `.reg-active`, `.reg-done`, `.reg-waitlist`) are filled solid "cartridge label sticker" chips — a saturated fill color with a 2px ink border and high-contrast text, uppercase, letter-spaced. `.reg-closed` alone is the deliberate exception: an unfilled outline in muted/border color, representing an "empty, unprinted slot" rather than an inactive filled state.
- **Tag chips** (`.tag-chip`, `.badge`) are quieter: muted text on `--bg3`, 1px `--border`, `--radius-chip`.

### Cards / Containers
- **Corner Style:** rectangular (`--radius: 0`).
- **Background:** `--bg2`, with a 2px ink border.
- **Shadow Strategy:** `--shadow-md` at rest, jumping to a hand-tuned `6px 6px 0 var(--pink-h)` on hover with a 2px up-left translate — the "pulling a cartridge off the shelf" motion.
- **Internal Padding:** `1.1rem 1.25rem`.

### Inputs / Fields
- **Style:** `--bg` background, 2px ink border, `--radius: 0`, `--font-body`.
- **Focus:** border shifts to `--accent` plus a 2px accent outline (no glow/shadow effect).
- **Error / Disabled:** invalid fields swap the border/outline color to `--danger`; disabled buttons drop to 0.4 opacity and lose their shadow/hover response.

### Navigation
- Sticky header (`--bg2`, 2px ink bottom border). Logo is Silkscreen 700 with a pink text-shadow echoing the shadow system. Nav links are muted, brightening to accent on hover. Mobile (≤700px) wraps the nav to a second line rather than overflowing, and hides the user's name text (avatar-only).

### Signature Component: Pixel Confetti
A burst of flat, hard-edged squares (no blur, no gradient — same material as the rest of the world) in the three party hues, fired from `burstConfetti(originEl)` in `app.js`. It triggers at exactly three points in the app, each gated to a real milestone rather than page load: a successful self-registration, a freshly-posted round that contains the signed-in player's own match (never for rounds that already existed before the current visit), and a newly-decided tournament champion. `prefers-reduced-motion` shrinks the piece count and collapses the animation duration rather than removing the moment outright.

## Do's and Don'ts

### Do:
- **Do** keep `--radius: 0` on every structural surface (cards, buttons, modals, inputs, panels); reserve `--radius-chip` (3px) for small status/tag chips only.
- **Do** color structural shadows with `var(--pink-h)`, never `var(--ink)` or black — a dark shadow disappears against this palette's near-black base.
- **Do** render app-authored fixed chrome (nav, section headings, dialog titles, round labels, the "vs" separator, standings rank, score numerals, status chips) in the pixel display faces (Silkscreen / Press Start 2P).
- **Do** render anything a user typed (names, descriptions, form content) in the body face (Space Grotesk), even directly adjacent to pixel-font chrome.
- **Do** gate any new celebratory effect (confetti or otherwise) to a genuine, one-time milestone the user just reached — never fire it on page load or for pre-existing state.
- **Do** respect `prefers-reduced-motion` for any new motion (scale it down, don't silently drop the moment).

### Don't:
- **Don't** use a soft blurred drop-shadow or glow anywhere — every shadow in this system is a hard, zero-blur offset.
- **Don't** round a structural panel, button, card, or modal corner; rounding above 3px signals "not a cartridge" in this world.
- **Don't** put user-generated text in Silkscreen or Press Start 2P — it was built for short, fixed, all-caps-friendly labels and degrades badly on long or unpredictable strings.
- **Don't** treat `.reg-closed`'s outline-only treatment as the norm for status chips — it's a deliberate one-off "empty slot" exception; every other status chip in the system is filled solid.
- **Don't** add ambient/ornamental animation (auto-playing sparkle, idle looping motion) — this world's only motion signature is the three earned confetti triggers plus ordinary hover/press feedback.
