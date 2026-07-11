# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend — calendar layout
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Filesystem layout policy for ``VCALENDAR`` markdown collections.

Layout::

    {collection-root}/
        .calendar.yaml
        recurring/{slug}.md           # rrule-based items
        tasks/{slug}.md               # undated VTODOs
        YYYY/MM/DD_{slug}.md          # dated VEVENT / VTODO
"""

import os
from typing import Any, Dict, Iterator

from radicale.storage.markdown.convert_ical import (
    event_path_components, md_filename_for_event)
from radicale.storage.markdown.frontmatter import slugify
from radicale.storage.markdown.profile import CollectionLayout


class CalendarLayout(CollectionLayout):

    _MANAGED = {"recurring", "tasks"}

    def target_path(self, root: str, fm: Dict[str, Any]) -> str:
        path_parts = event_path_components(fm)
        if path_parts is not None:
            year, month, filename = path_parts
            return os.path.join(root, year, month, filename)
        if "rrule" in fm:
            filename = slugify(fm.get("title", "event")) + ".md"
            return os.path.join(root, "recurring", filename)
        if fm.get("component") == "vtodo" and "start" not in fm:
            filename = slugify(fm.get("title", "task")) + ".md"
            return os.path.join(root, "tasks", filename)
        filename = slugify(fm.get("title", "event")) + ".md"
        return os.path.join(root, "recurring", filename)

    def iter_item_paths(self, root: str) -> Iterator[str]:
        if not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if dirpath == root:
                dirnames[:] = [d for d in dirnames
                               if d in self._MANAGED or d.isdigit()]
            for fname in filenames:
                if fname.endswith(".md") and not fname.startswith("."):
                    yield os.path.join(dirpath, fname)

    def is_managed_dir(self, name: str) -> bool:
        return name in self._MANAGED or name.isdigit()
