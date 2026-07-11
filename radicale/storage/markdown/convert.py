# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Public API surface for the legacy single-converter module name.

The iCalendar converter now lives in :mod:`convert_ical`. This module
re-exports the symbols that the old monolithic ``convert`` exposed so any
direct importers (tests, embedders) continue to work without modification.
"""

from radicale.storage.markdown.convert_ical import (  # noqa: F401
    _apply_ical_extra,
    _build_valarm,
    _extract_tzid,
    _human_to_valarm,
    _parse_dt,
    _parse_time,
    _parse_trigger_duration,
    _timedelta_to_human,
    _valarm_to_human,
    event_path_components,
    md_filename_for_event,
    md_to_vcalendar,
    slugify,
    vcalendar_to_md,
)
from radicale.storage.markdown.frontmatter import (  # noqa: F401
    parse_md_file,
    render_md_file,
)
