"""Construtor administrativo de painéis informativos com Components V2.

Os administradores criam um painel principal e adicionam botões. Cada botão
abre, de forma privada, uma página de conteúdo com título, texto, miniatura e
banner próprios.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store
from utils.ui_v2 import DEFAULT_ACCENT, build_layout, parse_emoji, parse_hex, status_layout, valid_url

log = logging.getLogger("cogs.painel_interativo")

NAMESPACE = "interactive_panels"
MAX_PAGES = 20

STYLE_MAP: dict[str, discord.ButtonStyle] = {
    "primario": discord.ButtonStyle.primary,
    "secundario": discord.ButtonStyle.secondary,
    "sucesso": discord.ButtonStyle.success,
    "perigo": discord.ButtonStyle.danger,
}

STYLE_CHOICES = [
    app_commands.Choice(name="Primário (azul)", value="primario"),
    app_commands.Choice(name="Secundário (cinza)", value="secundario"),
    app_commands.Choice(name="Sucesso (verde)", value="sucesso"),
    app_commands.Choice(name="Perigo (vermelho)", value="perigo"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def _clean_optional_media(value: Optional[str], current: Optional[str]) -> Optional[str]:
    if value is None:
        return current
    raw = value.strip()
    if raw.lower() in {"remover", "nenhum", "none", "-"}:
        return None
    return raw or None


async def _get_panel(guild_id: int, panel_id: str) -> Optional[dict[str, Any]]:
    panel = await store.get(guild_id, NAMESPACE, panel_id.lower(), None)
    return panel if isinstance(panel, dict) else None


async def _save_panel(guild_id: int, panel: dict[str, Any]) -> None:
    panel["updated_at"] = _now()
    await store.set(guild_id, NAMESPACE, panel["id"], panel)


class PanelPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"infopanel:(?P<panel>[0-9a-f]{8}):(?P<page>[0-9a-f]{6})",
):
    def __init__(
        self,
        panel_id: str,
        page_id: str,
        *,
        label: str = "Abrir",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
    ) -> None:
        self.panel_id = panel_id
        self.page_id = page_id
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                emoji=parse_emoji(emoji),
                custom_id=f"infopanel:{panel_id}:{page_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match,
        /,
    ) -> "PanelPageButton":
        button = item if isinstance(item, discord.ui.Button) else None
        return cls(
            match["panel"],
            match["page"],
            label=button.label if button and button.label else "Abrir",
            style=button.style if button else discord.ButtonStyle.secondary,
            emoji=str(button.emoji) if button and button.emoji else None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Este botão só funciona em servidores.", ephemeral=True)
            return
        panel = await _get_panel(interaction.guild.id, self.panel_id)
        if not panel:
            await interaction.response.send_message(
                view=status_layout("Painel indisponível", "Este painel foi removido ou não existe mais."),
                ephemeral=True,
            )
            return
        pages = panel.get("pages", [])
        page = next(
            (
                value
                for value in pages
                if isinstance(value, dict) and value.get("id") == self.page_id
            ),
            None,
        ) if isinstance(pages, list) else None
        if not page:
            await interaction.response.send_message(
                view=status_layout("Página indisponível", "Esta página foi removida do painel."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=build_page_view(panel, page),
            ephemeral=True,
        )


def build_public_panel(panel: dict[str, Any]) -> discord.ui.LayoutView:
    buttons: list[discord.ui.Button] = []
    panel_id = panel["id"]
    for index, page in enumerate(panel.get("pages", [])):
        style = STYLE_MAP.get(str(page.get("style", "secundario")), discord.ButtonStyle.secondary)
        page_id = str(page.get("id") or f"{index:06x}")
        buttons.append(
            discord.ui.Button(
                label=str(page.get("label", f"Página {index + 1}"))[:80],
                style=style,
                emoji=parse_emoji(page.get("emoji")),
                custom_id=f"infopanel:{panel_id}:{page_id}",
            )
        )
    return build_layout(
        title=str(panel.get("title", "Informações")),
        description=str(panel.get("description", "Selecione uma opção abaixo.")),
        accent=int(panel.get("accent", DEFAULT_ACCENT)),
        thumbnail=panel.get("thumbnail"),
        banner=panel.get("banner"),
        buttons=buttons,
        footer=panel.get("footer") or "Selecione uma opção para consultar os detalhes",
        timeout=None,
        buttons_per_row=5,
    )


def build_page_view(panel: dict[str, Any], page: dict[str, Any]) -> discord.ui.LayoutView:
    return build_layout(
        title=str(page.get("title", page.get("label", "Informações"))),
        description=str(page.get("content", "Sem conteúdo.")),
        accent=int(page.get("accent") or panel.get("accent", DEFAULT_ACCENT)),
        thumbnail=page.get("thumbnail"),
        banner=page.get("banner"),
        footer=str(page.get("footer") or panel.get("title", "Painel informativo")),
        timeout=300,
    )


class CreatePanelModal(discord.ui.Modal, title="Criar painel interativo"):
    panel_title = discord.ui.TextInput(
        label="Título principal",
        placeholder="Ex.: Informações sobre as aulas",
        max_length=200,
    )
    description = discord.ui.TextInput(
        label="Descrição principal",
        placeholder="Explique o objetivo do painel.",
        style=discord.TextStyle.paragraph,
        max_length=3500,
    )
    accent = discord.ui.TextInput(
        label="Cor lateral (#RRGGBB)",
        placeholder="#5865F2",
        required=False,
        max_length=7,
    )
    thumbnail = discord.ui.TextInput(
        label="URL da miniatura (opcional)",
        required=False,
        max_length=500,
    )
    banner = discord.ui.TextInput(
        label="URL do banner (opcional)",
        required=False,
        max_length=500,
    )

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__(timeout=600)
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _is_admin(interaction):
            await interaction.response.send_message("Somente administradores podem criar painéis.", ephemeral=True)
            return
        thumbnail = str(self.thumbnail.value).strip() or None
        banner = str(self.banner.value).strip() or None
        if not valid_url(thumbnail) or not valid_url(banner):
            await interaction.response.send_message("Miniatura e banner devem usar URLs http/https.", ephemeral=True)
            return
        try:
            accent = parse_hex(str(self.accent.value), DEFAULT_ACCENT)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        panel_id = secrets.token_hex(4)
        panel = {
            "id": panel_id,
            "owner_id": interaction.user.id,
            "channel_id": self.channel.id,
            "title": str(self.panel_title.value).strip(),
            "description": str(self.description.value).strip(),
            "accent": accent,
            "thumbnail": thumbnail,
            "banner": banner,
            "footer": "Selecione uma opção para consultar os detalhes",
            "pages": [],
            "created_at": _now(),
            "updated_at": _now(),
            "published": [],
        }
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=PanelBuilderView(panel_id, interaction.user.id),
            ephemeral=True,
        )


class AddPageModal(discord.ui.Modal, title="Adicionar botão ao painel"):
    label_input = discord.ui.TextInput(
        label="Nome do botão",
        placeholder="Ex.: Diretrizes",
        max_length=80,
    )
    emoji_input = discord.ui.TextInput(
        label="Emoji (opcional)",
        placeholder="Ex.: 📘 ou <:emoji:ID>",
        required=False,
        max_length=100,
    )
    style_input = discord.ui.TextInput(
        label="Estilo do botão",
        placeholder="primario, secundario, sucesso ou perigo",
        default="secundario",
        max_length=12,
    )
    title_input = discord.ui.TextInput(
        label="Título da página",
        placeholder="Ex.: Diretrizes das aulas",
        max_length=200,
    )
    content_input = discord.ui.TextInput(
        label="Conteúdo da página",
        placeholder="Escreva as informações que aparecerão ao clicar.",
        style=discord.TextStyle.paragraph,
        max_length=3500,
    )

    def __init__(self, panel_id: str, author_id: int) -> None:
        super().__init__(timeout=600)
        self.panel_id = panel_id
        self.author_id = author_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id or not interaction.guild or not _is_admin(interaction):
            await interaction.response.send_message("Você não pode editar este painel.", ephemeral=True)
            return
        panel = await _get_panel(interaction.guild.id, self.panel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        pages = panel.setdefault("pages", [])
        if len(pages) >= MAX_PAGES:
            await interaction.response.send_message(
                f"Este painel já atingiu o limite de {MAX_PAGES} botões.",
                ephemeral=True,
            )
            return
        style = str(self.style_input.value).strip().lower().replace("á", "a").replace("í", "i")
        aliases = {
            "primary": "primario",
            "primário": "primario",
            "secondary": "secundario",
            "secundário": "secundario",
            "success": "sucesso",
            "danger": "perigo",
        }
        style = aliases.get(style, style)
        if style not in STYLE_MAP:
            await interaction.response.send_message(
                "Estilo inválido. Use primario, secundario, sucesso ou perigo.",
                ephemeral=True,
            )
            return
        pages.append(
            {
                "id": secrets.token_hex(3),
                "label": str(self.label_input.value).strip(),
                "emoji": str(self.emoji_input.value).strip() or None,
                "style": style,
                "title": str(self.title_input.value).strip(),
                "content": str(self.content_input.value).strip(),
                "accent": panel.get("accent", DEFAULT_ACCENT),
                "thumbnail": None,
                "banner": None,
                "footer": None,
            }
        )
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=PanelBuilderView(self.panel_id, self.author_id, notice="Botão adicionado com sucesso."),
            ephemeral=True,
        )


class BuilderActionButton(discord.ui.Button):
    def __init__(self, action: str, *, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, PanelBuilderView):
            await interaction.response.send_message("Editor indisponível.", ephemeral=True)
            return
        if interaction.user.id != view.author_id or not _is_admin(interaction):
            await interaction.response.send_message("Somente quem abriu o editor pode usá-lo.", ephemeral=True)
            return
        cog = interaction.client.get_cog("PainelInterativo")
        if not isinstance(cog, PainelInterativo):
            await interaction.response.send_message("Editor indisponível.", ephemeral=True)
            return

        if self.action == "add":
            await interaction.response.send_modal(AddPageModal(view.panel_id, view.author_id))
        elif self.action == "preview":
            await cog.preview_panel(interaction, view.panel_id)
        elif self.action == "publish":
            await cog.publish_saved_panel(interaction, view.panel_id, None)
        elif self.action == "help":
            await interaction.response.send_message(
                view=status_layout(
                    "Comandos avançados do painel",
                    "Use `/painel editar_principal`, `/painel editar_botao`, `/painel adicionar_botao`, "
                    "`/painel remover_botao` e `/painel publicar` para controlar todos os detalhes, "
                    "incluindo banner e miniatura de cada página.",
                ),
                ephemeral=True,
            )


class PanelBuilderView(discord.ui.LayoutView):
    def __init__(self, panel_id: str, author_id: int, notice: Optional[str] = None) -> None:
        super().__init__(timeout=600)
        self.panel_id = panel_id
        self.author_id = author_id
        description = (
            f"**ID do painel:** `{panel_id}`\n\n"
            "Adicione os botões, visualize o resultado e publique quando estiver pronto. "
            "Guarde o ID para editar o painel depois."
        )
        if notice:
            description = f"**{notice}**\n\n" + description
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Editor de painel\n{description}"),
                discord.ui.ActionRow(
                    BuilderActionButton("add", label="Adicionar botão", style=discord.ButtonStyle.primary),
                    BuilderActionButton("preview", label="Visualizar", style=discord.ButtonStyle.secondary),
                    BuilderActionButton("publish", label="Publicar", style=discord.ButtonStyle.success),
                    BuilderActionButton("help", label="Comandos avançados", style=discord.ButtonStyle.secondary),
                ),
                accent_color=DEFAULT_ACCENT,
            )
        )


class PainelInterativo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PanelPageButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(PanelPageButton)

    async def preview_panel(self, interaction: discord.Interaction, panel_id: str) -> None:
        assert interaction.guild is not None
        panel = await _get_panel(interaction.guild.id, panel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        await interaction.response.send_message(view=build_public_panel(panel), ephemeral=True)

    async def publish_saved_panel(
        self,
        interaction: discord.Interaction,
        panel_id: str,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        panel = await _get_panel(interaction.guild.id, panel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        if not panel.get("pages"):
            await interaction.response.send_message(
                view=status_layout("Painel vazio", "Adicione ao menos um botão antes de publicar."),
                ephemeral=True,
            )
            return
        target = channel
        if target is None:
            saved_channel = interaction.guild.get_channel(int(panel.get("channel_id", 0) or 0))
            target = saved_channel if isinstance(saved_channel, discord.TextChannel) else None
        if target is None and isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Escolha um canal de texto válido.", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            message = await target.send(view=build_public_panel(panel))
        except discord.HTTPException:
            await interaction.followup.send(
                view=status_layout("Falha ao publicar", "Verifique as permissões do bot no canal."),
                ephemeral=True,
            )
            return
        panel["channel_id"] = target.id
        published = panel.setdefault("published", [])
        published.append({"channel_id": target.id, "message_id": message.id, "published_at": _now()})
        panel["published"] = published[-20:]
        await _save_panel(interaction.guild.id, panel)
        await interaction.followup.send(
            view=status_layout("Painel publicado", f"O painel `{panel_id}` foi enviado para {target.mention}."),
            ephemeral=True,
        )

    painel_group = app_commands.Group(
        name="painel",
        description="Criação de painéis informativos interativos",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @painel_group.command(name="criar", description="Abre o editor de um novo painel informativo")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def create_panel(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
    ) -> None:
        channel = canal or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Escolha um canal de texto.", ephemeral=True)
            return
        await interaction.response.send_modal(CreatePanelModal(channel))

    @painel_group.command(name="adicionar_botao", description="Adiciona um botão e uma página a um painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(estilo=STYLE_CHOICES)
    async def add_button(
        self,
        interaction: discord.Interaction,
        painel_id: str,
        nome_botao: str,
        titulo_pagina: str,
        conteudo: str,
        estilo: app_commands.Choice[str],
        emoji: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        if not valid_url(miniatura) or not valid_url(banner):
            await interaction.response.send_message("Miniatura e banner devem usar URLs http/https.", ephemeral=True)
            return
        panel = await _get_panel(interaction.guild.id, painel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        pages = panel.setdefault("pages", [])
        if len(pages) >= MAX_PAGES:
            await interaction.response.send_message(f"Limite de {MAX_PAGES} botões atingido.", ephemeral=True)
            return
        pages.append(
            {
                "id": secrets.token_hex(3),
                "label": nome_botao[:80],
                "emoji": emoji.strip() if emoji else None,
                "style": estilo.value,
                "title": titulo_pagina[:200],
                "content": conteudo[:3500],
                "accent": panel.get("accent", DEFAULT_ACCENT),
                "thumbnail": miniatura.strip() if miniatura else None,
                "banner": banner.strip() if banner else None,
                "footer": None,
            }
        )
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=status_layout(
                "Botão adicionado",
                f"O botão **{nome_botao[:80]}** foi adicionado como página `{len(pages)}` do painel `{painel_id}`.",
            ),
            ephemeral=True,
        )

    @painel_group.command(name="editar_principal", description="Edita o conteúdo principal de um painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def edit_main(
        self,
        interaction: discord.Interaction,
        painel_id: str,
        titulo: Optional[str] = None,
        descricao: Optional[str] = None,
        cor: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
        rodape: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        panel = await _get_panel(interaction.guild.id, painel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        new_thumbnail = _clean_optional_media(miniatura, panel.get("thumbnail"))
        new_banner = _clean_optional_media(banner, panel.get("banner"))
        if not valid_url(new_thumbnail) or not valid_url(new_banner):
            await interaction.response.send_message("Miniatura e banner devem usar URLs http/https.", ephemeral=True)
            return
        if cor is not None:
            try:
                panel["accent"] = parse_hex(cor, int(panel.get("accent", DEFAULT_ACCENT)))
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        if titulo is not None:
            panel["title"] = titulo[:200]
        if descricao is not None:
            panel["description"] = descricao[:3500]
        if rodape is not None:
            panel["footer"] = None if rodape.lower() in {"remover", "nenhum", "-"} else rodape[:500]
        panel["thumbnail"] = new_thumbnail
        panel["banner"] = new_banner
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=status_layout("Painel atualizado", f"As informações principais de `{painel_id}` foram salvas."),
            ephemeral=True,
        )

    @painel_group.command(name="editar_botao", description="Edita um botão e a página que ele abre")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(estilo=STYLE_CHOICES)
    async def edit_button(
        self,
        interaction: discord.Interaction,
        painel_id: str,
        numero: app_commands.Range[int, 1, MAX_PAGES],
        nome_botao: Optional[str] = None,
        titulo_pagina: Optional[str] = None,
        conteudo: Optional[str] = None,
        estilo: Optional[app_commands.Choice[str]] = None,
        emoji: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        panel = await _get_panel(interaction.guild.id, painel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        pages = panel.get("pages", [])
        index = int(numero) - 1
        if not isinstance(pages, list) or index >= len(pages):
            await interaction.response.send_message("Esse número de botão não existe.", ephemeral=True)
            return
        page = pages[index]
        new_thumbnail = _clean_optional_media(miniatura, page.get("thumbnail"))
        new_banner = _clean_optional_media(banner, page.get("banner"))
        if not valid_url(new_thumbnail) or not valid_url(new_banner):
            await interaction.response.send_message("Miniatura e banner devem usar URLs http/https.", ephemeral=True)
            return
        if nome_botao is not None:
            page["label"] = nome_botao[:80]
        if titulo_pagina is not None:
            page["title"] = titulo_pagina[:200]
        if conteudo is not None:
            page["content"] = conteudo[:3500]
        if estilo is not None:
            page["style"] = estilo.value
        if emoji is not None:
            page["emoji"] = None if emoji.lower() in {"remover", "nenhum", "-"} else emoji.strip()
        page["thumbnail"] = new_thumbnail
        page["banner"] = new_banner
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=status_layout("Botão atualizado", f"A página `{numero}` do painel `{painel_id}` foi salva."),
            ephemeral=True,
        )

    @painel_group.command(name="remover_botao", description="Remove um botão de um painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_button(
        self,
        interaction: discord.Interaction,
        painel_id: str,
        numero: app_commands.Range[int, 1, MAX_PAGES],
    ) -> None:
        assert interaction.guild is not None
        panel = await _get_panel(interaction.guild.id, painel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        pages = panel.get("pages", [])
        index = int(numero) - 1
        if not isinstance(pages, list) or index >= len(pages):
            await interaction.response.send_message("Esse número de botão não existe.", ephemeral=True)
            return
        removed = pages.pop(index)
        await _save_panel(interaction.guild.id, panel)
        await interaction.response.send_message(
            view=status_layout(
                "Botão removido",
                f"O botão **{removed.get('label', numero)}** foi removido. Os botões seguintes foram renumerados.",
            ),
            ephemeral=True,
        )

    @painel_group.command(name="visualizar", description="Mostra uma prévia privada do painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def preview_command(self, interaction: discord.Interaction, painel_id: str) -> None:
        await self.preview_panel(interaction, painel_id)

    @painel_group.command(name="publicar", description="Publica uma nova cópia do painel no canal escolhido")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish_command(
        self,
        interaction: discord.Interaction,
        painel_id: str,
        canal: Optional[discord.TextChannel] = None,
    ) -> None:
        await self.publish_saved_panel(interaction, painel_id.lower(), canal)

    @painel_group.command(name="listar", description="Lista os painéis salvos neste servidor")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def list_panels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        records = await store.all(interaction.guild.id, NAMESPACE)
        panels = [value for value in records.values() if isinstance(value, dict)]
        panels.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        if not panels:
            description = "Nenhum painel foi criado neste servidor."
        else:
            lines = []
            for panel in panels[:20]:
                pages = panel.get("pages", [])
                lines.append(
                    f"`{panel.get('id')}` • **{panel.get('title', 'Sem título')}** • {len(pages)} botão(ões)"
                )
            description = "\n".join(lines)
        await interaction.response.send_message(
            view=status_layout("Painéis interativos", description),
            ephemeral=True,
        )

    @painel_group.command(name="excluir", description="Exclui a configuração salva de um painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_panel(self, interaction: discord.Interaction, painel_id: str) -> None:
        assert interaction.guild is not None
        panel = await _get_panel(interaction.guild.id, painel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return
        await store.delete(interaction.guild.id, NAMESPACE, painel_id.lower())
        await interaction.response.send_message(
            view=status_layout(
                "Painel excluído",
                f"A configuração `{painel_id}` foi apagada. Mensagens já publicadas deixarão de abrir páginas.",
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PainelInterativo(bot))
