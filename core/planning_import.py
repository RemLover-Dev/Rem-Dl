"""Bulk-import normalizer for the Download Planning List.

Parses free-form pasted text (Telegram exports, chat logs) into preview
rows. Nothing here writes anything — the import route stores only the
rows the user confirms in the preview.

Site names were chosen so most aliases are the site name with some
letters missing (dan -> danbooru, kona -> Konachan, gel -> Gelbooru,
yande -> yande.re). Exact aliases match first; anything else falls back
to prefix matching against every known name, and prefix collisions are
reported as candidates instead of silently picking one.
"""
import re

# canonical -> aliases (lowercase). Canonicals themselves always count
# as exact names, so "anime_dl" typed as an id resolves directly.
SITE_ALIASES = {
    "dan": ["danbooru", "dan"],
    "gelbooru": ["gelbooru", "gel"],
    "gsbooru": ["gsbooru", "gs"],
    "kona": ["konachan", "kona"],
    "yande": ["yande.re", "yandere", "yande"],
    "safe": ["safebooru", "safe"],
    "sankaku": ["sankaku", "sanka"],
    "rule34": ["rule34", "r34"],
    "zero": ["zerochan", "zero"],
    "anime_dl": ["anime-pictures", "animepictures", "anime", "anime_dl"],
    "pixiv": ["pixiv", "pix"],
    "pinterest": ["pinterest", "pin"],
    "eshuushuu": ["e-shuushuu", "eshuushuu", "shuushuu", "esh"],
    "nekosapi": ["nekosapi"],
    "nekosia": ["nekosia"],
    "neko": ["nekos.best", "nekobest", "neko"],
    "nekos_life": ["nekos.life", "nekoslife"],
    "x": ["twitter", "x"],
}

SITE_ORDER = sorted(SITE_ALIASES)  # alphabetical — dropdowns list it as-is

DEFAULT_SITE = "gelbooru"
NO_SEARCH_SITES = {"neko", "nekos_life", "nekosia", "nekosapi"}

# rating metatag hosts — a pasted "rating:x" token promotes into the
# entry's rating only for these; everywhere else it is an ordinary tag
RATING_SITES = {"dan", "gelbooru", "gsbooru", "kona", "yande", "sankaku"}
ALLOWED_RATINGS = {
    "dan": set("gseq"), "gelbooru": set("gseq"),
    "gsbooru": set("gsq"),
    "kona": set("sqe"), "yande": set("sqe"), "sankaku": set("sqe"),
}

_TOKEN_TRIM = ";:.,!?*\"'`()[]{}"
_TIME_LEAD = re.compile(
    r"^(?:\[?\d{1,2}:\d{2}(?::\d{2})?[,]?\s+\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\]?"
    r"|\[?\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\]?"
    r"|\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?"
    r"|\[\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?\]"
    r"|\[?\d{1,2}:\d{2}(?::\d{2})?\]?"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)"
    r")\s*(?:utc|gmt)?(?:[+-]\d{1,2}(?::\d{2})?)?\s*", re.IGNORECASE)
# a plain "-" is NOT a bullet — it introduces exclusion tags (-video etc.)
_BULLET_LEAD = re.compile(r"^(?:[\u2013\u2014\u2022*\u25aa>\u00bb]+\s+)+")
_KICKER = re.compile(r"^([^\W\d_][\w .\-]{0,31}?):\s+(\S.*)$", re.UNICODE)
_SYMBOL_TOKEN = re.compile(r"^[\W_]+$", re.UNICODE)
_URL = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN = re.compile(
    r"^[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|re|life|best|app|co|jp|io|me|ru|xxx|to|id)\b.*$",
    re.IGNORECASE)
_RATING_TOKEN = re.compile(r"^rating:([gsqe])$", re.IGNORECASE)
_PURE_DIGITS = re.compile(r"^#?\d{4,10}$")


def normalize_site_term(term):
    """Lowercase + strip outer punctuation so 'Dan:' / '[gelbooru]' / 'kona.' resolve."""
    return str(term or "").strip().lower().strip(_TOKEN_TRIM).strip()


def resolve_site(term):
    """One token -> (site|None, candidates|None)."""
    term = normalize_site_term(term)
    if not term:
        return (None, None)
    exact = [c for c in SITE_ORDER if term == c or term in SITE_ALIASES[c]]
    if exact:
        return (exact[0], None)
    hits = [c for c in SITE_ORDER
            if any(name.startswith(term) for name in [c] + SITE_ALIASES[c])]
    if len(hits) == 1:
        return (hits[0], None)
    if not hits:
        return (None, None)
    return (None, sorted(hits))


def _clean_line(raw):
    """Strip chat noise: bullets, leading timestamps, sender kickers."""
    line = raw.strip()
    if not line:
        return ""
    line = _BULLET_LEAD.sub("", line).strip()
    line = _TIME_LEAD.sub("", line, count=1).strip()
    # "(rem dan)" / "[ahoge blue_hair]" — whole-line bracket peel
    opener = line[0] if len(line) > 1 else ""
    closers = {"(": ")", "[": "]", "{": "}"}
    if opener in closers and line[-1] == closers[opener]:
        line = line[1:-1].strip()
    tokens = line.split()
    if len(tokens) == 1 and tokens[0].endswith(":"):
        # bare sender-name line ('Hanekawa:') — pure Telegram noise unless it
        # resolves to a site label ('Dan:')
        name = tokens[0].rstrip(": \"'")
        if name.isalpha():
            site, cand = resolve_site(name)
            if site is None and not cand:
                return ""
    if len(tokens) >= 2:
        kick = _KICKER.match(line)
        if kick:
            site, _cand = resolve_site(kick.group(1))
            # a site kicker ("Dan: rem blue_hair") stays for the site-first
            # rule; only a chat sender name is real noise
            if site is None and kick.group(1)[0].isupper():
                remainder = kick.group(2).strip()
                if remainder:
                    return remainder
                return ""  # '[10:34] Hanekawa: ' — sender line with no message
    return line


def _classify(tokens, ctx_site):
    """Tokens -> (site_tokens, tags, rating, note, warnings).

    Lines may be 'site tags', 'tags site', all-site (label line → sites
    with no tags), or plain tags under the site context already known."""
    r = [(t, resolve_site(t)[0]) for t in tokens]
    sites, body = [], tokens
    if len(r) >= 2 and r[0][1] and not r[1][1]:
        sites, body = [r[0][1]], tokens[1:]
    elif len(r) >= 2 and r[-1][1] and not r[-2][1]:
        sites, body = [r[-1][1]], tokens[:-1]
    elif all(s for _, s in r):
        sites, body = [s for _, s in r], []
    tags, rating, note, warns = [], "", "", []
    dash = False
    for t in body:
        if _URL.match(t) or _DOMAIN.match(t):
            # source links are useful as a note, not as a tag
            note = (note + " " + t).strip()
            continue
        st = t.strip('"`')
        if st == "-":
            # leading bare dash = Telegram exclusion marker: attaches to
            # the tokens after it ('- video' -> tags '-video')
            dash = True
            warns.append("treated the stray dash as an exclusion marker")
            continue
        if dash and not _SYMBOL_TOKEN.match(st):
            st = "-" + st  # sticky: every remaining token becomes excluded
        elif _SYMBOL_TOKEN.match(st):
            continue  # >>, ---, ... — transient formatting noise
        elif st.startswith("-") and len(st) > 1:
            warns.append(f"exclusion '{st}' — kept as a '-' tag token")
        site, _cand = resolve_site(st)
        if site and not sites and not tags:
            # a second site name on the line ('gel dan rem') is noise
            warns.append(f"token '{st}' looks like a site name but the site placement is unclear")
        rt = _RATING_TOKEN.match(st.lower())
        if rt and (sites and sites[0] or ctx_site) in RATING_SITES:
            host = sites[0] if sites else ctx_site
            code = rt.group(1).lower()
            if code not in ALLOWED_RATINGS[host]:
                warns.append(f"rating '{code}' is not supported by {host}")
            elif not rating:
                rating = f"rating:{code}"
            continue
        if _PURE_DIGITS.match(st):
            if (sites and sites[0] or ctx_site) == "pixiv":
                tags.append(f"artworks:{st.lstrip('#')}")
                continue
            warns.append(f"bare number '{st.lstrip('#')}' is only auto-detected for Pixiv feeds")
        tags.append(st)
    return sites, tags, rating, note, warns


def _row(source, site, tags, rating, note, warnings, existing_keys, seen):
    tags_str = " ".join(tags)
    key = (site, tags_str.lower(), rating)
    row = {
        "source": source, "site": site, "tags": tags_str, "rating": rating,
        "note": note, "warnings": list(warnings),
        "duplicate": key in seen, "exists": (site, tags_str.lower()) in existing_keys,
        "candidates": None,
    }
    seen.add(key)
    return row


def normalize_bulk_text(text, existing=None):
    """Parse pasted text into preview rows (see module docstring).

    One tag line = one entry. A tag line with no site yet stays pending
    until the next site label ('taya_oco' then 'dan'); further site-less
    tag lines merge into the same pending entry; anything still pending
    at the end defaults to Gelbooru with a warning — nothing is silently
    discarded.
    """
    existing = existing or []
    existing_keys = {
        ((p.get("site") or "").strip().lower(),
         (p.get("tags") or "").strip().lower())
        for p in existing if isinstance(p, dict)
    }
    seen = set()
    rows = []
    current_site = None
    current_rating = ""  # a rating token carried by a label line ('Dan: rating:s')
    rating_site = None  # the site that label applies to
    label_extra = {"warnings": [], "note": ""}  # label-line warns/notes ride on the next row

    def apply_extra(warnings, note):
        base = list(label_extra["warnings"]) + list(warnings)
        merged = (label_extra["note"] + " " + note).strip()
        label_extra["warnings"].clear()
        label_extra["note"] = ""
        return base, merged

    pending = None  # {"tags": [...], "sources": [..], "rating", "note", "warnings"}

    def flush(default_site=True):
        nonlocal pending
        if pending is None:
            return
        site = pending["site"] or DEFAULT_SITE
        base, note = apply_extra(pending["warnings"], pending["note"])
        if default_site and not pending["site"]:
            base.append(f"no site label found — defaulted to {DEFAULT_SITE}")
        rows.append(_row("\n".join(pending["sources"]), site,
                         pending["tags"], pending["rating"], note,
                         base, existing_keys, seen))
        pending = None

    for raw in str(text or "").splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        tokens = line.split()
        if len(tokens) == 1:
            # single ambiguous token ('sh' -> GSbooru/Safe/…) — surface the
            # candidates instead of guessing
            _site, cand = resolve_site(tokens[0])
            if cand:
                rows.append({
                    "source": line, "site": None, "tags": "", "rating": "",
                    "note": "", "warnings": [f"'{normalize_site_term(tokens[0])}' matches several sites — pick one below"],
                    "duplicate": False, "exists": False, "candidates": cand,
                })
                continue
        sites, tags, rating, note, warns = _classify(tokens, current_site)
        if tags and not rating and current_rating and rating_site == (sites[0] if sites else current_site or ""):
            # the rating label ('Dan: rating:s') only applies to tag lines of
            # its own site, and only when the code is allowed there
            code = current_rating.split(":", 1)[-1]
            host = sites[0] if sites else current_site
            if host in ALLOWED_RATINGS and code in ALLOWED_RATINGS[host]:
                rating = current_rating

        if not tags:
            if not sites:
                if note or warns or rating:
                    skim = warns or []
                    if rating:
                        skim.append(f"rating token dropped with the tags ('{rating}')")
                    rows.append({
                        "source": line, "site": None, "tags": "", "rating": "",
                        "note": note,
                        "warnings": skim or ["could not confidently parse this line"],
                        "duplicate": False, "exists": False, "candidates": None,
                    })
                continue
            # label line: the label closes a pending entry AND becomes the
            # context for whatever follows; a rating token on the label
            # ('Dan: rating:s') is the default rating for those entries
            if pending is not None:
                pending["site"] = sites[-1]
                flush(default_site=False)
            elif len(sites) > 1:
                pass  # 'Dan Danbooru' — repeated labels on one line, last wins
            current_site = sites[-1]
            current_rating = rating
            rating_site = sites[-1]
            label_extra = {"warnings": list(warns), "note": note}
            continue

        def attach_exclusion_to_previous():
            """'X tag' then '-Y tag' belong to ONE entry — an exclusion-only
            line merges into the previous row instead of becoming its own."""
            if not tags or not all(t.startswith("-") for t in tags):
                return False
            prev = rows[-1] if rows else None
            host = sites[0] if sites else current_site
            if prev is None or not prev.get("tags"):
                return False
            if host and host != prev.get("site"):
                return False  # exclusion points at a different site element
            old_key = (prev["site"], prev["tags"].lower(), prev["rating"] or "")
            merged = (prev["tags"] + " " + " ".join(tags)).strip()
            new_key = (prev["site"], merged.lower(), prev["rating"] or "")
            prev["tags"] = merged
            prev["duplicate"] = bool(prev["duplicate"]) or new_key in seen
            prev["exists"] = (prev["site"], merged.lower()) in existing_keys
            prev["warnings"] = list(prev["warnings"]) + [
                w for w in warns if w.startswith("exclusion")] + [
                "exclusion line attached to the previous entry"]
            seen.discard(old_key)
            seen.add(new_key)
            return True

        if sites:
            if attach_exclusion_to_previous():
                continue
            flush()
            if len(sites) > 1:
                warns.append("multiple site labels on one line — using the last one")
            current_site = sites[-1]
            base, note = apply_extra(warns, note)
            rows.append(_row(line, current_site, tags, rating, note, base,
                             existing_keys, seen))
            continue

        if current_site:
            if attach_exclusion_to_previous():
                continue
            flush()
            base, note = apply_extra(warns, note)
            rows.append(_row(line, current_site, tags, rating, note, base,
                             existing_keys, seen))
            continue

        # no site anywhere yet — collect into the pending entry
        if pending is None:
            pending = {"site": None, "tags": [], "sources": [], "rating": "",
                       "note": "", "warnings": []}
        pending["tags"].extend(tags)
        pending["sources"].append(line)
        pending["rating"] = pending["rating"] or rating
        pending["note"] = (pending["note"] + " " + note).strip()
        pending["warnings"].extend(warns)

    flush()
    # label lines at the top ('Dan:' with entries underneath) already
    # produced their rows; unresolved labels add nothing
    return rows


def flag_duplicates(rows):
    """Back-compat helper: recompute duplicate/exists flags in place."""
    seen = set()
    for row in rows:
        key = (row.get("site"), (row.get("tags") or "").lower(),
               row.get("rating") or "")
        row["duplicate"] = key in seen
        seen.add(key)
    return rows
