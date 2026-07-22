"""Grouped United Kingdom identity data for the PayPal guest form.

Names, dates and addresses are generated together. Address rows deliberately
keep street, city, PayPal county and postcode as one immutable tuple so a
syntactically plausible but non-existent postcode is never mixed in.
"""
from __future__ import annotations

import random
import string
from datetime import date
from typing import Any


GB_FIRST_NAMES: tuple[tuple[str, str], ...] = (
    ("George", "M"), ("James", "M"), ("Oliver", "M"), ("Harry", "M"),
    ("Jack", "M"), ("Thomas", "M"), ("William", "M"), ("Henry", "M"),
    ("Olivia", "F"), ("Amelia", "F"), ("Isla", "F"), ("Ava", "F"),
    ("Emily", "F"), ("Grace", "F"), ("Sophie", "F"), ("Charlotte", "F"),
)

GB_LAST_NAMES: tuple[str, ...] = (
    "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson",
    "Davies", "Patel", "Robinson", "Wright", "Thompson", "Evans", "Walker",
    "White", "Roberts", "Green", "Hall", "Thomas", "Clarke",
)

# Each row is a complete address unit. Do not randomise the house number or
# postcode independently; UK postcodes can identify a very small group of
# delivery points.
GB_ADDRESSES: tuple[dict[str, str], ...] = (
    {"street": "10 Downing Street", "city": "London", "state": "London",
     "zip": "SW1A 2AA", "country": "GB"},
    {"street": "221B Baker Street", "city": "London", "state": "London",
     "zip": "NW1 6XE", "country": "GB"},
    {"street": "1 King Street", "city": "Manchester", "state": "Greater Manchester",
     "zip": "M2 6AW", "country": "GB"},
    {"street": "12 Deansgate", "city": "Manchester", "state": "Greater Manchester",
     "zip": "M3 1WY", "country": "GB"},
    {"street": "80 Princes Street", "city": "Edinburgh", "state": "Edinburgh City",
     "zip": "EH2 2ER", "country": "GB"},
    {"street": "137 Princes Street", "city": "Edinburgh", "state": "Edinburgh City",
     "zip": "EH1 1SG", "country": "GB"},
    {"street": "6 Royal Avenue", "city": "Belfast", "state": "Belfast City",
     "zip": "BT1 1DA", "country": "GB"},
)


def _gen_dob(min_age: int = 25, max_age: int = 55) -> tuple[str, str]:
    """Return the same date as PayPal GB DMY and ISO values."""
    today = date.today()
    year = today.year - random.randint(min_age, max_age)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{day:02d}/{month:02d}/{year:04d}", f"{year:04d}-{month:02d}-{day:02d}"


def _gen_password(length: int = 12) -> str:
    length = max(8, length)
    required = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@$%&*"),
    ]
    pool = string.ascii_letters + string.digits + "!@$%&*"
    chars = required + random.choices(pool, k=length - len(required))
    random.shuffle(chars)
    return "".join(chars)


def _gen_email(first_name: str, last_name: str) -> str:
    suffix = random.randint(1000, 9999)
    domain = random.choice(("gmail.com", "gmail.com", "outlook.com"))
    return f"{first_name.lower()}.{last_name.lower()}{suffix}@{domain}"


def generate_gb_identity(*, gender: str = "any") -> dict[str, Any]:
    gender = str(gender or "any").upper()
    names = [item for item in GB_FIRST_NAMES if gender in {"M", "F"} and item[1] == gender]
    first_name, sex = random.choice(names or list(GB_FIRST_NAMES))
    last_name = random.choice(GB_LAST_NAMES)
    address = dict(random.choice(GB_ADDRESSES))
    dob_dmy, dob_iso = _gen_dob()
    full_address = (
        f"{address['street']}, {address['city']}, {address['zip']}, United Kingdom"
    )
    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": f"{first_name} {last_name}",
        "gender": sex,
        "date_of_birth": dob_dmy,
        "date_of_birth_dmy": dob_dmy,
        "date_of_birth_iso": dob_iso,
        "postal_code": address["zip"],
        "state": address["state"],
        "city": address["city"],
        "street": address["street"],
        "full_address": full_address,
        "email": _gen_email(first_name, last_name),
        "password": _gen_password(),
        "nationality": "GB",
        "tax_residency_country": "GB",
        "tax_residency_name": "United Kingdom",
        "address": address,
    }


def generate_gb_identity_for_paypal(*, gender: str = "any") -> dict[str, Any]:
    base = generate_gb_identity(gender=gender)
    return {
        **base,
        "name": base["full_name"],
        "billing_postal_code": base["postal_code"],
        "billing_state": base["state"],
        "billing_city": base["city"],
        "billing_line1": base["street"],
        "billing_country": "GB",
        "region": "GB",
    }


__all__ = [
    "GB_FIRST_NAMES",
    "GB_LAST_NAMES",
    "GB_ADDRESSES",
    "generate_gb_identity",
    "generate_gb_identity_for_paypal",
]


if __name__ == "__main__":
    import json

    print(json.dumps(generate_gb_identity(), indent=2))
