"""Extração leve e cacheada de metadados EXIF essenciais (Date Taken e GPS)."""

from __future__ import annotations

import os
import ntpath
import stat
from datetime import datetime
from functools import lru_cache
from PIL import Image, ExifTags

PHOTO_EXIF_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.tiff', '.tif',
    '.heic', '.heif', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw', '.pef',
}

_HEIF_REGISTERED = False


def media_is_resident(file_path: str | None) -> bool:
    """Consulta atributos, sem abrir/hidratar placeholders do Windows.

    Deve ser chamado fora da thread da interface: até stat pode aguardar rede.
    """
    if not file_path or ntpath.splitdrive(file_path)[0].upper() == 'O:':
        return False
    try:
        info = os.stat(file_path)
        # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS.
        unavailable = 0x1000 | 0x40000 | 0x400000
        return stat.S_ISREG(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & unavailable)
    except OSError:
        return False


def _ensure_heif_registered():
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    _HEIF_REGISTERED = True


def _rational_to_float(value) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            num, den = value
            if not den:
                return None
            return float(num) / float(den)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_decimal(dms, ref: str) -> float | None:
    if not dms or len(dms) != 3:
        return None
    deg = _rational_to_float(dms[0])
    minutes = _rational_to_float(dms[1])
    seconds = _rational_to_float(dms[2])
    if deg is None or minutes is None or seconds is None:
        return None
    # Verificar NaN
    if deg != deg or minutes != minutes or seconds != seconds:
        return None
    decimal = abs(deg) + abs(minutes) / 60.0 + abs(seconds) / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode('ascii', errors='ignore')
    ref_str = str(ref or '').strip().upper()
    if ref_str in ('S', 'W'):
        decimal = -decimal
    return decimal


def _format_exif_datetime(raw_value) -> str | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode('utf-8', errors='ignore')
    text = str(raw_value).strip().rstrip('\x00').strip()
    if not text or text.startswith('0000:00:00'):
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y:%m:%d'):
        try:
            dt = datetime.strptime(text[:19], fmt)
            return dt.strftime('%d/%m/%Y %H:%M:%S' if '%H' in fmt else '%d/%m/%Y')
        except ValueError:
            continue
    return text


@lru_cache(maxsize=512)
def _extract_exif_cached(norm_path: str, file_size: int, mtime_ns: int) -> tuple[str | None, str | None]:
    ext = os.path.splitext(norm_path)[1].lower()
    if ext in ('.heic', '.heif'):
        _ensure_heif_registered()

    try:
        with Image.open(norm_path) as img:
            exif = img.getexif()
            if not exif:
                return None, None

            # Date Taken: prioridade ExifIFD DateTimeOriginal (36867) -> DateTimeDigitized (36868) -> DateTime (306)
            exif_ifd = exif.get_ifd(0x8769) if hasattr(exif, 'get_ifd') else {}
            raw_date = (
                (exif_ifd.get(36867) if exif_ifd else None)
                or (exif_ifd.get(36868) if exif_ifd else None)
                or exif.get(306)
            )
            date_taken = _format_exif_datetime(raw_date)

            # GPS: GPSInfo IFD (0x8825)
            gps_text = None
            gps_raw = exif.get_ifd(0x8825) if hasattr(exif, 'get_ifd') else None
            if gps_raw:
                gps_named = {
                    ExifTags.GPSTAGS.get(k, k): v for k, v in gps_raw.items()
                }
                lat_dms = gps_named.get('GPSLatitude')
                lat_ref = gps_named.get('GPSLatitudeRef', '')
                lon_dms = gps_named.get('GPSLongitude')
                lon_ref = gps_named.get('GPSLongitudeRef', '')
                lat = _dms_to_decimal(lat_dms, lat_ref)
                lon = _dms_to_decimal(lon_dms, lon_ref)
                if (
                    lat is not None
                    and lon is not None
                    and -90.0 <= lat <= 90.0
                    and -180.0 <= lon <= 180.0
                    and not (lat == 0.0 and lon == 0.0)
                ):
                    gps_text = f"{lat:.6f}, {lon:.6f}"

            return date_taken, gps_text
    except Exception:
        return None, None


def extract_exif_summary(file_path: str | None) -> tuple[str | None, str | None]:
    """Retorna ``(date_taken, gps_text)`` apenas com os campos disponíveis no EXIF."""
    if not file_path or not isinstance(file_path, str):
        return None, None
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in PHOTO_EXIF_EXTENSIONS:
        return None, None
    if not media_is_resident(file_path):
        return None, None
    try:
        if not os.path.isfile(file_path):
            return None, None
        st = os.stat(file_path)
    except OSError:
        return None, None
    return _extract_exif_cached(
        os.path.normcase(os.path.normpath(file_path)),
        int(st.st_size),
        int(getattr(st, 'st_mtime_ns', int(st.st_mtime))),
    )
