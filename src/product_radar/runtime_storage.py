"""Where the working records live: the project's ``data/`` and ``media/live/``.

Stored references keep the logical project layout (``data/...``,
``media/live/...``). By default both trees are physically inside the project.
The machine-local setting ``config/local/runtime_storage.json`` may place them
under one other local folder (for example on a faster filesystem). That folder
must already exist and carry ``MARKER``. A setting that names a missing,
unmarked or linked folder fails closed: it never creates an empty store, which
would silently reset the owner's saved Stop switch to its default and hide the
spending ledger. Code, configuration, credentials and the daily backup
destination (the project's own ``data/backups``) never move.

The setting is read once per process and project-root spelling; changing it
requires a restart of any long-running research process.

``lexical=True`` maps spellings from the setting alone, without opening the
store or its marker. Only checks of saved backup references use it, so a copy
kept in the project's ``data/backups`` can still be verified after the store
folder itself is lost; every read or write of working records stays strict.
"""

from functools import lru_cache
from pathlib import Path, PurePosixPath

from .models import SafetyError
from .storage import load_json, safe_path


SETTING = ("config", "local", "runtime_storage.json")
MARKER = "runtime-storage.json"
MARKER_VALUE = {"schema_version": 1, "kind": "product-radar-runtime-storage-v1"}
RELOCATED = (("data",), ("media", "live"))


def _relocated(parts):
    return any(tuple(parts[:len(prefix)]) == prefix for prefix in RELOCATED)


@lru_cache(maxsize=64)
def _configured(project_root, lexical=False):
    root = Path(project_root)
    setting = root.joinpath(*SETTING)
    if not setting.exists() and not setting.is_symlink():
        return root
    value = load_json(setting, 4096)
    text = value.get("runtime_root") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "runtime_root"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(text, str) or not text or len(text) > 1024
            or any(char in text for char in ("\x00", "\\", "\n", "\r"))):
        raise SafetyError("RUNTIME_STORAGE_SETTING_INVALID")
    target = PurePosixPath(text)
    # Windows drives are case-insensitive and the scheduled task spells the
    # project in lower case, so overlap is also refused case-insensitively.
    folded, project = PurePosixPath(text.casefold()), PurePosixPath(str(root).casefold())
    if (not target.is_absolute() or str(target) != text or any(part in (".", "..") for part in target.parts)
            or folded == project or project in folded.parents or folded in project.parents):
        raise SafetyError("RUNTIME_STORAGE_SETTING_INVALID")
    if lexical:
        return Path(text)  # spelling only: the store is not opened
    target = safe_path(Path(text))  # no linked component, including the folder itself
    try:
        marker = load_json(target / MARKER, 4096)
    except SafetyError:
        raise SafetyError("RUNTIME_STORAGE_UNAVAILABLE") from None
    if marker != MARKER_VALUE or not target.is_dir():
        raise SafetyError("RUNTIME_STORAGE_UNAVAILABLE")
    return target


def runtime_root(project_root, *, lexical=False):
    """Physical folder that holds ``data/`` and ``media/live/`` for this project."""
    return _configured(str(Path(project_root).absolute()), lexical)


def _parts(parts):
    flat = []
    for part in parts:
        if not isinstance(part, (str, PurePosixPath, Path)) or not str(part):
            raise SafetyError("RUNTIME_PATH_INVALID")
        flat.extend(PurePosixPath(str(part)).parts)
    if (not flat or flat[0] == "/" or any(part in (".", "..") or "\x00" in part or "\\" in part for part in flat)):
        raise SafetyError("RUNTIME_PATH_INVALID")
    return tuple(flat)


def runtime_path(project_root, *parts, lexical=False):
    """Physical path for a project-relative path such as ``"data/outputs"``."""
    relative = _parts(parts)
    root = Path(project_root).absolute()
    return (runtime_root(root, lexical=lexical) if _relocated(relative) else root).joinpath(*relative)


def logical_path(project_root, path, *, lexical=False):
    """Project-relative spelling of a path at its canonical location, else None.

    A relocated tree is canonical only under the runtime folder; a stale copy at
    the old in-project location is not accepted as the same record.
    """
    root = Path(project_root).absolute()
    path = Path(path)
    base = runtime_root(root, lexical=lexical)
    if base != root:
        try:
            relative = path.relative_to(base).parts
        except ValueError:
            relative = None
        if relative is not None:
            return PurePosixPath(*relative) if relative and _relocated(relative) else None
    try:
        relative = path.relative_to(root).parts
    except ValueError:
        return None
    if not relative or base != root and _relocated(relative):
        return None
    return PurePosixPath(*relative)


def records_root(memory_root, project_root):
    """Folder holding ``data/`` for a research store at ``<x>/data/research/captured``.

    Purely lexical, for readers that must not probe files (the owner summary).
    Any other store layout (for example an isolated test store) keeps the
    project root, exactly as before relocation existed.
    """
    path = Path(memory_root).absolute()
    return path.parents[2] if path.parts[-3:] == ("data", "research", "captured") else Path(project_root)
