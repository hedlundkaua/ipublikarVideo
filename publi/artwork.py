"""Artwork composition for the Publi quiz videos."""
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


from .alternatives import option_labels
W, H = 1080, 1920
MASCOT_SVG = Path(__file__).resolve().parents[1] / "assets" / "mascot" / "publi.svg"

# Keep quiz content inside the central Shorts-safe area. These measurements are
# approximately 90% of the original artwork, with extra breathing room at the
# top and bottom for the app chrome shown over published Shorts.
QUESTION_BOX = (104, 140, 977, 527)
QUESTION_TEXT = (140, 185)
QUESTION_FONT_SIZE = 43
QUESTION_WRAP_WIDTH = 792
OPTION_LEFT = 117
OPTION_RIGHT = 963
OPTION_TOP = 565
OPTION_STEP = 158
OPTION_HEIGHT = 117
OPTION_TEXT_LEFT = 158
OPTION_FONT_SIZE = 31
MASCOT_WIDTH = 414
MASCOT_BOTTOM_MARGIN = 305
REVEAL_CHECK_POSITION = (175, 1580)
REVEAL_CHECK_FONT_SIZE = 108
FOOTER_FONT_SIZE = 31


def _font(size, bold=False):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""), size)
    except OSError:
        return ImageFont.load_default()


def rasterize_publi(color, output, width=430):
    """Rasterize the supplied SVG, recolouring only its first body path."""
    try:
        import cairosvg
    except ImportError as exc:
        raise RuntimeError("CairoSVG não está instalado; não foi possível rasterizar o Publi.") from exc
    if not MASCOT_SVG.exists():
        raise RuntimeError(f"SVG oficial do Publi não encontrado: {MASCOT_SVG}")
    try:
        root = ET.parse(MASCOT_SVG).getroot()
        body = next(node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "path")
        parent = next(node for node in root.iter() if body in list(node))
        outer_path = body.get("d", "").split("z", 1)[0] + "z"
        body_fill = ET.Element(body.tag, {"d": outer_path, "fill": color})
        parent.insert(list(parent).index(body), body_fill)
        body.set("fill", "#000000")
        svg = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        rendered = cairosvg.svg2png(bytestring=svg, output_width=max(width * 4, 1200))
        image = Image.open(BytesIO(rendered)).convert("RGBA")
        bounds = image.getbbox()
        if not bounds:
            raise ValueError("o SVG não produziu pixels visíveis")
        image = image.crop(bounds)
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        image.save(output)
    except Exception as exc:
        raise RuntimeError(f"Falha ao rasterizar o SVG do Publi: {exc}") from exc
    return Path(output)


def _wrapped(draw, text, font, max_width):
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) > max_width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\n".join(lines)


def _fit_lines(draw, text, max_width, max_lines=2, start_size=31, min_size=20, bold=True):
    """Fit copy into a bounded number of lines, shrinking when necessary."""
    for size in range(start_size, min_size - 1, -1):
        font = _font(size, bold)
        wrapped = _wrapped(draw, text, font, max_width)
        if len(wrapped.splitlines() or [""]) <= max_lines:
            return wrapped, font
    return _wrapped(draw, text, _font(min_size, bold), max_width), _font(min_size, bold)


def _hex_rgb(color):
    value = str(color).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    try:
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
    except (TypeError, ValueError):
        return (255, 107, 53)


def make_youtube_thumbnail(niche, color, output, outfit_path=None):
    """Create the deterministic 1280x720 thumbnail for the horizontal upload."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    red, green, blue = _hex_rgb(color)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    light = luminance < 0.52
    background = "#f5f2e9" if light else "#101426"
    foreground = "#101426" if light else "#ffffff"
    band = "#ffffff" if light else "#1d2540"
    image = Image.new("RGB", (1280, 720), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 26, 1252, 694), 34, outline=color, width=10)
    draw.text((104, 105), "PUBLI", font=_font(74, True), fill=foreground, anchor="lm")
    draw.text((105, 172), "QUIZ", font=_font(45, True), fill=color, anchor="lm")
    with TemporaryDirectory(prefix="publi-youtube-thumbnail-") as temporary:
        mascot_path = Path(temporary) / "publi.png"
        rasterize_publi(color, mascot_path, width=410)
        mascot = Image.open(mascot_path).convert("RGBA")
        x, y = (1280 - mascot.width) // 2 + 115, 12
        image.paste(mascot, (x, y), mascot)
        if outfit_path:
            try:
                outfit = Image.open(outfit_path).convert("RGBA")
                outfit.thumbnail((mascot.width, mascot.height))
                image.paste(outfit, (x + (mascot.width - outfit.width) // 2, y + int(mascot.height * .48)), outfit)
            except (OSError, ValueError):
                pass
    draw.rounded_rectangle((28, 505, 1252, 694), 34, fill=band, outline=color, width=8)
    copy = f"Quizzes de {niche} para você treinar"
    wrapped, font = _fit_lines(draw, copy, 1090, max_lines=2, start_size=58, min_size=18)
    draw.multiline_text((640, 600), wrapped, font=font, fill=foreground, anchor="mm", align="center", spacing=8)
    image.save(output, format="PNG", optimize=True)
    return image


def make_preview(question, options, color, output, outfit_path=None, correct_option=None, reveal=False, labels=None, orientation="vertical"):
    """Create a 1080x1920 quiz card using the official Publi SVG."""
    if orientation == "horizontal":
        return make_preview_horizontal(question, options, color, output, outfit_path, correct_option, reveal, labels)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (W, H), "#101426")
    draw = ImageDraw.Draw(image)
    question_font = _font(QUESTION_FONT_SIZE, True)
    draw.rounded_rectangle(QUESTION_BOX, 34, fill="#1d2540", outline=color, width=6)
    draw.multiline_text(
        QUESTION_TEXT,
        _wrapped(draw, question, question_font, QUESTION_WRAP_WIDTH),
        font=question_font,
        fill="white",
        spacing=11,
    )
    labels = labels or option_labels(options)
    palette = ["#29385f", "#34305e", "#244d55", "#4b365f"]
    option_font = _font(OPTION_FONT_SIZE, True)
    for index, option in enumerate(options):
        y = OPTION_TOP + index * OPTION_STEP
        selected = reveal and index == correct_option
        draw.rounded_rectangle(
            (OPTION_LEFT, y, OPTION_RIGHT, y + OPTION_HEIGHT),
            25,
            fill="#16803c" if selected else palette[index % len(palette)],
            outline="#7dff9d" if selected else "#ffffff",
            width=4 if selected else 3,
        )
        wrapped, fitted_font = _fit_lines(draw, f"{labels[index]}. {option}", OPTION_RIGHT - OPTION_TEXT_LEFT - 35, start_size=OPTION_FONT_SIZE)
        draw.multiline_text((OPTION_TEXT_LEFT, y + OPTION_HEIGHT // 2), wrapped, font=fitted_font, fill="white", anchor="lm", spacing=5)
    with TemporaryDirectory(prefix="publi-mascot-") as temporary:
        mascot_path = Path(temporary) / "publi.png"
        rasterize_publi(color, mascot_path, width=MASCOT_WIDTH)
        mascot = Image.open(mascot_path).convert("RGBA")
        x = (W - mascot.width) // 2
        y = H - mascot.height - MASCOT_BOTTOM_MARGIN
        image.paste(mascot, (x, y), mascot)
        if outfit_path:
            try:
                outfit = Image.open(outfit_path).convert("RGBA")
                outfit.thumbnail((mascot.width, mascot.height))
                image.paste(outfit, (x + (mascot.width - outfit.width) // 2, y + int(mascot.height * .48)), outfit)
            except (OSError, ValueError):
                pass
    if reveal:
        draw.text(
            REVEAL_CHECK_POSITION,
            "✓",
            font=_font(REVEAL_CHECK_FONT_SIZE, True),
            fill="#7dff9d",
            anchor="mm",
        )
    draw.text((540, 1800), "PUBLI QUIZ", font=_font(FOOTER_FONT_SIZE, True), fill="#aeb9df", anchor="mm")
    image.save(output)
    return image


def make_preview_horizontal(question, options, color, output, outfit_path=None,
                            correct_option=None, reveal=False, labels=None):
    """Create the dedicated 16:9 composition: quiz left, Publi right."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1920, 1080), "#101426")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 60, 1190, 360), 34, fill="#1d2540", outline=color, width=6)
    wrapped, font = _fit_lines(draw, question, 1010, max_lines=4, start_size=48, min_size=30)
    draw.multiline_text((120, 210), wrapped, font=font, fill="white", anchor="lm", spacing=10)
    labels = labels or option_labels(options)
    palette = ["#29385f", "#34305e", "#244d55", "#4b365f"]
    for index, option in enumerate(options):
        y = 405 + index * 145
        selected = reveal and index == correct_option
        draw.rounded_rectangle((90, y, 1170, y + 112), 24, fill="#16803c" if selected else palette[index % len(palette)], outline="#7dff9d" if selected else "#ffffff", width=4 if selected else 3)
        text, option_font = _fit_lines(draw, f"{labels[index]}. {option}", 985, start_size=34, min_size=22)
        draw.multiline_text((135, y + 56), text, font=option_font, fill="white", anchor="lm", spacing=5)
    with TemporaryDirectory(prefix="publi-horizontal-") as temporary:
        mascot_path = Path(temporary) / "publi.png"
        rasterize_publi(color, mascot_path, width=590)
        mascot = Image.open(mascot_path).convert("RGBA")
        x, y = 1270, 1080 - mascot.height - 105
        image.paste(mascot, (x, y), mascot)
        if outfit_path:
            try:
                outfit = Image.open(outfit_path).convert("RGBA")
                outfit.thumbnail((mascot.width, mascot.height))
                image.paste(outfit, (x + (mascot.width - outfit.width) // 2, y + int(mascot.height * .48)), outfit)
            except (OSError, ValueError):
                pass
    if reveal:
        draw.text((1300, 180), "✓", font=_font(120, True), fill="#7dff9d", anchor="mm")
    draw.text((1575, 72), "PUBLI QUIZ", font=_font(38, True), fill="#aeb9df", anchor="mm")
    image.save(output)
    return image


def make_outro(message, color, output, outfit_path=None):
    """Create the YouTube Shorts closing card with a comments call to action."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (W, H), "#101426")
    draw = ImageDraw.Draw(image)

    # Keep the copy away from the Shorts controls on the right-hand edge.
    draw.rounded_rectangle((60, 100, 950, 790), 42, fill="#1d2540", outline=color, width=8)
    font_size = 62
    while font_size >= 38:
        font = _font(font_size, True)
        wrapped = _wrapped(draw, message, font, 770)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=18, align="center")
        if box[3] - box[1] <= 540:
            break
        font_size -= 4
    draw.multiline_text(
        (505, 445), wrapped, font=font, fill="white", spacing=18,
        align="center", anchor="mm",
    )

    with TemporaryDirectory(prefix="publi-outro-") as temporary:
        mascot_path = Path(temporary) / "publi.png"
        rasterize_publi(color, mascot_path, width=470)
        mascot = Image.open(mascot_path).convert("RGBA")
        x, y = 105, H - mascot.height - 205
        image.paste(mascot, (x, y), mascot)
        if outfit_path:
            try:
                outfit = Image.open(outfit_path).convert("RGBA")
                outfit.thumbnail((mascot.width, mascot.height))
                image.paste(outfit, (x + (mascot.width - outfit.width) // 2, y + int(mascot.height * .48)), outfit)
            except (OSError, ValueError):
                pass

    arrow_y = 1370
    draw.line((600, arrow_y, 965, arrow_y), fill="white", width=25)
    draw.polygon(((965, arrow_y), (885, arrow_y - 58), (885, arrow_y + 58)), fill=color)
    draw.text((780, arrow_y - 92), "COMENTE AQUI", font=_font(35, True), fill="white", anchor="mm")
    draw.text((540, 1800), "PUBLI QUIZ", font=_font(34, True), fill="#aeb9df", anchor="mm")
    image.save(output)
    return image


def make_outro_horizontal(message, color, output, outfit_path=None):
    """Create a matching 16:9 closing card."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1920, 1080), "#101426")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 100, 1160, 860), 42, fill="#1d2540", outline=color, width=8)
    wrapped, font = _fit_lines(draw, message, 900, max_lines=6, start_size=62, min_size=36)
    draw.multiline_text((615, 430), wrapped, font=font, fill="white", spacing=18, align="center", anchor="mm")
    draw.text((615, 760), "COMENTE SEU PLACAR", font=_font(38, True), fill=color, anchor="mm")
    with TemporaryDirectory(prefix="publi-outro-horizontal-") as temporary:
        mascot_path = Path(temporary) / "publi.png"
        rasterize_publi(color, mascot_path, width=600)
        mascot = Image.open(mascot_path).convert("RGBA")
        x, y = 1260, 1080 - mascot.height - 100
        image.paste(mascot, (x, y), mascot)
    draw.text((1580, 72), "PUBLI QUIZ", font=_font(38, True), fill="#aeb9df", anchor="mm")
    image.save(output)
    return image
