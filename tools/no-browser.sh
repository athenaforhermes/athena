#!/bin/sh
# Athena — the stand-in for a web browser, on a POSIX host.
# An app that opens its own page when it starts (Gradio's inbrowser, a launcher's
# `xdg-open`) would otherwise put the app outside the window Athena draws for it.
# Python's webbrowser runs $BROWSER in place of a real browser, so pointing BROWSER
# here makes the call a no-op. Same job as no-browser.cmd, which Windows uses.
exit 0
