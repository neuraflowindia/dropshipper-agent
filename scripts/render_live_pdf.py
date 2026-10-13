#!/usr/bin/env python3
"""Render a checked local report through an owned native browser process.

Write to a unique staged PDF, inspect it, then atomically create the destination.
Never delete an old PDF or terminate any personal browser session. This helper
does not approve/upload a package; integration must add the PDF to its manifest.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError
from product_radar.storage import safe_path

CHROME_CANDIDATES = (
    Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
)
POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def windows_path(path, runner=subprocess.run):
    result = runner(["wslpath", "-w", str(Path(path).absolute())], capture_output=True, text=True, check=True, timeout=10)
    value = result.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise SafetyError("LIVE_NATIVE_PATH_INVALID")
    return value


def _command(chrome, html, profile, output, timeout, runner):
    browser = windows_path(chrome, runner)
    arguments = ["--headless=new", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
                 "--disable-default-apps", "--no-first-run", "--allow-file-access-from-files",
                 "--run-all-compositor-stages-before-draw", "--virtual-time-budget=2500",
                 "--no-pdf-header-footer", "--print-to-pdf-no-header",
                 "--user-data-dir=" + windows_path(profile, runner),
                 "--print-to-pdf=" + windows_path(output, runner),
                 "file:///" + quote(windows_path(html, runner).replace("\\", "/"), safe="/:")]
    payload = base64.b64encode(json.dumps({"browser": browser, "arguments": subprocess.list2cmdline(arguments),
                                         "timeout_ms": timeout * 1000}).encode()).decode()
    # Base64 JSON is data, not interpolated executable paths or HTML. The only
    # taskkill target is the process returned by this specific Start-Process.
    script = """
$ErrorActionPreference='Stop'
$request=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$owned=$null
try {
  $owned=Start-Process -FilePath $request.browser -ArgumentList $request.arguments -PassThru -WindowStyle Hidden
  if(-not $owned.WaitForExit([int]$request.timeout_ms)){exit 124}
  if($owned.ExitCode -ne 0){exit 2}
} finally {
  if($owned -and -not $owned.HasExited){& taskkill.exe /PID $owned.Id /T /F | Out-Null}
}
""".replace("PAYLOAD", payload)
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    return [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def inspect_pdf(raw):
    import fitz
    if not isinstance(raw, bytes) or not raw.startswith(b"%PDF-") or len(raw) > 64 * 1024 * 1024:
        raise SafetyError("LIVE_PDF_INVALID")
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if not 1 <= len(document) <= 100:
                raise SafetyError("LIVE_PDF_INVALID")
            for page in document:
                if page.rect.width >= page.rect.height or not page.get_text().strip():
                    raise SafetyError("LIVE_PDF_LAYOUT_INVALID")
            return {"pages": len(document), "portrait": True, "bytes": len(raw)}
    except (ValueError, RuntimeError):
        raise SafetyError("LIVE_PDF_INVALID") from None


def render(html_path, output_path, chrome_path=None, *, timeout=90, runner=subprocess.run):
    html_path, output_path = Path(html_path).absolute(), Path(output_path).absolute()
    safe_path(html_path.parent)
    safe_path(output_path.parent)
    if html_path.is_symlink() or output_path.is_symlink() or not html_path.is_file():
        raise SafetyError("LIVE_RENDER_PATH_INVALID")
    if output_path.exists():
        raise SafetyError("LIVE_PDF_ALREADY_EXISTS")
    if type(timeout) is not int or not 10 <= timeout <= 120:
        raise SafetyError("LIVE_RENDER_TIMEOUT_INVALID")
    with html_path.open("rb") as stream:
        html = stream.read(32 * 1024 * 1024 + 1)
    if len(html) > 32 * 1024 * 1024 or re.search(rb"<(?:script|iframe|object|embed)\b", html, re.I):
        raise SafetyError("LIVE_RENDER_HTML_INVALID")
    chrome = Path(chrome_path) if chrome_path else next((path for path in CHROME_CANDIDATES if path.is_file()), None)
    if chrome not in CHROME_CANDIDATES or not chrome.is_file():
        raise SafetyError("LIVE_NATIVE_BROWSER_UNAVAILABLE")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".live-pdf-", dir=output_path.parent) as temporary:
        temporary = Path(temporary)
        staged = temporary / "staged.pdf"
        command = _command(chrome, html_path, temporary / "profile", staged, timeout, runner)
        try:
            result = runner(command, capture_output=True, timeout=timeout + 25)
        except subprocess.TimeoutExpired:
            raise SafetyError("LIVE_NATIVE_RENDER_TIMEOUT") from None
        if result.returncode != 0:
            raise SafetyError("LIVE_NATIVE_RENDER_FAILED")
        if not staged.is_file():
            raise SafetyError("LIVE_NATIVE_PDF_MISSING")
        with staged.open("rb") as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
        result = inspect_pdf(raw)
        if hashlib.sha256(html_path.read_bytes()).digest() != hashlib.sha256(html).digest():
            raise SafetyError("LIVE_RENDER_HTML_CHANGED")
        # A concurrent render cannot replace an existing accepted PDF. Hard
        # linking the complete staged file is an atomic create-if-absent.
        try:
            os.link(staged, output_path)
        except FileExistsError:
            raise SafetyError("LIVE_PDF_ALREADY_EXISTS") from None
        return dict(result, path=str(output_path), html_sha256=hashlib.sha256(html).hexdigest(), pdf_sha256=hashlib.sha256(raw).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    try:
        result = render(args.html, args.output, args.chrome, timeout=args.timeout)
        print(json.dumps(result, sort_keys=True))
        return 0
    except SafetyError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
