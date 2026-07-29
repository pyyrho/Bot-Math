"""Sistema de tickets (atendimento privado) com Discord Components V2.

Os administradores configuram o texto do painel (título, descrição, cor,
banner, rodapé), até 5 cargos de staff a serem notificados, a categoria onde
os canais de ticket serão criados e um canal opcional de logs. O painel
público é publicado com um botão "Abrir Ticket"; ao clicar, o membro recebe
um canal privado só seu + staff, com um mini painel para chamar a equipe ou
encerrar o atendimento. Um painel administrativo (com funções como assumir,
adicionar membro, encerrar e excluir) só é acessível via `/ticket painel`,
comando restrito à staff e sempre respondido de forma efêmera — portanto
nunca fica visível para o membro do ticket.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store
from utils.ui_v2 import DEFAULT_ACCENT, build_layout, parse_emoji, parse_hex, status_layout, valid_url

log = logging.getLogger("cogs.tickets")

CONFIG_NS = "tickets_config"
TICKETS_NS = "tickets_active"
COOLDOWN_SECONDS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "title": "Central de Atendimento",
        "description": "Clique no botão abaixo para abrir um atendimento privado com a nossa equipe.",
        "accent": DEFAULT_ACCENT,
        "thumbnail": None,
        "banner": None,
        "footer": "Nossa equipe irá atendê-lo em breve.",
        "staff_role_ids": [],
        "category_id": None,
        "log_channel_id": None,
        "next_number": 1,
    }


def _clean_optional_media(value: Optional[str], current: Optional[str]) -> Optional[str]:
    if value is None:
        return current
    raw = value.strip()
    if raw.lower() in {"remover", "nenhum", "none", "-"}:
        return None
    return raw or None


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return ascii_text or "usuario"


def _is_staff(interaction: discord.Interaction, config: dict[str, Any]) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    staff_ids = {int(role_id) for role_id in (config.get("staff_role_ids") or [])}
    return any(role.id in staff_ids for role in member.roles)


async def _get_config(guild_id: int) -> Optional[dict[str, Any]]:
    config = await store.get(guild_id, CONFIG_NS, "config", None)
    return config if isinstance(config, dict) else None


async def _save_config(guild_id: int, config: dict[str, Any]) -> None:
    config["updated_at"] = _now()
    await store.set(guild_id, CONFIG_NS, "config", config)


async def _get_ticket(guild_id: int, channel_id: int) -> Optional[dict[str, Any]]:
    record = await store.get(guild_id, TICKETS_NS, str(channel_id), None)
    return record if isinstance(record, dict) else None


async def _save_ticket(guild_id: int, record: dict[str, Any]) -> None:
    await store.set(guild_id, TICKETS_NS, str(record["channel_id"]), record)


def _ticket_button(
    action: str,
    ref: int,
    *,
    label: str,
    style: discord.ButtonStyle,
    emoji: Optional[str] = None,
) -> discord.ui.Button:
    return discord.ui.Button(
        label=label,
        style=style,
        emoji=parse_emoji(emoji),
        custom_id=f"ticket:{action}:{ref}",
    )


def _staff_roles(guild: discord.Guild, config: dict[str, Any]) -> list[discord.Role]:
    roles = []
    for role_id in config.get("staff_role_ids", []) or []:
        role = guild.get_role(int(role_id))
        if role:
            roles.append(role)
    return roles


async def _close_ticket(
    guild: discord.Guild,
    record: dict[str, Any],
    config: dict[str, Any],
    *,
    closed_by: discord.abc.User,
) -> None:
    record["status"] = "closed"
    record["closed_at"] = _now()
    record["closed_by"] = closed_by.id
    await _save_ticket(guild.id, record)

    channel = guild.get_channel(int(record["channel_id"]))
    if isinstance(channel, discord.TextChannel):
        owner = guild.get_member(int(record["owner_id"]))
        try:
            if owner:
                await channel.set_permissions(owner, view_channel=True, send_messages=False, read_message_history=True)
        except discord.HTTPException:
            log.exception("Falha ao ajustar permissões do dono ao encerrar o ticket %s", record.get("number"))
        if not channel.name.startswith("fechado-"):
            try:
                await channel.edit(name=f"fechado-{channel.name}"[:100], reason=f"Ticket encerrado por {closed_by}")
            except discord.HTTPException:
                log.exception("Falha ao renomear o canal do ticket %s", record.get("number"))
        try:
            await channel.send(
                f"🔒 Este ticket foi encerrado por {closed_by.mention}. O canal permanece apenas para consulta.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    log_channel_id = config.get("log_channel_id")
    if log_channel_id:
        log_channel = guild.get_channel(int(log_channel_id))
        if isinstance(log_channel, discord.TextChannel):
            summary = (
                f"**Ticket #{int(record.get('number', 0)):04d} encerrado**\n"
                f"Aberto por: <@{record.get('owner_id')}>\n"
                f"Encerrado por: {closed_by.mention}\n"
                f"Aberto em: {record.get('opened_at', '—')}\n"
                f"Encerrado em: {record.get('closed_at', '—')}"
            )
            try:
                await log_channel.send(summary, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Falha ao enviar log de encerramento do ticket %s", record.get("number"))


async def _handle_open(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    assert guild is not None
    config = await _get_config(guild.id)
    if not config:
        await interaction.response.send_message(
            view=status_layout("Sistema indisponível", "O sistema de tickets ainda não foi configurado neste servidor."),
            ephemeral=True,
        )
        return

    member = guild.get_member(interaction.user.id) or interaction.user
    active = await store.all(guild.id, TICKETS_NS)
    for record in active.values():
        if isinstance(record, dict) and record.get("owner_id") == member.id and record.get("status") == "open":
            existing_channel = guild.get_channel(int(record.get("channel_id", 0)))
            mention = existing_channel.mention if existing_channel else "seu ticket"
            await interaction.response.send_message(
                view=status_layout("Você já possui um ticket aberto", f"Continue o atendimento em {mention}."),
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)

    category = guild.get_channel(int(config.get("category_id") or 0))
    category = category if isinstance(category, discord.CategoryChannel) else None
    number = int(config.get("next_number", 1))

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_permissions=True,
            manage_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        ),
    }
    staff_roles = _staff_roles(guild, config)
    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_messages=True,
            attach_files=True, embed_links=True,
        )

    channel_name = f"ticket-{number:04d}-{_slugify(member.display_name)}"[:100]
    try:
        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{number:04d} • aberto por {member} ({member.id})",
            reason=f"Ticket aberto por {member} (#{number:04d})",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            view=status_layout("Sem permissão", "Não tenho permissão para criar canais aqui. Avise um administrador."),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        log.exception("Falha ao criar canal de ticket")
        await interaction.followup.send(
            view=status_layout("Falha ao abrir ticket", "Ocorreu um erro ao criar o canal. Tente novamente."),
            ephemeral=True,
        )
        return

    record = {
        "channel_id": channel.id,
        "guild_id": guild.id,
        "owner_id": member.id,
        "number": number,
        "status": "open",
        "claimed_by": None,
        "opened_at": _now(),
        "closed_at": None,
        "last_call": None,
    }
    await _save_ticket(guild.id, record)
    config["next_number"] = number + 1
    await _save_config(guild.id, config)

    role_mentions = " ".join(role.mention for role in staff_roles)
    header = member.mention if not role_mentions else f"{member.mention} • {role_mentions}"
    try:
        await channel.send(
            header,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
    except discord.HTTPException:
        pass

    panel_view = build_layout(
        title=f"{config.get('title', 'Central de Atendimento')} — Ticket #{number:04d}",
        description=config.get("description", "Aguarde, em instantes um membro da equipe irá atendê-lo."),
        accent=int(config.get("accent", DEFAULT_ACCENT)),
        thumbnail=config.get("thumbnail"),
        banner=config.get("banner"),
        footer=config.get("footer") or "Utilize os botões abaixo para chamar a equipe ou encerrar o atendimento",
        buttons=[
            _ticket_button("chamar", channel.id, label="Chamar Staff", style=discord.ButtonStyle.secondary, emoji="🔔"),
            _ticket_button("fechar", channel.id, label="Fechar Ticket", style=discord.ButtonStyle.danger, emoji="🔒"),
        ],
        timeout=None,
    )
    try:
        await channel.send(view=panel_view)
    except discord.HTTPException:
        log.exception("Falha ao enviar o painel do ticket %s", number)

    await interaction.followup.send(
        view=status_layout("Ticket aberto", f"Seu ticket foi criado em {channel.mention}."),
        ephemeral=True,
    )


async def _handle_call(interaction: discord.Interaction, channel_id: int) -> None:
    guild = interaction.guild
    assert guild is not None
    record = await _get_ticket(guild.id, channel_id)
    if not record or record.get("status") != "open":
        await interaction.response.send_message(
            view=status_layout("Ticket indisponível", "Este ticket não está mais ativo."),
            ephemeral=True,
        )
        return

    last_call = record.get("last_call")
    if last_call:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_call)).total_seconds()
        except ValueError:
            elapsed = COOLDOWN_SECONDS
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed)
            await interaction.response.send_message(
                view=status_layout("Aguarde um momento", f"Você poderá chamar a equipe novamente em {remaining}s."),
                ephemeral=True,
            )
            return

    config = await _get_config(guild.id) or _default_config()
    roles = _staff_roles(guild, config)
    mention_text = " ".join(role.mention for role in roles) if roles else "Equipe"

    record["last_call"] = _now()
    await _save_ticket(guild.id, record)

    await interaction.response.send_message(
        f"{mention_text} • {interaction.user.mention} está solicitando atendimento neste ticket.",
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
    )


async def _handle_close(interaction: discord.Interaction, channel_id: int) -> None:
    guild = interaction.guild
    assert guild is not None
    record = await _get_ticket(guild.id, channel_id)
    if not record:
        await interaction.response.send_message(
            view=status_layout("Ticket indisponível", "Este ticket não foi encontrado."),
            ephemeral=True,
        )
        return
    if record.get("status") != "open":
        await interaction.response.send_message(
            view=status_layout("Ticket já encerrado", "Este ticket já foi encerrado anteriormente."),
            ephemeral=True,
        )
        return

    config = await _get_config(guild.id) or _default_config()
    is_owner = interaction.user.id == record.get("owner_id")
    if not (is_owner or _is_staff(interaction, config)):
        await interaction.response.send_message(
            "Apenas o autor do ticket ou a equipe podem encerrá-lo.", ephemeral=True
        )
        return

    await _close_ticket(guild, record, config, closed_by=interaction.user)
    await interaction.response.send_message(
        view=status_layout("Ticket encerrado", "Este ticket foi arquivado e não aceita mais mensagens do membro."),
    )


class TicketActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket:(?P<action>abrir|chamar|fechar):(?P<ref>[0-9]{1,25})",
):
    def __init__(
        self,
        action: str,
        ref: int,
        *,
        label: str = "Ticket",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
    ) -> None:
        self.action = action
        self.ref = ref
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                emoji=parse_emoji(emoji),
                custom_id=f"ticket:{action}:{ref}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> "TicketActionButton":
        button = item if isinstance(item, discord.ui.Button) else None
        return cls(
            match["action"],
            int(match["ref"]),
            label=button.label if button and button.label else "Ticket",
            style=button.style if button else discord.ButtonStyle.secondary,
            emoji=str(button.emoji) if button and button.emoji else None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Isso só funciona dentro de servidores.", ephemeral=True)
            return
        if self.action == "abrir":
            await _handle_open(interaction)
        elif self.action == "chamar":
            await _handle_call(interaction, self.ref)
        elif self.action == "fechar":
            await _handle_close(interaction, self.ref)


class ConfigureTicketModal(discord.ui.Modal, title="Configurar painel de tickets"):
    titulo = discord.ui.TextInput(
        label="Título do painel",
        placeholder="Ex.: Central de Atendimento",
        max_length=200,
    )
    descricao = discord.ui.TextInput(
        label="Descrição do painel",
        placeholder="Explique como o membro deve usar o sistema de tickets.",
        style=discord.TextStyle.paragraph,
        max_length=3500,
    )
    cor = discord.ui.TextInput(
        label="Cor lateral (#RRGGBB)",
        placeholder="#5865F2",
        required=False,
        max_length=7,
    )
    banner = discord.ui.TextInput(
        label="URL do banner (opcional)",
        required=False,
        max_length=500,
    )
    rodape = discord.ui.TextInput(
        label="Rodapé (opcional)",
        required=False,
        max_length=500,
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.config = config
        self.titulo.default = config.get("title")
        self.descricao.default = config.get("description")
        self.cor.default = f"#{int(config.get('accent', DEFAULT_ACCENT)):06X}"
        self.banner.default = config.get("banner")
        self.rodape.default = config.get("footer")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        banner_value = str(self.banner.value).strip() or None
        if not valid_url(banner_value):
            await interaction.response.send_message("O banner deve ser uma URL http/https válida.", ephemeral=True)
            return
        try:
            accent = parse_hex(str(self.cor.value), int(self.config.get("accent", DEFAULT_ACCENT)))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        config = await _get_config(interaction.guild.id) or _default_config()
        config["title"] = str(self.titulo.value).strip()
        config["description"] = str(self.descricao.value).strip()
        config["accent"] = accent
        config["banner"] = banner_value
        config["footer"] = str(self.rodape.value).strip() or None
        await _save_config(interaction.guild.id, config)
        await interaction.response.send_message(
            view=status_layout(
                "Configuração salva",
                "O texto do painel de tickets foi atualizado. Use `/ticket publicar` para publicá-lo (ou republicá-lo).",
            ),
            ephemeral=True,
        )


class TicketClaimButton(discord.ui.Button):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(label="Assumir Ticket", style=discord.ButtonStyle.primary, emoji="📌")
        self.record = record

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicketAdminView)
        if not _is_staff(interaction, view.config):
            await interaction.response.send_message("Você não tem permissão para usar este painel.", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        record = await _get_ticket(guild.id, self.record["channel_id"])
        if not record or record.get("status") != "open":
            await interaction.response.send_message("Este ticket não está mais ativo.", ephemeral=True)
            return
        record["claimed_by"] = interaction.user.id
        await _save_ticket(guild.id, record)
        channel = guild.get_channel(int(record["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"📌 Ticket assumido por {interaction.user.mention}.")
            except discord.HTTPException:
                pass
        await interaction.response.send_message("Você assumiu este ticket.", ephemeral=True)


class TicketCloseButton(discord.ui.Button):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(label="Fechar e Arquivar", style=discord.ButtonStyle.danger, emoji="🔒")
        self.record = record

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicketAdminView)
        if not _is_staff(interaction, view.config):
            await interaction.response.send_message("Você não tem permissão para usar este painel.", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        record = await _get_ticket(guild.id, self.record["channel_id"])
        if not record or record.get("status") != "open":
            await interaction.response.send_message("Este ticket já está encerrado.", ephemeral=True)
            return
        await _close_ticket(guild, record, view.config, closed_by=interaction.user)
        await interaction.response.send_message("Ticket encerrado e arquivado com sucesso.", ephemeral=True)


class TicketDeleteButton(discord.ui.Button):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(label="Excluir Definitivamente", style=discord.ButtonStyle.danger, emoji="🗑️")
        self.record = record

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicketAdminView)
        if not _is_staff(interaction, view.config):
            await interaction.response.send_message("Você não tem permissão para usar este painel.", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        record = await _get_ticket(guild.id, self.record["channel_id"])
        channel = guild.get_channel(int(self.record["channel_id"]))
        if record and view.config.get("log_channel_id"):
            log_channel = guild.get_channel(int(view.config["log_channel_id"]))
            if isinstance(log_channel, discord.TextChannel):
                try:
                    await log_channel.send(
                        f"🗑️ Ticket #{int(record.get('number', 0)):04d} excluído por {interaction.user.mention}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
        await store.delete(guild.id, TICKETS_NS, str(self.record["channel_id"]))
        await interaction.response.send_message("Ticket excluído. O canal será removido em instantes.", ephemeral=True)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.delete(reason=f"Ticket excluído por {interaction.user}")
            except discord.HTTPException:
                pass


class TicketAddMemberSelect(discord.ui.UserSelect):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(placeholder="Adicionar um membro a este ticket", min_values=1, max_values=1)
        self.record = record

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TicketAdminView)
        if not _is_staff(interaction, view.config):
            await interaction.response.send_message("Você não tem permissão para usar este painel.", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        channel = guild.get_channel(int(self.record["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal do ticket não encontrado.", ephemeral=True)
            return
        target = self.values[0]
        try:
            await channel.set_permissions(target, view_channel=True, send_messages=True, read_message_history=True)
        except discord.HTTPException:
            await interaction.response.send_message(
                "Não foi possível adicionar o membro (verifique minhas permissões no canal).", ephemeral=True
            )
            return
        try:
            await channel.send(f"➕ {target.mention} foi adicionado ao ticket por {interaction.user.mention}.")
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"{target.mention} foi adicionado ao ticket.", ephemeral=True)


class TicketAdminView(discord.ui.LayoutView):
    """Painel administrativo — só é enviado como resposta efêmera a quem tem permissão de staff."""

    def __init__(self, record: dict[str, Any], config: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.record = record
        self.config = config
        claimed = record.get("claimed_by")
        claimed_text = f"<@{claimed}>" if claimed else "Ninguém ainda"
        description = (
            f"**Ticket:** #{int(record.get('number', 0)):04d}\n"
            f"**Aberto por:** <@{record.get('owner_id')}>\n"
            f"**Responsável atual:** {claimed_text}\n\n"
            "-# Estas ações não ficam visíveis para o membro do ticket."
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Painel administrativo do ticket\n{description}"),
                discord.ui.ActionRow(
                    TicketClaimButton(record),
                    TicketCloseButton(record),
                    TicketDeleteButton(record),
                ),
                discord.ui.ActionRow(TicketAddMemberSelect(record)),
                accent_color=int(config.get("accent", DEFAULT_ACCENT)),
            )
        )


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(TicketActionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(TicketActionButton)

    ticket_group = app_commands.Group(
        name="ticket",
        description="Sistema de tickets de atendimento privado",
        guild_only=True,
    )

    @ticket_group.command(name="configurar_texto", description="Edita título, descrição, cor, banner e rodapé do painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_text(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id) or _default_config()
        await interaction.response.send_modal(ConfigureTicketModal(config))

    @ticket_group.command(name="configurar_imagem", description="Define ou remove a miniatura e o banner do painel")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_image(
        self,
        interaction: discord.Interaction,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id) or _default_config()
        new_thumbnail = _clean_optional_media(miniatura, config.get("thumbnail"))
        new_banner = _clean_optional_media(banner, config.get("banner"))
        if not valid_url(new_thumbnail) or not valid_url(new_banner):
            await interaction.response.send_message("Miniatura e banner devem ser URLs http/https válidas.", ephemeral=True)
            return
        config["thumbnail"] = new_thumbnail
        config["banner"] = new_banner
        await _save_config(interaction.guild.id, config)
        await interaction.response.send_message(
            view=status_layout("Imagens atualizadas", "A miniatura e o banner do painel de tickets foram atualizados."),
            ephemeral=True,
        )

    @ticket_group.command(name="configurar_cargos", description="Define até 5 cargos de staff notificados nos tickets")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_roles(
        self,
        interaction: discord.Interaction,
        cargo_1: Optional[discord.Role] = None,
        cargo_2: Optional[discord.Role] = None,
        cargo_3: Optional[discord.Role] = None,
        cargo_4: Optional[discord.Role] = None,
        cargo_5: Optional[discord.Role] = None,
    ) -> None:
        assert interaction.guild is not None
        roles = [role for role in (cargo_1, cargo_2, cargo_3, cargo_4, cargo_5) if role is not None]
        config = await _get_config(interaction.guild.id) or _default_config()
        config["staff_role_ids"] = [role.id for role in roles]
        await _save_config(interaction.guild.id, config)
        if roles:
            description = f"Os cargos de staff foram definidos: {', '.join(role.mention for role in roles)}"
        else:
            description = "Nenhum cargo de staff configurado. Somente administradores poderão gerenciar os tickets."
        await interaction.response.send_message(view=status_layout("Cargos atualizados", description), ephemeral=True)

    @ticket_group.command(name="configurar_categoria", description="Define a categoria onde os tickets serão criados")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_category(self, interaction: discord.Interaction, categoria: discord.CategoryChannel) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id) or _default_config()
        config["category_id"] = categoria.id
        await _save_config(interaction.guild.id, config)
        await interaction.response.send_message(
            view=status_layout("Categoria definida", f"Os tickets serão criados dentro de **{categoria.name}**."),
            ephemeral=True,
        )

    @ticket_group.command(name="configurar_logs", description="Define ou remove o canal de logs de tickets")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_logs(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
        remover: bool = False,
    ) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id) or _default_config()
        if remover:
            config["log_channel_id"] = None
            description = "O canal de logs foi removido."
        elif canal is not None:
            config["log_channel_id"] = canal.id
            description = f"Os registros de abertura/encerramento serão enviados para {canal.mention}."
        else:
            await interaction.response.send_message("Informe um canal ou use `remover: true`.", ephemeral=True)
            return
        await _save_config(interaction.guild.id, config)
        await interaction.response.send_message(view=status_layout("Canal de logs atualizado", description), ephemeral=True)

    @ticket_group.command(name="publicar", description="Publica (ou republica) o painel de abertura de tickets")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish(self, interaction: discord.Interaction, canal: Optional[discord.TextChannel] = None) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id)
        if not config:
            await interaction.response.send_message(
                "Configure o painel primeiro com `/ticket configurar_texto`.", ephemeral=True
            )
            return
        target = canal or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Escolha um canal de texto válido.", ephemeral=True)
            return

        button = _ticket_button("abrir", interaction.guild.id, label="Abrir Ticket", style=discord.ButtonStyle.primary, emoji="🎫")
        panel_view = build_layout(
            title=config.get("title", "Central de Atendimento"),
            description=config.get("description", "Clique no botão abaixo para abrir um atendimento privado."),
            accent=int(config.get("accent", DEFAULT_ACCENT)),
            thumbnail=config.get("thumbnail"),
            banner=config.get("banner"),
            footer=config.get("footer") or "Um membro da equipe irá atendê-lo em breve",
            buttons=[button],
            timeout=None,
            buttons_per_row=1,
        )

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            await target.send(view=panel_view)
        except discord.HTTPException:
            await interaction.followup.send(
                view=status_layout("Falha ao publicar", "Verifique minhas permissões no canal escolhido."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            view=status_layout("Painel publicado", f"O painel de tickets foi enviado para {target.mention}."),
            ephemeral=True,
        )

    @ticket_group.command(name="status", description="Lista os tickets abertos neste servidor")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def status_command(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        records = await store.all(interaction.guild.id, TICKETS_NS)
        open_tickets = [record for record in records.values() if isinstance(record, dict) and record.get("status") == "open"]
        open_tickets.sort(key=lambda record: record.get("number", 0))
        if not open_tickets:
            description = "Nenhum ticket aberto no momento."
        else:
            lines = []
            for record in open_tickets[:25]:
                channel = interaction.guild.get_channel(int(record.get("channel_id", 0)))
                mention = channel.mention if channel else "canal removido"
                claimed = record.get("claimed_by")
                claimed_text = f"<@{claimed}>" if claimed else "não assumido"
                lines.append(
                    f"`#{int(record.get('number', 0)):04d}` • {mention} • aberto por <@{record.get('owner_id')}> • {claimed_text}"
                )
            description = "\n".join(lines)
        await interaction.response.send_message(view=status_layout("Tickets abertos", description), ephemeral=True)

    @ticket_group.command(name="painel", description="Abre o painel administrativo do ticket atual (somente staff)")
    @app_commands.guild_only()
    async def admin_panel(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id) or _default_config()
        if not _is_staff(interaction, config):
            await interaction.response.send_message("Você não tem permissão para acessar o painel administrativo.", ephemeral=True)
            return
        channel_id = interaction.channel.id if interaction.channel else 0
        record = await _get_ticket(interaction.guild.id, channel_id)
        if not record:
            await interaction.response.send_message("Use este comando dentro de um canal de ticket.", ephemeral=True)
            return
        await interaction.response.send_message(view=TicketAdminView(record, config), ephemeral=True)

    @ticket_group.command(name="fechar", description="Encerra o ticket do canal atual")
    @app_commands.guild_only()
    async def close_command(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Use este comando dentro de um canal de ticket.", ephemeral=True)
            return
        record = await _get_ticket(interaction.guild.id, channel.id)
        if not record:
            await interaction.response.send_message("Este canal não é um ticket ativo.", ephemeral=True)
            return
        if record.get("status") != "open":
            await interaction.response.send_message("Este ticket já foi encerrado.", ephemeral=True)
            return
        config = await _get_config(interaction.guild.id) or _default_config()
        if not (interaction.user.id == record.get("owner_id") or _is_staff(interaction, config)):
            await interaction.response.send_message("Apenas o autor do ticket ou a equipe podem encerrá-lo.", ephemeral=True)
            return
        await _close_ticket(interaction.guild, record, config, closed_by=interaction.user)
        await interaction.response.send_message(view=status_layout("Ticket encerrado", "Este ticket foi arquivado."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
