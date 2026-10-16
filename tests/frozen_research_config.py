"""Frozen research settings for fixture tests.

Map-lane and catalogue tests check exact outcomes (heading counts, which
sub-area a candidate routes to, which sub-area is built or topped up, the
owner's runway text). Those must not follow the live settings, which keep
growing (the r11 map has about 1,700 headings). These fixtures are the files
the tests were written against (``tests/fixtures/research_config``):

* ``catalogue.json``: revision ``2026-10-15-r10-w10``, 224 headings in 48
  sub-areas (``git show`` of the r10 catalogue);
* ``product_map.json``: its product-map settings with the soft target (40)
  and the AI top-up block;
* ``dropship_platforms.json``: the platform settings before the r11 routing
  words were added to the discovery category words (its Dropdash row follows
  the current settings shape: the pages and page selectors seen signed in on
  16 October 2026, "Profitable Products" as a plain list and the weekly
  category pages, R15).

A test module that needs them uses
``setUpModule, tearDownModule = frozen_research_config.module_fixtures()``;
the modules read these paths when called (``catalogue.DEFAULT_PATH``,
``map_materials.SETTINGS``, ``dropship_platforms.SETTINGS``). The live files
are checked separately (``test_live_research_config``).
"""

from pathlib import Path
from unittest.mock import patch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research_config"
CATALOGUE = FIXTURES / "catalogue.json"
PRODUCT_MAP = FIXTURES / "product_map.json"
DROPSHIP_PLATFORMS = FIXTURES / "dropship_platforms.json"


def use_frozen_config():
    """Point the three settings paths at the fixtures; returns the undo."""
    from product_radar import catalogue, dropship_platforms, map_materials
    patchers = [patch.object(catalogue, "DEFAULT_PATH", CATALOGUE),
                patch.object(map_materials, "SETTINGS", PRODUCT_MAP),
                patch.object(dropship_platforms, "SETTINGS", DROPSHIP_PLATFORMS)]
    for patcher in patchers:
        patcher.start()

    def stop():
        for patcher in reversed(patchers):
            patcher.stop()
    return stop


def module_fixtures():
    """``(setUpModule, tearDownModule)`` that use the frozen settings for one test module."""
    undo = []

    def setUpModule():
        undo.append(use_frozen_config())

    def tearDownModule():
        undo.pop()()
    return setUpModule, tearDownModule
