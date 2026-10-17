# Measured, not felt

Written by `tests/live/t_perf.py`. Every row is a number this file produced on the
machine it ran on, with the method in the last column, so a row that stops being true
can be re-measured rather than argued about. The rows that need a live guest, a cold
engine or half an hour of running are named at the bottom as **pending**, with the
command that would fill them — a missing number is stated, never guessed.

| What | Number | Target | How it was measured |
| --- | --- | --- | --- |
| one encode at the guest's own size | 8.4 ms | n/a — cost of an event | median of 5 `_jpeg` calls on random pixels |
| 200 polls of a still screen | 1 encode(s) | 1 encode | FakeVnc, quiet after the first frame |
| a poll that finds the cache warm | 0.00 ms | < an encode | 200 cached polls |
| identical rectangles arriving (200 polls) | 1 encode(s) | 1 encode | FakeVnc re-sending byte-identical pixels; measured after the fix |
| ... the same traffic before the fix | ~1681 ms of encoding | 0 | 200 arriving rectangles x the measured encode cost; the number the fix removes |
| a pointer event on the held connection | 0.00 ms | < 10 ms | median of 50 events into FakeVnc, same socket as the frames |

## Pending, with the exact command

| What | Target | Command |
| --- | --- | --- |
| Pointer event → changed pixels, p95 | ≤ 60 ms | `python t_perf.py --guest 127.0.0.1:5901` |
| Engine start → first route answer | ≤ 3 s | start the engine, then time `athena_status` |
| Panel first paint, 100 apps | ≤ 1.5 s | `node uitest/driver.mjs --timing` |
| RSS after 30 min with a screen open | ≤ 400 MB | run a machine, open Screen, read `athena_report` |

## What the numbers are for

The mouse-lag item was parked for two reasons: it had no target, and "feels better" is
not a proof. The mechanism is measured above — an arriving rectangle that is
byte-identical to what is already on screen used to invalidate the encoded frame and pay
a full encode, so a pointer moving across a still desktop cost tens of milliseconds per
movement at 1280×800. The fix is one comparison per row in `_read_update`, and the row
that says what it removed is the reason the fix is in rather than merely plausible.
