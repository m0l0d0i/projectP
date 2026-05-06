from __future__ import annotations

from io import BytesIO

import segno
from aiogram.types import BufferedInputFile


async def build_qr_png(data: str, filename: str = 'subscription.png') -> BufferedInputFile:
    qr = segno.make_qr(data)
    buffer = BytesIO()
    qr.save(buffer, kind='png', scale=8)
    return BufferedInputFile(buffer.getvalue(), filename=filename)
