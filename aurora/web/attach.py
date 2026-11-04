"""R381 — turn an uploaded file into something the chat can send: text documents become text (fenced by the page),
pictures become a data URL for a vision model. Standard library only, plus pypdf and Pillow when installed."""
import base64
import html
import io
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_ATTACH = 20_000_000       # bytes accepted per file
MAX_TEXT = 150_000            # characters of extracted text per file
MAX_ZIP_PART = 30_000_000     # uncompressed size of one office part (zip-bomb guard)
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
# R420: music and video are stored unchanged, never read into the chat; the agent gets the path (e.g. to tag music)
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
MAX_MEDIA = 200_000_000       # bytes accepted for one audio/video file (it is held in memory once while saved)
MAX_SIDE = 1568               # longest edge sent to the model; the original on the user's machine is untouched


class Unsupported(Exception):
    pass


def _ext(name: str) -> str:
    m = re.search(r"\.[A-Za-z0-9]+$", name)
    return m.group(0).lower() if m else ""


def _zip_xml(z: zipfile.ZipFile, part: str) -> ET.Element:
    info = z.getinfo(part)
    if info.file_size > MAX_ZIP_PART:
        raise Unsupported(f"{part} is too large inside the file")
    return ET.fromstring(z.read(part))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = _zip_xml(z, "word/document.xml")
    paras = []
    for p in root.iter():
        if _local(p.tag) == "p":
            paras.append("".join((t.text or "") if _local(t.tag) == "t" else ("\t" if _local(t.tag) == "tab" else "")
                                 for t in p.iter()))
    return "\n".join(paras)


def _pptx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
        out = []
        for i, n in enumerate(slides, 1):
            texts = [t.text or "" for t in _zip_xml(z, n).iter() if _local(t.tag) == "t"]
            out.append(f"--- slide {i} ---\n" + "\n".join(x for x in texts if x))
    return "\n".join(out)


def _xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in _zip_xml(z, "xl/sharedStrings.xml"):
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        names = {}
        if "xl/workbook.xml" in z.namelist():
            for i, sh in enumerate((e for e in _zip_xml(z, "xl/workbook.xml").iter() if _local(e.tag) == "sheet"), 1):
                names[i] = sh.get("name", f"sheet{i}")
        sheets = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
        out = []
        for n in sheets:
            idx = int(re.search(r"(\d+)\.xml", n).group(1))
            out.append(f"--- sheet {names.get(idx, idx)} ---")
            for row in _zip_xml(z, n).iter():
                if _local(row.tag) != "row":
                    continue
                cells = []
                for c in row:
                    v = next((x for x in c if _local(x.tag) == "v"), None)
                    isv = next((x for x in c if _local(x.tag) == "is"), None)
                    if c.get("t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                        cells.append(shared[int(v.text)])
                    elif isv is not None:
                        cells.append("".join(t.text or "" for t in isv.iter() if _local(t.tag) == "t"))
                    else:
                        cells.append(v.text if v is not None and v.text else "")
                out.append("\t".join(cells).rstrip("\t"))
    return "\n".join(out)


def _odf(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = _zip_xml(z, "content.xml")
    out = []
    for e in root.iter():
        if _local(e.tag) in ("p", "h"):
            out.append("".join(e.itertext()))
    return "\n".join(out)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise Unsupported("PDF needs the pypdf package on the server")
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        raise Unsupported("the PDF is password-protected")
    out = []
    for i, pg in enumerate(r.pages, 1):
        out.append(f"--- page {i} ---\n" + (pg.extract_text() or ""))
        if sum(map(len, out)) > MAX_TEXT:
            break
    return "\n".join(out)


def _html(data: bytes) -> str:
    t = data.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", t)
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def _rtf(data: bytes) -> str:
    t = data.decode("latin-1")
    t = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "replace"), t)
    t = re.sub(r"\\par[d]?\b", "\n", t)
    t = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", t)
    return t


def _plain(data: bytes) -> str:
    if b"\0" in data[:8192]:
        raise Unsupported("this looks like a binary file, not a supported document or image")
    return data.decode("utf-8", "replace")


_EXTRACT = {".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx, ".odt": _odf, ".ods": _odf, ".odp": _odf,
            ".pdf": _pdf, ".html": _html, ".htm": _html, ".rtf": _rtf}


def _image(data: bytes, name: str) -> str:
    ext = _ext(name)
    mime = IMAGE_TYPES[ext]
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return f"data:{mime};base64," + base64.b64encode(data).decode()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise Unsupported("the picture could not be read")
    try:
        rotated = im.getexif().get(0x0112, 1) not in (0, 1)   # R392 (bug 10): an EXIF-rotated picture is re-encoded upright
    except Exception:
        rotated = False
    im = ImageOps.exif_transpose(im)
    if not rotated and max(im.size) <= MAX_SIDE and len(data) <= 4_000_000 and mime != "image/gif":
        return f"data:{mime};base64," + base64.b64encode(data).decode()    # small enough: send the bytes as they are
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    if im.mode in ("RGBA", "LA", "P"):
        im.convert("RGBA").save(buf, "PNG")
        mime = "image/png"
    else:
        im.convert("RGB").save(buf, "JPEG", quality=88)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


OLD_UPLOAD_DIR = "web-uploads"   # R406's folder; R413 moved its files into workspace/uploads, R432 into workspace/input
OLD_INPUT_DIR = "uploads"        # R413's name for workspace/input (R432)
WORKSPACE = "workspace"          # R413: the user's files, under AURORA_HOME, one sub-folder per kind
KINDS = ("input", "outputs")     # input: what the user attached (R432; was uploads); outputs: what the agent made for the user
INPUT_KEEP_DAYS = 30             # input only: outputs are the user's results and are never pruned


def _move_into(old, d):
    import shutil
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for f in old.iterdir():
        if f.is_file() and not (d / f.name).exists():
            shutil.move(str(f), str(d / f.name))
    try:
        old.rmdir()
    except OSError:
        pass


def folder(kind: str = "input"):
    """R413: AURORA_HOME/workspace/<kind>. R432: the first call moves R406's web-uploads and R413's workspace/uploads
    into workspace/input and leaves workspace/uploads as a link to it, so paths named in older chats still open."""
    import os
    from ..service import aurora_home
    if kind not in KINDS:
        raise ValueError(kind)
    d = aurora_home() / WORKSPACE / kind
    if kind == "input":
        for old in (aurora_home() / OLD_UPLOAD_DIR, aurora_home() / WORKSPACE / OLD_INPUT_DIR):
            if old.is_dir() and not old.is_symlink():
                _move_into(old, d)
        link = aurora_home() / WORKSPACE / OLD_INPUT_DIR
        if d.is_dir() and not link.exists() and not link.is_symlink():
            try:
                os.symlink(kind, link)
            except OSError:
                pass
    return d


def input_dir():
    return folder("input")


def outputs_dir():
    """R413: created on demand, since the agent is told to save into it."""
    d = folder("outputs")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def save_original(name: str, data: bytes) -> str:
    """R406: the attached bytes, unchanged (EXIF and all), as a file the agent's tools can open; returns its path.
    Files in input older than INPUT_KEEP_DAYS are removed on the way."""
    import os
    import time
    import uuid
    d = input_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    cut = time.time() - INPUT_KEEP_DAYS * 86400
    for old in d.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cut:
                old.unlink()
        except OSError:
            pass
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[-80:] or "file"
    f = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{safe}"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    return str(f)


def files(kind: str = "input") -> list:
    """R408: the saved input files (R413: or outputs), newest first: {name, path, size, mtime, image}."""
    d = folder(kind)
    if not d.is_dir():
        return []
    out = [{"name": f.name, "path": str(f), "size": f.stat().st_size, "mtime": int(f.stat().st_mtime),
            "image": _ext(f.name) in IMAGE_TYPES, "preview": preview_kind(f.name)}
           for f in d.iterdir() if f.is_file()]
    return sorted(out, key=lambda u: (-u["mtime"], u["name"]))


def delete_file(name: str, kind: str = "input") -> bool:
    """R408: remove one saved file by its file name (never a path outside its folder)."""
    f = file_path(name, kind)
    if f is None:
        return False
    f.unlink()
    return True


def file_path(name: str, kind: str = "input"):
    """R410: the Path of one saved input file (R413: or output) by file name, or None."""
    if kind not in KINDS or not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    try:
        d = folder(kind).resolve()
        f = (d / name).resolve()
    except (OSError, ValueError):
        return None
    return f if f.parent == d and f.is_file() else None   # resolve(): a symlink out of the folder is refused


def thumbnail(f, side: int = 96):
    """R410: a small preview (PNG bytes) of a saved picture, or None; the saved file itself is never changed."""
    ext = _ext(f.name)
    if ext not in IMAGE_TYPES and ext != ".pdf":
        return None
    try:
        from PIL import Image, ImageOps
        if ext == ".pdf":                    # R414: the first page stands for the document
            page = _pdf_page_png(f, side)
            if page is None:
                return None
            im = Image.open(io.BytesIO(page))
        else:
            im = ImageOps.exif_transpose(Image.open(f))
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.convert("RGBA").save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


# R414: what the Files preview can show, by extension. Served as their own type only when listed in VIEW_TYPES;
# svg and html are previewed as source text (as a document in this origin they could run script).
VIEW_TYPES = {**IMAGE_TYPES, ".pdf": "application/pdf",
              ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4",
              ".flac": "audio/flac", ".opus": "audio/ogg", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
              ".mov": "video/quicktime"}
MARKDOWN = {".md", ".markdown"}
OFFICE = {".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp", ".rtf"}
PREVIEW_TEXT_MAX = 200_000     # characters a text preview carries


def preview_kind(name: str) -> str:
    """R414: image | pdf | audio | video | markdown | document | text | none (decided by extension; text is checked on read)."""
    ext = _ext(name)
    mime = VIEW_TYPES.get(ext, "")
    if mime.startswith(("image/", "audio/", "video/")):
        return mime.split("/")[0]
    if ext == ".pdf":
        return "pdf"
    if ext in MARKDOWN:
        return "markdown"
    if ext in OFFICE:
        return "document"
    return "text"


def preview_text(f) -> dict:
    """R414: {kind: markdown|text, text, cut} for a Files preview; office files give their extracted text.
    Raises Unsupported for a binary or unreadable file. The file itself is never changed."""
    kind = preview_kind(f.name)
    if kind not in ("markdown", "document", "text"):
        raise Unsupported("no text preview for this type")
    data = f.read_bytes()
    if kind == "document":
        text = process(f.name, data)["text"]
    else:
        text = _plain(data[:PREVIEW_TEXT_MAX * 4])
    return {"kind": "markdown" if kind == "markdown" else "text", "text": text[:PREVIEW_TEXT_MAX], "cut": len(text) > PREVIEW_TEXT_MAX}


def _pdf_page_png(f, side: int):
    """R414: the first page of a PDF as PNG bytes via poppler's pdftoppm, or None when it is missing or fails."""
    import shutil
    import subprocess
    from pathlib import Path
    exe = shutil.which("pdftoppm")
    if not exe:
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:   # pdftoppm cannot write a PNG to stdout
        try:
            r = subprocess.run([exe, "-f", "1", "-l", "1", "-singlefile", "-png", "-scale-to", str(side * 2), str(f), tmp + "/p"],
                               capture_output=True, timeout=20)
            data = (Path(tmp) / "p.png").read_bytes() if r.returncode == 0 else b""
        except (OSError, subprocess.SubprocessError):
            return None
    return data if data.startswith(b"\x89PNG") else None


def delete_all_files(kind: str = "input") -> int:
    """R410: remove every saved input file (R413: or output); returns how many."""
    n = 0
    for u in files(kind):
        try:
            f = folder(kind) / u["name"]
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def is_input(path: str) -> bool:
    """R406: only a file this server saved may be named to the model as an attachment."""
    from pathlib import Path
    try:
        p = Path(path).resolve()
        return p.parent == input_dir().resolve() and p.is_file()
    except (OSError, ValueError):
        return False


def media_kind(name: str) -> str:
    """R420: 'audio', 'video' or '' by extension."""
    ext = _ext(name or "")
    return "audio" if ext in AUDIO_EXTS else "video" if ext in VIDEO_EXTS else ""


def max_attach(name: str) -> int:
    """R420: the byte limit for this file name (audio/video may be larger than a document)."""
    return MAX_MEDIA if media_kind(name) else MAX_ATTACH


def media_text(kind: str, name: str, size: int, path: str) -> str:
    """R420: what the model is told about a stored audio/video file instead of its content."""
    return (f"[{kind.capitalize()} file \"{name}\" ({size / 1_000_000:.1f} MB) was saved unchanged in the input folder: {path}. "
            "Its content is not in this message; use your tools on that path to inspect or change it "
            "(e.g. read or write its tags), and save changed copies under workspace/outputs.]")


def process(name: str, data: bytes) -> dict:
    """{kind:'text', name, text, cut}, {kind:'image', name, data_url} or {kind:'media', name, media} (R420);
    raises Unsupported with a plain reason."""
    name = re.sub(r"[\x00-\x1f/\\]", "_", name or "file")[:120] or "file"
    if not data:
        raise Unsupported("the file is empty")
    if len(data) > max_attach(name):
        raise Unsupported(f"larger than {max_attach(name) // 1_000_000} MB")
    ext = _ext(name)
    if media_kind(name):
        return {"kind": "media", "name": name, "media": media_kind(name)}
    if ext in IMAGE_TYPES:
        return {"kind": "image", "name": name, "data_url": _image(data, name)}
    try:
        text = (_EXTRACT.get(ext) or _plain)(data)
    except Unsupported:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise Unsupported(f"{ext or 'this file'} could not be read (damaged or not what its name says)")
    except Exception as e:
        raise Unsupported(f"could not read it ({type(e).__name__})")
    if ext in _EXTRACT:                       # extracted from a PDF/office file: tidy the edges
        text = text.replace("\r\n", "\n").strip("\n")
    # R405: a plain text file is kept exactly as it is (raw), line endings and trailing newline included
    if not text.strip():
        raise Unsupported("no text found in it (a scanned PDF or a picture-only file?)")
    cut = len(text) > MAX_TEXT
    return {"kind": "text", "name": name, "text": text[:MAX_TEXT], "cut": cut}
