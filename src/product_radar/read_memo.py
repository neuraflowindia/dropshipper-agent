"""Reuse pure derived reads inside one explicit, unchanging read scope.

This is a speed aid only; it never changes a rule, result or error. Reuse is
active only inside `pure_read_scope()` (opened by one report-pool build).
Outside a scope every call computes from scratch, exactly as before.

Keys bind the identity of the exact table objects a reader uses, plus its
explicit arguments. The scope holds references to those objects until it
closes, so Python cannot reuse an object id for different data meanwhile.
Code inside a scope only reads its state: no table or record is edited while
the scope is open. A saved commit produces a new verified copy (new objects,
new keys); nothing here is global, kept after the scope closes, or shared with
another thread or process.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .models import SafetyError, digest


_ACTIVE = ContextVar("product_radar_pure_read_scope", default=None)
ALL_TABLES = None


@contextmanager
def pure_read_scope():
    """Open one scope; a nested scope shares its outer scope (same rules)."""
    if _ACTIVE.get() is not None:
        yield
        return
    token = _ACTIVE.set({})
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def remember(name, state, args, compute, *, tables=ALL_TABLES, copy=True):
    """Return compute() or the value it gave for these same objects and args.

    `tables` names every state table the reader can read (ALL_TABLES binds
    the state mapping and each of its current tables). `copy=True` gives each
    caller its own deep copy, so editing one result cannot affect another.
    Internal helpers whose callers only read may pass `copy=False`. A plain
    SafetyError is remembered and raised again with the same code; any other
    exception (including a SafetyError subclass) is not remembered.
    """
    values = _ACTIVE.get()
    if values is None or type(state) is not dict:
        return compute()
    try:
        hash(args)
    except TypeError:
        return compute()
    if tables is ALL_TABLES:
        anchors = (state, *state.values())
        identity = (id(state), tuple((table, id(value)) for table, value in state.items()))
    else:
        anchors = tuple(state.get(table) for table in tables)
        identity = tuple(id(value) for value in anchors)
    cache_key = (name, identity, args)
    entry = values.get(cache_key)
    if entry is None:
        try:
            outcome = ("value", compute())
        except SafetyError as error:
            if type(error) is not SafetyError:
                raise
            outcome = ("error", error.args)
        # Keep the bound objects alive for the whole scope (no id reuse).
        entry = values[cache_key] = (outcome, anchors)
    (kind, value), _ = entry
    if kind == "error":
        raise SafetyError(*value)
    return deepcopy(value) if copy else value


def same_object_digest(value):
    """digest(value), reused for this very object inside a scope.

    Saved records are not edited while a scope is open, so the hash of one
    object cannot change; outside a scope this is exactly digest(value).
    """
    values = _ACTIVE.get()
    if values is None or type(value) not in (dict, list):
        return digest(value)
    cache_key = ("same_object_digest", id(value))
    entry = values.get(cache_key)
    if entry is None:
        # Keep the object alive for the whole scope (no id reuse).
        entry = values[cache_key] = (digest(value), value)
    return entry[0]
