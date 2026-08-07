# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
from datetime import date

UNKNOWN_YEARS = frozenset({0, 1604, 1900})


def is_unknown_year(birthday: date) -> bool:
    return birthday.year in UNKNOWN_YEARS


def fmt_birthday_short(birthday: date) -> str:
    if is_unknown_year(birthday):
        return birthday.strftime("%d.%m.")
    return birthday.strftime("%d.%m.%Y")


def fmt_birthday_age(birthday: date, reference) -> int | None:
    if is_unknown_year(birthday):
        return None
    ref_year = reference if isinstance(reference, int) else reference.year
    return ref_year - birthday.year
