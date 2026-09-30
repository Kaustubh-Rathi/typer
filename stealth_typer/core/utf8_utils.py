"""Port of StealthDesk ``src/core/utf8_utils.h``.

Line endings are normalized so auto-typing text containing ``\\r\\n`` does not
inject ENTER twice, and text is decoded byte-by-byte into Unicode codepoints
exactly the way the C++ helper does (invalid bytes become U+FFFD).
"""

from typing import List, Tuple

REPLACEMENT = 0xFFFD


def normalize_line_endings(text: str) -> str:
    """Normalize Windows (\\r\\n) and classic Mac (\\r) endings to \\n."""
    result: List[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\r":
            result.append("\n")
            if i + 1 < length and text[i + 1] == "\n":
                i += 1  # Skip \n in \r\n pair
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def utf8_to_codepoints(text: str) -> List[int]:
    """Decode a UTF-8 string into Unicode codepoints."""
    data = text.encode("utf-8", "surrogatepass")
    codepoints: List[int] = []
    i = 0
    length = len(data)

    while i < length:
        b0 = data[i]
        if b0 < 0x80:  # 1-byte ASCII
            codepoints.append(b0)
            i += 1
        elif (b0 & 0xE0) == 0xC0:  # 2-byte sequence
            if i + 1 < length:
                b1 = data[i + 1]
                codepoints.append(((b0 & 0x1F) << 6) | (b1 & 0x3F))
                i += 2
            else:
                codepoints.append(REPLACEMENT)
                i += 1
        elif (b0 & 0xF0) == 0xE0:  # 3-byte sequence
            if i + 2 < length:
                b1 = data[i + 1]
                b2 = data[i + 2]
                codepoints.append(((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F))
                i += 3
            else:
                codepoints.append(REPLACEMENT)
                i += 1
        elif (b0 & 0xF8) == 0xF0:  # 4-byte sequence
            if i + 3 < length:
                b1 = data[i + 1]
                b2 = data[i + 2]
                b3 = data[i + 3]
                cp = (
                    ((b0 & 0x07) << 18)
                    | ((b1 & 0x3F) << 12)
                    | ((b2 & 0x3F) << 6)
                    | (b3 & 0x3F)
                )
                codepoints.append(cp)
                i += 4
            else:
                codepoints.append(REPLACEMENT)
                i += 1
        else:  # Invalid UTF-8 lead byte
            codepoints.append(REPLACEMENT)
            i += 1

    return codepoints


def codepoint_to_utf8(cp: int) -> str:
    """Encode a Unicode codepoint back to a UTF-8 string."""
    out = bytearray()
    if cp <= 0x7F:
        out.append(cp)
    elif cp <= 0x7FF:
        out.append(0xC0 | ((cp >> 6) & 0x1F))
        out.append(0x80 | (cp & 0x3F))
    elif cp <= 0xFFFF:
        out.append(0xE0 | ((cp >> 12) & 0x0F))
        out.append(0x80 | ((cp >> 6) & 0x3F))
        out.append(0x80 | (cp & 0x3F))
    elif cp <= 0x10FFFF:
        out.append(0xF0 | ((cp >> 18) & 0x07))
        out.append(0x80 | ((cp >> 12) & 0x3F))
        out.append(0x80 | ((cp >> 6) & 0x3F))
        out.append(0x80 | (cp & 0x3F))
    return out.decode("latin-1")


def codepoint_to_surrogates(cp: int) -> Tuple[int, int]:
    """Convert a codepoint > 0xFFFF into a UTF-16 high/low surrogate pair."""
    if cp <= 0xFFFF:
        return (cp, 0)
    v = cp - 0x10000
    return (0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF))
