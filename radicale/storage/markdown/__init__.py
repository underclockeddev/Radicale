# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Storage backend that uses Markdown files with YAML frontmatter.

Filesystem layout::

    calendars/{user}/{calendar}/
        .calendar.yaml              # collection metadata
        recurring/                  # recurring events (rrule-based)
            daily-standup.md
        2026/
            03/
                20_team-lunch.md    # DD_slug.md
                21_dentist.md

Each ``.md`` file is a calendar event. YAML frontmatter contains structured
fields (title, start, end, rrule, etc.) and the markdown body becomes the
event description.  The CalDAV protocol layer is unmodified — this backend
converts between Markdown and iCalendar on the fly.
"""

import json
import os
import posixpath
import time
import threading
import uuid
from hashlib import sha256
from typing import (Callable, ContextManager, Dict, Iterable, Iterator, List,
                    Mapping, Optional, Set, Tuple, Union, overload)

import yaml

import radicale.item as radicale_item
from radicale import config, pathutils, storage, types
from radicale.log import logger
from radicale.storage.markdown.convert import (
    event_path_components, md_filename_for_event, md_to_vcalendar,
    parse_md_file, render_md_file, slugify, vcalendar_to_md,
)


class Collection(storage.BaseCollection):
    """A calendar collection backed by a directory of Markdown files."""

    _storage: "Storage"
    _path: str
    _filesystem_path: str
    _meta_cache: Optional[Mapping[str, str]]

    def __init__(self, storage_: "Storage", path: str,
                 filesystem_path: Optional[str] = None) -> None:
        self._storage = storage_
        self._path = pathutils.strip_path(path)
        if filesystem_path is None:
            folder = storage_._get_collection_root_folder()
            filesystem_path = pathutils.path_to_filesystem(folder, self.path)
        self._filesystem_path = filesystem_path
        self._meta_cache = None

    # ── Identity ────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return self._path

    @property
    def last_modified(self) -> str:
        latest = os.path.getmtime(self._filesystem_path)
        for md_path in self._iter_md_files():
            mtime = os.path.getmtime(md_path)
            if mtime > latest:
                latest = mtime
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                             time.gmtime(latest))

    # ── Metadata (.calendar.yaml) ───────────────────────────────────────

    @property
    def _meta_path(self) -> str:
        return os.path.join(self._filesystem_path, ".calendar.yaml")

    @overload
    def get_meta(self, key: None = None) -> Mapping[str, str]: ...

    @overload
    def get_meta(self, key: str) -> Optional[str]: ...

    def get_meta(self, key=None):
        if self._meta_cache is None:
            self._meta_cache = self._load_meta()
        if key is None:
            return self._meta_cache
        return self._meta_cache.get(key)

    def _load_meta(self) -> Mapping[str, str]:
        """Load collection properties from .calendar.yaml."""
        props: Dict[str, str] = {}
        if os.path.isfile(self._meta_path):
            try:
                with open(self._meta_path, "r") as f:
                    raw = yaml.safe_load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        props[str(k)] = str(v)
            except Exception as e:
                logger.warning("Failed to load %r: %s", self._meta_path, e)

        # If we have .md files but no tag, infer VCALENDAR
        if "tag" not in props:
            for _ in self._iter_md_files():
                props["tag"] = "VCALENDAR"
                break

        return radicale_item.check_and_sanitize_props(props)

    def set_meta(self, props: Mapping[str, str]) -> None:
        sanitized = radicale_item.check_and_sanitize_props(dict(props))
        os.makedirs(os.path.dirname(self._meta_path), exist_ok=True)
        with open(self._meta_path, "w") as f:
            yaml.dump(dict(sanitized), f, default_flow_style=False,
                      allow_unicode=True)
        self._meta_cache = None

    # ── File scanning ───────────────────────────────────────────────────

    def _iter_md_files(self) -> Iterator[str]:
        """Yield absolute paths to all .md files in this collection.

        Scans year/month and recurring/ subdirectories, but does NOT
        recurse into peer collections (directories that look like calendar
        collections — those with .calendar.yaml or non-date names).
        """
        base = self._filesystem_path
        if not os.path.isdir(base):
            return
        for dirpath, dirnames, filenames in os.walk(base):
            # Skip hidden dirs
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            # At the collection root level, only recurse into
            # year dirs (numeric) and 'recurring'
            if dirpath == base:
                dirnames[:] = [d for d in dirnames
                               if d == "recurring" or d.isdigit()]
            yield from (
                os.path.join(dirpath, fname)
                for fname in filenames
                if fname.endswith(".md") and not fname.startswith(".")
            )

    def _href_for_md(self, md_path: str) -> str:
        """Compute the CalDAV href for a markdown file.

        The href is ``{uid}.ics`` so CalDAV clients see normal .ics names.
        We read the uid from frontmatter, falling back to a hash of the path.
        """
        return self._storage._href_for_md_path(md_path)

    def _md_path_for_href(self, href: str) -> Optional[str]:
        """Look up the filesystem path for a CalDAV href."""
        return self._storage._md_path_for_href(self.path, href)

    # ── Item conversion ─────────────────────────────────────────────────

    def _item_from_md(self, md_path: str) -> Optional[radicale_item.Item]:
        """Read a .md file and return a radicale Item."""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Cannot read %r: %s", md_path, e)
            return None

        fm, body = parse_md_file(text)
        if not fm:
            logger.debug("No frontmatter in %r, skipping", md_path)
            return None

        try:
            cal = md_to_vcalendar(fm, body)
        except Exception as e:
            logger.warning("Cannot convert %r to iCalendar: %s", md_path, e)
            return None

        href = self._href_for_md(md_path)
        last_modified = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT",
            time.gmtime(os.path.getmtime(md_path)))

        try:
            ical_text = cal.serialize()
        except Exception as e:
            logger.warning("Cannot serialize iCalendar for %r: %s",
                           md_path, e)
            return None

        return radicale_item.Item(
            collection=self,
            href=href,
            last_modified=last_modified,
            text=ical_text,
        )

    # ── Read operations ─────────────────────────────────────────────────

    def get_all(self) -> Iterator[radicale_item.Item]:
        for md_path in self._iter_md_files():
            item = self._item_from_md(md_path)
            if item is not None:
                yield item

    def get_multi(self, hrefs: Iterable[str]
                  ) -> Iterator[Tuple[str, Optional[radicale_item.Item]]]:
        for href in hrefs:
            md_path = self._md_path_for_href(href)
            if md_path and os.path.isfile(md_path):
                item = self._item_from_md(md_path)
                yield (href, item)
            else:
                yield (href, None)

    # ── Write operations ────────────────────────────────────────────────

    def upload(self, href: str, item: radicale_item.Item
              ) -> Tuple[radicale_item.Item, Optional[radicale_item.Item]]:
        # Get old item if it exists
        old_item: Optional[radicale_item.Item] = None
        old_md_path = self._md_path_for_href(href)
        if old_md_path and os.path.isfile(old_md_path):
            old_item = self._item_from_md(old_md_path)

        # Convert iCalendar to markdown
        cal = item.vobject_item
        fm, body = vcalendar_to_md(cal)

        # Determine filesystem path
        path_parts = event_path_components(fm)
        if path_parts is not None:
            year, month, filename = path_parts
            target_dir = os.path.join(self._filesystem_path, year, month)
        else:
            # Recurring event → recurring/ folder
            filename = slugify(fm.get("title", "event")) + ".md"
            target_dir = os.path.join(self._filesystem_path, "recurring")

        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, filename)

        # If the old file was at a different path (e.g., date changed), remove it
        if old_md_path and old_md_path != target_path and os.path.isfile(old_md_path):
            os.remove(old_md_path)
            self._cleanup_empty_dirs(os.path.dirname(old_md_path))

        # Write the markdown file
        md_text = render_md_file(fm, body)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        # Update the index
        uid = fm.get("uid", "")
        if uid:
            self._storage._update_index(self.path, uid, target_path)

        # Read back the uploaded item
        uploaded = self._item_from_md(target_path)
        if uploaded is None:
            raise RuntimeError("Failed to read back uploaded item")
        return uploaded, old_item

    def delete(self, href: Optional[str] = None) -> None:
        if href is None:
            # Delete the entire collection
            import shutil
            if os.path.isdir(self._filesystem_path):
                shutil.rmtree(self._filesystem_path)
            self._storage._clear_index(self.path)
            return

        md_path = self._md_path_for_href(href)
        if md_path is None or not os.path.isfile(md_path):
            raise storage.ComponentNotFoundError(href)

        os.remove(md_path)
        self._cleanup_empty_dirs(os.path.dirname(md_path))

        # Remove from index
        uid = href[:-4] if href.endswith(".ics") else href
        self._storage._remove_from_index(self.path, uid)

    def _cleanup_empty_dirs(self, dirpath: str) -> None:
        """Remove empty year/month directories up to the collection root."""
        while dirpath != self._filesystem_path:
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
                else:
                    break
            except OSError:
                break
            dirpath = os.path.dirname(dirpath)

    # ── Sync ────────────────────────────────────────────────────────────

    def sync(self, old_token: str = "") -> Tuple[str, Iterable[str]]:
        token = "http://radicale.org/ns/sync/%s" % self.etag.strip('"')
        if old_token:
            raise ValueError("Sync tokens are not supported")

        def hrefs_iter() -> Iterator[str]:
            for item in self.get_all():
                assert item.href
                yield item.href

        return token, hrefs_iter()


class Storage(storage.BaseStorage):
    """Markdown-backed storage engine for Radicale."""

    _collection_class = Collection
    _filesystem_folder: str
    _lock: threading.RLock

    # UID → filesystem path index, keyed by collection path
    _uid_index: Dict[str, Dict[str, str]]  # {collection_path: {uid: md_path}}

    def __init__(self, configuration: config.Configuration) -> None:
        super().__init__(configuration)
        self._filesystem_folder = configuration.get(
            "storage", "filesystem_folder")
        self._lock = threading.RLock()
        self._uid_index = {}

        root = self._get_collection_root_folder()
        if not os.path.exists(root):
            logger.info("Creating markdown storage root: %r", root)
            os.makedirs(root, exist_ok=True)
        logger.info("Markdown storage location: %r", self._filesystem_folder)

    def _get_collection_root_folder(self) -> str:
        return os.path.join(self._filesystem_folder, "collection-root")

    # ── UID ↔ path index ────────────────────────────────────────────────

    def _build_index(self, collection_path: str,
                     filesystem_path: str) -> Dict[str, str]:
        """Scan all .md files and build UID → path mapping."""
        index: Dict[str, str] = {}
        if not os.path.isdir(filesystem_path):
            return index
        for dirpath, dirnames, filenames in os.walk(filesystem_path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.endswith(".md") and not fname.startswith("."):
                    full_path = os.path.join(dirpath, fname)
                    uid = self._uid_from_md_file(full_path)
                    if uid:
                        index[uid] = full_path
        return index

    def _get_index(self, collection_path: str,
                   filesystem_path: str) -> Dict[str, str]:
        """Get or build the index for a collection."""
        if collection_path not in self._uid_index:
            self._uid_index[collection_path] = self._build_index(
                collection_path, filesystem_path)
        return self._uid_index[collection_path]

    def _uid_from_md_file(self, md_path: str) -> Optional[str]:
        """Read just the UID from a markdown file's frontmatter."""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                # Read just enough to get frontmatter
                content = f.read(4096)
        except (OSError, UnicodeDecodeError):
            return None

        fm, _ = parse_md_file(content)
        uid = fm.get("uid")
        return str(uid) if uid else None

    def _href_for_md_path(self, md_path: str) -> str:
        """Return the CalDAV href for a markdown file."""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read(4096)
            fm, _ = parse_md_file(content)
            uid = fm.get("uid")
            if uid:
                return str(uid) + ".ics"
        except Exception:
            pass
        # Fallback: hash the path
        return sha256(md_path.encode()).hexdigest()[:16] + ".ics"

    def _md_path_for_href(self, collection_path: str,
                          href: str) -> Optional[str]:
        """Look up the filesystem path for a CalDAV href."""
        uid = href[:-4] if href.endswith(".ics") else href
        # collection_path may already be stripped or sanitized
        collection_path = collection_path.strip("/")
        folder = self._get_collection_root_folder()
        try:
            fs_path = pathutils.path_to_filesystem(folder, collection_path)
        except ValueError:
            return None
        index = self._get_index(collection_path, fs_path)
        return index.get(uid)

    def _update_index(self, collection_path: str, uid: str,
                      md_path: str) -> None:
        collection_path = collection_path.strip("/")
        if collection_path in self._uid_index:
            self._uid_index[collection_path][uid] = md_path

    def _remove_from_index(self, collection_path: str, uid: str) -> None:
        collection_path = collection_path.strip("/")
        if collection_path in self._uid_index:
            self._uid_index[collection_path].pop(uid, None)

    def _clear_index(self, collection_path: str) -> None:
        collection_path = collection_path.strip("/")
        self._uid_index.pop(collection_path, None)

    # ── BaseStorage interface ───────────────────────────────────────────

    def discover(
            self, path: str, depth: str = "0",
            child_context_manager: Optional[
                Callable[[str, Optional[str]], ContextManager[None]]] = None,
            user_groups: Set[str] = set()
    ) -> Iterator[types.CollectionOrItem]:
        if child_context_manager is None:
            @types.contextmanager
            def child_context_manager(path: str,
                                      href: Optional[str]) -> Iterator[None]:
                yield

        sane_path = pathutils.strip_path(path)
        attributes = sane_path.split("/") if sane_path else []

        folder = self._get_collection_root_folder()
        os.makedirs(folder, exist_ok=True)

        try:
            filesystem_path = pathutils.path_to_filesystem(folder, sane_path)
        except ValueError as e:
            logger.debug("Unsafe path %r: %s", sane_path, e)
            return

        # Check if path points to an item (href) or a collection
        href: Optional[str] = None
        if not os.path.isdir(filesystem_path):
            # Maybe it's an item href like "uid.ics"
            if attributes:
                href = attributes.pop()
            else:
                return
        else:
            href = None

        sane_path = "/".join(attributes)
        collection = Collection(self, pathutils.unstrip_path(sane_path, True))

        if href:
            # Looking up a specific item
            md_path = collection._md_path_for_href(href)
            if md_path and os.path.isfile(md_path):
                item = collection._item_from_md(md_path)
                if item is not None:
                    yield item
            return

        yield collection

        if depth == "0":
            return

        # Yield items in this collection (only if it's a tagged collection)
        if collection.tag:
            for item in collection.get_all():
                with child_context_manager(sane_path, item.href):
                    yield item

        # Yield child collections (subdirectories that are collections,
        # not year/month/recurring dirs)
        for entry in os.scandir(filesystem_path):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            # Skip year directories and 'recurring' — they're part of
            # the current collection, not child collections
            if name == "recurring" or name.isdigit():
                continue
            if not pathutils.is_safe_filesystem_path_component(name):
                continue
            child_path = posixpath.join(sane_path, name)
            with child_context_manager(child_path, None):
                yield Collection(
                    self, pathutils.unstrip_path(child_path, True))

    def move(self, item: radicale_item.Item,
             to_collection: storage.BaseCollection, to_href: str) -> None:
        assert isinstance(to_collection, Collection)
        assert isinstance(item.collection, Collection)
        assert item.href

        # Get the source md path
        src_path = item.collection._md_path_for_href(item.href)
        if not src_path or not os.path.isfile(src_path):
            raise storage.ComponentNotFoundError(item.href)

        # Read the file content
        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()

        # Write to destination collection via upload
        to_collection.upload(to_href, item)

        # Remove source
        os.remove(src_path)
        item.collection._cleanup_empty_dirs(os.path.dirname(src_path))

        # Update index
        uid = item.uid
        if uid:
            self._remove_from_index(item.collection.path, uid)

    def create_collection(
            self, href: str,
            items: Optional[Iterable[radicale_item.Item]] = None,
            props: Optional[Mapping[str, str]] = None
    ) -> Tuple[Collection, Dict[str, radicale_item.Item], List[str]]:
        folder = self._get_collection_root_folder()
        sane_path = pathutils.strip_path(href)
        filesystem_path = pathutils.path_to_filesystem(folder, sane_path)

        os.makedirs(filesystem_path, exist_ok=True)

        col = Collection(self, pathutils.unstrip_path(sane_path, True))

        if props:
            col.set_meta(props)

        new_hrefs: List[str] = []
        replaced_items: Dict[str, radicale_item.Item] = {}

        if items is not None and props and props.get("tag"):
            for item in items:
                uploaded, old = col.upload(item.uid + ".ics", item)
                if old:
                    replaced_items[uploaded.href or ""] = old
                else:
                    new_hrefs.append(uploaded.href or "")

        return col, replaced_items, new_hrefs

    @types.contextmanager
    def acquire_lock(self, mode: str, user: str = "",
                     *args, **kwargs) -> Iterator[None]:
        self._lock.acquire()
        try:
            yield
        finally:
            # Invalidate indexes on write to pick up external changes
            if mode == "w":
                self._uid_index.clear()
            self._lock.release()

    def verify(self) -> bool:
        root = self._get_collection_root_folder()
        if not os.path.isdir(root):
            logger.error("Storage root not found: %r", root)
            return False
        logger.info("Markdown storage verified: %r", root)
        return True
