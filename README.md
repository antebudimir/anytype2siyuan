# Anytype → SiYuan Markdown preprocessor

`anytype_prep.py` converts a flat Anytype Markdown export into a nested
folder tree that SiYuan's "Import - Markdown folder" accepts directly,
reconstructing the page hierarchy, internal links, code-block languages,
and bookmark URLs that the export loses. For other Markdown importers
(Joplin, Obsidian) the hierarchy and asset work applies, but the
`[[...]]` link rewriting targets SiYuan specifically.

## Purpose

Anytype's Markdown export writes every page as a sibling `.md` file in
one flat directory. The page tree exists only in two other places:

- each file's YAML frontmatter (`Links` / `Backlinks` lists), and
- the native backup ZIP's snapshot objects (`link` blocks with
  `style: "Page"` encode real parent-child edges).

Neither SiYuan nor Joplin reconstructs the hierarchy from either source,
so a naive import gives you 1,800 pages in one folder with dead links.

## The two-source trick

The script combines both export artifacts:

1. **Markdown export** (content): page bodies, headings, wiki links,
   `files/` attachments, YAML frontmatter.
2. **Native backup ZIP** (structure): real page tree (`details` +
   snapshot links), page titles, and code-block languages
   (`blocks[].fields.lang`), mapped via object ids in the frontmatter.

With both, you get a faithful tree, correct titles, restored code
languages, and typed triage of orphaned objects. Without the ZIP the
script still works but falls back to a first-backlink parenting
heuristic and loses code languages and typed `Unsorted/` folders —
always pass the ZIP.

## Usage

```bash
python3 anytype_prep.py <md-export-folder> <output-folder> <native-backup.zip>
```

In Anytype: Settings - Backup/Export - export both the Markdown bundle
and the native backup, then unzip the Markdown one. The output folder is
created fresh; import it with SiYuan's "Import - Markdown folder".

## What it preserves

- Page tree as nested folders (SiYuan merges `X.md` + `X/` into one doc)
- Internal `[[...]]` links rewritten to importer-resolvable wiki links
  (SiYuan format — Joplin/Obsidian importers will not resolve these)
- Attachments copied into a per-document `assets/` folder next to each
  referencing page (SiYuan resolves `assets/...` relative to each `.md`
  file's own directory — a root-level `assets/` only works for root docs)
- Code-block languages (from the native backup)
- Creation dates (`*Created: ...*` line)
- Bookmark URLs: the Anytype frontmatter `Source:` value is emitted as a
  `Source: <https://...>` autolink line
- Home page pinned to the tree root (kept under its exported
  `- Home - -` name; rename it in SiYuan after import if you like)

## What it can't preserve

Anytype features that exist only as dynamic queries or relations in the
backend are honestly lost:

- **Set / Collection membership** — these are saved queries, not real
  parent links; members become orphans
- **Relations and tags** (beyond what the body text mentions)
- Orphaned objects are parked in typed `Unsorted/Bookmarks`,
  `Unsorted/Pages`, `Unsorted/Tasks`, `Unsorted/Misc` folders for manual
  triage

## Verifying an output tree

Checks worth running before importing (all paths relative to OUT):

```bash
# every wiki link resolves to an existing doc
python3 - <<'EOF'
import pathlib, posixpath, re
out = pathlib.Path("OUT")
mds = list(out.rglob("*.md"))
rel = {p.relative_to(out).with_suffix("").as_posix() for p in mds}
bad = 0
for p in mds:
    cur = p.relative_to(out).with_suffix("").parent.as_posix()
    for m in re.finditer(r"\[\[([^\[\]|]+)\|", p.read_text()):
        if m.group(1).startswith((" ", '"', "'", "$")): continue
        if posixpath.normpath(posixpath.join(cur, m.group(1))) not in rel:
            bad += 1; print(p.name, "->", m.group(1))
print("wiki links broken:", bad)
EOF

# no leftover flat-export asset links, all asset refs resolve
grep -rl '](files/' --include='*.md' OUT | wc -l      # expect 0
find OUT -name '*.md' | wc -l                          # doc count
```

After import, spot-check a bookmark page (its `Source:` URL should be
clickable) and a PDF/asset link (should open in SiYuan's asset viewer;
importer timestamp-prefixes asset filenames — that is normal).

## Tested with

- Anytype v0.57.3 desktop export (Markdown bundle + native ZIP), 2026-10-23
- SiYuan master (Markdown folder importer, per-directory asset resolution)
- 1,834 exported pages → 1,785 imported docs, 1,226 wiki links 0 broken,
  491 asset refs 0 broken, 1,573 code-block languages restored

The script targets Anytype's current export format; if a future Anytype
release changes the frontmatter keys or backup snapshot shape, the
parsers in `parse_frontmatter` / `load_native_backup` are the place to
look. No third-party dependencies — Python 3.8+ standard library only.

## License

MIT — see [LICENSE](LICENSE).
