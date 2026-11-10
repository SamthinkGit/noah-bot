"""Mapa de la ubicación de un evento, con OpenStreetMap (sin API key).

Todo es síncrono (usa `requests`): llámalo con `asyncio.to_thread`.
"""

import io
import logging
import math
import re
from urllib.parse import quote, unquote, unquote_plus

import requests
from PIL import Image, ImageDraw, ImageFont

_log = logging.getLogger(__name__)

# Las políticas de Nominatim y de los tiles de OSM piden identificar la aplicación.
USER_AGENT = "noah-bot/0.1 (Discord bot)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TIMEOUT = 10

TILE_SIZE = 256
ZOOM = 16
WIDTH, HEIGHT = 800, 380
PIN_COLOR = (226, 75, 74)

_COORDS = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
# Formatos que usan los enlaces de Google Maps, del más preciso al menos.
_URL_COORDS = (
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),
    re.compile(r"[?&](?:q|query|ll|destination)=(-?\d+\.\d+)(?:,|%2C)(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),
)
_URL_PLACE = re.compile(r"/maps/place/([^/@?]+)")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _valid(lat: float, lon: float) -> tuple[float, float] | None:
    return (lat, lon) if -85 <= lat <= 85 and -180 <= lon <= 180 else None


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


def maps_url(location: str) -> str:
    """Enlace para abrir la ubicación en Google Maps (o el enlace que pusieron)."""
    location = location.strip()
    if is_url(location):
        return location
    return f"https://www.google.com/maps/search/?api=1&query={quote(location)}"


def _coords_from_url(session: requests.Session, url: str) -> tuple[tuple[float, float] | None, str]:
    """Saca coordenadas y el nombre del sitio (si lo trae) de un enlace de mapas."""
    final_url = url
    try:
        # Los enlaces cortos (maps.app.goo.gl) redirigen al enlace completo.
        response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        final_url = response.url
    except requests.RequestException:
        pass

    # Google a veces redirige a una página de cookies con el enlace codificado dentro:
    # primero se mira el enlace original y luego el final, ya descodificado.
    name = ""
    for candidate in (url, unquote(final_url)):
        if match := _URL_PLACE.search(candidate):
            name = unquote_plus(match[1]).strip()
            break

    for candidate in (final_url, url):
        for pattern in _URL_COORDS:
            if match := pattern.search(candidate):
                coords = _valid(float(match[1]), float(match[2]))
                if coords is not None:
                    return coords, name
    return None, name


def _geocode(session: requests.Session, query: str) -> tuple[float, float] | None:
    try:
        response = session.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "accept-language": "es"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError):
        _log.warning("No se ha podido buscar la dirección en Nominatim", exc_info=True)
        return None
    if not results:
        return None
    try:
        return _valid(float(results[0]["lat"]), float(results[0]["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def _reverse_name(session: requests.Session, lat: float, lon: float) -> str:
    """Nombre legible de unas coordenadas: el sitio, o la calle y la ciudad."""
    try:
        response = session.get(
            NOMINATIM_REVERSE_URL,
            params={
                "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "accept-language": "es",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        _log.warning("No se ha podido buscar el nombre del sitio en Nominatim", exc_info=True)
        return ""
    if not isinstance(result, dict):
        return ""

    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    street = " ".join(
        str(part) for part in (address.get("road"), address.get("house_number")) if part
    )
    city = next(
        (address[key] for key in ("city", "town", "village", "municipality") if address.get(key)),
        "",
    )
    parts = [str(result.get("name") or ""), street, str(city)]
    return ", ".join(dict.fromkeys(part for part in parts if part))


def locate(location: str) -> tuple[tuple[float, float] | None, str]:
    """Coordenadas de una dirección, unas coordenadas o un enlace de Google Maps.

    También devuelve el nombre del sitio cuando la ubicación es un enlace (si no,
    ya es legible tal cual y el nombre va vacío).
    """
    location = location.strip()
    if not location:
        return None, ""
    if not is_url(location) and (match := _COORDS.search(location)):
        coords = _valid(float(match[1]), float(match[2]))
        if coords is not None:
            return coords, ""

    session = _session()
    if not is_url(location):
        return _geocode(session, location), ""

    coords, name = _coords_from_url(session, location)
    if coords is None and name:
        coords = _geocode(session, name)
    if coords is not None and not name:
        name = _reverse_name(session, *coords)
    return coords, name


def _world_pixel(lat: float, lon: float) -> tuple[float, float]:
    scale = TILE_SIZE * 2**ZOOM
    x = (lon + 180) / 360 * scale
    lat_rad = math.radians(lat)
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * scale
    return x, y


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_map(lat: float, lon: float) -> bytes | None:
    """PNG del mapa centrado en el punto, con un pin. None si fallan los tiles."""
    center_x, center_y = _world_pixel(lat, lon)
    left, top = center_x - WIDTH / 2, center_y - HEIGHT / 2
    first_x, first_y = int(left // TILE_SIZE), int(top // TILE_SIZE)
    last_x, last_y = int((left + WIDTH) // TILE_SIZE), int((top + HEIGHT) // TILE_SIZE)
    tiles_per_side = 2**ZOOM

    image = Image.new("RGB", (WIDTH, HEIGHT), (232, 228, 218))
    session = _session()
    loaded = 0
    for tile_x in range(first_x, last_x + 1):
        for tile_y in range(first_y, last_y + 1):
            if not 0 <= tile_y < tiles_per_side:
                continue
            url = TILE_URL.format(z=ZOOM, x=tile_x % tiles_per_side, y=tile_y)
            try:
                response = session.get(url, timeout=TIMEOUT)
                response.raise_for_status()
                tile = Image.open(io.BytesIO(response.content)).convert("RGB")
            except Exception:
                _log.warning("No se ha podido descargar un tile del mapa", exc_info=True)
                continue
            image.paste(
                tile,
                (round(tile_x * TILE_SIZE - left), round(tile_y * TILE_SIZE - top)),
            )
            loaded += 1
    if not loaded:
        return None

    draw = ImageDraw.Draw(image)
    pin_x, pin_y = WIDTH / 2, HEIGHT / 2
    # Pin: gota con la punta en el punto exacto.
    draw.ellipse((pin_x - 7, pin_y - 3, pin_x + 7, pin_y + 3), fill=(110, 110, 110))
    draw.polygon(
        [(pin_x - 12, pin_y - 28), (pin_x + 12, pin_y - 28), (pin_x, pin_y)], fill=PIN_COLOR
    )
    draw.ellipse((pin_x - 15, pin_y - 46, pin_x + 15, pin_y - 16), fill=PIN_COLOR)
    draw.ellipse((pin_x - 6, pin_y - 37, pin_x + 6, pin_y - 25), fill=(255, 255, 255))

    attribution = "© OpenStreetMap"
    font = _font(13)
    text_width = draw.textlength(attribution, font=font)
    draw.rectangle(
        (WIDTH - text_width - 12, HEIGHT - 20, WIDTH, HEIGHT), fill=(255, 255, 255)
    )
    draw.text((WIDTH - text_width - 6, HEIGHT - 18), attribution, font=font, fill=(60, 60, 60))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_map(location: str) -> tuple[tuple[float, float] | None, bytes | None, str]:
    """Busca la ubicación y dibuja el mapa: (coordenadas, PNG, nombre del sitio).

    Nunca lanza excepciones.
    """
    try:
        coords, name = locate(location)
        if coords is None:
            return None, None, name
        return coords, render_map(*coords), name
    except Exception:
        _log.exception("Error generando el mapa de un evento")
        return None, None, ""
