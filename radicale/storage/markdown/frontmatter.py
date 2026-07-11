# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Format-agnostic Markdown + YAML frontmatter primitives.

These helpers are shared by every :class:`ItemConverter`. They know nothing
about iCalendar or vCard — only how to parse / render the ``---``-fenced
YAML header and the body, and how to slugify text for filenames.
"""

import re
from typing import Any, Dict, Tuple

import yaml


def parse_md_file(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse a markdown file into ``(frontmatter_dict, body_text)``.

    Expects YAML frontmatter between ``---`` fences. Returns
    ``({}, original_text)`` if the document is not frontmatter-fenced.
    """
    text = text.strip()
    if not text.startswith("---"):
        return {}, text

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
    yaml_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True,
                         sort_keys=False)
    parts = ["---", yaml_str.rstrip(), "---"]
    if body:
        parts.append("")
        parts.append(body)
    parts.append("")
    return "\n".join(parts)


def slugify(text: str, fallback: str = "item") -> str:
    """Convert text to a filesystem-safe slug (lowercase, hyphenated)."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:60] or fallback
