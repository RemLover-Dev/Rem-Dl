"""Shared moebooru adapter pieces: konachan/yande fetch pages, sankaku
reuses the query/keep shape with its own API. Moebooru has no comma-OR
in the rating metatag (live-verified) — a single rating goes
server-side, multi-rating is filtered locally via keep()."""

from core.watchers.net import std_fields


def letters_of(ratings):
    return {str(r).split(":")[-1] for r in (ratings or [])
            if str(r).split(":")[-1] in ("s", "q", "e")}


def query(tags, ratings):
    tag = " ".join(str(t) for t in tags if str(t).strip())
    letters = sorted(letters_of(ratings))
    if len(letters) == 1:
        tag = (tag + " rating:" + letters[0]).strip()
    return tag


def keep(w, p):
    letters = letters_of(w.get("ratings") or [])
    return len(letters) <= 1 or p.get("rating") in letters


fields = std_fields
