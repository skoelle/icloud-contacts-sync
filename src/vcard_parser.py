"""Parst rohen vCard-Text in ein flaches dict, passend zum contacts-Schema."""
import json
import logging

import vobject

logger = logging.getLogger(__name__)


def _get(vcard, attr, default=None):
    return getattr(vcard, attr).value if hasattr(vcard, attr) else default


def parse_vcard(raw_text: str, account: str) -> dict | None:
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
    given_name = n.value.given if n else None
    family_name = n.value.family if n else None
    middle_name = n.value.additional if n else None
    prefix = n.value.prefix if n else None
    suffix = n.value.suffix if n else None

    emails = [{"type": ",".join(e.type_paramlist) if e.type_paramlist else "other", "value": e.value}
              for e in getattr(vcard, "email_list", [])]
    phones = [{"type": ",".join(t.type_paramlist) if t.type_paramlist else "other", "value": t.value}
              for t in getattr(vcard, "tel_list", [])]

    addresses = []
    for a in getattr(vcard, "adr_list", []):
        v = a.value
        addresses.append({
            "type": ",".join(a.type_paramlist) if a.type_paramlist else "other",
            "street": v.street, "city": v.city, "region": v.region,
            "zip": v.code, "country": v.country,
        })

    urls = [{"type": ",".join(u.type_paramlist) if u.type_paramlist else "other", "value": u.value}
            for u in getattr(vcard, "url_list", [])]

    categories = [c.strip() for c in vcard.categories.value] if hasattr(vcard, "categories") else []

    birthday = str(vcard.bday.value)[:10] if hasattr(vcard, "bday") else None

    org = None
    if hasattr(vcard, "org"):
        org_val = vcard.org.value
        org = org_val[0] if isinstance(org_val, list) else org_val

    return {
        "account": account,
        "uid": uid,
        "etag": None,
        "full_name": _get(vcard, "fn"),
        "given_name": given_name,
        "family_name": family_name,
        "middle_name": middle_name,
        "prefix": prefix,
        "suffix": suffix,
        "nickname": _get(vcard, "nickname"),
        "organization": org,
        "job_title": _get(vcard, "title"),
        "department": None,
        "birthday": birthday,
        "anniversary": None,
        "notes": _get(vcard, "note"),
        "photo_base64": None,
        "emails": json.dumps(emails, ensure_ascii=False),
        "phones": json.dumps(phones, ensure_ascii=False),
        "addresses": json.dumps(addresses, ensure_ascii=False),
        "urls": json.dumps(urls, ensure_ascii=False),
        "social_profiles": json.dumps([], ensure_ascii=False),
        "related_names": json.dumps([], ensure_ascii=False),
        "categories": json.dumps(categories, ensure_ascii=False),
        "raw_vcard": raw_text,
        "source": "icloud",
    }
