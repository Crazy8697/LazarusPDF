"""Reading order (recursive XY-cut, column aware) and garble checks."""
import re
import unicodedata

X_GAP = 7.0   # pt: minimum gutter between columns
Y_GAP = 0.5   # pt: minimum gap between stacked rows

_OK_SYMBOLS = set(".,;:!?'\"()[]{}-–—/\\&%$#@*+=<>“”‘’…°§¶•·|_~^`©®™±×÷½¼¾")


def _split(idx, boxes, axis, min_gap):
    """Group indices into bands separated by gaps along axis (0 = x, 1 = y)."""
    lo, hi = (0, 2) if axis == 0 else (1, 3)
    order = sorted(idx, key=lambda i: boxes[i][lo])
    groups, cur, end = [], [], None
    for i in order:
        b = boxes[i]
        if cur and b[lo] - end >= min_gap:
            groups.append(cur)
            cur, end = [], None
        cur.append(i)
        end = b[hi] if end is None else max(end, b[hi])
    if cur:
        groups.append(cur)
    return groups


def _order(idx, boxes):
    if len(idx) <= 1:
        return list(idx)
    cols = _split(idx, boxes, 0, X_GAP)
    if len(cols) > 1:
        return [i for c in cols for i in _order(c, boxes)]
    rows = _split(idx, boxes, 1, Y_GAP)
    if len(rows) > 1:
        # Rows that are themselves multi-column get merged, so a two-column body
        # whose paragraph breaks happen to line up is still read column by column.
        merged = []
        for r in rows:
            multi = len(_split(r, boxes, 0, X_GAP)) > 1
            if merged and multi and merged[-1][1]:
                merged[-1][0].extend(r)
            else:
                merged.append([list(r), multi])
        if len(merged) == 1:
            merged = [[r, False] for r in rows]
        out = []
        for group, _ in merged:
            if len(group) == len(idx):  # no progress; fall back
                out.extend(sorted(group, key=lambda i: (boxes[i][1], boxes[i][0])))
            else:
                out.extend(_order(group, boxes))
        return out
    return sorted(idx, key=lambda i: (boxes[i][1], boxes[i][0]))


def reading_order(boxes):
    """boxes: list of (x0, y0, x1, y1). Returns indices in reading order."""
    return _order(list(range(len(boxes))), boxes)


def join_lines(lines):
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not out:
            out = ln
        elif out.endswith("-"):
            out += ln          # keep the hyphen; never guess whether it was a break
        else:
            out += " " + ln
    return out


def garble_ratio(text):
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    bad = 0.0
    for c in chars:
        o = ord(c)
        if c == "�" or 0xE000 <= o <= 0xF8FF or unicodedata.category(c) in ("Cc", "Co", "Cn", "Cs"):
            bad += 1
        elif not (c.isalnum() or c in _OK_SYMBOLS):
            bad += 0.5
    return bad / len(chars)


_WORD = re.compile(r"[A-Za-z]{4,}")


def looks_garbled(text, strict=False):
    """True if text is clearly not readable English. strict = OCR output."""
    if garble_ratio(text) > (0.20 if strict else 0.30):
        return True
    if "(cid:" in text:
        return True
    if strict:
        words = _WORD.findall(text)
        if len(words) >= 3:
            novowel = sum(1 for w in words if not re.search(r"[aeiouyAEIOUY]", w))
            if novowel / len(words) > 0.4:
                return True
    return False
