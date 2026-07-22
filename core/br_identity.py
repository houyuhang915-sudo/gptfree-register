"""Brazil identity/address generator for PayPal guest automation.

This mirrors the narrow shape used by ``jp_identity.generate_jp_identity_for_paypal``
without changing the Japan-specific kana flow.
"""
from __future__ import annotations

import random
import string
import unicodedata
from typing import Any


BR_FIRST_NAMES: tuple[tuple[str, str], ...] = (
    ("Lucas", "M"), ("Gabriel", "M"), ("Rafael", "M"), ("Mateus", "M"),
    ("Gustavo", "M"), ("Felipe", "M"), ("Bruno", "M"), ("Thiago", "M"),
    ("Eduardo", "M"), ("Andre", "M"), ("Mariana", "F"), ("Juliana", "F"),
    ("Camila", "F"), ("Fernanda", "F"), ("Amanda", "F"), ("Beatriz", "F"),
    ("Carolina", "F"), ("Larissa", "F"), ("Patricia", "F"), ("Renata", "F"),
)

BR_LAST_NAMES: tuple[str, ...] = (
    "Silva", "Santos", "Oliveira", "Souza", "Pereira", "Costa", "Rodrigues",
    "Almeida", "Nascimento", "Lima", "Araújo", "Fernandes", "Carvalho",
    "Gomes", "Martins", "Rocha", "Ribeiro", "Melo", "Barbosa", "Dias",
)

BR_ADDRESSES: tuple[tuple[str, str, str, str], ...] = (
    ("Avenida Paulista", "São Paulo", "SP", "01310-100"),
    ("Rua Augusta", "São Paulo", "SP", "01305-000"),
    ("Rua Oscar Freire", "São Paulo", "SP", "01426-001"),
    ("Avenida Atlântica", "Rio de Janeiro", "RJ", "22021-001"),
    ("Rua Visconde de Pirajá", "Rio de Janeiro", "RJ", "22410-001"),
    ("Avenida Afonso Pena", "Belo Horizonte", "MG", "30130-003"),
    ("Rua da Bahia", "Belo Horizonte", "MG", "30160-011"),
    ("Rua XV de Novembro", "Curitiba", "PR", "80020-310"),
    ("Avenida Sete de Setembro", "Curitiba", "PR", "80230-010"),
    ("Rua dos Andradas", "Porto Alegre", "RS", "90020-007"),
    ("Avenida Beira Mar", "Fortaleza", "CE", "60165-121"),
    ("Avenida Boa Viagem", "Recife", "PE", "51011-000"),
    ("Avenida Tancredo Neves", "Salvador", "BA", "41820-020"),
    ("SCS Quadra 2", "Brasília", "DF", "70302-000"),
)

BR_CARD_BINS: tuple[tuple[str, str], ...] = (
    ("401178", "Visa"),
    ("415275", "Visa"),
    ("451416", "Visa"),
    ("516292", "Mastercard"),
    ("522840", "Mastercard"),
    ("544731", "Mastercard"),
)

BR_STATE_NAMES: dict[str, str] = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo",
    "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina",
    "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins",
}


def _strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", str(value or ""))
        if unicodedata.category(ch) != "Mn"
    )


def br_state_aliases(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    upper = raw.upper()
    code = upper if upper in BR_STATE_NAMES else ""
    if not code:
        raw_plain = _strip_accents(raw).lower()
        for uf, name in BR_STATE_NAMES.items():
            if raw.lower() == name.lower() or raw_plain == _strip_accents(name).lower():
                code = uf
                break
    aliases: list[str] = []
    if code:
        aliases.extend([code, BR_STATE_NAMES[code], _strip_accents(BR_STATE_NAMES[code])])
    else:
        aliases.extend([raw, _strip_accents(raw)])
    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        key = alias.lower()
        if alias and key not in seen:
            seen.add(key)
            out.append(alias)
    return out


def _luhn_check_digit(digits: str) -> str:
    total = 0
    for i, ch in enumerate(digits[::-1]):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def _generate_card_number(bin_prefix: str, total_len: int = 16) -> str:
    mid = "".join(random.choices(string.digits, k=total_len - len(bin_prefix) - 1))
    head = bin_prefix + mid
    return head + _luhn_check_digit(head)


def _gen_dob(min_age: int = 23, max_age: int = 55) -> str:
    from datetime import date
    today = date.today()
    for _ in range(100):
        year = random.randint(today.year - max_age, today.year - min_age)
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        age = today.year - year - ((today.month, today.day) < (month, day))
        if min_age <= age <= max_age:
            return f"{year:04d}/{month:02d}/{day:02d}"
    year = today.year - min_age - 1
    return f"{year:04d}/{today.month:02d}/{today.day:02d}"


def _dob_ymd_to_dmy(value: str) -> str:
    parts = str(value or "").replace("-", "/").split("/")
    if len(parts) != 3:
        return value
    yyyy, mm, dd = parts
    return f"{dd.zfill(2)}/{mm.zfill(2)}/{yyyy.zfill(4)}"


def _gen_expiry() -> str:
    from datetime import date
    year = date.today().year + random.randint(2, 6)
    return f"{random.randint(1, 12):02d}/{str(year)[-2:]}"


def _gen_password(length: int = 12) -> str:
    chars = [
        random.choice("!@#$%&*"),
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
    ]
    chars.extend(random.choices(string.ascii_letters + string.digits, k=max(0, length - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


def _gen_email(first: str, last: str, dob: str) -> str:
    yy = dob.split("/")[0][-2:]
    base = (first + last + yy + "".join(random.choices(string.digits, k=random.randint(0, 2)))).lower()
    base = base.replace("á", "a").replace("ã", "a").replace("é", "e").replace("í", "i")
    return f"{base}@{random.choice(('gmail.com', 'outlook.com', 'hotmail.com'))}"


def _gen_cpf() -> str:
    nums = [random.randint(0, 9) for _ in range(9)]
    for weight_start in (10, 11):
        total = sum(n * w for n, w in zip(nums, range(weight_start, 1, -1)))
        digit = 11 - (total % 11)
        nums.append(0 if digit >= 10 else digit)
    return "{}{}{}.{}{}{}.{}{}{}-{}{}".format(*nums)


def _infer_district(city: str, street_name: str) -> str:
    city_l = (city or "").lower()
    street_l = (street_name or "").lower()
    if "paulista" in street_l or "augusta" in street_l:
        return "Bela Vista"
    if "oscar freire" in street_l:
        return "Jardins"
    if "atlântica" in street_l or "atlantica" in street_l:
        return "Copacabana"
    if "pirajá" in street_l or "piraja" in street_l:
        return "Ipanema"
    if "boa viagem" in street_l:
        return "Boa Viagem"
    if "beira mar" in street_l:
        return "Meireles"
    if "tancredo neves" in street_l:
        return "Caminho das Árvores"
    if "brasília" in city_l or "brasilia" in city_l:
        return "Asa Sul"
    return "Centro"


def generate_br_identity(*, gender: str = "any", card_brand: str = "any") -> dict[str, Any]:
    gender = (gender or "any").upper()
    names = [n for n in BR_FIRST_NAMES if gender in {"M", "F"} and n[1] == gender]
    if not names:
        names = list(BR_FIRST_NAMES)
    first_name, sex = random.choice(names)
    last_name = random.choice(BR_LAST_NAMES)
    street_name, city, state, postal = random.choice(BR_ADDRESSES)
    street_number = str(random.randint(10, 2999))
    district = _infer_district(city, street_name)
    street = street_name
    full_street = f"{street_name}, {street_number}"
    dob = _gen_dob()

    brand_filter = (card_brand or "any").lower()
    bins = [b for b in BR_CARD_BINS if b[1].lower() == brand_filter] or list(BR_CARD_BINS)
    bin_prefix, brand = random.choice(bins)
    card_number = _generate_card_number(bin_prefix)
    card_expiry = _gen_expiry()

    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": f"{first_name} {last_name}",
        "gender": sex,
        "date_of_birth": dob,
        "date_of_birth_dmy": _dob_ymd_to_dmy(dob),
        "cpf": _gen_cpf(),
        "postal_code": postal,
        "state": state,
        "city": city,
        "street": street,
        "street_number": street_number,
        "district": district,
        "full_street": full_street,
        "full_address": f"{full_street}, {district}, {city} - {state}, {postal}, Brasil",
        "card_number": card_number,
        "card_number_formatted": " ".join(card_number[i:i + 4] for i in range(0, len(card_number), 4)),
        "card_bin": bin_prefix,
        "card_brand": brand,
        "card_expiry": card_expiry,
        "card_cvv": f"{random.randint(0, 999):03d}",
        "email": _gen_email(first_name, last_name, dob),
        "password": _gen_password(),
    }


def generate_br_identity_for_paypal(*, gender: str = "any", card_brand: str = "Visa") -> dict[str, Any]:
    base = generate_br_identity(gender=gender, card_brand=card_brand)
    state_name = BR_STATE_NAMES.get(base["state"], base["state"])
    return {
        "first_name": base["first_name"],
        "last_name": base["last_name"],
        "name": base["full_name"],
        "date_of_birth": base["date_of_birth"],
        "date_of_birth_dmy": base["date_of_birth_dmy"],
        "cpf": base["cpf"],
        "billing_postal_code": base["postal_code"],
        "billing_state": base["state"],
        "billing_state_name": state_name,
        "billing_city": base["city"],
        "billing_line1": base["street"],
        "billing_number": base["street_number"],
        "billing_district": base["district"],
        "billing_country": "BR",
        "address": {
            "street": base["street"],
            "number": base["street_number"],
            "district": base["district"],
            "full_street": base["full_street"],
            "city": base["city"],
            "state": base["state"],
            "stateName": state_name,
            "zip": base["postal_code"],
            "country": "BR",
        },
        "card_number": base["card_number"],
        "card_number_formatted": base["card_number_formatted"],
        "cardNumber": base["card_number"],
        "card_bin": base["card_bin"],
        "card_brand": base["card_brand"],
        "card_expiry": base["card_expiry"],
        "cardExpiry": base["card_expiry"],
        "card_cvv": base["card_cvv"],
        "cardCvv": base["card_cvv"],
        "email": base["email"],
        "password": base["password"],
        "gender": base["gender"],
        "region": "BR",
    }


__all__ = [
    "BR_FIRST_NAMES",
    "BR_LAST_NAMES",
    "BR_ADDRESSES",
    "BR_CARD_BINS",
    "BR_STATE_NAMES",
    "br_state_aliases",
    "generate_br_identity",
    "generate_br_identity_for_paypal",
]


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(generate_br_identity(), ensure_ascii=False, indent=2))
