"""Find every trading card in a photo, in any layout, without a model call.

A photo may hold one card, two side by side, a 2x2 or 3x3 block, a binder
page, or a loose scatter at odd angles. `find_cards` returns each card's four
corners in reading order (top row first, left to right) and `crop` squares one
up into an upright image.

How it works, all classical image processing:

1. Candidates. Every closed, four-sided, card-proportioned outline in the
   photo, traced two ways: from edges, and from "whatever is not the
   background" (the background being what the photo's border is made of).
   One card usually gives several: its edge, the frame printed inside its
   border, its art box. A binder page or a mat gives one too.
2. Nesting decides which is which. Outlines are sorted into what contains
   what. An outline holding two or more like-sized card outlines that fill
   most of it is a container (a page, a pair of touching cards) and is set
   aside for what it holds. Any other outline is a card, and what it holds
   is that card's own print.
3. Cards in one photo are the same physical size, so outlines far from the
   common size are dropped.
4. A card first found by its inner frame (its border being close to the
   background's colour) is moved out to its real edge.
5. A blob of not-background with no outline in it is cut along seams that
   run the whole way across it, if there are any (cards lying against each
   other).

Nothing here knows what is printed on a card. A photo with no visible
background and no traceable outlines gives nothing to separate; callers fall
back to a fixed grid for that.
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

CARD_ASPECT = 6.3 / 8.8            # short side / long side
OUT_W, OUT_H = 480, 671            # size of a squared-up crop

_WORK_SIDE = 1100                  # detection runs on a copy this long on its long side
_ASPECT_LO, _ASPECT_HI = 0.60, 0.84    # a card seen at a moderate angle
_MIN_AREA, _MAX_AREA = 0.012, 0.97     # share of the frame one card may cover
_MAX_GRID = 4                      # largest rows or columns of a block


# ---------------------------------------------------------------- geometry

def _order(pts: np.ndarray) -> np.ndarray:
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    centre = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - centre[1], pts[:, 0] - centre[0])
    pts = pts[np.argsort(angles)]              # clockwise on screen
    start = int(np.argmin(pts.sum(axis=1)))
    return np.roll(pts, -start, axis=0)


def _portrait(quad: np.ndarray) -> np.ndarray:
    """The same corners, started so the long sides run top to bottom."""
    q = _order(quad)
    top = np.linalg.norm(q[1] - q[0])
    side = np.linalg.norm(q[3] - q[0])
    return np.roll(q, -1, axis=0) if top > side else q


def crop(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """One card, squared up to an upright OUT_W x OUT_H image."""
    src = _portrait(quad)
    dst = np.array([[0, 0], [OUT_W - 1, 0], [OUT_W - 1, OUT_H - 1], [0, OUT_H - 1]], dtype=np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (OUT_W, OUT_H))


def _sides(quad: np.ndarray) -> tuple[float, float]:
    """(across, down) lengths of a quad in its own top-left-first order."""
    q = _order(quad)
    across = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    down = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    return float(across), float(down)


def _aspect(quad: np.ndarray) -> float:
    a, b = _sides(quad)
    return min(a, b) / max(a, b) if max(a, b) else 0.0


def _short(quad: np.ndarray) -> float:
    return min(_sides(quad))


def _area(quad: np.ndarray) -> float:
    return float(cv2.contourArea(_order(quad)))


def _centre(quad: np.ndarray) -> np.ndarray:
    return np.asarray(quad, dtype=np.float32).reshape(4, 2).mean(axis=0)


def _inside(inner: np.ndarray, outer: np.ndarray) -> bool:
    c = _centre(inner)
    return cv2.pointPolygonTest(_order(outer).reshape(-1, 1, 2), (float(c[0]), float(c[1])), False) >= 0


def _is_card(quad: np.ndarray, frame: float) -> bool:
    return _ASPECT_LO <= _aspect(quad) <= _ASPECT_HI and _MIN_AREA * frame <= _area(quad) <= _MAX_AREA * frame


def _quad_of(contour: np.ndarray) -> np.ndarray:
    """A contour's outline as four corners. A clean four-sided outline keeps
    its own corners, which carry the perspective; anything rougher gets the
    tightest rotated rectangle."""
    box = cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32)
    hull = cv2.convexHull(contour)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.035, 0.05):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            poly = approx.reshape(4, 2).astype(np.float32)
            if cv2.contourArea(poly) >= 0.85 * cv2.contourArea(box):
                return poly
    return box


# ---------------------------------------------------------------- candidates

def _background_mask(small: np.ndarray) -> Optional[np.ndarray]:
    """Pixels unlike the photo's border, or None when the border is not one
    even background (cards run off the edge, a page fills the frame)."""
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (5, 5), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 0.3                         # light and shadow vary across a table; colour does not
    h, w = lab.shape[:2]
    m = max(4, int(min(h, w) * 0.03))
    ring = np.concatenate([lab[:m].reshape(-1, 3), lab[-m:].reshape(-1, 3),
                           lab[:, :m].reshape(-1, 3), lab[:, -m:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    spread = np.linalg.norm(ring - bg, axis=1)
    if np.percentile(spread, 80) > 24:        # the border itself is busy
        return None
    dist = np.linalg.norm(lab - bg, axis=2)
    cut = max(13.0, float(np.percentile(spread, 98)) + 5.0)
    mask = (dist > cut).astype(np.uint8) * 255
    k = max(3, int(min(h, w) * 0.006)) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    # Only pinholes are closed: anything wider would join neighbouring cards.
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def _one_piece(contour: np.ndarray) -> bool:
    """Whether an outline is one four-sided thing. Two cards that touch or
    overlap at an angle trace a single outline with a notch in it; a lone
    card, rounded corners and all, has none."""
    hull = cv2.contourArea(cv2.convexHull(contour))
    return hull > 0 and cv2.contourArea(contour) >= 0.955 * hull


def _blobs(mask: np.ndarray) -> list[np.ndarray]:
    """The outline of each separate thing in the mask. Neighbouring cards are
    often joined by a thin bridge (the shadow in the gap between them, a
    corner that touches). The mask is shrunk until such bridges break, the
    pieces are told apart, and each is grown back to its own full size."""
    reach = max(7, int(min(mask.shape[:2]) * 0.03)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (reach, reach))
    core = cv2.erode(mask, kernel)
    count, labels = cv2.connectedComponents(core)
    out = []
    for label in range(1, count):
        piece = cv2.dilate((labels == label).astype(np.uint8) * 255, kernel)
        piece = cv2.bitwise_and(piece, mask)
        contours, _ = cv2.findContours(piece, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            out.append(max(contours, key=cv2.contourArea))
    return out


def _edge_quads(small: np.ndarray, frame: float) -> list[np.ndarray]:
    """Card-shaped outlines traced from edges."""
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    med = float(np.median(blurred))
    out: list[np.ndarray] = []
    for lo, hi in ((max(10.0, 0.5 * med), min(250.0, 1.3 * med + 40)), (25.0, 80.0)):
        edges = cv2.Canny(blurred, lo, hi)
        edges = cv2.morphologyEx(cv2.dilate(edges, np.ones((3, 3), np.uint8)), cv2.MORPH_CLOSE,
                                 np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            if not _MIN_AREA * frame <= cv2.contourArea(cnt) <= _MAX_AREA * frame:
                continue
            if not _one_piece(cnt):
                continue
            quad = _quad_of(cnt)
            if _is_card(quad, frame):
                out.append(quad)
    return out


# ---------------------------------------------------------------- one blob: a card, or a block of them

def _seam(grad: np.ndarray, at: int, band: int) -> float:
    """How much of a line across a block is a real edge. `grad` holds the
    step strength with the line running along axis 0. Returns the share of
    the line with a strong step in the band around it, or 0 when the line
    does not reach both ends of the block (a line printed on a card stops at
    the card's border; a seam between cards does not)."""
    lo, hi = max(0, at - band), min(grad.shape[1], at + band + 1)
    hit = grad[:, lo:hi].max(axis=1) > 50
    n = len(hit)
    end = max(3, n // 30)
    if hit[:end].mean() < 0.7 or hit[-end:].mean() < 0.7:
        return 0.0
    return float(hit.mean())


def _split(small: np.ndarray, quad: np.ndarray) -> Optional[list[np.ndarray]]:
    """A blob with no outline in it, as cards: its cells when seams run right
    across it at a rows x columns count that gives card-shaped cells, else
    [itself] when it is card-shaped, else None."""
    q = _order(quad)
    across, down = _sides(quad)
    if min(across, down) < 20:
        return None
    width = 420
    height = max(40, int(round(width * down / across)))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    flat = cv2.GaussianBlur(cv2.warpPerspective(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY),
                                                cv2.getPerspectiveTransform(q, dst), (width, height)), (3, 3), 0)
    gx = np.abs(cv2.Sobel(flat, cv2.CV_32F, 1, 0, ksize=3))        # upright seams
    gy = np.abs(cv2.Sobel(flat, cv2.CV_32F, 0, 1, ksize=3)).T      # level seams

    def at(u: float, v: float) -> np.ndarray:
        top = q[0] + (q[1] - q[0]) * u
        bottom = q[3] + (q[2] - q[3]) * u
        return top + (bottom - top) * v

    best = None
    for rows in range(1, _MAX_GRID + 1):
        for cols in range(1, _MAX_GRID + 1):
            if rows == cols == 1:
                continue
            cw, ch = across / cols, down / rows
            if abs(min(cw, ch) / max(cw, ch) - CARD_ASPECT) / CARD_ASPECT > 0.14:
                continue
            seams = [_seam(gx, round(k * width / cols), max(3, width // 60)) for k in range(1, cols)]
            seams += [_seam(gy, round(k * height / rows), max(3, height // 60)) for k in range(1, rows)]
            if min(seams) >= 0.93 and (best is None or min(seams) > best[0]):
                best = (min(seams), rows, cols)
    if best is None:
        return [quad] if _ASPECT_LO <= _aspect(quad) <= _ASPECT_HI else None
    _, rows, cols = best
    return [np.array([at(c / cols, r / rows), at((c + 1) / cols, r / rows),
                      at((c + 1) / cols, (r + 1) / rows), at(c / cols, (r + 1) / rows)], dtype=np.float32)
            for r in range(rows) for c in range(cols)]


def _blank(small: np.ndarray, quad: np.ndarray) -> bool:
    """An empty pocket or a bare patch of page: next to no detail in it."""
    patch = cv2.cvtColor(crop(small, quad), cv2.COLOR_BGR2GRAY)
    inner = patch[OUT_H // 10: -OUT_H // 10, OUT_W // 10: -OUT_W // 10]
    return float(inner.std()) < 9.0


def _grow(small: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """An outline moved out to the card's real edge. A card whose border is
    close to the background's colour is first found by the frame printed
    inside that border. The true edge is still there, fainter, a little
    further out on each side; each side moves out to it when it is found."""
    q = _portrait(quad)
    w, h = 300, int(round(300 / CARD_ASPECT))
    m = 42                                           # how far out to look: 14% of the width
    dst = np.array([[m, m], [m + w - 1, m], [m + w - 1, m + h - 1], [m, m + h - 1]], dtype=np.float32)
    to_flat = cv2.getPerspectiveTransform(q, dst)
    flat = cv2.warpPerspective(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), to_flat, (w + 2 * m, h + 2 * m),
                               borderMode=cv2.BORDER_REPLICATE)
    flat = cv2.GaussianBlur(flat, (3, 3), 0)
    gx = np.abs(cv2.Sobel(flat, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(flat, cv2.CV_32F, 0, 1, ksize=3))
    rows = slice(m + h // 6, m + h - h // 6)
    cols = slice(m + w // 6, m + w - w // 6)

    def outer(profile: np.ndarray) -> int:
        """profile[i] is the edge strength i+3 pixels outside the side.
        Returns how far out the outermost clear edge is, or 0."""
        floor = float(np.median(profile))
        for i in range(len(profile) - 1, -1, -1):
            if profile[i] > max(14.0, 3.0 * floor) and profile[i] >= profile[max(0, i - 1):i + 2].max():
                return i + 3
        return 0

    span = m - 4
    left = outer(gx[rows, m - 3 - span:m - 3].mean(axis=0)[::-1])
    right = outer(gx[rows, m + w + 2:m + w + 2 + span].mean(axis=0))
    top = outer(gy[m - 3 - span:m - 3, cols].mean(axis=1)[::-1])
    bottom = outer(gy[m + h + 2:m + h + 2 + span, cols].mean(axis=1))
    if not (left or right or top or bottom):
        return quad
    grown = np.array([[m - left, m - top], [m + w - 1 + right, m - top],
                      [m + w - 1 + right, m + h - 1 + bottom], [m - left, m + h - 1 + bottom]], dtype=np.float32)
    out = cv2.perspectiveTransform(grown.reshape(-1, 1, 2), np.linalg.inv(to_flat)).reshape(4, 2)
    # Believe it only if the result is still card-shaped.
    if abs(_aspect(out) - CARD_ASPECT) > max(0.05, abs(_aspect(quad) - CARD_ASPECT) + 0.02):
        return quad
    return out.astype(np.float32)


# ---------------------------------------------------------------- the photo

def _reading_order(quads: list[np.ndarray]) -> list[tuple[np.ndarray, int, int]]:
    """(quad, row, col), rows top to bottom and cards left to right in each."""
    if not quads:
        return []
    centres = [_centre(q) for q in quads]
    height = float(np.median([max(_sides(q)) for q in quads]))
    rows: list[list[int]] = []
    for i in sorted(range(len(quads)), key=lambda i: centres[i][1]):
        if rows and abs(centres[i][1] - np.mean([centres[j][1] for j in rows[-1]])) < 0.5 * height:
            rows[-1].append(i)
        else:
            rows.append([i])
    return [(quads[i], r, c) for r, row in enumerate(rows)
            for c, i in enumerate(sorted(row, key=lambda i: centres[i][0]))]


def _cards_among(outlines: list[np.ndarray], separate: list[np.ndarray]) -> list[np.ndarray]:
    """The outlines that are cards, by what contains what. `separate` are the
    outlines known to be separate objects lying on the background."""
    # One of each: near-identical outlines (the two edge passes, the
    # background's version of the same card) collapse to the largest.
    outlines = sorted(outlines, key=lambda o: (not any(o is s for s in separate), -_area(o)))
    unique: list[np.ndarray] = []
    for o in outlines:
        if any(np.linalg.norm(_centre(o) - _centre(u)) < 0.12 * _short(u)
               and 0.8 * _area(u) < _area(o) < 1.25 * _area(u) for u in unique):
            continue
        unique.append(o)
    unique.sort(key=_area, reverse=True)

    # parent[i]: the smallest outline that holds outline i.
    parent = [-1] * len(unique)
    for i, o in enumerate(unique):
        for k in range(i - 1, -1, -1):          # larger ones, smallest first
            if _area(unique[k]) > 1.08 * _area(o) and _inside(o, unique[k]):
                parent[i] = k
                break
    children: dict[int, list[int]] = {}
    for i, k in enumerate(parent):
        children.setdefault(k, []).append(i)

    def is_separate(q: np.ndarray) -> bool:
        return any(q is s for s in separate)

    def holds_cards(i: int) -> bool:
        """A page or a block, not a card: three or more like-sized card
        outlines inside it, or two that are known to be separate objects.
        Two like-sized boxes alone prove nothing, since many cards are
        printed that way (art box above, text box below)."""
        kids = children.get(i, [])
        if len(kids) < 2:
            return False
        areas = [_area(unique[k]) for k in kids]
        mid = float(np.median(areas))
        alike = [k for k, a in zip(kids, areas) if mid / 1.3 <= a <= mid * 1.3]
        return len(alike) >= 3 or (len(alike) == 2 and all(is_separate(unique[k]) for k in alike))

    cards: list[np.ndarray] = []

    def visit(i: int) -> None:
        if holds_cards(i):
            for k in children.get(i, []):
                visit(k)
        else:
            cards.append(unique[i])

    for i in children.get(-1, []):
        visit(i)
    return cards


def _even_out(cards: list[np.ndarray]) -> list[np.ndarray]:
    """Cards in one photo are one size, so an outline clearly smaller than
    the rest was found by the frame inside the card's border. It is scaled
    out, about its own centre, to the size of the larger ones."""
    if len(cards) < 3:
        return cards
    shorts = np.array([_short(q) for q in cards])
    target = float(np.percentile(shorts, 75))
    out = []
    for q, s in zip(cards, shorts):
        if 0.87 * target <= s < 0.955 * target:
            c = _centre(q)
            q = (c + (q.reshape(4, 2) - c) * (target / s)).astype(np.float32)
        out.append(q)
    return out


def _fill_grid(small: np.ndarray, mask: Optional[np.ndarray], cards: list[np.ndarray]) -> list[np.ndarray]:
    """Cards the outlines missed, where the ones found make a regular grid
    with holes in it. A hole is filled when something is there: it is not
    background, and it is not a blank patch."""
    if len(cards) < 3:
        return []
    centres = np.array([_centre(q) for q in cards])
    wide = float(np.median([_sides(_portrait(q))[0] for q in cards]))
    tall = float(np.median([_sides(_portrait(q))[1] for q in cards]))

    def pitch(values: np.ndarray, size: float) -> Optional[float]:
        """The step between neighbouring rows (or columns), from the gaps
        between the found cards' centres."""
        steps = []
        for a in values:
            near = [abs(a - b) for b in values if 0.6 * size < abs(a - b) < 1.6 * size]
            if near:
                steps.append(min(near))
        return float(np.median(steps)) if steps else None

    dx, dy = pitch(centres[:, 0], wide), pitch(centres[:, 1], tall)
    if dx is None and dy is None:
        return []
    dx, dy = dx or wide * 1.08, dy or tall * 1.08
    x0, y0 = float(centres[:, 0].min()), float(centres[:, 1].min())
    cols = np.round((centres[:, 0] - x0) / dx).astype(int)
    rows = np.round((centres[:, 1] - y0) / dy).astype(int)
    # Only a real grid: every found card sits close to its grid point.
    off = np.hypot(centres[:, 0] - (x0 + cols * dx), centres[:, 1] - (y0 + rows * dy))
    if off.max() > 0.22 * wide or cols.max() > _MAX_GRID or rows.max() > _MAX_GRID:
        return []
    taken = set(zip(rows.tolist(), cols.tolist()))
    # Where each row and column really sits, from the cards in it.
    xs = {c: float(np.mean(centres[cols == c, 0])) for c in set(cols.tolist())}
    ys = {r: float(np.mean(centres[rows == r, 1])) for r in set(rows.tolist())}
    added = []
    for r in range(rows.max() + 1):
        for c in range(cols.max() + 1):
            if (r, c) in taken or r not in ys or c not in xs:
                continue
            cx, cy = xs[c], ys[r]
            if mask is not None and not mask[int(np.clip(cy, 0, mask.shape[0] - 1)), int(np.clip(cx, 0, mask.shape[1] - 1))]:
                continue
            quad = np.array([[cx - wide / 2, cy - tall / 2], [cx + wide / 2, cy - tall / 2],
                             [cx + wide / 2, cy + tall / 2], [cx - wide / 2, cy + tall / 2]], dtype=np.float32)
            if not _blank(small, quad):
                added.append(quad)
    return added


def find_cards(img: np.ndarray) -> list[dict]:
    """Every card in the photo: [{"quad": 4x2 corners in the photo's own
    pixels, "row": r, "col": c}], in reading order. Empty when none is found."""
    h, w = img.shape[:2]
    scale = min(1.0, _WORK_SIDE / max(h, w))
    small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale)))) if scale < 1 else img
    frame = float(small.shape[0] * small.shape[1])

    outlines = _edge_quads(small, frame)
    blobs: list[np.ndarray] = []          # each separate thing on the background
    lone: list[np.ndarray] = []           # those that are one card-shaped piece
    mask = _background_mask(small)
    if mask is not None:
        for cnt in _blobs(mask):
            if cv2.contourArea(cnt) < _MIN_AREA * frame:
                continue
            quad = _quad_of(cnt)
            blobs.append(quad)
            if _one_piece(cnt) and _is_card(quad, frame):
                lone.append(quad)
    cards = _cards_among(outlines + lone, lone)

    # A blob with no card found in it: cards lying against each other with
    # nothing traced between them, or a card the edges missed.
    for blob in blobs:
        if any(_inside(c, blob) for c in cards):
            continue
        cells = _split(small, blob)
        if cells and all(_is_card(c, frame) for c in cells):
            cards.extend(cells if len(cells) == 1 else [c for c in cells if not _blank(small, c)])
    if not cards:
        return []

    # Cards in one photo are one size: keep the largest group of like-sized
    # ones (by count, then by how much of the photo they cover).
    areas = [_area(q) for q in cards]
    groups = [[b for b, other in enumerate(areas) if a / 1.6 <= other <= a * 1.6] for a in areas]
    best = max(groups, key=lambda g: (len(g), sum(areas[b] for b in g)))
    cards = [cards[b] for b in best]

    cards = [_grow(small, q) for q in cards]
    cards = [q for q in cards if not _blank(small, q)]
    cards = _even_out(cards)
    cards += _fill_grid(small, mask, cards)
    return [{"quad": (quad / scale).astype(np.float32), "row": r, "col": c}
            for quad, r, c in _reading_order(cards)]


def contact_sheet(crops: list[np.ndarray], cols: int = 3) -> np.ndarray:
    """The squared-up cards laid out in a plain grid, `cols` across, in the
    order given. Page-level identification reads this instead of the photo:
    whatever the layout on the table was, card i sits at row i // cols,
    column i % cols."""
    rows = max(1, -(-len(crops) // cols))
    gap = 16
    sheet = np.full((rows * (OUT_H + gap) + gap, cols * (OUT_W + gap) + gap, 3), 245, np.uint8)
    for i, card in enumerate(crops):
        r, c = divmod(i, cols)
        y, x = gap + r * (OUT_H + gap), gap + c * (OUT_W + gap)
        sheet[y:y + OUT_H, x:x + OUT_W] = card
    return sheet
