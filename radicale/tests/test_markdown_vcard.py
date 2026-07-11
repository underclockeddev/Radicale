# Tests for the markdown storage backend — CardDAV (VCARD) profile

import os
import shutil
import tempfile

import pytest
import vobject
import yaml

from radicale.config import Configuration, DEFAULT_CONFIG_SCHEMA, load
from radicale.item import Item
from radicale.storage.markdown import Collection, Storage
from radicale.storage.markdown.convert_vcard import md_to_vcard, vcard_to_md
from radicale.storage.markdown.frontmatter import (
    parse_md_file, render_md_file)
from radicale.storage.markdown.profile import ProfileRegistry


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="radicale-md-vcard-test-")
    yield d
    shutil.rmtree(d)


@pytest.fixture
def storage(tmpdir):
    cfg = load()
    cfg.update({"storage": {
        "filesystem_folder": tmpdir,
        "type": "markdown",
    }}, "test")
    return Storage(cfg)


# ── Registry ────────────────────────────────────────────────────────────────

def test_registry_resolves_addressbook_profile():
    prof = ProfileRegistry.get("VADDRESSBOOK")
    assert prof.tag == "VADDRESSBOOK"
    assert prof.item_extension == ".vcf"
    assert prof.metadata_filename == ".addressbook.yaml"


def test_registry_resolves_calendar_profile():
    prof = ProfileRegistry.get("VCALENDAR")
    assert prof.tag == "VCALENDAR"
    assert prof.item_extension == ".ics"


def test_registry_unknown_tag_raises():
    with pytest.raises(KeyError):
        ProfileRegistry.get("BOGUS")


def test_registry_or_default_unknown_returns_default():
    prof = ProfileRegistry.get_or_default("BOGUS")
    assert prof.tag == ProfileRegistry.default_tag()


# ── Frontmatter primitives are unchanged ────────────────────────────────────

def test_frontmatter_roundtrip():
    fm = {"uid": "abc", "fn": "Jane"}
    text = render_md_file(fm, "hello")
    fm2, body2 = parse_md_file(text)
    assert fm2 == fm
    assert body2 == "hello"


# ── md → vCard ──────────────────────────────────────────────────────────────

def test_md_to_vcard_minimal():
    fm = {"uid": "u1", "fn": "Jane Public"}
    card = md_to_vcard(fm)
    assert card.fn.value == "Jane Public"
    assert card.uid.value == "u1"


def test_md_to_vcard_structured_n():
    fm = {
        "uid": "u2",
        "fn": "Jane Q. Public, PhD",
        "n": {
            "family": "Public",
            "given": "Jane",
            "additional": ["Q."],
            "prefixes": [],
            "suffixes": ["PhD"],
        },
    }
    card = md_to_vcard(fm)
    assert card.n.value.family == "Public"
    assert card.n.value.given == "Jane"
    assert "PhD" in (card.n.value.suffix or [])


def test_md_to_vcard_typed_email_tel():
    fm = {
        "uid": "u3", "fn": "X",
        "email": [
            {"value": "a@work.example", "type": ["work", "pref"]},
            {"value": "a@home.example", "type": ["home"]},
        ],
        "tel": [
            {"value": "+1-555-0100", "type": ["cell"]},
        ],
    }
    card = md_to_vcard(fm)
    emails = card.contents.get("email", [])
    assert len(emails) == 2
    assert emails[0].value == "a@work.example"
    assert "work" in [t.lower() for t in emails[0].params.get("TYPE", [])]
    tels = card.contents.get("tel", [])
    assert tels[0].value == "+1-555-0100"


def test_md_to_vcard_structured_adr():
    fm = {
        "uid": "u4", "fn": "X",
        "adr": [{
            "type": ["home"],
            "street": "123 Maple St",
            "locality": "Springfield",
            "region": "IL",
            "code": "62701",
            "country": "USA",
        }],
    }
    card = md_to_vcard(fm)
    adr = card.adr.value
    assert adr.street == "123 Maple St"
    assert adr.city == "Springfield"
    assert adr.country == "USA"


def test_md_to_vcard_org_hierarchy():
    fm = {"uid": "u5", "fn": "X",
          "org": ["ACME", "Engineering", "Platform"]}
    card = md_to_vcard(fm)
    assert list(card.org.value) == ["ACME", "Engineering", "Platform"]


def test_md_to_vcard_bday_categories():
    fm = {"uid": "u6", "fn": "X",
          "bday": "1985-04-12",
          "categories": ["friends", "team"]}
    card = md_to_vcard(fm)
    assert str(card.bday.value) == "1985-04-12"
    assert "friends" in card.categories.value


def test_md_to_vcard_unknown_property_preserved():
    fm = {"uid": "u7", "fn": "X",
          "vcard_extra": {"X-CUSTOM-FOO": "bar"}}
    card = md_to_vcard(fm)
    assert "x-custom-foo" in card.contents
    assert card.contents["x-custom-foo"][0].value == "bar"


def test_md_to_vcard_body_becomes_note():
    fm = {"uid": "u8", "fn": "X"}
    card = md_to_vcard(fm, body="Met at PyCon. Hates Tuesdays.")
    assert card.note.value == "Met at PyCon. Hates Tuesdays."


# ── vCard → md ──────────────────────────────────────────────────────────────

def _parse_vcf(text: str):
    return vobject.readOne(text)


SIMPLE_VCARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:4.0\r\n"
    "UID:roundtrip-1\r\n"
    "FN:Jane Public\r\n"
    "N:Public;Jane;Q.;;PhD\r\n"
    "EMAIL;TYPE=WORK,PREF:jane@work.example\r\n"
    "EMAIL;TYPE=HOME:jane@home.example\r\n"
    "TEL;TYPE=CELL:+1-555-0100\r\n"
    "ORG:ACME;Engineering\r\n"
    "TITLE:Staff Engineer\r\n"
    "BDAY:1985-04-12\r\n"
    "CATEGORIES:friends,soccer\r\n"
    "NOTE:Met at PyCon.\r\n"
    "X-CUSTOM-FOO:bar\r\n"
    "END:VCARD\r\n"
)


def test_vcard_to_md_basic():
    card = _parse_vcf(SIMPLE_VCARD)
    fm, body = vcard_to_md(card)
    assert fm["uid"] == "roundtrip-1"
    assert fm["fn"] == "Jane Public"
    assert fm["n"]["family"] == "Public"
    assert fm["n"]["given"] == "Jane"
    assert len(fm["email"]) == 2
    assert fm["email"][0]["value"] == "jane@work.example"
    assert "work" in fm["email"][0]["type"]
    assert fm["tel"][0]["value"] == "+1-555-0100"
    assert fm["org"] == ["ACME", "Engineering"]
    assert fm["title"] == "Staff Engineer"
    assert fm["bday"] == "1985-04-12"
    assert "friends" in fm["categories"]
    assert body == "Met at PyCon."
    assert "X-CUSTOM-FOO" in fm["vcard_extra"]


def test_vcard_roundtrip_preserves_uid_and_fn():
    card = _parse_vcf(SIMPLE_VCARD)
    fm, body = vcard_to_md(card)
    card2 = md_to_vcard(fm, body)
    assert card2.uid.value == card.uid.value
    assert card2.fn.value == card.fn.value


def test_vcard_roundtrip_preserves_unknown_property():
    card = _parse_vcf(SIMPLE_VCARD)
    fm, body = vcard_to_md(card)
    card2 = md_to_vcard(fm, body)
    assert "x-custom-foo" in card2.contents
    assert card2.contents["x-custom-foo"][0].value == "bar"


def test_vcard_roundtrip_typed_emails_preserved():
    card = _parse_vcf(SIMPLE_VCARD)
    fm, body = vcard_to_md(card)
    card2 = md_to_vcard(fm, body)
    emails = card2.contents.get("email", [])
    assert len(emails) == 2
    addrs = {e.value for e in emails}
    assert addrs == {"jane@work.example", "jane@home.example"}


def test_md_render_then_parse_is_idempotent():
    card = _parse_vcf(SIMPLE_VCARD)
    fm, body = vcard_to_md(card)
    text1 = render_md_file(fm, body)
    fm2, body2 = parse_md_file(text1)
    text2 = render_md_file(fm2, body2)
    assert text1 == text2


# ── End-to-end via Storage ──────────────────────────────────────────────────

def test_storage_create_addressbook_and_upload(storage, tmpdir):
    col, _, _ = storage.create_collection(
        "/user/contacts/",
        props={"tag": "VADDRESSBOOK", "D:displayname": "Contacts"})
    assert col.tag == "VADDRESSBOOK"
    assert col.profile.tag == "VADDRESSBOOK"
    assert col.profile.item_extension == ".vcf"

    # Metadata file is .addressbook.yaml
    assert os.path.isfile(os.path.join(
        col._filesystem_path, ".addressbook.yaml"))

    item = Item(collection=col, text=SIMPLE_VCARD)
    uploaded, old = col.upload("roundtrip-1.vcf", item)
    assert old is None
    assert uploaded.href == "roundtrip-1.vcf"

    # File is laid out under contacts/
    contacts_dir = os.path.join(col._filesystem_path, "contacts")
    assert os.path.isdir(contacts_dir)
    files = os.listdir(contacts_dir)
    assert len(files) == 1
    assert files[0].endswith(".md")

    # Round-trip via get_multi
    pairs = list(col.get_multi(["roundtrip-1.vcf"]))
    assert pairs[0][0] == "roundtrip-1.vcf"
    assert pairs[0][1] is not None
    assert "FN:Jane Public" in pairs[0][1].serialize()


def test_storage_isolation_cal_vs_addressbook(storage, tmpdir):
    cal, _, _ = storage.create_collection(
        "/u/cal/",
        props={"tag": "VCALENDAR", "D:displayname": "Cal"})
    ab, _, _ = storage.create_collection(
        "/u/contacts/",
        props={"tag": "VADDRESSBOOK", "D:displayname": "Contacts"})
    assert cal.profile.tag == "VCALENDAR"
    assert ab.profile.tag == "VADDRESSBOOK"
    assert cal._filesystem_path != ab._filesystem_path


def test_storage_delete_addressbook_item(storage, tmpdir):
    col, _, _ = storage.create_collection(
        "/u/contacts/",
        props={"tag": "VADDRESSBOOK", "D:displayname": "Contacts"})
    item = Item(collection=col, text=SIMPLE_VCARD)
    col.upload("roundtrip-1.vcf", item)
    col.delete("roundtrip-1.vcf")
    contacts_dir = os.path.join(col._filesystem_path, "contacts")
    assert not os.path.isdir(contacts_dir) or not os.listdir(contacts_dir)
