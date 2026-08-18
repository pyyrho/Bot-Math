"""Avisos privados enviados pela equipe através do bot.

O destinatário recebe uma mensagem em Components V2 sem a identidade do
administrador. A autoria real permanece registrada no armazenamento interno e,
opcionalmente, em um canal de logs configurado pela administração.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import store
from utils.ui_v2 import DEFAULT_ACCENT, build_layout, status_layout

log = logging.getLogger("cogs.avisos_dm")

CONFIG_NS = "dm_notice_config"
NOTICE_NS = "dm_notices"
MAX_SAVED_NOTICES = 250
SEND_COOLDOWN_SECONDS = 5.0
NOTICE_ACCENT = 0xF1C40F


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def _quote(value: str, *, limit: int = 1800) -> str:
    """Formata texto como citação sem ultrapassar o limite do painel."""
    cleaned = value.strip()[:limit]
    return "\n".join(f"> {line}" if line else ">" for line in cleaned.splitlines())


def _relative_time(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value))
        return f"<t:{int(moment.timestamp())}:R>"
    except (TypeError, ValueError, OverflowError):
        return "horário indisponível"


def _default_config() -> dict[str, Any]:
    return {"log_channel_id": None}


def _audit_channel_error(
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> str | None:
    """Impede que autoria e conteúdo de moderação vazem em canal público."""
    if channel.permissions_for(guild.default_role).view_channel:
        return "O canal de auditoria precisa ser privado para o cargo @everyone."

    me = guild.me
    if me is None:
        return "Não consegui verificar minhas permissões nesse canal."
    permissions = channel.permissions_for(me)
    if not permissions.view_channel or not permissions.send_messages:
        return "Preciso das permissões Ver canal e Enviar mensagens nesse canal."
    return None


async def _get_config(guild_id: int) -> dict[str, Any]:
    saved = await store.get(guild_id, CONFIG_NS, "config", None)
    config = _default_config()
    if isinstance(saved, dict):
        config.update(saved)
    return config


async def _save_config(guild_id: int, config: dict[str, Any]) -> None:
    config["updated_at"] = _utc_now()
    await store.set(guild_id, CONFIG_NS, "config", config)


@dataclass(slots=True)
class DeliveryResult:
    status: str
    notice_id: str
    detail: str

    @property
    def sent(self) -> bool:
        return self.status == "sent"


def _recipient_layout(
    guild: discord.Guild,
    *,
    notice_id: str,
    subject: str,
    message: str,
) -> discord.ui.LayoutView:
    thumbnail = str(guild.icon.url) if guild.icon else None
    description = (
        f"Você recebeu uma mensagem privada da equipe do servidor **{guild.name}**.\n\n"
        f"## {subject}\n"
        f"{_quote(message)}"
    )
    return build_layout(
        title="Aviso Mod.",
        description=description,
        accent=NOTICE_ACCENT,
        thumbnail=thumbnail,
        footer=(
            f"Código do aviso: {notice_id} · Para falar com a moderação, "
            "acesse o ModMail do servidor."
        ),
        timeout=None,
    )


class NoticeActionButton(discord.ui.Button):
    def __init__(self, action: str) -> None:
        if action == "confirm":
            super().__init__(
                label="Confirmar envio",
                style=discord.ButtonStyle.success,
                emoji="✉️",
            )
        else:
            super().__init__(
                label="Cancelar",
                style=discord.ButtonStyle.secondary,
                emoji="✖️",
            )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, NoticePreviewView):
            await interaction.response.send_message(
                "Esta confirmação não está mais disponível.", ephemeral=True
            )
            return
        await view.handle_action(interaction, self.action)


class NoticePreviewView(discord.ui.LayoutView):
    """Prévia efêmera vinculada ao administrador que abriu o formulário."""

    def __init__(
        self,
        *,
        guild_id: int,
        author_id: int,
        target_id: int,
        target_name: str,
        subject: str,
        message: str,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.author_id = author_id
        self.target_id = target_id
        self.subject = subject
        self.message = message
        self._finished = False
        self._action_lock = asyncio.Lock()

        target_label = discord.utils.escape_markdown(target_name)[:80]
        description = (
            f"**Destinatário:** {target_label} (`{target_id}`)\n"
            f"**Assunto:** {subject}\n\n"
            f"{_quote(message, limit=1200)}\n\n"
            "-# Seu perfil não aparecerá na DM. A autoria ficará disponível somente na auditoria administrativa."
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# Confirmar aviso privado\n{description}"),
                discord.ui.ActionRow(
                    NoticeActionButton("confirm"),
                    NoticeActionButton("cancel"),
                ),
                accent_color=NOTICE_ACCENT,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id != self.author_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Somente quem abriu esta prévia pode utilizá-la.", ephemeral=True
            )
            return False
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Sua permissão de administrador não está mais ativa.", ephemeral=True
            )
            return False
        return True

    async def handle_action(self, interaction: discord.Interaction, action: str) -> None:
        async with self._action_lock:
            if self._finished:
                await interaction.response.send_message(
                    "Este aviso já foi processado.", ephemeral=True
                )
                return

            if action == "cancel":
                self._finished = True
                self.stop()
                await interaction.response.edit_message(
                    view=status_layout(
                        "Envio cancelado",
                        "Nenhuma mensagem foi enviada ao membro.",
                        accent=0x95A5A6,
                    )
                )
                return

            self._finished = True
            await interaction.response.defer(ephemeral=True)
            cog = interaction.client.get_cog("AvisosDM")
            if not isinstance(cog, AvisosDM):
                self._finished = False
                await interaction.edit_original_response(
                    view=status_layout(
                        "Sistema indisponível",
                        "O módulo de avisos privados não está carregado.",
                        accent=0xE74C3C,
                    )
                )
                return

            try:
                result = await cog.deliver_notice(
                    interaction,
                    target_id=self.target_id,
                    subject=self.subject,
                    message=self.message,
                )
            except Exception:
                log.exception("Falha inesperada ao confirmar um aviso privado")
                self.stop()
                await interaction.edit_original_response(
                    view=status_layout(
                        "Falha inesperada",
                        "O aviso não foi concluído. Nada deve ser reenviado sem uma nova confirmação.",
                        accent=0xE74C3C,
                    )
                )
                return
            self.stop()
            if result.sent:
                view = status_layout(
                    "Aviso enviado",
                    f"A DM foi entregue com sucesso. Código de auditoria: `{result.notice_id}`.",
                    accent=0x2ECC71,
                )
            else:
                view = status_layout(
                    "Aviso não enviado",
                    f"{result.detail}\n\nCódigo da tentativa: `{result.notice_id}`.",
                    accent=0xE74C3C,
                )
            await interaction.edit_original_response(view=view)


class ComposeNoticeModal(discord.ui.Modal, title="Escrever aviso privado"):
    subject_input = discord.ui.TextInput(
        label="Assunto",
        placeholder="Ex.: Orientação da equipe",
        min_length=3,
        max_length=100,
    )
    message_input = discord.ui.TextInput(
        label="Mensagem",
        placeholder="Escreva o aviso que o membro receberá na DM.",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1800,
    )

    def __init__(self, *, guild_id: int, author_id: int, target_id: int) -> None:
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.author_id = author_id
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.author_id
            or interaction.guild_id != self.guild_id
            or not _is_admin(interaction)
        ):
            await interaction.response.send_message(
                "Esta ação está restrita a administradores.", ephemeral=True
            )
            return

        assert interaction.guild is not None
        target = interaction.guild.get_member(self.target_id)
        if target is None:
            await interaction.response.send_message(
                "O membro não está mais no servidor.", ephemeral=True
            )
            return
        if target.bot:
            await interaction.response.send_message(
                "Avisos privados só podem ser enviados para membros humanos.",
                ephemeral=True,
            )
            return

        subject = str(self.subject_input.value).strip()
        message = str(self.message_input.value).strip()
        await interaction.response.send_message(
            view=NoticePreviewView(
                guild_id=interaction.guild.id,
                author_id=interaction.user.id,
                target_id=target.id,
                target_name=target.display_name,
                subject=subject,
                message=message,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class AvisosDM(commands.Cog):
    """Envio administrativo e auditável de mensagens privadas."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._send_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_send: dict[tuple[int, int], float] = {}

    async def _save_record(self, guild_id: int, record: dict[str, Any]) -> None:
        await store.set(guild_id, NOTICE_NS, record["id"], record)
        records = await store.all(guild_id, NOTICE_NS)
        if len(records) <= MAX_SAVED_NOTICES:
            return

        ordered = sorted(
            (
                value
                for value in records.values()
                if isinstance(value, dict) and value.get("id")
            ),
            key=lambda item: str(item.get("created_at", "")),
        )
        excess = max(0, len(records) - MAX_SAVED_NOTICES)
        for old_record in ordered[:excess]:
            await store.delete(guild_id, NOTICE_NS, str(old_record["id"]))

    async def _send_audit_log(
        self,
        guild: discord.Guild,
        record: dict[str, Any],
    ) -> None:
        config = await _get_config(guild.id)
        raw_channel_id = config.get("log_channel_id")
        try:
            channel_id = int(raw_channel_id) if raw_channel_id else 0
        except (TypeError, ValueError):
            channel_id = 0
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        privacy_error = _audit_channel_error(guild, channel)
        if privacy_error:
            log.error(
                "Log do aviso %s não publicado: %s",
                record.get("id"),
                privacy_error,
            )
            return

        status_label = {
            "sent": "Entregue",
            "dm_closed": "DM fechada",
            "failed": "Falha no Discord",
        }.get(str(record.get("status")), str(record.get("status", "desconhecido")))
        description = (
            f"**Código:** `{record['id']}`\n"
            f"**Destinatário:** <@{record['target_id']}> (`{record['target_id']}`)\n"
            f"**Responsável:** <@{record['sender_id']}> (`{record['sender_id']}`)\n"
            f"**Assunto:** {record['subject']}\n"
            f"**Resultado:** {status_label}\n\n"
            f"{_quote(str(record['message']), limit=700)}"
        )
        try:
            await channel.send(
                view=build_layout(
                    title="Registro de aviso privado",
                    description=description,
                    accent=0x2ECC71 if record.get("status") == "sent" else 0xE74C3C,
                    footer="A identidade do responsável é visível apenas neste registro administrativo.",
                    timeout=None,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Falha ao publicar o log do aviso %s", record.get("id"))

    async def _record_attempt(
        self,
        guild: discord.Guild,
        *,
        notice_id: str,
        target_id: int,
        sender_id: int,
        subject: str,
        message: str,
        status: str,
        dm_message_id: int | None = None,
    ) -> None:
        record = {
            "id": notice_id,
            "target_id": target_id,
            "sender_id": sender_id,
            "subject": subject,
            "message": message,
            "status": status,
            "dm_message_id": dm_message_id,
            "created_at": _utc_now(),
        }
        try:
            await self._save_record(guild.id, record)
        except Exception:
            log.exception("Falha ao persistir o aviso %s", notice_id)
        try:
            await self._send_audit_log(guild, record)
        except Exception:
            log.exception("Falha inesperada no log do aviso %s", notice_id)

    async def deliver_notice(
        self,
        interaction: discord.Interaction,
        *,
        target_id: int,
        subject: str,
        message: str,
    ) -> DeliveryResult:
        guild = interaction.guild
        notice_id = secrets.token_hex(4).upper()
        if not guild or not _is_admin(interaction):
            return DeliveryResult("denied", notice_id, "Sua permissão administrativa não pôde ser confirmada.")

        target = guild.get_member(target_id)
        if target is None:
            return DeliveryResult("missing", notice_id, "O membro não está mais no servidor.")
        if target.bot:
            return DeliveryResult("invalid", notice_id, "O destinatário não pode ser um bot.")

        lock_key = (guild.id, interaction.user.id)
        lock = self._send_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            elapsed = now - self._last_send.get(lock_key, 0.0)
            if elapsed < SEND_COOLDOWN_SECONDS:
                remaining = max(1, int(SEND_COOLDOWN_SECONDS - elapsed + 0.999))
                return DeliveryResult(
                    "cooldown",
                    notice_id,
                    f"Aguarde {remaining}s antes de enviar outro aviso.",
                )
            self._last_send[lock_key] = now

            # A autoria precisa estar registrada antes do envio. Assim, uma
            # indisponibilidade do banco/arquivo nunca gera uma DM sem auditoria.
            pending_record = {
                "id": notice_id,
                "target_id": target.id,
                "sender_id": interaction.user.id,
                "subject": subject,
                "message": message,
                "status": "pending",
                "dm_message_id": None,
                "created_at": _utc_now(),
            }
            try:
                await self._save_record(guild.id, pending_record)
            except Exception:
                log.exception("Auditoria indisponível antes do aviso %s", notice_id)
                return DeliveryResult(
                    "audit_unavailable",
                    notice_id,
                    "O armazenamento de auditoria está indisponível; por segurança, a DM não foi enviada.",
                )

            status = "sent"
            dm_message_id: int | None = None
            try:
                sent_message = await target.send(
                    view=_recipient_layout(
                        guild,
                        notice_id=notice_id,
                        subject=subject,
                        message=message,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                dm_message_id = sent_message.id
            except discord.Forbidden:
                status = "dm_closed"
            except discord.HTTPException:
                status = "failed"
                log.exception("Falha do Discord ao enviar aviso %s", notice_id)

            await self._record_attempt(
                guild,
                notice_id=notice_id,
                target_id=target.id,
                sender_id=interaction.user.id,
                subject=subject,
                message=message,
                status=status,
                dm_message_id=dm_message_id,
            )

        if status == "sent":
            return DeliveryResult(status, notice_id, "A mensagem foi entregue.")
        if status == "dm_closed":
            return DeliveryResult(
                status,
                notice_id,
                "O membro está com as mensagens diretas fechadas ou bloqueou o bot.",
            )
        return DeliveryResult(
            status,
            notice_id,
            "O Discord recusou o envio. Tente novamente em alguns instantes.",
        )

    aviso_group = app_commands.Group(
        name="aviso",
        description="Avisos privados e anônimos enviados pela administração",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @aviso_group.command(name="enviar", description="Envia uma DM institucional sem revelar seu perfil")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(membro="Membro que receberá o aviso privado")
    async def send_notice(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
    ) -> None:
        if membro.bot:
            await interaction.response.send_message(
                view=status_layout(
                    "Destinatário inválido",
                    "Escolha um membro humano do servidor.",
                    accent=0xE74C3C,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ComposeNoticeModal(
                guild_id=interaction.guild_id or 0,
                author_id=interaction.user.id,
                target_id=membro.id,
            )
        )

    @aviso_group.command(name="configurar_logs", description="Define o canal administrativo que receberá os registros")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        canal="Canal privado de auditoria",
        remover="Remove o canal configurado",
    )
    async def configure_logs(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
        remover: bool = False,
    ) -> None:
        assert interaction.guild is not None
        config = await _get_config(interaction.guild.id)
        if remover:
            config["log_channel_id"] = None
            description = "O canal de auditoria foi removido. Os registros continuarão salvos internamente."
        elif canal is not None:
            privacy_error = _audit_channel_error(interaction.guild, canal)
            if privacy_error:
                await interaction.response.send_message(
                    view=status_layout(
                        "Canal de auditoria inseguro",
                        privacy_error,
                        accent=0xE74C3C,
                    ),
                    ephemeral=True,
                )
                return
            config["log_channel_id"] = canal.id
            description = f"Os próximos registros de avisos serão enviados para {canal.mention}."
        else:
            await interaction.response.send_message(
                view=status_layout(
                    "Configuração incompleta",
                    "Informe um canal ou marque `remover: true`.",
                    accent=0xE74C3C,
                ),
                ephemeral=True,
            )
            return

        await _save_config(interaction.guild.id, config)
        await interaction.response.send_message(
            view=status_layout("Auditoria configurada", description),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @aviso_group.command(name="historico", description="Consulta os últimos avisos privados da administração")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(membro="Filtra os registros por destinatário")
    async def notice_history(
        self,
        interaction: discord.Interaction,
        membro: discord.Member | None = None,
    ) -> None:
        assert interaction.guild is not None
        records = await store.all(interaction.guild.id, NOTICE_NS)
        notices = [value for value in records.values() if isinstance(value, dict)]
        if membro is not None:
            notices = [item for item in notices if int(item.get("target_id", 0) or 0) == membro.id]
        notices.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)

        if not notices:
            description = "Nenhum aviso privado foi encontrado para esse filtro."
        else:
            lines: list[str] = []
            for item in notices[:15]:
                status_icon = "✅" if item.get("status") == "sent" else "❌"
                lines.append(
                    f"{status_icon} `{item.get('id', '?')}` · <@{item.get('target_id', 0)}> · "
                    f"**{str(item.get('subject', 'Sem assunto'))[:80]}** · "
                    f"{_relative_time(item.get('created_at'))}"
                )
            description = "\n".join(lines)
            if len(notices) > 15:
                description += f"\n\nMais {len(notices) - 15} registro(s) não exibido(s)."

        await interaction.response.send_message(
            view=build_layout(
                title="Histórico de avisos privados",
                description=description,
                accent=DEFAULT_ACCENT,
                footer="Consulta visível somente para você.",
                timeout=180,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AvisosDM(bot))
