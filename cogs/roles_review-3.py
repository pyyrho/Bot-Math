"""Sistema de nível acadêmico com formulário e revisão administrativa.

Fluxo:
- Entusiasta, Pré-Universitário e Graduação: concessão automática.
- Mestrado, Doutorado e Pós-Doutorado: formulário e revisão.
- O painel público usa Components V2 e abre um menu privado.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store
from utils.ui_v2 import DEFAULT_ACCENT, build_layout, parse_hex, status_layout, valid_url

log = logging.getLogger("cogs.roles_review")

CONFIG_NS = "roles"
APPLICATION_NS = "role_apps"

LEVELS: dict[str, dict[str, Any]] = {
    "entusiasta": {
        "label": "Entusiasta",
        "description": "Interessado em matemática, sem exigência acadêmica.",
        "review": False,
    },
    "pre": {
        "label": "Pré-Universitário",
        "description": "Ensino médio, vestibulandos e olimpíadas.",
        "review": False,
    },
    "graduacao": {
        "label": "Graduação",
        "description": "Cursando ou graduado em curso superior.",
        "review": False,
    },
    "mestrado": {
        "label": "Mestrado",
        "description": "Cursando ou concluído. Requer análise.",
        "review": True,
    },
    "doutorado": {
        "label": "Doutorado",
        "description": "Cursando ou concluído. Requer análise.",
        "review": True,
    },
    "pos_doutorado": {
        "label": "Pós-Doutorado",
        "description": "Pesquisa pós-doutoral. Requer análise.",
        "review": True,
    },
}

ROLE_KEYS = tuple(LEVELS.keys())


def _legacy_roles_config() -> dict[str, Any]:
    """Lê a configuração JSON da versão anterior, quando ela ainda existir."""
    path = Path("data/roles_config.json")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _default_config() -> dict[str, Any]:
    """Mantém compatibilidade com variáveis e arquivo da versão anterior."""
    legacy = _legacy_roles_config()
    return {
        "roles": {
            "entusiasta": int(os.environ.get("CARGO_ENTUSIASTA", "0") or 0),
            "pre": int(legacy.get("cargo_pre", os.environ.get("CARGO_PRE_UNIVERSITARIO", "0")) or 0),
            "graduacao": int(legacy.get("cargo_grad", os.environ.get("CARGO_GRADUACAO", "0")) or 0),
            "mestrado": int(legacy.get("cargo_mestrado", os.environ.get("CARGO_MESTRADO", "0")) or 0),
            "doutorado": int(legacy.get("cargo_dout", os.environ.get("CARGO_DOUTORADO", "0")) or 0),
            "pos_doutorado": int(os.environ.get("CARGO_POS_DOUTORADO", "0") or 0),
        },
        "pending_role": int(legacy.get("cargo_pendente", os.environ.get("CARGO_PENDENTE", "0")) or 0),
        "review_channel": int(legacy.get("canal_revisao", os.environ.get("CANAL_REVISAO", "0")) or 0),
        "panel": {
            "title": "Currículo/nível",
            "description": (
                "Selecione o nível acadêmico que melhor representa seu momento atual.\n\n"
                "Cargos de **Mestrado**, **Doutorado** e **Pós-Doutorado** exigem "
                "verificação. Nesses casos, um formulário será enviado para análise da administração."
            ),
            "accent": DEFAULT_ACCENT,
            "thumbnail": None,
            "banner": None,
        },
    }


async def get_config(guild_id: int) -> dict[str, Any]:
    saved = await store.get(guild_id, CONFIG_NS, "config", None)
    base = _default_config()
    if not isinstance(saved, dict):
        return base

    if isinstance(saved.get("roles"), dict):
        base["roles"].update(saved["roles"])
    for key in ("pending_role", "review_channel"):
        if key in saved:
            base[key] = saved[key]
    if isinstance(saved.get("panel"), dict):
        base["panel"].update(saved["panel"])
    return base


async def set_config(guild_id: int, config: dict[str, Any]) -> None:
    await store.set(guild_id, CONFIG_NS, "config", config)


def _manageable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(
        me
        and me.guild_permissions.manage_roles
        and role < me.top_role
        and not role.managed
        and role != guild.default_role
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _role_from_config(guild: discord.Guild, config: dict[str, Any], level: str) -> Optional[discord.Role]:
    raw = config.get("roles", {}).get(level, 0)
    try:
        role_id = int(raw)
    except (TypeError, ValueError):
        return None
    return guild.get_role(role_id) if role_id else None


def _all_level_roles(guild: discord.Guild, config: dict[str, Any]) -> list[discord.Role]:
    result: list[discord.Role] = []
    for level in ROLE_KEYS:
        role = _role_from_config(guild, config, level)
        if role and role not in result:
            result.append(role)
    pending_raw = config.get("pending_role", 0)
    try:
        pending = guild.get_role(int(pending_raw)) if pending_raw else None
    except (TypeError, ValueError):
        pending = None
    if pending and pending not in result:
        result.append(pending)
    return result


class OpenAcademicMenuButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Gerenciar cargos",
            style=discord.ButtonStyle.primary,
            custom_id="mathroles:open",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RolesReview")
        if not isinstance(cog, RolesReview):
            await interaction.response.send_message("O sistema de cargos está indisponível.", ephemeral=True)
            return
        await cog.open_level_menu(interaction)


class AcademicPanelView(discord.ui.LayoutView):
    """View persistente do painel público."""

    def __init__(
        self,
        *,
        title: str = "Currículo/nível",
        description: str = "Selecione seu nível acadêmico.",
        accent: int = DEFAULT_ACCENT,
        thumbnail: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
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
        children.append(discord.ui.ActionRow(OpenAcademicMenuButton()))
        self.add_item(discord.ui.Container(*children, accent_color=accent))


class AcademicLevelSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label=data["label"],
                value=level,
                description=data["description"],
            )
            for level, data in LEVELS.items()
        ]
        options.append(
            discord.SelectOption(
                label="Nenhuma das opções",
                value="none",
                description="Remove seu cargo de nível acadêmico.",
            )
        )
        super().__init__(
            placeholder="Selecione seu nível de matemática...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="mathroles:select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RolesReview")
        if not isinstance(cog, RolesReview):
            await interaction.response.send_message("O sistema de cargos está indisponível.", ephemeral=True)
            return
        await cog.handle_level_selection(interaction, self.values[0])


class AcademicMenuView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# Selecione seu nível de matemática\n"
                    "Escolha apenas a opção que melhor representa seu nível atual."
                ),
                discord.ui.ActionRow(AcademicLevelSelect()),
                accent_color=DEFAULT_ACCENT,
            )
        )


class AdvancedApplicationModal(discord.ui.Modal):
    area = discord.ui.TextInput(
        label="Qual área da matemática lhe interessa?",
        placeholder="Ex.: lógica, álgebra, análise, EDPs...",
        style=discord.TextStyle.paragraph,
        max_length=600,
    )
    background = discord.ui.TextInput(
        label="Qual é sua formação acadêmica?",
        placeholder="Esta resposta pode ser breve.",
        style=discord.TextStyle.paragraph,
        max_length=600,
        required=False,
    )
    readings = discord.ui.TextInput(
        label="Quais livros ou artigos leu recentemente?",
        placeholder="Conte também o que achou relevante nessas leituras.",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, level: str) -> None:
        self.level = level
        super().__init__(title=f"Solicitação: {LEVELS[level]['label']}", timeout=600)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RolesReview")
        if not isinstance(cog, RolesReview):
            await interaction.response.send_message("O sistema de cargos está indisponível.", ephemeral=True)
            return
        await cog.submit_application(
            interaction,
            level=self.level,
            area=str(self.area.value).strip(),
            background=str(self.background.value).strip(),
            readings=str(self.readings.value).strip(),
        )


class ReviewDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"mathrole:(?P<action>approve|reject):(?P<application>[0-9a-f]{8})",
):
    def __init__(self, action: str, application_id: str) -> None:
        self.action = action
        self.application_id = application_id
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Aprovar" if approve else "Rejeitar",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"mathrole:{action}:{application_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match,
        /,
    ) -> "ReviewDecisionButton":
        return cls(match["action"], match["application"])

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        allowed = interaction.user.guild_permissions.manage_roles or interaction.user.guild_permissions.administrator
        if not allowed:
            await interaction.response.send_message("Você não possui permissão para revisar cargos.", ephemeral=True)
        return allowed

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RolesReview")
        if not isinstance(cog, RolesReview):
            await interaction.response.send_message("O sistema de revisão está indisponível.", ephemeral=True)
            return
        await cog.decide_application(interaction, self.application_id, self.action)


class RolesReview(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(AcademicPanelView())
        self.bot.add_dynamic_items(ReviewDecisionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(ReviewDecisionButton)

    async def open_level_menu(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Este painel só funciona em servidores.", ephemeral=True)
            return
        await interaction.response.send_message(view=AcademicMenuView(), ephemeral=True)

    async def handle_level_selection(self, interaction: discord.Interaction, level: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Este menu só funciona em servidores.", ephemeral=True)
            return

        if level == "none":
            await interaction.response.defer(ephemeral=True)
            config = await get_config(interaction.guild.id)
            roles = [role for role in _all_level_roles(interaction.guild, config) if role in interaction.user.roles]
            manageable = [role for role in roles if _manageable(interaction.guild, role)]
            try:
                if manageable:
                    await interaction.user.remove_roles(*manageable, reason="Remoção voluntária de nível acadêmico")
            except discord.HTTPException:
                log.exception("Falha ao remover cargos acadêmicos de %s", interaction.user.id)
                await interaction.followup.send(
                    view=status_layout("Falha ao remover", "Verifique a hierarquia de cargos do bot."),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                view=status_layout("Cargo removido", "Seu cargo de nível acadêmico foi removido."),
                ephemeral=True,
            )
            return

        data = LEVELS.get(level)
        if not data:
            await interaction.response.send_message("Opção inválida.", ephemeral=True)
            return

        config = await get_config(interaction.guild.id)
        role = _role_from_config(interaction.guild, config, level)
        if not role:
            await interaction.response.send_message(
                view=status_layout(
                    "Cargo não configurado",
                    f"A administração ainda não vinculou o nível **{data['label']}** a um cargo.",
                ),
                ephemeral=True,
            )
            return
        if not _manageable(interaction.guild, role):
            await interaction.response.send_message(
                view=status_layout(
                    "Cargo inalcançável",
                    "O cargo do bot precisa ficar acima dos cargos acadêmicos.",
                ),
                ephemeral=True,
            )
            return

        if data["review"]:
            review_channel_id = int(config.get("review_channel", 0) or 0)
            review_channel = interaction.guild.get_channel(review_channel_id)
            if not isinstance(review_channel, discord.TextChannel):
                await interaction.response.send_message(
                    view=status_layout(
                        "Revisão não configurada",
                        "A administração ainda não definiu o canal de análise.",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(AdvancedApplicationModal(level))
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._apply_level(interaction.guild, interaction.user, config, role, level)
        except discord.HTTPException:
            await interaction.followup.send(
                view=status_layout(
                    "Falha ao atualizar",
                    "Não consegui alterar seu cargo. Verifique a hierarquia de cargos do bot.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            view=status_layout(
                "Nível atualizado",
                f"O cargo **{role.name}** foi aplicado ao seu perfil.",
                accent=role.color.value or DEFAULT_ACCENT,
            ),
            ephemeral=True,
        )

    async def _apply_level(
        self,
        guild: discord.Guild,
        member: discord.Member,
        config: dict[str, Any],
        target_role: discord.Role,
        level: str,
    ) -> None:
        remove = [
            role
            for role in _all_level_roles(guild, config)
            if role in member.roles and role != target_role and _manageable(guild, role)
        ]
        try:
            if remove:
                await member.remove_roles(*remove, reason=f"Alteração de nível acadêmico para {level}")
            if target_role not in member.roles:
                await member.add_roles(target_role, reason=f"Nível acadêmico selecionado: {level}")
        except discord.HTTPException:
            log.exception("Falha ao aplicar nível %s para %s", level, member.id)
            raise

    async def submit_application(
        self,
        interaction: discord.Interaction,
        *,
        level: str,
        area: str,
        background: str,
        readings: str,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Este formulário só funciona em servidores.", ephemeral=True)
            return

        config = await get_config(interaction.guild.id)
        channel_id = int(config.get("review_channel", 0) or 0)
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                view=status_layout("Canal ausente", "O canal de revisão não está configurado."),
                ephemeral=True,
            )
            return

        target_role = _role_from_config(interaction.guild, config, level)
        if not target_role:
            await interaction.response.send_message(
                view=status_layout("Cargo ausente", "O cargo solicitado não está configurado."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        existing_records = await store.all(interaction.guild.id, APPLICATION_NS)
        existing = next(
            (
                value
                for value in existing_records.values()
                if isinstance(value, dict)
                and value.get("status") == "pending"
                and int(value.get("user_id", 0) or 0) == interaction.user.id
            ),
            None,
        )
        if existing:
            existing_level = LEVELS.get(existing.get("level", ""), {}).get("label", "nível avançado")
            await interaction.followup.send(
                view=status_layout(
                    "Solicitação já pendente",
                    f"Você já possui uma solicitação de **{existing_level}** aguardando análise.",
                ),
                ephemeral=True,
            )
            return

        pending_id = int(config.get("pending_role", 0) or 0)
        pending_role = interaction.guild.get_role(pending_id) if pending_id else None
        pending_added = False
        if pending_role and _manageable(interaction.guild, pending_role) and pending_role not in interaction.user.roles:
            try:
                await interaction.user.add_roles(pending_role, reason=f"Solicitação pendente: {level}")
                pending_added = True
            except discord.HTTPException:
                log.exception("Falha ao aplicar cargo pendente em %s", interaction.user.id)

        application_id = secrets.token_hex(4)
        application = {
            "id": application_id,
            "guild_id": interaction.guild.id,
            "user_id": interaction.user.id,
            "user_name": str(interaction.user),
            "level": level,
            "area": area,
            "background": background,
            "readings": readings,
            "status": "pending",
            "created_at": _utc_now(),
            "reviewer_id": None,
            "reviewed_at": None,
            "message_id": None,
            "channel_id": channel.id,
        }

        await store.set(interaction.guild.id, APPLICATION_NS, application_id, application)
        review_view = self._review_layout(application)
        try:
            message = await channel.send(view=review_view)
        except discord.HTTPException:
            log.exception("Falha ao enviar solicitação %s", application_id)
            await store.delete(interaction.guild.id, APPLICATION_NS, application_id)
            if pending_added and pending_role and pending_role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(pending_role, reason="Falha ao registrar solicitação")
                except discord.HTTPException:
                    pass
            await interaction.followup.send(
                view=status_layout("Falha no envio", "Não consegui enviar sua solicitação para a administração."),
                ephemeral=True,
            )
            return

        application["message_id"] = message.id
        await store.set(interaction.guild.id, APPLICATION_NS, application_id, application)
        await interaction.followup.send(
            view=status_layout(
                "Solicitação enviada",
                f"Seu pedido de **{LEVELS[level]['label']}** foi enviado para análise. "
                "Você receberá uma mensagem quando houver uma decisão.",
            ),
            ephemeral=True,
        )

    def _review_layout(
        self,
        application: dict[str, Any],
        *,
        reviewer: Optional[discord.abc.User] = None,
    ) -> discord.ui.LayoutView:
        level = application.get("level", "mestrado")
        data = LEVELS.get(level, {"label": level})
        user_id = int(application.get("user_id", 0) or 0)
        background = application.get("background") or "*(não informado)*"
        status = application.get("status", "pending")

        status_text = {
            "pending": "Aguardando análise",
            "approved": "Aprovado",
            "rejected": "Rejeitado",
        }.get(status, status)
        accent = {
            "pending": 0xF39C12,
            "approved": 0x2ECC71,
            "rejected": 0xE74C3C,
        }.get(status, DEFAULT_ACCENT)

        description = (
            f"**Usuário**\n<@{user_id}> (`{user_id}`)\n\n"
            f"**Nível solicitado**\n{data['label']}\n\n"
            f"**Área de interesse**\n{application.get('area', 'Não informado')}\n\n"
            f"**Formação/background**\n{background}\n\n"
            f"**Papers/livros recentes**\n{application.get('readings', 'Não informado')}\n\n"
            f"**Status**\n{status_text}"
        )
        if reviewer:
            description += f" por {reviewer.mention}"

        buttons: list[discord.ui.Button] = []
        if status == "pending":
            app_id = application["id"]
            buttons = [
                discord.ui.Button(
                    label="Aprovar",
                    style=discord.ButtonStyle.success,
                    custom_id=f"mathrole:approve:{app_id}",
                ),
                discord.ui.Button(
                    label="Rejeitar",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"mathrole:reject:{app_id}",
                ),
            ]
        return build_layout(
            title=f"Nova solicitação: {data['label']}",
            description=description,
            accent=accent,
            buttons=buttons,
            footer=f"ID da solicitação: {application.get('id', '?')}",
            timeout=None if status == "pending" else 180,
        )

    async def decide_application(
        self,
        interaction: discord.Interaction,
        application_id: str,
        action: str,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Ação inválida fora do servidor.", ephemeral=True)
            return

        application = await store.get(interaction.guild.id, APPLICATION_NS, application_id, None)
        if not isinstance(application, dict):
            await interaction.response.send_message("Esta solicitação não foi encontrada.", ephemeral=True)
            return
        if application.get("status") != "pending":
            await interaction.response.send_message(
                f"Esta solicitação já foi **{application.get('status', 'processada')}**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        config = await get_config(interaction.guild.id)
        user_id = int(application.get("user_id", 0) or 0)
        member = interaction.guild.get_member(user_id)
        level = str(application.get("level", ""))
        target_role = _role_from_config(interaction.guild, config, level)

        if not member:
            await interaction.followup.send("O membro não está mais no servidor.", ephemeral=True)
            return

        try:
            if action == "approve":
                if not target_role or not _manageable(interaction.guild, target_role):
                    await interaction.followup.send(
                        "O cargo solicitado não existe ou está acima do cargo do bot.",
                        ephemeral=True,
                    )
                    return
                await self._apply_level(interaction.guild, member, config, target_role, level)
                application["status"] = "approved"
            else:
                pending_id = int(config.get("pending_role", 0) or 0)
                pending_role = interaction.guild.get_role(pending_id) if pending_id else None
                if pending_role and pending_role in member.roles and _manageable(interaction.guild, pending_role):
                    await member.remove_roles(pending_role, reason="Solicitação acadêmica rejeitada")
                application["status"] = "rejected"
        except discord.HTTPException:
            log.exception("Falha ao processar solicitação %s", application_id)
            await interaction.followup.send(
                "Não consegui alterar os cargos. Verifique permissões e hierarquia.",
                ephemeral=True,
            )
            return

        application["reviewer_id"] = interaction.user.id
        application["reviewed_at"] = _utc_now()
        await store.set(interaction.guild.id, APPLICATION_NS, application_id, application)

        if interaction.message:
            try:
                await interaction.message.edit(view=self._review_layout(application, reviewer=interaction.user))
            except discord.HTTPException:
                log.exception("Falha ao atualizar mensagem de revisão %s", application_id)

        decision = "aprovada" if action == "approve" else "rejeitada"
        try:
            await member.send(
                f"Sua solicitação de **{LEVELS.get(level, {}).get('label', level)}** "
                f"no servidor **{interaction.guild.name}** foi **{decision}**."
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(f"Solicitação **{decision}** com sucesso.", ephemeral=True)

    cargos_group = app_commands.Group(
        name="cargos",
        description="Configuração dos níveis acadêmicos",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @cargos_group.command(name="configurar", description="Configura os cargos acadêmicos e o canal de revisão")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def configure_roles(
        self,
        interaction: discord.Interaction,
        entusiasta: discord.Role,
        pre_universitario: discord.Role,
        graduacao: discord.Role,
        mestrado: discord.Role,
        doutorado: discord.Role,
        pos_doutorado: discord.Role,
        pendente: discord.Role,
        canal_revisao: discord.TextChannel,
    ) -> None:
        assert interaction.guild is not None
        selected = [
            entusiasta,
            pre_universitario,
            graduacao,
            mestrado,
            doutorado,
            pos_doutorado,
            pendente,
        ]
        invalid = [role.mention for role in selected if not _manageable(interaction.guild, role)]
        if invalid:
            await interaction.response.send_message(
                view=status_layout(
                    "Hierarquia inválida",
                    "O cargo do bot deve ficar acima destes cargos: " + ", ".join(invalid),
                ),
                ephemeral=True,
            )
            return

        config = await get_config(interaction.guild.id)
        config["roles"] = {
            "entusiasta": entusiasta.id,
            "pre": pre_universitario.id,
            "graduacao": graduacao.id,
            "mestrado": mestrado.id,
            "doutorado": doutorado.id,
            "pos_doutorado": pos_doutorado.id,
        }
        config["pending_role"] = pendente.id
        config["review_channel"] = canal_revisao.id
        await set_config(interaction.guild.id, config)
        await interaction.response.send_message(
            view=status_layout(
                "Sistema configurado",
                f"Seis níveis acadêmicos foram vinculados. Revisões serão enviadas para {canal_revisao.mention}.",
            ),
            ephemeral=True,
        )

    @cargos_group.command(name="painel", description="Publica o painel interativo de nível acadêmico")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def publish_panel(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
        titulo: Optional[str] = None,
        descricao: Optional[str] = None,
        cor: Optional[str] = None,
        miniatura: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> None:
        assert interaction.guild is not None
        channel = canal or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Escolha um canal de texto.", ephemeral=True)
            return
        if not valid_url(miniatura) or not valid_url(banner):
            await interaction.response.send_message("Miniatura e banner precisam ser URLs http/https.", ephemeral=True)
            return
        try:
            accent = parse_hex(cor, DEFAULT_ACCENT)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        config = await get_config(interaction.guild.id)
        panel = config["panel"]
        if titulo is not None:
            panel["title"] = titulo[:200]
        if descricao is not None:
            panel["description"] = descricao[:3500]
        if cor is not None:
            panel["accent"] = accent
        if miniatura is not None:
            panel["thumbnail"] = miniatura.strip() or None
        if banner is not None:
            panel["banner"] = banner.strip() or None
        await set_config(interaction.guild.id, config)

        view = AcademicPanelView(
            title=panel["title"],
            description=panel["description"],
            accent=int(panel.get("accent", DEFAULT_ACCENT)),
            thumbnail=panel.get("thumbnail"),
            banner=panel.get("banner"),
        )
        try:
            await channel.send(view=view)
        except discord.HTTPException:
            await interaction.response.send_message(
                view=status_layout("Falha ao publicar", "Verifique as permissões do bot no canal."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=status_layout("Painel publicado", f"O painel foi enviado para {channel.mention}."),
            ephemeral=True,
        )

    @cargos_group.command(name="configuracao", description="Mostra a configuração atual dos níveis")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def show_config(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await get_config(interaction.guild.id)
        lines: list[str] = []
        for level, data in LEVELS.items():
            role = _role_from_config(interaction.guild, config, level)
            lines.append(f"**{data['label']}**: {role.mention if role else '`não configurado`'}")
        pending_id = int(config.get("pending_role", 0) or 0)
        pending = interaction.guild.get_role(pending_id) if pending_id else None
        channel_id = int(config.get("review_channel", 0) or 0)
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        lines.extend(
            [
                f"**Pendente**: {pending.mention if pending else '`não configurado`'}",
                f"**Canal de revisão**: {channel.mention if channel else '`não configurado`'}",
            ]
        )
        await interaction.response.send_message(
            view=status_layout("Configuração dos cargos", "\n".join(lines)),
            ephemeral=True,
        )

    @cargos_group.command(name="pendentes", description="Lista solicitações acadêmicas pendentes")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def pending_applications(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        records = await store.all(interaction.guild.id, APPLICATION_NS)
        pending = [value for value in records.values() if isinstance(value, dict) and value.get("status") == "pending"]
        pending.sort(key=lambda item: item.get("created_at", ""))
        if not pending:
            description = "Não há solicitações aguardando análise."
        else:
            lines = []
            for app in pending[:20]:
                level = LEVELS.get(app.get("level", ""), {}).get("label", app.get("level", "?"))
                lines.append(f"`{app.get('id')}` • <@{app.get('user_id')}> • **{level}**")
            description = "\n".join(lines)
            if len(pending) > 20:
                description += f"\n\nMais {len(pending) - 20} solicitação(ões) não exibida(s)."
        await interaction.response.send_message(
            view=status_layout("Solicitações pendentes", description),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesReview(bot))
