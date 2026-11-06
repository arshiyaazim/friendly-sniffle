def normalize_phone_880(raw: str) -> str:
    from modules.phone_normalizer import normalize_phone
    return normalize_phone(raw) or ""


CRITICAL_NUMBERS_RAW: list[str] = []
CRITICAL_NUMBERS: tuple[str, ...] = ()
