"""Construtores de painéis usando Discord Components V2."""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

import discord

DEFAULT_ACCENT = 0x5865F2


def valid_url(value: Optional[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_hex(value: Optional[str], default: int = DEFAULT_ACCENT) -> int:
    raw = (value or "").strip().lstrip("#")
    if not raw:
        return default
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
        raise ValueError("Use uma cor no formato #RRGGBB.")
    return int(raw, 16)


def parse_emoji(value: Optional[str]) -> Optional[discord.PartialEmoji | str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("<") and raw.endswith(">"):
        return discord.PartialEmoji.from_str(raw)
    return raw


def chunked(items: list[discord.ui.Item], size: int = 5) -> Iterable[list[discord.ui.Item]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_layout(
    *,
    title: str,
    description: str,
    accent: int = DEFAULT_ACCENT,
    thumbnail: Optional[str] = None,
    banner: Optional[str] = None,
    buttons: Optional[list[discord.ui.Button]] = None,
    footer: Optional[str] = None,
    timeout: Optional[float] = 180,
    buttons_per_row: int = 5,
) -> discord.ui.LayoutView:
    """Cria um painel V2 com container, texto, mídia e botões."""
    view = discord.ui.LayoutView(timeout=timeout)
    content = f"# {title}\n{description}".strip()
    children: list[discord.ui.Item] = []

    if thumbnail:
        children.append(
            discord.ui.Section(
                discord.ui.TextDisplay(content),
                accessory=discord.ui.Thumbnail(thumbnail),
            )
        )
    else:
        children.append(discord.ui.TextDisplay(content))

    if banner:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=banner)
        children.append(gallery)

    if footer:
        children.append(discord.ui.TextDisplay(f"-# {footer}"))

    for row_buttons in chunked(list(buttons or []), max(1, min(buttons_per_row, 5))):
        children.append(discord.ui.ActionRow(*row_buttons))

    view.add_item(discord.ui.Container(*children, accent_color=accent))
    return view


def status_layout(
    title: str,
    description: str,
    *,
    accent: int = DEFAULT_ACCENT,
    thumbnail: Optional[str] = None,
) -> discord.ui.LayoutView:
    return build_layout(
        title=title,
        description=description,
        accent=accent,
        thumbnail=thumbnail,
        timeout=120,
    )
