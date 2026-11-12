"""Google Drive: sign-in, browsing, and importing spreadsheets from it.
Google is a stand-in that answers in the shapes its API documents."""
from urllib.parse import parse_qs, urlsplit

import pytest

from lib import google_drive
from test_accounts_sales_payouts import _events, _user, app  # noqa: F401
from webapp import drive, listing_import, runtime

SHEET, FOLDER = google_drive.SHEET, google_drive.FOLDER


class _Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeGoogle:
    """accounts.google.com, oauth2.googleapis.com and the Drive API."""

    def __init__(self):
        self.calls, self.revoked = [], []
        self.refresh_ok = True
        self.files = {
            "f1": {"id": "f1", "name": "Binder", "mimeType": FOLDER},
            "s1": {"id": "s1", "name": "My cards", "mimeType": SHEET},
            "c1": {"id": "c1", "name": "whatnot.csv", "mimeType": "text/csv", "size": "60"},
            "d1": {"id": "d1", "name": "notes.docx", "mimeType": "application/msword", "size": "10"},
        }
        self.content = {
            "s1": b"Name,Set,Number,Qty,Condition,Holo,Price paid\n"
                  b"Dragapult ex,Twilight Masquerade,130/167,2,Near Mint,yes,$9.50\n"
                  b"Dreepy,Twilight Masquerade,128,1,,,\n"
                  b",,,,,,\n"
                  b"Sold already,,,0,,,\n",
            "c1": b"Title,Quantity,Price\nCharizard holo no number,1,50.00\n",
        }

    def post(self, url, data=None, params=None, timeout=None):
        self.calls.append(("POST", url, data or params))
        if url == google_drive.REVOKE_URL:
            self.revoked.append(params["token"])
            return _Resp(200, {})
        if data["grant_type"] == "authorization_code":
            if data["code"] != "good-code":
                return _Resp(400, {"error": "invalid_grant", "error_description": "Bad Request"})
            return _Resp(200, {"access_token": "at-1", "refresh_token": "rt-SECRET", "expires_in": 3600})
        if not self.refresh_ok or data["refresh_token"] != "rt-SECRET":
            return _Resp(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        return _Resp(200, {"access_token": "at-2", "expires_in": 3600})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if headers.get("Authorization") != "Bearer at-2":
            return _Resp(401, {"error": "unauthorized"})
        params = params or {}
        if url == f"{google_drive.API}/files":
            wanted = [f for f in self.files.values()
                      if f["mimeType"] == FOLDER or
                      (f["mimeType"] in (SHEET, "text/csv") if "spreadsheet" in params["q"] else f["mimeType"].startswith("image/"))]
            return _Resp(200, {"files": wanted, "nextPageToken": None})
        file_id = url.split("/files/")[1].split("/")[0]
        if file_id not in self.files:
            return _Resp(404, {"error": "notFound"})
        if url.endswith("/export"):
            assert params == {"mimeType": "text/csv"}
            return _Resp(200, content=self.content[file_id])
        if params.get("alt") == "media":
            return _Resp(200, content=self.content[file_id])
        return _Resp(200, self.files[file_id])


@pytest.fixture
def google(app, monkeypatch):  # noqa: F811
    fake = FakeGoogle()
    monkeypatch.setattr(google_drive.requests, "post", fake.post)
    monkeypatch.setattr(google_drive.requests, "get", fake.get)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    app.register_blueprint(drive.drive_bp)
    app.register_blueprint(listing_import.listings_bp)
    app.google = fake
    return app


def _connect(client):
    r = client.get("/api/drive/connect?next=/portfolio")
    q = parse_qs(urlsplit(r.headers["Location"]).query)
    return client.get(f"/api/drive/callback?state={q['state'][0]}&code=good-code"), q


def test_connecting_asks_google_for_read_only_access_and_keeps_the_token_sealed(google):
    me = _user(google, "a@example.com")
    assert me.get("/api/drive/status").get_json() == {"configured": True, "connected": False}
    assert google.test_client().get("/api/drive/status").status_code == 401

    r, q = _connect(me)
    assert q["scope"] == [google_drive.SCOPE] and q["scope"][0].endswith("drive.readonly")
    assert (q["access_type"], q["prompt"], q["response_type"]) == (["offline"], ["consent"], ["code"])
    assert q["redirect_uri"] == ["http://localhost/api/drive/callback"]
    assert r.headers["Location"] == "/portfolio?drive=connected"
    assert me.get("/api/drive/status").get_json()["connected"] is True

    # What is stored cannot be read without the app's secret key, and no route returns it.
    stored = [v for k, v in ((k, runtime.state_get(k)) for k in ("google-token:user-1",)) if v][0]
    assert "rt-SECRET" not in stored["sealed"]
    assert "rt-SECRET" not in me.get("/api/drive/status").get_data(as_text=True)

    # Another account is not connected by this.
    other = _user(google, "b@example.com")
    assert other.get("/api/drive/status").get_json()["connected"] is False
    assert other.get("/api/drive/files?kind=sheets").status_code == 409


def test_a_sign_in_this_browser_did_not_start_is_refused(google):
    me = _user(google, "a@example.com")
    me.get("/api/drive/connect?next=/portfolio")
    assert me.get("/api/drive/callback?state=forged&code=good-code").headers["Location"] == "/portfolio?drive=failed"
    assert me.get("/api/drive/callback?state=forged&code=good-code").headers["Location"].endswith("drive=failed")
    assert me.get("/api/drive/status").get_json()["connected"] is False

    r = me.get("/api/drive/connect?next=/portfolio")
    state = parse_qs(urlsplit(r.headers["Location"]).query)["state"][0]
    assert me.get(f"/api/drive/callback?state={state}&error=access_denied").headers["Location"].endswith("drive=declined")
    # Only a path on this site is ever returned to.
    assert me.get("/api/drive/connect?next=//evil.example").headers["Location"].startswith(google_drive.AUTH_URL)
    state = parse_qs(urlsplit(me.get("/api/drive/connect?next=//evil.example").headers["Location"]).query)["state"][0]
    assert me.get(f"/api/drive/callback?state={state}&code=good-code").headers["Location"] == "/portfolio?drive=connected"


def test_without_google_credentials_the_feature_says_so(google, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    me = _user(google, "a@example.com")
    assert me.get("/api/drive/status").get_json() == {"configured": False, "connected": False}
    assert me.get("/api/drive/connect?next=/").headers["Location"] == "/?drive=not-set-up"
    assert me.get("/api/drive/files").status_code == 400


def test_browsing_lists_folders_and_only_the_kind_of_file_asked_for(google):
    me = _user(google, "a@example.com")
    _connect(me)
    listing = me.get("/api/drive/files?kind=sheets").get_json()
    assert [f["name"] for f in listing["folders"]] == ["Binder"]
    assert sorted(f["name"] for f in listing["files"]) == ["My cards", "whatnot.csv"]
    query = google.google.calls[-1][2]["q"]
    assert query.startswith("'root' in parents and trashed = false")
    assert "sharedWithMe = true" in (me.get("/api/drive/files?kind=sheets&folder=shared-with-me"),
                                     google.google.calls[-1][2]["q"])[1]
    # A folder id is never pasted into the query unchecked.
    assert me.get("/api/drive/files?folder=x' or '1'='1").status_code == 400
    assert me.get("/api/drive/files?kind=everything").status_code == 400


def test_a_card_list_sheet_from_drive_is_previewed_then_added_once(google):
    me = _user(google, "a@example.com")
    _connect(me)
    body = {"file_id": "s1", "source": "cards"}
    p = me.post("/api/drive/import/sheet", json={**body, "dry_run": True}).get_json()
    assert (p["dry_run"], p["file"], p["listings"], p["new"], p["new_cards"]) == (True, "My cards", 2, 2, 3)
    assert (p["skipped"], p["not_listed"]) == (1, 1)
    assert me.get("/api/inventory").get_json()["items"] == []

    r = me.post("/api/drive/import/sheet", json=body).get_json()
    assert (r["added"], r["cards"], r["unmatched"]) == (2, 3, 0)
    have = {i["name"]: i for i in me.get("/api/inventory").get_json()["items"]}
    drag = have["Dragapult ex"]
    assert (drag["quantity"], drag["condition"], drag["is_holo"], drag["card_number"], drag["acquired_price"]) == (
        2, "NM", True, "130", 9.5)
    assert (have["Dreepy"]["quantity"], have["Dreepy"]["condition"]) == (1, "NM")
    assert drag["listed_on"] == []                    # cards you own, not a listing elsewhere
    added = _events(me, "added")["events"]
    assert {e["channel"] for e in added} == {"other"} and "imported from a spreadsheet" in added[0]["note"]

    again = me.post("/api/drive/import/sheet", json=body).get_json()
    assert (again["added"], again["duplicates"]) == (0, 2)


def test_other_sheets_go_through_the_same_importers_and_wrong_files_are_refused(google):
    me = _user(google, "a@example.com")
    _connect(me)
    r = me.post("/api/drive/import/sheet", json={"file_id": "c1", "source": "whatnot"}).get_json()
    assert (r["added"], r["unmatched"]) == (0, 1)                      # a title with no card number waits for a person
    r = me.post("/api/drive/import/sheet", json={"file_id": "c1", "source": "tcgplayer"})
    assert r.status_code == 400 and "TCGplayer Id" in r.get_json()["error"]
    r = me.post("/api/drive/import/sheet", json={"file_id": "d1", "source": "cards"})
    assert r.status_code == 400 and "not a Google Sheet or a CSV" in r.get_json()["error"]
    assert me.post("/api/drive/import/sheet", json={"file_id": "nope", "source": "cards"}).status_code == 400
    assert me.post("/api/drive/import/sheet", json={"file_id": "s1", "source": "ebay"}).status_code == 400
    r = me.post("/api/listings/import", data={"source": "cards", "file": (__import__("io").BytesIO(b"Qty\n1\n"), "x.csv")},
                content_type="multipart/form-data")
    assert r.status_code == 400 and "no Name column" in r.get_json()["error"]


def test_a_withdrawn_sign_in_asks_to_connect_again_and_disconnect_revokes(google):
    me = _user(google, "a@example.com")
    _connect(me)
    google.google.refresh_ok = False                                   # access removed in the Google account
    r = me.get("/api/drive/files?kind=sheets")
    assert r.status_code == 409 and r.get_json()["reconnect"] is True
    assert me.get("/api/drive/status").get_json()["connected"] is False     # the dead token is dropped

    google.google.refresh_ok = True
    _connect(me)
    assert me.post("/api/drive/disconnect").get_json()["connected"] is False
    assert google.google.revoked == ["rt-SECRET"]
    assert me.get("/api/drive/files").status_code == 409


def test_oversized_files_are_refused_before_and_after_download():
    class Http(FakeGoogle):
        pass

    http = Http()
    http.files["big"] = {"id": "big", "name": "huge.jpg", "mimeType": "image/jpeg", "size": "999"}
    http.files["lies"] = {"id": "lies", "name": "sneaky.jpg", "mimeType": "image/jpeg"}
    http.content["lies"] = b"x" * 500
    d = google_drive.Drive("at-2", http=http)
    with pytest.raises(google_drive.DriveError, match="huge.jpg is over"):
        d.download("big", 100)
    with pytest.raises(google_drive.DriveError, match="sneaky.jpg is over"):
        d.download("lies", 100)
    with pytest.raises(google_drive.DriveError, match="not a Drive file id"):
        d.download("../etc", 100)
