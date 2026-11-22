from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.noah_gochi import (
    DEFAULT_STATES,
    RELATION_AXES,
    RELATION_LABELS,
    TRAIT_LABELS,
)
from noah_bot.modules.noah_gochi_ai import (
    CharacterProfile,
    StoryRequest,
)


TRAIT_EMOJIS = {
    "1️⃣": "height",
    "2️⃣": "build",
    "3️⃣": "sociability",
    "4️⃣": "quirkiness",
    "5️⃣": "favorite_color",
    "6️⃣": "nickname",
    "7️⃣": "strength",
    "8️⃣": "speed",
}
FINISH_EMOJI = "✅"
CUSTOMIZATION_EMOJIS = [*TRAIT_EMOJIS.keys(), FINISH_EMOJI]
STATE_LABELS = {
    "energy": "Energia",
    "happiness": "Felicidad",
    "confidence": "Confianza",
    "social_battery": "Bateria social",
}
STATE_DESCRIPTIONS = {
    "energy": "Physical or mental energy available after the event.",
    "happiness": "Emotional wellbeing or mood after the event.",
    "confidence": "How secure the character feels about themselves.",
    "social_battery": "How much social capacity the character has left.",
}
DELTA_LIMITS = {
    "min": -20,
    "max": 20,
}


@dataclass(slots=True, frozen=True)
class TraitPrompt:
    key: str
    prompt: str


TRAIT_PROMPTS = {
    "height": TraitPrompt(
        key="height",
        prompt="Escribe la altura. Ejemplos: `alto`, `bajita`, `1.80`, `mediana`.",
    ),
    "build": TraitPrompt(
        key="build",
        prompt="Escribe la complexion. Ejemplos: `atletico`, `delgado`, `fuerte`.",
    ),
    "sociability": TraitPrompt(
        key="sociability",
        prompt="Escribe `sociable` o `introvertido`.",
    ),
    "quirkiness": TraitPrompt(
        key="quirkiness",
        prompt="Escribe `peculiar` o `comun`.",
    ),
    "favorite_color": TraitPrompt(
        key="favorite_color",
        prompt="Escribe el color preferido. Ejemplos: `negro`, `azul`, `verde lima`.",
    ),
    "nickname": TraitPrompt(
        key="nickname",
        prompt="Escribe el apodo que quieres guardar.",
    ),
    "strength": TraitPrompt(
        key="strength",
        prompt="Escribe `fuerte` o `debil`.",
    ),
    "speed": TraitPrompt(
        key="speed",
        prompt="Escribe `rapido` o `lento`.",
    ),
}


def _require_guild(ctx: commands.Context) -> bool:
    return ctx.guild is not None


def _is_admin(ctx: commands.Context) -> bool:
    return isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator


def _normalize_trait_value(trait_key: str, raw_value: str) -> str:
    value = " ".join(raw_value.split()).strip()
    lowered = value.lower()

    binary_aliases = {
        "sociability": {
            "sociable": "Sociable",
            "introvertido": "Introvertido",
            "introvertida": "Introvertido",
        },
        "quirkiness": {
            "peculiar": "Peculiar",
            "comun": "Comun",
            "común": "Comun",
        },
        "strength": {
            "fuerte": "Fuerte",
            "debil": "Debil",
            "débil": "Debil",
        },
        "speed": {
            "rapido": "Rapido",
            "rápido": "Rapido",
            "lento": "Lento",
        },
    }

    aliases = binary_aliases.get(trait_key)
    if aliases is not None:
        return aliases.get(lowered, value.capitalize())

    return value


def _format_traits(character: dict) -> str:
    lines = []
    traits = character.get("traits", {})
    for trait_key, label in TRAIT_LABELS.items():
        value = traits.get(trait_key) or "Sin definir"
        lines.append(f"**{label}:** {value}")
    return "\n".join(lines)


def _format_states(states: dict[str, int]) -> str:
    lines = []
    for state_key, label in STATE_LABELS.items():
        lines.append(
            f"**{label}:** `{int(states.get(state_key, DEFAULT_STATES[state_key]))}`"
        )
    return "\n".join(lines)


def _format_state_delta_lines(deltas: dict[str, int]) -> str:
    if not deltas:
        return "Sin cambios."

    lines = []
    for state_key, label in STATE_LABELS.items():
        delta = int(deltas.get(state_key, 0))
        if delta == 0:
            continue
        lines.append(f"{label}: `{delta:+d}`")

    return "\n".join(lines) if lines else "Sin cambios."


def _format_relation_delta_lines(deltas: dict[str, int]) -> str:
    if not deltas:
        return "Sin cambios."

    lines = []
    for axis_key, left_label, _right_label in RELATION_AXES:
        delta = int(deltas.get(axis_key, 0))
        if delta == 0:
            continue
        lines.append(f"{left_label}: `{delta:+d}`")

    return "\n".join(lines) if lines else "Sin cambios."


def _build_character_embed(
    member: discord.Member | discord.User,
    character: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Noah Gochi · {character['name']}",
        description=f"Resumen del personaje de {member.mention}.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Perfil", value=_format_traits(character), inline=False)
    embed.add_field(
        name="Estados actuales",
        value=_format_states(character.get("states", {})),
        inline=False,
    )

    image_url = character.get("image_url")
    if image_url:
        embed.set_image(url=image_url)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)

    return embed


def _build_customization_embed(
    member: discord.Member | discord.User,
    character: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Configura a {character['name']}",
        description=(
            "Reacciona para editar un atributo.\n"
            "`1️⃣` Altura\n"
            "`2️⃣` Complexion\n"
            "`3️⃣` Sociable / Introvertido\n"
            "`4️⃣` Peculiar / Comun\n"
            "`5️⃣` Color preferido\n"
            "`6️⃣` Apodo\n"
            "`7️⃣` Fuerte / Debil\n"
            "`8️⃣` Rapido / Lento\n"
            f"`{FINISH_EMOJI}` Terminar"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.add_field(name="Estado actual", value=_format_traits(character), inline=False)
    return embed


def _build_relation_embed(
    author: discord.Member,
    target: discord.Member,
    author_character: dict,
    target_character: dict,
    relation_scores: dict[str, int],
    relation_summary: str,
    recent_changes: list[dict],
    last_interaction: dict | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Relacion: {author_character['name']} 🤝 {target_character['name']}",
        description=relation_summary,
        color=discord.Color.blurple(),
    )

    lines = []
    for axis_key, left_label, right_label in RELATION_AXES:
        score = int(relation_scores.get(axis_key, 0))
        lines.append(f"**{left_label} ⟷ {right_label}:** `{score:+d}`")
    embed.add_field(name="Atributos", value="\n".join(lines), inline=False)

    if recent_changes:
        recent_lines = []
        for change in reversed(recent_changes[-3:]):
            axis_key = change.get("axis")
            if axis_key not in RELATION_LABELS:
                continue
            left_label, _right_label = RELATION_LABELS[axis_key]
            recent_lines.append(f"{left_label}: `{int(change.get('delta', 0)):+d}`")
        if recent_lines:
            embed.add_field(
                name="Ultimos cambios",
                value="\n".join(recent_lines),
                inline=False,
            )

    if last_interaction and last_interaction.get("summary"):
        embed.add_field(
            name="Ultima interaccion",
            value=str(last_interaction["summary"]),
            inline=False,
        )

    embed.set_image(url=target_character.get("image_url") or target.display_avatar.url)
    embed.set_thumbnail(url=author_character.get("image_url") or author.display_avatar.url)
    embed.set_footer(text=f"{author.display_name} y {target.display_name}")
    return embed


def _extract_image_url_from_message(message: discord.Message) -> str | None:
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        if content_type.startswith("image/") or attachment.filename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            return attachment.url

    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url

    return None


def _build_story_request(
    *,
    guild_name: str,
    mode: str,
    actor_snapshot: dict,
    target_snapshot: dict | None,
    server_topics: list[str],
    relation_snapshot: dict | None,
) -> StoryRequest:
    relation_definitions = {
        axis_key: {
            "positive": left_label,
            "negative": right_label,
        }
        for axis_key, left_label, right_label in RELATION_AXES
    }

    actor_profile = CharacterProfile(
        user_id=actor_snapshot["user_id"],
        name=actor_snapshot["name"],
        traits=dict(actor_snapshot["traits"]),
        states=dict(actor_snapshot["states"]),
    )

    target_profile = None
    if target_snapshot is not None:
        target_profile = CharacterProfile(
            user_id=target_snapshot["user_id"],
            name=target_snapshot["name"],
            traits=dict(target_snapshot["traits"]),
            states=dict(target_snapshot["states"]),
        )

    relation_snapshot = relation_snapshot or {}
    return StoryRequest(
        mode=mode,
        guild_name=guild_name,
        server_topics=list(server_topics),
        actor=actor_profile,
        target=target_profile,
        state_definitions=dict(STATE_DESCRIPTIONS),
        relation_definitions=relation_definitions,
        delta_limits=dict(DELTA_LIMITS),
        relation_scores=dict(relation_snapshot.get("scores", {})),
        recent_history=list(relation_snapshot.get("recent_history", [])),
    )


async def _wait_for_trait_message(
    ctx: commands.Context,
    trait_key: str,
) -> str | None:
    prompt = TRAIT_PROMPTS[trait_key]
    await ctx.send(prompt.prompt)

    def check(message: discord.Message) -> bool:
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
        )

    try:
        response = await ctx.bot.wait_for("message", timeout=120, check=check)
    except asyncio.TimeoutError:
        return None

    value = _normalize_trait_value(trait_key, response.content)
    return value if value.strip() else None


async def _run_customization_flow(
    ctx: commands.Context,
    character: dict,
) -> None:
    manager = get_bot_context(ctx.bot).gochi_manager
    menu_message = await ctx.send(embed=_build_customization_embed(ctx.author, character))

    for emoji in CUSTOMIZATION_EMOJIS:
        try:
            await menu_message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def reaction_check(reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
        return (
            reaction.message.id == menu_message.id
            and user.id == ctx.author.id
            and str(reaction.emoji) in CUSTOMIZATION_EMOJIS
        )

    while True:
        try:
            reaction, user = await ctx.bot.wait_for(
                "reaction_add",
                timeout=180,
                check=reaction_check,
            )
        except asyncio.TimeoutError:
            await ctx.send(
                "⏳ Se cerró el menu de configuracion. Lo que ya guardaste se mantiene."
            )
            return

        selected_emoji = str(reaction.emoji)
        try:
            await menu_message.remove_reaction(reaction.emoji, user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        if selected_emoji == FINISH_EMOJI:
            latest = manager.get_character(ctx.guild.id, ctx.author.id)
            await menu_message.edit(embed=_build_customization_embed(ctx.author, latest))
            await ctx.send("✅ Noah Gochi guardado.")
            return

        trait_key = TRAIT_EMOJIS[selected_emoji]
        value = await _wait_for_trait_message(ctx, trait_key)
        if value is None:
            await ctx.send("⏳ No recibí respuesta a tiempo para ese atributo.")
            continue

        updated = manager.update_trait(ctx.guild.id, ctx.author.id, trait_key, value)
        if updated is None:
            await ctx.send("❌ No he encontrado tu Noah Gochi para actualizarlo.")
            return

        await menu_message.edit(embed=_build_customization_embed(ctx.author, updated))
        await ctx.send(f"✅ {TRAIT_LABELS[trait_key]} actualizado a `{value}`.")


async def _wait_for_interaction_choice(
    ctx: commands.Context,
    message: discord.Message,
    allowed_emojis: set[str],
) -> str | None:
    def reaction_check(reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
        return (
            reaction.message.id == message.id
            and user.id == ctx.author.id
            and str(reaction.emoji) in allowed_emojis
        )

    try:
        reaction, _user = await ctx.bot.wait_for(
            "reaction_add",
            timeout=120,
            check=reaction_check,
        )
    except asyncio.TimeoutError:
        return None

    return str(reaction.emoji)


def register_gotchi_commands(noah_group: commands.Group) -> None:
    @noah_group.group(invoke_without_command=True)
    async def gochi(ctx: commands.Context) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        manager = get_bot_context(ctx.bot).gochi_manager
        character = manager.get_character(ctx.guild.id, ctx.author.id)
        if character is None:
            await ctx.send("❌ Aun no tienes Noah Gochi. Usa `.noah gochi new <nombre>`.")
            return

        await ctx.send(embed=_build_character_embed(ctx.author, character))

    @gochi.command()
    async def new(ctx: commands.Context, *, name: str) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        sanitized_name = " ".join(name.split()).strip()
        if not sanitized_name:
            await ctx.send("❌ Debes indicar un nombre. Ejemplo: `.noah gochi new Sam`")
            return

        manager = get_bot_context(ctx.bot).gochi_manager
        character = manager.create_or_update_character(
            guild_id=ctx.guild.id,
            user_id=ctx.author.id,
            name=sanitized_name,
        )
        await ctx.send(f"🧸 Noah Gochi preparado para {ctx.author.mention}: **{sanitized_name}**.")
        await _run_customization_flow(ctx, character)

    @gochi.command()
    async def setimage(ctx: commands.Context) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        manager = get_bot_context(ctx.bot).gochi_manager
        character = manager.get_character(ctx.guild.id, ctx.author.id)
        if character is None:
            await ctx.send("❌ Primero crea tu Noah Gochi con `.noah gochi new <nombre>`.")
            return

        if not ctx.message.reference:
            await ctx.send("❌ Responde a un mensaje que tenga una imagen.")
            return

        try:
            replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.NotFound:
            await ctx.send("❌ No he podido encontrar el mensaje original.")
            return

        image_url = _extract_image_url_from_message(replied)
        if image_url is None:
            await ctx.send("❌ No encontré ninguna imagen en ese mensaje.")
            return

        updated = manager.set_character_image(ctx.guild.id, ctx.author.id, image_url)
        if updated is None:
            await ctx.send("❌ No he encontrado tu Noah Gochi.")
            return

        await ctx.send("🖼️ Imagen del Noah Gochi actualizada.")

    @gochi.command()
    async def daily(ctx: commands.Context) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        manager = context.gochi_manager
        actor_snapshot = manager.build_profile_snapshot(ctx.guild.id, ctx.author.id)
        if actor_snapshot is None:
            await ctx.send("❌ Primero crea tu Noah Gochi con `.noah gochi new <nombre>`.")
            return

        cooldown = manager.get_daily_cooldown_remaining(ctx.guild.id, ctx.author.id)
        if not cooldown["ready"]:
            hours = cooldown["seconds_left"] // 3600
            minutes = (cooldown["seconds_left"] % 3600) // 60
            await ctx.send(
                f"⏳ Tu daily estara disponible de nuevo en `{hours}h {minutes}m`."
            )
            return

        target_user_id = manager.choose_daily_target(ctx.guild.id, ctx.author.id)
        if target_user_id is None:
            await ctx.send("❌ No hay Noah Gochis disponibles para el daily.")
            return

        is_internal = str(target_user_id) == str(ctx.author.id)
        target_snapshot = None
        relation_snapshot = None

        if not is_internal:
            target_snapshot = manager.build_profile_snapshot(ctx.guild.id, target_user_id)
            relation_snapshot = manager.build_relation_snapshot(
                ctx.guild.id,
                ctx.author.id,
                target_user_id,
            )

        request = _build_story_request(
            guild_name=ctx.guild.name,
            mode="daily_internal" if is_internal else "daily_external",
            actor_snapshot=actor_snapshot,
            target_snapshot=target_snapshot,
            server_topics=manager.get_topics(ctx.guild.id),
            relation_snapshot=relation_snapshot,
        )

        response = context.gochi_story_service.generate_daily_story(request)

        embed = discord.Embed(
            title=response.title,
            description=response.scene_description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Resultado", value=response.result_text, inline=False)
        embed.add_field(
            name="Impacto en la relacion",
            value=response.relation_impact_text,
            inline=False,
        )
        embed.add_field(
            name="Cambios de estado",
            value=_format_state_delta_lines(response.actor_state_deltas),
            inline=False,
        )

        if target_snapshot is not None:
            embed.add_field(
                name=f"Cambios de {target_snapshot['name']}",
                value=_format_state_delta_lines(response.target_state_deltas),
                inline=False,
            )
            target_member = ctx.guild.get_member(int(target_user_id))
            if target_snapshot.get("image_url"):
                embed.set_image(url=target_snapshot["image_url"])
            elif target_member is not None:
                embed.set_image(url=target_member.display_avatar.url)
        elif actor_snapshot.get("image_url"):
            embed.set_image(url=actor_snapshot["image_url"])

        embed.set_footer(text="Cooldown: 24h")
        await ctx.send(embed=embed)

        manager.apply_state_deltas(
            ctx.guild.id,
            ctx.author.id,
            response.actor_state_deltas,
        )
        if target_snapshot is not None:
            manager.apply_state_deltas(
                ctx.guild.id,
                target_user_id,
                response.target_state_deltas,
            )
            manager.apply_relation_update(
                ctx.guild.id,
                ctx.author.id,
                target_user_id,
                response.relation_deltas,
                summary=response.last_interaction_summary or response.result_text,
                source="daily",
            )

        manager.mark_daily_used(ctx.guild.id, ctx.author.id)

    @gochi.command()
    async def interact(
        ctx: commands.Context,
        user: discord.Member | None = None,
    ) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        context = get_bot_context(ctx.bot)
        manager = context.gochi_manager
        actor_snapshot = manager.build_profile_snapshot(ctx.guild.id, ctx.author.id)
        if actor_snapshot is None:
            await ctx.send("❌ Primero crea tu Noah Gochi con `.noah gochi new <nombre>`.")
            return

        if user is None:
            candidates = [
                user_id
                for user_id in manager.list_character_user_ids(ctx.guild.id)
                if user_id != str(ctx.author.id)
            ]
            if not candidates:
                await ctx.send("❌ No hay otro Noah Gochi disponible para interactuar.")
                return
            target_user_id = context.gochi_manager.rng.choice(candidates)
            target_member = ctx.guild.get_member(int(target_user_id))
            if target_member is None:
                await ctx.send("❌ No he podido resolver el usuario objetivo.")
                return
        else:
            if user.id == ctx.author.id:
                await ctx.send("❌ Usa `.noah gochi daily` para eventos internos contigo mismo.")
                return
            target_member = user
            target_user_id = str(user.id)

        target_snapshot = manager.build_profile_snapshot(ctx.guild.id, target_user_id)
        if target_snapshot is None:
            await ctx.send("❌ Ese usuario aun no tiene Noah Gochi.")
            return

        cooldown = manager.get_interaction_cooldown_remaining(
            ctx.guild.id,
            ctx.author.id,
            target_user_id,
        )
        if not cooldown["ready"]:
            minutes = cooldown["seconds_left"] // 60
            seconds = cooldown["seconds_left"] % 60
            await ctx.send(
                "⏳ Ya has interactuado con este Noah Gochi en la ultima hora. "
                f"Vuelve a intentarlo en `{minutes}m {seconds}s`."
            )
            return

        relation_snapshot = manager.build_relation_snapshot(
            ctx.guild.id,
            ctx.author.id,
            target_user_id,
        )
        request = _build_story_request(
            guild_name=ctx.guild.name,
            mode="interaction",
            actor_snapshot=actor_snapshot,
            target_snapshot=target_snapshot,
            server_topics=manager.get_topics(ctx.guild.id),
            relation_snapshot=relation_snapshot,
        )
        response = context.gochi_story_service.generate_interaction_story(request)

        embed = discord.Embed(
            title=response.title,
            description=response.scene_description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Situacion", value=response.result_text, inline=False)

        option_map = {option.emoji: option for option in response.options}
        option_lines = [
            f"{option.emoji} **{option.label}** - {option.description}"
            for option in response.options
        ]
        embed.add_field(name="Opciones", value="\n".join(option_lines), inline=False)

        if target_snapshot.get("image_url"):
            embed.set_image(url=target_snapshot["image_url"])
        else:
            embed.set_image(url=target_member.display_avatar.url)

        message = await ctx.send(embed=embed)
        for emoji in option_map:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

        selected_emoji = await _wait_for_interaction_choice(
            ctx,
            message,
            set(option_map.keys()),
        )
        if selected_emoji is None:
            await ctx.send("⏳ La interaccion se quedo sin reaccion y no aplico cambios.")
            return

        selected_option = option_map[selected_emoji]
        resolved_embed = discord.Embed(
            title=response.title,
            description=response.scene_description,
            color=discord.Color.blurple(),
        )
        resolved_embed.add_field(
            name="Decision",
            value=f"{selected_option.emoji} **{selected_option.label}**\n{selected_option.description}",
            inline=False,
        )
        resolved_embed.add_field(
            name="Impacto en la relacion",
            value=_format_relation_delta_lines(selected_option.relation_deltas),
            inline=False,
        )
        resolved_embed.add_field(
            name=f"Cambios de {actor_snapshot['name']}",
            value=_format_state_delta_lines(selected_option.actor_state_deltas),
            inline=False,
        )
        resolved_embed.add_field(
            name=f"Cambios de {target_snapshot['name']}",
            value=_format_state_delta_lines(selected_option.target_state_deltas),
            inline=False,
        )
        if target_snapshot.get("image_url"):
            resolved_embed.set_image(url=target_snapshot["image_url"])
        else:
            resolved_embed.set_image(url=target_member.display_avatar.url)

        await message.edit(embed=resolved_embed)

        manager.apply_state_deltas(
            ctx.guild.id,
            ctx.author.id,
            selected_option.actor_state_deltas,
        )
        manager.apply_state_deltas(
            ctx.guild.id,
            target_user_id,
            selected_option.target_state_deltas,
        )
        manager.apply_relation_update(
            ctx.guild.id,
            ctx.author.id,
            target_user_id,
            selected_option.relation_deltas,
            summary=(
                f"{response.last_interaction_summary} "
                f"{actor_snapshot['name']} eligio `{selected_option.label}`."
            ),
            source="interact",
        )
        manager.mark_interaction_used(ctx.guild.id, ctx.author.id, target_user_id)

    @gochi.command()
    async def test(
        ctx: commands.Context,
        user: discord.Member | None = None,
    ) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        if not _is_admin(ctx):
            await ctx.send("❌ Solo los administradores pueden usar este comando.")
            return

        context = get_bot_context(ctx.bot)
        manager = context.gochi_manager
        actor_snapshot = manager.build_profile_snapshot(ctx.guild.id, ctx.author.id)
        if actor_snapshot is None:
            await ctx.send("❌ Primero crea tu Noah Gochi con `.noah gochi new <nombre>`.")
            return

        if user is None:
            candidates = [
                user_id
                for user_id in manager.list_character_user_ids(ctx.guild.id)
                if user_id != str(ctx.author.id)
            ]
            if not candidates:
                await ctx.send("❌ No hay otro Noah Gochi disponible para probar.")
                return
            target_user_id = context.gochi_manager.rng.choice(candidates)
            target_member = ctx.guild.get_member(int(target_user_id))
            if target_member is None:
                await ctx.send("❌ No he podido resolver el usuario objetivo.")
                return
        else:
            if user.id == ctx.author.id:
                await ctx.send("❌ El test de interaccion requiere otro usuario.")
                return
            target_member = user
            target_user_id = str(user.id)

        target_snapshot = manager.build_profile_snapshot(ctx.guild.id, target_user_id)
        if target_snapshot is None:
            await ctx.send("❌ Ese usuario aun no tiene Noah Gochi.")
            return

        relation_snapshot = manager.build_relation_snapshot(
            ctx.guild.id,
            ctx.author.id,
            target_user_id,
        )
        request = _build_story_request(
            guild_name=ctx.guild.name,
            mode="interaction",
            actor_snapshot=actor_snapshot,
            target_snapshot=target_snapshot,
            server_topics=manager.get_topics(ctx.guild.id),
            relation_snapshot=relation_snapshot,
        )

        try:
            response = context.gochi_story_service.generate_interaction_story(request)
        except Exception as exc:
            await ctx.send(f"❌ El test de Noah Gochi falló al generar la interaccion: {exc}")
            return

        if not response.options:
            await ctx.send("❌ El test generó una interaccion sin opciones.")
            return

        selected_option = context.gochi_manager.rng.choice(response.options)
        embed = discord.Embed(
            title=f"{response.title} [TEST]",
            description=response.scene_description,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Situacion", value=response.result_text, inline=False)
        embed.add_field(
            name="Decision simulada",
            value=(
                f"{selected_option.emoji} **{selected_option.label}**\n"
                f"{selected_option.description}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Impacto simulado en la relacion",
            value=_format_relation_delta_lines(selected_option.relation_deltas),
            inline=False,
        )
        embed.add_field(
            name=f"Cambios simulados de {actor_snapshot['name']}",
            value=_format_state_delta_lines(selected_option.actor_state_deltas),
            inline=False,
        )
        embed.add_field(
            name=f"Cambios simulados de {target_snapshot['name']}",
            value=_format_state_delta_lines(selected_option.target_state_deltas),
            inline=False,
        )
        if target_snapshot.get("image_url"):
            embed.set_image(url=target_snapshot["image_url"])
        else:
            embed.set_image(url=target_member.display_avatar.url)
        embed.set_footer(text="Modo test: no guarda cambios, no consume cooldowns.")
        await ctx.send(embed=embed)

    @gochi.command()
    async def relation(ctx: commands.Context, user: discord.Member) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        manager = get_bot_context(ctx.bot).gochi_manager
        author_character = manager.get_character(ctx.guild.id, ctx.author.id)
        target_character = manager.get_character(ctx.guild.id, user.id)

        if author_character is None or target_character is None:
            await ctx.send("❌ Ambos usuarios deben tener un Noah Gochi creado.")
            return

        relation = manager.get_or_create_relation(ctx.guild.id, ctx.author.id, user.id)
        embed = _build_relation_embed(
            author=ctx.author,
            target=user,
            author_character=author_character,
            target_character=target_character,
            relation_scores=relation.scores,
            relation_summary=manager.describe_relation(relation.scores),
            recent_changes=relation.recent_changes,
            last_interaction=relation.last_interaction,
        )
        await ctx.send(embed=embed)

    @gochi.command()
    async def addtopic(ctx: commands.Context, *, topic: str) -> None:
        if not _require_guild(ctx):
            await ctx.send("❌ Este comando solo funciona dentro de un servidor.")
            return

        manager = get_bot_context(ctx.bot).gochi_manager
        result = manager.add_topic(ctx.guild.id, topic)

        if not result["ok"]:
            if result["code"] == "empty":
                await ctx.send("❌ Escribe un tema valido.")
                return
            if result["code"] == "duplicate":
                await ctx.send("⚠️ Ese tema ya existe en este servidor.")
                return
            await ctx.send("❌ No he podido guardar el tema.")
            return

        await ctx.send(
            f"✅ Tema añadido: `{result['topic']}`\n"
            f"Temas guardados: `{len(result['topics'])}`"
        )

    @gochi.command()
    async def help(ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Noah Gochi Commands",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Crear", value="`.noah gochi new <nombre>`", inline=False)
        embed.add_field(name="Ver personaje", value="`.noah gochi`", inline=False)
        embed.add_field(name="Poner imagen", value="`.noah gochi setimage`", inline=False)
        embed.add_field(name="Daily", value="`.noah gochi daily`", inline=False)
        embed.add_field(name="Interactuar", value="`.noah gochi interact [@usuario]`", inline=False)
        embed.add_field(name="Test admin", value="`.noah gochi test [@usuario]`", inline=False)
        embed.add_field(name="Relacion", value="`.noah gochi relation @usuario`", inline=False)
        embed.add_field(name="Tema del server", value="`.noah gochi addtopic <tema>`", inline=False)
        await ctx.send(embed=embed)
