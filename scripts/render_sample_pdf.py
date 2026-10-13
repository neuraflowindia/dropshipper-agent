#!/usr/bin/env python3
"""Render the generated local HTML report to PDF with Windows Google Chrome."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


CHROME_CANDIDATES = [
    Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
]


def to_windows_path(path: Path) -> str:
    completed = subprocess.run(
        ["wslpath", "-w", str(path.resolve())],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def file_url(path: Path) -> str:
    windows_path = to_windows_path(path).replace("\\", "/")
    return "file:///" + quote(windows_path, safe="/:")


def find_chrome(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        return explicit
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No supported Chrome or Edge executable was found.")


def render(html_path: Path, output_path: Path, chrome_path: Path) -> None:
    if not html_path.exists():
        raise FileNotFoundError(html_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    with tempfile.TemporaryDirectory(
        prefix=".chrome-p5-",
        dir=output_path.parent,
    ) as profile_dir:
        command = [
            str(chrome_path),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--no-first-run",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=2500",
            f"--user-data-dir={to_windows_path(Path(profile_dir))}",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={to_windows_path(output_path)}",
            file_url(html_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Chrome PDF rendering failed with exit code "
                f"{completed.returncode}:\n{completed.stderr.strip()}"
            )

    if not output_path.exists() or output_path.stat().st_size < 100_000:
        raise RuntimeError("Chrome did not create a plausible PDF output.")
    print(output_path)
    print(f"{output_path.stat().st_size} bytes")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("preview/report/report.html"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "outputs/client-preview/"
            "NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf"
        ),
    )
    parser.add_argument("--chrome", type=Path)
    args = parser.parse_args()
    chrome_path = find_chrome(args.chrome)
    render(args.html, args.output, chrome_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
