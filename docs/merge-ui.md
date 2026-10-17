# The desktop merge — contract

`desktop/plugin.js` is **generated**. Nobody edits it. It is the fold of Athena's
own panel source plus two ported donor UIs, and the fold is a script.

This document is the written half of R9's split: it says which file is the source
of truth, the order the pipeline runs in, what the fold guarantees, and what a
merge must never do. It exists so a mechanical split of the panel cannot quietly
undo itself — `merge_ui.py` regenerates `desktop/plugin.js`, so a split applied to
the *built* file is a split the next build erases.

## Which file is which

| File | Role |
| --- | --- |
| `desktop/plugin.js.pre-merge` | **The source of truth for Athena's own panel.** Hand-edited. This is where a UI change goes. |
| `desktop/plugin.js` | **Generated.** The fold of `plugin.js.pre-merge` + both donors. Loaded by Hermes. |
| `desktop-plugins/athena/plugin.js` | A **deployment mirror**. Hermes loads this copy; the suite refreshes it when it has gone stale. |

`plugin.js.pre-merge` is created from `plugin.js` on the first run of
`merge_ui.py` and used to restore the pristine source on every run after — so the
merge always starts from the same place and never compounds on itself.

## Why there is one file

Desktop plugins load uncompiled and may import **only** `@hermes/plugin-sdk`,
`react` and `react/jsx-runtime`. There are no relative imports, so the donor UIs
cannot be imported — they have to live in the same file. That is the reason the
panel is one piece today, and it is why any split is a *source* split (parts that
are concatenated into `plugin.js.pre-merge`), never a split of the built file.

## The pipeline, in order

`tests/live/` holds it; `run_all.sh` drives it in this order and nothing else may
reorder it:

1. `merge_ui.py` — restores `plugin.js` from `plugin.js.pre-merge`, folds in the
   donors, applies the theming pass, asserts the contract, writes `plugin.js`.
   - reads the donors from `plugins/hermes-filebox/desktop/plugin.js` and
     `plugins/sysmon/desktop/plugin.js`;
   - is run **twice** by the suite, and must be idempotent (same output both
     times) or the double run is a FAIL;
2. `mk_ui_test.py` — copies the just-built `plugin.js` to `uitest/athena.mjs`
   with an export block, so the Node probes see today's panel.
   **Re-run this after any UI change, before the driver.**
3. `probe_tabs.mjs`, `probe_net_ui.mjs`, `uitest/driver.mjs` — the Node checks.

The suite records the md5 of `desktop/plugin.js` **as it arrived** before any of
this runs, and requires `merge_ui.py` to reproduce it byte for byte. A mismatch is
one failure and one only: *you changed the sources and did not rebuild* — run
`python merge_ui.py` and commit `desktop/plugin.js`.

## What the fold guarantees

- **Each donor is scoped.** Its body is wrapped in an IIFE that returns only what
  Athena needs (`const AthenaFiles = (() => { … return { Explorer, CSS, EN } })()`),
  so a donor's `ID`/`CSS`/`activeJob`/`paneDispose` cannot collide with Athena's
  identically-named module-level bindings. Renaming ~90 symbols is what this
  avoids.
- **Each donor is adapted, not rewritten.** `subCtx(base, prefix, ns)` re-points
  `ctx.rest` at `/files` or `/sys` and namespaces `ctx.storage` keys, so the
  ported code runs unchanged while its routes and storage stay out of Athena's
  way.
- **Storage is namespaced.** The browser's `tabs`/`viewMode` keys live under
  `files:`/`sys:` prefixes in the shared store, not bare.
- **The import block is the union** of all three halves — `OLD_IMPORTS` is
  replaced by `NEW_IMPORTS` verbatim.
- **Tabs and the render chain are rewritten** together: the tab list gains
  `files`, `machines`, `runs`; the render chain gains the matching `t === '…'`
  arms.
- **Theming is applied last, by name.** `retheme()` maps the widget-only aliases
  a pane never inherits (`--card`, `--border`, `--accent`, `--muted-foreground`,
  `--foreground`, `--background`) onto the `--ui-*` tokens the pane actually
  inherits, and the literal palette colours onto the app's semantic set.
- **The contract is asserted, twice over:** a list of tokens that must be present
  (`_EXPECT`) and a list that must be absent (`_FORBID`). A merge that silently
  drops a ported tab, or restores a removed one, fails here rather than shipping.

## What a merge must never do

- **Reference another plugin's mount.** Any `/api/plugins/<not-athena>` surviving
  into the output is a `SystemExit`. A ported tab that streams media builds an
  absolute URL and no other check sees it (the route checker inspects `ctx.rest`
  paths only), so this is asserted at the single door every merge passes through.
- **Restore the standalone System tab.** `['system', 'System']`, `t === 'system'`,
  `function SystemTab(`, `sysMin`/`setSysMin`/`openSystemTab` are forbidden: the
  deep view is a window opened from the mini monitor, not a tab.
- **Ship a raw terminal.** `jsx('pre', { className: 'ah-term'` is forbidden —
  every terminal in the UI goes through `AutoTerm`, which follows its output. A
  raw `<pre>` is one that has quietly stopped scrolling.
- **Let a donor smuggle an `import`/`export`** into its scoped body — asserted per
  donor before wrapping.
- **Keep the donor self-updater.** `UpdateChip` and its four strings are removed;
  Athena has no self-update path, so the chip could only ever report "check
  failed".

## What a split must do (R9)

A mechanical split of the panel happens in **`plugin.js.pre-merge`** — the source —
and only there. The parts are concatenated back to a `plugin.js.pre-merge` that is
byte-identical to what it replaced, so `merge_ui.py` produces the same
`plugin.js`, the committed-build check passes, and the suite proves the split
changed nothing. A split of the built file is undone by the next build; a split
that is not byte-identical is not a split, it is a behaviour change.
