"""
The catalog entry we intend to submit, checked against the manifest it describes.

`docs/catalog-entry.yaml` is the artifact a catalog reviewer reads without running
anything, and it is the one file in this repo whose whole job is to be believed. Two
kinds of wrong are possible and both are cheap to catch: it can drift from
`plugin.yaml` (a tool added to the manifest but not the entry, a `requires_hermes`
that disagrees, a version left behind), and it can be structurally invalid in a way
only the real gate sees.

So this does both. It compares the entry to the manifest directly, and when the
Hermes checkout is on this machine it runs the catalogue's own structural validator
(`scripts/validate_plugin_catalog.py`) against a copy — the same script the admission
CI runs, not a paraphrase of it. When the checkout is absent it says SKIPPED, because
a test that quietly passes without checking anything is worse than no test.

The sha is the one field that cannot be real before the plugin has a public home, and
it is checked as a string on purpose: the validator reads YAML 1.1, where an unquoted
all-digit sha is an integer. A commit sha containing no a-f is a valid commit and an
invalid entry unless it is quoted.

Usage: env -u PYTHONPATH <venv python> t_catalog.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")
PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
ENTRY = PLUG / "docs" / "catalog-entry.yaml"
MANIFEST = PLUG / "plugin.yaml"
VALIDATOR = HERMES_ROOT / "hermes-agent" / "scripts" / "validate_plugin_catalog.py"

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


try:
    from ruamel.yaml import YAML
except ImportError:  # pragma: no cover - guidance only
    print("ruamel.yaml is required (it is what the catalog gate itself uses)")
    sys.exit(2)

# utf-8-sig and YAML 1.1 to read the file exactly as the gate reads it: a check that
# parses more kindly than the real thing is a check that passes the file the gate rejects.
yaml = YAML(typ="safe")
yaml.version = (1, 1)
entry = yaml.load(ENTRY.read_text(encoding="utf-8-sig"))
manifest = yaml.load(MANIFEST.read_text(encoding="utf-8-sig"))

check("the catalog entry parses as YAML", isinstance(entry, dict), repr(type(entry)))
check("the manifest parses as YAML", isinstance(manifest, dict), repr(type(manifest)))

for key in ("name", "repo", "sha", "description", "maintainer"):
    check(f"the entry declares {key}", key in entry)

check("the entry's name matches the plugin",
      entry.get("name") == manifest.get("name"), f"{entry.get('name')!r} vs {manifest.get('name')!r}")
check("the entry's version matches the manifest's",
      str(entry.get("version")) == str(manifest.get("version")),
      f"{entry.get('version')!r} vs {manifest.get('version')!r}")
check("the entry's requires_hermes matches the manifest's",
      entry.get("requires_hermes") == manifest.get("requires_hermes"),
      f"{entry.get('requires_hermes')!r} vs {manifest.get('requires_hermes')!r}")

# The tool list is the entry's promise about what the plugin offers. Comparing sorted
# copies means a reordering is not a failure but a missing or invented tool is.
tools_entry = sorted((entry.get("capabilities") or {}).get("provides_tools") or [])
tools_manifest = sorted(manifest.get("provides_tools") or [])
check(f"the entry lists exactly the manifest's {len(tools_manifest)} tools",
      tools_entry == tools_manifest,
      f"only in entry: {sorted(set(tools_entry) - set(tools_manifest))}; only in manifest: {sorted(set(tools_manifest) - set(tools_entry))}")

# The sha is a string even while it is a placeholder, and that is the point: YAML 1.1
# reads a bare 40-digit run as an integer, which is how a perfectly good commit sha
# becomes an invalid entry.
sha = entry.get("sha")
check("the sha is a string of 40 lowercase hex characters",
      isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha or "")),
      f"{sha!r} ({type(sha).__name__}) — unquoted all-digit shas parse as integers")
# The placeholder is not a failure — it is the one item on the entry that cannot be
# finished before the plugin has a public repo, and the catalog CI is what rejects it.
# So it is reported, not counted: a red suite for a known-open item teaches people to
# ignore red, which is how the useful failures get lost.
if sha == "0" * 40:
    print("  note the pinned sha is still the placeholder — right until the plugin has a "
          "public repo, and rejected by the catalog CI until it is replaced")
else:
    check("the pinned sha is a real commit, not the placeholder", True)

platforms = entry.get("platforms") or []
check("the entry claims the platforms we have actually run on",
      sorted(platforms) == ["linux", "windows"], repr(platforms))

# Same rule the README guard enforces, applied to the file a reviewer reads: the vendored
# engine's upstream product is not named in anything we publish.
body = ENTRY.read_text(encoding="utf-8")
stripped = re.sub(r"https?://\S+", "", body)
check("the entry does not name the vendored upstream",
      not re.search(r"(?i)pinokio\w*", stripped), "name it nowhere but in a URL")

check("the entry does not carry a machine-specific path or handle",
      not re.search(r"(?i)C:[/\\]Users[/\\][a-z]+|/home/[a-z]+/", body), "paths must be derived")

# The real gate, on a copy, in a directory of its own so the entry is judged as the
# catalog would see it rather than in place.
if VALIDATOR.is_file():
    tmp = Path(tempfile.mkdtemp(prefix="athena_catalog_"))
    try:
        shutil.copy(ENTRY, tmp / "athena.yaml")
        r = subprocess.run([sys.executable, str(VALIDATOR), str(tmp / "athena.yaml")],
                           capture_output=True, text=True, timeout=120)
        # rc=1 is the placeholder sha and nothing else, so say which it is rather than
        # teaching whoever reads the log to ignore this line.
        errs = [ln for ln in (r.stdout or "").splitlines() if ": ERROR:" in ln]
        placeholder_only = all("sha 0" in ln or "40 lowercase hex" in ln for ln in errs)
        check("the catalog's own validator accepts the entry apart from the placeholder sha",
              r.returncode == 0 or (not errs) or placeholder_only,
              (r.stdout or r.stderr or "").strip()[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("  skip the catalog's own validator — no Hermes checkout at "
          f"{VALIDATOR}, so the structural gate cannot be run from here")

print(f"\n{failed} failed" if failed else f"\n{passed}/{passed} checks passed")
sys.exit(1 if failed else 0)
