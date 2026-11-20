"""One-time login capture for TCGPlayer / Whatnot seller portals (terminal).

The web UI does the same thing from Scan -> Connections. This script is the
terminal version, for when the app is not running:

    cd pokemon-bulk-lister
    python -m webapp.setup_portal --site tcgplayer
    python -m webapp.setup_portal --site whatnot

A real Chromium window opens at the portal's login page. Sign in (and clear any
2FA), reach the seller area, then press Enter back here to save the session.
The headless CSV uploader then reuses that session.

Automating these portals is generally against their Terms: see
lib/portal_uploader.py. Use a personal/secondary account at your own risk.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    from lib.portal_uploader import PORTALS, LoginCapture, portal_state_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, choices=sorted(PORTALS))
    args = parser.parse_args()

    state_path = portal_state_path(args.site, ROOT)
    if state_path.exists():
        print(f"NOTE: existing session at {state_path} will be replaced if you continue.")

    capture = LoginCapture(args.site, state_path)
    print(f"\n=== {args.site} login ===")
    print(f"1. A Chromium window opens at {capture.login_url}.")
    print("2. Sign in (clear any 2FA / captcha) and reach the seller dashboard.")
    print("3. Come back here and press Enter to save the session.\n")
    capture.start()
    try:
        input("Press Enter once you are signed in and on the seller dashboard... ")
    except KeyboardInterrupt:
        capture.cancel()
        print("\naborted, nothing saved.")
        return 1

    status = capture.status()
    if status["state"] != "waiting":
        print(f"{status['state']}: {status['detail']}", file=sys.stderr)
        return 1
    status = capture.finish()
    if status["state"] != "saved":
        print(f"failed to save session: {status['detail']}", file=sys.stderr)
        return 1
    print(f"\nSaved {args.site} session to {state_path}")
    print("The headless CSV uploader will now reuse this session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
