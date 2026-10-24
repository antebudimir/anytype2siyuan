#!/usr/bin/env python3
"""Pre-process a flat Anytype Markdown export into a nested folder structure
that SiYuan's "Markdown Folder" importer accepts directly.

Anytype exports every page as a sibling .md file in one flat directory and
encodes the page tree in each file's YAML frontmatter (Links / Backlinks).
SiYuan's importer preserves directory hierarchy and merges X.md + X/ into a
single document, so materializing the tree as nested folders is sufficient.

The native backup ZIP (optional third argument) is used to restore what the
Markdown export lost: page titles (details.name) and code block languages
(blocks[].fields.lang). Page ids in the export frontmatter map the two.

Usage:
    python3 anytype_prep.py SRC OUT [NATIVE_BACKUP_ZIP]
"""

import html
import json
import posixpath
import re
import shutil
import sys
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import unquote

MAX_NAME_LEN = 80
MIN_BODY_CHARS = 10

# Pages pinned to the tree root regardless of backlink-based parenting
# (keyed by export file name). Anytype's flat export carries no real
# hierarchy, and the first-backlink heuristic lets a stray note that
# links to an important page capture it; pin hub pages explicitly.
FORCE_ROOT = {"home.md"}

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

FRONTMATTER_DELIM = "---"
LIST_ITEM_RE = re.compile(r"^\s{2,}-\s+(.*)$")
KEY_RE = re.compile(r"^([A-Za-z 0-9_-]+):\s*(.*)$")
LINK_RE = re.compile(
    r"\[([^\]]*)\]\(<?([^)#<>]+?\.md)(?:>?\)|#[^)]*\)|\?[^)]*\))")

ASSET_LINK_RE = re.compile(r"\]\((files/[^)\s<>]+)\)")


def parse_frontmatter(text):
    """Split a file into (frontmatter dict, body). Returns ({}, text) if absent."""
    if not text.startswith(FRONTMATTER_DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end = i
            break
    if end is None:
        return {}, text
    meta = {}
    current_key = None
    for line in lines[1:end]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        key_match = KEY_RE.match(line)
        item_match = LIST_ITEM_RE.match(line)
        if key_match and not line.startswith((" ", "\t")):
            key = key_match.group(1).strip()
            value = key_match.group(2).strip().strip('"')
            meta[key] = value if value else []
            current_key = key
        elif item_match and current_key is not None:
            if isinstance(meta.get(current_key), list):
                meta[current_key].append(item_match.group(1).strip())
    body = "".join(lines[end + 1:])
    return meta, body


def first_heading(body):
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("#!"):
            return stripped[2:].strip()
    return None


def load_native_backup(zip_path):
    """Map object id -> (details.name, ordered [(lang, code_text), ...]).

    Also returns a child-object-id -> parent-object-id map built from
    the real page tree: any snapshot block whose link has style "Page"
    embeds a child page at that position, so link.targetBlockId is the
    child and the snapshot's own object id its parent. A third map
    carries each object's layout, used to triage orphans by type.
    """
    names, codes, parents, layouts = {}, {}, {}, {}
    with zipfile.ZipFile(zip_path) as zf:
        for entry in zf.namelist():
            if not entry.endswith(".pb.json"):
                continue
            try:
                data = json.loads(zf.read(entry))
            except (ValueError, UnicodeDecodeError):
                continue
            snapshot = data.get("snapshot")
            if not isinstance(snapshot, dict):
                continue
            data = snapshot.get("data") or {}
            obj_id = (data.get("details") or {}).get("id") or data.get("id")
            # fall back to the entry name, which is the object id
            if not obj_id:
                obj_id = Path(entry).name[: -len(".pb.json")]
            name = (data.get("details") or {}).get("name")
            if isinstance(name, str) and name:
                names[obj_id] = name
            layout = (data.get("details") or {}).get("resolvedLayout") \
                or (data.get("details") or {}).get("layout")
            layouts[obj_id] = layout
            blocks = []
            for block in data.get("blocks") or []:
                text = block.get("text")
                if isinstance(text, dict) and text.get("style") == "Code":
                    lang = (block.get("fields") or {}).get("lang") or ""
                    blocks.append((lang, text.get("text") or ""))
                link = block.get("link")
                if (isinstance(link, dict) and link.get("style") == "Page"
                        and link.get("targetBlockId")):
                    # first Page link wins: the real tree has one parent
                    parents.setdefault(link["targetBlockId"], obj_id)
            if blocks:
                codes[obj_id] = blocks
    return names, codes, parents, layouts


FENCE_RE = re.compile(r"^\s{0,3}```(.*)$")


def normalize_code(text):
    """Comparable form of code text: rstrip each line, drop edge blanks."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def restore_code_langs(body, code_blocks, stats):
    """Fill bare opening fences with languages from the native backup.

    Matching is by normalized code text, not block order, because the
    Markdown export does not always emit code blocks in block-tree order.
    """
    if not code_blocks:
        return body, 0
    lang_by_text = {}
    for lang, native_text in code_blocks:
        lang_by_text.setdefault(normalize_code(native_text), lang or "")
    lines = body.split("\n")
    applied = 0
    inside = False
    for i, line in enumerate(lines):
        fence = FENCE_RE.match(line)
        if fence is None:
            continue
        if inside:
            inside = False  # closing fence
            continue
        inside = True
        if fence.group(1).strip():
            continue  # opening fence already carries a language
        # The fence body is the next lines up to the closing fence.
        j = i + 1
        while j < len(lines) and not FENCE_RE.match(lines[j]):
            j += 1
        exported = normalize_code("\n".join(lines[i + 1 : j]))
        if exported not in lang_by_text:
            stats["mismatch"] += 1
            continue
        lang = lang_by_text[exported]
        if lang and lang.lower() not in ("plain", "plain text"):
            lines[i] = "```" + lang
            applied += 1
        else:
            stats["plain"] += 1
    return "\n".join(lines), applied


def fs_safe_title(title):
    """Sanitize a page title for use as a file/folder name.

    Keep case, spaces, and characters the filesystem accepts (they are shown
    verbatim in SiYuan's sidebar); replace only characters that are invalid
    or risky on common filesystems.
    """
    name = unicodedata.normalize("NFC", title)
    name = re.sub(r'[/\\:<>*?"|\x00-\x1f]', " - ", name)

    name = re.sub(r" {2,}", " ", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "untitled"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def strip_dedupe_suffix(stem):
    """Drop Anytype's collision suffixes: trailing _b, _q, and 2_2 style."""
    stem = re.sub(r"_\d+_\d+$", "", stem)
    stem = re.sub(r"_[a-z0-9]$", "", stem)
    return stem


def safe_name(title, used):
    """Filesystem-safe, unique file/folder name derived from a page title."""
    name = fs_safe_title(title)
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN].rstrip(" -.") or "untitled"
    candidate = name
    counter = 1
    while candidate in used:
        suffix = f" {counter}"
        candidate = name[: MAX_NAME_LEN - len(suffix)].rstrip(" -.") + suffix
        counter += 1
    used.add(candidate)
    return candidate


def rewrite_links(body, mapping, rel_of, cur_dir, stats):
    """Rewrite [text](target.md) links into SiYuan wiki-link text [[dest|text]].

    SiYuan's Markdown importer turns wiki-link text into real block
    references that navigate inside the current tab. Markdown link
    destinations cannot express page titles with spaces: angle-quoted
    destinations are percent-encoded by Lute and the importer never
    decodes them, so they never resolve. Wiki-link text is matched
    verbatim against real-space document paths and resolves exactly.

    Unresolvable targets degrade to plain text so that no raw [[...]]
    markers survive in the notes. Heading anchors are dropped (the link
    navigates to the page) because Anytype's anchor slugs do not match
    SiYuan's heading-text anchors.
    """
    def replace(match):
        text, target = match.group(1), match.group(2)
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        safe = mapping.get(Path(target).name)
        if not text or "|" in text or safe is None or safe not in rel_of:
            stats["plain"] += 1
            return text
        dest = posixpath.relpath(rel_of[safe], cur_dir or ".")
        stats["wiki"] += 1
        return f"[[{dest}|{text}]]"
    return LINK_RE.sub(replace, body)


def rewrite_assets(body):
    """Rewrite (files/...) attachment links to (assets/...) for SiYuan import.

    SiYuan's Markdown importer materializes assets/ links by copying the
    referenced file from the import folder into the notebook, so the export's
    files/ directory must be re-labeled and copied next to the pages. Returns
    (new_body, referenced_names). Code fence content is left untouched.
    """
    referenced = []
    lines = body.split("\n")
    inside = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            inside = not inside
            continue
        if inside:
            continue

        def replace(match):
            name = match.group(1)[len("files/"):]
            referenced.append(name)
            return "](assets/" + name + ")"

        lines[i] = ASSET_LINK_RE.sub(replace, line)
    return "\n".join(lines), referenced


def build_parent_map(pages, native_parents=None):
    """parent[name] = parent name; cycle-safe first-backlink choice.

    With a native backup, the real Anytype tree wins: the page's object
    id is looked up in native_parents and kept only if the parent maps
    to an exported page; everything else goes to root. The backlink
    heuristic is only used when no native structure is available.
    """
    oid_to_name = {p["id"]: name for name, p in pages.items() if p["id"]}
    parent = {}
    for name, page in pages.items():
        if name in FORCE_ROOT:
            parent[name] = None  # pinned to root, skip any heuristic
            continue
        chosen = None
        if native_parents is not None and page["id"]:
            poid = native_parents.get(page["id"])
            if poid:
                # reject cyclic ancestors
                cursor, cyclic, steps = poid, False, 0
                while cursor and cursor in oid_to_name and steps < len(pages):
                    if oid_to_name[cursor] == name:
                        cyclic = True
                        break
                    cursor = native_parents.get(cursor)
                    steps += 1
                if not cyclic and poid in oid_to_name:
                    chosen = oid_to_name[poid]
        else:
            for backlink in page["backlinks"]:
                if backlink not in pages or backlink == name:
                    continue
                # Walk the candidate's ancestor chain; reject if it reaches us.
                cursor, cyclic, steps = backlink, False, 0
                while cursor is not None and steps < len(pages):
                    if cursor == name:
                        cyclic = True
                        break
                    cursor = parent.get(cursor)
                    steps += 1
                if not cyclic:
                    chosen = backlink
                    break
        parent[name] = chosen  # None => root
    return parent


def unsorted_dir(page, layouts):
    """Triage folder for an orphan object, based on its native layout.

    Anytype Set-view membership is a dynamic query that the backup does
    not preserve, so unparented objects cannot be placed faithfully;
    they go to Unsorted/, grouped by object type for easy triage.
    """
    layout = layouts.get(page.get("id"))
    if layout == 11:
        return ["Unsorted", "Bookmarks"]
    if layout == 13:
        return ["Unsorted", "Tasks"]
    if layout in (0, 1):
        return ["Unsorted", "Pages"]
    return ["Unsorted", "Misc"]


def main(src_dir, out_dir, native_zip=None):
    src, out = Path(src_dir), Path(out_dir)
    if not src.is_dir():
        sys.exit(f"source directory not found: {src}")

    native_names, native_codes, native_parents, native_layouts = {}, {}, {}, {}
    if native_zip:
        native_names, native_codes, native_parents, native_layouts = \
            load_native_backup(native_zip)
        print(f"Loaded native backup: {len(native_names)} names, "
              f"{len(native_codes)} objects with code blocks, "
              f"{len(native_parents)} tree parent links")

    # Pass 1: read & map.
    pages = {}
    used_names = set()
    total = 0
    for path in sorted(src.glob("*.md")):
        total += 1
        raw = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(raw)
        created = meta.get("Creation date", "")
        if isinstance(created, list):
            created = ""
        obj_id = meta.get("id")
        if not isinstance(obj_id, str):
            obj_id = ""
        title = first_heading(body) or native_names.get(obj_id)
        stem = strip_dedupe_suffix(path.stem)
        fallback = stem if stem else path.stem
        safe = safe_name(title or fallback, used_names)
        source = meta.get("Source")
        if not isinstance(source, str) or not source.startswith(("http://", "https://")):
            source = ""
        pages[path.name] = {
            "safe": safe,
            "id": obj_id,
            "backlinks": [b for b in meta.get("Backlinks", []) if isinstance(b, str)],
            "created": created,
            "title": title,
            "codes": native_codes.get(obj_id, []),
            "file_links": [l for key in ("Links", "Picture")
                           for l in (meta.get(key) if isinstance(meta.get(key), list) else [])
                           if isinstance(l, str) and l.startswith("files/")],
            "source": source,
            "body": body,
        }

    # Pass 2: drop empty bodies, restore code block languages. The
    # file name itself carries the page title (no YAML frontmatter), so
    # SiYuan's importer derives HPath from the same name the rewritten
    # link targets use and resolves every internal link.
    mapping = {name: page["safe"] for name, page in pages.items()}
    skipped = set()
    lang_stats = {"applied": 0, "plain": 0, "mismatch": 0}
    for name, page in pages.items():
        body, applied = restore_code_langs(page["body"], page["codes"], lang_stats)
        lang_stats["applied"] += applied
        # A Source URL counts as content: URL-only bookmark pages are
        # legitimate and must not be skipped as empty.
        if len(re.sub(r"\s", "", body + page["source"])) < MIN_BODY_CHARS:
            skipped.add(name)
            continue
        # The title node (from the file name) replaces the first heading
        # when they are the same.
        if page["title"]:
            body = re.sub(r"^# " + re.escape(page["title"]) + r"\s*\n+",
                          "", body, count=1)
        parts = []
        if page["created"]:
            parts.append(f"*Created: {page['created']}*\n\n")
        if page["source"]:
            parts.append(f"Source: <{page['source']}>\n\n")
        parts.append(body)
        page["final"] = "".join(parts)
    for name in skipped:
        del pages[name]

    # Pass 3: build tree, resolve each surviving page's output-relative
    # path, then rewrite internal links as wiki links. Link rewriting
    # needs the final folder layout, so it runs after skip removal.
    parent = build_parent_map(
        pages, native_parents if native_parents else None)
    children = {}
    oid_names = {p["id"]: name for name, p in pages.items() if p["id"]}
    nearest_live = {}  # page -> nearest surviving native ancestor page
    if native_parents:
        for name, page in pages.items():
            cursor, steps = native_parents.get(page["id"]), 0
            while cursor and cursor in oid_names and steps < len(pages):
                ancestor = oid_names[cursor]
                if ancestor in pages:
                    nearest_live[name] = ancestor
                    break
                cursor = native_parents.get(pages[ancestor]["id"])
                steps += 1
    for name in pages:
        par = parent.get(name)
        if par is not None and par not in pages:
            par = nearest_live.get(name)  # native ancestor above skipped parents
        if par is not None and par not in pages:
            par = None  # no surviving native ancestor -> orphan
        children.setdefault(par, []).append(name)

    rel_of = {}
    def assign_rel(name, dir_parts):
        rel_of[name] = "/".join(dir_parts + [pages[name]["safe"]])
        for child in sorted(children.get(name, [])):
            assign_rel(child, dir_parts + [pages[name]["safe"]])
    unsorted_count = {}
    unsorted_parts = {}
    for root_page in sorted(children.get(None, [])):
        # A parentless object that embeds child pages is a real hub of
        # the Anytype tree (e.g. Personal, Patterns of Attachment): keep
        # it at the root. Only childless orphans are triage candidates.
        if root_page in FORCE_ROOT or children.get(root_page):
            assign_rel(root_page, [])
            continue
        # No recoverable parent (Anytype Set view membership is lost):
        # park the page in a typed Unsorted triage folder.
        dir_parts = unsorted_dir(pages[root_page], native_layouts)
        unsorted_parts[root_page] = dir_parts
        key = "/".join(dir_parts)
        unsorted_count[key] = unsorted_count.get(key, 0) + 1
        assign_rel(root_page, dir_parts)

    link_stats = {"wiki": 0, "plain": 0}
    rel_by_safe = {pages[name]["safe"]: rel_of[name] for name in rel_of}
    files_dir = src / "files"
    asset_stats = {"copied": 0, "missing": 0, "attachments": 0}
    for name, page in pages.items():
        page["final"] = rewrite_links(
            page["final"], mapping, rel_by_safe,
            posixpath.dirname(rel_of[name]), link_stats)
        page["final"], refs = rewrite_assets(page["final"])
        page["asset_refs"] = set(refs)
        # Frontmatter Links entries that the body never links would be lost
        # with the frontmatter; surface them as an Attachments section when
        # the file actually exists in the export's files/ directory.
        extra = [l[len("files/"):].strip() for l in page["file_links"]]
        extra = [l for l in extra
                 if l and l not in refs and (files_dir / l).is_file()]
        if extra:
            page["final"] += "\n\n## Attachments\n\n" + "\n".join(
                f"- [{Path(l).name}](assets/{l})" for l in extra) + "\n"
            asset_stats["attachments"] += len(extra)
            page["asset_refs"].update(extra)

    def write_page(name, directory):
        page = pages[name]
        kids = sorted(children.get(name, []))
        if not kids:
            (directory / f"{page['safe']}.md").write_text(
                page["final"], encoding="utf-8")
            return
        target = directory / page["safe"]
        target.mkdir(parents=True, exist_ok=True)
        (target.parent / f"{page['safe']}.md").write_text(
            page["final"], encoding="utf-8")
        for child in kids:
            write_page(child, target)

    out.mkdir(parents=True, exist_ok=True)
    for root_page in sorted(children.get(None, [])):
        if root_page in unsorted_parts:
            target = out
            for part in unsorted_parts[root_page]:
                target = target / part
            target.mkdir(parents=True, exist_ok=True)
            write_page(root_page, target)
        else:
            write_page(root_page, out)

    # Copy every referenced attachment from the export's files/ directory
    # into an assets/ folder next to the referencing page: the Markdown
    # importer resolves assets/ links relative to each .md file's own
    # directory (kernel/model/import.go joins the dest with currentDir),
    # so a single root-level assets/ dir would not be found by nested docs.
    if files_dir.is_dir():
        done = set()
        for name, page in pages.items():
            page_dir = posixpath.dirname(rel_of[name])
            assets_dir = (out / page_dir / "assets") if page_dir \
                else (out / "assets")
            for ref in sorted(page["asset_refs"]):
                key = (page_dir, ref)
                if key in done:
                    continue
                done.add(key)
                src_file = files_dir / ref
                if not src_file.is_file():
                    src_file = files_dir / unquote(ref)
                if not src_file.is_file():
                    asset_stats["missing"] += 1
                    continue
                dest_file = assets_dir / ref
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest_file)
                asset_stats["copied"] += 1

    # Warnings: generated wiki links that fail to resolve, plus leftover
    # markdown page links whose target file does not exist.
    out_rel = set()
    for path in out.rglob("*.md"):
        out_rel.add(path.relative_to(out).with_suffix("").as_posix())
    warnings = []
    for path in out.rglob("*.md"):
        body = path.read_text(encoding="utf-8")
        cur_dir = path.relative_to(out).with_suffix("").parent.as_posix()
        for match in re.finditer(r"\[\[([^[\]|]+)\|", body):
            dest = match.group(1)
            if dest.startswith((" ", '"', "'", "$")):
                continue  # shell [[ ]] test, not a wiki link
            resolved = posixpath.normpath(posixpath.join(cur_dir, dest))
            if resolved not in out_rel:
                warnings.append(f"{path.name}: broken wiki link -> {dest}")
        for match in LINK_RE.finditer(body):
            target = match.group(2)
            if target.startswith(("http://", "https://")):
                continue  # external URL, not a page link
            warnings.append(f"{path.name}: unresolved markdown link -> {target}")

    print(f"Loaded {total} pages, skipped {len(skipped)} empty files")
    print(f"Built nested tree in {out}")
    print(f"Internal links: {link_stats['wiki']} wiki links, "
          f"{link_stats['plain']} unresolved (kept as plain text)")
    if asset_stats["copied"] or asset_stats["missing"]:
        print(f"Assets: {asset_stats['copied']} copied into assets/, "
              f"{asset_stats['missing']} missing from files/, "
              f"{asset_stats['attachments']} frontmatter-only attachment link(s) recovered")
    if native_zip:
        print(f"Code block languages: {lang_stats['applied']} applied, "
              f"{lang_stats['plain']} plain, {lang_stats['mismatch']} unresolved")
    if unsorted_count:
        print("Unsorted triage (orphaned objects):")
        for key in sorted(unsorted_count):
            print(f"  {key}: {unsorted_count[key]}")
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for w in sorted(set(warnings))[:20]:
            print(f"  {w}")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2],
         sys.argv[3] if len(sys.argv) == 4 else None)
