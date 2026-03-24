# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Conversion between Markdown (YAML frontmatter) and iCalendar formats.

Handles bidirectional conversion:
  - MD file → vobject VCALENDAR component (for CalDAV serving)
  - vobject VCALENDAR component → MD file content (for CalDAV writes)
"""

import datetime
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import vobject
import yaml


# ── Frontmatter ↔ iCalendar field mapping ──────────────────────────────────

# Maps frontmatter keys to iCalendar property names
_FM_TO_ICAL = {
    "title": "summary",
    "location": "location",
    "description": "description",
    "status": "status",
    "url": "url",
    "geo": "geo",
    "priority": "priority",
    "sequence": "sequence",
    "transparency": "transp",
}

# Reverse mapping for iCalendar → frontmatter
_ICAL_TO_FM = {v.upper(): k for k, v in _FM_TO_ICAL.items()}
# Add extras that don't round-trip through the simple map
_ICAL_TO_FM["SUMMARY"] = "title"
_ICAL_TO_FM["TRANSP"] = "transparency"

# iCalendar properties we handle explicitly (not via the simple map)
_HANDLED_ICAL_PROPS = {
    "uid", "summary", "dtstart", "dtend", "duration",
    "location", "description", "status", "categories",
    "rrule", "exdate", "rdate", "transp", "url", "geo",
    "priority", "sequence", "dtstamp", "created", "last-modified",
    "due", "percent-complete",
    # Structural / always-present
    "prodid", "version", "calscale", "method",
}

_STATUS_MAP = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
}

_STATUS_MAP_REV = {v: k for k, v in _STATUS_MAP.items()}


# ── Parsing helpers ─────────────────────────────────────────────────────────

def parse_md_file(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse a markdown file into (frontmatter_dict, body_text).

    Expects YAML frontmatter between ``---`` fences.
    """
    text = text.strip()
    if not text.startswith("---"):
        return {}, text

    # Find closing fence
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    yaml_str = text[3:end].strip()
    body = text[end + 4:].strip()

    try:
        fm = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        fm = {}

    if not isinstance(fm, dict):
        fm = {}

    return fm, body


def render_md_file(fm: Dict[str, Any], body: str = "") -> str:
    """Render a frontmatter dict and body into a markdown file string."""
    # Use block style for readability, allow unicode
    yaml_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True,
                         sort_keys=False)
    parts = ["---", yaml_str.rstrip(), "---"]
    if body:
        parts.append("")
        parts.append(body)
    parts.append("")  # trailing newline
    return "\n".join(parts)


# ── MD → iCalendar ─────────────────────────────────────────────────────────

def _parse_dt(value: Any, tz_name: Optional[str] = None
              ) -> Tuple[Any, bool]:
    """Parse a date/datetime from frontmatter value.

    Returns (datetime_or_date, is_date_only).
    """
    if isinstance(value, datetime.datetime):
        if tz_name and value.tzinfo is None:
            import pytz
            tz = pytz.timezone(tz_name)
            value = tz.localize(value)
        return value, False
    if isinstance(value, datetime.date):
        return value, True

    s = str(value)
    # Try datetime first
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if tz_name and dt.tzinfo is None:
                import pytz
                tz = pytz.timezone(tz_name)
                dt = tz.localize(dt)
            return dt, False
        except ValueError:
            continue
    # Try date
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date(), True
    except ValueError:
        pass

    raise ValueError("Cannot parse date/datetime: %r" % value)


def _parse_time(value: str) -> datetime.time:
    """Parse a time string like '09:00' or '14:30'."""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    raise ValueError("Cannot parse time: %r" % value)


# ── Reminder helpers ───────────────────────────────────────────────────────

# Maps human-readable units to ISO 8601 duration components
_DURATION_UNITS = [
    ("w", "W", 7 * 24 * 3600),
    ("d", "D", 24 * 3600),
    ("h", "H", 3600),
    ("m", "M", 60),
]


def _parse_trigger_duration(trigger_str: str) -> Optional[datetime.timedelta]:
    """Parse an iCal TRIGGER duration like '-P1D', '-PT15M', 'PT1H'."""
    s = trigger_str.strip()
    negative = s.startswith("-")
    s = s.lstrip("+-")
    if not s.startswith("P"):
        return None
    s = s[1:]  # strip P

    weeks = days = hours = minutes = seconds = 0
    # Date part (before T)
    time_part = ""
    if "T" in s:
        date_part, time_part = s.split("T", 1)
    else:
        date_part = s

    for val, unit in re.findall(r"(\d+)([WDHMS])", date_part):
        n = int(val)
        if unit == "W":
            weeks = n
        elif unit == "D":
            days = n

    for val, unit in re.findall(r"(\d+)([HMS])", time_part):
        n = int(val)
        if unit == "H":
            hours = n
        elif unit == "M":
            minutes = n
        elif unit == "S":
            seconds = n

    td = datetime.timedelta(weeks=weeks, days=days, hours=hours,
                            minutes=minutes, seconds=seconds)
    return -td if negative else td


def _timedelta_to_human(td: datetime.timedelta, related: str) -> str:
    """Convert a timedelta + RELATED param to human-readable reminder string.

    related is 'START' or 'END'.
    """
    total = int(abs(td.total_seconds()))
    before = td.total_seconds() <= 0
    anchor = related.lower()  # "start" or "end"

    if total == 0:
        return "at %s" % anchor

    parts = []
    weeks = total // (7 * 24 * 3600)
    remainder = total % (7 * 24 * 3600)
    days = remainder // (24 * 3600)
    remainder %= 24 * 3600
    hours = remainder // 3600
    remainder %= 3600
    minutes = remainder // 60

    if weeks:
        parts.append("%dw" % weeks)
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)

    if not parts:
        parts.append("0m")

    time_str = " ".join(parts)
    direction = "before" if before else "after"
    return "%s %s %s" % (time_str, direction, anchor)


def _human_to_valarm(reminder: str) -> Optional[vobject.base.Component]:
    """Parse a human-readable reminder string into a VALARM component.

    Formats: '15m before start', '1d before end', '1w 2d after start',
             'at start', 'at end'
    """
    s = reminder.strip().lower()

    # "at start" / "at end"
    m = re.match(r"at\s+(start|end)$", s)
    if m:
        related = m.group(1).upper()
        trigger = "PT0S"
        return _build_valarm(trigger, related)

    # "Xd Yh before/after start/end"
    m = re.match(
        r"([\d\s]*\d\s*[wdhm](?:\s+\d+\s*[wdhm])*)\s+(before|after)\s+(start|end)$",
        s)
    if not m:
        return None

    time_str = m.group(1)
    direction = m.group(2)
    anchor = m.group(3).upper()

    weeks = days = hours = minutes = 0
    for val, unit in re.findall(r"(\d+)\s*([wdhm])", time_str):
        n = int(val)
        if unit == "w":
            weeks = n
        elif unit == "d":
            days = n
        elif unit == "h":
            hours = n
        elif unit == "m":
            minutes = n

    # Build ISO 8601 duration
    date_parts = ""
    if weeks:
        date_parts += "%dW" % weeks
    if days:
        date_parts += "%dD" % days
    time_parts = ""
    if hours:
        time_parts += "%dH" % hours
    if minutes:
        time_parts += "%dM" % minutes

    if time_parts:
        duration = "P%sT%s" % (date_parts, time_parts)
    elif date_parts:
        duration = "P%s" % date_parts
    else:
        duration = "PT0S"

    sign = "-" if direction == "before" else ""
    trigger = sign + duration

    return _build_valarm(trigger, anchor)


def _build_valarm(trigger: str, related: str) -> vobject.base.Component:
    """Build a VALARM vobject component."""
    lines = ["BEGIN:VALARM", "ACTION:DISPLAY",
             "DESCRIPTION:Reminder"]
    if related == "END":
        lines.append("TRIGGER;RELATED=END:%s" % trigger)
    else:
        lines.append("TRIGGER:%s" % trigger)
    lines.append("END:VALARM")
    alarm_text = "\r\n".join(lines)
    wrapper = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
               + alarm_text + "\r\nEND:VEVENT\r\nEND:VCALENDAR")
    parsed = vobject.readOne(wrapper)
    for child in parsed.vevent.components():
        if child.name == "VALARM":
            return child
    raise ValueError("Failed to parse VALARM")


def _valarm_to_human(alarm: vobject.base.Component) -> Optional[str]:
    """Convert a VALARM component to a human-readable reminder string."""
    if not hasattr(alarm, "trigger"):
        return None
    trigger = alarm.trigger
    trigger_val = trigger.value
    # trigger_val can be a timedelta or a datetime
    if isinstance(trigger_val, datetime.timedelta):
        td = trigger_val
    elif isinstance(trigger_val, datetime.datetime):
        # Absolute trigger — can't represent as relative
        return None
    else:
        # Try parsing as string
        td = _parse_trigger_duration(str(trigger_val))
        if td is None:
            return None

    # Check RELATED param (default is START)
    params = getattr(trigger, "params", {})
    related_list = params.get("RELATED", ["START"])
    related = related_list[0] if isinstance(related_list, list) else str(related_list)
    related = related.upper() if related else "START"

    return _timedelta_to_human(td, related)


def md_to_vcalendar(fm: Dict[str, Any], body: str = "") -> vobject.base.Component:
    """Convert frontmatter + body into a VCALENDAR vobject component."""
    cal = vobject.iCalendar()

    comp_type = fm.get("component", "vevent")
    vevent = cal.add(comp_type)

    # UID
    uid = fm.get("uid") or str(uuid.uuid4())
    vevent.add("uid").value = str(uid)

    tz_name = fm.get("timezone")
    # Sanitize timezone in case it was stored as a repr string
    if tz_name and not isinstance(tz_name, str):
        tz_name = str(tz_name)
    if tz_name:
        m = re.match(r".*<.*'(.+)'.*>", tz_name)
        if m:
            tz_name = m.group(1)

    # DTSTART / DTEND
    is_recurring_time_only = "start_time" in fm
    if is_recurring_time_only:
        # Recurring event with time-only fields
        base_date_val = fm.get("dtstart")
        if base_date_val:
            base_date, _ = _parse_dt(base_date_val)
            if isinstance(base_date, datetime.datetime):
                base_date = base_date.date()
        else:
            base_date = datetime.date.today()

        start_time = _parse_time(fm["start_time"])
        start_dt = datetime.datetime.combine(base_date, start_time)
        if tz_name:
            import pytz
            tz = pytz.timezone(tz_name)
            start_dt = tz.localize(start_dt)
        vevent.add("dtstart").value = start_dt

        if "end_time" in fm:
            end_time = _parse_time(fm["end_time"])
            end_dt = datetime.datetime.combine(base_date, end_time)
            if tz_name:
                import pytz
                tz = pytz.timezone(tz_name)
                end_dt = tz.localize(end_dt)
            vevent.add("dtend").value = end_dt
    elif "start" in fm:
        start_val, is_date = _parse_dt(fm["start"], tz_name)
        dtstart = vevent.add("dtstart")
        dtstart.value = start_val
        if is_date:
            dtstart.value_param = "DATE"

        if "end" in fm:
            end_val, end_is_date = _parse_dt(fm["end"], tz_name)
            dtend = vevent.add("dtend")
            dtend.value = end_val
            if end_is_date:
                dtend.value_param = "DATE"

    # All-day flag override
    if fm.get("all_day") and hasattr(vevent, "dtstart"):
        dt = vevent.dtstart.value
        if isinstance(dt, datetime.datetime):
            vevent.dtstart.value = dt.date()
            vevent.dtstart.value_param = "DATE"
        if hasattr(vevent, "dtend"):
            dt = vevent.dtend.value
            if isinstance(dt, datetime.datetime):
                vevent.dtend.value = dt.date()
                vevent.dtend.value_param = "DATE"

    # DUE (VTODO-specific)
    if "due" in fm:
        due_val, due_is_date = _parse_dt(fm["due"], tz_name)
        due_prop = vevent.add("due")
        due_prop.value = due_val
        if due_is_date:
            due_prop.value_param = "DATE"
        elif tz_name:
            due_prop.params["TZID"] = [tz_name]

    # PERCENT-COMPLETE (VTODO-specific)
    if "percent_complete" in fm:
        vevent.add("percent-complete").value = str(fm["percent_complete"])

    # Simple string properties
    for fm_key, ical_attr in _FM_TO_ICAL.items():
        if fm_key in fm and fm_key not in ("status",):
            val = fm[fm_key]
            if val is not None:
                vevent.add(ical_attr).value = str(val)

    # Status (mapped)
    if "status" in fm:
        status = _STATUS_MAP.get(str(fm["status"]).lower(),
                                  str(fm["status"]).upper())
        vevent.add("status").value = status

    # Categories
    cats = fm.get("categories")
    if cats:
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",")]
        cat_prop = vevent.add("categories")
        cat_prop.value = list(cats)

    # RRULE
    if "rrule" in fm:
        rrule_str = str(fm["rrule"])
        # vobject expects the RRULE value without the "RRULE:" prefix
        if rrule_str.upper().startswith("RRULE:"):
            rrule_str = rrule_str[6:]
        rrule_prop = vevent.add("rrule")
        rrule_prop.value = rrule_str

    # EXDATE
    exdates = fm.get("exdate")
    if exdates:
        if not isinstance(exdates, list):
            exdates = [exdates]
        for exd in exdates:
            dt_val, _ = _parse_dt(exd)
            if isinstance(dt_val, datetime.datetime):
                dt_val = dt_val.date()
            exdate = vevent.add("exdate")
            exdate.value = [dt_val]

    # Description from body (fallback)
    if "description" not in fm and body:
        vevent.add("description").value = body

    # Timestamp properties — set explicitly for ETag stability
    from vobject.icalendar import utc as vobj_utc
    _TS_FIELDS = {
        "dtstamp": "DTSTAMP",
        "created": "CREATED",
        "last_modified": "LAST-MODIFIED",
    }
    for fm_key, ical_name in _TS_FIELDS.items():
        ts_val = fm.get(fm_key)
        if ts_val:
            try:
                dt = datetime.datetime.strptime(str(ts_val), "%Y%m%dT%H%M%SZ")
                dt = dt.replace(tzinfo=vobj_utc)
                vevent.add(ical_name).value = dt
            except (ValueError, AttributeError):
                pass

    # Reminders → VALARM sub-components
    reminders = fm.get("reminders")
    if reminders:
        if isinstance(reminders, str):
            reminders = [r.strip() for r in reminders.split(",")]
        for reminder in reminders:
            try:
                alarm = _human_to_valarm(str(reminder))
                if alarm:
                    vevent.add(alarm)
            except Exception:
                pass

    # ical_extra: inject preserved iCalendar properties
    ical_extra = fm.get("ical_extra", {})
    if isinstance(ical_extra, dict):
        _apply_ical_extra(vevent, ical_extra)

    # Reconstruct RECURRENCE-ID overrides from completions list
    completions = fm.get("completions")
    if completions and comp_type == "vtodo":
        tz_name = fm.get("timezone")
        for completion in completions:
            try:
                rid_val, is_date = _parse_dt(completion, tz_name)
            except (ValueError, AttributeError):
                continue
            override = vobject.newFromBehavior("vtodo")
            override.add("uid").value = fm.get("uid", "")
            override.add("summary").value = fm.get("title", "")

            # RECURRENCE-ID
            rid_prop = override.add("recurrence-id")
            rid_prop.value = rid_val
            if tz_name and not is_date:
                rid_prop.params["TZID"] = [tz_name]

            # DTSTART (same as RECURRENCE-ID)
            dtstart = override.add("dtstart")
            dtstart.value = rid_val
            if is_date:
                dtstart.value_param = "DATE"
            elif tz_name:
                dtstart.params["TZID"] = [tz_name]

            # STATUS + COMPLETED
            override.add("status").value = "COMPLETED"
            override.add("percent-complete").value = "100"
            if isinstance(rid_val, datetime.datetime):
                from vobject.icalendar import utc as vobj_utc
                completed_dt = rid_val.astimezone(vobj_utc) if rid_val.tzinfo else rid_val.replace(tzinfo=vobj_utc)
                override.add("completed").value = completed_dt

            # DTSTAMP (required)
            ts_val = fm.get("dtstamp")
            if ts_val:
                try:
                    from vobject.icalendar import utc as vobj_utc
                    dt = datetime.datetime.strptime(str(ts_val), "%Y%m%dT%H%M%SZ")
                    override.add("dtstamp").value = dt.replace(tzinfo=vobj_utc)
                except (ValueError, AttributeError):
                    pass

            cal.add(override)

    return cal


def _apply_ical_extra(component: vobject.base.Component,
                      extra: Dict[str, Any]) -> None:
    """Apply ical_extra dict entries as raw iCalendar properties."""
    for prop_name, prop_value in extra.items():
        name_upper = prop_name.upper()

        if name_upper == "VALARM":
            # VALARM is a sub-component, parse it
            alarms = prop_value if isinstance(prop_value, list) else [prop_value]
            for alarm_text in alarms:
                alarm_text = str(alarm_text).strip()
                if not alarm_text.startswith("BEGIN:VALARM"):
                    alarm_text = "BEGIN:VALARM\n" + alarm_text
                if not alarm_text.endswith("END:VALARM"):
                    alarm_text = alarm_text + "\nEND:VALARM"
                # Wrap in VCALENDAR/VEVENT to parse
                wrapper = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
                           + alarm_text.replace("\n", "\r\n")
                           + "\r\nEND:VEVENT\r\nEND:VCALENDAR")
                try:
                    parsed = vobject.readOne(wrapper)
                    for child_alarm in parsed.vevent.components():
                        if child_alarm.name == "VALARM":
                            component.add(child_alarm)
                except Exception:
                    pass
            continue

        # Simple property or list of properties
        values = prop_value if isinstance(prop_value, list) else [prop_value]
        for val in values:
            try:
                prop = component.add(name_upper)
                val_str = str(val)
                # Properties that vobject expects as datetime objects
                _DATETIME_PROPS = {
                    "DTSTAMP", "CREATED", "LAST-MODIFIED",
                    "DTSTART", "DTEND", "DUE", "COMPLETED",
                }
                if name_upper in _DATETIME_PROPS:
                    try:
                        if val_str.endswith("Z") and len(val_str) == 16:
                            from vobject.icalendar import utc as vobj_utc
                            dt = datetime.datetime.strptime(val_str, "%Y%m%dT%H%M%SZ")
                            dt = dt.replace(tzinfo=vobj_utc)
                            prop.value = dt
                            continue
                    except (ValueError, AttributeError):
                        pass
                prop.value = val_str
            except Exception:
                pass


# ── iCalendar → MD ─────────────────────────────────────────────────────────

def vcalendar_to_md(cal: vobject.base.Component,
                    default_timezone: str = ""
                    ) -> Tuple[Dict[str, Any], str]:
    """Convert a VCALENDAR vobject component to (frontmatter_dict, body).

    Extracts the main VEVENT/VTODO/VJOURNAL (the one without RECURRENCE-ID)
    and maps its properties to frontmatter fields. Unrecognized properties
    go into ical_extra.

    RECURRENCE-ID overrides with STATUS:COMPLETED are stored as entries
    in a ``completions`` frontmatter list.

    If *default_timezone* is set and the event has floating-time DTSTART
    (no TZID, no Z suffix), the default timezone is assigned.
    """
    # Separate main component from RECURRENCE-ID overrides
    main_comp = None
    overrides: list = []
    for comp in cal.components():
        if comp.name not in ("VEVENT", "VTODO", "VJOURNAL"):
            continue
        if hasattr(comp, "recurrence_id") and comp.recurrence_id.value:
            overrides.append(comp)
        elif main_comp is None:
            main_comp = comp

    # Fallback: if no component without RECURRENCE-ID, use first override
    if main_comp is None and overrides:
        main_comp = overrides.pop(0)
    if main_comp is None:
        return {"uid": str(uuid.uuid4())}, ""

    vevent = main_comp

    fm: Dict[str, Any] = {}
    body = ""

    # Store component type so we recreate the right one
    fm["component"] = vevent.name.lower()  # vevent, vtodo, vjournal

    # UID
    if hasattr(vevent, "uid"):
        fm["uid"] = vevent.uid.value

    # SUMMARY → title
    if hasattr(vevent, "summary"):
        fm["title"] = vevent.summary.value

    # DTSTART / DTEND
    tz_name = None
    if hasattr(vevent, "dtstart"):
        dtstart = vevent.dtstart.value
        tz_name = _extract_tzid(vevent.dtstart)

        if isinstance(dtstart, datetime.datetime):
            fm["start"] = dtstart.strftime("%Y-%m-%dT%H:%M:%S")
        elif isinstance(dtstart, datetime.date):
            fm["start"] = dtstart.strftime("%Y-%m-%d")
            fm["all_day"] = True

    if hasattr(vevent, "dtend"):
        dtend = vevent.dtend.value
        if isinstance(dtend, datetime.datetime):
            fm["end"] = dtend.strftime("%Y-%m-%dT%H:%M:%S")
        elif isinstance(dtend, datetime.date):
            fm["end"] = dtend.strftime("%Y-%m-%d")

    if tz_name:
        fm["timezone"] = tz_name
    elif default_timezone and "start" in fm and not fm.get("all_day"):
        # Floating time (no TZID, no Z) — assign default timezone so
        # CalDAV clients always get an explicit TZID back.
        fm["timezone"] = default_timezone

    # DUE (VTODO-specific, similar to DTEND)
    if hasattr(vevent, "due"):
        due_val = vevent.due.value
        if isinstance(due_val, datetime.datetime):
            fm["due"] = due_val.strftime("%Y-%m-%dT%H:%M:%S")
            if not tz_name:
                due_tz = _extract_tzid(vevent.due)
                if due_tz:
                    fm["timezone"] = due_tz
        elif isinstance(due_val, datetime.date):
            fm["due"] = due_val.strftime("%Y-%m-%d")

    # PERCENT-COMPLETE (VTODO-specific)
    if "percent-complete" in vevent.contents:
        try:
            fm["percent_complete"] = int(
                vevent.contents["percent-complete"][0].value)
        except (ValueError, TypeError):
            fm["percent_complete"] = str(
                vevent.contents["percent-complete"][0].value)

    # Preserve timestamp properties for ETag stability
    for ts_prop in ("dtstamp", "created", "last_modified"):
        attr = ts_prop.replace("_", "-")
        if attr in vevent.contents:
            val = vevent.contents[attr][0].value
            if isinstance(val, datetime.datetime):
                fm[ts_prop] = val.strftime("%Y%m%dT%H%M%SZ")
            elif isinstance(val, str):
                fm[ts_prop] = val

    # Simple properties
    for ical_prop, fm_key in _ICAL_TO_FM.items():
        if fm_key in ("title",):  # already handled
            continue
        attr = ical_prop.lower()
        if hasattr(vevent, attr):
            val = getattr(vevent, attr).value
            if ical_prop == "STATUS":
                val = _STATUS_MAP_REV.get(str(val).upper(), str(val).lower())
            fm[fm_key] = str(val) if val is not None else None

    # Categories
    if hasattr(vevent, "categories"):
        cats = vevent.categories.value
        if cats:
            fm["categories"] = list(cats) if not isinstance(cats, list) else cats

    # RRULE
    if hasattr(vevent, "rrule"):
        fm["rrule"] = vevent.rrule.value

    # EXDATE
    if "exdate" in vevent.contents:
        exdates = []
        for exdate_prop in vevent.contents["exdate"]:
            for dt_val in exdate_prop.value:
                if isinstance(dt_val, datetime.datetime):
                    exdates.append(dt_val.strftime("%Y-%m-%d"))
                elif isinstance(dt_val, datetime.date):
                    exdates.append(dt_val.strftime("%Y-%m-%d"))
        if exdates:
            fm["exdate"] = exdates

    # Description → body
    if hasattr(vevent, "description"):
        desc = vevent.description.value
        if desc:
            body = desc

    # Collect unhandled properties into ical_extra
    ical_extra: Dict[str, Any] = {}
    for prop_name, prop_list in vevent.contents.items():
        if prop_name.lower() in _HANDLED_ICAL_PROPS:
            continue
        if prop_name.lower() in {k.lower() for k in _FM_TO_ICAL.values()}:
            continue
        # Skip sub-components (e.g. VALARM) — handled separately below
        if prop_list and hasattr(prop_list[0], 'components'):
            continue
        values = []
        for prop in prop_list:
            try:
                val = prop.value
                if isinstance(val, datetime.datetime):
                    values.append(val.strftime("%Y%m%dT%H%M%SZ"))
                elif isinstance(val, datetime.date):
                    values.append(val.strftime("%Y%m%d"))
                else:
                    values.append(str(val))
            except (AttributeError, Exception):
                # Fallback: serialize the raw line
                try:
                    values.append(prop.serialize().strip())
                except Exception:
                    pass
        if len(values) == 1:
            ical_extra[prop_name.upper()] = values[0]
        else:
            ical_extra[prop_name.upper()] = values

    # Collect VALARM sub-components as human-readable reminders
    reminders = []
    raw_alarms = []
    for child in vevent.components():
        if child.name == "VALARM":
            human = _valarm_to_human(child)
            if human:
                reminders.append(human)
            else:
                # Can't parse — preserve raw for lossless round-trip
                try:
                    raw_alarms.append(child.serialize().strip())
                except Exception:
                    pass
    if reminders:
        fm["reminders"] = reminders
    if raw_alarms:
        ical_extra["VALARM"] = raw_alarms if len(raw_alarms) > 1 else raw_alarms[0]

    if ical_extra:
        fm["ical_extra"] = ical_extra

    # Extract completions from RECURRENCE-ID overrides
    if overrides:
        completions = []
        for ov in overrides:
            status = getattr(ov, "status", None)
            is_completed = (status and status.value
                            and status.value.upper() == "COMPLETED")
            if is_completed and hasattr(ov, "recurrence_id"):
                rid = ov.recurrence_id.value
                if isinstance(rid, datetime.datetime):
                    completions.append(rid.strftime("%Y-%m-%dT%H:%M:%S"))
                elif isinstance(rid, datetime.date):
                    completions.append(rid.isoformat())
                else:
                    completions.append(str(rid))
        if completions:
            fm["completions"] = completions

    return fm, body


def _extract_tzid(dt_prop) -> Optional[str]:
    """Extract timezone name from a vobject datetime property."""
    # Check TZID parameter
    params = getattr(dt_prop, "params", {})
    tzid = params.get("TZID")
    if tzid:
        if isinstance(tzid, list):
            tzid = tzid[0]
        return str(tzid)
    # Check tzinfo
    val = dt_prop.value
    if isinstance(val, datetime.datetime) and val.tzinfo:
        # pytz timezones have a .zone attribute
        if hasattr(val.tzinfo, "zone"):
            return val.tzinfo.zone
        # zoneinfo/dateutil timezones have a .key attribute
        if hasattr(val.tzinfo, "key"):
            return val.tzinfo.key
        # vobject tzicalvtz: extract name from .tzid or repr
        if hasattr(val.tzinfo, "tzid"):
            return val.tzinfo.tzid
        tz_str = repr(val.tzinfo)
        m = re.match(r".*'(.+)'.*", tz_str)
        if m:
            return m.group(1)
        tz_name = str(val.tzinfo)
        if tz_name and tz_name != "tzutc()":
            return tz_name
        # Detect UTC: tzutc(), datetime.timezone.utc, pytz.UTC, etc.
        if val.utcoffset() == datetime.timedelta(0):
            return "UTC"
    return None


# ── Filename helpers ────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:60] or "event"


def md_filename_for_event(fm: Dict[str, Any]) -> str:
    """Generate a DD_slug.md filename from frontmatter."""
    title = fm.get("title", "event")
    slug = slugify(title)

    # Extract day from start date
    start = fm.get("start")
    if start:
        try:
            dt, _ = _parse_dt(start)
            if isinstance(dt, datetime.datetime):
                day = dt.day
            else:
                day = dt.day
            return "%02d_%s.md" % (day, slug)
        except (ValueError, AttributeError):
            pass

    return "%s.md" % slug


def event_path_components(fm: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """Return (year, month, filename) for a dated event, or None for recurring.

    Returns ``None`` when the item should go in ``recurring/`` (has rrule)
    or ``tasks/`` (undated VTODO — caller checks component type).
    """
    if "rrule" in fm:
        return None  # goes in recurring/

    start = fm.get("start")
    if not start:
        return None  # undated — caller decides recurring/ vs tasks/

    try:
        dt, _ = _parse_dt(start)
        return (str(dt.year), "%02d" % dt.month, md_filename_for_event(fm))
    except (ValueError, AttributeError):
        return None
