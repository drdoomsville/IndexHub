"""Render every web page and verify the embedded JavaScript actually parses.

The pages keep their JS in Python triple-quoted strings, so a single backslash
escape like "\\n" inside a JS string literal is consumed by Python and emitted as
a *raw* newline. A raw newline inside a JS "..." / '...' string is a syntax error
that aborts the whole <script> block at parse time -- silently disabling every
handler in it (e.g. the "Start scan" button). This guards against that and any
other syntax error in the inline scripts.

Requires Node.js on PATH (used only as a JS syntax checker via `node --check`).
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import webui

NODE = shutil.which("node")
if not NODE:
    print("SKIP: node not on PATH; cannot syntax-check inline JS")
    sys.exit(0)


def check_scripts(html: str, label: str):
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, f"{label}: no <script> blocks found"
    for i, block in enumerate(blocks):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(block)
            path = fh.name
        try:
            res = subprocess.run([NODE, "--check", path],
                                 capture_output=True, text=True)
        finally:
            Path(path).unlink(missing_ok=True)
        assert res.returncode == 0, (
            f"{label}: <script> block #{i} has a JS syntax error:\n{res.stderr}")


PAGES = {
    "landing": lambda: webui.render_page(webui.LANDING_HTML),
    "duplicates": lambda: webui.render_page(webui.DUPS_HTML),
    "media-org": lambda: webui.render_page(webui.MEDIAORG_HTML),
    "media-app": lambda: webui.render_app("media"),
    "documents-app": lambda: webui.render_app("documents"),
}

for name, render in PAGES.items():
    check_scripts(render().decode("utf-8"), name)
    print(f"ok: {name} inline JS parses")

print("PASS: all rendered pages have syntactically valid inline JS")
