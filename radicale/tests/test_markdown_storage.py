# Tests for the markdown storage backend

import datetime
import os
import shutil
import tempfile

import pytest
import vobject
import yaml

from radicale.config import Configuration, DEFAULT_CONFIG_SCHEMA, load
from radicale.item import Item
from radicale.storage.markdown import Collection, Storage
from radicale.storage.markdown.convert import (
    event_path_components,
    md_filename_for_event,
    md_to_vcalendar,
    parse_md_file,
    render_md_file,
    slugify,
    vcalendar_to_md,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpdir():
    d = tempfile.mkdtemp(prefix="radicale-md-test-")
    yield d
    shutil.rmtree(d)


@pytest.fixture
def populated_dir(tmpdir):
    """Create a populated markdown calendar structure."""
    root = os.path.join(tmpdir, "collection-root")
    cal = os.path.join(root, "user", "work")
    os.makedirs(os.path.join(cal, "2026", "03"))
    os.makedirs(os.path.join(cal, "recurring"))

    with open(os.path.join(cal, ".calendar.yaml"), "w") as f:
        yaml.dump({"tag": "VCALENDAR", "D:displayname": "Work"}, f)

    with open(os.path.join(cal, "2026", "03", "20_team-lunch.md"), "w") as f:
        f.write("---\nuid: lunch-001\ntitle: Team Lunch\n"
                "start: 2026-03-20T12:00:00\nend: 2026-03-20T13:00:00\n"
                "location: Cafe\n---\n\nBring food!\n")

    with open(os.path.join(cal, "2026", "03", "21_dentist.md"), "w") as f:
        f.write("---\nuid: dentist-001\ntitle: Dentist\n"
                "start: 2026-03-21T10:00:00\nend: 2026-03-21T11:00:00\n"
                "---\n")

    with open(os.path.join(cal, "recurring", "standup.md"), "w") as f:
        f.write('---\nuid: standup-001\ntitle: Daily Standup\n'
                'start_time: "09:00"\nend_time: "09:15"\n'
                'dtstart: 2026-01-05\n'
                'rrule: "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"\n---\n')

    return tmpdir


@pytest.fixture
def storage(populated_dir):
    config = load()
    config.update(
        {"storage": {"type": "markdown",
                      "filesystem_folder": populated_dir}},
        "test",
    )
    return Storage(config)


# ── convert.py tests ────────────────────────────────────────────────────────

class TestParseMdFile:
    def test_basic_parse(self):
        fm, body = parse_md_file(
            "---\ntitle: Hello\n---\n\nSome body text.")
        assert fm["title"] == "Hello"
        assert "body text" in body

    def test_no_frontmatter(self):
        fm, body = parse_md_file("Just plain text")
        assert fm == {}
        assert "plain text" in body

    def test_empty_frontmatter(self):
        fm, body = parse_md_file("---\n---\n\nBody")
        assert fm == {}
        assert "Body" in body


class TestRenderMdFile:
    def test_round_trip(self):
        fm = {"uid": "test-1", "title": "Meeting"}
        body = "Discussion notes."
        text = render_md_file(fm, body)
        fm2, body2 = parse_md_file(text)
        assert fm2["uid"] == "test-1"
        assert fm2["title"] == "Meeting"
        assert "Discussion notes" in body2


class TestSlugify:
    def test_basic(self):
        assert slugify("Team Lunch") == "team-lunch"

    def test_special_chars(self):
        assert slugify("Q&A Session!") == "qa-session"

    def test_long_title(self):
        slug = slugify("A" * 100)
        assert len(slug) <= 60


class TestMdFilename:
    def test_dated_event(self):
        fm = {"title": "Sprint Review", "start": "2026-03-15T14:00:00"}
        assert md_filename_for_event(fm) == "15_sprint-review.md"

    def test_no_date(self):
        fm = {"title": "Some Event"}
        assert md_filename_for_event(fm) == "some-event.md"


class TestEventPathComponents:
    def test_dated_event(self):
        fm = {"title": "Lunch", "start": "2026-03-20T12:00:00"}
        result = event_path_components(fm)
        assert result == ("2026", "03", "20_lunch.md")

    def test_recurring_returns_none(self):
        fm = {"title": "Standup", "rrule": "FREQ=DAILY"}
        assert event_path_components(fm) is None


class TestMdToVcalendar:
    def test_basic_event(self):
        fm = {"uid": "test-1", "title": "Meeting",
              "start": "2026-03-20T14:00:00",
              "end": "2026-03-20T15:00:00"}
        cal = md_to_vcalendar(fm)
        text = cal.serialize()
        assert "VEVENT" in text
        assert "test-1" in text
        assert "Meeting" in text

    def test_categories(self):
        fm = {"uid": "t1", "title": "X", "start": "2026-01-01",
              "categories": ["work", "urgent"]}
        cal = md_to_vcalendar(fm)
        text = cal.serialize()
        assert "CATEGORIES" in text

    def test_recurring_event(self):
        fm = {"uid": "r1", "title": "Standup",
              "start_time": "09:00", "end_time": "09:30",
              "dtstart": "2026-01-05",
              "rrule": "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"}
        cal = md_to_vcalendar(fm)
        text = cal.serialize()
        assert "RRULE" in text
        assert "FREQ=DAILY" in text

    def test_body_becomes_description(self):
        fm = {"uid": "t1", "title": "X", "start": "2026-01-01"}
        cal = md_to_vcalendar(fm, body="Important notes.")
        assert cal.vevent.description.value == "Important notes."

    def test_all_day_event(self):
        fm = {"uid": "t1", "title": "Holiday",
              "start": "2026-12-25", "all_day": True}
        cal = md_to_vcalendar(fm)
        assert isinstance(cal.vevent.dtstart.value, datetime.date)


class TestVcalendarToMd:
    def test_basic_round_trip(self):
        cal = vobject.readOne(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:rt1\r\n"
            "SUMMARY:Test\r\nDTSTART:20260320T100000\r\n"
            "DTEND:20260320T110000\r\nLOCATION:Room A\r\n"
            "END:VEVENT\r\nEND:VCALENDAR")
        fm, body = vcalendar_to_md(cal)
        assert fm["uid"] == "rt1"
        assert fm["title"] == "Test"
        assert fm["location"] == "Room A"
        assert "2026-03-20" in fm["start"]

    def test_preserves_unknown_properties(self):
        cal = vobject.readOne(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:rt2\r\n"
            "SUMMARY:Test\r\nDTSTART:20260320T100000\r\n"
            "X-CUSTOM-PROP:hello\r\n"
            "END:VEVENT\r\nEND:VCALENDAR")
        fm, body = vcalendar_to_md(cal)
        assert "ical_extra" in fm
        assert "X-CUSTOM-PROP" in fm["ical_extra"]


class TestFullConversionRoundTrip:
    def test_md_to_ical_to_md(self):
        fm_orig = {
            "uid": "round-1",
            "title": "Sprint Planning",
            "start": "2026-04-01T10:00:00",
            "end": "2026-04-01T11:30:00",
            "location": "Room B",
            "categories": ["meetings"],
        }
        body_orig = "Bring your laptops."

        # MD → iCal
        cal = md_to_vcalendar(fm_orig, body_orig)

        # iCal → MD
        fm_rt, body_rt = vcalendar_to_md(cal)
        assert fm_rt["uid"] == "round-1"
        assert fm_rt["title"] == "Sprint Planning"
        assert "Room B" in fm_rt["location"]
        assert "Bring your laptops" in body_rt


# ── Storage tests ───────────────────────────────────────────────────────────

class TestStorageDiscover:
    def test_discover_root(self, storage):
        items = list(storage.discover("/", depth="1"))
        paths = [getattr(i, "path", None) for i in items]
        assert "" in paths  # root collection
        assert "user" in paths

    def test_discover_user(self, storage):
        items = list(storage.discover("/user/", depth="1"))
        paths = [getattr(i, "path", None) for i in items]
        assert "user" in paths
        assert "user/work" in paths

    def test_discover_collection(self, storage):
        items = list(storage.discover("/user/work/", depth="1"))
        collections = [i for i in items if isinstance(i, Collection)]
        events = [i for i in items if isinstance(i, Item)]
        assert len(collections) == 1
        assert collections[0].tag == "VCALENDAR"
        assert len(events) == 3  # lunch, dentist, standup

    def test_discover_item(self, storage):
        items = list(storage.discover("/user/work/lunch-001.ics", depth="0"))
        assert len(items) == 1
        assert items[0].uid == "lunch-001"

    def test_discover_nonexistent(self, storage):
        items = list(storage.discover("/user/work/nope.ics", depth="0"))
        assert len(items) == 0


class TestCollectionRead:
    def test_get_all(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        items = list(col.get_all())
        uids = {item.uid for item in items}
        assert "lunch-001" in uids
        assert "dentist-001" in uids
        assert "standup-001" in uids

    def test_get_multi(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        results = dict(col.get_multi(
            ["lunch-001.ics", "standup-001.ics", "nonexistent.ics"]))
        assert results["lunch-001.ics"] is not None
        assert results["standup-001.ics"] is not None
        assert results["nonexistent.ics"] is None

    def test_get_meta(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        assert col.get_meta("tag") == "VCALENDAR"
        assert col.get_meta("D:displayname") == "Work"

    def test_etag_changes(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        etag1 = col.etag

        # Add an event
        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "new-1"
        ev.add("summary").value = "New Event"
        ev.add("dtstart").value = datetime.datetime(2026, 5, 1, 10, 0, 0)
        item = Item(collection=col, vobject_item=cal, href="new-1.ics")
        col.upload("new-1.ics", item)

        etag2 = col.etag
        assert etag1 != etag2

    def test_has_uid(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        assert col.has_uid("lunch-001")
        assert not col.has_uid("nonexistent")


class TestCollectionWrite:
    def test_upload_new_event(self, storage, populated_dir):
        col = list(storage.discover("/user/work/", depth="0"))[0]

        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "new-event-1"
        ev.add("summary").value = "Sprint Planning"
        ev.add("dtstart").value = datetime.datetime(2026, 4, 1, 10, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 4, 1, 11, 30, 0)

        item = Item(collection=col, vobject_item=cal, href="new-event-1.ics")
        uploaded, old = col.upload("new-event-1.ics", item)

        assert uploaded.uid == "new-event-1"
        assert old is None

        # Verify MD file exists at correct path
        expected = os.path.join(
            populated_dir, "collection-root", "user", "work",
            "2026", "04", "01_sprint-planning.md")
        assert os.path.isfile(expected)

        # Verify content
        with open(expected) as f:
            fm, body = parse_md_file(f.read())
        assert fm["uid"] == "new-event-1"
        assert fm["title"] == "Sprint Planning"

    def test_upload_recurring_event(self, storage, populated_dir):
        col = list(storage.discover("/user/work/", depth="0"))[0]

        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "weekly-sync"
        ev.add("summary").value = "Weekly Sync"
        ev.add("dtstart").value = datetime.datetime(2026, 1, 5, 14, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 1, 5, 15, 0, 0)
        ev.add("rrule").value = "FREQ=WEEKLY;BYDAY=MO"

        item = Item(collection=col, vobject_item=cal, href="weekly-sync.ics")
        uploaded, old = col.upload("weekly-sync.ics", item)

        assert uploaded.uid == "weekly-sync"

        # Verify it went to recurring/
        recurring_dir = os.path.join(
            populated_dir, "collection-root", "user", "work", "recurring")
        files = os.listdir(recurring_dir)
        assert any("weekly-sync" in f for f in files)

    def test_delete_item(self, storage, populated_dir):
        col = list(storage.discover("/user/work/", depth="0"))[0]

        # Verify item exists
        results = dict(col.get_multi(["lunch-001.ics"]))
        assert results["lunch-001.ics"] is not None

        # Delete it
        col.delete("lunch-001.ics")

        # Verify file removed
        md_path = os.path.join(
            populated_dir, "collection-root", "user", "work",
            "2026", "03", "20_team-lunch.md")
        assert not os.path.isfile(md_path)

    def test_delete_cleans_empty_dirs(self, storage, populated_dir):
        col = list(storage.discover("/user/work/", depth="0"))[0]

        # Delete both events in 2026/03/
        col.delete("lunch-001.ics")
        col.delete("dentist-001.ics")

        # 03/ directory should be cleaned up
        march_dir = os.path.join(
            populated_dir, "collection-root", "user", "work", "2026", "03")
        assert not os.path.isdir(march_dir)

    def test_set_meta(self, storage):
        col = list(storage.discover("/user/work/", depth="0"))[0]
        col.set_meta({"tag": "VCALENDAR", "D:displayname": "Personal"})
        assert col.get_meta("D:displayname") == "Personal"


class TestStorageOperations:
    def test_verify(self, storage):
        assert storage.verify()

    def test_create_collection(self, storage, populated_dir):
        col, replaced, new_hrefs = storage.create_collection(
            "/user/personal/",
            props={"tag": "VCALENDAR",
                   "D:displayname": "Personal Calendar"})
        assert col.path == "user/personal"
        assert col.get_meta("tag") == "VCALENDAR"
        assert col.get_meta("D:displayname") == "Personal Calendar"

        # Verify directory exists
        expected = os.path.join(
            populated_dir, "collection-root", "user", "personal")
        assert os.path.isdir(expected)

    def test_acquire_lock(self, storage):
        with storage.acquire_lock("r"):
            col = list(storage.discover("/user/work/", depth="0"))[0]
            items = list(col.get_all())
            assert len(items) == 3

    def test_move_item(self, storage, populated_dir):
        # Create a second collection
        col2, _, _ = storage.create_collection(
            "/user/personal/",
            props={"tag": "VCALENDAR"})

        col1 = list(storage.discover("/user/work/", depth="0"))[0]
        items = dict(col1.get_multi(["lunch-001.ics"]))
        item = items["lunch-001.ics"]
        assert item is not None

        storage.move(item, col2, "lunch-001.ics")

        # Should be gone from col1
        results = dict(col1.get_multi(["lunch-001.ics"]))
        assert results["lunch-001.ics"] is None

        # Should be findable in col2
        col2_items = list(col2.get_all())
        uids = {i.uid for i in col2_items}
        assert "lunch-001" in uids
