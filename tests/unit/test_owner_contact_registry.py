"""Tests for the optional CSV-backed contact registry."""
import pytest


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import modules.owner_contact_registry as registry_module

    csv_path = tmp_path / "contacts.csv"
    csv_path.write_text(
        "Phone Number,Name\n"
        "01700000001,Sample Contact\n"
        "8801700000001,Duplicate Sample Contact\n"
        "01800000002,Another Sample Contact\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_module, "_CSV_PATH", csv_path)
    monkeypatch.setattr(registry_module, "_loaded", False)
    monkeypatch.setattr(registry_module, "_registry", frozenset())
    monkeypatch.setattr(registry_module, "_load_error", None)
    return registry_module


def test_normalized_phone_formats_match_registry(registry):
    assert registry.is_owner_managed_contact("01700000001") is True
    assert registry.is_owner_managed_contact("8801700000001") is True
    assert registry.is_owner_managed_contact("+8801700000001") is True


def test_duplicate_normalized_phones_have_one_registry_entry(registry):
    assert registry.registry_size() == 2


def test_unknown_or_invalid_phone_does_not_match(registry):
    assert registry.is_owner_managed_contact("01900000003") is False
    assert registry.is_owner_managed_contact("1.8e10") is False
    assert registry.is_owner_managed_contact("") is False


def test_missing_registry_file_fails_open_without_matching(registry, tmp_path, monkeypatch):
    missing_path = tmp_path / "missing.csv"
    monkeypatch.setattr(registry, "_CSV_PATH", missing_path)
    registry.reload_registry()

    assert registry.is_owner_managed_contact("01700000001") is False
    assert registry.registry_load_error()
