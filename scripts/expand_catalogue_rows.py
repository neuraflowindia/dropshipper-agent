#!/usr/bin/env python3
"""Turn compact heading rows (JSONL) into a new product-map catalogue revision.

Each line is one heading row::

    {"sub_area_id": "branch-hk-preparation-tools", "label": "Herb strippers",
     "purpose": "strip leaves from herb stems", "included": ["herb stripper"],
     "excluded": ["herb scissors"], "meta_phrase": "herb stripper tool",
     "trends_phrase": "herb stripper", "season": "evergreen"}

or a new sub-area (its category comes from the id: hd, hk, bb or to)::

    {"new_sub_area": {"id": "branch-hk-spice-and-herb-tools", "name": "Spice and herb tools",
                      "routing_words": ["herb", "spice grinder"]}}

The rows are expanded into full headings in the house style
(``product_radar.catalogue_rows``), checked against the whole catalogue and
written as ``config/research/catalogue.json`` (and, for new sub-areas, their
routing words into ``config/research/product_map.json``). Every refused row is
listed and nothing is written. Local files only: nothing is fetched or sent.
"""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar import catalogue as catalogue_config, map_materials  # noqa: E402
from product_radar.catalogue import CATALOGUE_LIMIT, validate_catalogue  # noqa: E402
from product_radar.catalogue_rows import (AUTHOR, REVISION, RowsRefused, encode_catalogue, encode_settings,  # noqa: E402
                                          expand_rows, now, read_rows)
from product_radar.map_materials import SETTINGS_LIMIT  # noqa: E402
from product_radar.models import SafetyError  # noqa: E402
from product_radar.storage import atomic_bytes, load_json  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rows", nargs="+", type=Path, help="JSONL files of compact heading rows, in order.")
    parser.add_argument("--catalogue", type=Path, help="The catalogue to extend (default: the saved catalogue).")
    parser.add_argument("--settings", type=Path, help="The product-map settings to extend (default: the saved ones).")
    parser.add_argument("--output", type=Path, help="Where to write the catalogue (default: --catalogue).")
    parser.add_argument("--settings-output", type=Path, help="Where to write the settings (default: --settings).")
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--author", default=AUTHOR)
    parser.add_argument("--at", help="Review time of the new headings (default: now, UTC).")
    parser.add_argument("--dry-run", action="store_true", help="Check and report; write nothing.")
    args = parser.parse_args(argv)
    args.catalogue = args.catalogue or catalogue_config.DEFAULT_PATH
    args.settings = args.settings or map_materials.SETTINGS
    try:
        catalogue = validate_catalogue(load_json(args.catalogue, CATALOGUE_LIMIT))
        settings = load_json(args.settings, SETTINGS_LIMIT)
        before = len(catalogue["families"]), len(catalogue["branches"])
        result, new_settings, warnings = expand_rows(catalogue, settings, read_rows(args.rows), at=args.at or now(),
                                                     revision=args.revision, author=args.author)
        encoded = encode_catalogue(result)
    except RowsRefused as error:
        print("Refused; nothing was written:", file=sys.stderr)
        for problem in error.problems:
            print("  " + problem, file=sys.stderr)
        return 1
    except (SafetyError, OSError) as error:
        print("Refused; nothing was written: " + str(error), file=sys.stderr)
        return 1
    for warning in warnings:
        print("warning: " + warning, file=sys.stderr)
    print(f"{len(result['families']) - before[0]} new headings and {len(result['branches']) - before[1]} new sub-areas; "
          f"{len(result['families'])} headings in revision {result['revision']} ({len(encoded):,} bytes).")
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0
    # The catalogue first, then the settings that name its sub-areas; if the
    # settings cannot be written, the previous catalogue is put back, so the
    # pair never disagrees.
    output = args.output or args.catalogue
    previous = output.read_bytes() if output.exists() else None
    _write(output, encoded)
    if new_settings != settings:
        try:
            _write(args.settings_output or args.settings, encode_settings(new_settings))
        except (SafetyError, OSError) as error:
            if previous is None:
                output.unlink()
            else:
                _write(output, previous)
            print("Refused; nothing was written: the settings could not be saved (" + str(error) + ").",
                  file=sys.stderr)
            return 1
    return 0


def _write(path, encoded):
    """Replace the file atomically, keeping an ordinary readable file mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    atomic_bytes(path, encoded)
    path.chmod(mode | 0o644)


if __name__ == "__main__":
    sys.exit(main())
