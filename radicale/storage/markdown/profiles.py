# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend — profile registration
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Concrete :class:`CollectionProfile` instances and their registration.

Importing this module has the side effect of populating
:class:`ProfileRegistry`. The storage backend's ``__init__.py`` imports it
eagerly so the registry is always populated before any
:class:`Collection` is constructed.
"""

import datetime
from typing import Any, Dict, Optional

from radicale.storage.markdown.convert_ical import ICalConverter, _parse_dt
from radicale.storage.markdown.convert_vcard import VCardConverter
from radicale.storage.markdown.layout_addressbook import AddressbookLayout
from radicale.storage.markdown.layout_calendar import CalendarLayout
from radicale.storage.markdown.profile import (
    CollectionProfile, ProfileRegistry)


class CalendarProfile(CollectionProfile):
    tag = "VCALENDAR"
    item_extension = ".ics"
    metadata_filename = ".calendar.yaml"
    default_displayname = "Calendar"

    def __init__(self, default_timezone: str = "") -> None:
        self.converter = ICalConverter(default_timezone=default_timezone)
        self.layout = CalendarLayout()

    def merge_on_upload(self, old_fm: Optional[Dict[str, Any]],
                        new_fm: Dict[str, Any]) -> Dict[str, Any]:
        """Merge recurring VTODO completions across writes.

        Two sources of completions are reconciled:

        1. RECURRENCE-ID overrides on the incoming item (already pulled
           into ``new_fm['completions']`` by the converter).
        2. The old file's ``completions`` list — preserved so once-marked
           tasks stay marked even after a client roundtrips the master.

        Additionally, if ``DTSTART`` advanced and the item is a recurring
        VTODO, the old start date is recorded as a completion.
        """
        if not old_fm:
            return new_fm

        if ("rrule" in new_fm and new_fm.get("component") == "vtodo"
                and "start" in new_fm and "start" in old_fm):
            try:
                old_start, _ = _parse_dt(old_fm["start"])
                new_start, _ = _parse_dt(new_fm["start"])
                old_d = (old_start.date()
                         if isinstance(old_start, datetime.datetime)
                         else old_start)
                new_d = (new_start.date()
                         if isinstance(new_start, datetime.datetime)
                         else new_start)
                if new_d > old_d:
                    completions = list(new_fm.get("completions", []))
                    if isinstance(old_start, datetime.datetime):
                        completions.append(
                            old_start.strftime("%Y-%m-%dT%H:%M:%S"))
                    else:
                        completions.append(old_d.isoformat())
                    new_fm["completions"] = completions
            except (ValueError, AttributeError):
                pass

        if old_fm.get("completions"):
            old_c = set(str(c) for c in old_fm["completions"])
            new_c = set(str(c) for c in new_fm.get("completions", []))
            merged = sorted(old_c | new_c)
            if merged:
                new_fm["completions"] = merged

        return new_fm


class AddressbookProfile(CollectionProfile):
    tag = "VADDRESSBOOK"
    item_extension = ".vcf"
    metadata_filename = ".addressbook.yaml"
    default_displayname = "Address Book"

    def __init__(self) -> None:
        self.converter = VCardConverter()
        self.layout = AddressbookLayout()


def register_default_profiles(default_timezone: str = "") -> None:
    """(Re)register the built-in profiles. Idempotent."""
    ProfileRegistry.register(
        CalendarProfile(default_timezone=default_timezone))
    ProfileRegistry.register(AddressbookProfile())
    ProfileRegistry.set_default("VCALENDAR")


# Eager registration at import time.
register_default_profiles()
