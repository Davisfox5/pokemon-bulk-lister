"""lib/card_finder: cards found in a photo whatever the layout."""
import cv2
import numpy as np
import pytest

from lib import card_finder

CW, CH = 252, 352


def _card(rng):
    border = [(60, 210, 245), (200, 205, 210), (40, 160, 60), (180, 60, 60)][int(rng.integers(4))]
    img = np.full((CH, CW, 3), border, np.uint8)
    cv2.rectangle(img, (13, 13), (CW - 14, CH - 14), tuple(int(x) for x in rng.integers(70, 230, 3)), -1)
    art = cv2.resize(rng.integers(0, 255, (10, 14, 3)).astype(np.uint8), (CW - 44, 136), interpolation=cv2.INTER_CUBIC)
    img[40:176, 22:CW - 22] = art
    for y in range(200, 320, 20):
        cv2.line(img, (24, y), (int(rng.integers(120, CW - 24)), y), (30, 30, 30), 2)
    return img


def _photo(rows, cols, gap=22, tilt=3.0, seed=1, background=(42, 40, 44)):
    """rows x cols cards on a plain surface. Returns (photo, true corners)."""
    rng = np.random.default_rng(seed)
    width = cols * CW + (cols - 1) * gap + 440
    height = rows * CH + (rows - 1) * gap + 440
    photo = np.full((height, width, 3), background, np.uint8)
    photo = np.clip(photo + rng.normal(0, 3, photo.shape), 0, 255).astype(np.uint8)
    truth = []
    for r in range(rows):
        for c in range(cols):
            centre = (220 + c * (CW + gap) + CW / 2, 220 + r * (CH + gap) + CH / 2)
            m = cv2.getRotationMatrix2D((CW / 2, CH / 2), float(rng.normal(0, tilt)), 1.0)
            m[0, 2] += centre[0] - CW / 2
            m[1, 2] += centre[1] - CH / 2
            card = _card(rng)
            warped = cv2.warpAffine(card, m, (width, height))
            mask = cv2.warpAffine(np.full((CH, CW), 255, np.uint8), m, (width, height))
            photo[mask > 127] = warped[mask > 127]
            corners = np.array([[0, 0], [CW, 0], [CW, CH], [0, CH]], np.float32)
            truth.append((m[:, :2] @ corners.T).T + m[:, 2])
    return photo, truth


def _overlap(a, b, shape):
    ma, mb = np.zeros(shape[:2], np.uint8), np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(ma, [np.int32(a)], 1)
    cv2.fillPoly(mb, [np.int32(b)], 1)
    return int((ma & mb).sum()) / max(1, int((ma | mb).sum()))


@pytest.mark.parametrize("rows,cols", [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 3)])
def test_every_card_is_found_whatever_the_layout(rows, cols):
    photo, truth = _photo(rows, cols)
    found = card_finder.find_cards(photo)
    assert len(found) == rows * cols
    for corners in truth:
        assert max(_overlap(corners, f["quad"], photo.shape) for f in found) > 0.8
    # Reading order: top row first, left to right.
    centres = [f["quad"].mean(axis=0) for f in found]
    assert [(f["row"], f["col"]) for f in found] == [(r, c) for r in range(rows) for c in range(cols)]
    assert all(centres[i][1] < centres[i + cols][1] for i in range(len(centres) - cols))


def test_a_card_is_one_card_not_its_two_printed_boxes():
    photo, truth = _photo(1, 1, tilt=12.0, seed=5, background=(225, 228, 226))
    found = card_finder.find_cards(photo)
    assert len(found) == 1 and _overlap(truth[0], found[0]["quad"], photo.shape) > 0.8


def test_crops_are_upright_and_one_size():
    photo, _ = _photo(1, 2, tilt=8.0, seed=3)
    for f in card_finder.find_cards(photo):
        assert card_finder.crop(photo, f["quad"]).shape == (card_finder.OUT_H, card_finder.OUT_W, 3)


def test_an_empty_photo_has_no_cards():
    blank = np.full((900, 700, 3), (120, 118, 121), np.uint8)
    assert card_finder.find_cards(blank) == []


def test_contact_sheet_puts_card_i_at_row_i_over_3():
    crops = [np.full((card_finder.OUT_H, card_finder.OUT_W, 3), 10 * i, np.uint8) for i in range(5)]
    sheet = card_finder.contact_sheet(crops)
    assert sheet.shape[0] == 2 * (card_finder.OUT_H + 16) + 16 and sheet.shape[1] == 3 * (card_finder.OUT_W + 16) + 16
    y, x = 16 + (card_finder.OUT_H + 16) + 50, 16 + (card_finder.OUT_W + 16) + 50      # row 1, col 1: card 4
    assert int(sheet[y, x, 0]) == 40


def test_a_card_held_in_a_hand_over_a_busy_room():
    """No clean background and no closed outline: fingers cover parts of the
    card's sides and the room behind is out of focus."""
    rng = np.random.default_rng(11)
    room = cv2.resize(rng.integers(40, 220, (9, 7, 3)).astype(np.uint8), (700, 900), interpolation=cv2.INTER_CUBIC)
    room = cv2.GaussianBlur(room, (0, 0), 9)
    cv2.rectangle(room, (520, 380), (700, 900), (150, 150, 155), -1)      # a blurred laptop
    room = cv2.GaussianBlur(room, (0, 0), 5)
    skin = (150, 170, 215)
    cv2.ellipse(room, (330, 640), (290, 330), 0, 0, 360, skin, -1)        # the palm
    x, y = 230, 250
    card = _card(rng)
    # Printed art is fine detail, in focus; so is the rules text under it.
    card[40:176, 22:CW - 22] = cv2.resize(rng.integers(0, 255, (68, 104, 3)).astype(np.uint8), (CW - 44, 136))
    cv2.putText(card, "Cardname  HP 70", (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 2)
    for line in range(196, 330, 12):                                     # rules text, as a real card has
        for word in range(26, CW - 40, 34):
            cv2.putText(card, "text", (word, line), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (25, 25, 25), 1)
    room[y:y + CH, x:x + CW] = card
    for cy in (330, 420, 510):                                           # fingertips over the right side
        cv2.ellipse(room, (x + CW + 10, cy), (38, 30), 0, 0, 360, skin, -1)
    cv2.ellipse(room, (x - 8, 470), (34, 70), 0, 0, 360, skin, -1)        # the thumb over the left side
    truth = np.array([[x, y], [x + CW, y], [x + CW, y + CH], [x, y + CH]], np.float32)

    found = card_finder.find_cards(room)
    assert len(found) == 1
    assert _overlap(truth, found[0]["quad"], room.shape) > 0.6
    # Most of the crop is the card, not the room.
    assert cv2.contourArea(found[0]["quad"].astype(np.float32)) < 2.2 * CW * CH


def _binder_page(seed=5, glare=True, skew=0.0):
    """A 3x3 binder page held up in a room: black pockets larger than the
    cards, each card sitting a little differently in its pocket, the page
    shot at an angle, and glare joining two cards across their pockets.
    Returns (photo, true card corners)."""
    rng = np.random.default_rng(seed)
    pocket_w, pocket_h = CW + 70, CH + 60
    page_w, page_h = 3 * pocket_w + 60, 3 * pocket_h + 60
    page = np.full((page_h, page_w, 3), (34, 32, 36), np.uint8)
    page = np.clip(page + rng.normal(0, 5, page.shape), 0, 255).astype(np.uint8)
    for k in range(1, 3):                                     # pocket seams
        cv2.line(page, (30 + k * pocket_w, 20), (30 + k * pocket_w, page_h - 20), (70, 70, 74), 3)
        cv2.line(page, (20, 30 + k * pocket_h), (page_w - 20, 30 + k * pocket_h), (70, 70, 74), 3)
    corners = []
    for r in range(3):
        for c in range(3):
            x = 30 + c * pocket_w + (pocket_w - CW) // 2 + int(rng.integers(-18, 19))
            y = 30 + r * pocket_h + (pocket_h - CH) // 2 + int(rng.integers(-14, 15))
            card = _card(rng)
            card[:13], card[-13:], card[:, :13], card[:, -13:] = (205, 208, 212), (205, 208, 212), (205, 208, 212), (205, 208, 212)
            page[y:y + CH, x:x + CW] = card
            corners.append(np.array([[x, y], [x + CW, y], [x + CW, y + CH], [x, y + CH]], np.float32))
    if glare:                                                 # a streak across the top two right-hand pockets
        x0, y0 = int(corners[1][1][0]) - 20, int(corners[1][0][1]) + 30
        cv2.rectangle(page, (x0, y0), (int(corners[2][0][0]) + 40, y0 + 60), (215, 215, 220), -1)
    # The page in a room, shot at an angle.
    width, height = page_w + 500, page_h + 560
    room = np.full((height, width, 3), (188, 196, 204), np.uint8)
    room[height - 220:] = (150, 150, 158)
    room = np.clip(room + rng.normal(0, 6, room.shape), 0, 255).astype(np.uint8)
    src = np.array([[0, 0], [page_w, 0], [page_w, page_h], [0, page_h]], np.float32)
    dst = np.array([[250 + skew, 260], [250 + page_w, 260 + skew], [250 + page_w - skew, 260 + page_h],
                    [250, 260 + page_h - skew]], np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, m, (width, height))
    mask = cv2.warpPerspective(np.full((page_h, page_w), 255, np.uint8), m, (width, height))
    room[mask > 127] = warped[mask > 127]
    truth = [cv2.perspectiveTransform(q.reshape(-1, 1, 2), m).reshape(4, 2) for q in corners]
    return room, truth


@pytest.mark.parametrize("glare,skew", [(False, 0.0), (True, 0.0), (True, 60.0)])
def test_a_binder_page_gives_every_card_by_its_own_edge_not_its_pocket(glare, skew):
    photo, truth = _binder_page(glare=glare, skew=skew)
    found = card_finder.find_cards(photo)
    assert len(found) == 9
    for corners in truth:
        # Tight on the card: a pocket's outline would overlap far less.
        assert max(_overlap(corners, f["quad"], photo.shape) for f in found) > 0.9
    centres = [f["quad"].mean(axis=0) for f in found]
    assert [(f["row"], f["col"]) for f in found] == [(r, c) for r in range(3) for c in range(3)]
    assert centres[0][0] < centres[1][0] < centres[2][0] and centres[0][1] < centres[3][1] < centres[6][1]


def test_an_empty_pocket_is_not_filled_in():
    photo, truth = _binder_page(glare=False)
    hole = truth[4]
    cv2.fillPoly(photo, [np.int32(hole + [[-6, -6], [6, -6], [6, 6], [-6, 6]])], (34, 32, 36))
    found = card_finder.find_cards(photo)
    assert len(found) == 8
    assert max(_overlap(hole, f["quad"], photo.shape) for f in found) < 0.2
