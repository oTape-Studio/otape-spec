# Changelog

All notable changes to the oTAPE specification. Versions follow the rules of §15: minor versions only add
optional features, and major versions may change meaning.

## 1.0: release candidate (2026-10-29)

First public version, by Jan Osio.

- ZIP container with a `mimetype` first entry, `manifest.json`, one continuous audio file per recorded side
- Cassette types C46, C60, C90, C120 and custom; integer frame timeline at 44 100 or 48 000 Hz
- Per-side record tabs (`intact` / `broken` / `taped_over`) with physical transition rules
- Procedural appearance (shell, label styles, lettering roles) and J-card text
- Artwork slots with fixed physical sizes, keep-out zones, safe areas and layering
- Fold-out booklet panels, a sandboxed HTML booklet profile, Markdown notes
- Track markers, history events, extensions, and preservation of unknown data
- `cassette.rights`: copyright notice, SPDX licence and an optional plain-text licence file (`docs/license.txt`)
- JSON Schema, machine-readable geometry, SVG templates, example manifests and cassettes, validator

Appendix A of the specification lists the changes from the earlier draft.
