"""Character origin tracking ported from the stage Ruby SourceMap."""
import re
from .preprocessor import preprocess

def replace(text, origins, pattern, replacement):
    if isinstance(pattern, str):
        pattern = re.compile(re.escape(pattern))
    parts, mapped, pos = [], [], 0
    for match in pattern.finditer(text):
        start, end = match.span()
        piece = replacement(match) if callable(replacement) else replacement
        parts.extend([text[pos:start], piece])
        mapped.extend(origins[pos:start])
        mapped.extend([origins[start]] * len(piece))
        pos = end
    parts.append(text[pos:])
    mapped.extend(origins[pos:])
    return "".join(parts), mapped


from .preprocessor import _TEXT, _ENTITY, _decode_entity
_ENCODE = re.compile(r"[<>'\"&]|[^\u0020-\u007e]")
def mapped(source):
    originals = iter((m.group(), m.start()) for m in _TEXT.finditer(source))
    text, origins = source, list(range(len(source) + 1))
    text, origins = replace(text, origins, _ENTITY, _decode_entity)
    text, origins = replace(text, origins, _ENCODE, lambda m: f"&#x{ord(m[0]):x};")
    steps = [(re.compile(r"(?<!\\) |\n+"), ""), (re.compile(r"\\\\ "), "\\\\"),
             ("&#x26;", "&"), ("&#x22;", '"'), (re.compile(r"(?<!\\\\)\\&#xa;"), "\\ "), ("&#xa;", "")]
    for pattern, replacement in steps:
        text, origins = replace(text, origins, pattern, replacement)
    pieces, output, pos = [], [], 0
    for match in _TEXT.finditer(text):
        start, end = match.span()
        pieces.append(text[pos:start]); output.extend(origins[pos:start])
        group, origin = next(originals, ("", 0))
        pieces.append(group); output.extend(range(origin, origin + len(group)))
        pos = end
    pieces.append(text[pos:]); output.extend(origins[pos:])
    return "".join(pieces), output

def caller_position(source, text, char_offset):
    scan, origins = mapped(source)
    if scan != text or char_offset is None or not 0 <= char_offset <= len(scan):
        return None, None
    char = origins[char_offset]
    return char, len(source[:char].encode("utf-8"))
