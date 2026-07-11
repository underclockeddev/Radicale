# This file is part of Radicale - CalDAV and CardDAV server
# Markdown storage backend
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Storage backend that uses Markdown files with YAML frontmatter.

One storage class drives both CalDAV (VCALENDAR) and CardDAV (VADDRESSBOOK)
collections. All per-tag variation lives behind a :class:`CollectionProfile`
strategy (see :mod:`radicale.storage.markdown.profile`). The Collection
class is tag-agnostic.

Filesystem layout — calendars::

    calendars/{user}/{calendar}/
        .calendar.yaml
        recurring/daily-standup.md
        tasks/water-plants.md
        2026/03/20_team-lunch.md

Filesystem layout — address books::

    contacts/{user}/{addressbook}/
        .addressbook.yaml
        contacts/jane-public.md
"""

import os
import posixpath
import threading
import time
from hashlib import sha256
from typing import (Any, Callable, ContextManager, Dict, Iterable, Iterator,
                    List, Mapping, Optional, Set, Tuple, overload)

import yaml

import radicale.item as radicale_item
from radicale import config, pathutils, storage, types
from radicale.log import logger
from radicale.storage.markdown.frontmatter import parse_md_file, render_md_file
from radicale.storage.markdown.profile import (
    CollectionProfile, ProfileRegistry)
# Side-effect import: populates ProfileRegistry.
from radicale.storage.markdown import profiles as _profiles  # noqa: F401


class Collection(storage.BaseCollection):
    """A markdown-backed collection (calendar OR addressbook).

    Per-tag behavior is delegated to a :class:`CollectionProfile`. Tag is
    resolved from on-disk metadata (or inferred from layout) on first
    metadata read, then cached.
    """

    _storage: "Storage"
    _path: str
    _filesystem_path: str
    _meta_cache: Optional[Mapping[str, str]]
    _profile_cache: Optional[CollectionProfile]

    def __init__(self, storage_: "Storage", path: str,
                 filesystem_path: Optional[str] = None) -> None:
        self._storage = storage_
        self._path = pathutils.strip_path(path)
        if filesystem_path is None:
            if storage_._flat_collections:
                # Route via the second path segment (collection name).
                parts = self.path.split("/") if self.path else []
                entry = None
                if len(parts) >= 2:
                    entry = storage_._flat_entry_for_name(parts[1])
                if entry is None:
                    entry = storage_._flat_collections[0]
                filesystem_path = entry["path"]
            else:
                folder = storage_._get_collection_root_folder()
                filesystem_path = pathutils.path_to_filesystem(
                    folder, self.path)
        self._filesystem_path = filesystem_path
        self._meta_cache = None
        self._profile_cache = None

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

    # ── Profile resolution ──────────────────────────────────────────────

    @property
    def profile(self) -> CollectionProfile:
        if self._profile_cache is None:
            # Ensure meta is loaded so tag is known.
            _ = self.get_meta(None)
            if self._profile_cache is None:
                # No metadata + no items → default profile.
                self._profile_cache = ProfileRegistry.get_or_default(None)
        return self._profile_cache

    def _bind_profile(self, tag: Optional[str]) -> None:
        self._profile_cache = ProfileRegistry.get_or_default(tag)

    # ── Metadata ────────────────────────────────────────────────────────

    def _existing_meta_path(self) -> Optional[str]:
        """Probe registered profiles for a metadata file that exists."""
        for fname in ProfileRegistry.metadata_filenames():
            candidate = os.path.join(self._filesystem_path, fname)
            if os.path.isfile(candidate):
                return candidate
        return None

    @property
    def _meta_path(self) -> str:
        """Where to write metadata.

        If a metadata file already exists, return its path. Otherwise
        return the path corresponding to the bound (or default) profile.
        """
        existing = self._existing_meta_path()
        if existing is not None:
            return existing
        prof = self._profile_cache or ProfileRegistry.get_or_default(None)
        return os.path.join(self._filesystem_path, prof.metadata_filename)

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
        """Load collection properties, binding the profile in the process."""
        props: Dict[str, str] = {}
        existing = self._existing_meta_path()
        if existing is not None:
            try:
                with open(existing, "r") as f:
                    raw = yaml.safe_load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        props[str(k)] = str(v)
            except Exception as e:
                logger.warning("Failed to load %r: %s", existing, e)

        if "tag" not in props:
            inferred = self._infer_tag_from_layout()
            if inferred is not None:
                props["tag"] = inferred

        self._bind_profile(props.get("tag"))
        return radicale_item.check_and_sanitize_props(props)

    def _infer_tag_from_layout(self) -> Optional[str]:
        """Guess the tag from on-disk directory shape.

        For each registered profile, ask its layout whether items exist.
        First non-empty layout wins.
        """
        if not os.path.isdir(self._filesystem_path):
            return None
        for prof in ProfileRegistry.all():
            for _ in prof.layout.iter_item_paths(self._filesystem_path):
                return prof.tag
        return None

    def set_meta(self, props: Mapping[str, str]) -> None:
        sanitized = radicale_item.check_and_sanitize_props(dict(props))
        # Bind profile early so we write to the correct metadata filename.
        self._bind_profile(sanitized.get("tag"))
        os.makedirs(self._filesystem_path, exist_ok=True)
        with open(self._meta_path, "w") as f:
            yaml.dump(dict(sanitized), f, default_flow_style=False,
                      allow_unicode=True)
        self._meta_cache = None

    # ── File scanning ───────────────────────────────────────────────────

    def _iter_md_files(self) -> Iterator[str]:
        # If profile is already bound, use its layout. Otherwise we have
        # to handle the bootstrapping case (collection has no metadata
        # yet) — iterate via every registered layout.
        if self._profile_cache is not None:
            yield from self._profile_cache.layout.iter_item_paths(
                self._filesystem_path)
            return
        seen: Set[str] = set()
        for prof in ProfileRegistry.all():
            for path in prof.layout.iter_item_paths(self._filesystem_path):
                if path not in seen:
                    seen.add(path)
                    yield path

    def _href_for_md(self, md_path: str) -> str:
        return self._storage._href_for_md_path(md_path, self.profile)

    def _md_path_for_href(self, href: str) -> Optional[str]:
        return self._storage._md_path_for_href(self.path, href)

    # ── Item conversion ─────────────────────────────────────────────────

    def _item_from_md(self, md_path: str) -> Optional[radicale_item.Item]:
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

        prof = self.profile
        try:
            vobj = prof.converter.md_text_to_vobject(fm, body)
        except Exception as e:
            logger.warning("Cannot convert %r to %s: %s",
                           md_path, prof.tag, e)
            return None

        href = self._href_for_md(md_path)
        last_modified = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT",
            time.gmtime(os.path.getmtime(md_path)))

        try:
            wire_text = prof.converter.serialize_for_protocol(vobj)
        except Exception as e:
            logger.warning("Cannot serialize %r for %s: %s",
                           md_path, prof.tag, e)
            return None

        return radicale_item.Item(
            collection=self,
            href=href,
            last_modified=last_modified,
            text=wire_text,
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
        prof = self.profile

        # Old item (if any)
        old_item: Optional[radicale_item.Item] = None
        old_fm: Optional[Dict[str, Any]] = None
        old_md_path = self._md_path_for_href(href)
        if old_md_path and os.path.isfile(old_md_path):
            old_item = self._item_from_md(old_md_path)
            try:
                with open(old_md_path, "r", encoding="utf-8") as f:
                    old_fm, _ = parse_md_file(f.read())
            except Exception:
                old_fm = None

        # Convert wire object → markdown
        ctx = {"default_timezone": self._storage._default_timezone}
        fm, body = prof.converter.vobject_to_md(item.vobject_item, ctx)

        # Profile-specific merge step (e.g. recurring completions for VTODO)
        fm = prof.merge_on_upload(old_fm, fm)

        # Place the file via layout
        target_path = prof.layout.target_path(self._filesystem_path, fm)
        target_dir = os.path.dirname(target_path)

        if old_md_path and old_md_path != target_path \
                and os.path.isfile(old_md_path):
            os.remove(old_md_path)
            prof.layout.cleanup_empty_dirs(
                self._filesystem_path, os.path.dirname(old_md_path))

        os.makedirs(target_dir, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(render_md_file(fm, body))

        uid = prof.converter.extract_uid(fm)
        if uid:
            self._storage._update_index(self.path, uid, target_path)

        uploaded = self._item_from_md(target_path)
        if uploaded is None:
            raise RuntimeError("Failed to read back uploaded item")
        return uploaded, old_item

    def delete(self, href: Optional[str] = None) -> None:
        if href is None:
            import shutil
            if os.path.isdir(self._filesystem_path):
                shutil.rmtree(self._filesystem_path)
            self._storage._clear_index(self.path)
            return

        md_path = self._md_path_for_href(href)
        if md_path is None or not os.path.isfile(md_path):
            raise storage.ComponentNotFoundError(href)

        os.remove(md_path)
        self.profile.layout.cleanup_empty_dirs(
            self._filesystem_path, os.path.dirname(md_path))

        uid = _href_to_uid(href)
        self._storage._remove_from_index(self.path, uid)

    # Legacy hook kept for back-compat with code calling
    # ``Collection._cleanup_empty_dirs`` directly.
    def _cleanup_empty_dirs(self, dirpath: str) -> None:
        self.profile.layout.cleanup_empty_dirs(self._filesystem_path, dirpath)

    # ── Sync ────────────────────────────────────────────────────────────

    def sync(self, old_token: str = "") -> Tuple[str, Iterable[str]]:
        token = "http://radicale.org/ns/sync/%s" % self.etag.strip('"')
        if old_token and old_token == token:
            return token, iter([])

        def hrefs_iter() -> Iterator[str]:
            for item in self.get_all():
                assert item.href
                yield item.href

        return token, hrefs_iter()


def _href_to_uid(href: str) -> str:
    """Strip a known item extension off a CalDAV/CardDAV href."""
    root, ext = os.path.splitext(href)
    if ext.lower() in (".ics", ".vcf"):
        return root
    return href


class Storage(storage.BaseStorage):
    """Markdown-backed storage engine driving the profile registry."""

    _collection_class = Collection
    _filesystem_folder: str
    _lock: threading.RLock

    _uid_index: Dict[str, Dict[str, str]]

    # List of flat collections, ordered. Each entry:
    #   {"name": str, "path": str, "tag": str}
    _flat_collections: List[Dict[str, str]]

    def __init__(self, configuration: config.Configuration) -> None:
        super().__init__(configuration)
        self._filesystem_folder = configuration.get(
            "storage", "filesystem_folder")
        self._default_timezone = configuration.get(
            "storage", "default_timezone") or ""

        self._flat_collections = self._load_flat_collections(configuration)

        # Re-register profiles to thread the default timezone into the
        # calendar profile's converter.
        from radicale.storage.markdown.profiles import (
            register_default_profiles)
        register_default_profiles(default_timezone=self._default_timezone)

        self._lock = threading.RLock()
        self._uid_index = {}

        root = self._get_collection_root_folder()
        if not os.path.exists(root):
            logger.info("Creating markdown storage root: %r", root)
            os.makedirs(root, exist_ok=True)
        if self._flat_collections:
            for entry in self._flat_collections:
                logger.info("Markdown flat collection: name=%s tag=%s path=%r",
                            entry["name"], entry["tag"], entry["path"])
        else:
            logger.info("Markdown storage location: %r",
                        self._filesystem_folder)

    def _load_flat_collections(self, configuration: config.Configuration
                               ) -> List[Dict[str, str]]:
        """Build the ordered flat-collection list from configuration.

        Sources, in order of precedence:

        1. ``flat_collections`` (JSON list) — multi-collection flat mode.
        2. ``flat_collection_path`` + ``flat_collection_tag`` — single entry.
        3. ``flat_calendar_path`` — legacy single-entry calendar mount.
        """
        import json as _json

        raw_multi = ""
        try:
            raw_multi = configuration.get("storage", "flat_collections") or ""
        except Exception:
            raw_multi = ""
        if raw_multi:
            try:
                parsed = _json.loads(raw_multi)
                out: List[Dict[str, str]] = []
                for i, entry in enumerate(parsed):
                    name = str(entry.get("name") or "default")
                    path = str(entry.get("path") or "")
                    tag = str(entry.get("tag") or "VCALENDAR").upper()
                    if not path:
                        logger.warning(
                            "flat_collections[%d] missing 'path' — skipped", i)
                        continue
                    out.append({"name": name, "path": path, "tag": tag})
                if out:
                    return out
            except Exception as e:
                logger.warning("Bad flat_collections JSON %r: %s",
                               raw_multi, e)

        single_path = ""
        try:
            single_path = configuration.get(
                "storage", "flat_collection_path") or ""
        except Exception:
            pass
        if not single_path:
            try:
                single_path = configuration.get(
                    "storage", "flat_calendar_path") or ""
            except Exception:
                pass
        if single_path:
            try:
                single_tag = (configuration.get(
                    "storage", "flat_collection_tag") or "VCALENDAR").upper()
            except Exception:
                single_tag = "VCALENDAR"
            return [{"name": "default", "path": single_path,
                     "tag": single_tag}]
        return []

    @property
    def _flat_collection_path(self) -> str:
        """Back-compat alias: path of the first flat collection (if any)."""
        return self._flat_collections[0]["path"] if self._flat_collections else ""

    def _flat_entry_for_name(self, name: str
                             ) -> Optional[Dict[str, str]]:
        for entry in self._flat_collections:
            if entry["name"] == name:
                return entry
        return None

    def _flat_entry_for_path(self, fs_path: str
                             ) -> Optional[Dict[str, str]]:
        for entry in self._flat_collections:
            if entry["path"] == fs_path:
                return entry
        return None

    def _get_collection_root_folder(self) -> str:
        if self._flat_collections:
            # Returned only as a discovery root for path-relative ops;
            # actual per-collection roots live in _flat_collections.
            return self._flat_collections[0]["path"]
        return os.path.join(self._filesystem_folder, "collection-root")

    # ── UID ↔ path index ────────────────────────────────────────────────

    def _build_index(self, collection_path: str,
                     filesystem_path: str) -> Dict[str, str]:
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
        if collection_path not in self._uid_index:
            self._uid_index[collection_path] = self._build_index(
                collection_path, filesystem_path)
        return self._uid_index[collection_path]

    def _uid_from_md_file(self, md_path: str) -> Optional[str]:
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read(4096)
        except (OSError, UnicodeDecodeError):
            return None
        fm, _ = parse_md_file(content)
        uid = fm.get("uid")
        return str(uid) if uid else None

    def _href_for_md_path(self, md_path: str,
                          profile: Optional[CollectionProfile] = None) -> str:
        ext = profile.item_extension if profile else ".ics"
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read(4096)
            fm, _ = parse_md_file(content)
            uid = fm.get("uid")
            if uid:
                return str(uid) + ext
        except Exception:
            pass
        return sha256(md_path.encode()).hexdigest()[:16] + ext

    def _md_path_for_href(self, collection_path: str,
                          href: str) -> Optional[str]:
        uid = _href_to_uid(href)
        collection_path = collection_path.strip("/")
        if self._flat_collections:
            parts = collection_path.split("/") if collection_path else []
            entry: Optional[Dict[str, str]] = None
            if len(parts) >= 2:
                entry = self._flat_entry_for_name(parts[1])
            if entry is None:
                entry = self._flat_collections[0]
            fs_path = entry["path"]
        else:
            folder = self._get_collection_root_folder()
            try:
                fs_path = pathutils.path_to_filesystem(
                    folder, collection_path)
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

        if self._flat_collections:
            yield from self._discover_flat(
                sane_path, attributes, depth, child_context_manager)
        else:
            yield from self._discover_nested(
                sane_path, attributes, depth, child_context_manager)

    def _bootstrap_flat_entry(self, entry: Dict[str, str]) -> None:
        """Ensure a configured flat collection has a metadata file."""
        flat_dir = entry["path"]
        os.makedirs(flat_dir, exist_ok=True)
        for fname in ProfileRegistry.metadata_filenames():
            if os.path.isfile(os.path.join(flat_dir, fname)):
                return
        prof = ProfileRegistry.get_or_default(entry["tag"])
        meta_path = os.path.join(flat_dir, prof.metadata_filename)
        with open(meta_path, "w", encoding="utf-8") as f:
            yaml.dump({"tag": prof.tag,
                       "D:displayname": prof.default_displayname}, f)
        logger.info("Created default %s in flat dir %r",
                    prof.metadata_filename, flat_dir)

    def _discover_flat(
            self, sane_path: str, attributes: List[str], depth: str,
            child_context_manager: Callable[
                [str, Optional[str]], ContextManager[None]]
    ) -> Iterator[types.CollectionOrItem]:
        for entry in self._flat_collections:
            self._bootstrap_flat_entry(entry)

        n = len(attributes)

        if n == 0:
            # Root pseudo-collection. Use the first flat dir as its physical
            # backing — it's only used for last_modified.
            root_backing = self._flat_collections[0]["path"]
            yield Collection(self, "/", root_backing)
            return

        if n == 1:
            user_path = attributes[0]
            # The user principal itself — back it with the first flat dir.
            yield Collection(
                self, pathutils.unstrip_path(user_path, True),
                self._flat_collections[0]["path"])
            if depth != "0":
                for entry in self._flat_collections:
                    child_path = user_path + "/" + entry["name"]
                    with child_context_manager(child_path, None):
                        yield Collection(
                            self,
                            pathutils.unstrip_path(child_path, True),
                            entry["path"])
            return

        if n == 2:
            col_name = attributes[1]
            entry = self._flat_entry_for_name(col_name)
            if entry is None:
                return
            col_path = "/".join(attributes)
            col = Collection(
                self, pathutils.unstrip_path(col_path, True), entry["path"])
            yield col
            if depth != "0" and col.tag:
                for item in col.get_all():
                    with child_context_manager(col_path, item.href):
                        yield item
            return

        if n == 3:
            col_name = attributes[1]
            entry = self._flat_entry_for_name(col_name)
            if entry is None:
                return
            href = attributes[2]
            col_path = "/".join(attributes[:2])
            col = Collection(
                self, pathutils.unstrip_path(col_path, True), entry["path"])
            md_path = col._md_path_for_href(href)
            if md_path and os.path.isfile(md_path):
                item = col._item_from_md(md_path)
                if item is not None:
                    yield item

    def _discover_nested(
            self, sane_path: str, attributes: List[str], depth: str,
            child_context_manager: Callable[
                [str, Optional[str]], ContextManager[None]]
    ) -> Iterator[types.CollectionOrItem]:
        folder = self._get_collection_root_folder()
        os.makedirs(folder, exist_ok=True)

        try:
            filesystem_path = pathutils.path_to_filesystem(folder, sane_path)
        except ValueError as e:
            logger.debug("Unsafe path %r: %s", sane_path, e)
            return

        href: Optional[str] = None
        if not os.path.isdir(filesystem_path):
            if attributes:
                href = attributes.pop()
            else:
                return
        else:
            href = None

        sane_path = "/".join(attributes)
        collection = Collection(self, pathutils.unstrip_path(sane_path, True))

        if href:
            md_path = collection._md_path_for_href(href)
            if md_path and os.path.isfile(md_path):
                item = collection._item_from_md(md_path)
                if item is not None:
                    yield item
            return

        yield collection

        if depth == "0":
            return

        if collection.tag:
            for item in collection.get_all():
                with child_context_manager(sane_path, item.href):
                    yield item

        def _is_managed(name: str) -> bool:
            return any(p.layout.is_managed_dir(name)
                       for p in ProfileRegistry.all())

        for entry in os.scandir(filesystem_path):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            if _is_managed(name) or name.isdigit():
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

        src_path = item.collection._md_path_for_href(item.href)
        if not src_path or not os.path.isfile(src_path):
            raise storage.ComponentNotFoundError(item.href)

        to_collection.upload(to_href, item)

        if os.path.isfile(src_path):
            os.remove(src_path)
            item.collection.profile.layout.cleanup_empty_dirs(
                item.collection._filesystem_path, os.path.dirname(src_path))

        uid = item.uid
        if uid:
            self._remove_from_index(item.collection.path, uid)

    def create_collection(
            self, href: str,
            items: Optional[Iterable[radicale_item.Item]] = None,
            props: Optional[Mapping[str, str]] = None
    ) -> Tuple[Collection, Dict[str, radicale_item.Item], List[str]]:
        sane_path = pathutils.strip_path(href)

        if self._flat_collections:
            parts = sane_path.split("/") if sane_path else []
            entry: Optional[Dict[str, str]] = None
            if len(parts) >= 2:
                entry = self._flat_entry_for_name(parts[1])
            if entry is None:
                entry = self._flat_collections[0]
            filesystem_path = entry["path"]
        else:
            folder = self._get_collection_root_folder()
            filesystem_path = pathutils.path_to_filesystem(folder, sane_path)

        os.makedirs(filesystem_path, exist_ok=True)

        col = Collection(self, pathutils.unstrip_path(sane_path, True),
                         filesystem_path)

        if props:
            col.set_meta(props)

        new_hrefs: List[str] = []
        replaced_items: Dict[str, radicale_item.Item] = {}

        if items is not None and props and props.get("tag"):
            ext = col.profile.item_extension
            for item in items:
                uploaded, old = col.upload(item.uid + ext, item)
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
