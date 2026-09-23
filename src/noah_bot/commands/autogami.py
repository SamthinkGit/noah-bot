import asyncio
import json
import re
from contextlib import suppress
from pathlib import Path

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import (
    _parse_embed_metadata,
    build_loading_embed,
    render_autogami_trade_preview,
    render_embeds_to_png,
)
from noah_bot.modules.send_message import delete_message, send_message


MEDIA_DIR = Path(__file__).resolve().parents[3] / "media"
TOKEN_GETTER_ARCHIVE = MEDIA_DIR / "autogami_token_getter.rar"
CONSENT_ACCEPT_EMOJI = "✅"
CONSENT_DECLINE_EMOJI = "❌"
AUTOGAMI_V_BATCH_SIZE = 5
AUTOGAMI_V_DELAY_SECONDS = 6
AUTOGAMI_V_RESPONSE_TIMEOUT_SECONDS = 3
AUTOGAMI_TRADE_DELAY_SECONDS = 5.5
AUTOGAMI_TRADE_VIEW_TIMEOUT_SECONDS = 300
WAIFUGAMI_BOT_USER_ID = 722418701852344391
CHEST_TRIGGER_PATTERN = re.compile(r"\.open\s+<treasure\s+type>", re.IGNORECASE)
USER_MENTION_PATTERN = re.compile(r"^<@!?(\d+)>$")
FAV_POSITION_START = {"inicio", "principio", "start", "first", "-inicio", "-first"}
FAV_POSITION_END = {"final", "fin", "end", "last", "-final", "-last"}
CHEST_TYPE_COLORS = {
    "platinum": 0x424860,
    "bronze": 0xCD8032,
    "silver": 0xAAA9AD,
    "gold": 0xFFD900,
    "diamond": 0xB9F2FF,
    "zeta": 0xFFC0CA,
}
# Colores más lejanos que esto de cualquier cofre conocido se tratan como cofre de evento.
CHEST_COLOR_MAX_DISTANCE = 45


def _build_autogami_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Autogami",
        description="Sincroniza tu token y prueba el envio automatizado.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=".noah autogami sync",
        value="Recibe instrucciones y el RAR para sacar tu token.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami sync <token>",
        value="Borra tu mensaje, pide consentimiento y guarda tu token cifrado.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami test",
        value="Envia `test success` usando tu token sincronizado.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami addfav <emoji> [inicio|final]",
        value="Guarda un emoji favorito para reaccionar a los `Waifu Claimed!`. Por defecto va al final.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami delfav <emoji>",
        value="Borra un emoji de tus favs de Autogami.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami showfavs",
        value="Muestra todos tus emojis favoritos guardados.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami sortfavs <emoji> <emoji> ...",
        value="Reordena tus favs: los emojis indicados pasan al principio en ese orden y el resto se queda detrás.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami chestconfig <emoji>",
        value="Configura el emoji para abrir chest automáticamente. Solo admins/Funcionarios.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami v <num> <num> ... [-merge] [-silent]",
        value="Envia `.v` en tandas de 5 con 6s de espera entre cada tanda. Acepta `-user @user`. Alias: `.nv`, `.nvms`. `-silent` requiere `-merge`.",
        inline=False,
    )
    embed.add_field(
        name=".noah autogami trade <nums...> to @user <nums...>",
        value="Prepara una oferta de trade, recopila previews silenciosas de ambos lados y permite aceptar o declinar con botones.",
        inline=False,
    )
    return embed


async def _send_sync_instructions(ctx: commands.Context) -> None:
    lines = [
        f"{ctx.author.mention} sigue estos pasos para sincronizar Autogami:",
        "1. Descarga el RAR adjunto.",
        "2. Extráelo en cualquier carpeta de tu PC.",
        "3. Ejecuta `token_getter.exe`.",
        "4. Copia el token obtenido.",
        "5. Usa `.noah autogami sync <token>` para guardarlo.",
    ]

    if not TOKEN_GETTER_ARCHIVE.exists():
        await ctx.send(
            "❌ No encuentro `media/autogami_token_getter.rar`, así que no puedo enviarte el asistente ahora mismo."
        )
        return

    await ctx.send(
        "\n".join(lines),
        file=discord.File(TOKEN_GETTER_ARCHIVE, filename=TOKEN_GETTER_ARCHIVE.name),
    )


async def _delete_token_message(ctx: commands.Context) -> bool:
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        await ctx.send(
            f"{ctx.author.mention} no he podido borrar tu mensaje, así que no voy a guardar el token. "
            "Bórralo manualmente y vuelve a intentarlo en un canal donde yo pueda eliminar mensajes."
        )
        return False

    return True


async def _request_sync_consent(ctx: commands.Context) -> bool:
    consent_message = await ctx.send(
        "\n".join(
            [
                f"{ctx.author.mention} antes de guardar tu token debes ser consciente de que Noah no puede:",
                "- Leer tus mensajes privados o usar tu cuenta para espiar conversaciones",
                "- Enviar mensajes por su cuenta sin una acción o consentimiento tuyo",
                "- Usar tu token para otra cosa que no sean automatizaciones concretas de Autogami",
                "",
                "Y Noah solo puede usar mensajes específicos que tú decidas automatizar con Autogami.",
                "",
                f"Reacciona con {CONSENT_ACCEPT_EMOJI} para aceptar o con {CONSENT_DECLINE_EMOJI} para cancelar.",
            ]
        )
    )

    for emoji in (CONSENT_ACCEPT_EMOJI, CONSENT_DECLINE_EMOJI):
        try:
            await consent_message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def reaction_check(reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
        return (
            user.id == ctx.author.id
            and reaction.message.id == consent_message.id
            and str(reaction.emoji) in {CONSENT_ACCEPT_EMOJI, CONSENT_DECLINE_EMOJI}
        )

    try:
        reaction, _ = await ctx.bot.wait_for(
            "reaction_add",
            timeout=120.0,
            check=reaction_check,
        )
    except asyncio.TimeoutError:
        await consent_message.edit(
            content=f"{ctx.author.mention} sincronización cancelada por tiempo de espera."
        )
        return False

    accepted = str(reaction.emoji) == CONSENT_ACCEPT_EMOJI
    if accepted:
        await consent_message.edit(
            content=f"{ctx.author.mention} consentimiento recibido. Guardando token cifrado..."
        )
        return True

    await consent_message.edit(content=f"{ctx.author.mention} sincronización cancelada.")
    return False


async def _send_autogami_test(ctx: commands.Context, token: str) -> tuple[int, str]:
    if ctx.guild is None:
        raise RuntimeError("Autogami test requires a guild channel.")

    return await asyncio.to_thread(
        send_message,
        "test success",
        token,
        str(ctx.author.id),
        str(ctx.guild.id),
        str(ctx.channel.id),
    )


async def _send_autogami_message(
    ctx: commands.Context,
    token: str,
    message: str,
    acting_user_id: int | None = None,
) -> tuple[int, str]:
    if ctx.guild is None:
        raise RuntimeError("Autogami requires a guild channel.")

    return await asyncio.to_thread(
        send_message,
        message,
        token,
        str(acting_user_id if acting_user_id is not None else ctx.author.id),
        str(ctx.guild.id),
        str(ctx.channel.id),
    )


def _normalize_waifu_identifier(value: str) -> str:
    sanitized = value.strip()
    if sanitized.isdigit():
        return str(int(sanitized))
    return sanitized.casefold()


def _display_name_for_user(user: discord.abc.User) -> str:
    return getattr(user, "display_name", getattr(user, "name", str(user.id)))


def _can_manage_autogami_chests(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    return any(role.name == "Funcionarios" for role in member.roles)


def _iter_embed_text_parts(embed: discord.Embed) -> list[str]:
    parts = [
        embed.title or "",
        embed.description or "",
    ]

    if embed.author and embed.author.name:
        parts.append(embed.author.name)

    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)

    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")

    return parts


def _find_chest_embed(message: discord.Message) -> discord.Embed | None:
    if message.author.id != WAIFUGAMI_BOT_USER_ID:
        return None

    for embed in message.embeds:
        if any(
            CHEST_TRIGGER_PATTERN.search(text)
            for text in _iter_embed_text_parts(embed)
        ):
            return embed

    return None


def _split_rgb(color_value: int) -> tuple[int, int, int]:
    return (
        (color_value >> 16) & 0xFF,
        (color_value >> 8) & 0xFF,
        color_value & 0xFF,
    )


def _color_distance(left: int, right: int) -> int:
    left_red, left_green, left_blue = _split_rgb(left)
    right_red, right_green, right_blue = _split_rgb(right)
    return (
        (left_red - right_red) ** 2
        + (left_green - right_green) ** 2
        + (left_blue - right_blue) ** 2
    )


def _resolve_chest_type(embed: discord.Embed) -> str | None:
    if embed.color is None:
        return None

    color_value = embed.color.value
    nearest_type = min(
        CHEST_TYPE_COLORS,
        key=lambda chest_type: _color_distance(
            color_value,
            CHEST_TYPE_COLORS[chest_type],
        ),
    )

    if (
        _color_distance(color_value, CHEST_TYPE_COLORS[nearest_type])
        > CHEST_COLOR_MAX_DISTANCE**2
    ):
        return "event"

    if nearest_type != "zeta":
        return nearest_type

    image_url = ""
    if embed.image and embed.image.url:
        image_url = embed.image.url

    if "zetachest" in image_url.casefold():
        return "zeta"
    return "event"


def _parse_autogami_v_inputs(
    values: tuple[str, ...],
) -> tuple[list[str], set[str], str | None, list[str]]:
    numbers: list[str] = []
    flags: set[str] = set()
    target_user_raw: str | None = None
    errors: list[str] = []

    index = 0
    while index < len(values):
        raw_value = values[index]
        index += 1

        value = raw_value.strip()
        if not value:
            continue

        if value.startswith("-"):
            normalized_flag = value.lower()
            if normalized_flag == "-user":
                while index < len(values) and not values[index].strip():
                    index += 1

                if index >= len(values):
                    errors.append("❌ `-user` requiere una mención o ID de usuario.")
                    break

                target_user_raw = values[index].strip()
                index += 1
                continue

            flags.add(normalized_flag)
            continue

        numbers.append(value)

    return numbers, flags, target_user_raw, errors


def _resolve_autogami_v_target_user(
    ctx: commands.Context,
    raw_target: str | None,
) -> discord.abc.User:
    if raw_target is None:
        return ctx.author

    match = USER_MENTION_PATTERN.fullmatch(raw_target)
    user_id: int | None = None
    if match is not None:
        user_id = int(match.group(1))
    elif raw_target.isdigit():
        user_id = int(raw_target)

    if user_id is not None:
        for mentioned_user in ctx.message.mentions:
            if mentioned_user.id == user_id:
                return mentioned_user

        if ctx.guild is not None:
            member = ctx.guild.get_member(user_id)
            if member is not None:
                return member

    raise ValueError("❌ No he podido resolver el usuario indicado en `-user`.")


def _sanitize_trade_numbers(
    values: list[str],
    *,
    side_label: str,
) -> tuple[list[str], list[str]]:
    sanitized: list[str] = []
    errors: list[str] = []

    for raw_value in values:
        if raw_value.isdigit():
            sanitized.append(str(int(raw_value)))
            continue

        errors.append(
            f"❌ Los IDs de {side_label} deben ser números. Valor inválido: `{raw_value}`."
        )

    return sanitized, errors


def _parse_autogami_trade_inputs(
    values: tuple[str, ...],
) -> tuple[list[str], str | None, list[str], list[str]]:
    cleaned_values = [value.strip() for value in values if value.strip()]
    if not cleaned_values:
        return [], None, [], [
            "❌ Usa `.noah autogami trade <num> <num> ... to @user <num> <num> ...`."
        ]

    separator_indexes = [
        index for index, value in enumerate(cleaned_values) if value.casefold() == "to"
    ]
    if not separator_indexes:
        return [], None, [], [
            "❌ Falta `to`. Usa `.noah autogami trade <nums...> to @user <nums...>`."
        ]

    separator_index = separator_indexes[0]
    if separator_index + 1 >= len(cleaned_values):
        return [], None, [], [
            "❌ Después de `to` debes indicar una mención o ID de usuario."
        ]

    left_raw = cleaned_values[:separator_index]
    target_user_raw = cleaned_values[separator_index + 1]
    right_raw = cleaned_values[separator_index + 2 :]

    left_numbers, left_errors = _sanitize_trade_numbers(
        left_raw,
        side_label="la oferta del usuario que inicia",
    )
    right_numbers, right_errors = _sanitize_trade_numbers(
        right_raw,
        side_label="la oferta del usuario objetivo",
    )

    errors = [*left_errors, *right_errors]
    if not left_numbers and not right_numbers:
        errors.append("❌ El trade debe tener al menos una carta en alguno de los lados.")

    return left_numbers, target_user_raw, right_numbers, errors


def _format_trade_numbers(numbers: list[str]) -> str:
    return " ".join(numbers) if numbers else "Sin cartas"


def _build_autogami_trade_embed(
    initiator: discord.abc.User,
    target_user: discord.abc.User,
    initiator_numbers: list[str],
    target_numbers: list[str],
    *,
    status: str,
    detail: str | None = None,
    preview_url: str | None = None,
) -> discord.Embed:
    titles = {
        "pending": "Autogami Trade",
        "processing": "Autogami Trade en progreso",
        "accepted": "Autogami Trade aceptado",
        "declined": "Autogami Trade declinado",
        "failed": "Autogami Trade fallido",
        "expired": "Autogami Trade expirado",
    }
    colors = {
        "pending": discord.Color.orange(),
        "processing": discord.Color.blurple(),
        "accepted": discord.Color.green(),
        "declined": discord.Color.red(),
        "failed": discord.Color.red(),
        "expired": discord.Color.dark_grey(),
    }
    descriptions = {
        "pending": (
            f"{initiator.mention} propone este trade a {target_user.mention}. "
            f"Solo {target_user.mention} puede aceptarlo o declinarlo."
        ),
        "processing": (
            f"{target_user.mention} ha aceptado. Ejecutando la secuencia automática del trade."
        ),
        "accepted": f"Trade completado entre {initiator.mention} y {target_user.mention}.",
        "declined": f"{target_user.mention} ha declinado la oferta.",
        "failed": "La secuencia automática del trade no pudo completarse.",
        "expired": "La oferta expiró antes de recibir respuesta.",
    }

    embed = discord.Embed(
        title=titles[status],
        description=descriptions[status],
        color=colors[status],
    )
    embed.add_field(
        name=f"Oferta de {_display_name_for_user(initiator)}",
        value=f"`{_format_trade_numbers(initiator_numbers)}`",
        inline=False,
    )
    embed.add_field(
        name=f"Oferta de {_display_name_for_user(target_user)}",
        value=f"`{_format_trade_numbers(target_numbers)}`",
        inline=False,
    )

    if detail:
        embed.add_field(name="Detalle", value=detail, inline=False)

    if preview_url:
        embed.set_image(url=preview_url)

    return embed


def _build_autogami_trade_result_embed(
    initiator: discord.abc.User,
    target_user: discord.abc.User,
    initiator_numbers: list[str],
    target_numbers: list[str],
    *,
    success: bool,
    detail: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Resumen del trade" if success else "Trade interrumpido",
        description=(
            f"{initiator.mention} y {target_user.mention} han completado el trade."
            if success
            else f"El trade entre {initiator.mention} y {target_user.mention} no terminó correctamente."
        ),
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.add_field(
        name=f"{_display_name_for_user(initiator)} entregó",
        value=f"`{_format_trade_numbers(initiator_numbers)}`",
        inline=False,
    )
    embed.add_field(
        name=f"{_display_name_for_user(target_user)} entregó",
        value=f"`{_format_trade_numbers(target_numbers)}`",
        inline=False,
    )
    if detail:
        embed.add_field(name="Detalle", value=detail, inline=False)
    return embed


def _extract_message_id(response_body: str) -> str | None:
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        return None

    message_id = payload.get("id")
    if isinstance(message_id, str) and message_id:
        return message_id
    return None


def _response_matches_batch(message: discord.Message, batch: list[str]) -> bool:
    if message.author.id != WAIFUGAMI_BOT_USER_ID:
        return False

    if not message.embeds or len(message.embeds) != len(batch):
        return False

    expected_ids = {_normalize_waifu_identifier(value) for value in batch}
    local_ids = [
        _parse_embed_metadata(embed).get("local_id")
        for embed in message.embeds
    ]
    parsed_ids = {
        _normalize_waifu_identifier(local_id)
        for local_id in local_ids
        if local_id
    }

    if parsed_ids:
        return parsed_ids == expected_ids

    return True


async def _wait_for_waifugami_response(
    ctx: commands.Context,
    batch: list[str],
) -> discord.Message:
    def message_check(message: discord.Message) -> bool:
        return (
            message.channel.id == ctx.channel.id
            and _response_matches_batch(message, batch)
        )

    return await ctx.bot.wait_for(
        "message",
        timeout=AUTOGAMI_V_RESPONSE_TIMEOUT_SECONDS,
        check=message_check,
    )


async def _send_autogami_merged_batches(
    ctx: commands.Context,
    captured_batches: list[tuple[list[str], list[discord.Embed]]],
) -> None:
    for index, (batch, embeds) in enumerate(captured_batches, start=1):
        buffer = await asyncio.to_thread(render_embeds_to_png, embeds)
        if buffer is None:
            await ctx.send(
                f"⚠️ No pude hacer merge de la respuesta {index}/{len(captured_batches)} "
                f"para `{'.v ' + ' '.join(batch)}` porque no encontré imágenes."
            )
            continue

        await ctx.send(
            file=discord.File(buffer, filename=f"autogami_merge_{index}.png"),
        )


async def _delete_autogami_silent_messages(
    ctx: commands.Context,
    token: str,
    acting_user_id: int,
    sent_message_id: str | None,
    waifugami_response: discord.Message,
) -> None:
    delete_tasks = []

    if sent_message_id and ctx.guild is not None:
        delete_tasks.append(
            asyncio.to_thread(
                delete_message,
                sent_message_id,
                token,
                str(acting_user_id),
                str(ctx.guild.id),
                str(ctx.channel.id),
            )
        )

    delete_tasks.append(waifugami_response.delete())

    results = await asyncio.gather(*delete_tasks, return_exceptions=True)
    _ = results


async def _update_autogami_progress_message(
    progress_message: discord.Message | None,
    completed_batches: int,
    total_batches: int,
) -> None:
    if progress_message is None or total_batches <= 0:
        return

    percent = int(completed_batches * 100 / total_batches)
    embed = build_loading_embed(
        title=f"Autogami {completed_batches}/{total_batches}",
        percent=percent,
    )
    await progress_message.edit(embed=embed)


async def _run_autogami_v_batches(
    ctx: commands.Context,
    target_user: discord.abc.User,
    token: str,
    batches: list[list[str]],
    *,
    capture_embeds: bool,
    silent: bool,
    progress_message: discord.Message | None = None,
) -> list[tuple[list[str], list[discord.Embed]]]:
    captured_batches: list[tuple[list[str], list[discord.Embed]]] = []
    context = get_bot_context(ctx.bot)
    show_progress = progress_message is not None

    for index, batch in enumerate(batches, start=1):
        command_text = f".v {' '.join(batch)}"
        wait_task: asyncio.Task[discord.Message] | None = None
        if capture_embeds:
            wait_task = asyncio.create_task(_wait_for_waifugami_response(ctx, batch))

        try:
            status, body = await _send_autogami_message(
                ctx,
                token,
                command_text,
                acting_user_id=target_user.id,
            )
        except Exception:
            if wait_task is not None:
                wait_task.cancel()
                with suppress(asyncio.CancelledError):
                    await wait_task
            if show_progress:
                with suppress(discord.HTTPException, discord.NotFound):
                    await _update_autogami_progress_message(
                        progress_message, index, len(batches)
                    )
            if index < len(batches):
                await asyncio.sleep(AUTOGAMI_V_DELAY_SECONDS)
            continue

        if not 200 <= status < 300:
            if wait_task is not None:
                wait_task.cancel()
                with suppress(asyncio.CancelledError):
                    await wait_task
            if show_progress:
                with suppress(discord.HTTPException, discord.NotFound):
                    await _update_autogami_progress_message(
                        progress_message, index, len(batches)
                    )
            if index < len(batches):
                await asyncio.sleep(AUTOGAMI_V_DELAY_SECONDS)
            continue

        context.daily_stats.increment_autogami_uses(
            ctx.guild.id,
            ctx.guild.name,
            target_user.id,
            _display_name_for_user(target_user),
        )

        if capture_embeds and wait_task is not None:
            try:
                waifugami_response = await wait_task
            except asyncio.TimeoutError:
                if show_progress:
                    with suppress(discord.HTTPException, discord.NotFound):
                        await _update_autogami_progress_message(
                            progress_message, index, len(batches)
                        )
                if index < len(batches):
                    await asyncio.sleep(AUTOGAMI_V_DELAY_SECONDS)
                continue

            response_embeds = list(waifugami_response.embeds)
            if silent:
                sent_message_id = _extract_message_id(body)
                with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await _delete_autogami_silent_messages(
                        ctx,
                        token,
                        target_user.id,
                        sent_message_id,
                        waifugami_response,
                    )

            captured_batches.append((batch, response_embeds))

        if show_progress:
            with suppress(discord.HTTPException, discord.NotFound):
                await _update_autogami_progress_message(
                    progress_message, index, len(batches)
                )

        if index < len(batches):
            await asyncio.sleep(AUTOGAMI_V_DELAY_SECONDS)

    return captured_batches


def _build_autogami_batches(numbers: list[str]) -> list[list[str]]:
    return [
        numbers[index : index + AUTOGAMI_V_BATCH_SIZE]
        for index in range(0, len(numbers), AUTOGAMI_V_BATCH_SIZE)
    ]


async def _collect_autogami_trade_embeds(
    ctx: commands.Context,
    target_user: discord.abc.User,
    numbers: list[str],
) -> list[discord.Embed]:
    if not numbers:
        return []

    context = get_bot_context(ctx.bot)
    token = context.autogami_tokens.get_token(target_user.id)
    if token is None:
        return []

    captured_batches = await _run_autogami_v_batches(
        ctx,
        target_user,
        token,
        _build_autogami_batches(numbers),
        capture_embeds=True,
        silent=True,
    )

    embeds: list[discord.Embed] = []
    for _, batch_embeds in captured_batches:
        embeds.extend(batch_embeds)
    return embeds


async def _update_trade_loading_message(
    message: discord.Message | None,
    *,
    title: str,
    percent: int,
) -> None:
    if message is None:
        return

    with suppress(discord.HTTPException, discord.NotFound):
        await message.edit(embed=build_loading_embed(title=title, percent=percent))


async def _execute_autogami_trade_sequence(
    ctx: commands.Context,
    initiator: discord.abc.User,
    target_user: discord.abc.User,
    initiator_numbers: list[str],
    target_numbers: list[str],
) -> tuple[bool, str]:
    if ctx.guild is None:
        return False, "El trade solo puede ejecutarse dentro de un servidor."

    context = get_bot_context(ctx.bot)
    initiator_token = context.autogami_tokens.get_token(initiator.id)
    if initiator_token is None:
        return False, f"{initiator.mention} ya no tiene token sincronizado."

    target_token = context.autogami_tokens.get_token(target_user.id)
    if target_token is None:
        return False, f"{target_user.mention} ya no tiene token sincronizado."

    steps: list[tuple[discord.abc.User, str, str]] = [
        (initiator, initiator_token, f".t {target_user.mention}"),
        (target_user, target_token, ".tac"),
    ]
    if initiator_numbers:
        steps.append((initiator, initiator_token, f".ta {' '.join(initiator_numbers)}"))
    if target_numbers:
        steps.append((target_user, target_token, f".ta {' '.join(target_numbers)}"))
    steps.append((initiator, initiator_token, ".tc"))
    steps.append((target_user, target_token, ".tc"))

    for index, (acting_user, token, command_text) in enumerate(steps, start=1):
        try:
            status, body = await _send_autogami_message(
                ctx,
                token,
                command_text,
                acting_user_id=acting_user.id,
            )
        except Exception as exc:
            return (
                False,
                f"Falló el paso {index}/{len(steps)} (`{command_text}`) para "
                f"{_display_name_for_user(acting_user)}: {exc}",
            )

        if not 200 <= status < 300:
            error_body = body[:250] if body else "sin respuesta"
            return (
                False,
                f"El paso {index}/{len(steps)} (`{command_text}`) devolvió HTTP {status}: {error_body}",
            )

        context.daily_stats.increment_autogami_uses(
            ctx.guild.id,
            ctx.guild.name,
            acting_user.id,
            _display_name_for_user(acting_user),
        )

        if index < len(steps):
            await asyncio.sleep(AUTOGAMI_TRADE_DELAY_SECONDS)

    return True, "La secuencia automática terminó correctamente."


class AutogamiTradeView(discord.ui.View):
    def __init__(
        self,
        *,
        ctx: commands.Context,
        initiator: discord.abc.User,
        target_user: discord.abc.User,
        initiator_numbers: list[str],
        target_numbers: list[str],
    ) -> None:
        super().__init__(timeout=AUTOGAMI_TRADE_VIEW_TIMEOUT_SECONDS)
        self.ctx = ctx
        self.initiator = initiator
        self.target_user = target_user
        self.initiator_numbers = initiator_numbers
        self.target_numbers = target_numbers
        self.message: discord.Message | None = None
        self.preview_url: str | None = None
        self.finished = False

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _reject_non_target(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.target_user.id:
            return False

        if interaction.response.is_done():
            await interaction.followup.send(
                f"Solo {self.target_user.mention} puede responder a este trade.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"Solo {self.target_user.mention} puede responder a este trade.",
                ephemeral=True,
            )
        return True

    async def on_timeout(self) -> None:
        if self.finished or self.message is None:
            return

        self._disable_buttons()
        self.finished = True
        with suppress(discord.HTTPException, discord.NotFound):
            await self.message.edit(
                embed=_build_autogami_trade_embed(
                    self.initiator,
                    self.target_user,
                    self.initiator_numbers,
                    self.target_numbers,
                    status="expired",
                    preview_url=self.preview_url,
                ),
                view=self,
            )

    @discord.ui.button(label="Aceptar trade", style=discord.ButtonStyle.success)
    async def accept_trade(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _ = button
        if await self._reject_non_target(interaction):
            return

        self.message = interaction.message
        self.finished = True
        self._disable_buttons()
        await interaction.response.edit_message(
            embed=_build_autogami_trade_embed(
                self.initiator,
                self.target_user,
                self.initiator_numbers,
                self.target_numbers,
                status="processing",
                preview_url=self.preview_url,
            ),
            view=self,
        )

        success, detail = await _execute_autogami_trade_sequence(
            self.ctx,
            self.initiator,
            self.target_user,
            self.initiator_numbers,
            self.target_numbers,
        )
        final_status = "accepted" if success else "failed"
        with suppress(discord.HTTPException, discord.NotFound):
            await interaction.message.edit(
                embed=_build_autogami_trade_embed(
                    self.initiator,
                    self.target_user,
                    self.initiator_numbers,
                    self.target_numbers,
                    status=final_status,
                    detail=detail,
                    preview_url=self.preview_url,
                ),
                view=self,
            )

        await interaction.followup.send(
            embed=_build_autogami_trade_result_embed(
                self.initiator,
                self.target_user,
                self.initiator_numbers,
                self.target_numbers,
                success=success,
                detail=detail,
            )
        )
        self.stop()

    @discord.ui.button(label="Declinar trade", style=discord.ButtonStyle.danger)
    async def decline_trade(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _ = button
        if await self._reject_non_target(interaction):
            return

        self.message = interaction.message
        self.finished = True
        self._disable_buttons()
        await interaction.response.edit_message(
            embed=_build_autogami_trade_embed(
                self.initiator,
                self.target_user,
                self.initiator_numbers,
                self.target_numbers,
                status="declined",
                preview_url=self.preview_url,
            ),
            view=self,
        )
        self.stop()


async def _run_autogami_v(ctx: commands.Context, values: tuple[str, ...]) -> None:
    if ctx.guild is None:
        await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
        return

    if not values:
        await ctx.send(
            "❌ Usa `.noah autogami v <num> <num> <num> ... [-merge] [-silent]` o `.nv <num> <num> ... [-merge] [-silent]`."
        )
        return

    sanitized_numbers, flags, target_user_raw, parse_errors = _parse_autogami_v_inputs(
        values
    )
    if parse_errors:
        await ctx.send(parse_errors[0])
        return

    merge_requested = "-merge" in flags
    silent_requested = "-silent" in flags
    unknown_flags = sorted(flag for flag in flags if flag not in {"-merge", "-silent"})

    if unknown_flags:
        await ctx.send(f"❌ Flag(s) no reconocidos: {' '.join(unknown_flags)}")
        return

    if silent_requested and not merge_requested:
        await ctx.send("❌ `-silent` solo funciona si también usas `-merge`.")
        return

    if not sanitized_numbers:
        await ctx.send(
            "❌ No has indicado números válidos para enviar con `.v`."
        )
        return

    try:
        target_user = _resolve_autogami_v_target_user(ctx, target_user_raw)
    except ValueError as exc:
        await ctx.send(str(exc))
        return

    context = get_bot_context(ctx.bot)
    token = context.autogami_tokens.get_token(target_user.id)
    if token is None:
        if target_user.id == ctx.author.id:
            await ctx.send(
                "❌ No tienes un token sincronizado. Usa `.noah autogami sync` primero."
            )
            return

        await ctx.send(
            f"❌ {target_user.mention} no tiene un token sincronizado en Autogami."
        )
        return

    batches = _build_autogami_batches(sanitized_numbers)
    show_progress = merge_requested and silent_requested
    progress_message: discord.Message | None = None

    if show_progress:
        progress_message = await ctx.send(
            embed=build_loading_embed(title=f"Autogami 0/{len(batches)}", percent=0)
        )

    captured_batches = await _run_autogami_v_batches(
        ctx,
        target_user,
        token,
        batches,
        capture_embeds=merge_requested,
        silent=silent_requested,
        progress_message=progress_message if show_progress else None,
    )

    if show_progress and progress_message is not None:
        with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
            await progress_message.delete()

    if merge_requested and captured_batches:
        await _send_autogami_merged_batches(ctx, captured_batches)


def register_autogami_commands(bot: commands.Bot, noah_group: commands.Group) -> None:
    @bot.listen("on_message")
    async def _handle_autogami_chest_spawn(message: discord.Message) -> None:
        if message.guild is None:
            return

        chest_embed = _find_chest_embed(message)
        if chest_embed is None:
            return

        chest_type = _resolve_chest_type(chest_embed)
        if chest_type is None:
            return

        context = get_bot_context(bot)
        chest_emoji = context.autogami_tokens.get_chest_emoji(message.guild.id)
        if not chest_emoji:
            return

        context.autogami_chest_messages[message.id] = chest_type

        try:
            await message.add_reaction(chest_emoji)
        except discord.HTTPException:
            pass

    @bot.listen("on_reaction_add")
    async def _handle_autogami_chest_reaction(
        reaction: discord.Reaction,
        user: discord.User | discord.Member,
    ) -> None:
        if user.bot or reaction.message.guild is None:
            return

        context = get_bot_context(bot)
        chest_type = context.autogami_chest_messages.get(reaction.message.id)
        if chest_type is None:
            return

        configured_emoji = context.autogami_tokens.get_chest_emoji(
            reaction.message.guild.id
        )
        if configured_emoji is None or str(reaction.emoji) != configured_emoji:
            return

        token = context.autogami_tokens.get_token(user.id)
        if token is None:
            return

        try:
            await asyncio.to_thread(
                send_message,
                f".open {chest_type}",
                token,
                str(user.id),
                str(reaction.message.guild.id),
                str(reaction.message.channel.id),
            )
            context.daily_stats.increment_autogami_uses(
                reaction.message.guild.id,
                reaction.message.guild.name,
                user.id,
                _display_name_for_user(user),
            )
        except Exception:
            return

    @noah_group.group()
    async def autogami(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=_build_autogami_help_embed())

    @autogami.command()
    async def help(ctx: commands.Context) -> None:
        await ctx.send(embed=_build_autogami_help_embed())

    @autogami.command()
    async def sync(ctx: commands.Context, *, token: str | None = None) -> None:
        if not token:
            await _send_sync_instructions(ctx)
            return

        sanitized_token = token.strip()
        if not sanitized_token:
            await _send_sync_instructions(ctx)
            return

        if not await _delete_token_message(ctx):
            return

        if not await _request_sync_consent(ctx):
            return

        context = get_bot_context(ctx.bot)
        context.autogami_tokens.set_token(
            ctx.author.id,
            sanitized_token,
            username=ctx.author.display_name,
        )
        await ctx.send(
            f"{ctx.author.mention} tu token de Autogami se ha sincronizado y guardado cifrado correctamente."
        )

    @autogami.command()
    async def test(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        token = context.autogami_tokens.get_token(ctx.author.id)
        if token is None:
            await ctx.send(
                "❌ No tienes un token sincronizado. Usa `.noah autogami sync` primero."
            )
            return

        try:
            status, body = await _send_autogami_test(ctx, token)
        except Exception as exc:
            await ctx.send(f"❌ Falló el test de Autogami: {exc}")
            return

        if 200 <= status < 300:
            await ctx.send("✅ Test de Autogami enviado correctamente.")
            return

        error_body = body[:300] if body else "sin respuesta"
        await ctx.send(
            f"❌ El test de Autogami falló con estado HTTP {status}: {error_body}"
        )

    @autogami.command()
    async def addfav(
        ctx: commands.Context,
        emoji: str,
        position: str = "final",
    ) -> None:
        normalized_position = position.strip().casefold()
        if normalized_position in FAV_POSITION_START:
            at_start = True
        elif normalized_position in FAV_POSITION_END:
            at_start = False
        else:
            await ctx.send(
                "❌ Usa `.noah autogami addfav <emoji> [inicio|final]`."
            )
            return

        context = get_bot_context(ctx.bot)
        added = context.autogami_tokens.add_favorite_emoji(
            ctx.author.id,
            emoji,
            at_start=at_start,
        )
        if added:
            position_text = "al principio" if at_start else "al final"
            await ctx.send(
                f"{ctx.author.mention} he guardado `{emoji}` {position_text} de tus favs de Autogami."
            )
            return

        await ctx.send(f"{ctx.author.mention} `{emoji}` ya estaba en tus favs de Autogami.")

    @autogami.command()
    async def delfav(ctx: commands.Context, emoji: str) -> None:
        context = get_bot_context(ctx.bot)
        removed = context.autogami_tokens.remove_favorite_emoji(ctx.author.id, emoji)
        if removed:
            await ctx.send(
                f"{ctx.author.mention} he borrado `{emoji}` de tus favs de Autogami."
            )
            return

        await ctx.send(f"{ctx.author.mention} `{emoji}` no estaba en tus favs de Autogami.")

    @autogami.command()
    async def showfavs(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        favorites = context.autogami_tokens.get_favorite_emojis(ctx.author.id)
        if not favorites:
            await ctx.send(
                f"{ctx.author.mention} no tienes favs guardados todavía. Usa `.noah autogami addfav <emoji>`."
            )
            return

        favorites_text = " ".join(favorites)
        await ctx.send(
            f"{ctx.author.mention} tus favs de Autogami son: {favorites_text}"
        )

    @autogami.command(aliases=["reorderfavs"])
    async def sortfavs(ctx: commands.Context, *emojis: str) -> None:
        context = get_bot_context(ctx.bot)
        favorites = context.autogami_tokens.get_favorite_emojis(ctx.author.id)
        if not favorites:
            await ctx.send(
                f"{ctx.author.mention} no tienes favs guardados todavía. Usa `.noah autogami addfav <emoji>`."
            )
            return

        requested = [emoji.strip() for emoji in emojis if emoji.strip()]
        if not requested:
            await ctx.send(
                "❌ Usa `.noah autogami sortfavs <emoji> <emoji> ...`. "
                f"Tu orden actual es: {' '.join(favorites)}"
            )
            return

        missing = [emoji for emoji in requested if emoji not in favorites]
        if missing:
            await ctx.send(
                f"❌ Estos emojis no están en tus favs: {' '.join(missing)}"
            )
            return

        reordered = context.autogami_tokens.reorder_favorite_emojis(
            ctx.author.id,
            requested,
        )
        if reordered is None:
            await ctx.send("❌ No he podido reordenar tus favs.")
            return

        await ctx.send(
            f"{ctx.author.mention} tu nuevo orden de favs es: {' '.join(reordered)}"
        )

    @autogami.command()
    async def chestconfig(ctx: commands.Context, emoji: str) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if (
            not isinstance(ctx.author, discord.Member)
            or not _can_manage_autogami_chests(ctx.author)
        ):
            await ctx.send(
                "❌ Solo administradores o el rol `Funcionarios` pueden usar este comando."
            )
            return

        context = get_bot_context(ctx.bot)
        configured = context.autogami_tokens.set_chest_emoji(ctx.guild.id, emoji)
        if not configured:
            await ctx.send("❌ Debes indicar un emoji válido.")
            return

        await ctx.send(
            f"✅ El emoji automático de chest ha quedado configurado en `{emoji}`."
        )

    @autogami.command()
    async def trade(ctx: commands.Context, *values: str) -> None:
        if ctx.guild is None:
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        initiator_numbers, target_user_raw, target_numbers, parse_errors = (
            _parse_autogami_trade_inputs(values)
        )
        if parse_errors:
            await ctx.send(parse_errors[0])
            return

        try:
            target_user = _resolve_autogami_v_target_user(ctx, target_user_raw)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        if target_user.id == ctx.author.id:
            await ctx.send("❌ No puedes abrir un trade contigo mismo.")
            return

        if getattr(target_user, "bot", False):
            await ctx.send("❌ El usuario objetivo debe ser una persona, no un bot.")
            return

        context = get_bot_context(ctx.bot)
        initiator_token = context.autogami_tokens.get_token(ctx.author.id)
        if initiator_token is None:
            await ctx.send(
                "❌ No tienes un token sincronizado. Usa `.noah autogami sync` primero."
            )
            return

        target_token = context.autogami_tokens.get_token(target_user.id)
        if target_token is None:
            await ctx.send(
                f"❌ {target_user.mention} no tiene un token sincronizado en Autogami."
            )
            return

        loading_message = await ctx.send(
            embed=build_loading_embed(title="Trade 0/2", percent=0)
        )

        try:
            await _update_trade_loading_message(
                loading_message,
                title=f"Recopilando oferta de {_display_name_for_user(ctx.author)}",
                percent=25,
            )
            initiator_embeds = await _collect_autogami_trade_embeds(
                ctx,
                ctx.author,
                initiator_numbers,
            )

            if initiator_numbers and target_numbers:
                await asyncio.sleep(AUTOGAMI_TRADE_DELAY_SECONDS)

            await _update_trade_loading_message(
                loading_message,
                title=f"Recopilando oferta de {_display_name_for_user(target_user)}",
                percent=65,
            )
            target_embeds = await _collect_autogami_trade_embeds(
                ctx,
                target_user,
                target_numbers,
            )

            await _update_trade_loading_message(
                loading_message,
                title="Preparando preview del trade",
                percent=100,
            )

            preview_buffer: discord.File | None = None
            embed_preview_url: str | None = None
            preview_detail: str | None = None
            try:
                preview_png = await asyncio.to_thread(
                    render_autogami_trade_preview,
                    _display_name_for_user(ctx.author),
                    initiator_numbers,
                    initiator_embeds,
                    _display_name_for_user(target_user),
                    target_numbers,
                    target_embeds,
                )
                preview_buffer = discord.File(
                    preview_png,
                    filename="autogami_trade_preview.png",
                )
                embed_preview_url = "attachment://autogami_trade_preview.png"
            except Exception:
                preview_detail = (
                    "No pude generar la preview completa del trade, así que la oferta se envía "
                    "con visualización reducida."
                )
                try:
                    fallback_preview = await asyncio.to_thread(
                        render_autogami_trade_preview,
                        _display_name_for_user(ctx.author),
                        initiator_numbers,
                        [],
                        _display_name_for_user(target_user),
                        target_numbers,
                        [],
                    )
                    preview_buffer = discord.File(
                        fallback_preview,
                        filename="autogami_trade_preview.png",
                    )
                    embed_preview_url = "attachment://autogami_trade_preview.png"
                except Exception:
                    preview_buffer = None
                    embed_preview_url = None

            view = AutogamiTradeView(
                ctx=ctx,
                initiator=ctx.author,
                target_user=target_user,
                initiator_numbers=initiator_numbers,
                target_numbers=target_numbers,
            )
            trade_embed = _build_autogami_trade_embed(
                ctx.author,
                target_user,
                initiator_numbers,
                target_numbers,
                status="pending",
                detail=preview_detail,
                preview_url=embed_preview_url,
            )
            send_kwargs = {
                "embed": trade_embed,
                "view": view,
            }
            if preview_buffer is not None:
                send_kwargs["file"] = preview_buffer

            try:
                sent_message = await ctx.send(
                    **send_kwargs,
                )
            except Exception:
                sent_message = await ctx.send(
                    embed=_build_autogami_trade_embed(
                        ctx.author,
                        target_user,
                        initiator_numbers,
                        target_numbers,
                        status="pending",
                        detail=(
                            "No pude adjuntar la preview visual, pero la oferta de trade sí quedó creada."
                        ),
                    ),
                    view=view,
                )
            view.message = sent_message
            if sent_message.attachments:
                view.preview_url = sent_message.attachments[0].url
        except Exception as exc:
            await ctx.send(
                embed=_build_autogami_trade_embed(
                    ctx.author,
                    target_user,
                    initiator_numbers,
                    target_numbers,
                    status="failed",
                    detail=f"El trade no pudo prepararse: `{exc}`",
                )
            )
        finally:
            with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                await loading_message.delete()

    @autogami.command(name="v")
    async def autogami_v(ctx: commands.Context, *values: str) -> None:
        await _run_autogami_v(ctx, values)

    @bot.command(name="nv")
    async def autogami_v_alias(ctx: commands.Context, *values: str) -> None:
        await _run_autogami_v(ctx, values)

    @bot.command(name="nvms")
    async def autogami_v_merge_silent_alias(ctx: commands.Context, *values: str) -> None:
        await _run_autogami_v(ctx, (*values, "-merge", "-silent"))
