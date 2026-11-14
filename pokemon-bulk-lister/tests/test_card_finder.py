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
