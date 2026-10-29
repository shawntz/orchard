"""Best-effort PDF text extraction. Uses pypdf when installed; otherwise a standard-library fallback."""
import io
import logging
import re
import zlib

STREAM = re.compile(rb'<<(.{0,2000}?)>>\s*stream\r?\n(.*?)\r?\nendstream', re.S)
TEXT_OP = re.compile(rb'\[((?:\\.|[^\]\\])*)\]\s*TJ|\(((?:\\.|[^()\\]|\((?:\\.|[^()\\])*\))*)\)\s*(?:Tj|\'|")|(T\*|Td|TD|ET)\b', re.S)
LITERAL = re.compile(rb'\(((?:\\.|[^()\\]|\((?:\\.|[^()\\])*\))*)\)|(-?\d+(?:\.\d+)?)', re.S)
ESCAPES = {b'n': b'\n', b'r': b'\r', b't': b'\t', b'b': b'\b', b'f': b'\f', b'(': b'(', b')': b')', b'\\': b'\\'}


def is_pdf(data):
    return data[:1024].lstrip().startswith(b'%PDF-')


def unescape(raw):
    def repl(m):
        token = m.group(1)
        if token[:1].isdigit():
            return bytes([int(token, 8) & 0xFF])
        return ESCAPES.get(token, b'' if token in (b'\n', b'\r') else token)
    return re.sub(rb'\\([0-7]{1,3}|.|\n)', repl, raw, flags=re.S)


def fallback_text(data):
    """Decode simple content streams. Fonts with custom encodings (common in publisher PDFs) yield nothing usable."""
    lines, current = [], []
    for header, body in STREAM.findall(data):
        if b'/Subtype' in header and b'/Image' in header:
            continue
        if b'FlateDecode' in header:
            try:
                body = zlib.decompressobj().decompress(body)
            except zlib.error:
                continue
        elif b'/Filter' in header:
            continue
        for match in TEXT_OP.finditer(body):
            if match.group(3):
                if current:
                    lines.append(''.join(current))
                    current = []
                continue
            if match.group(2) is not None:
                current.append(unescape(match.group(2)).decode('latin-1'))
                continue
            for literal, kerning in LITERAL.findall(match.group(1)):
                # Large negative TJ offsets are word gaps in most generators.
                current.append(unescape(literal).decode('latin-1') if not kerning else ' ' if float(kerning) < -180 else '')
    if current:
        lines.append(''.join(current))
    return '\n'.join(line.strip() for line in lines if line.strip())


def readable(text):
    if not text or len(text) < 200:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in text)
    words = re.findall(r'[A-Za-z]{3,}', text)
    return letters / len(text) > 0.75 and len(words) > 40


def extract(data):
    """Return (text or None, method). None means the PDF is stored but its text could not be read reliably."""
    try:
        from pypdf import PdfReader
        logging.getLogger('pypdf').setLevel(logging.ERROR)
    except ImportError:
        PdfReader = None
    if PdfReader:
        try:
            text = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
            if readable(text):
                return text, 'pypdf'
        except Exception:
            pass
    try:
        text = fallback_text(data)
    except Exception:
        text = ''
    return (text, 'stdlib') if readable(text) else (None, 'pypdf' if PdfReader else 'stdlib')
