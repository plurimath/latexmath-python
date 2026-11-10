"""LaTeX stage preprocessing ported from its pinned Ruby contract."""
from html.entities import name2codepoint
import re
_TEXT = re.compile(r"\\(?:mbox|text)\{[^}]+\}")
_ENTITY = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")
def _decode_entity(match):
    token = match[1]
    if token.startswith("#x"):
        return chr(int(token[2:], 16))
    if token.startswith("#"):
        return chr(int(token[1:]))
    point = 39 if token == "apos" else name2codepoint.get(token)
    return chr(point) if point is not None else match[0]

def _hex_encode(text):
    return "".join(f"&#x{ord(c):x};" if c in "<>'\"&" or not 0x20 <= ord(c) <= 0x7e else c for c in text)
def preprocess(source):
    groups = iter(match.group() for match in _TEXT.finditer(source))
    text = _hex_encode(_ENTITY.sub(_decode_entity, source))
    text = re.sub(r"(?<!\\) |\n+", "", text)
    text = text.replace("\\\\ ", "\\\\\\\\").replace("&#x26;", "&").replace("&#x22;", '"')
    text = re.sub(r"(?<!\\\\)\\&#xa;", lambda _: "\\ ", text).replace("&#xa;", "")
    text = _TEXT.sub(lambda _: next(groups, ""), text)
    return text, None, False
