# ARAM Mayhem Database — DESIGN system

## Color (OKLCH-tinted neutrals + tier hues)

**Strategy**: Committed. Tier colours carry 30%+ of visible surface area as champion-thumbnail frames; the dark slate background recedes.

### Neutrals (all faintly tinted toward 270° cool)

| token | hex | OKLCH approx | use |
|---|---|---|---|
| `bg` | `#0e1116` | `oklch(0.18 0.012 270)` | page background |
| `panel` | `#161a22` | `oklch(0.22 0.014 270)` | filter bar, detail panel surround |
| `card` | `#1f2530` | `oklch(0.25 0.016 270)` | augment cards |
| `inner` | `#11151d` | `oklch(0.2 0.013 270)` | augment card body |
| `text` | `#e6e8eb` | `oklch(0.92 0.005 270)` | body |
| `muted` | `#9aa0a6` | `oklch(0.65 0.01 270)` | captions, meta |
| `border` | `#30363d` | `oklch(0.35 0.013 270)` | input borders |
| `border-hover` | `#58606b` | `oklch(0.45 0.014 270)` | input focus |

### Tier hues (committed strategy; each carries ~25 thumbnails)

| tier | hex | role |
|---|---|---|
| OP | iridescent gradient (white → lavender → blue → pink → gold) | apex; prismShift animation |
| T1 | `#ff5a3c` → `#c8262c` 4-stop gradient | premium; slow prismShift |
| T2 | `#f5c518` solid | gold-equivalent |
| T3 | `#8ec441` solid | green |
| T4 | `#3aa0ff` solid | blue |
| T5 | `#7a7f8a` solid | grey |

### Role-chip hues (data, not decoration)

`Assassin #ef4444` · `Fighter #f97316` · `Mage #3b82f6` · `Marksman #22c55e` · `Support #ec4899` · `Tank #a855f7`

## Typography

Two Google Fonts, one request:

- **Body / heading / chip / pill**: Noto Sans TC (400 / 500 / 600 / 700). Latin glyphs ship paired.
- **Caption mincho**: Noto Serif TC (400 / 500). Reserved for three small lines: page subtitle, detail-panel sub-heading, augment-card lift/games row. Anywhere else == sans.

Fallback chain: `-apple-system, "Segoe UI", "Microsoft JhengHei", "PingFang TC", sans-serif`.

### Scale (px, irregular for rhythm)

`9 · 10 · 11 · 12 · 13 · 14 · 16 · 18 · 22`

Never flat steps — h1 = 22, tier-pill = 16, body = 13, augment-name = 10, lift caption = 9.

## Spacing scale

`4 · 5 · 6 · 8 · 10 · 12 · 14 · 16 · 18 · 24 · 32 · 40` — pick by rhythm, not by token slot.

## Border radius

`4` (badge) · `5` (chip pill) · `6` (input, gh-star, role-chip outer) · `8` (champ, aug, search) · `10` (filter-bar, detail panel, tier-row)

## Component contract

### Tier pill (`.tier-pill`)

Filled (NOT a frame) inline pill at the start of each tier-heading row. OP pill animates `prismShift` 6 s + `shineSweep` 3.2 s. Other tiers solid.

### Champion thumbnail (`.champ`)

`aspect-ratio: 1/1`, 2 px frame in tier hue. OP uses double-background gradient frame; T1 uses 4-stop hot-coal gradient + slow shift; T2–T5 solid border. Hover = `translateY(-1px)`. Selected = `translateY(-2px) + brightness(1.08) + 1 px white outer ring`.

### Augment card (`.aug`)

Icon (48 px desktop / 36 px mobile) + name (2-line clamp) + smoothed WR colored by good/bad. `box-shadow` inset 2 px in rarity colour (`kPrismatic #d36bff` / `kGold #f5c518` / `kSilver #c0c5cc`). Hover spawns absolute popup with desc + stats.

### Role chip (`.chip`)

`border-radius: 18px` pill button. Active state = role hue filled. CSS variable `--role-color` per `[data-role]`.

### Detail host

A grid item with `grid-column: 1 / -1` inside `.tier-grid`. JS moves it after the last champ in the clicked champion's visual row (uses `offsetTop` to detect row).

## Motion

All animations purposeful, none decorative.

- `prismShift` 6s ease-in-out infinite — OP frame hue drift
- `prismShift` 9s ease-in-out infinite — T1 frame (slower → reads as second tier)
- `shineSweep` 3.2s linear infinite — OP pill highlight pass
- `slideDown` 0.18s ease-out — detail panel reveal
- Hover/selected — 0.08s `transform` + `filter`

**No** layout-property animations. **No** bounce / elastic. Ease-out exponential curves only.

## Banned (already enforced; documented to keep enforced)

- Side-stripe borders (we removed an early one)
- Gradient text (background-clip text + gradient)
- Glassmorphism / blur as decoration
- Hero-metric template (big-number + small-label + accent gradient)
- Modal as first thought (detail panel is inline)
- Identical card grids (tier blocks vary by champion count; augment grids by rarity)

## Iconography

GitHub octicon for the Star button. No emoji decoration in the chrome — emoji belongs in Mayhem augment names themselves (the data) but not in UI labels.
