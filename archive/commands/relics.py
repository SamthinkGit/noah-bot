import asyncio
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context, is_guard_active
from noah_bot.modules.discord_formatter import with_delete_button
from noah_bot.modules.relics_game import (
    LINK_COOLDOWN_SECONDS,
    RELIC_ORDER,
    RELIC_TYPES,
    RelicsGameManager,
    resolve_relic_type,
)


MEDIA_DIR = Path(__file__).resolve().parents[3] / "media"
ALERT_POLL_SECONDS = 5


def _can_manage_relics(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    return any(role.name == "Funcionarios" for role in member.roles)


def _format_pv(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _format_remaining(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    if minutes == 0:
        return f"{secs}s"
    return f"{minutes}m {secs}s"


def _relic_image_file(relic: dict) -> Optional[discord.File]:
    image_path = MEDIA_DIR / relic["image_name"]
    if not image_path.exists():
        return None
    return discord.File(image_path, filename=image_path.name)


def _relic_image_name(relic: dict) -> Optional[str]:
    image_path = MEDIA_DIR / relic["image_name"]
    if not image_path.exists():
        return None
    return image_path.name


def _build_relic_embed(relic: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{relic['title']} · Tier {relic['tier']}",
        color=discord.Color(relic["color"]),
    )

    winner_id = relic.get("claimed_by")

    if relic.get("auto_claim") and winner_id:
        embed.description = (
            f"La reliquia ha elegido automáticamente a <@{winner_id}> y le ha entregado "
            f"**+{relic['reward_essence']} EE**."
        )
    elif winner_id:
        embed.description = (
            f"La reliquia ya ha sido vinculada. La recompensa de "
            f"**+{relic['reward_essence']} EE** ha sido entregada."
        )
    else:
        embed.description = None

    if not relic.get("auto_claim"):
        embed.add_field(
            name="Vinculación por intento",
            value=f"`+{_format_pv(relic['link_value'])}pv`",
            inline=True,
        )

    embed.add_field(
        name="Recompensa",
        value=f"`+{relic['reward_essence']} EE`",
        inline=True,
    )

    lines = [
        f"→ <@{link['user_id']}>: `{_format_pv(link['pv'])}pv`"
        for link in relic.get("linkers", [])
    ]

    embed.add_field(
        name="Jugadores vinculados",
        value="\n".join(lines) if lines else "Nadie se ha vinculado todavía.",
        inline=False,
    )

    if winner_id:
        embed.add_field(
            name="Estado",
            value=f"La reliquia se ha vinculado a <@{winner_id}>.",
            inline=False,
        )

    image_name = _relic_image_name(relic)
    if image_name is not None:
        embed.set_thumbnail(url=f"attachment://{image_name}")

    return embed


def _build_inventory_embed(user: discord.Member | discord.User, inventory: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"Inventario de reliquias de {user.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="Extractos de esencia",
        value=f"`{inventory['essence_extracts']}` EE",
        inline=False,
    )

    linked_counts = inventory.get("linked_counts", {})
    lines = []
    for relic_key in RELIC_ORDER:
        relic = RELIC_TYPES[relic_key]
        lines.append(f"**{relic.title}:** {linked_counts.get(relic_key, 0)}")

    embed.add_field(
        name="Reliquias vinculadas",
        value="\n".join(lines),
        inline=False,
    )

    return embed


def _build_explain_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Noah Relics",
        description="Una reliquia activa aparece y los jugadores compiten por vincularla.",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="Cómo funciona",
        value=(
            "1. Usa `.noah relics spawn` para invocar una reliquia.\n"
            "2. Usa `.noah relics link` para sumar pv a tu probabilidad.\n"
            "3. Si la vinculación sale, ganas extractos de esencia."
        ),
        inline=False,
    )
    embed.add_field(
        name="Reglas rápidas",
        value=(
            "Solo puede haber una reliquia activa a la vez.\n"
            "Cuando una reliquia termina o se cancela, puede spawnearse otra.\n"
            f"Cada `link` tiene un cooldown de **{LINK_COOLDOWN_SECONDS // 60} minutos**.\n"
            "Si spameas `link` durante el cooldown, hay un 50% de probabilidad de quedar desvinculado."
        ),
        inline=False,
    )
    embed.add_field(
        name="Comandos",
        value=(
            "`.noah relics spawn`\n"
            "`.noah relics link`\n"
            "`.noah relics alert`\n"
            "`.noah relics remaining`\n"
            "`.noah relics showprobs`\n"
            "`.noah relics inv [@usuario]`\n"
            "`.noah relics sacrifice`\n"
            "`.noah relics gift <cantidad> @usuario`\n"
            "`.noah relics explain`"
        ),
        inline=False,
    )
    return embed


def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Comandos de Noah Relics",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="Comandos",
        value=(
            "`.noah relics spawn` Invoca una reliquia si no hay otra activa.\n"
            "`.noah relics link` Intenta vincularte a la reliquia activa.\n"
            "`.noah relics alert` Activa o desactiva avisos cuando puedas volver a usar `link`.\n"
            "`.noah relics remaining` Muestra tu cooldown actual de vinculación.\n"
            "`.noah relics showprobs` Muestra las probabilidades del modo.\n"
            "`.noah relics inv [@usuario]` Muestra tu inventario o el de otro usuario.\n"
            "`.noah relics sacrifice` Sacrifica tus pv actuales en la reliquia activa.\n"
            "`.noah relics gift <cantidad> @usuario` Regala EE a otro usuario.\n"
            "`.noah relics explain` Explica brevemente cómo se juega.\n"
            "`.noah relics help` Muestra esta lista de comandos.\n"
            "`.noah relics forcespawn <tipo>` Fuerza un spawn. Solo admins/Funcionarios.\n"
            "`.noah relics cancelspawn` Cancela la reliquia activa. Solo admins/Funcionarios."
        ),
        inline=False,
    )
    return embed


def _build_probabilities_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Probabilidades de Noah Relics",
        color=discord.Color.orange(),
        description=(
            "Los `pv` son porcentaje directo de vinculación acumulada. "
            "Si tienes `0.5pv`, tienes un `0.5%` de probabilidad; si llegas a `3pv`, tienes un `3%`."
        ),
    )

    spawn_lines = []
    for relic_key in RELIC_ORDER:
        relic = RELIC_TYPES[relic_key]
        spawn_lines.append(
            f"**{relic.title}:** `{relic.spawn_weight}%` spawn, "
            f"`+{relic.reward_essence} EE`"
        )

    embed.add_field(
        name="Probabilidades de aparición",
        value="\n".join(spawn_lines),
        inline=False,
    )

    link_lines = []
    for relic_key in RELIC_ORDER:
        relic = RELIC_TYPES[relic_key]
        if relic.auto_claim:
            link_lines.append(
                f"**{relic.title}:** se vincula automáticamente al invocador."
            )
            continue

        link_lines.append(
            f"**{relic.title}:** `+{_format_pv(relic.link_value)}pv` por intento"
        )

    embed.add_field(
        name="Probabilidad de vinculación",
        value="\n".join(link_lines),
        inline=False,
    )

    embed.add_field(
        name="Ejemplo",
        value=(
            "Si sale un **Amuleto**, cada `link` suma `0.5pv`.\n"
            "Tras 1 intento tienes `0.5%`.\n"
            "Tras 2 intentos tienes `1%`.\n"
            "Si haces spam durante el cooldown, hay un `50%` de quedar desvinculado."
        ),
        inline=False,
    )

    return embed


async def _get_active_message(
    bot: commands.Bot,
    manager: RelicsGameManager,
) -> tuple[Optional[dict], Optional[discord.Message]]:
    relic = manager.get_active_relic()
    if relic is None:
        return None, None

    channel_id = relic.get("channel_id")
    message_id = relic.get("message_id")
    if not channel_id or not message_id:
        return relic, None

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden):
            manager.clear_active_relic()
            return None, None
        except discord.HTTPException:
            return relic, None

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return relic, None

    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        manager.clear_active_relic()
        return None, None
    except (discord.Forbidden, discord.HTTPException):
        return relic, None

    return relic, message


async def _refresh_relic_message(message: discord.Message, relic: dict) -> None:
    try:
        await message.edit(embed=_build_relic_embed(relic))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def _send_relic_message(
    channel: discord.abc.Messageable,
    relic: dict,
) -> discord.Message:
    embed = _build_relic_embed(relic)
    image_file = _relic_image_file(relic)

    if image_file is None:
        return await channel.send(embed=embed)

    return await channel.send(embed=embed, file=image_file)


async def _resolve_alert_channel(
    bot: commands.Bot,
    channel_id: int | None,
) -> Optional[discord.TextChannel | discord.Thread]:
    if channel_id is None:
        return None

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return None

    return channel


async def _delete_alert_message(
    bot: commands.Bot,
    alert: Optional[dict],
) -> None:
    if not alert or not alert.get("message_id") or not alert.get("channel_id"):
        return

    channel = await _resolve_alert_channel(bot, alert.get("channel_id"))
    if channel is None:
        return

    try:
        await channel.get_partial_message(int(alert["message_id"])).delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def _cleanup_alert_messages(
    bot: commands.Bot,
    alerts: list[dict],
) -> None:
    for alert in alerts:
        await _delete_alert_message(bot, alert)


async def _dispatch_link_alert(
    bot: commands.Bot,
    manager: RelicsGameManager,
    alert: dict,
) -> bool:
    channel = await _resolve_alert_channel(bot, alert.get("channel_id"))
    if channel is None:
        return False

    if is_guard_active(bot, getattr(getattr(channel, "guild", None), "id", None)):
        return False

    try:
        message = await channel.send(
            f"<@{alert['user_id']}> ya puedes volver a usar `.noah relics link`."
        )
    except (discord.Forbidden, discord.HTTPException):
        return False

    manager.set_link_alert_message(alert["user_id"], message.id)
    return True


async def _process_relic_alerts(bot: commands.Bot) -> None:
    context = get_bot_context(bot)
    manager = context.relics_manager

    for alert in manager.get_active_link_alerts():
        cooldown = manager.get_link_cooldown_remaining(alert["user_id"])
        if cooldown["ready"]:
            if not alert.get("message_id"):
                await _dispatch_link_alert(bot, manager, alert)
            continue

        if alert.get("message_id"):
            await _delete_alert_message(bot, alert)
            manager.set_link_alert_message(alert["user_id"], None)


async def _relic_alert_loop(bot: commands.Bot) -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await _process_relic_alerts(bot)
        except Exception:
            pass

        await asyncio.sleep(ALERT_POLL_SECONDS)


def _ensure_relic_alert_loop(bot: commands.Bot) -> None:
    task = getattr(bot, "_noah_relics_alert_task", None)
    if task is not None and not task.done():
        return

    bot._noah_relics_alert_task = asyncio.create_task(_relic_alert_loop(bot))


async def register_relic_cleanup(
    ctx: commands.Context,
    reaction_emoji: str = "🔗",
) -> None:
    try:
        await ctx.message.add_reaction(reaction_emoji)
    except discord.HTTPException:
        pass

    await asyncio.sleep(3)

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


def register_relics_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @bot.listen("on_ready")
    async def _start_relic_alert_loop() -> None:
        _ensure_relic_alert_loop(bot)

    @noah_group.group()
    async def relics(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Usa `.noah relics help` para ver cómo funciona.")

    @relics.command()
    async def spawn(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo se puede usar dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        manager = context.relics_manager

        active_relic, active_message = await _get_active_message(ctx.bot, manager)
        if active_relic is not None:
            if active_message is not None:
                try:
                    await active_message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

            sent_message = await _send_relic_message(ctx.channel, active_relic)
            manager.set_active_message(sent_message.id, ctx.channel.id)
            return

        result = manager.spawn_relic(
            user_id=str(ctx.author.id),
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
        )

        if not result["ok"]:
            await ctx.send("❌ No se ha podido invocar la reliquia.")
            return

        relic = result["relic"]
        sent_message = await _send_relic_message(ctx.channel, relic)

        if result["code"] == "spawned":
            manager.set_active_message(sent_message.id, ctx.channel.id)

    @relics.command()
    async def link(ctx: commands.Context) -> None:
        _ensure_relic_alert_loop(ctx.bot)
        context = get_bot_context(ctx.bot)
        manager = context.relics_manager
        active_relic, active_message = await _get_active_message(ctx.bot, manager)

        if active_relic is None:
            await ctx.send("❌ No hay ninguna reliquia activa ahora mismo.", delete_after=8)
            return

        result = manager.link_user(str(ctx.author.id))
        updated_relic = result.get("relic")
        await _delete_alert_message(ctx.bot, result.get("cleared_alert"))

        if not result["ok"]:
            if result["code"] == "cooldown":
                if result.get("was_unlinked") and updated_relic is not None and active_message is not None:
                    await _refresh_relic_message(active_message, updated_relic)
                    await ctx.send(
                        (
                            "💀 Has intentado forzar la vinculación durante el cooldown y "
                            "la reliquia te ha desvinculado."
                        ),
                        delete_after=8,
                    )
                    return

                await ctx.send(
                    (
                        "⏳ Aún no puedes volver a vincularte. "
                        f"Te faltan `{_format_remaining(result['seconds_left'])}`."
                    ),
                    delete_after=8,
                )
                return

            await ctx.send("❌ No se ha podido procesar la vinculación.", delete_after=8)
            return

        if updated_relic is not None and active_message is not None:
            await _refresh_relic_message(active_message, updated_relic)

        if result["code"] == "claimed":
            await _cleanup_alert_messages(ctx.bot, result.get("cleanup_alerts", []))

        await register_relic_cleanup(ctx)

    @relics.command()
    async def inv(
        ctx: commands.Context,
        user: discord.Member | discord.User | None = None,
    ) -> None:
        context = get_bot_context(ctx.bot)
        target_user = user or ctx.author
        inventory = context.relics_manager.get_user_inventory(str(target_user.id))
        await ctx.send(embed=_build_inventory_embed(target_user, inventory))

    @relics.command()
    async def alert(ctx: commands.Context) -> None:
        _ensure_relic_alert_loop(ctx.bot)
        context = get_bot_context(ctx.bot)
        manager = context.relics_manager

        if manager.get_active_relic() is None:
            await ctx.send("❌ No hay ninguna reliquia activa ahora mismo.")
            return

        result = manager.toggle_link_alert(str(ctx.author.id), ctx.channel.id)
        if not result["ok"]:
            await ctx.send("❌ No se ha podido actualizar la alerta.")
            return

        if result["code"] == "disabled":
            await _delete_alert_message(ctx.bot, result.get("alert"))
            await register_relic_cleanup(ctx, "🔕")
            return

        await _process_relic_alerts(ctx.bot)
        await register_relic_cleanup(ctx, "🔔")

    @relics.command()
    @with_delete_button()
    async def remaining(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        cooldown = context.relics_manager.get_link_cooldown_remaining(str(ctx.author.id))

        if cooldown["ready"]:
            await ctx.send("✅ Ya puedes volver a usar `.noah relics link`.")
            return

        await ctx.send(
            "⏳ Te quedan "
            f"`{_format_remaining(cooldown['seconds_left'])}` "
            "para volver a vincularte."
        )

    @relics.command()
    async def sacrifice(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        manager = context.relics_manager
        active_relic, active_message = await _get_active_message(ctx.bot, manager)

        if active_relic is None:
            await ctx.send("❌ No hay ninguna reliquia activa que sacrificar.")
            return

        result = manager.sacrifice_link(str(ctx.author.id))
        if not result["ok"]:
            if result["code"] == "not_linked":
                await ctx.send("❌ No estás vinculado a la reliquia activa.")
                return

            await ctx.send("❌ No se ha podido realizar el sacrificio.")
            return

        updated_relic = result["relic"]
        if active_message is not None:
            await _refresh_relic_message(active_message, updated_relic)

        await ctx.send(
            (
                f"🩸 Has sacrificado `{_format_pv(result['lost_pv'])}pv`. "
                f"Se han restado `{_format_pv(result['removed_from_others'])}pv` "
                "entre los demás vinculados."
            )
        )

    @relics.command()
    async def gift(
        ctx: commands.Context,
        quantity: int,
        user: discord.Member,
    ) -> None:
        context = get_bot_context(ctx.bot)
        result = context.relics_manager.gift_essence(
            from_user_id=str(ctx.author.id),
            to_user_id=str(user.id),
            quantity=quantity,
        )

        if not result["ok"]:
            if result["code"] == "invalid_quantity":
                await ctx.send("❌ La cantidad debe ser mayor que 0.")
                return
            if result["code"] == "same_user":
                await ctx.send("❌ No puedes regalarte EE a ti mismo.")
                return
            if result["code"] == "insufficient_funds":
                await ctx.send(
                    f"❌ No tienes suficientes EE. Saldo actual: `{result['current_balance']}`."
                )
                return

            await ctx.send("❌ No se ha podido completar el regalo.")
            return

        await ctx.send(
            (
                f"🎁 Has enviado `{quantity} EE` a {user.mention}. "
                f"Tu saldo ahora es `{result['sender_balance']} EE`."
            )
        )

    @relics.command()
    async def forcespawn(ctx: commands.Context, *, relic_type: str) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo se puede usar dentro de un servidor.")
            return

        if not _can_manage_relics(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        resolved_type = resolve_relic_type(relic_type)
        if resolved_type is None:
            valid_types = ", ".join(RELIC_TYPES[key].title for key in RELIC_ORDER)
            await ctx.send(f"❌ Tipo inválido. Opciones: {valid_types}.")
            return

        context = get_bot_context(ctx.bot)
        manager = context.relics_manager
        active_relic, _ = await _get_active_message(ctx.bot, manager)
        if active_relic is not None:
            await ctx.send("❌ Ya hay una reliquia activa. Usa `.noah relics cancelspawn` primero.")
            return

        result = manager.spawn_relic(
            user_id=str(ctx.author.id),
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            forced_type=resolved_type,
            ignore_daily_limit=True,
        )

        if not result["ok"]:
            await ctx.send("❌ No se ha podido forzar la reliquia.")
            return

        relic = result["relic"]
        sent_message = await _send_relic_message(ctx.channel, relic)

        if result["code"] == "spawned":
            manager.set_active_message(sent_message.id, ctx.channel.id)

    @relics.command()
    async def cancelspawn(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo se puede usar dentro de un servidor.")
            return

        if not _can_manage_relics(ctx.author):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        context = get_bot_context(ctx.bot)
        manager = context.relics_manager
        relic, message = await _get_active_message(ctx.bot, manager)

        if relic is None:
            await ctx.send("❌ No hay ninguna reliquia activa.")
            return

        cleanup_alerts = manager.get_active_link_alerts()
        manager.clear_active_relic()

        if message is not None:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        await _cleanup_alert_messages(ctx.bot, cleanup_alerts)

        await ctx.send("✅ La reliquia activa ha sido cancelada.")

    @relics.command()
    async def explain(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_explain_embed())

    @relics.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_help_embed())

    @relics.command()
    async def showprobs(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_probabilities_embed())
