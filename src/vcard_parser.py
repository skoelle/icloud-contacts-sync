# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
"""Parst rohen vCard-Text in ein flaches dict, passend zum contacts-Schema."""
import json
import logging

import vobject

logger = logging.getLogger(__name__)


def _get(vcard, attr, default=None):
    return getattr(vcard, attr).value if hasattr(vcard, attr) else default


def _scalar(val):
    """Macht aus einem potenziellen List-Wert einen String (fuer MySQL-PARAMETER)."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return ",".join(str(v) for v in val)
    return str(val)


def _type_str(obj) -> str:
    """Liest den TYPE-Parameter sicher aus einem vobject-Element."""
    if hasattr(obj, "type_paramlist") and obj.type_paramlist:
        return ",".join(obj.type_paramlist)
    params = getattr(obj, "params", {})
    type_val = params.get("TYPE") or params.get("type")
    if type_val:
        return type_val if isinstance(type_val, str) else ",".join(type_val)
    return "other"


def parse_vcard(raw_text: str, account: str, etag: str | None = None) -> dict | None:
    try:
        vcard = vobject.readOne(raw_text)
    except Exception as exc:
        logger.warning("vCard konnte nicht geparst werden: %s", exc)
        return None

    uid = _get(vcard, "uid")
    if not uid:
        logger.warning("vCard ohne UID übersprungen")
        return None

    n = getattr(vcard, "n", None)
    given_name = _scalar(n.value.given) if n else None
    family_name = _scalar(n.value.family) if n else None
    middle_name = _scalar(n.value.additional) if n else None
    prefix = _scalar(n.value.prefix) if n else None
    suffix = _scalar(n.value.suffix) if n else None

    emails = [{"type": _type_str(e), "value": e.value}
              for e in getattr(vcard, "email_list", [])]
    phones = [{"type": _type_str(t), "value": t.value}
              for t in getattr(vcard, "tel_list", [])]

    addresses = []
    for a in getattr(vcard, "adr_list", []):
        v = a.value
        addresses.append({
            "type": _type_str(a),
            "street": v.street, "city": v.city, "region": v.region,
            "zip": v.code, "country": v.country,
        })

    urls = [{"type": _type_str(u), "value": u.value}
            for u in getattr(vcard, "url_list", [])]

    social_profiles = []
    for sp in vcard.contents.get("x-socialprofile", []):
        params = getattr(sp, "params", {})
        type_val = params.get("TYPE") or params.get("type") or "other"
        if isinstance(type_val, list):
            type_val = type_val[0] if type_val else "other"
        username = params.get("X-USER") or params.get("x-user") or ""
        if isinstance(username, list):
            username = username[0] if username else ""
        social_profiles.append({
            "type": type_val,
            "username": username,
            "url": sp.value if sp.value else "",
        })

    categories = [c.strip() for c in vcard.categories.value] if hasattr(vcard, "categories") else []

    birthday = str(vcard.bday.value)[:10] if hasattr(vcard, "bday") else None

    org = None
    if hasattr(vcard, "org"):
        org_val = vcard.org.value
        org = org_val[0] if isinstance(org_val, list) else org_val

    return {
        "account": account,
        "uid": uid,
        "etag": etag,
        "full_name": _scalar(_get(vcard, "fn")),
        "given_name": given_name,
        "family_name": family_name,
        "middle_name": middle_name,
        "prefix": prefix,
        "suffix": suffix,
        "nickname": _scalar(_get(vcard, "nickname")),
        "organization": org,
        "job_title": _scalar(_get(vcard, "title")),
        "department": None,
        "birthday": birthday,
        "anniversary": None,
        "notes": _scalar(_get(vcard, "note")),
        "photo_base64": None,
        "emails": json.dumps(emails, ensure_ascii=False),
        "phones": json.dumps(phones, ensure_ascii=False),
        "addresses": json.dumps(addresses, ensure_ascii=False),
        "urls": json.dumps(urls, ensure_ascii=False),
        "social_profiles": json.dumps(social_profiles, ensure_ascii=False),
        "related_names": json.dumps([], ensure_ascii=False),
        "categories": json.dumps(categories, ensure_ascii=False),
        "raw_vcard": raw_text,
        "source": "icloud",
    }
