# Keyboard, focus, and the parts a person has to check by hand

The panel is a surface people drive with a keyboard while watching a machine somewhere else, so
"you can reach it with a mouse" is not the standard. This file says what is **asserted**, where,
and what is **manual** — the second list is short and honest rather than full and imaginary.

## What is asserted

| Behaviour | Where | What it proves |
| --- | --- | --- |
| The keyboard-taking pair (`Ctrl+Alt`) is a value, not a gesture to feel | `t_screen.py` (`keyOwnerStep`) | pressing the pair is not the release; `Ctrl+Alt+F2` cancels so the combination still reaches the guest; letting go of one modifier while the other is down is not the release either; a lone `Ctrl` is not one |
| The dock draws who holds the keyboard, and the one click that changes it | `uitest/driver.mjs` (`dockNotes`, `keyboardButton`, `keyboardNote`) | both states are drawn, the label is the function's own answer, and the button's `onClick` runs without throwing |
| A refused keystroke is reported rather than swallowed | `uitest/driver.mjs` (lease block) + `t_ui_js.py` | the refusal is a sentence in the panel, and the route that carries it is checked by name |
| Every failure path in the panel reads the answer before believing it | `t_ui_js.py` (8 checks) | no mutating call site can treat `ok: false` as success; the checker itself is executed with five shapes of answer |
| An event goes down the connection the session already has | `t_perf.py` + `t_screen.py` | a keystroke costs no handshake, which is what makes typing feel direct |

## What is manual, and how to check it

These need a person at the machine, and they are listed as unverified rather than claimed:

1. **Tab order through each panel region.** The intended order is header → tabs → the region's
   own controls, left to right. Type `Tab` from the address of the panel and follow it through
   Apps, Machines, Screen, Terminal, Files, Settings, Runs. A region that traps focus is a bug.
2. **Enter and Space activate the focused control** — a dock button, a tab, a chip. A control that
   only answers a mouse is a control a keyboard user does not have.
3. **Escape closes what it opened**: the app menu, the machine menu, the viewer window. Escape
   while the guest holds the keyboard does **not** release it — that is deliberate (the keystroke
   belongs to the guest) and is the reason the release has its own button.
4. **Focus is visible in both themes.** `Tab` once and look: the focused control must have a ring
   or border set off from the surface around it. The panel uses `--accent` for this, so if the
   accent and the surface are close in a theme, this is where it shows.
5. **A screen reader reads the panel's own words.** The dock's states, an app's health, and a
   refusal are text, not colour alone; status is never carried by a colour with no sentence.

## Why the split is where it is

The automatic checks cover the parts that can be *decided*: a value, a route, a sentence, a
connection count. Focus order needs a rendering engine and a person, and a test that pretends
otherwise would be a test that passes whenever the panel happens to be mounted the way the test
imagined. A manual list that says "unverified" is worth more than an automated claim nobody has
watched fail.
