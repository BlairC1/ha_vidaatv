"""Shared helpers for the Hisense TV integration.

The display-name/source-id handling lives here rather than in each platform so
media_player and remote cannot drift apart: both build their lists and resolve
selections through exactly the same functions.
"""

from __future__ import annotations

from typing import Any


def source_display_name(source: dict[str, Any]) -> str | None:
    """Return the human-readable name for a source entry.

    The TV reports ``displayname`` (e.g. "Onkyo AVR") alongside ``sourcename``
    (e.g. "HDMI3"). We prefer the display name so the list matches what the
    coordinator reports as the current source - Home Assistant only highlights
    the active source when it is a member of the source list.
    """
    if not isinstance(source, dict):
        return None
    return (
        source.get("displayname")
        or source.get("sourcename")
        or source.get("name")
        or (f"Source {source['sourceid']}" if source.get("sourceid") else None)
    )


def app_display_name(app: dict[str, Any]) -> str | None:
    """Return the human-readable name for an app entry."""
    if not isinstance(app, dict):
        return None
    return app.get("name")


def build_source_list(
    sources: list[dict[str, Any]] | None,
    apps: list[dict[str, Any]] | None,
) -> list[str]:
    """Build the combined, de-duplicated input + app list."""
    names: list[str] = []
    for src in sources or []:
        name = source_display_name(src)
        if name and name not in names:
            names.append(name)
    for app in apps or []:
        name = app_display_name(app)
        if name and name not in names:
            names.append(name)
    return names


def _norm(value: str | None) -> str:
    """Normalise a name for tolerant matching."""
    return (value or "").strip().casefold()


def resolve_source_id(
    name: str,
    sources: list[dict[str, Any]] | None,
) -> str | None:
    """Map a display name back to the TV's own source id.

    Lists hold display names ("Onkyo AVR") but the TV expects its source id
    ("HDMI3"), and this firmware silently ignores the numeric ids pyvidaa's
    SOURCE_MAP produces. Inputs whose display name equals their id (HDMI2,
    HDMI4) worked without this; named ones did not.

    Returns None when the name is not a known input (i.e. it is an app).
    """
    wanted = _norm(name)
    for src in sources or []:
        if not isinstance(src, dict):
            continue
        candidates = (
            src.get("displayname"),
            src.get("sourcename"),
            src.get("name"),
        )
        if wanted in tuple(_norm(c) for c in candidates):
            return src.get("sourceid") or src.get("sourcename") or name
    return None


def find_app(
    name: str,
    apps: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return the app entry matching ``name``, if any."""
    wanted = _norm(name)
    for app in apps or []:
        if isinstance(app, dict) and _norm(app.get("name")) == wanted:
            return app
    return None


def app_icon_url(app: dict[str, Any] | None) -> str | None:
    """Extract a usable icon URL from an app entry.

    ``httpIcon`` arrives in an odd concatenated form, e.g.
    ``data:image/png;base64,iconDownloadhttps://img.vidaahub.com/...jpg``
    - a data-URI prefix, the marker ``iconDownload``, then the real URL. Pull
    the http(s) URL out when present.
    """
    if not isinstance(app, dict):
        return None
    raw = app.get("httpIcon") or ""
    if not isinstance(raw, str):
        return None
    idx = raw.find("http")
    if idx >= 0:
        url = raw[idx:].strip()
        return url or None
    return None
