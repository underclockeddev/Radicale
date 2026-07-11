# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend - addressbook layout
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Filesystem layout policy for ``VADDRESSBOOK`` markdown collections.

Layout::

    {collection-root}/
        .addressbook.yaml
        {slug}.md                    # one file per VCARD

The slug is derived from ``fn`` (preferred) or falls back to ``n.family``,
``n.given``, then ``uid``.
"""

import os
from typing import Any, Dict, Iterator

from radicale.storage.markdown.frontmatter import slugify
from radicale.storage.markdown.profile import CollectionLayout


class AddressbookLayout(CollectionLayout):

    def _slug_for(self, fm: Dict[str, Any]) -> str:
        fn = fm.get("fn")
        if not fn:
            n = fm.get("n") or {}
            if isinstance(n, dict):
                fn = " ".join(filter(None, [
                    str(n.get("given") or ""),
                    str(n.get("family") or ""),
                ])).strip()
        if not fn:
            fn = str(fm.get("uid") or "contact")
        return slugify(fn, fallback="contact")

    def target_path(self, root: str, fm: Dict[str, Any]) -> str:
        filename = self._slug_for(fm) + ".md"
        return os.path.join(root, filename)

    def iter_item_paths(self, root: str) -> Iterator[str]:
        if not os.path.isdir(root):
            return
        for fname in sorted(os.listdir(root)):
            if fname.endswith(".md") and not fname.startswith("."):
                path = os.path.join(root, fname)
                if os.path.isfile(path):
                    yield path

    def is_managed_dir(self, name: str) -> bool:
        return False
