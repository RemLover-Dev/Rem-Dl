# Web UI conventions

Applies to every list, dropdown, autosuggest result and chip row in `web/`.

- Sort alphabetically (case-insensitive) unless a code comment explains why
  another order is meaningful.
- Render tag text with `cleanTagDisplay()` for one tag or
  `displayTagList(raw, site)` for queue/history strings. They handle
  underscores, roman numerals, capitalization and site-specific separators.
- Reuse the existing tag pill classes (`.v-tag`, `.g-tag-pill`) and their
  hover/pointer behavior. Do not print raw tag strings or invent a new pill.
- Use the existing theme variables instead of hardcoded colors.
