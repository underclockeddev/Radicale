# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend — vCard converter
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Conversion between Markdown (YAML frontmatter) and vCard (VCARD).

Bidirectional:
  - md frontmatter + body → vobject VCARD (for CardDAV serving)
  - vobject VCARD → frontmatter + body (for CardDAV writes)

Frontmatter schema mirrors the iCalendar converter's design:

- single-cardinality scalars: ``fn``, ``title``, ``bday``, ``rev`` etc.
- structured fields: ``n``, ``adr``, ``geo`` (nested mappings).
- typed multi-valued fields: ``email``, ``tel``, ``url``, ``impp``,
  ``related`` (list of ``{value, type}`` mappings).
- unknown / X-properties: ``vcard_extra`` (mirrors ``ical_extra``).
- body: single ``NOTE`` property. Extra NOTEs go to ``vcard_extra``.
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional, Tuple

import vobject

from radicale.storage.markdown.profile import ItemConverter


# ── Schema constants ────────────────────────────────────────────────────────

# Properties handled explicitly by this converter; everything else is routed
# to ``vcard_extra`` for lossless round-trip.
_HANDLED_VCARD_PROPS = {
    "version", "uid", "fn", "n", "nickname", "kind",
    "email", "tel", "impp", "adr", "label",
    "org", "title", "role",
    "bday", "anniversary",
    "url", "categories", "photo", "geo", "related", "lang", "tz",
    "note", "rev",
}

# Structured-text properties — N and ADR. RFC 6350 § 6.2.2 / § 6.3.1.
_N_FIELDS = ("family", "given", "additional", "prefixes", "suffixes")
_ADR_FIELDS = ("pobox", "extended", "street", "locality",
               "region", "code", "country")


# ── Helpers: vCard ⇄ frontmatter primitives ────────────────────────────────

def _norm_type_params(prop: vobject.base.ContentLine) -> List[str]:
    """Return the TYPE parameter list, lowercased, as a list of strings."""
    params = getattr(prop, "params", {}) or {}
    raw = params.get("TYPE", [])
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for v in raw:
        if isinstance(v, list):
            out.extend(str(x).lower() for x in v)
        else:
            out.append(str(v).lower())
    # PREF=1 → fold into TYPE list for ergonomic frontmatter shape
    pref = params.get("PREF")
    if pref:
        if "pref" not in out:
            out.append("pref")
    return out


def _set_type_params(prop: vobject.base.ContentLine,
                     types: Optional[List[str]]) -> None:
    if not types:
        return
    canonical = [str(t).lower() for t in types]
    prop.params["TYPE"] = list(canonical)


def _split_structured(value: str, fields: Tuple[str, ...]
                      ) -> Dict[str, Any]:
    """Split a vCard semicolon-delimited structured value into a dict.

    Empty components → empty string (or empty list for ``additional``,
    ``prefixes``, ``suffixes`` per N's spec which permits multi-values).
    """
    parts = value.split(";")
    out: Dict[str, Any] = {}
    multi_in_n = {"additional", "prefixes", "suffixes"}
    for i, name in enumerate(fields):
        raw = parts[i] if i < len(parts) else ""
        if fields is _N_FIELDS and name in multi_in_n:
            out[name] = [s for s in raw.split(",") if s]
        else:
            out[name] = raw
    return out


def _join_structured(d: Dict[str, Any], fields: Tuple[str, ...]) -> str:
    parts: List[str] = []
    multi_in_n = {"additional", "prefixes", "suffixes"}
    for name in fields:
        v = d.get(name, "")
        if fields is _N_FIELDS and name in multi_in_n and isinstance(v, list):
            parts.append(",".join(str(x) for x in v))
        else:
            parts.append(str(v) if v is not None else "")
    return ";".join(parts)


def _typed_multi_to_fm(card: vobject.base.Component, prop_name: str
                       ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for prop in card.contents.get(prop_name, []):
        entry: Dict[str, Any] = {"value": str(prop.value)}
        types = _norm_type_params(prop)
        if types:
            entry["type"] = types
        out.append(entry)
    return out


def _typed_multi_from_fm(card: vobject.base.Component, prop_name: str,
                         entries: Any) -> None:
    if not entries:
        return
    if isinstance(entries, str) or isinstance(entries, dict):
        entries = [entries]
    for entry in entries:
        if isinstance(entry, dict):
            value = entry.get("value", "")
            types = entry.get("type")
        else:
            value = str(entry)
            types = None
        prop = card.add(prop_name)
        prop.value = str(value)
        _set_type_params(prop, types)


# ── md → vCard ──────────────────────────────────────────────────────────────

def md_to_vcard(fm: Dict[str, Any], body: str = ""
                ) -> vobject.base.Component:
    """Build a vobject VCARD component from frontmatter + body."""
    card = vobject.vCard()

    # VERSION — default 4.0, but honor explicitly stored vcard_version
    version_val = str(fm.get("vcard_version", "4.0"))
    card.add("version").value = version_val

    # UID
    uid = fm.get("uid") or str(uuid.uuid4())
    card.add("uid").value = str(uid)

    # FN (required by RFC 6350)
    fn_val = fm.get("fn")
    if not fn_val:
        # Derive from N if missing
        n_val = fm.get("n")
        if isinstance(n_val, dict):
            fn_val = " ".join(filter(None, [
                str(n_val.get("given") or ""),
                str(n_val.get("family") or ""),
            ])).strip()
        fn_val = fn_val or "Unnamed Contact"
    card.add("fn").value = str(fn_val)

    # N — structured
    n = fm.get("n")
    if isinstance(n, dict):
        n_prop = card.add("n")
        # vobject's N expects a vobject.vcard.Name; build from string parts
        from vobject.vcard import Name
        n_prop.value = Name(
            family=str(n.get("family", "") or ""),
            given=str(n.get("given", "") or ""),
            additional=(n.get("additional") or []),
            prefix=(n.get("prefixes") or []),
            suffix=(n.get("suffixes") or []),
        )

    # NICKNAME — single or list
    nick = fm.get("nickname")
    if nick:
        if isinstance(nick, list):
            nick = ",".join(str(n) for n in nick)
        card.add("nickname").value = str(nick)

    # KIND
    if "kind" in fm:
        card.add("kind").value = str(fm["kind"])

    # Typed multi: EMAIL, TEL, IMPP, URL, RELATED
    _typed_multi_from_fm(card, "email", fm.get("email"))
    _typed_multi_from_fm(card, "tel", fm.get("tel"))
    _typed_multi_from_fm(card, "impp", fm.get("impp"))
    _typed_multi_from_fm(card, "url", fm.get("url"))
    _typed_multi_from_fm(card, "related", fm.get("related"))

    # ADR — structured + typed multi
    adrs = fm.get("adr")
    if adrs:
        if isinstance(adrs, dict):
            adrs = [adrs]
        for adr in adrs:
            adr_prop = card.add("adr")
            # vobject's ADR expects vobject.vcard.Address
            from vobject.vcard import Address
            adr_prop.value = Address(
                box=str(adr.get("pobox", "") or ""),
                extended=str(adr.get("extended", "") or ""),
                street=str(adr.get("street", "") or ""),
                city=str(adr.get("locality", "") or ""),
                region=str(adr.get("region", "") or ""),
                code=str(adr.get("code", "") or ""),
                country=str(adr.get("country", "") or ""),
            )
            _set_type_params(adr_prop, adr.get("type"))
            if adr.get("label"):
                adr_prop.params["LABEL"] = [str(adr["label"])]

    # ORG — list (hierarchy)
    org = fm.get("org")
    if org:
        if isinstance(org, str):
            org = [org]
        card.add("org").value = list(org)

    # TITLE / ROLE
    if "title" in fm:
        card.add("title").value = str(fm["title"])
    if "role" in fm:
        card.add("role").value = str(fm["role"])

    # BDAY / ANNIVERSARY (stored as YYYY-MM-DD strings)
    for fm_key, vc_name in (("bday", "bday"),
                            ("anniversary", "anniversary")):
        if fm_key in fm:
            val = fm[fm_key]
            if isinstance(val, (datetime.date, datetime.datetime)):
                val = val.strftime("%Y-%m-%d")
            card.add(vc_name).value = str(val)

    # CATEGORIES
    cats = fm.get("categories")
    if cats:
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",")]
        card.add("categories").value = list(cats)

    # PHOTO — {type: uri|data, value: ..., mediatype: ...}
    photo = fm.get("photo")
    if isinstance(photo, dict):
        photo_prop = card.add("photo")
        kind = (photo.get("type") or "uri").lower()
        if kind == "data":
            photo_prop.value = str(photo.get("data", ""))
            photo_prop.encoded = False
            photo_prop.params["ENCODING"] = ["b"]
            mt = photo.get("mediatype", "image/jpeg")
            photo_prop.params["TYPE"] = [str(mt).split("/")[-1].upper()]
        else:
            photo_prop.value = str(photo.get("value", ""))
            if version_val.startswith("4"):
                photo_prop.params["VALUE"] = ["uri"]

    # GEO — {lat, lon}
    geo = fm.get("geo")
    if isinstance(geo, dict) and "lat" in geo and "lon" in geo:
        geo_prop = card.add("geo")
        if version_val.startswith("4"):
            geo_prop.value = "geo:%s,%s" % (geo["lat"], geo["lon"])
        else:
            geo_prop.value = "%s;%s" % (geo["lat"], geo["lon"])

    # LANG
    langs = fm.get("lang")
    if langs:
        if isinstance(langs, str):
            langs = [langs]
        for lang in langs:
            card.add("lang").value = str(lang)

    # TZ
    if "tz" in fm:
        card.add("tz").value = str(fm["tz"])

    # NOTE — body
    if body:
        card.add("note").value = body

    # REV
    if "rev" in fm:
        card.add("rev").value = str(fm["rev"])

    # vcard_extra: inject preserved properties (X-* and other unknowns)
    extra = fm.get("vcard_extra", {})
    if isinstance(extra, dict):
        _apply_vcard_extra(card, extra)

    return card


def _apply_vcard_extra(card: vobject.base.Component,
                       extra: Dict[str, Any]) -> None:
    for prop_name, prop_value in extra.items():
        name_upper = prop_name.upper()
        values = prop_value if isinstance(prop_value, list) else [prop_value]
        for val in values:
            try:
                if isinstance(val, dict):
                    prop = card.add(name_upper)
                    prop.value = str(val.get("value", ""))
                    types = val.get("type")
                    if types:
                        prop.params["TYPE"] = (list(types)
                                               if isinstance(types, list)
                                               else [str(types)])
                else:
                    prop = card.add(name_upper)
                    prop.value = str(val)
            except Exception:
                pass


# ── vCard → md ──────────────────────────────────────────────────────────────

def vcard_to_md(card: vobject.base.Component
                ) -> Tuple[Dict[str, Any], str]:
    """Convert a vobject VCARD component to ``(frontmatter, body)``."""
    fm: Dict[str, Any] = {}
    body = ""

    # VERSION
    if hasattr(card, "version"):
        fm["vcard_version"] = str(card.version.value)

    # UID
    if hasattr(card, "uid"):
        fm["uid"] = str(card.uid.value)
    else:
        fm["uid"] = str(uuid.uuid4())

    # FN
    if hasattr(card, "fn"):
        fm["fn"] = str(card.fn.value)

    # N
    if hasattr(card, "n"):
        nv = card.n.value
        # vobject Name has attributes family, given, additional, prefix, suffix
        def _as_list(v: Any) -> List[str]:
            if v is None or v == "":
                return []
            if isinstance(v, list):
                return [str(x) for x in v if x]
            return [s for s in str(v).split(",") if s]

        fm["n"] = {
            "family": str(getattr(nv, "family", "") or ""),
            "given": str(getattr(nv, "given", "") or ""),
            "additional": _as_list(getattr(nv, "additional", None)),
            "prefixes": _as_list(getattr(nv, "prefix", None)),
            "suffixes": _as_list(getattr(nv, "suffix", None)),
        }

    # NICKNAME
    if hasattr(card, "nickname"):
        raw = str(card.nickname.value)
        if "," in raw:
            fm["nickname"] = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            fm["nickname"] = raw

    # KIND
    if hasattr(card, "kind"):
        fm["kind"] = str(card.kind.value)

    # Typed multis
    for prop_name, fm_key in (("email", "email"), ("tel", "tel"),
                              ("impp", "impp"), ("url", "url"),
                              ("related", "related")):
        entries = _typed_multi_to_fm(card, prop_name)
        if entries:
            fm[fm_key] = entries

    # ADR — structured + typed
    adrs_out: List[Dict[str, Any]] = []
    for adr_prop in card.contents.get("adr", []):
        av = adr_prop.value
        entry: Dict[str, Any] = {
            "pobox": str(getattr(av, "box", "") or ""),
            "extended": str(getattr(av, "extended", "") or ""),
            "street": str(getattr(av, "street", "") or ""),
            "locality": str(getattr(av, "city", "") or ""),
            "region": str(getattr(av, "region", "") or ""),
            "code": str(getattr(av, "code", "") or ""),
            "country": str(getattr(av, "country", "") or ""),
        }
        types = _norm_type_params(adr_prop)
        if types:
            entry["type"] = types
        label = adr_prop.params.get("LABEL") if hasattr(adr_prop, "params") else None
        if label:
            entry["label"] = label[0] if isinstance(label, list) else str(label)
        adrs_out.append(entry)
    if adrs_out:
        fm["adr"] = adrs_out

    # ORG
    if hasattr(card, "org"):
        ov = card.org.value
        fm["org"] = list(ov) if isinstance(ov, list) else [str(ov)]

    # TITLE / ROLE
    if hasattr(card, "title"):
        fm["title"] = str(card.title.value)
    if hasattr(card, "role"):
        fm["role"] = str(card.role.value)

    # BDAY / ANNIVERSARY
    for vc_name, fm_key in (("bday", "bday"),
                            ("anniversary", "anniversary")):
        if hasattr(card, vc_name):
            v = getattr(card, vc_name).value
            if isinstance(v, (datetime.date, datetime.datetime)):
                fm[fm_key] = v.strftime("%Y-%m-%d")
            else:
                fm[fm_key] = str(v)

    # CATEGORIES
    if hasattr(card, "categories"):
        cv = card.categories.value
        fm["categories"] = list(cv) if isinstance(cv, list) else [str(cv)]

    # PHOTO
    if hasattr(card, "photo"):
        photo_prop = card.photo
        params = getattr(photo_prop, "params", {}) or {}
        encoding = params.get("ENCODING", [])
        if encoding and str(encoding[0]).lower() in ("b", "base64"):
            type_param = params.get("TYPE", [""])[0]
            fm["photo"] = {
                "type": "data",
                "mediatype": "image/" + str(type_param).lower(),
                "data": str(photo_prop.value),
            }
        else:
            fm["photo"] = {"type": "uri", "value": str(photo_prop.value)}

    # GEO
    if hasattr(card, "geo"):
        gv = str(card.geo.value)
        if gv.lower().startswith("geo:"):
            gv = gv[4:]
        for sep in (",", ";"):
            if sep in gv:
                lat, _, lon = gv.partition(sep)
                try:
                    fm["geo"] = {"lat": float(lat.strip()),
                                 "lon": float(lon.strip())}
                except ValueError:
                    fm["geo"] = {"raw": str(card.geo.value)}
                break

    # LANG (multi)
    langs = []
    for lp in card.contents.get("lang", []):
        langs.append(str(lp.value))
    if langs:
        fm["lang"] = langs

    # TZ
    if hasattr(card, "tz"):
        fm["tz"] = str(card.tz.value)

    # NOTE → body. Extra NOTEs preserved in vcard_extra.
    notes = card.contents.get("note", [])
    if notes:
        body = str(notes[0].value)
        if len(notes) > 1:
            extra_notes = [str(n.value) for n in notes[1:]]

    # REV
    if hasattr(card, "rev"):
        rv = card.rev.value
        if isinstance(rv, (datetime.date, datetime.datetime)):
            fm["rev"] = rv.strftime("%Y%m%dT%H%M%SZ")
        else:
            fm["rev"] = str(rv)

    # Collect unhandled properties (X-* + everything else) → vcard_extra
    extra: Dict[str, Any] = {}
    for prop_name, prop_list in card.contents.items():
        lname = prop_name.lower()
        if lname in _HANDLED_VCARD_PROPS:
            continue
        # Skip sub-components (vCard 4 has none in practice)
        if prop_list and hasattr(prop_list[0], "components"):
            continue
        values: List[Any] = []
        for prop in prop_list:
            try:
                types = _norm_type_params(prop)
                if types:
                    values.append({"value": str(prop.value), "type": types})
                else:
                    values.append(str(prop.value))
            except Exception:
                try:
                    values.append(prop.serialize().strip())
                except Exception:
                    pass
        if values:
            extra[prop_name.upper()] = (values[0] if len(values) == 1
                                         else values)

    # Multiple NOTEs (the 2nd+) — store under vcard_extra to preserve
    if "notes" in dir() and len(notes) > 1:
        extra.setdefault("NOTE", []).extend([str(n.value) for n in notes[1:]])

    if extra:
        fm["vcard_extra"] = extra

    return fm, body


# ── Profile-conformant adapter ──────────────────────────────────────────────

class VCardConverter(ItemConverter):
    """``ItemConverter`` strategy for VADDRESSBOOK collections."""

    def md_text_to_vobject(self, fm: Dict[str, Any], body: str
                           ) -> vobject.base.Component:
        return md_to_vcard(fm, body)

    def vobject_to_md(self, comp: vobject.base.Component,
                      context: Optional[Dict[str, Any]] = None
                      ) -> Tuple[Dict[str, Any], str]:
        # CardDAV items may arrive as a bare VCARD or as a containing
        # vobject wrapper — normalize.
        if comp.name == "VCARD":
            return vcard_to_md(comp)
        # Some Radicale paths wrap the VCARD; pick first VCARD child.
        for child in getattr(comp, "components", lambda: [])():
            if child.name == "VCARD":
                return vcard_to_md(child)
        return vcard_to_md(comp)

    def serialize_for_protocol(self, comp: vobject.base.Component) -> str:
        return comp.serialize()

    def extract_uid(self, fm: Dict[str, Any]) -> Optional[str]:
        uid = fm.get("uid")
        return str(uid) if uid else None
