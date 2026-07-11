# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Strategy + Registry seam for the Markdown storage backend.

A :class:`CollectionProfile` bundles every per-tag decision (item format,
file layout, metadata filename, per-upload merge semantics) behind a single
abstraction. The :class:`Collection` class depends only on this ABC, so
adding a new collection family (e.g. journals, contact groups) means
registering a new profile — never editing existing branches.

Layout
------
``ItemConverter``
    md text ⇄ vobject component. Format-only concern.
``CollectionLayout``
    Where on disk an item lives, and how to enumerate items in a
    collection. Filesystem-policy-only concern.
``CollectionProfile``
    Aggregates the above with the metadata filename, file extension,
    and per-tag upload-merge hook.
``ProfileRegistry``
    Module-level singleton mapping ``collection.tag`` → profile.
"""

import abc
from typing import Any, Dict, Iterator, List, Optional, Tuple

import vobject


class ItemConverter(abc.ABC):
    """Strategy for converting a single item between markdown and vobject."""

    @abc.abstractmethod
    def md_text_to_vobject(self, fm: Dict[str, Any], body: str
                           ) -> vobject.base.Component:
        """Build a vobject Component from frontmatter + body."""

    @abc.abstractmethod
    def vobject_to_md(self, comp: vobject.base.Component,
                      context: Optional[Dict[str, Any]] = None
                      ) -> Tuple[Dict[str, Any], str]:
        """Convert a vobject Component into ``(frontmatter, body)``."""

    @abc.abstractmethod
    def serialize_for_protocol(self, comp: vobject.base.Component) -> str:
        """Serialize the vobject Component to the wire format (.ics / .vcf)."""

    @abc.abstractmethod
    def extract_uid(self, fm: Dict[str, Any]) -> Optional[str]:
        """Return the UID from a frontmatter dict, or ``None``."""


class CollectionLayout(abc.ABC):
    """Strategy for placing items on disk and iterating them."""

    @abc.abstractmethod
    def target_path(self, root: str, fm: Dict[str, Any]) -> str:
        """Absolute path where an item with this frontmatter should live."""

    @abc.abstractmethod
    def iter_item_paths(self, root: str) -> Iterator[str]:
        """Yield absolute paths to every item file under *root*."""

    @abc.abstractmethod
    def is_managed_dir(self, name: str) -> bool:
        """Is *name* a directory this layout owns inside the collection?

        Used by the storage discover loop to distinguish layout-internal
        subdirectories from peer child collections.
        """

    def cleanup_empty_dirs(self, root: str, start: str) -> None:
        """Remove empty layout-internal directories from *start* up to *root*."""
        import os
        while start != root and start.startswith(root):
            try:
                if not os.listdir(start):
                    os.rmdir(start)
                else:
                    break
            except OSError:
                break
            start = os.path.dirname(start)


class CollectionProfile(abc.ABC):
    """Aggregate of every per-tag decision for the markdown backend."""

    tag: str
    item_extension: str          # ".ics" or ".vcf" — protocol-side extension
    metadata_filename: str       # e.g. ".calendar.yaml"
    default_displayname: str

    converter: ItemConverter
    layout: CollectionLayout

    def merge_on_upload(self, old_fm: Optional[Dict[str, Any]],
                        new_fm: Dict[str, Any]) -> Dict[str, Any]:
        """Hook for per-tag upload merging.

        Default: identity (return ``new_fm`` unchanged). The calendar
        profile overrides this to merge ``completions`` across writes.
        """
        return new_fm


class ProfileRegistry:
    """Module-level mapping of collection tag → profile.

    Profiles are registered eagerly when ``radicale.storage.markdown`` is
    imported (see ``profiles.py``). Lookup is by ``collection.tag``.
    """

    _profiles: Dict[str, CollectionProfile] = {}
    _default_tag: str = "VCALENDAR"

    @classmethod
    def register(cls, profile: CollectionProfile) -> None:
        cls._profiles[profile.tag.upper()] = profile

    @classmethod
    def get(cls, tag: Optional[str]) -> CollectionProfile:
        key = (tag or cls._default_tag).upper()
        if key not in cls._profiles:
            raise KeyError(
                "No markdown profile registered for tag %r "
                "(registered: %s)" % (
                    tag, sorted(cls._profiles.keys())))
        return cls._profiles[key]

    @classmethod
    def get_or_default(cls, tag: Optional[str]) -> CollectionProfile:
        """Like :meth:`get` but falls back to the default profile."""
        try:
            return cls.get(tag)
        except KeyError:
            return cls.get(cls._default_tag)

    @classmethod
    def all(cls) -> List[CollectionProfile]:
        return list(cls._profiles.values())

    @classmethod
    def set_default(cls, tag: str) -> None:
        cls._default_tag = tag.upper()

    @classmethod
    def default_tag(cls) -> str:
        return cls._default_tag

    @classmethod
    def metadata_filenames(cls) -> List[str]:
        return [p.metadata_filename for p in cls.all()]
