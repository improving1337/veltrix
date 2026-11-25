"""Validate uploaded images and send them as Telegram photos."""
import asyncio
import io
import warnings

from PIL import Image, ImageOps

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 24_000_000


def prepare(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as image:
                if image.width * image.height > MAX_PIXELS or getattr(image, "is_animated", False):
                    raise ValueError()
                result = ImageOps.exif_transpose(image)
                result.thumbnail((2560, 2560))
                if "A" in result.getbands() or "transparency" in result.info:
                    layer = result.convert("RGBA")
                    result = Image.new("RGB", layer.size, "white")
                    result.paste(layer, mask=layer.getchannel("A"))
                output = io.BytesIO()
                result.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
                return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Нужно обычное фото JPEG, PNG или WebP размером до 24 Мп") from None


async def read_upload(request):
    reader = await request.multipart()
    photo = None
    caption = None
    while (field := await reader.next()) is not None:
        if field.name == "photo" and photo is None:
            raw = bytearray()
            while chunk := await field.read_chunk():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Фото должно быть не больше 10 МБ")
            photo = await asyncio.to_thread(prepare, raw)
        elif field.name == "text" and caption is None:
            raw_caption = await field.read_chunk(8192)
            if not field.at_eof():
                raise ValueError("Подпись слишком длинная")
            caption = raw_caption.decode("utf-8").strip()
        else:
            raise ValueError("Прикрепи одно фото")
    if photo is None:
        raise ValueError("Прикрепи фото")
    caption = caption or ""
    if len(caption) > 1024:
        raise ValueError("Подпись к фото должна быть до 1024 символов")
    return photo, caption


async def send(client, peer, data, caption=""):
    photo = io.BytesIO(data)
    photo.name = "photo.jpg"
    return await client.send_file(peer, photo, caption=caption, force_document=False)
