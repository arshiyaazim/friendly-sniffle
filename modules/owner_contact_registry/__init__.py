  """Optional CSV-backed contact registry."""
  from __future__ import annotations

  import csv
  import logging
  import os
  import threading
  from pathlib import Path
  from typing import Optional

  log = logging.getLogger(__name__)

  _REPO_ROOT = Path(__file__).resolve().parents[2]
  _configured_path = os.getenv("OWNER_CONTACT_REGISTRY_PATH", "resources/owner_contacts.csv").strip()
  _CSV_PATH = Path(_configured_path)
  if not _CSV_PATH.is_absolute():
     _CSV_PATH = _REPO_ROOT / _CSV_PATH

  # Loaded frozenset of canonical 8801XXXXXXXXX phones.
  # A lock guards the lazy load so the first concurrent caller doesn't double-read.
_lock = threading.Lock()
_loaded = False
_registry: frozenset[str] = frozenset()
_load_error: Optional[str] = None


def _normalize(raw_phone: str) -> Optional[str]:
    """Delegate to the canonical Fazle-Core phone normalizer.

    Returns the canonical 8801XXXXXXXXX key, or None if the input is not a
    recognisable Bangladeshi mobile number.
    """
    # Lazy import so this module has no hard import-time dependency on the
    # full phone_normalizer stack (which itself imports DB config). The
    # import is cheap on warm path.
    from modules.phone_normalizer import normalize_phone

    return normalize_phone(raw_phone)


def _load_registry() -> frozenset[str]:
    """Read CSV, normalize every phone, return frozenset of canonical keys.

    Logs OWNER_CONTACT_REGISTRY_ERROR on failure. Never raises -- the caller
    gets an empty registry and is_owner_managed_contact() returns False for
    everything, preserving prior runtime behavior until the CSV can be fixed.
    """
    global _loaded, _registry, _load_error

    if not _CSV_PATH.exists():
        _load_error = f"missing CSV at {_CSV_PATH}"
        log.error(
            "OWNER_CONTACT_REGISTRY_ERROR path=%s -- is_owner_managed_contact "
            "will return False until the CSV is restored. "
            "Falling back to NO CONTACT SUPPRESSION (per section 12 fail-safe).",
            _CSV_PATH,
        )
        return frozenset()

    try:
        with _CSV_PATH.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        _load_error = f"read/parse failure: {e!r}"
        log.error(
            "OWNER_CONTACT_REGISTRY_ERROR -- cannot read %s: %s. "
            "Falling back to NO CONTACT SUPPRESSION.",
            _CSV_PATH, e,
        )
        return frozenset()

    keys: set[str] = set()
    invalid_count = 0
    for r in rows:
        raw = (r.get("Phone Number") or "").strip()
        n = _normalize(raw)
        if n is None:
            invalid_count += 1
            continue
        keys.add(n)

    if invalid_count:
        log.warning(
            "[owner_contact_registry] CSV had %d invalid phone(s) skipped; "
            "verify the CSV row format.", invalid_count,
        )

    log.info(
        "[owner_contact_registry] loaded %d unique normalized phones from %s",
        len(keys), _CSV_PATH,
    )
    return frozenset(keys)


def _ensure_loaded() -> frozenset[str]:
    """Load registry on first call. Thread-safe via _lock."""
    global _loaded, _registry, _load_error
    if _loaded:
        return _registry
    with _lock:
        if _loaded:
            return _registry
        _registry = _load_registry()
        _loaded = True
        return _registry


def is_owner_managed_contact(phone: str) -> bool:
    """True iff the given phone (any reasonable format) appears in the
    authoritative Owner-managed contact group CSV.

    Phone values are normalized via the same canonical Fazle-Core mechanism
    identity resolution uses, so callers can pass:
      - 018xxxxxxxx
      - 88018xxxxxxxx
      - +88018xxxxxxxx
      - 0088018xxxxxxxx
      - any string containing those digits with surrounding punctuation/whitespace

    and they all match the same canonical key.

    Returns False for:
      - phones not in the CSV
      - phones the normalizer rejects (foreign formats, missing digits)
      - when the CSV cannot be loaded (fail-open per section 12)
      - empty string

    NEVER returns True from a broken registry.
    """
    if not phone:
        return False
    n = _normalize(str(phone).strip())
    if n is None:
        return False
    return n in _ensure_loaded()


def registry_size() -> int:
    """Return the current size of the loaded registry. Useful for diagnostics."""
    return len(_ensure_loaded())


def registry_load_error() -> Optional[str]:
    """If the loader encountered a non-fatal error, return its message.
    Used by tests + diagnostic pages."""
    return _load_error


def reload_registry() -> frozenset[str]:
    """Force-reload the registry from CSV. Used by tests and after Owner
    updates the file. Returns the new registry size."""
    global _loaded, _registry, _load_error
    with _lock:
        _registry = _load_registry()
        _loaded = True
        return _registry