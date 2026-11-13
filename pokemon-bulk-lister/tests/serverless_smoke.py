"""The scan bench end to end as it runs on a serverless host.

Not collected by pytest: tests/test_serverless.py runs this file in a fresh
interpreter, because the app decides where it is running when it is first
imported. The caller sets VERCEL=1, DATA_ROOT, DATABASE_URL, SECRET_KEY,
BLOB_READ_WRITE_TOKEN, CRON_SECRET and JOB_SLICE_SECONDS=0 (one unit of work
per status request, so every job here is resumed many times).

Vercel Blob, the vision model, the price sources and eBay are replaced with
in-memory stand-ins. Everything else is the real code.
"""
import io
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from webapp import runtime  # noqa: E402

assert runtime.SERVERLESS and str(runtime.DATA_ROOT) == os.environ["DATA_ROOT"]


class FakeBlob:
    """vercel.blob.sync, as far as the app uses it."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def put_blob(self, pathname, body, *, access, allow_overwrite=False, content_type=None):
        assert access == "public" and allow_overwrite
        self.files[pathname] = bytes(body)
        return type("Put", (), {"url": f"https://store1.public.blob.vercel-storage.com/{pathname}"})()

    def get_blob(self, url_or_pathname, *, access):
        key = url_or_pathname.split(".vercel-storage.com/", 1)[-1]
        return type("Get", (), {"body": self.files[key]})()


blob = FakeBlob()
runtime._blob = lambda: blob

import lib.vision_identify as vision  # noqa: E402
from webapp import app as appmod  # noqa: E402
from webapp import auth, connections, db, models  # noqa: E402

client = appmod.app.test_client()
DATA = runtime.DATA_ROOT


def new_instance():
    """What a recycled instance looks like: the working files are gone."""
    shutil.rmtree(DATA / "output" / "crops", ignore_errors=True)
    shutil.rmtree(DATA / "output" / "pages", ignore_errors=True)
    shutil.rmtree(DATA / "output" / "csvs", ignore_errors=True)
    shutil.rmtree(DATA / "input", ignore_errors=True)
    for d in (appmod.CROPS_DIR, appmod.CSVS_DIR, appmod.INPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cards():
    with db.connect(appmod.DB_PATH) as conn:
        return db.list_cards(conn, sort="oldest")


def run_to_end(status_url, limit=200):
    polls = 0
    while True:
        polls += 1
        status = client.get(status_url).get_json()
        assert "queue" not in status and "lease" not in status       # internals stay inside
        if not status["running"]:
            return status, polls
        assert polls < limit, status


# ---------------------------------------------------------------- upload: photos go to Blob
page = np.full((1500, 1100, 3), 235, np.uint8)
for r in range(3):
    for c in range(3):
        x, y = 60 + c * 340, 60 + r * 470
        cv2.rectangle(page, (x, y), (x + 300, y + 420), (40 + 60 * c, 90 + 40 * r, 200 - 50 * c), -1)
ok, jpg = cv2.imencode(".jpg", page)
r = client.post("/api/grids/upload", data={"file": (io.BytesIO(jpg.tobytes()), "IMG_1.jpg"), "mode": "grid"},
                content_type="multipart/form-data")
assert r.status_code == 200, r.get_data(as_text=True)
n = r.get_json()["total_crops"]
assert n >= 3, r.get_json()
crop_keys = sorted(k for k in blob.files if k.startswith("output/crops/"))
assert len(crop_keys) == n and "output/pages/IMG_1.jpg" in blob.files, sorted(blob.files)
assert all(c["crop_path"] in blob.files for c in cards())

# The same camera filename again must not overwrite the first page, even on
# an instance that has never seen the first one's files.
new_instance()
r = client.post("/api/grids/upload", data={"file": (io.BytesIO(jpg.tobytes()), "IMG_1.jpg"), "mode": "single"},
                content_type="multipart/form-data")
assert r.get_json()["grids"][0]["filename"] == "IMG_1-2.jpg", r.get_json()
total = n + 1

# ---------------------------------------------------------------- a photo from Google Drive is filed like an upload
from webapp import drive as drive_mod  # noqa: E402

assert client.post("/api/drive/import/photo", json={"file_id": "p1"}).status_code == 401      # needs a login


class FakeDrive:
    def download(self, file_id, max_bytes):
        assert max_bytes == 40 * 1024 * 1024
        if file_id == "doc":
            return b"%PDF", {"name": "notes.pdf"}
        return jpg.tobytes(), {"name": "IMG_1.jpg"}                    # the same camera name a third time


drive_mod.drive_for = lambda uid: FakeDrive()
appmod.app.config["TESTING"] = True
with client.session_transaction() as sess:
    sess["uid"] = 1                                                    # some signed-in account
real_uid = auth.current_user_id
auth.current_user_id = lambda: 1
r = client.post("/api/drive/import/photo", json={"file_id": "p1", "mode": "single"})
assert r.status_code == 200, r.get_data(as_text=True)
assert r.get_json()["grids"] == [{"filename": "IMG_1-3.jpg", "crops": 1}], r.get_json()
assert "output/crops/IMG_1-3_r0c0.jpg" in blob.files
assert client.post("/api/drive/import/photo", json={"file_id": "doc"}).get_json()["skipped"] == ["notes.pdf"]
auth.current_user_id = real_uid
with client.session_transaction() as sess:
    sess.clear()
total += 1
crop_keys = sorted(k for k in blob.files if k.startswith("output/crops/"))

# ---------------------------------------------------------------- photos are served from Blob
new_instance()
r = client.get("/" + crop_keys[0])
assert r.status_code == 302 and r.headers["Location"].endswith(crop_keys[0]), r.status_code
assert client.get("/output/crops/nope.jpg").status_code == 404

# ---------------------------------------------------------------- scan job, one slice per status request
seen_files = []


def fake_page(path):
    assert Path(path).exists(), path                       # fetched back from Blob
    seen_files.append(Path(path).name)
    return [{"row": r_, "col": c_, "name": f"Card {r_}{c_}", "set_name": "Test Set", "set_code": "tst",
             "card_number": str(r_ * 3 + c_ + 1), "rarity": "Common", "is_holo": False, "confidence": 0.95}
            for r_ in range(3) for c_ in range(3)]


def fake_crop(path):
    assert Path(path).exists(), path
    seen_files.append(Path(path).name)
    return {"name": "Single Card", "set_name": "Test Set", "set_code": "tst", "card_number": "99",
            "rarity": "Rare", "is_holo": True, "confidence": 0.9}


vision.identify_page, vision.identify_crop = fake_page, fake_crop
priced = []


def fake_price(card):
    priced.append(card["id"])
    return {"final_price": 1.25, "pricing_confidence": 0.8, "tcgplayer_market": 1.25}, None


appmod._price_card_pass1 = fake_price

assert client.get("/api/pricing/status").get_json()["running"] is False
assert client.post("/api/scan/process").get_json() == {"started": True}
assert client.post("/api/scan/process").status_code == 409            # one job at a time, across instances
assert client.post("/api/pricing/run-all").status_code == 409
status, polls = run_to_end("/api/pricing/status")
assert polls >= 4, polls                                               # identify x2, switch, price, finish
assert f"identified {total}/{total}" in status["message"] and f"priced {total} card(s)" in status["message"], status
assert "IMG_1.jpg" in seen_files                                       # the page photo, for the 9-card call
after = cards()
assert all(c["name"] and c["final_price"] == 1.25 for c in after) and len(priced) == total
assert {c["name"] for c in after} >= {"Card 00", "Single Card"}

# Running again finds nothing to do and spends nothing.
calls = len(seen_files)
client.post("/api/scan/process")
status, _ = run_to_end("/api/pricing/status")
assert status["message"] == "nothing to identify; nothing to price" and len(seen_files) == calls, status

# A slice that died mid-run (lease never released) is picked up once the lease runs out.
client.post("/api/pricing/run-all", json={})
state = runtime.state_get(appmod.MAIN_JOB)
state["lease"] = 4102444800.0                                          # held "forever" by a dead instance
runtime.state_set(appmod.MAIN_JOB, state)
assert client.get("/api/pricing/status").get_json()["running"] is True
assert runtime.state_get(appmod.MAIN_JOB)["progress"] == 0             # nobody else may work it
state["lease"] = 1.0                                                   # expired
runtime.state_set(appmod.MAIN_JOB, state)
status, _ = run_to_end("/api/pricing/status")
assert status["message"] == f"done, priced {total} card(s)", status

# ---------------------------------------------------------------- CSV download from another instance
r = client.post("/api/export/csvs")
assert r.status_code == 200, r.get_data(as_text=True)
new_instance()
r = client.get("/output/csvs/whatnot_seller_hub.csv")
assert r.status_code == 200 and b"Card 00" in r.data, r.status_code

# ---------------------------------------------------------------- TCGplayer export, sent in parts, kept in the database
header = "TCGplayer Id,Product Line,Set Name,Product Name,Number,Condition\n"
part1 = header + "501,Pokemon,T01: Test Set,Card 00,1/99,Near Mint\n502,Magic,X,Y,1,Near Mint\n"
part2 = header + "503,Pokemon,T01: Test Set,Card 01,2/99,Near Mint\n"
for i, text in enumerate((part1, part2), start=1):
    r = client.post("/api/tcgplayer/catalog", content_type="multipart/form-data",
                    data={"file": (io.BytesIO(text.encode()), "e.csv"), "part": str(i), "parts": "2"})
    assert r.status_code == 200, r.get_data(as_text=True)
info = r.get_json()
assert (info["loaded"], info["rows"], info["matched"]) == (True, 2, 2), info
assert not (DATA / "output" / "cache" / "tcgplayer_catalog.csv").exists()
r = client.post("/api/tcgplayer/catalog", content_type="multipart/form-data",
                data={"file": (io.BytesIO(part2.encode()), "e.csv")})            # a new upload replaces
assert r.get_json()["rows"] == 1

# ---------------------------------------------------------------- eBay token: sealed in the database, no file
token_path = runtime.data_path("output/cache/ebay_user_token.json")
os.environ.update(EBAY_CLIENT_ID="app", EBAY_CLIENT_SECRET="cert", EBAY_REDIRECT_URI="ru")
first = connections._ebay_auth()
assert not first.is_authorized()
first._save_refresh_token("v^1.1#REFRESH", 3600)
assert not token_path.exists()
sealed = runtime.state_get("ebay-token:operator")["sealed"]
assert "REFRESH" not in sealed
assert connections._ebay_auth().is_authorized() and connections._ebay_auth()._refresh_token == "v^1.1#REFRESH"
real_key = os.environ["SECRET_KEY"]
os.environ["SECRET_KEY"] = "a different key"
assert not connections._ebay_auth().is_authorized()                   # unreadable: reconnect, never a crash
os.environ["SECRET_KEY"] = real_key

# ---------------------------------------------------------------- eBay publish, one card per slice
import lib.ebay_lister as ebay_lister  # noqa: E402

published = []


class FakeLister:
    def __init__(self, auth=None):
        pass

    def publish_card(self, card):
        if card["name"] == "Card 01":
            raise ebay_lister.EbayListingError("missing policy")
        published.append(card["name"])
        return {"listing_id": f"L{len(published)}", "offer_id": "O", "url": "https://ebay.example/x"}


ebay_lister.EbayLister = FakeLister
with db.connect(appmod.DB_PATH) as conn:
    for c in cards():
        db.update_card(conn, c["id"], {"image_url": "https://img.example/x.jpg"})
r = client.post("/api/publish/ebay", json={"expected": total + 5})
assert r.status_code == 200
status, _ = run_to_end("/api/publish/job-status")
assert status["message"].startswith("nothing listed") and not published, status
client.post("/api/publish/ebay", json={"expected": total})
assert client.post("/api/publish/ebay", json={"expected": total}).status_code == 409
status, polls = run_to_end("/api/publish/job-status")
assert polls >= total and len(published) == total - 1, (polls, published)
assert status["message"].startswith(f"done: {total - 1} of {total} listed on eBay, 1 failed"), status
assert sum(1 for c in cards() if c["ebay_listing_id"]) == total - 1
runtime.forget_ebay_token("operator", token_path)
assert not connections._ebay_auth().is_authorized()

# ---------------------------------------------------------------- what a hosted install cannot do says so
r = client.post("/api/publish/portal", json={"site": "whatnot"})
assert r.status_code == 409 and "hosted install" in r.get_json()["error"]
assert client.post("/api/connections/portal/whatnot/login").status_code == 409
session = client.get("/api/session").get_json()
assert session["max_upload_bytes"] == 4_000_000 and session["hosted"] is True

# ---------------------------------------------------------------- the daily cron
assert client.get("/api/cron/daily").status_code == 401
assert client.get("/api/cron/daily", headers={"Authorization": "Bearer wrong"}).status_code == 401
r = client.get("/api/cron/daily", headers={"Authorization": f"Bearer {os.environ['CRON_SECRET']}"})
assert r.status_code == 200 and "prices_refreshed" in r.get_json(), r.get_data(as_text=True)

# ---------------------------------------------------------------- under the public host the bench needs a login
os.environ["VERCEL_PROJECT_PRODUCTION_URL"] = "bulk.example.com"
public = {"Host": "bulk.example.com", "X-Forwarded-Proto": "https"}
r = client.get("/", headers=public)
assert r.status_code == 302 and "/login" in r.headers["Location"], r.status_code
assert client.get("/api/cards", headers=public).status_code == 401
assert client.get("/api/pricing/status", headers=public).status_code == 401   # a stranger cannot drive jobs
assert client.get("/login", headers=public).status_code == 200
# The scheduler has no login; its secret is what lets it in, and only that.
assert client.get("/api/cron/daily", headers=public).status_code == 401
assert client.get("/api/cron/daily", headers={
    **public, "Authorization": f"Bearer {os.environ['CRON_SECRET']}"}).status_code == 200
assert client.get("/login", headers={"Host": "evil.example"}).status_code == 403

# ---------------------------------------------------------------- each account has its own bench
def account(email):
    c = appmod.app.test_client()
    r = c.post("/register", json={"email": email, "password": "password123"}, headers=public)
    assert r.status_code == 200, r.get_data(as_text=True)
    s = models.get_session()
    try:
        return c, s.query(models.User).filter_by(email=email).one().id
    finally:
        s.close()


def bench_card(uid, stem):
    db.set_owner(uid)
    try:
        with db.connect(appmod.DB_PATH) as conn:
            grid = db.get_or_create_grid(conn, f"{stem}.jpg", f"input/{stem}.jpg")
            card = db.insert_card_stub(conn, grid, f"output/crops/{stem}_r0c0.jpg", 0, 0)
            db.update_card(conn, card, {"name": f"Card of {stem}"})
            return card
    finally:
        db.set_owner(None)


ann, ann_id = account("ann@example.com")
ben, ben_id = account("ben@example.com")
ann_card, ben_card = bench_card(ann_id, "annpage"), bench_card(ben_id, "benpage")
assert ann.get("/", headers=public).status_code == 200            # no operator needed to scan
mine = ann.get("/api/cards", headers=public).get_json()
assert [c["id"] for c in mine["cards"]] == [ann_card] and mine["stats"]["total"] == 1, mine
assert [c["id"] for c in ben.get("/api/cards", headers=public).get_json()["cards"]] == [ben_card]
assert ann.get(f"/api/cards/{ben_card}", headers=public).status_code == 404
assert ann.patch(f"/api/cards/{ben_card}", json={"name": "mine now"}, headers=public).status_code == 404
assert ann.post("/api/cards/bulk", headers=public, json=[
    {"crop_path": "output/crops/benpage_r0c0.jpg", "name": "mine now"}]).get_json() == {"applied": 0}
assert ben.get(f"/api/cards/{ben_card}", headers=public).get_json()["name"] == "Card of benpage"
# One account's job is not the other's.
assert ann.post("/api/pricing/run-all", headers=public).status_code == 200
assert runtime.state_get(f"{appmod.MAIN_JOB}:u{ann_id}")["queue"] == [ann_card]
assert ben.get("/api/pricing/status", headers=public).get_json()["running"] is False
runtime.state_set(f"{appmod.MAIN_JOB}:u{ann_id}", {"running": False})
# Exports are per account, and the publishing side stays with the operator.
written = ann.post("/api/export/csvs", headers=public).get_json()["written"]
assert all(f"/u{ann_id}/" in w["file"] for w in written), written
assert ann.get("/" + written[0]["file"], headers=public).status_code == 200
assert ben.get("/" + written[0]["file"], headers=public).status_code == 404
session = ann.get("/api/session", headers=public).get_json()
assert session["logged_in"] is True and session["operator"] is False
assert ann.get("/api/publish/status", headers=public).status_code == 403
assert ann.get("/api/connections", headers=public).status_code == 403
assert ann.post("/api/publish/ebay", headers=public).status_code == 403

# ---------------------------------------------------------------- no SECRET_KEY, no start
del os.environ["SECRET_KEY"]
try:
    auth.ensure_secret_key(appmod.app)
except RuntimeError as exc:
    assert "SECRET_KEY" in str(exc)
else:
    raise AssertionError("a serverless install must refuse to start without SECRET_KEY")

assert models.get_session().query(models.StoredFile).count() == len(blob.files)
print("serverless smoke ok")
