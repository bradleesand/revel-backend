"""Tests for phone normalisation and segment accounting."""

import pytest

from sms.utils import is_e164, is_gsm7, mask_phone_number, normalize_phone_number, segment_count


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+1 (555) 123-4567", "+15551234567"),
        ("  +44 20.7946 0958 ", "+442079460958"),
        ("+15551234567", "+15551234567"),
    ],
)
def test_normalize_phone_number(raw: str, expected: str) -> None:
    assert normalize_phone_number(raw) == expected


@pytest.mark.parametrize(
    ("number", "valid"),
    [
        ("+15551234567", True),
        ("+442079460958", True),
        ("15551234567", False),  # no plus
        ("+05551234567", False),  # leading zero
        ("+1555", False),  # too short
        ("+1234567890123456", False),  # 16 digits
        ("+1555123456a", False),
    ],
)
def test_is_e164(number: str, valid: bool) -> None:
    assert is_e164(number) is valid


def test_is_gsm7() -> None:
    assert is_gsm7("Reminder: Test Event is tomorrow. https://example.com/e/1")
    assert is_gsm7("Price: 10€ {ok}")  # extension table
    assert not is_gsm7("Reminder ⏰")
    assert not is_gsm7("Curly ’quote’")


@pytest.mark.parametrize(
    ("text", "segments"),
    [
        ("", 1),
        ("a" * 160, 1),
        ("a" * 161, 2),
        ("a" * 306, 2),
        ("a" * 307, 3),
        ("€" * 80, 1),  # 160 septets
        ("€" * 81, 2),
        ("⏰" + "a" * 69, 1),  # UCS-2, 70 chars
        ("⏰" + "a" * 70, 2),
    ],
)
def test_segment_count(text: str, segments: int) -> None:
    assert segment_count(text) == segments


def test_mask_phone_number() -> None:
    assert mask_phone_number("+15551234567") == "+1••••••4567"
    assert mask_phone_number("+1555") == "+1555"


class TestDefaultCountryPrefix:
    def test_ten_digits_get_the_prefix(self) -> None:
        assert normalize_phone_number("(555) 123-4567", default_prefix="+1") == "+15551234567"

    def test_eleven_digits_starting_with_prefix_get_a_plus(self) -> None:
        assert normalize_phone_number("1 555 123 4567", default_prefix="+1") == "+15551234567"

    def test_international_and_odd_lengths_untouched(self) -> None:
        assert normalize_phone_number("+44 20 7946 0958", default_prefix="+1") == "+442079460958"
        assert normalize_phone_number("555-1234", default_prefix="+1") == "5551234"
        assert normalize_phone_number("5551234567") == "5551234567"  # no prefix configured
