#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marca de agua en lote — PyWebView + Flask + Pillow.

Uso:
    python app.py            -> abre la ventana de escritorio (PyWebView)
    python app.py --browser  -> solo servidor Flask en http://127.0.0.1:5000
"""

import io
import os
import re
import sys
import json
import base64
import shutil
import zipfile
import tempfile
import threading
import subprocess
import datetime as dt
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file, abort
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageFile

# ---------------------------------------------------------------- configuración

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
              ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".ts"}
EXTS = IMAGE_EXTS | VIDEO_EXTS
PREVIEW_MAX = 1400          # lado mayor al que se reduce la imagen para la vista previa
THUMB_MAX = 220
Image.MAX_IMAGE_PIXELS = None
# sin esto, guardar un JPEG con optimize=True falla con «broken data stream»
# en imágenes grandes o muy detalladas
ImageFile.MAXBLOCK = 32 * 1024 * 1024
ImageFile.LOAD_TRUNCATED_IMAGES = True

APP_DIR = Path(__file__).parent.resolve()
TMP_DIR = Path(tempfile.mkdtemp(prefix="watermark_"))
UPLOAD_DIR = TMP_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024   # 2 GB por lote

# ------------------------------------------------------------------- estado

STATE = {
    "images": {},        # id -> {"id","path","name","w","h","bytes"}
    "order": [],         # ids en orden
    "next_id": 1,
    "thumbs": {},        # id -> bytes JPEG
}
JOB = {
    "running": False, "done": 0, "total": 0,
    "error": None, "token": None, "filename": None, "current": "",
    "cancel": False, "failed": [], "notes": [], "sub": 0.0, "proc": None,
    "mode": "zip", "count": 0,
}
ZIPS = {}                # token -> Path del ZIP
OUTS = {}                # token -> {"dir": Path, "files": [nombres]}
LOCK = threading.Lock()


# ------------------------------------------------------------------- fuentes

_FONT_CACHE = None


def _font_dirs():
    home = Path.home()
    dirs = [APP_DIR / "fonts"]
    if sys.platform.startswith("win"):
        dirs += [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                 home / "AppData/Local/Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                 home / "Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 home / ".fonts", home / ".local/share/fonts"]
    return [d for d in dirs if d.is_dir()]


def list_fonts(refresh=False):
    """Devuelve [{'path','family','style','label'}] de las fuentes del sistema.

    Nunca lanza excepción: si una carpeta o un archivo falla, se salta.
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None and not refresh:
        return _FONT_CACHE
    found, seen = [], set()
    for d in _font_dirs():
        try:
            walker = os.walk(str(d), onerror=lambda e: None)
            for root, _dirs, files in walker:
                for name in files:
                    if not name.lower().endswith((".ttf", ".otf", ".ttc")):
                        continue
                    key = name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    p = Path(root) / name
                    try:
                        f = ImageFont.truetype(str(p), 16)
                        family, style = f.getname()
                    except Exception:
                        continue
                    family = family or p.stem
                    style = style or "Regular"
                    found.append({
                        "path": str(p), "family": family, "style": style,
                        "label": f"{family} — {style}",
                    })
        except Exception as e:
            print(f"[fuentes] no se pudo leer {d}: {e}")
    found.sort(key=lambda x: (x["family"].lower(), x["style"].lower()))
    print(f"[fuentes] {len(found)} encontradas")
    _FONT_CACHE = found
    return found


def add_font_path(path):
    """Registra una fuente elegida a mano por el usuario."""
    p = Path(path)
    try:
        f = ImageFont.truetype(str(p), 16)
        family, style = f.getname()
    except Exception:
        return None
    entry = {"path": str(p), "family": family or p.stem,
             "style": style or "Regular",
             "label": f"{family} — {style}" if style else family}
    fonts = list_fonts()
    if not any(x["path"] == entry["path"] for x in fonts):
        fonts.append(entry)
        fonts.sort(key=lambda x: (x["family"].lower(), x["style"].lower()))
    return entry


def load_font(path, size):
    size = max(1, int(size))
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        for f in list_fonts():
            try:
                return ImageFont.truetype(f["path"], size)
            except Exception:
                continue
    return ImageFont.load_default()


# ------------------------------------------------------- motor de marca de agua

def hex_rgb(value, fallback=(255, 255, 255)):
    try:
        v = value.lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def num(d, key, default, lo=None, hi=None):
    try:
        v = float(d.get(key, default))
    except (TypeError, ValueError):
        v = float(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


_ASSET_CACHE = {}


def load_asset(path):
    """Carga (y cachea) una imagen usada como icono o firma."""
    if not path:
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _ASSET_CACHE:
        return _ASSET_CACHE[key]
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("RGBA")
    except Exception:
        return None
    _ASSET_CACHE.clear()
    _ASSET_CACHE[key] = img
    return img


def scale_to_height(img, height):
    height = max(1, int(round(height)))
    if img.height == height:
        return img
    w = max(1, int(round(img.width * height / img.height)))
    return img.resize((w, height), Image.LANCZOS)


def build_content(s, base_px, scale):
    """Dibuja el contenido de la marca (texto + icono, o la firma), sin sombra.

    Devuelve la capa RGBA ya compuesta, con la opacidad todavía al 100 %.
    """
    px_mode = s.get("size_base") == "px"

    # ------------------------------------------------------------ firma
    if s.get("mark_mode") == "image":
        sig = load_asset(s.get("sign_path"))
        if sig is None:
            return None
        h = (num(s, "size_px", 120, 1, 20000) * scale) if px_mode \
            else (num(s, "size_pct", 10, 0.1, 200) / 100 * base_px)
        return scale_to_height(sig, h)

    # ------------------------------------------------------------- texto
    text = s.get("text", "") or ""
    icon = load_asset(s.get("icon_path")) if s.get("icon_on") else None
    if not text.strip() and icon is None:
        return None

    font_px = (num(s, "size_px", 48, 1, 20000) * scale) if px_mode \
        else (num(s, "size_pct", 6, 0.1, 200) / 100 * base_px)
    font_px = max(1, round(font_px))
    font = load_font(s.get("font_path", ""), font_px)

    align = s.get("align", "left")
    spacing = num(s, "line_spacing", 20, -50, 200) / 100 * font_px
    stroke_w = round(num(s, "outline_width", 6, 0, 40) / 100 * font_px) \
        if s.get("outline_on") else 0

    if text.strip():
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = probe.multiline_textbbox((0, 0), text, font=font, spacing=spacing,
                                        align=align, stroke_width=stroke_w)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        txt = Image.new("RGBA", (max(1, int(tw)) + 2, max(1, int(th)) + 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt)
        stroke_fill = None
        if stroke_w:
            oa = int(num(s, "outline_opacity", 100, 0, 100) / 100 * 255)
            stroke_fill = hex_rgb(s.get("outline_color", "#000000"), (0, 0, 0)) + (oa,)
        d.multiline_text((1 - bbox[0], 1 - bbox[1]), text, font=font, spacing=spacing,
                         align=align, fill=hex_rgb(s.get("color", "#ffffff")) + (255,),
                         stroke_width=stroke_w, stroke_fill=stroke_fill)
    else:
        txt = None
        th = font_px

    if icon is None:
        return txt

    # el icono se ajusta a la altura real del texto para que parezca un glifo más
    icon = scale_to_height(icon, th if txt is not None else font_px)
    if txt is None:
        return icon

    gap = int(num(s, "icon_gap", 25, 0, 200) / 100 * font_px)
    h = max(icon.height, txt.height)
    out = Image.new("RGBA", (icon.width + gap + txt.width, h), (0, 0, 0, 0))
    if s.get("icon_pos", "left") == "right":
        out.alpha_composite(txt, (0, (h - txt.height) // 2))
        out.alpha_composite(icon, (txt.width + gap, (h - icon.height) // 2))
    else:
        out.alpha_composite(icon, (0, (h - icon.height) // 2))
        out.alpha_composite(txt, (icon.width + gap, (h - txt.height) // 2))
    return out


def build_text_layer(s, base_px, scale=1.0):
    """Marca completa: contenido + sombra + opacidad + rotación."""
    content = build_content(s, base_px, scale)
    if content is None:
        return None

    ref = content.height  # la sombra se mide sobre el alto de la marca
    sdx = num(s, "shadow_dx", 4, -100, 100) / 100 * ref
    sdy = num(s, "shadow_dy", 4, -100, 100) / 100 * ref
    blur = num(s, "shadow_blur", 4, 0, 50) / 100 * ref

    if s.get("shadow_on"):
        pad = int(abs(sdx) + abs(sdy) + blur * 3) + 4
        layer = Image.new("RGBA", (content.width + pad * 2, content.height + pad * 2),
                          (0, 0, 0, 0))
        # silueta del contenido: así el icono y la firma también proyectan sombra
        a = content.getchannel("A")
        sa = int(num(s, "shadow_opacity", 60, 0, 100) / 100 * 255)
        sil = Image.new("RGBA", content.size,
                        hex_rgb(s.get("shadow_color", "#000000"), (0, 0, 0)) + (0,))
        sil.putalpha(a.point(lambda v: int(v * sa / 255)))
        sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        sh.alpha_composite(sil, (int(pad + sdx), int(pad + sdy)))
        if blur > 0:
            sh = sh.filter(ImageFilter.GaussianBlur(blur))
        layer = Image.alpha_composite(layer, sh)
        layer.alpha_composite(content, (pad, pad))
    else:
        layer = content

    op = num(s, "opacity", 55, 0, 100) / 100
    if op < 1:
        layer = layer.copy()
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * op)))

    angle = num(s, "angle", 0, -180, 180)
    if angle:
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    return layer


def anchor_xy(img_size, layer_size, pos, mx, my):
    W, H = img_size
    w, h = layer_size
    mx, my = int(mx), int(my)
    left, center, right = mx, (W - w) // 2, W - w - mx
    top, middle, bottom = my, (H - h) // 2, H - h - my
    table = {
        "tl": (left, top),   "tc": (center, top),    "tr": (right, top),
        "ml": (left, middle), "mc": (center, middle), "mr": (right, middle),
        "bl": (left, bottom), "bc": (center, bottom), "br": (right, bottom),
    }
    return table.get(pos, table["br"])


def base_side(w, h, mode):
    if mode == "width":
        return w
    if mode == "height":
        return h
    if mode == "diag":
        return (w * w + h * h) ** 0.5
    if mode == "min":
        return min(w, h)
    # "area": media geométrica. Es la única medida que no cambia cuando cambia
    # la relación de aspecto con el mismo número de píxeles, así que una foto
    # 3:2 y una vertical 9:16 reciben una marca del mismo peso visual.
    return (w * h) ** 0.5


def build_overlay(size, s, scale=1.0):
    """Capa RGBA transparente con la marca ya colocada, del tamaño dado.

    Es la pieza que comparten imágenes y vídeos: para una imagen se compone
    encima directamente; para un vídeo se guarda como PNG y FFmpeg la
    superpone en cada fotograma.
    """
    W, H = size
    base = base_side(W, H, s.get("size_base", "area"))
    layer = build_text_layer(s, base, scale)
    if layer is None:
        return None

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    # el mosaico repite la marca por toda la superficie
    if s.get("tile"):
        gap = num(s, "tile_gap", 40, 0, 400) / 100
        step_x = max(8, int(layer.width * (1 + gap)))
        step_y = max(8, int(layer.height * (1 + gap)))
        for y in range(-layer.height, H + step_y, step_y):
            for x in range(-layer.width, W + step_x, step_x):
                canvas.alpha_composite(layer, (x, y))
        return canvas

    if s.get("size_base") == "px":
        mx = num(s, "margin_x_px", 40, 0, 20000) * scale
        my = num(s, "margin_y_px", 40, 0, 20000) * scale
    else:
        mx = num(s, "margin_x_pct", 3, 0, 45) / 100 * base
        my = num(s, "margin_y_pct", 3, 0, 45) / 100 * base

    x, y = anchor_xy(size, layer.size, s.get("pos", "br"), mx, my)
    canvas.alpha_composite(layer, (x, y))
    return canvas


def apply_watermark(img, s, scale=1.0):
    img = img.convert("RGBA")
    overlay = build_overlay(img.size, s, scale)
    if overlay is None:
        return img
    return Image.alpha_composite(img, overlay)

# --------------------------------------------------------------------- vídeo

def find_bin(name):
    """Busca ffmpeg/ffprobe en el PATH, junto a app.py o en rutas típicas."""
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    local = APP_DIR / "bin" / exe
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    for c in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
              r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
        p = Path(c) / exe
        if p.is_file():
            return str(p)
    return None


FFMPEG = find_bin("ffmpeg")
FFPROBE = find_bin("ffprobe")


def run_quiet(cmd, **kw):
    """Ejecuta un proceso sin abrir una ventana de consola en Windows."""
    if sys.platform.startswith("win"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kw["startupinfo"] = si
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kw)


# ------------------------------------------------- aceleración por hardware

# Familias de codificadores por GPU, en orden de preferencia según plataforma.
# El nombre corto es lo que se muestra en la interfaz.
GPU_FAMILIES = [
    ("nvenc", "NVIDIA", {"h264": "h264_nvenc", "hevc": "hevc_nvenc",
                         "av1": "av1_nvenc"}),
    ("qsv", "Intel Quick Sync", {"h264": "h264_qsv", "hevc": "hevc_qsv",
                                 "av1": "av1_qsv", "vp9": "vp9_qsv"}),
    ("videotoolbox", "Apple VideoToolbox", {"h264": "h264_videotoolbox",
                                            "hevc": "hevc_videotoolbox",
                                            "prores": "prores_videotoolbox"}),
    ("amf", "AMD AMF", {"h264": "h264_amf", "hevc": "hevc_amf", "av1": "av1_amf"}),
    ("vaapi", "VAAPI", {"h264": "h264_vaapi", "hevc": "hevc_vaapi",
                        "av1": "av1_vaapi", "vp9": "vp9_vaapi"}),
]

_GPU = None


def encoder_works(name):
    """Comprueba de verdad que un codificador funciona.

    Es imprescindible probarlo: FFmpeg lista h264_nvenc aunque no haya
    tarjeta NVIDIA, y solo falla al intentar usarlo.
    """
    try:
        r = run_quiet([FFMPEG, "-v", "error", "-f", "lavfi",
                       "-i", "color=black:s=256x144:d=0.1",
                       "-c:v", name, "-f", "null", "-"],
                      capture_output=True, timeout=45)
        return r.returncode == 0
    except Exception:
        return False


def detect_gpu(force=False):
    """Devuelve {'available','family','label','encoders'} de la primera GPU útil."""
    global _GPU
    if _GPU is not None and not force:
        return _GPU
    none = {"available": False, "family": "", "label": "", "encoders": {}}
    if not FFMPEG:
        _GPU = none
        return _GPU
    try:
        listed = run_quiet([FFMPEG, "-hide_banner", "-encoders"],
                           capture_output=True, timeout=45).stdout.decode("utf-8", "replace")
    except Exception:
        _GPU = none
        return _GPU

    # macOS primero en Apple, NVIDIA primero en el resto
    order = GPU_FAMILIES
    if sys.platform == "darwin":
        order = sorted(GPU_FAMILIES, key=lambda f: f[0] != "videotoolbox")

    for family, label, encs in order:
        usable = {}
        for codec, enc in encs.items():
            if enc in listed and encoder_works(enc):
                usable[codec] = enc
        if usable:
            _GPU = {"available": True, "family": family, "label": label,
                    "encoders": usable}
            print(f"[gpu] {label} disponible: {', '.join(sorted(usable))}")
            return _GPU

    print("[gpu] sin aceleración por hardware; se usará la CPU")
    _GPU = none
    return _GPU


# Códec de origen -> codificador por CPU que produce ese mismo formato
CPU_ENCODERS = {
    "h264": "libx264", "hevc": "libx265", "vp9": "libvpx-vp9",
    "vp8": "libvpx", "av1": "libsvtav1", "mpeg4": "mpeg4",
    "msmpeg4v3": "msmpeg4", "wmv2": "wmv2", "mpeg2video": "mpeg2video",
    "prores": "prores_ks", "mjpeg": "mjpeg", "theora": "libtheora",
}
CRF_CODECS = {"libx264", "libx265", "libsvtav1", "libvpx-vp9", "libvpx"}


def pick_encoder(src_codec, use_gpu):
    """Elige con qué codificar para conservar el formato de origen.

    Devuelve (encoder, es_gpu, cambio_de_formato).
    """
    src_codec = (src_codec or "").lower()
    if use_gpu:
        gpu = detect_gpu()
        enc = gpu["encoders"].get(src_codec)
        if enc:
            return enc, True, False
    cpu = CPU_ENCODERS.get(src_codec)
    if cpu:
        # el codificador podría no estar compilado en esta build
        return cpu, False, False
    # formato exótico sin codificador: se recurre a H.264
    if use_gpu:
        enc = detect_gpu()["encoders"].get("h264")
        if enc:
            return enc, True, True
    return "libx264", False, True


def probe_video(path):
    """Devuelve {'w','h','duration','fps','has_audio'} en dimensiones de pantalla."""
    if not FFPROBE:
        return None
    try:
        r = run_quiet([FFPROBE, "-v", "error", "-show_streams", "-show_format",
                       "-of", "json", str(path)],
                      capture_output=True, timeout=60)
        info = json.loads(r.stdout or b"{}")
    except Exception:
        return None

    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if not w or not h:
        return None

    # un vídeo de móvil viene girado por metadatos; FFmpeg lo endereza al
    # decodificar, así que la marca debe calcularse sobre las medidas ya giradas
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(float(sd["rotation"]))) % 180
    if str(v.get("tags", {}).get("rotate", "")).strip() in ("90", "270", "-90"):
        rot = 90
    if rot == 90:
        w, h = h, w

    dur = 0.0
    try:
        dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    except (TypeError, ValueError):
        pass

    fps = 0.0
    try:
        n, d = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except Exception:
        pass

    # bitrate de vídeo: el del stream, y si falta se deduce del tamaño total
    vbr = 0
    try:
        vbr = int(v.get("bit_rate") or 0)
    except (TypeError, ValueError):
        pass
    if not vbr:
        try:
            total = int(info.get("format", {}).get("bit_rate") or 0)
            abr = sum(int(x.get("bit_rate") or 0)
                      for x in info.get("streams", [])
                      if x.get("codec_type") == "audio")
            vbr = max(0, total - abr)
        except (TypeError, ValueError):
            pass
    if not vbr and dur > 0:
        try:
            vbr = int(Path(path).stat().st_size * 8 / dur * 0.95)
        except OSError:
            pass

    return {"w": w, "h": h, "duration": dur, "fps": round(fps, 2),
            "codec": (v.get("codec_name") or "").lower(),
            "pix_fmt": v.get("pix_fmt") or "",
            "bitrate": vbr,
            "has_audio": any(s.get("codec_type") == "audio"
                             for s in info.get("streams", []))}


def grab_frame(path, at=None, duration=0.0):
    """Extrae un fotograma como imagen PIL para la lista y la vista previa."""
    if not FFMPEG:
        return None
    if at is None:
        at = min(2.0, duration * 0.25) if duration else 0.0
    try:
        r = run_quiet([FFMPEG, "-v", "error", "-ss", f"{max(0.0, at):.3f}",
                       "-i", str(path), "-frames:v", "1", "-f", "image2",
                       "-c:v", "png", "-"],
                      capture_output=True, timeout=120)
        if not r.stdout:
            # algunos vídeos fallan al buscar; se reintenta desde el principio
            r = run_quiet([FFMPEG, "-v", "error", "-i", str(path), "-frames:v", "1",
                           "-f", "image2", "-c:v", "png", "-"],
                          capture_output=True, timeout=120)
        if not r.stdout:
            return None
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:
        return None


TIME_RE = re.compile(rb"out_time=(\d+):(\d+):(\d+\.?\d*)")
TIME_US_RE = re.compile(rb"out_time_us=(\d+)")
# líneas de progreso de FFmpeg (clave=valor); nunca son errores
PROGRESS_KEY_RE = re.compile(
    r"^(frame|fps|stream_\S+|bitrate|total_size|out_time\S*|dup_frames|"
    r"drop_frames|speed|progress)=")


def rate_args(enc, mode, bitrate, opts):
    """Parámetros de calidad según lo que acepta CADA codificador.

    No son intercambiables: libsvtav1 rechaza -maxrate fuera del modo CRF,
    VideoToolbox no entiende -crf, y los preajustes de x264 ("medium") no
    valen para SVT-AV1, que usa números. Pasar los de otro rompe la
    codificación antes de escribir un solo fotograma.
    """
    br = int(bitrate or 0)
    crf = int(num(opts, "crf", 18, 0, 51))
    x_preset = opts.get("preset", "medium")
    # SVT-AV1 mide el preajuste de 0 (lento) a 13 (rápido)
    svt_preset = {"veryfast": "10", "faster": "9", "medium": "8", "slow": "6"}\
        .get(x_preset, "8")

    if enc in ("libx264", "libx265"):
        if mode == "crf":
            return ["-crf", str(crf), "-preset", x_preset]
        if br:
            return ["-b:v", str(br), "-maxrate", str(int(br * 1.25)),
                    "-bufsize", str(int(br * 2)), "-preset", x_preset]
        return ["-crf", "18", "-preset", x_preset]

    if enc == "libsvtav1":
        if mode == "crf" or not br:
            return ["-crf", str(crf), "-preset", svt_preset]
        # en modo bitrate SVT-AV1 no admite maxrate ni bufsize
        return ["-b:v", str(br), "-preset", svt_preset]

    if enc in ("libvpx-vp9", "libvpx"):
        if mode == "crf" or not br:
            return ["-crf", str(crf), "-b:v", "0"]
        return ["-b:v", str(br)]

    if "nvenc" in enc:
        if mode == "crf" or not br:
            return ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        return ["-rc", "vbr", "-b:v", str(br), "-maxrate", str(int(br * 1.25)),
                "-bufsize", str(int(br * 2))]

    if "qsv" in enc or "amf" in enc or "vaapi" in enc:
        return ["-b:v", str(br)] if br else ["-q:v", str(crf)]

    if "videotoolbox" in enc:
        # VideoToolbox no acepta -crf; solo bitrate o calidad 0-100
        return ["-b:v", str(br)] if br else ["-q:v", "60"]

    if enc == "prores_ks":
        return ["-profile:v", "3"]

    if enc == "mjpeg":
        return ["-q:v", "3"]

    # mpeg4, wmv2, msmpeg4, mpeg2video y demás: solo bitrate simple
    return ["-b:v", str(br)] if br else []


def encode_video(src, dest, overlay_png, opts, meta, on_progress, should_cancel):
    """Superpone el PNG y recodifica conservando el formato de origen.

    Por defecto replica el códec, el bitrate, el formato de píxel, los fps y el
    contenedor del vídeo original, y copia el audio tal cual.
    """
    use_gpu = bool(opts.get("gpu"))
    enc, is_gpu, changed = pick_encoder(meta.get("codec", ""), use_gpu)
    duration = meta.get("duration", 0) or 0
    mode = opts.get("vmode", "same")
    br = int(meta.get("bitrate") or 0)

    # el formato de píxel del original conserva el color (10 bits, 4:2:2, etc.)
    pix = meta.get("pix_fmt") or ""

    def build(audio_args, encoder, rate, use_pix):
        vf = f",format={pix}" if (pix and use_pix and not is_gpu) else ""
        cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
               "-i", str(src), "-i", str(overlay_png),
               "-filter_complex", f"[0:v][1:v]overlay=0:0:format=auto{vf}",
               "-c:v", encoder, *rate]
        if pix and use_pix and not is_gpu:
            cmd += ["-pix_fmt", pix]
        cmd += ["-map_metadata", "0"]
        if str(dest).lower().endswith((".mp4", ".mov", ".m4v")):
            cmd += ["-movflags", "+faststart"]
        return cmd + list(audio_args) + [str(dest)]

    def attempt(audio_args, encoder, rate, use_pix=True):
        kw = {}
        if sys.platform.startswith("win"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        # el progreso va por stdout y los errores por stderr, en canales
        # separados: si se mezclan, las líneas de progreso tapan el error
        p = subprocess.Popen(build(audio_args, encoder, rate, use_pix),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        JOB["proc"] = p
        errs = []

        def drain_errors():
            try:
                for line in p.stderr:
                    txt = line.decode("utf-8", "replace").rstrip()
                    if txt and not PROGRESS_KEY_RE.match(txt):
                        errs.append(txt)
                        del errs[:-40]
            except Exception:
                pass

        t = threading.Thread(target=drain_errors, daemon=True)
        t.start()
        try:
            for raw in p.stdout:
                if should_cancel():
                    p.terminate()
                    break
                m = TIME_RE.search(raw)
                if m and duration > 0:
                    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    on_progress(min(1.0, sec / duration))
                    continue
                m = TIME_US_RE.search(raw)
                if m and duration > 0:
                    on_progress(min(1.0, int(m.group(1)) / 1e6 / duration))
        finally:
            p.wait()
            t.join(timeout=5)
            JOB["proc"] = None
        return p.returncode, "\n".join(errs)

    audio_keep = ["-c:a", "copy"] if opts.get("audio_copy", True) else ["-c:a", "aac", "-b:a", "192k"]
    audio_aac = ["-c:a", "aac", "-b:a", "192k"]
    cpu_enc = CPU_ENCODERS.get(meta.get("codec", ""), "libx264")

    # cadena de intentos, del ideal al más conservador; cada eslabón corrige
    # una causa distinta de fallo
    plans = [(enc, rate_args(enc, mode, br, opts), audio_keep, True, None)]

    if br and mode != "crf":
        plans.append((enc, rate_args(enc, "crf", 0, opts), audio_keep, True,
                      "no admitió el bitrate del original, se usó calidad fija"))
    if opts.get("audio_copy", True):
        plans.append((enc, rate_args(enc, mode, br, opts), audio_aac, True,
                      "el audio no cabía en el contenedor, se recodificó a AAC"))
    if pix:
        plans.append((enc, rate_args(enc, mode, br, opts), audio_keep, False,
                      f"el formato de color «{pix}» no era compatible"))
    if is_gpu:
        plans.append((cpu_enc, rate_args(cpu_enc, mode, br, opts), audio_keep, True,
                      "la GPU falló, se usó la CPU"))
    if enc != "libx264" and cpu_enc != "libx264":
        plans.append(("libx264", rate_args("libx264", "crf", 0, opts), audio_aac, True,
                      f"no se pudo codificar en «{meta.get('codec')}», se usó H.264"))

    last_err = ""
    for encoder, rate, audio, use_pix, note in plans:
        if should_cancel():
            return "cancelado"
        if note:
            on_progress(0.0)
        code, last_err = attempt(audio, encoder, rate, use_pix)
        if code == 0:
            if note:
                msg = f"{Path(src).name}: {note}"
                JOB["notes"].append(msg)
                print(f"[aviso] {msg}", flush=True)
            elif changed:
                msg = (f"{Path(src).name}: sin codificador para "
                       f"«{meta.get('codec')}», se usó H.264")
                JOB["notes"].append(msg)
                print(f"[aviso] {msg}", flush=True)
            return None

    if should_cancel():
        return "cancelado"
    return explain_ffmpeg_error(last_err)


# líneas de FFmpeg que solo dicen «algo falló», sin decir qué
NOISE = ("terminating thread", "task finished with error", "conversion failed",
         "error sending frames", "nothing was written", "could not open encoder before")


def explain_ffmpeg_error(err):
    """Extrae la línea que de verdad explica el fallo."""
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    if not lines:
        return "FFmpeg falló sin dar detalles"
    print("[ffmpeg] " + "\n[ffmpeg] ".join(lines[-12:]), flush=True)
    signal = [l for l in lines
              if not any(n in l.lower() for n in NOISE)
              and any(k in l.lower() for k in
                      ("error", "invalid", "unable", "no such", "not supported",
                       "only supported", "cannot", "denied", "no space",
                       "unknown", "bad parameter", "failed"))]
    return (signal[0] if signal else lines[0])[:300]


def open_image(path):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def save_image(img, dest, fmt, quality, exif=None, icc=None):
    """Guarda conservando la máxima fidelidad posible.

    - El perfil de color ICC se conserva: sin él, una foto de cámara con
      AdobeRGB o P3 se reinterpreta como sRGB y los colores se apagan.
    - JPEG usa submuestreo 4:4:4 (subsampling=0), que no descarta información
      de color como el 4:2:0 habitual.
    - PNG y «WebP sin pérdida» no pierden absolutamente nada.
    """
    fmt = fmt.upper()
    kw = {}
    if exif:
        kw["exif"] = exif
    if icc:
        kw["icc_profile"] = icc

    if fmt in ("JPEG", "JPG"):
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        else:
            img = img.convert("RGB")
        img.save(dest, "JPEG", quality=int(quality), subsampling=0,
                 optimize=True, **kw)
    elif fmt == "WEBP_LOSSLESS":
        img.save(dest, "WEBP", lossless=True, quality=100, method=4, **kw)
    elif fmt == "WEBP":
        img.save(dest, "WEBP", quality=int(quality), method=4, **kw)
    else:
        img.save(dest, "PNG", compress_level=6, **kw)


# ---------------------------------------------------------------- helpers imgs

def register_image(path):
    """Registra una imagen o un vídeo en la lista de trabajo."""
    p = Path(path)
    ext = p.suffix.lower()
    if not p.is_file() or ext not in EXTS:
        return None
    is_video = ext in VIDEO_EXTS
    with LOCK:
        for item in STATE["images"].values():
            if item["path"] == str(p):
                return None

    if is_video:
        if not FFMPEG:
            return None
        info = probe_video(p)
        if not info:
            return None
        w, h = info["w"], info["h"]
        extra = {"kind": "video", "duration": info["duration"],
                 "fps": info["fps"], "has_audio": info["has_audio"],
                 "codec": info["codec"], "pix_fmt": info["pix_fmt"],
                 "bitrate": info["bitrate"]}
    else:
        try:
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im)
                w, h = im.size
        except Exception:
            return None
        extra = {"kind": "image"}

    with LOCK:
        iid = str(STATE["next_id"])
        STATE["next_id"] += 1
        item = {"id": iid, "path": str(p), "name": p.name,
                "w": w, "h": h, "bytes": p.stat().st_size, **extra}
        STATE["images"][iid] = item
        STATE["order"].append(iid)
        return item


FRAME_CACHE = {}


def media_image(item):
    """Imagen base de un elemento: la propia foto, o un fotograma del vídeo."""
    if item.get("kind") == "video":
        iid = item["id"]
        if iid not in FRAME_CACHE:
            fr = grab_frame(item["path"], duration=item.get("duration", 0))
            if fr is None:
                raise RuntimeError("No se pudo extraer un fotograma del vídeo.")
            FRAME_CACHE[iid] = fr
        return FRAME_CACHE[iid].copy()
    return open_image(item["path"])


def expand_paths(paths, recursive=True):
    out = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in EXTS:
                    out.append(f)
        elif p.is_file():
            out.append(p)
    return out


def image_list():
    return [STATE["images"][i] for i in STATE["order"] if i in STATE["images"]]


# --------------------------------------------------------------------- rutas

@app.route("/")
def index():
    return render_template("index.html", desktop=DESKTOP)


@app.get("/api/fonts")
def api_fonts():
    try:
        return jsonify(list_fonts(refresh=request.args.get("refresh") == "1"))
    except Exception as e:
        print("[fuentes] error:", e)
        return jsonify([])


PRESETS_FILE = APP_DIR / "presets.json"


def read_presets():
    try:
        import json
        return json.loads(PRESETS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def write_presets(data):
    import json
    PRESETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


@app.get("/api/presets")
def api_presets():
    return jsonify(sorted(read_presets().keys()))


@app.post("/api/presets/save")
def api_preset_save():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Ponle un nombre al preset."}), 400
    data = read_presets()
    data[name] = d.get("data", {})
    try:
        write_presets(data)
    except Exception as e:
        return jsonify({"error": f"No se pudo guardar: {e}"}), 500
    return jsonify({"names": sorted(data.keys())})


@app.post("/api/presets/load")
def api_preset_load():
    name = ((request.get_json(force=True) or {}).get("name") or "").strip()
    data = read_presets()
    if name not in data:
        return jsonify({"error": "Ese preset no existe."}), 404
    return jsonify({"data": data[name]})


@app.post("/api/presets/delete")
def api_preset_delete():
    name = ((request.get_json(force=True) or {}).get("name") or "").strip()
    data = read_presets()
    data.pop(name, None)
    try:
        write_presets(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"names": sorted(data.keys())})


@app.post("/api/asset-upload")
def api_asset_upload():
    """Sube un icono o una firma (modo navegador) y devuelve su ruta en disco."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No llegó ningún archivo."}), 400
    dest = UPLOAD_DIR / ("asset_" + Path(f.filename).name)
    f.save(dest)
    try:
        with Image.open(dest) as im:
            im.verify()
    except Exception:
        return jsonify({"error": "Ese archivo no es una imagen válida."}), 400
    return jsonify({"path": str(dest), "name": Path(f.filename).name})


@app.get("/api/asset-thumb")
def api_asset_thumb():
    """Miniatura del icono o la firma, para mostrarla en el panel."""
    img = load_asset(request.args.get("path", ""))
    if img is None:
        abort(404)
    img = img.copy()
    img.thumbnail((120, 120), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.post("/api/add-typed")
def api_add_typed():
    """Agrega una carpeta o un archivo escribiendo su ruta a mano."""
    raw = ((request.get_json(force=True) or {}).get("path") or "").strip()
    raw = raw.strip('"').strip("'")
    if not raw:
        return jsonify({"error": "Escribe una ruta."}), 400
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.exists():
        return jsonify({"error": f"No existe esa ruta: {p}"}), 400
    files = expand_paths([p])
    if not files:
        return jsonify({"error": "No hay imágenes en esa ruta."}), 400
    added = [a for a in (register_image(f) for f in files) if a]
    return jsonify({"added": len(added), "images": image_list()})


@app.post("/api/add-paths")
def api_add_paths():
    data = request.get_json(force=True) or {}
    files = expand_paths(data.get("paths", []), data.get("recursive", True))
    added = [register_image(f) for f in files]
    added = [a for a in added if a]
    return jsonify({"added": len(added), "images": image_list()})


@app.post("/api/upload")
def api_upload():
    """Alternativa para el modo navegador: sube los archivos a una carpeta temporal."""
    added = 0
    for f in request.files.getlist("files"):
        name = Path(f.filename or "").name
        if not name or Path(name).suffix.lower() not in EXTS:
            continue
        dest = UPLOAD_DIR / name
        i = 1
        while dest.exists():
            dest = UPLOAD_DIR / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        f.save(dest)
        if register_image(dest):
            added += 1
    return jsonify({"added": added, "images": image_list()})


@app.post("/api/remove")
def api_remove():
    ids = (request.get_json(force=True) or {}).get("ids", [])
    with LOCK:
        for i in ids:
            STATE["images"].pop(i, None)
            STATE["thumbs"].pop(i, None)
            FRAME_CACHE.pop(i, None)
            if i in STATE["order"]:
                STATE["order"].remove(i)
    return jsonify({"images": image_list()})


@app.post("/api/clear")
def api_clear():
    with LOCK:
        STATE["images"].clear()
        STATE["order"].clear()
        STATE["thumbs"].clear()
        FRAME_CACHE.clear()
    return jsonify({"images": image_list()})


@app.get("/api/thumb/<iid>")
def api_thumb(iid):
    item = STATE["images"].get(iid)
    if not item:
        abort(404)
    cached = STATE["thumbs"].get(iid)
    if cached is None:
        try:
            im = media_image(item).convert("RGB")
        except Exception:
            abort(404)
        im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        cached = buf.getvalue()
        STATE["thumbs"][iid] = cached
    return send_file(io.BytesIO(cached), mimetype="image/jpeg")


@app.post("/api/preview")
def api_preview():
    data = request.get_json(force=True) or {}
    iid = str(data.get("id", ""))
    item = STATE["images"].get(iid)
    if not item:
        return jsonify({"error": "No hay ninguna imagen seleccionada."}), 404
    try:
        im = media_image(item)
    except Exception as e:
        return jsonify({"error": f"No se pudo abrir el archivo: {e}"}), 400
    full_w = im.width
    # se reduce primero: como todo va en %, la vista previa es fiel al resultado
    im.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.LANCZOS)
    scale = im.width / full_w if full_w else 1
    out = apply_watermark(im, data.get("settings", {}), scale)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=88)
    return jsonify({
        "image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
        "name": item["name"], "w": item["w"], "h": item["h"],
    })


def run_job(settings, out_fmt, quality, suffix, video_opts, pack_zip=True):
    try:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        work = TMP_DIR / f"work_{stamp.replace(' ', '_')}"
        work.mkdir(parents=True, exist_ok=True)
        items = image_list()
        JOB["total"] = len(items)
        used = set()
        produced = []          # [(nombre_final, ruta_en_disco)]

        def uniq(stem, ext):
            arc, n = stem + ext, 1
            while arc.lower() in used:
                arc = f"{stem}_{n}{ext}"
                n += 1
            used.add(arc.lower())
            return arc

        for item in items:
            if JOB["cancel"]:
                break
            JOB["current"] = item["name"]
            JOB["sub"] = 0.0
            stem = Path(item["name"]).stem + (suffix or "")
            try:
                if item.get("kind") == "video":
                    # ------------------------------------------------- vídeo
                    overlay = build_overlay((item["w"], item["h"]), settings)
                    if overlay is None:
                        raise RuntimeError("La marca de agua está vacía.")
                    png = work / f"ov_{item['id']}.png"
                    overlay.save(png, "PNG")
                    # mismo contenedor que el original
                    vext = Path(item["name"]).suffix.lower() or ".mp4"
                    name = uniq(stem, vext)
                    out_path = work / name
                    err = encode_video(
                        item["path"], out_path, png, video_opts, item,
                        lambda f: JOB.__setitem__("sub", f),
                        lambda: JOB["cancel"])
                    png.unlink(missing_ok=True)
                    if err:
                        if err == "cancelado":
                            break
                        raise RuntimeError(err)
                    produced.append((name, out_path))
                else:
                    # ------------------------------------------------ imagen
                    src = open_image(item["path"])
                    keep = settings.get("keep_exif", True)
                    exif = src.info.get("exif") if keep else None
                    icc = src.info.get("icc_profile") if keep else None
                    out = apply_watermark(src, settings)

                    if out_fmt == "keep":
                        ext = Path(item["name"]).suffix.lower()
                        fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
                               ".webp": "WEBP", ".bmp": "PNG", ".tif": "PNG",
                               ".tiff": "PNG"}.get(ext, "PNG")
                    else:
                        fmt = out_fmt.upper()
                    ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                           "WEBP_LOSSLESS": ".webp"}[fmt]

                    name = uniq(stem, ext)
                    out_path = work / name
                    with open(out_path, "wb") as fh:
                        save_image(out, fh, fmt, quality, exif, icc)
                    produced.append((name, out_path))
            except Exception as e:
                msg = f"{item['name']}: {e}"
                JOB["failed"].append(msg)
                print(f"[error] {msg}", flush=True)
            JOB["done"] += 1
            JOB["sub"] = 0.0

        if JOB["cancel"]:
            shutil.rmtree(work, ignore_errors=True)
            JOB["error"] = "Proceso cancelado."
            return

        if not produced:
            shutil.rmtree(work, ignore_errors=True)
            JOB["error"] = "No se generó ningún archivo."
            return

        token = dt.datetime.now().strftime("%H%M%S%f")

        if pack_zip:
            zip_name = f"{stamp} export watermark.zip"
            zip_path = TMP_DIR / f"job_{stamp.replace(' ', '_')}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for name, path in produced:
                    zf.write(path, name)
            shutil.rmtree(work, ignore_errors=True)
            ZIPS[token] = zip_path
            JOB["filename"] = zip_name
            JOB["mode"] = "zip"
        else:
            # se dejan sueltos en la carpeta de trabajo
            OUTS[token] = {"dir": work, "files": [n for n, _ in produced]}
            JOB["filename"] = ""
            JOB["mode"] = "files"
            JOB["count"] = len(produced)
        JOB["token"] = token
    except Exception as e:
        JOB["error"] = str(e)
        print(f"[error] {e}", flush=True)
    finally:
        JOB["running"] = False
        JOB["current"] = ""
        JOB["sub"] = 0.0
        JOB["proc"] = None


@app.post("/api/process")
def api_process():
    if JOB["running"]:
        return jsonify({"error": "Ya hay un lote en proceso."}), 409
    if not STATE["order"]:
        return jsonify({"error": "Agrega imágenes antes de procesar."}), 400
    data = request.get_json(force=True) or {}
    JOB.update({"running": True, "done": 0, "total": 0, "error": None,
                "token": None, "filename": None, "cancel": False,
                "failed": [], "notes": [], "current": "", "sub": 0.0, "proc": None,
                "mode": "zip", "count": 0})
    threading.Thread(
        target=run_job,
        args=(data.get("settings", {}), data.get("format", "keep"),
              data.get("quality", 95), data.get("suffix", ""),
              data.get("video", {}), data.get("pack", True)),
        daemon=True,
    ).start()
    return jsonify({"started": True})


@app.get("/api/progress")
def api_progress():
    return jsonify({k: v for k, v in JOB.items() if k != "proc"})


@app.post("/api/cancel")
def api_cancel():
    JOB["cancel"] = True
    p = JOB.get("proc")
    if p is not None:
        try:
            p.terminate()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.get("/api/env")
def api_env():
    """Le dice a la interfaz si puede trabajar con vídeo y si hay GPU."""
    gpu = detect_gpu(force=request.args.get("refresh") == "1")
    return jsonify({"ffmpeg": bool(FFMPEG), "ffprobe": bool(FFPROBE),
                    "ffmpeg_path": FFMPEG or "",
                    "gpu": gpu["available"], "gpu_label": gpu["label"],
                    "gpu_codecs": sorted(gpu["encoders"].keys())})


@app.get("/api/files/<token>")
def api_files(token):
    """Lista de los archivos generados cuando no se empaquetan en ZIP."""
    out = OUTS.get(token)
    if not out:
        abort(404)
    return jsonify([{"name": n, "url": f"/api/file/{token}/{i}"}
                    for i, n in enumerate(out["files"])])


@app.get("/api/file/<token>/<int:idx>")
def api_file(token, idx):
    out = OUTS.get(token)
    if not out or idx >= len(out["files"]):
        abort(404)
    name = out["files"][idx]
    path = Path(out["dir"]) / name
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


@app.get("/api/download/<token>")
def api_download(token):
    path = ZIPS.get(token)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=JOB["filename"] or "watermark.zip",
                     mimetype="application/zip")


# ------------------------------------------------------------ API de escritorio

DESKTOP = False


def open_kind(webview):
    """OPEN_DIALOG cambió de nombre en pywebview 5.2; sirve para ambas."""
    k = getattr(getattr(webview, "FileDialog", None), "OPEN", None)
    return k if k is not None else webview.OPEN_DIALOG


class Api:
    """Puente hacia los diálogos nativos del sistema (solo en PyWebView)."""

    def _win(self):
        import webview
        return webview.windows[0]

    def pick_folder(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        r = self._win().create_file_dialog(kind)
        return list(r) if r else []

    def pick_images(self):
        import webview
        types = (
            "Imágenes y vídeos (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff;"
            "*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.mpg;*.mpeg;*.wmv;*.flv;*.3gp;*.ts)",
            "Solo imágenes (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff)",
            "Solo vídeos (*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.mpg;*.mpeg;*.wmv;*.flv;*.3gp;*.ts)",
            "Todos los archivos (*.*)",
        )
        r = self._win().create_file_dialog(
            open_kind(webview), allow_multiple=True, file_types=types)
        return list(r) if r else []

    def pick_asset(self):
        """Elige una imagen suelta para usar como icono o firma."""
        import webview
        types = ("Imágenes (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff)",)
        r = self._win().create_file_dialog(
            open_kind(webview), allow_multiple=False, file_types=types)
        if not r:
            return None
        p = r if isinstance(r, str) else r[0]
        if load_asset(p) is None:
            return {"error": "No se pudo leer esa imagen."}
        return {"path": p, "name": Path(p).name}

    def save_files(self, token):
        """Copia los archivos sueltos a la carpeta que elija el usuario."""
        import webview
        out = OUTS.get(token)
        if not out:
            return {"ok": False, "error": "Los archivos ya no están disponibles."}
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        r = self._win().create_file_dialog(kind)
        if not r:
            return {"ok": False, "cancelled": True}
        target = Path(r[0] if isinstance(r, (list, tuple)) else r)
        copied, errors = 0, []
        for name in out["files"]:
            src = Path(out["dir"]) / name
            if not src.exists():
                continue
            dest = target / name
            i = 1
            while dest.exists():
                dest = target / f"{Path(name).stem}_{i}{Path(name).suffix}"
                i += 1
            try:
                shutil.copyfile(src, dest)
                copied += 1
            except Exception as e:
                errors.append(f"{name}: {e}")
        return {"ok": True, "copied": copied, "path": str(target),
                "errors": errors}

    def pick_font(self):
        import webview
        r = self._win().create_file_dialog(
            open_kind(webview), allow_multiple=False,
            file_types=("Fuentes (*.ttf;*.otf;*.ttc)",))
        if not r:
            return None
        return add_font_path(r[0])

    def save_zip(self, token, filename):
        import webview
        src = ZIPS.get(token)
        if not src or not Path(src).exists():
            return {"ok": False, "error": "El archivo ZIP ya no está disponible."}
        # FileDialog.SAVE en pywebview 5.2+, SAVE_DIALOG en versiones anteriores
        save_kind = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        if save_kind is None:
            save_kind = webview.SAVE_DIALOG
        r = self._win().create_file_dialog(save_kind, save_filename=filename)
        if not r:
            return {"ok": False, "cancelled": True}
        dest = r if isinstance(r, str) else r[0]
        try:
            shutil.copyfile(src, dest)
            return {"ok": True, "path": dest}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main():
    global DESKTOP
    browser = "--browser" in sys.argv
    if browser:
        list_fonts()
        app.run(host="127.0.0.1", port=5000, debug=False)
        return
    try:
        import webview
    except ImportError:
        print("PyWebView no está instalado. Ejecuta: pip install pywebview")
        print("Mientras tanto puedes usar: python app.py --browser")
        return
    DESKTOP = True
    list_fonts()
    webview.create_window("Marca de agua en lote", app, js_api=Api(),
                          width=1440, height=920, min_size=(1040, 680))
    webview.start(gui=os.environ.get("PYWEBVIEW_GUI") or None,
                  private_mode=False)
    shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
