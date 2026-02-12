import io
import secrets

import qrcode


def generate_request_token() -> str:
    """Генерирует безопасный одноразовый токен для deeplink/QR."""
    return secrets.token_urlsafe(16)


def generate_qr_png(data: str) -> bytes:
    """Генерирует PNG-изображение QR-кода и возвращает байты."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

