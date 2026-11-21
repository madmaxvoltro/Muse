#!/usr/bin/env python3
"""Upload a history export to Muse from the command line (stdlib only, no install needed).

    python adapters/importer/import_history.py --kind letterboxd --user <muse-user-uuid> letterboxd-export.zip
    python adapters/importer/import_history.py --kind netflix --profile Max --user <uuid> ViewingActivity.csv
    python adapters/importer/import_history.py --kind youtube --user <uuid> takeout-*.zip

Kinds: youtube, spotify, soundcloud, letterboxd, imdb, netflix, trakt. See
shared/muse_taste/importers.py for which files of each export are read.

Env: MUSE_INGESTION_URL (default http://127.0.0.1:8771), MUSE_IMPORT_KEY (a key whose
source is `import` or `muse` in MUSE_ADAPTER_KEYS). Re-importing the same file is safe.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", required=True, choices=["youtube", "spotify", "soundcloud", "letterboxd", "imdb", "netflix", "trakt"])
    ap.add_argument("--user", required=True, help="Muse user id (the Jellyfin user GUID)")
    ap.add_argument("--profile", help="Netflix: only this profile")
    ap.add_argument("files", nargs="+", type=Path)
    a = ap.parse_args()
    base = os.environ.get("MUSE_INGESTION_URL", "http://127.0.0.1:8771").rstrip("/")
    key = os.environ.get("MUSE_IMPORT_KEY")
    if not key:
        print("set MUSE_IMPORT_KEY", file=sys.stderr)
        return 2
    for f in a.files:
        params = {"user_id": a.user, "filename": f.name}
        if a.profile:
            params["profile"] = a.profile
        req = urllib.request.Request(
            f"{base}/import/{a.kind}?{urllib.parse.urlencode(params)}",
            data=f.read_bytes(),
            method="POST",
            headers={"X-Muse-Api-Key": key, "Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                print(f.name, json.loads(resp.read()))
        except urllib.error.HTTPError as e:
            print(f.name, e.code, e.read().decode(errors="replace"), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
