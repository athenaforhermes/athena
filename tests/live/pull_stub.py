"""A stand-in for `hf download`, so the pull path can be tested without a network.

Real pulls are proved against the real CLI in a live probe (t_pull.py says so at
the top). What is tested here is everything *around* the CLI: the job, the queue,
the byte counter, the destination resolution and the failure reporting — and the
only honest way to exercise those is to run a child process that behaves like the
one they wrap. This one accepts the same arguments the puller passes, writes what
`--local-dir` asks for, and can be told to fail or to take its time.

It is driven entirely by environment variables set by the test:

    ATH_STUB_BYTES     how many bytes the "weight file" should be (default 1024)
    ATH_STUB_SLEEP     seconds to spend "downloading" (default 0)
    ATH_STUB_FAIL      exit non-zero with this message after writing
    ATH_STUB_JOURNAL   a file to append `start <t>` / `end <t>` lines to, which is
                       how the test proves two pulls never run at once
"""

import os
import sys
import time
from pathlib import Path


def main(argv):
    journal = os.environ.get("ATH_STUB_JOURNAL")
    if journal:
        with open(journal, "a", encoding="utf-8") as f:
            f.write(f"start {time.time():.3f} {os.getpid()}\n")
    try:
        dest = None
        for i, a in enumerate(argv):
            if a == "--local-dir" and i + 1 < len(argv):
                dest = Path(argv[i + 1])
        size = int(os.environ.get("ATH_STUB_BYTES") or 1024)
        if dest is not None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "model.safetensors").write_bytes(b"w" * size)
            print(f"path={dest}")
        else:
            print("path=<cache>")
        time.sleep(float(os.environ.get("ATH_STUB_SLEEP") or 0))
        fail = os.environ.get("ATH_STUB_FAIL")
        if fail:
            print(fail, file=sys.stderr)
            return 1
        print("done")
        return 0
    finally:
        if journal:
            with open(journal, "a", encoding="utf-8") as f:
                f.write(f"end {time.time():.3f} {os.getpid()}\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
