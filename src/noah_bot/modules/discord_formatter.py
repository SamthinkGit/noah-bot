from PIL import Image, ImageDraw, ImageFont
import requests
import asyncio
from io import BytesIO
from typing import List
import json
from pathlib import Path
from functools import wraps
from typing import Optional
import discord


def _load_font(font_name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(font_name, size)
    except Exception:
        return ImageFont.load_default()


RARITY_COLORS = {
    "alpha": 0x3A423A,
    "beta": 0x357233,
    "gamma": 0x560000,
    "delta": 0x1D1D1D,
    "sigma": 0xE95C20,
    "epsilon": 0x5E258D,
    "zeta": 0xF54CE7,
    "omega": 0xB9F2FF,
}

RARITY_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "sigma": "σ",
    "epsilon": "ε",
    "zeta": "ζ",
    "omega": "ω",
}
RARITY_DISPLAY = {
    "alpha": "Alpha",
    "beta": "Beta",
    "gamma": "Gamma",
    "delta": "Delta",
    "sigma": "Sigma",
    "epsilon": "Epsilon",
    "zeta": "Zeta",
    "omega": "Omega",
}


class WaifuClaimFormatter:
    @staticmethod
    def build_embed(
        user: discord.Member | discord.User,
        waifu_name: str,
        rarity_symbol: str,
        claim_time_seconds: float,
    ) -> discord.Embed:
        """
        Builds a 'Waifu Claimed' embed.

        :param user: User who claimed the waifu
        :param waifu_name: Name of the claimed waifu
        :param rarity_symbol: Rarity symbol (e.g. δ, ★, S)
        :param claim_time_seconds: Claim time in seconds
        """

        embed = discord.Embed(
            title="🎉 Waifu Claimed!",
            description=(
                f"**Congrats {user.mention}**\n"
                f"You claimed **[{rarity_symbol}] {waifu_name}** "
                f"in just `{claim_time_seconds:.3f}`s!."
            ),
            color=discord.Color.pink(),
        )

        # User avatar as thumbnail
        embed.set_thumbnail(url=user.display_avatar.url)

        return embed


# ======= Loading bar rendering =======
def render_colored_bar(percent: int, width: int = 24) -> str:
    filled = int(width * percent / 100)
    empty = width - filled

    bar = "█" * filled + "░" * empty

    if percent < 40:
        lang = "diff"
        content = f"- {bar} {percent}%"
    elif percent < 80:
        lang = "fix"
        content = f"{bar} {percent}%"
    else:
        lang = "ini"
        content = f"[{bar} {percent}%]"

    return f"```{lang}\n{content}\n```"


def build_loading_embed(title: str, percent: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=render_colored_bar(percent),
        color=discord.Color.blurple(),
    )
    return embed


def with_loading(title: str = "Loading...", duration: float = 3.0, steps: int = 25):
    """
    title: Embed title
    duration: Total animation duration in seconds
    steps: Smoothness of the animation
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            delay = duration / steps

            # Initial message
            message = await ctx.send(embed=build_loading_embed(title, 0))

            for step in range(1, steps + 1):
                percent = int(step * 100 / steps)
                await asyncio.sleep(delay)

                try:
                    await message.edit(embed=build_loading_embed(title, percent))
                except discord.NotFound:
                    break

            # Remove loading message
            try:
                await message.delete()
            except discord.NotFound:
                pass

            # Execute real command
            return await func(ctx, *args, **kwargs)

        return wrapper

    return decorator


# ======= Delete button view =======


class DeleteMessageView(discord.ui.View):
    def __init__(
        self, author_id: int, command_message: discord.Message, timeout: int = 120
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.command_message = command_message

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # Only allow the command author to delete
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You can't delete this message.",
                ephemeral=True,
            )
            return

        # Delete bot message
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass

        # Delete command message
        try:
            await self.command_message.delete()
        except discord.NotFound:
            pass


def with_delete_button():
    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            original_send = ctx.send

            async def send_with_button(*send_args, **send_kwargs):
                view = DeleteMessageView(
                    author_id=ctx.author.id,
                    command_message=ctx.message,
                )
                send_kwargs["view"] = view
                return await original_send(*send_args, **send_kwargs)

            ctx.send = send_with_button
            return await func(ctx, *args, **kwargs)

        return wrapper

    return decorator


def build_quote_embed(
    user: discord.Member | discord.User,
    quoted_message: discord.Message,
) -> discord.Embed:
    """Embed de cita usado por `.noah quote` y `.noah rrpp quote`."""
    created_at = quoted_message.created_at.strftime("%d/%m/%Y")
    embed_color = (
        user.color
        if isinstance(user, discord.Member) and user.color.value
        else discord.Color.from_rgb(245, 187, 87)
    )

    quoted_lines = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in quoted_message.content.strip().splitlines()
    )

    embed = discord.Embed(
        description=f"{quoted_lines}\n\n*{user.mention} - {created_at}*",
        color=embed_color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


class EmbedTable:
    """
    Render tables in Discord using embeds.

    - Uses embeds (no markdown)
    - Stable layout (max 3 columns per row)
    - Automatically splits large tables
    """

    def __init__(
        self,
        headers: List[str],
        title: str = None,
        description: str = None,
        color: discord.Color = discord.Color.blurple(),
        max_columns: int = 3,
    ):
        self.headers = headers
        self.rows: List[List[str]] = []
        self.title = title
        self.description = description
        self.color = color
        self.max_columns = max_columns

    def add_row(self, row: List[str]) -> None:
        self.rows.append([str(cell) for cell in row])

    def render(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            color=self.color,
        )

        if not self.rows:
            embed.description = (embed.description or "")
            return embed

        # Convert rows → columns
        columns = list(zip(*self.rows))

        start = 0
        total_columns = len(self.headers)

        while start < total_columns:
            end = start + self.max_columns

            for header, column in zip(
                self.headers[start:end],
                columns[start:end],
            ):
                embed.add_field(
                    name=header,
                    value="\n".join(column),
                    inline=True,
                )

            start = end

            if start < total_columns:
                embed.add_field(name="\u200b", value="\u200b", inline=False)

        embed.set_footer(text=f"Rows: {len(self.rows)}")
        return embed


class DiscordChart:
    """
    Utility class to generate Discord-friendly tables using code blocks.
    """

    def __init__(self, headers: List[str]):
        self.headers = headers
        self.rows: List[List[str]] = []

    def add_row(self, row: List[str]) -> None:
        """
        Add a row to the table.

        Args:
            row (List[str]): Row values as strings.
        """
        self.rows.append(row)

    def _column_widths(self) -> List[int]:
        """
        Calculate max width for each column.
        """
        widths = [len(h) for h in self.headers]

        for row in self.rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        return widths

    def render(self) -> str:
        """
        Render the table as a Discord code block.

        Returns:
            str: Formatted table.
        """
        widths = self._column_widths()

        def format_row(row: List[str]) -> str:
            return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

        header = format_row(self.headers)
        separator = "-+-".join("-" * w for w in widths)
        body = "\n".join(format_row(r) for r in self.rows)

        table = f"{header}\n{separator}\n{body}"

        return f"```{table}```"


class UserEmojiManager:
    """
    Manages per-user emojis stored in a JSON file.
    """

    def __init__(self, file_path: str = "user_emojis.json"):
        self.file_path = Path(file_path)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.file_path.exists():
            return {}

        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def set_emoji(self, user_id: int, emoji: str) -> None:
        """
        Assign an emoji to a user.
        """
        self._data[str(user_id)] = emoji
        self._save()

    def get_emoji(self, user_id: int) -> Optional[str]:
        """
        Retrieve the emoji for a user.
        """
        return self._data.get(str(user_id))

    def remove_emoji(self, user_id: int) -> bool:
        """
        Remove the emoji assigned to a user.
        """
        key = str(user_id)
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False


class DiscordImageRenderer:
    def __init__(
        self,
        background_color=(255, 255, 255),
        padding=40,
        image_size=(300, 300),
        title_height=50,
        max_columns: int = 5,
    ):

        self.font_path_bold = "arialbd.ttf"
        self.background_color = background_color
        self.padding = padding
        self.image_size = image_size
        self.title_height = title_height
        self.max_columns = max_columns

        self.font_bold = _load_font("DejaVuSans-Bold.ttf", 22)
        self.font_big = _load_font("DejaVuSans-Bold.ttf", 40)

    def _download_image(self, url: str) -> Image.Image:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGBA")

    def _draw_text_shadow(self, draw, pos, text, font, fill):
        x, y = pos

        # single soft shadow
        draw.text(
            (x + 2, y + 2),
            text,
            font=font,
            fill=(0, 0, 0, 120),
        )

        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
        )

    def _fit_font_to_width(self, draw, text, font_path, max_width, start_size):
        size = start_size

        while size > 12:
            try:
                font = ImageFont.truetype(font_path, size)
            except Exception:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            width = bbox[2] - bbox[0]

            if width <= max_width:
                return font

            size -= 2

        return font

    def _draw_missing_image_placeholder(
        self,
        canvas: Image.Image,
        *,
        x: int,
        y: int,
        label: str,
    ) -> tuple[int, int]:
        draw = ImageDraw.Draw(canvas)
        box = [x, y, x + self.image_size[0], y + self.image_size[1]]
        draw.rounded_rectangle(
            box,
            radius=24,
            fill=(231, 235, 239, 255),
            outline=(189, 199, 209, 255),
            width=3,
        )

        missing_font = self._fit_font_to_width(
            draw,
            label,
            "DejaVuSans-Bold.ttf",
            self.image_size[0] - 40,
            28,
        )
        bbox = draw.textbbox((0, 0), label, font=missing_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            (
                x + (self.image_size[0] - text_width) // 2,
                y + (self.image_size[1] - text_height) // 2,
            ),
            label,
            font=missing_font,
            fill=(99, 110, 123, 255),
        )
        return x, y

    def render(self, images: list[dict]) -> Image.Image:
        count = len(images)
        if count == 0:
            return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

        max_cols = max(1, self.max_columns)
        cols = min(count, max_cols)
        rows = (count + max_cols - 1) // max_cols

        canvas_width = cols * self.image_size[0] + (cols + 1) * self.padding
        canvas_height = (
            rows * (self.image_size[1] + self.title_height) + (rows + 1) * self.padding
        )

        canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        for idx, data in enumerate(images):
            meta = data.get("meta", {})

            col = idx % cols
            row = idx // cols

            x = self.padding + col * (self.image_size[0] + self.padding)
            y = self.padding + row * (
                self.image_size[1] + self.title_height + self.padding
            )

            img: Image.Image | None = None
            try:
                img = self._download_image(data["url"])
                img.thumbnail(self.image_size)
                img_x = x + (self.image_size[0] - img.width) // 2
                img_y = y
                canvas.paste(img, (img_x, img_y), img)
            except Exception:
                img_x, img_y = self._draw_missing_image_placeholder(
                    canvas,
                    x=x,
                    y=y,
                    label="No image",
                )

            if meta.get("favorite"):
                emoji = meta["favorite"]
                emoji_url = self._emoji_to_twemoji_url(emoji)

                if emoji_url and img is not None:
                    try:
                        emoji_img = self._download_image(emoji_url)
                        emoji_img = emoji_img.resize((48, 48), Image.LANCZOS)

                        fx = img_x + img.width - emoji_img.width - 6
                        fy = img_y + 6

                        # Clean dark shadow (no glow, no halo)
                        shadow = emoji_img.copy().convert("RGBA")
                        shadow = shadow.point(lambda p: p * 0.6)  # darken

                        canvas.paste(shadow, (fx + 3, fy + 3), shadow)

                        # Main emoji
                        canvas.paste(emoji_img, (fx, fy), emoji_img)

                    except Exception:
                        pass

            if meta.get("local_id") and img is not None:
                local_id = meta["local_id"]

                font = self.font_big

                bbox = draw.textbbox((0, 0), local_id, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]

                lx = img_x + (img.width - text_w) // 2
                ly = img_y + img.height - text_h - 20

                draw.text(
                    (lx, ly),
                    local_id,
                    font=font,
                    fill="white",
                    stroke_width=3,
                    stroke_fill="black",
                )

            banner_y = img_y + self.image_size[1]
            draw.rectangle(
                [
                    x,
                    banner_y,
                    x + self.image_size[0],
                    banner_y + self.title_height,
                ],
                fill="white",
            )

            title = data.get("title", "")
            rarity = meta.get("rarity")

            if rarity:
                title = f"{title}  {rarity}"

            bbox = draw.textbbox((0, 0), title, font=self.font_bold)
            tx = x + (self.image_size[0] - (bbox[2] - bbox[0])) // 2
            ty = banner_y + (self.title_height - (bbox[3] - bbox[1])) // 2

            draw.text((tx, ty), title, font=self.font_bold, fill="black")

        return canvas

    def _emoji_to_twemoji_url(self, emoji: str) -> str | None:
        try:
            codepoints = "-".join(f"{ord(c):x}" for c in emoji)
            return f"https://twemoji.maxcdn.com/v/latest/72x72/{codepoints}.png"
        except Exception:
            return None


def collect_embed_images(embeds: list[discord.Embed]) -> list[dict]:
    images = []

    for index, embed in enumerate(embeds, start=1):
        meta = _parse_embed_metadata(embed)

        if embed.image and embed.image.url:
            images.append(
                {
                    "title": embed.title or f"Image {index}",
                    "url": embed.image.url,
                    "meta": meta,
                }
            )

    return images


def render_embeds_to_png(
    embeds: list[discord.Embed],
    *,
    max_columns: int = 5,
) -> BytesIO | None:
    images = collect_embed_images(embeds)
    if not images:
        return None

    renderer = DiscordImageRenderer(max_columns=max_columns)
    final_image = renderer.render(images)

    buffer = BytesIO()
    final_image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _open_png_buffer(buffer: BytesIO | None) -> Image.Image | None:
    if buffer is None:
        return None

    try:
        image = Image.open(buffer).convert("RGBA")
    except Exception:
        return None

    image.load()
    return image


def _fit_image_to_width(image: Image.Image, max_width: int) -> Image.Image:
    if image.width <= max_width:
        return image

    scale = max_width / image.width
    resized_height = max(1, int(image.height * scale))
    return image.resize((max_width, resized_height), Image.LANCZOS)


def _trade_bundle_columns(count: int) -> int:
    if count <= 1:
        return 1
    if count <= 4:
        return 2
    if count <= 9:
        return 3
    return 4


def _render_trade_bundle_image(embeds: list[discord.Embed]) -> Image.Image | None:
    images = collect_embed_images(embeds)
    if not images:
        return None

    buffer = render_embeds_to_png(
        embeds,
        max_columns=_trade_bundle_columns(len(images)),
    )
    return _open_png_buffer(buffer)


def _draw_trade_arrow(
    draw: ImageDraw.ImageDraw,
    right: int,
    top: int,
    *,
    color: tuple[int, int, int],
    direction: str,
) -> None:
    box_size = 76
    left = right - box_size
    bottom = top + box_size

    draw.rounded_rectangle(
        [left, top, right, bottom],
        radius=20,
        fill=(*color, 235),
    )

    shaft_width = 8
    margin = 18
    arrow_points = (
        [
            (left + margin, bottom - margin),
            (right - margin - 10, top + margin + 10),
        ]
        if direction == "up_right"
        else [
            (right - margin, bottom - margin),
            (left + margin + 10, top + margin + 10),
        ]
    )
    (start_x, start_y), (end_x, end_y) = arrow_points
    draw.line(
        [(start_x, start_y), (end_x, end_y)],
        fill=(255, 255, 255, 255),
        width=shaft_width,
    )

    if direction == "up_right":
        head = [
            (end_x, end_y),
            (end_x - 18, end_y),
            (end_x, end_y + 18),
        ]
    else:
        head = [
            (end_x, end_y),
            (end_x + 18, end_y),
            (end_x, end_y + 18),
        ]
    draw.polygon(head, fill=(255, 255, 255, 255))


def _draw_trade_owner_badge(
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    top: int,
    text: str,
    color: tuple[int, int, int],
    font: ImageFont.FreeTypeFont,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    badge_width = text_width + 34
    badge_height = text_height + 20
    draw.rounded_rectangle(
        [left, top, left + badge_width, top + badge_height],
        radius=18,
        fill=(*color, 235),
    )
    draw.text(
        (left + 17, top + 10),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )


def _build_trade_section_image(
    title: str,
    numbers: list[str],
    embeds: list[discord.Embed],
    *,
    color: tuple[int, int, int],
    direction: str,
) -> Image.Image:
    badge_font = _load_font("DejaVuSans-Bold.ttf", 22)
    text_font = _load_font("DejaVuSans.ttf", 24)
    placeholder_font = _load_font("DejaVuSans-Bold.ttf", 30)

    merged_image = _render_trade_bundle_image(embeds)
    if merged_image is not None:
        merged_image = _fit_image_to_width(merged_image, 1160)

    canvas_width = 1280 if merged_image is None else max(1280, merged_image.width + 120)
    placeholder_height = 260
    content_height = (merged_image.height + 48) if merged_image else placeholder_height
    canvas_height = 210 + content_height
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (248, 249, 251, 255))
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle(
        [8, 8, canvas_width - 8, canvas_height - 8],
        radius=28,
        outline=(*color, 255),
        width=6,
        fill=(255, 255, 255, 255),
    )

    _draw_trade_owner_badge(
        draw,
        left=34,
        top=26,
        text=title,
        color=color,
        font=badge_font,
    )
    numbers_text = " ".join(numbers) if numbers else "Sin cartas"
    draw.text(
        (36, 88),
        f"Oferta: {numbers_text}",
        font=text_font,
        fill=(74, 85, 104, 255),
    )

    _draw_trade_arrow(
        draw,
        canvas_width - 28,
        26,
        color=color,
        direction=direction,
    )

    content_left = 40
    content_top = 144
    content_right = canvas_width - 40
    content_bottom = canvas_height - 36

    draw.rounded_rectangle(
        [content_left, content_top, content_right, content_bottom],
        radius=24,
        fill=(244, 246, 248, 255),
        outline=(*color, 180),
        width=4,
    )

    if merged_image is None:
        placeholder_text = "Sin imagenes recopiladas"
        if not numbers:
            placeholder_text = "Sin cartas en esta oferta"

        bbox = draw.textbbox((0, 0), placeholder_text, font=placeholder_font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            (
                content_left + ((content_right - content_left) - text_width) // 2,
                content_top + ((content_bottom - content_top) - text_height) // 2,
            ),
            placeholder_text,
            font=placeholder_font,
            fill=(120, 128, 140, 255),
        )
        return canvas

    image_left = content_left + ((content_right - content_left) - merged_image.width) // 2
    image_top = content_top + 24
    canvas.paste(merged_image, (image_left, image_top), merged_image)
    return canvas


def render_autogami_trade_preview(
    author_name: str,
    author_numbers: list[str],
    author_embeds: list[discord.Embed],
    target_name: str,
    target_numbers: list[str],
    target_embeds: list[discord.Embed],
) -> BytesIO:
    top_section = _build_trade_section_image(
        f"{author_name} entrega",
        author_numbers,
        author_embeds,
        color=(34, 197, 94),
        direction="up_right",
    )
    bottom_section = _build_trade_section_image(
        f"{target_name} entrega",
        target_numbers,
        target_embeds,
        color=(239, 68, 68),
        direction="up_left",
    )

    spacing = 24
    canvas_width = max(top_section.width, bottom_section.width)
    canvas_height = top_section.height + bottom_section.height + spacing + 32
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (232, 236, 241, 255))

    top_x = (canvas_width - top_section.width) // 2
    bottom_x = (canvas_width - bottom_section.width) // 2
    canvas.paste(top_section, (top_x, 0), top_section)
    canvas.paste(bottom_section, (bottom_x, top_section.height + spacing), bottom_section)

    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# Helper function to parse embed metadata from
# `.v <waifu id>` from waifugami
def _parse_embed_metadata(embed):
    meta = {
        "favorite": None,
        "local_id": None,
        "rarity": None,
    }

    rarities = ["Delta", "Beta", "Gamma", "Alpha", "Omega", "Sigma", "Epsilon", "Zeta"]
    if embed.description:
        for line in embed.description.splitlines():
            if line.startswith("Favorite:"):
                meta["favorite"] = line.replace("Favorite:", "").strip()
            elif line.startswith("Local ID:"):
                meta["local_id"] = line.replace("Local ID:", "").strip()

            elif any(rarity in line for rarity in rarities):
                # Extrae símbolo entre paréntesis
                if "(" in line and ")" in line:
                    meta["rarity"] = line[line.find("(") + 1 : line.find(")")]  # noqa

    return meta
