#!/usr/bin/env python3
"""
prep_text.py: turn an EPUB, PDF, or TXT into clean text for TTS audiobook tools.

What it does:
  - Rejoins lines that break in the middle of a sentence. This covers PDF-style
    hard wraps, EPUBs converted from PDFs where every printed line became its
    own paragraph, and chapters split across files mid-sentence.
  - Fixes words hyphenated across line breaks ("con-" + "trol" -> "control"),
    but keeps real compounds ("self-control", "mob-psychology"). It uses the
    book's own vocabulary to decide.
  - Drops page numbers and running headers that repeat page after page.
  - Drops footnote reference numbers (<sup>) in EPUBs so they aren't read aloud.
  - Makes headings speakable: "CHAPTER IV" becomes "Chapter 4." The added
    period gives the voice a clean stop.

Output: one paragraph per line, a blank line between paragraphs, and two blank
lines before each chapter (tts-audiobook-tool treats 2+ blank lines as a
section break). Every line ends at the end of a sentence. With --sentences,
each sentence gets its own line inside the paragraph.

Usage:
  python3 prep_text.py book.epub                  # writes book.tts.txt
  python3 prep_text.py book.pdf -o clean.txt      # needs pdftotext (poppler-utils)
  python3 prep_text.py book.txt --sentences
  python3 prep_text.py book.pdf --keep-furniture  # don't remove headers/page numbers

Standard library only. Uses pysbd for sentence splitting if it's installed,
otherwise a built-in splitter. If English word lists are installed in
/usr/share/dict (they are in the Docker image), it uses them to judge hyphen
splits and to list words that look like OCR errors.
"""

import argparse
import posixpath
import re
import shutil
import statistics
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

SECTION = "\x03"
H_OPEN, H_CLOSE = "\x01", "\x02"

BLOCK_TAGS = {
    "p", "div", "li", "blockquote", "section", "article", "tr", "pre", "dd", "dt",
    "figcaption", "header", "footer", "aside", "table", "ul", "ol", "hr", "body",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Content inside these is dropped. <sup> is mostly footnote reference numbers.
SKIP_TAGS = {"script", "style", "head", "title", "sup"}

CLOSERS = r"[\"'”’)\]*_]*"
# Line ends in punctuation that can end a sentence or a clause.
TERMINAL_ANY = re.compile(r"[.!?…:;—–]" + CLOSERS + r"$")
# Line ends a sentence.
END_SENT = re.compile(r"[.!?…]" + CLOSERS + r"$")
# A paragraph can end here: end of sentence, a colon, or a dash that closes a quote.
PARA_END = re.compile(r"(?:[.!?…:]" + CLOSERS + r"|[—–][\"'”’)\]]+)$")
# A line that is only a page number (arabic, or lowercase roman for front matter).
PAGE_NUM = re.compile(r"^[\W_]*(\d{1,4}|[ivxlcdm]{1,7})[\W_]*$")
ROMAN = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
CHAPTER_WORD = r"(?:CHAPTER|Chapter|PART|Part|BOOK|Book|SECTION|Section|VOLUME|Volume)"
NAMED_HEADING = re.compile(r"^" + CHAPTER_WORD + r"\s+(?:[0-9]+|[IVXLC]+|[A-Z][a-z]+)\b")
STANDALONE_HEADING = re.compile(
    r"^(?:preface|introduction|prologue|epilogue|foreword|afterword|contents|appendix|"
    r"acknowledg(?:e)?ments|notes|index|conclusion|bibliography)\W*$",
    re.I,
)
# Prefixes whose hyphen is nearly always real, so "self-" + "control" stays "self-control".
KEEP_HYPHEN = {"self", "well", "ill", "half", "non", "anti", "quasi", "semi", "pseudo", "ultra", "neo"}
SMALL_WORDS = {"a", "an", "the", "and", "or", "nor", "but", "of", "in", "on", "at", "to",
               "for", "by", "with", "from", "as", "vs"}
ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "no",
    "vol", "ch", "fig", "pp", "ed", "rev", "gen", "col", "capt", "lt", "sgt", "hon",
    "mt", "ft", "viz", "cf", "al", "mme", "mlle", "messrs",
}
REPLACEMENTS = [
    ("­", ""), ("​", ""), ("﻿", ""),               # soft hyphen, zero-width, BOM
    (" ", " "), (" ", " "), (" ", " "),            # odd spaces
    ("ﬀ", "ff"), ("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬃ", "ffi"), ("ﬄ", "ffl"), ("ﬅ", "st"), ("ﬆ", "st"),
    ("‘‘", "“"), ("’’", "”"), ("``", "“"),                         # common OCR double-quote errors
]
SHORT_RATIO = 0.8  # a sentence-ending line shorter than this x typical line ends a paragraph
# Word lists used for hyphen decisions and the words-to-check list, when present
# (Debian's wamerican/wbritish packages; the Docker image installs them).
DICT_FILES = "/usr/share/dict/*-english*"


class PrepError(Exception):
    pass


_dictionary = None


def dictionary():
    """Lowercase words from the system word lists, or an empty set if there are none."""
    global _dictionary
    if _dictionary is None:
        import glob, os
        words = set()
        for f in glob.glob(os.environ.get("PREP_DICT", DICT_FILES)):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    for w in fh:
                        w = w.strip().lower().replace("’", "'")
                        if w:
                            words.add(w)               # includes contractions like "isn't"
            except OSError:
                pass
        _dictionary = words
    return _dictionary


# ---------------------------------------------------------------- input

class XHTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in HEADING_TAGS:
            self.parts.append("\n\n" + H_OPEN)
        elif tag == "br":
            self.parts.append("\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.parts.append("\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in HEADING_TAGS:
            self.parts.append(H_CLOSE + "\n\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def read_epub(path):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next(e for e in container.iter() if e.tag.split("}")[-1] == "rootfile")
        opf_path = rootfile.get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(z.read(opf_path))

        manifest = {}
        for e in opf.iter():
            if e.tag.split("}")[-1] == "item":
                manifest[e.get("id")] = (e.get("href", ""), e.get("media-type", ""), e.get("properties", ""))

        docs = []
        for ref in opf.iter():
            if ref.tag.split("}")[-1] != "itemref" or ref.get("linear", "yes") == "no":
                continue
            item = manifest.get(ref.get("idref"))
            if not item:
                continue
            href, mtype, props = item
            if "nav" in props.split():
                continue
            if "html" not in mtype and not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            full = posixpath.normpath(posixpath.join(opf_dir, unquote(href.split("#")[0])))
            if full not in names:
                continue
            parser = XHTMLText()
            parser.feed(z.read(full).decode("utf-8", errors="replace"))
            parser.close()
            docs.append(parser.text())
    if not docs:
        raise PrepError("No readable chapters found in the EPUB.")
    return f"\n{SECTION}\n".join(docs)


def read_pdf(path):
    if not shutil.which("pdftotext"):
        raise PrepError("pdftotext not found. Run inside: nix shell nixpkgs#poppler_utils")
    out = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(path), "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    if not out.strip():
        raise PrepError("The PDF has no text layer (it is scanned page images). Run OCR on it first.")
    return out


def decode_text(data):
    """Bytes of a text file to str: UTF-8, UTF-16 (with or without a BOM), or Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    head = data[:8000]
    if head.count(b"\x00") > len(head) // 4:                  # UTF-16 without a BOM
        le = head[1::2].count(b"\x00") > head[0::2].count(b"\x00")
        return data.decode("utf-16-le" if le else "utf-16-be", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")             # older Windows and OCR exports


def read_txt(path):
    text = decode_text(Path(path).read_bytes())
    # Two or more blank lines = possible section break (Calibre's chapter convention).
    return re.sub(r"\n[ \t]*\n[ \t]*\n(?:[ \t]*\n)*", f"\n{SECTION}\n", text)


def _join_heading(m):
    out = ""
    for part in m.group(1).split("\n"):
        part = " ".join(part.split())
        if not part:
            continue
        if out:
            out += " " if TERMINAL_ANY.search(out) or out.endswith(",") else ". "
        out += part
    return H_OPEN + out + H_CLOSE


def normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    # A soft hyphen at the end of a line is a word broken across lines: treat it as "-".
    text = re.sub("\u00ad[ \t]*\n", "-\n", text)
    for a, b in REPLACEMENTS:
        text = text.replace(a, b)
    # A heading split by <br> ("CHAPTER 1<br/>INTRODUCTION") goes on one line, with a pause.
    return re.sub(re.escape(H_OPEN) + r"(.*?)" + re.escape(H_CLOSE), _join_heading, text, flags=re.S)


def to_units(text):
    units = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t　]+", " ", raw).strip()
        if line == SECTION:
            units.append(("section", ""))
        elif H_OPEN in line or H_CLOSE in line:
            h = line.replace(H_OPEN, "").replace(H_CLOSE, "").strip()
            if h:
                units.append(("heading", h))
        elif not re.search(r"\w", line):
            units.append(("blank", ""))   # empty, or only symbols like "* * *"
        else:
            units.append(("text", line))
    return units


# ---------------------------------------------------------------- analysis

def detect_mode(units):
    texts = [t for k, t in units if k == "text"]
    if not texts:
        raise PrepError("No text found in the file.")
    mid_sentence = sum(1 for t in texts if not TERMINAL_ANY.search(t)) / len(texts)
    return ("physical" if mid_sentence > 0.35 else "paragraph"), mid_sentence


def blanks_are_paragraph_breaks(units):
    with_blank = without_blank = 0
    prev_text = saw_blank = False
    for k, _ in units:
        if k == "text":
            if prev_text:
                if saw_blank:
                    with_blank += 1
                else:
                    without_blank += 1
            prev_text, saw_blank = True, False
        elif k == "blank":
            saw_blank = True
        else:
            prev_text = saw_blank = False
    total = with_blank + without_blank
    # If nearly every line is followed by a blank, blanks carry no meaning.
    return total == 0 or with_blank / total < 0.5


def furniture_key(line):
    if NAMED_HEADING.match(line):
        return line.lower()            # "Chapter 1" and "Chapter 2" are different lines
    return re.sub(r"[\d\W_]+", " ", line).strip().lower()   # "12 MAN THE PUPPET" -> "man the puppet"


def strip_furniture(units, stats):
    # Short lines that repeat 3+ times, spread out through the book, are running headers.
    # The spread check keeps short lists ("Step 1", "Step 2", ...) safe.
    seen = defaultdict(list)
    n = 0
    for k, t in units:
        if k != "text":
            continue
        if len(t) <= 70 and not TERMINAL_ANY.search(t):
            key = furniture_key(t)
            if key:
                seen[key].append(n)
        n += 1
    repeated = {
        key for key, pos in seen.items()
        if len(pos) >= 3 and statistics.median([b - a for a, b in zip(pos, pos[1:])]) >= 4
    }
    out = []
    for k, t in units:
        if k == "text":
            if PAGE_NUM.match(t):
                stats["page_numbers"] += 1
                continue
            if len(t) <= 70 and not TERMINAL_ANY.search(t) and furniture_key(t) in repeated:
                stats["headers"][furniture_key(t)] += 1
                continue
        out.append((k, t))
    return out


def is_heading_like(line):
    if len(line) > 80:
        return False
    if NAMED_HEADING.match(line) or STANDALONE_HEADING.match(line):
        if len(line) <= 40 or not TERMINAL_ANY.search(line):
            return True                # "CHAPTER I." counts, a sentence starting "Chapter 3 ..." doesn't
    if TERMINAL_ANY.search(line):
        return False
    if ROMAN.fullmatch(line.rstrip(".")) and line.rstrip("."):
        return True                    # a bare "IV" on its own line
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) > 0.7


def starts_lowercase(line):
    m = re.search(r"[A-Za-z]", line)
    return bool(m) and m.group().islower()


def is_label(line, max_len):
    # A short unpunctuated line like "The Art of Suggestion" or "Step 1".
    if len(line) > max_len or TERMINAL_ANY.search(line) or sum(c.isalpha() for c in line) < 2:
        return False
    last = re.findall(r"[A-Za-z]+", line)
    return not last or last[-1].lower() not in SMALL_WORDS | {"his", "her", "its", "their", "my", "our", "your"}


def classify_headings(units, mode, typical, blanks_meaningful=False):
    # A heading-looking line only counts as a heading if it sits between sentences.
    # If the sentence before it is unfinished, or the next line carries on in
    # lowercase, it's body text and stays in the paragraph.
    max_label = 60 if mode == "paragraph" else 0.6 * typical
    idx = [i for i, (k, _) in enumerate(units) if k == "text"]
    nxt = {i: (units[idx[n + 1]][1] if n + 1 < len(idx) else "") for n, i in enumerate(idx)}
    out, prev = [], None
    for i, (k, t) in enumerate(units):
        if k == "text":
            between = (prev is None or TERMINAL_ANY.search(prev)) and not starts_lowercase(nxt[i])
            # In text with blank lines between paragraphs (Project Gutenberg style), a short
            # unpunctuated line standing alone is a title or heading.
            alone = blanks_meaningful and (i == 0 or units[i - 1][0] != "text") and (
                i + 1 == len(units) or units[i + 1][0] != "text")
            if between and (is_heading_like(t) or is_label(t, 80 if alone else max_label)):
                out.append(("heading", t))
                prev = None
                continue
            prev = t
        elif k in ("heading", "section"):
            prev = None
        out.append((k, t))
    return out


def typical_length(units):
    lengths = sorted(len(t) for k, t in units if k == "text")
    if not lengths:
        return 0
    if len(lengths) < 10:
        return max(lengths)
    return statistics.quantiles(lengths, n=10)[7]  # 80th percentile


def build_vocab(units):
    """Words as the book spells them, skipping the halves of line-end hyphenations."""
    plain, hyph, prefixes = Counter(), Counter(), Counter()
    carry = False
    for k, t in units:
        if k != "text":
            continue
        toks = re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", t)
        ends_split = bool(re.search(r"[A-Za-z]-$", t))
        if carry and toks and starts_lowercase(t):
            toks = toks[1:]
        if ends_split and toks:
            toks = toks[:-1]
        for w in toks:
            w = w.lower()
            if "-" in w:
                hyph[w] += 1
                prefixes[w.split("-")[0]] += 1
            else:
                plain[w] += 1
        carry = ends_split
    return plain, hyph, prefixes


def keep_hyphen(first, second, vocab):
    plain, hyph, prefixes = vocab
    f, s = first.lower(), second.lower()
    joined, hyphened = f + s, f"{f}-{s}"
    if plain[joined] or hyph[hyphened]:
        return hyph[hyphened] > plain[joined]      # the book spells it elsewhere
    words = dictionary()
    if words:
        if joined in words:
            return False                           # appa- + ratus -> apparatus
        if f in words and s in words and len(f) >= 3:
            return True                            # mob- + psychology -> mob-psychology
    if f in KEEP_HYPHEN or (len(f) >= 3 and prefixes[f]):
        return True                                # the book uses "mob-..." compounds elsewhere
    # Both halves are words in their own right ("mob-psychology"): keep the hyphen.
    # A kept hyphen just sounds like two words; a wrong join makes a non-word.
    return len(f) >= 3 and len(s) >= 3 and plain[f] > 0 and plain[s] > 0


# ---------------------------------------------------------------- assembly

def tidy(text):
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def end_cleanly(text):
    # TTS models stop more cleanly on a final period.
    text = text.rstrip(" ,-")
    return text if TERMINAL_ANY.search(text) else text + "."


def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = vals[a]
        total += -v if b != " " and vals[b] > v else v
    return total


def speakable_heading(h):
    h = tidy(h)
    letters = [c for c in h if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        words = []
        for i, w in enumerate(h.split(" ")):
            core = re.sub(r"\W", "", w)
            if core and ROMAN.fullmatch(core):
                words.append(w)                                    # keep IV as IV for now
            elif i > 0 and core.lower() in SMALL_WORDS:
                words.append(w.lower())
            else:
                words.append(re.sub(r"[a-z]", lambda m: m.group().upper(), w.lower(), count=1))
        h = " ".join(words)
    # Roman numerals after Chapter/Part/Book, or alone: "Chapter IV" -> "Chapter 4"
    m = re.match(r"(" + CHAPTER_WORD + r"\s+)([IVXLCDM]+)\b", h)
    if m and ROMAN.fullmatch(m.group(2)):
        h = m.group(1) + str(roman_to_int(m.group(2))) + h[m.end():]
    elif ROMAN.fullmatch(h.rstrip(".")) and h.rstrip("."):
        h = str(roman_to_int(h.rstrip(".")))
    return end_cleanly(h)


def join(cur, line, stats, vocab):
    stats["joined"] += 1
    m = re.search(r"([A-Za-z]+)-$", cur)
    if m:
        nxt = re.match(r"[a-z]+", line)
        compound = re.search(r"[A-Za-z]-[A-Za-z]+-$", cur)    # "up-to-" + "date"
        if nxt and not compound and not keep_hyphen(m.group(1), nxt.group(0), vocab):
            stats["dehyphenated"] += 1
            return cur[:-1] + line                 # con- + trol -> control
        return cur + line                          # self- + control, anti- + American
    if re.search(r"(?:[—–]|--|\S-)$", cur):
        return cur + line                          # dashes and number ranges: no space
    m, nxt = re.search(r"(?<![A-Za-z'’])([A-Za-z]+)$", cur), re.match(r"([a-z]+)(?![A-Za-z])", line)
    if m and nxt and is_split_word(m.group(1), nxt.group(1)):
        stats["dehyphenated"] += 1
        return cur + line                          # converter dropped the hyphen: seri + ously
    return cur + " " + line


def is_split_word(a, b):
    """True when a and b only make sense joined: "seri" + "ously" -> "seriously"."""
    words = dictionary()
    if not words:
        return False
    a, b = a.lower(), b.lower()
    joined = a + b
    if len(joined) < 5 or joined not in words:
        return False
    # Halves are checked strictly (no inflections), so "ear nestly" counts as split.
    return not (a in words or a in ARCHAIC) or not (b in words or b in ARCHAIC)


def breaks_after(prev, blank, mode, typical, blanks_meaningful):
    if not PARA_END.search(prev):
        return False                               # sentence continues: always rejoin
    if mode == "paragraph" or (blank and blanks_meaningful):
        return True
    return bool(END_SENT.search(prev)) and len(prev) < SHORT_RATIO * typical


def build_blocks(units, mode, typical, blanks_meaningful, stats, vocab):
    blocks, cur, prev, blank, pending = [], None, None, False, False

    def flush():
        nonlocal cur
        if cur:
            blocks.append(("para", end_cleanly(tidy(cur))))
        cur = None

    for k, t in units:
        if k == "section":
            pending = True                         # decide once we see what follows
        elif k == "blank":
            blank = True
        elif k == "heading":
            flush()
            chapter = pending or NAMED_HEADING.match(t) or STANDALONE_HEADING.match(t)
            if chapter and blocks and blocks[-1][0] != "heading":
                blocks.append(("section", ""))
            blocks.append(("heading", speakable_heading(t)))
            prev, blank, pending = None, False, False
        else:
            if cur is None:
                cur = t
            elif pending:
                # File/section boundary with no heading after it. Join if it split a sentence.
                if PARA_END.search(prev):
                    flush()
                    cur = t
                else:
                    cur = join(cur, t, stats, vocab)
            elif breaks_after(prev, blank, mode, typical, blanks_meaningful):
                flush()
                cur = t
            else:
                cur = join(cur, t, stats, vocab)
            prev, blank, pending = t, False, False
    flush()
    return blocks


_segmenter = None


def split_sentences(text):
    global _segmenter
    if _segmenter is None:
        try:
            import pysbd
            _segmenter = pysbd.Segmenter(language="en", clean=False)
        except ImportError:
            _segmenter = False
    if _segmenter:
        return [s.strip() for s in _segmenter.segment(text) if s.strip()]

    out, start = [], 0
    for m in re.finditer(r"([.!?…][\"'”’)\]]*)\s+(?=[\"'“‘(\[]?[A-Z0-9])", text):
        chunk = text[start:m.end(1)]
        word = re.search(r"([A-Za-z.]*)[.!?…][\"'”’)\]]*$", chunk)
        word = word.group(1).lower().rstrip(".") if word else ""
        if text[m.start(1)] == "." and (word in ABBREV or re.fullmatch(r"[a-z]", word)):
            continue                               # Mr. / e.g. / initials like J.
        out.append(chunk.strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def render(blocks, sentences):
    parts, gap = [], "\n\n"
    for kind, text in blocks:
        if kind == "section":
            gap = "\n\n\n"
            continue
        body = "\n".join(split_sentences(text)) if sentences and kind == "para" else text
        if parts:
            parts.append(gap)
        parts.append(body)
        gap = "\n\n"
    return "".join(parts).strip() + "\n"


# ---------------------------------------------------------------- checks

def words_to_check(text, limit=300):
    """Lowercase words that aren't in the word lists: usually OCR errors like "codperation".
    Capitalized words are skipped, since most of them are names."""
    words = dictionary()
    if not words:
        return []
    counts = Counter()
    for tok in re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)*", text):
        if not tok[:1].islower():
            continue
        t = tok.lower().replace("’", "'")
        if t in words or t in ARCHAIC:
            continue
        m = CONTRACTION.match(t)
        if m:
            stem, ending = m.groups()
            if ending in ("n't",) and (stem in words or stem in NT_STEMS):
                continue                             # isn't, doesn't, couldn't, can't, won't
            if ending != "n't" and (len(stem) < 3 or known(stem, words)):
                continue                             # it's, they're, we'll, you've, dog's
            # flag the stem for possessives ("codperation's"), the whole word otherwise
            flag = tok[: len(stem)] if ending == "'s" else tok
        else:
            if len(t) < 3 or known(t, words) or ROMAN.fullmatch(t.upper()):
                continue                             # also skips page refs like "xiv"
            flag = tok
        counts[flag] += 1
    return counts.most_common(limit)


CONTRACTION = re.compile(r"^(.+?)(n't|'s|'d|'ll|'re|'ve|'m|'t)$")
# Stems that don't stand alone as words: can't, won't, shan't, ain't.
NT_STEMS = {"ca", "wo", "sha", "ai", "do", "does", "did", "is", "was", "are", "were", "has", "have",
            "had", "could", "would", "should", "must", "might", "need", "dare", "ought", "may"}
# Old-book contractions; a leading apostrophe is dropped by the tokenizer ('tis -> tis).
ARCHAIC = {"tis", "twas", "twere", "twill", "twould", "neath", "gainst", "o'er", "e'er", "ne'er",
           "e'en", "o'clock", "ma'am", "y'all", "th'", "o'", "an'", "fo'c'sle", "bo'sun", "shew",
           "shewn", "shewed", "shewing", "connexion", "connexions"}


def split_words(text, limit=200):
    """Pairs like "seri ously" or "seri-ously" that should be one word. Returns [from, to, count]."""
    if not dictionary():
        return []
    counts = Counter()
    for m in re.finditer(r"(?<![A-Za-z'’])(?=([A-Za-z]+)(- ?| )([a-z]+)(?![A-Za-z'’]))", text):
        a, sep, b = m.groups()
        if is_split_word(a, b):
            counts[(a + sep + b, a + b)] += 1
    return [[f, t, n] for (f, t), n in counts.most_common(limit)]


SUFFIXES = ("iest", "ness", "ment", "ies", "ied", "ier", "ily", "ing", "est", "es", "ed", "er", "ly", "s", "d")


def known(w, words):
    """In the word list, directly or as a regular inflection (parties, carried, measuring)."""
    if w in words:
        return True
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            b = w[: -len(suf)]
            cands = {b, b + "e"}
            if suf.startswith("i"):
                cands.add(b + "y")
            if b[-1] == b[-2] and b[-1] not in "aeiou":
                cands.add(b[:-1])                  # stopped -> stop
            if cands & words:
                return True
    return False


# ---------------------------------------------------------------- main

def read_any(src):
    suffix = Path(src).suffix.lower()
    if suffix == ".epub":
        return read_epub(src)
    if suffix == ".pdf":
        return read_pdf(src)
    return read_txt(src)


def process(src, sentences=False, keep_furniture=False):
    """Convert a file. Returns a dict with the text, summary lines, headings and words to check."""
    raw = read_any(src)
    stats = Counter()
    stats["headers"] = Counter()
    units = to_units(normalize(raw))
    mode, mid = detect_mode(units)
    if not keep_furniture:
        units = strip_furniture(units, stats)
    typical = typical_length(units)
    blanks_meaningful = blanks_are_paragraph_breaks(units)
    units = classify_headings(units, mode, typical, blanks_meaningful)
    vocab = build_vocab(units)
    blocks = build_blocks(units, mode, typical, blanks_meaningful, stats, vocab)
    text = render(blocks, sentences)

    paras = sum(1 for k, _ in blocks if k == "para")
    heads = [t for k, t in blocks if k == "heading"]
    sections = sum(1 for k, _ in blocks if k == "section")
    summary = []
    if mode == "physical":
        summary.append(f"Layout: hard-wrapped lines ({mid:.0%} of lines broke mid-sentence)")
    else:
        summary.append(f"Layout: mostly one paragraph per line ({mid:.0%} of lines broke mid-sentence)")
    summary.append(f"Rejoined {stats['joined']:,} line breaks; fixed {stats['dehyphenated']:,} hyphenated words")
    removed = sum(stats["headers"].values())
    if not keep_furniture:
        top = ", ".join(f'"{k}" ({n}x)' for k, n in stats["headers"].most_common(3))
        summary.append(f"Removed {stats['page_numbers']:,} page numbers and {removed:,} running-header lines"
                       + (f": {top}" if top else ""))
    summary.append(f"{paras:,} paragraphs, {len(heads):,} headings, {sections:,} chapter breaks")
    return {
        "text": text,
        "summary": summary,
        "headings": heads,
        "check": words_to_check(text),
        "joins": split_words(text),
        "stats": {
            "layout": mode, "joined": stats["joined"], "dehyphenated": stats["dehyphenated"],
            "page_numbers": stats["page_numbers"], "headers": removed,
            "paragraphs": paras, "headings": len(heads), "chapters": sections,
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Clean an EPUB/PDF/TXT into TTS-friendly text.")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="default: <input name>.tts.txt")
    ap.add_argument("--sentences", action="store_true", help="one sentence per line inside paragraphs")
    ap.add_argument("--keep-furniture", action="store_true", help="don't remove page numbers / running headers")
    args = ap.parse_args()

    try:
        result = process(args.input, args.sentences, args.keep_furniture)
    except PrepError as e:
        sys.exit(str(e))
    out_path = args.output or args.input.with_name(args.input.stem + ".tts.txt")
    out_path.write_text(result["text"], encoding="utf-8")

    log = lambda s: print(s, file=sys.stderr)
    for line in result["summary"]:
        log(line)
    log(f"Wrote {out_path}")
    heads = result["headings"]
    if heads:
        log("Headings (check for leftover junk): " + " | ".join(heads[:15]) + (" | ..." if len(heads) > 15 else ""))
    if result["check"]:
        log("Not in the word list (possible OCR errors): " + ", ".join(w for w, _ in result["check"][:25]))
    if result["joins"]:
        log("Probably one word split in two: " + ", ".join(f'"{f}" -> {t}' for f, t, _ in result["joins"][:15]))


if __name__ == "__main__":
    main()
