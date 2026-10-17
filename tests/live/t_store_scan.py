"""
The store scan against an app folder that vanishes mid-scan.

`t_names` scans every folder under the plugin home's `api/`; `t_build` creates
four fixture apps there, asserts nothing is left behind, and deletes them. When
the two overlap, the scan told the truth about a folder that had just been
removed and died:

    FileNotFoundError: [WinError 3] ...\\athena\\api\\zz-buildtest-prompt

That is not only a test problem. A person uninstalling an app while the panel
refreshes hits the same path, so the scan is fixed in the product and this
reproduces the race without any timing at all: take the listing, delete one
folder from it, then scan the listing that was taken. No sleeps, no threads, no
"wait for it" — the listing is stale by construction, which is the one thing
that made the original flaky.

Usage: env -u PYTHONPATH <venv python> t_store_scan.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
sys.path.insert(0, str(PLUG / "tools"))
sys.path.insert(0, str(PLUG / "dashboard"))

import publish_store as pub  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}   {detail}")


def fixture(home: Path, name: str, title: str) -> Path:
    app = home / "api" / name
    app.mkdir(parents=True)
    (app / "athena.js").write_text(
        "module.exports = {\n"
        '  title: "' + title + '",\n'
        '  description: "A fixture app, for the scan only.",\n'
        "  run: [],\n"
        "};\n", encoding="utf-8")
    (app / "GUIDE.md").write_text("# " + title + "\n", encoding="utf-8")
    return app


def main():
    print("\n-- a store scan with a folder that vanishes --")
    home = Path(tempfile.mkdtemp(prefix="athena-scan-"))
    try:
        kept_a = fixture(home, "zz-scan-alpha", "Alpha")
        vanished = fixture(home, "zz-scan-gone", "Gone")
        kept_b = fixture(home, "zz-scan-beta", "Beta")

        listing = pub.apps_in(home)
        check("the listing finds all three apps",
              [p.name for p in listing] == ["zz-scan-alpha", "zz-scan-beta", "zz-scan-gone"],
              str([p.name for p in listing]))

        # The race, deterministically: the listing above is now stale.
        shutil.rmtree(vanished)

        records, errors = [], []
        for app in listing:
            try:
                files = pub.app_files(app)
                if not files:
                    continue          # nothing to publish from a gone folder
                records.append(pub.describe(app, pub.slug_for(app.name), app.name,
                                            "", files))
            except Exception as e:    # noqa: BLE001 — the old bug was any raise
                errors.append(f"{app.name}: {type(e).__name__}: {e}")

        check("scanning a stale listing does not raise", not errors, "; ".join(errors))
        check("the scan reports the two apps that remain",
              sorted(r["slug"] for r in records) == ["athena-zz-scan-alpha",
                                                     "athena-zz-scan-beta"],
              str(sorted(r["slug"] for r in records)))
        check("the remaining records still carry their title",
              sorted(r["title"] for r in records) == ["Alpha", "Beta"],
              str(sorted(r["title"] for r in records)))

        # And the honest answer to "what is installed now" is two, not three.
        relisted = [p.name for p in pub.apps_in(home)]
        check("a fresh listing skips the vanished app",
              relisted == ["zz-scan-alpha", "zz-scan-beta"], str(relisted))

        # Reading the dead path directly is still allowed to answer, because a
        # scan may already hold the path when the folder goes.
        rec = pub.describe(vanished, "zz-scan-gone", "zz-scan-gone", "", [])
        check("describing the vanished folder directly answers a record",
              isinstance(rec, dict) and rec.get("slug") == "zz-scan-gone",
              repr(rec)[:200])
        try:
            json.dumps(rec)       # it has to be publishable, not just truthy
            serialised = True
        except TypeError:
            serialised = False
        check("the record for a vanished folder can be serialised", serialised)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{passed} checks passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
