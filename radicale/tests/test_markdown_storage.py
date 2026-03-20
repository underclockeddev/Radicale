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

    def test_floating_time_gets_default_timezone(self):
        """Floating-time event gets default_timezone assigned."""
        cal = vobject.readOne(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ft1\r\n"
            "SUMMARY:Float\r\nDTSTART:20260320T150000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR")
        fm, body = vcalendar_to_md(cal, default_timezone="America/New_York")
        assert fm["timezone"] == "America/New_York"
        # The start time value should be preserved (not shifted)
        assert fm["start"] == "2026-03-20T15:00:00"

    def test_floating_time_no_default_stays_floating(self):
        """Without default_timezone, floating time stays as-is."""
        cal = vobject.readOne(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ft2\r\n"
            "SUMMARY:Float\r\nDTSTART:20260320T150000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR")
        fm, body = vcalendar_to_md(cal, default_timezone="")
        assert "timezone" not in fm

    def test_explicit_tz_not_overridden_by_default(self):
        """Events with explicit TZID keep their timezone."""
        cal = vobject.readOne(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ft3\r\n"
            "SUMMARY:Explicit\r\nDTSTART;TZID=Europe/London:20260320T150000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR")
        fm, body = vcalendar_to_md(cal, default_timezone="America/New_York")
        assert fm["timezone"] == "Europe/London"


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

    def test_update_recurring_event_title(self, storage, populated_dir):
        """Updating a recurring event's title should rename the file, not delete it."""
        col = list(storage.discover("/user/work/", depth="0"))[0]

        # Create a recurring event
        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "standup-001"
        ev.add("summary").value = "Daily Standup"
        ev.add("dtstart").value = datetime.datetime(2026, 3, 30, 9, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 3, 30, 9, 30, 0)
        ev.add("rrule").value = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

        item = Item(collection=col, vobject_item=cal, href="standup-001.ics")
        col.upload("standup-001.ics", item)

        recurring_dir = os.path.join(
            populated_dir, "collection-root", "user", "work", "recurring")
        assert os.path.isfile(os.path.join(recurring_dir, "daily-standup.md"))

        # Clear index to simulate server restart
        storage._uid_index = {}

        # Update the title
        cal2 = vobject.iCalendar()
        ev2 = cal2.add("vevent")
        ev2.add("uid").value = "standup-001"
        ev2.add("summary").value = "Morning Standup"
        ev2.add("dtstart").value = datetime.datetime(2026, 3, 30, 9, 0, 0)
        ev2.add("dtend").value = datetime.datetime(2026, 3, 30, 9, 30, 0)
        ev2.add("rrule").value = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

        col = list(storage.discover("/user/work/", depth="0"))[0]
        item2 = Item(collection=col, vobject_item=cal2, href="standup-001.ics")
        uploaded, old = col.upload("standup-001.ics", item2)

        assert uploaded.uid == "standup-001"
        assert old is not None
        # Old file should be gone, new file should exist
        assert not os.path.isfile(os.path.join(recurring_dir, "daily-standup.md"))
        assert os.path.isfile(os.path.join(recurring_dir, "morning-standup.md"))

    def test_recurring_to_onetime(self, storage, populated_dir):
        """Converting a recurring event to one-time should move it from recurring/ to YYYY/MM/."""
        col = list(storage.discover("/user/work/", depth="0"))[0]

        # Create recurring event
        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "convert-001"
        ev.add("summary").value = "Weekly Review"
        ev.add("dtstart").value = datetime.datetime(2026, 4, 10, 14, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 4, 10, 15, 0, 0)
        ev.add("rrule").value = "FREQ=WEEKLY"

        item = Item(collection=col, vobject_item=cal, href="convert-001.ics")
        col.upload("convert-001.ics", item)

        recurring_dir = os.path.join(
            populated_dir, "collection-root", "user", "work", "recurring")
        assert os.path.isdir(recurring_dir)

        storage._uid_index = {}

        # Update: remove rrule
        cal2 = vobject.iCalendar()
        ev2 = cal2.add("vevent")
        ev2.add("uid").value = "convert-001"
        ev2.add("summary").value = "Weekly Review"
        ev2.add("dtstart").value = datetime.datetime(2026, 4, 10, 14, 0, 0)
        ev2.add("dtend").value = datetime.datetime(2026, 4, 10, 15, 0, 0)

        col = list(storage.discover("/user/work/", depth="0"))[0]
        item2 = Item(collection=col, vobject_item=cal2, href="convert-001.ics")
        uploaded, old = col.upload("convert-001.ics", item2)

        assert uploaded.uid == "convert-001"
        # Should be in dated folder now
        dated_path = os.path.join(
            populated_dir, "collection-root", "user", "work",
            "2026", "04", "10_weekly-review.md")
        assert os.path.isfile(dated_path)
        # recurring dir should be cleaned up
        assert not os.path.isfile(os.path.join(recurring_dir, "weekly-review.md"))

    def test_onetime_to_recurring(self, storage, populated_dir):
        """Adding rrule to a one-time event should move it to recurring/."""
        col = list(storage.discover("/user/work/", depth="0"))[0]

        # Create one-time event
        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "evolve-001"
        ev.add("summary").value = "Team Meeting"
        ev.add("dtstart").value = datetime.datetime(2026, 5, 1, 10, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 5, 1, 11, 0, 0)

        item = Item(collection=col, vobject_item=cal, href="evolve-001.ics")
        col.upload("evolve-001.ics", item)

        dated_path = os.path.join(
            populated_dir, "collection-root", "user", "work",
            "2026", "05", "01_team-meeting.md")
        assert os.path.isfile(dated_path)

        storage._uid_index = {}

        # Update: add rrule
        cal2 = vobject.iCalendar()
        ev2 = cal2.add("vevent")
        ev2.add("uid").value = "evolve-001"
        ev2.add("summary").value = "Team Meeting"
        ev2.add("dtstart").value = datetime.datetime(2026, 5, 1, 10, 0, 0)
        ev2.add("dtend").value = datetime.datetime(2026, 5, 1, 11, 0, 0)
        ev2.add("rrule").value = "FREQ=WEEKLY"

        col = list(storage.discover("/user/work/", depth="0"))[0]
        item2 = Item(collection=col, vobject_item=cal2, href="evolve-001.ics")
        uploaded, old = col.upload("evolve-001.ics", item2)

        recurring_dir = os.path.join(
            populated_dir, "collection-root", "user", "work", "recurring")
        assert os.path.isfile(os.path.join(recurring_dir, "team-meeting.md"))
        assert not os.path.isfile(dated_path)

    def test_timezone_roundtrip(self, storage, populated_dir):
        """Events with timezones should round-trip correctly."""
        col = list(storage.discover("/user/work/", depth="0"))[0]

        ical_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:STANDARD
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:tz-test-001
DTSTART;TZID=America/New_York:20260420T140000
DTEND;TZID=America/New_York:20260420T150000
SUMMARY:Timezone Event
END:VEVENT
END:VCALENDAR"""

        item = Item(collection=col, text=ical_text)
        uploaded, _ = col.upload("tz-test-001.ics", item)
        assert uploaded.uid == "tz-test-001"

        # Read back and verify timezone is preserved
        storage._uid_index = {}
        col = list(storage.discover("/user/work/", depth="0"))[0]
        results = dict(col.get_multi(["tz-test-001.ics"]))
        readback = results["tz-test-001.ics"]
        assert readback is not None
        assert "TZID=America/New_York" in readback.serialize() or "America/New_York" in readback.serialize()

    def test_utc_timezone_roundtrip(self, storage, populated_dir):
        """UTC events (Z suffix) must round-trip without gaining/losing hours."""
        col = list(storage.discover("/user/work/", depth="0"))[0]

        ical_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:utc-test-001
DTSTART:20260320T200000Z
DTEND:20260320T210000Z
SUMMARY:UTC Event
END:VEVENT
END:VCALENDAR"""

        item = Item(collection=col, text=ical_text)
        uploaded, _ = col.upload("utc-test-001.ics", item)
        assert uploaded.uid == "utc-test-001"

        # Read back and verify UTC Z suffix is preserved
        storage._uid_index = {}
        col = list(storage.discover("/user/work/", depth="0"))[0]
        results = dict(col.get_multi(["utc-test-001.ics"]))
        readback = results["utc-test-001.ics"]
        assert readback is not None
        serialized = readback.serialize()
        assert "T200000Z" in serialized, f"Expected Z suffix, got: {serialized}"
        assert "T210000Z" in serialized

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

    def test_sync_token_full_sync(self, storage, populated_dir):
        """sync('') returns all hrefs + a token."""
        col = list(storage.discover("/user/work/", depth="0"))[0]
        token, hrefs = col.sync("")
        hrefs = list(hrefs)
        assert token.startswith("http://radicale.org/ns/sync/")
        assert len(hrefs) == 3  # lunch, dentist, weekly

    def test_sync_token_no_change(self, storage, populated_dir):
        """sync(current_token) returns empty hrefs (nothing changed)."""
        col = list(storage.discover("/user/work/", depth="0"))[0]
        token, _ = col.sync("")
        token2, hrefs = col.sync(token)
        hrefs = list(hrefs)
        assert token2 == token
        assert len(hrefs) == 0

    def test_sync_token_stale(self, storage, populated_dir):
        """sync(stale_token) returns all hrefs (full resync)."""
        col = list(storage.discover("/user/work/", depth="0"))[0]
        stale = "http://radicale.org/ns/sync/deadbeef"
        token, hrefs = col.sync(stale)
        hrefs = list(hrefs)
        assert token.startswith("http://radicale.org/ns/sync/")
        assert len(hrefs) == 3


# ── Flat mode tests ─────────────────────────────────────────────────────────

@pytest.fixture
def flat_dir(tmpdir):
    """Create a flat calendar directory (no user/collection nesting)."""
    cal = tmpdir  # the dir IS the calendar
    os.makedirs(os.path.join(cal, "2026", "03"), exist_ok=True)
    os.makedirs(os.path.join(cal, "recurring"), exist_ok=True)

    with open(os.path.join(cal, ".calendar.yaml"), "w") as f:
        yaml.dump({"tag": "VCALENDAR", "D:displayname": "My Calendar"}, f)

    with open(os.path.join(cal, "2026", "03", "20_meeting.md"), "w") as f:
        f.write('---\nuid: flat-001\ntitle: Meeting\n'
                'start: "2026-03-20T10:00:00"\nend: "2026-03-20T11:00:00"\n'
                '---\n')
    return tmpdir


@pytest.fixture
def flat_storage(flat_dir):
    cfg = load()
    cfg.update(
        {"storage": {"type": "markdown",
                      "filesystem_folder": flat_dir,
                      "flat_calendar_path": flat_dir}},
        "test",
    )
    return Storage(cfg)


class TestFlatMode:
    def test_discover_collection(self, flat_storage, flat_dir):
        """Discovering 'user/cal/' yields a collection with items."""
        results = list(flat_storage.discover("/grape/default/", depth="0"))
        assert len(results) == 1
        col = results[0]
        assert col.tag == "VCALENDAR"

    def test_discover_items(self, flat_storage, flat_dir):
        """Discovering at depth 1 yields items."""
        results = list(flat_storage.discover("/grape/default/", depth="1"))
        assert len(results) >= 2  # collection + at least 1 item
        items = [r for r in results if getattr(r, 'href', None)]
        assert len(items) == 1
        assert items[0].uid == "flat-001"

    def test_discover_user_principal(self, flat_storage, flat_dir):
        """Discovering 'user/' yields a collection and its calendar child."""
        results = list(flat_storage.discover("/grape/", depth="1"))
        assert len(results) >= 1  # at least the principal itself

    def test_upload_and_read(self, flat_storage, flat_dir):
        """Upload an event and read it back in flat mode."""
        col = list(flat_storage.discover("/grape/default/", depth="0"))[0]
        cal = vobject.iCalendar()
        ev = cal.add("vevent")
        ev.add("uid").value = "flat-new-001"
        ev.add("summary").value = "Flat Test"
        ev.add("dtstart").value = datetime.datetime(2026, 4, 15, 9, 0, 0)
        ev.add("dtend").value = datetime.datetime(2026, 4, 15, 10, 0, 0)

        item = Item(collection=col, vobject_item=cal, href="flat-new-001.ics")
        uploaded, old = col.upload("flat-new-001.ics", item)
        assert uploaded.uid == "flat-new-001"
        assert old is None

        # File should be directly in flat_dir/2026/04/
        expected = os.path.join(flat_dir, "2026", "04", "15_flat-test.md")
        assert os.path.isfile(expected)

        # Read back
        flat_storage._uid_index = {}
        col = list(flat_storage.discover("/grape/default/", depth="0"))[0]
        results = dict(col.get_multi(["flat-new-001.ics"]))
        assert results["flat-new-001.ics"] is not None

    def test_create_collection_flat(self, flat_storage, flat_dir):
        """create_collection in flat mode uses the flat dir."""
        col, _, _ = flat_storage.create_collection(
            "/grape/mycal/",
            props={"tag": "VCALENDAR", "D:displayname": "Test"})
        assert col.get_meta("tag") == "VCALENDAR"
        # .calendar.yaml should be in flat_dir (overwritten)
        meta_path = os.path.join(flat_dir, ".calendar.yaml")
        assert os.path.isfile(meta_path)

    def test_discover_specific_item(self, flat_storage, flat_dir):
        """Discovering a specific item href works in flat mode."""
        results = list(flat_storage.discover(
            "/grape/default/flat-001.ics", depth="0"))
        assert len(results) == 1
        assert results[0].uid == "flat-001"
