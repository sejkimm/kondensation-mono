# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools>=4.50", "brotli", "uharfbuzz"]
# ///
"""Build Kondensation Mono: Geist Mono condensed to a 0.55em cell, Pretendard Hangul in 1.1em cells, Nerd Font icons.

Latin: Geist Mono (variable) is instanced slightly heavier, then every glyph, advance and GPOS x value is
scaled by 550/600, so vertical stems keep the weight of the nominal style while spacing shrinks in proportion.
Hangul and CJK punctuation: Pretendard, scaled 1.15x (as in Jetendard) and centred in 1100-unit boxes.
Icons: Symbols Nerd Font Mono, scaled into 550-unit cells. A ccmp lookup composes conjoining jamo (NFD).

Usage: uv run build.py            # downloads sources into ./sources, writes ./dist/*.ttf
"""

import io
import math
import statistics
import urllib.request
import zipfile
from pathlib import Path

from fontTools.otlLib.builder import buildCoverage, buildLigatureSubstSubtable, buildLookup, buildSingleSubstSubtable
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.varLib import instancer
import uharfbuzz as hb

ROOT = Path(__file__).parent
SOURCES, DIST = ROOT / "sources", ROOT / "dist"
FAMILY, PS_FAMILY = "Kondensation Mono", "KondensationMono"

GEIST = "https://raw.githubusercontent.com/vercel/geist-font/main/fonts/GeistMono/variable/"
GEIST_FILES = {False: "GeistMono%5Bwght%5D.ttf", True: "GeistMono-Italic%5Bwght%5D.ttf"}
PRETENDARD_ZIP = "https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip"
NERD_ZIP = "https://github.com/ryanoasis/nerd-fonts/releases/download/v3.4.0/NerdFontsSymbolsOnly.zip"
NERD_FILE = "SymbolsNerdFontMono-Regular.ttf"
NERD_LICENSE = "LICENSE-NerdFonts.txt"  # MIT text, shipped next to the fonts in the release zip

COPYRIGHT = ("Copyright 2026 sejkimm. Based on Geist Mono (The Geist Project Authors, Vercel), "
             "Pretendard (Kil Hyung-jin) and Nerd Fonts symbols (Ryan L McIntyre).")
LICENSE = ("This Font Software is licensed under the SIL Open Font License, Version 1.1. "
           "This license is available with a FAQ at: https://openfontlicense.org")
LICENSE_URL = "https://openfontlicense.org"

SRC_CELL, CELL = 600, 550
SX = CELL / SRC_CELL
KOREAN_SCALE = 1.15  # Jetendard's enlargement of Pretendard Hangul
# Codepoints drawn from Pretendard, all two cells wide (halfwidth forms FF61-FFDC are left out).
KOREAN_RANGES = [(0x1100, 0x11FF), (0x3000, 0x303F), (0x3130, 0x318F), (0xA960, 0xA97F), (0xAC00, 0xD7A3),
                 (0xD7B0, 0xD7FF), (0xFF01, 0xFF60), (0xFFE0, 0xFFE6)]
# Powerline separators are stretched to fill the cell so neighbouring segments join; the Powerline symbols
# (branch, line number, lock) keep their shape at full line height, as in Nerd Font patched fonts.
POWERLINE_STRETCH = [*range(0xE0B0, 0xE0C9), 0xE0CA, 0xE0D2, 0xE0D4, 0xE0D6, 0xE0D7]
POWERLINE_SYMBOLS = range(0xE0A0, 0xE0A4)
WEIGHTS = [("Thin", 100), ("ExtraLight", 200), ("Light", 300), ("Regular", 400),
           ("Medium", 500), ("SemiBold", 600), ("Bold", 700), ("ExtraBold", 800)]


# ---------------------------------------------------------------- sources

def fetch_sources() -> None:
    SOURCES.mkdir(exist_ok=True)
    for italic, name in GEIST_FILES.items():
        dst = SOURCES / name.replace("%5B", "[").replace("%5D", "]")
        if not dst.exists():
            dst.write_bytes(urllib.request.urlopen(GEIST + name).read())
    if not all((SOURCES / f"Pretendard-{w}.ttf").exists() for w, _ in WEIGHTS):
        with zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(PRETENDARD_ZIP).read())) as z:
            for w, _ in WEIGHTS:
                name = f"Pretendard-{w}.ttf"
                (SOURCES / name).write_bytes(z.read(f"public/static/alternative/{name}"))
    if not ((SOURCES / NERD_FILE).exists() and (SOURCES / NERD_LICENSE).exists()):
        with zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(NERD_ZIP).read())) as z:
            (SOURCES / NERD_FILE).write_bytes(z.read(NERD_FILE))
            (SOURCES / NERD_LICENSE).write_bytes(z.read("LICENSE"))


def geist_path(italic: bool) -> Path:
    return SOURCES / GEIST_FILES[italic].replace("%5B", "[").replace("%5D", "]")


def style_name(weight: str, italic: bool) -> str:
    if weight == "Regular":
        return "Italic" if italic else "Regular"
    return weight + ("Italic" if italic else "")


# ---------------------------------------------------------------- Latin

def stem(font: TTFont, wght: float) -> float:
    gs = font.getGlyphSet(location={"wght": wght})
    bp = BoundsPen(gs)
    gs[font.getBestCmap()[ord("|")]].draw(bp)
    return bp.bounds[2] - bp.bounds[0]


def compensated_weight(vf: TTFont, nominal: int) -> float:
    """Weight whose vertical stems, after condensing by SX, match the nominal weight's stems."""
    target, lo, hi = stem(vf, nominal) / SX, float(nominal), 900.0
    if stem(vf, hi) < target:
        raise ValueError(f"cannot compensate weight {nominal}: stem at 900 is below {target:.1f}")
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if stem(vf, mid) < target else (lo, mid)
    return round((lo + hi) / 2, 1)


def scale_gpos_x(obj, seen: set) -> None:
    if obj is None or id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, list):
        for item in obj:
            scale_gpos_x(item, seen)
    elif isinstance(obj, ot.Anchor):
        obj.XCoordinate = round(obj.XCoordinate * SX)
    elif obj.__class__.__name__ == "ValueRecord":
        for attr in ("XPlacement", "XAdvance"):
            if hasattr(obj, attr):
                setattr(obj, attr, round(getattr(obj, attr) * SX))
    elif hasattr(obj, "__dict__"):
        for value in vars(obj).values():
            if isinstance(value, (list, ot.BaseTable)) or value.__class__.__name__ == "ValueRecord":
                scale_gpos_x(value, seen)


def condensed_latin(italic: bool, wght: float) -> TTFont:
    font = instancer.instantiateVariableFont(TTFont(geist_path(italic)), {"wght": wght})
    gs, glyf, hmtx = font.getGlyphSet(), font["glyf"], font["hmtx"].metrics
    new = {}
    for name in font.getGlyphOrder():
        rec = DecomposingRecordingPen(gs)
        gs[name].draw(rec)
        pen = TTGlyphPen(None)
        rec.replay(TransformPen(pen, (SX, 0, 0, 1, 0, 0)))
        new[name] = pen.glyph()
    for name, g in new.items():
        glyf[name] = g
        g.recalcBounds(glyf)
        hmtx[name] = (round(hmtx[name][0] * SX), g.xMin if g.numberOfContours else 0)
    if "GPOS" in font:
        scale_gpos_x(font["GPOS"].table, set())
    for tag in ("fpgm", "prep", "cvt ", "hdmx", "LTSH", "VDMX", "STAT"):
        if tag in font:
            del font[tag]
    post = font["post"]
    post.italicAngle = math.degrees(math.atan(math.tan(math.radians(post.italicAngle)) * SX))
    return font


# ---------------------------------------------------------------- Hangul, CJK, icons

# Conjoining jamo (U+1100 block) are drawn with the matching compatibility jamo (U+3131 block), as in Jetendard.
CHOSEONG = [0x3131, 0x3132, 0x3134, 0x3137, 0x3138, 0x3139, 0x3141, 0x3142, 0x3143, 0x3145, 0x3146, 0x3147,
            0x3148, 0x3149, 0x314A, 0x314B, 0x314C, 0x314D, 0x314E]
JONGSEONG = [0x3131, 0x3132, 0x3133, 0x3134, 0x3135, 0x3136, 0x3137, 0x3139, 0x313A, 0x313B, 0x313C, 0x313D,
             0x313E, 0x313F, 0x3140, 0x3141, 0x3142, 0x3144, 0x3145, 0x3146, 0x3147, 0x3148, 0x314A, 0x314B,
             0x314C, 0x314D, 0x314E]
JAMO_SOURCE = ({0x1100 + i: c for i, c in enumerate(CHOSEONG)} | {0x1161 + i: 0x314F + i for i in range(21)}
               | {0x11A8 + i: c for i, c in enumerate(JONGSEONG)})


def add_glyph(font: TTFont, name: str, src_gs, src_name: str, matrix: tuple, advance: int) -> None:
    rec = DecomposingRecordingPen(src_gs)
    src_gs[src_name].draw(rec)
    pen = TTGlyphPen(None)
    rec.replay(TransformPen(pen, matrix))
    g = pen.glyph()
    font["glyf"][name] = g  # also appends to the shared glyph order
    g.recalcBounds(font["glyf"])
    font["hmtx"].metrics[name] = (advance, g.xMin if g.numberOfContours else 0)


def map_codepoints(font: TTFont, mapping: dict) -> None:
    """Add codepoint -> glyph entries, creating a format 12 subtable for codepoints above U+FFFF."""
    cmap = font["cmap"]
    if not any(t.format == 12 for t in cmap.tables):
        t = CmapSubtable.newSubtable(12)
        t.platformID, t.platEncID, t.language, t.cmap = 3, 10, 0, dict(font.getBestCmap())
        cmap.tables.append(t)
    for table in cmap.tables:
        if table.isUnicode() and table.format != 14:
            table.cmap.update({cp: g for cp, g in mapping.items() if cp <= 0xFFFF or table.format in (12, 13)})
    font.setGlyphOrder(list(dict.fromkeys(font["glyf"].glyphOrder)))


def add_hangul(font: TTFont, src: TTFont) -> int:
    """Pretendard glyphs scaled like Jetendard, capped to fit the box and the line, centred in two cells."""
    dst_cmap, src_cmap, src_gs = font.getBestCmap(), src.getBestCmap(), src.getGlyphSet()
    requested = font["head"].unitsPerEm / src["head"].unitsPerEm * KOREAN_SCALE
    guard = max(8, round(CELL * 0.02))
    top = min(font["hhea"].ascent, font["OS/2"].sTypoAscender)
    bottom = max(font["hhea"].descent, font["OS/2"].sTypoDescender)
    mapping, copied = {}, {}
    for lo, hi in KOREAN_RANGES:
        for cp in range(lo, hi + 1):
            src_name = src_cmap.get(JAMO_SOURCE.get(cp, cp))
            if cp in dst_cmap or src_name is None:
                continue
            if src_name not in copied:
                bp = BoundsPen(src_gs)
                src_gs[src_name].draw(bp)
                s, dx = requested, 0.0
                if bp.bounds:
                    x0, y0, x1, y1 = bp.bounds
                    if x1 > x0:
                        s = min(s, (2 * CELL - 2 * guard) / (x1 - x0))
                    if y1 > 0:
                        s = min(s, top / y1)
                    if y0 < 0:
                        s = min(s, bottom / y0)
                    dx = CELL - (x0 + x1) / 2 * s
                copied[src_name] = f"kr.{src_name}"
                add_glyph(font, copied[src_name], src_gs, src_name, (s, 0, 0, s, dx, 0), 2 * CELL)
            mapping[cp] = copied[src_name]
    map_codepoints(font, mapping)
    for field in ("ulUnicodeRange1", "ulUnicodeRange2", "ulUnicodeRange3", "ulUnicodeRange4",
                  "ulCodePageRange1", "ulCodePageRange2"):
        setattr(font["OS/2"], field, getattr(font["OS/2"], field) | getattr(src["OS/2"], field))
    return len(mapping)


def add_nerd_icons(font: TTFont, src: TTFont) -> int:
    """Symbols Nerd Font Mono draws each icon in a 1em square; scale it to one cell, centred on the line."""
    dst_cmap, src_cmap, src_gs = font.getBestCmap(), src.getBestCmap(), src.getGlyphSet()
    src_top, src_bottom = src["hhea"].ascent, src["hhea"].descent
    top, bottom = font["hhea"].ascent, font["hhea"].descent
    s = CELL / src["head"].unitsPerEm
    icon = (s, 0, 0, s, 0, (top + bottom) / 2 - (src_top + src_bottom) / 2 * s)
    sy = (top - bottom) / (src_top - src_bottom)
    mapping, copied = {}, {}
    for cp, src_name in sorted(src_cmap.items()):
        if cp in dst_cmap:
            continue
        if src_name not in copied:
            matrix = icon
            if cp in POWERLINE_STRETCH or cp in POWERLINE_SYMBOLS:
                bp = BoundsPen(src_gs)
                src_gs[src_name].draw(bp)
                x0, _, x1, _ = bp.bounds
                if cp in POWERLINE_STRETCH:
                    sx = CELL / (x1 - x0)
                    matrix = (sx, 0, 0, sy, -x0 * sx, bottom - src_bottom * sy)
                else:
                    k = min(sy, CELL / (x1 - x0))
                    matrix = (k, 0, 0, k, CELL / 2 - (x0 + x1) / 2 * k,
                              (top + bottom) / 2 - (src_top + src_bottom) / 2 * k)
            copied[src_name] = f"nf.{src_name}"
            add_glyph(font, copied[src_name], src_gs, src_name, matrix, CELL)
        mapping[cp] = copied[src_name]
    map_codepoints(font, mapping)
    missing = set(src_cmap) - set(font.getBestCmap())
    if missing:
        raise AssertionError(f"{len(missing)} Nerd Font codepoints are not mapped, e.g. U+{min(missing):04X}")
    return len(mapping)


def add_jamo_ccmp(font: TTFont) -> int:
    """Compose L+V and L+V+T conjoining jamo (NFD text, such as macOS file names) into syllables."""
    cmap, ligs = font.getBestCmap(), {}
    for li in range(19):
        for vi in range(21):
            l, v, base = cmap.get(0x1100 + li), cmap.get(0x1161 + vi), 0xAC00 + (li * 21 + vi) * 28
            if not (l and v and base in cmap):
                continue
            ligs[(l, v)] = cmap[base]
            for ti in range(1, 28):
                t = cmap.get(0x11A7 + ti)
                if t and base + ti in cmap:
                    ligs[(l, v, t)] = cmap[base + ti]
    gsub = font["GSUB"].table
    gsub.LookupList.Lookup.append(buildLookup([buildLigatureSubstSubtable(ligs)]))
    gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)
    index = gsub.LookupList.LookupCount - 1
    existing = [r for r in gsub.FeatureList.FeatureRecord if r.FeatureTag == "ccmp"]
    if not existing:
        raise ValueError("expected Geist Mono to have a ccmp feature to extend")
    for r in existing:
        r.Feature.LookupListIndex.append(index)
        r.Feature.LookupCount = len(r.Feature.LookupListIndex)
    return len(ligs)


def terminalize_ligatures(font: TTFont) -> int:
    """Rewrite Geist Mono's n-to-1 ligatures as one glyph per character, the way terminals place them.

    Geist Mono's liga turns e.g. '->' into one glyph with a one-cell advance, drawn leftwards from its origin.
    A terminal draws that glyph at the first cell of the cluster, so it lands on the previous character.
    Here each ligature becomes a chaining rule: the first n-1 characters turn into empty one-cell spacers and
    the last character turns into the ligature glyph, which then reaches back over the spacers.
    """
    gsub = font["GSUB"].table
    liga = [r for r in gsub.FeatureList.FeatureRecord if r.FeatureTag == "liga"]
    old = sorted({i for r in liga for i in r.Feature.LookupListIndex})
    rules = []
    for i in old:
        for st in gsub.LookupList.Lookup[i].SubTable:
            st = getattr(st, "ExtSubTable", st)
            for first, ligs in st.ligatures.items():
                rules += [([first, *lig.Component], lig.LigGlyph) for lig in ligs]
    rules.sort(key=lambda r: -len(r[0]))  # longest sequence wins at each position
    spacer = "liga.spacer"
    font["glyf"][spacer] = TTGlyphPen(None).glyph()
    font["hmtx"].metrics[spacer] = (CELL, 0)
    font.setGlyphOrder(list(dict.fromkeys(font["glyf"].glyphOrder)))
    lookups = gsub.LookupList.Lookup
    components = sorted({g for seq, _ in rules for g in seq[:-1]})
    lookups.append(buildLookup([buildSingleSubstSubtable({g: spacer for g in components})]))
    to_spacer = len(lookups) - 1
    to_lig = {}
    for seq, lig in rules:
        if (seq[-1], lig) not in to_lig:
            lookups.append(buildLookup([buildSingleSubstSubtable({seq[-1]: lig})]))
            to_lig[(seq[-1], lig)] = len(lookups) - 1
    glyph_map = font.getReverseGlyphMap()
    subtables = []
    for seq, lig in rules:
        st = ot.ChainContextSubst()
        st.Format = 3
        st.BacktrackCoverage, st.LookAheadCoverage = [], []
        st.InputCoverage = [buildCoverage([g], glyph_map) for g in seq]
        st.BacktrackGlyphCount, st.InputGlyphCount, st.LookAheadGlyphCount = 0, len(seq), 0
        records = []
        for pos in range(len(seq)):
            rec = ot.SubstLookupRecord()
            rec.SequenceIndex = pos
            rec.LookupListIndex = to_spacer if pos < len(seq) - 1 else to_lig[(seq[-1], lig)]
            records.append(rec)
        st.SubstLookupRecord, st.SubstCount = records, len(records)
        subtables.append(st)
    chain = ot.Lookup()
    chain.LookupType, chain.LookupFlag, chain.SubTable = 6, 0, subtables
    chain.SubTableCount = len(subtables)
    lookups.append(chain)
    gsub.LookupList.LookupCount = len(lookups)
    for r in liga:
        r.Feature.LookupListIndex = [len(lookups) - 1]
        r.Feature.LookupCount = 1
    return len(rules)


def fix_overlap_flags(font: TTFont) -> None:
    """OVERLAP_SIMPLE (0x40) is only valid on a glyph's first flag; OTS (Chrome) rejects it elsewhere."""
    glyf = font["glyf"]
    for name in font.getGlyphOrder():
        g = glyf[name]
        if g.isComposite() or g.numberOfContours <= 0 or not any(f & 0x40 for f in g.flags[1:]):
            continue
        g.flags[0] |= 0x40
        for i in range(1, len(g.flags)):
            g.flags[i] &= ~0x40


# ---------------------------------------------------------------- naming and metadata

def set_names(font: TTFont, weight: str, nominal: int, italic: bool, version: str) -> None:
    ribbi = weight in ("Regular", "Bold")
    style = style_name(weight, italic)
    sub = ("Bold " if weight == "Bold" else "") + ("Italic" if italic else "") or "Regular"
    sub = sub.strip()
    family = FAMILY if ribbi else f"{FAMILY} {weight}"
    typo_sub = (f"{weight} Italic" if italic and weight != "Regular" else "Italic" if italic else weight)
    full = f"{FAMILY} {typo_sub}" if typo_sub != "Regular" else f"{FAMILY} Regular"
    ps = f"{PS_FAMILY}-{style}"
    name = font["name"]
    name.names = [r for r in name.names if r.nameID not in (0, 1, 2, 3, 4, 5, 6, 13, 14, 16, 17, 21, 22, 25)]
    for nid, s in ((0, COPYRIGHT), (1, family), (2, sub), (3, f"{version};{ps}"), (4, full), (5, version),
                   (6, ps), (13, LICENSE), (14, LICENSE_URL)):
        name.setName(s, nid, 3, 1, 0x409)
    if not ribbi:
        name.setName(FAMILY, 16, 3, 1, 0x409)
        name.setName(typo_sub, 17, 3, 1, 0x409)
    os2, head = font["OS/2"], font["head"]
    os2.usWeightClass = nominal
    sel = os2.fsSelection & ~(0x01 | 0x20 | 0x40)
    sel |= (0x01 if italic else 0) | (0x20 if weight == "Bold" else 0)
    sel |= 0x40 if not italic and weight != "Bold" else 0
    os2.fsSelection = sel
    head.macStyle = (1 if weight == "Bold" else 0) | (2 if italic else 0)
    os2.xAvgCharWidth = CELL
    font["post"].isFixedPitch = 1
    os2.panose.bProportion = 9
    font["hhea"].advanceWidthMax = max(a for a, _ in font["hmtx"].metrics.values())


# ---------------------------------------------------------------- verification

SHAPING_SAMPLES = ["mono->1", "a --> b", "x != y", "<!-- x -->", "|> <|", "한글->ok"]


def verify_shaping(path: Path, label: str) -> None:
    """With ligatures on, every character must still map to its own glyph and cell."""
    font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
    for text in SHAPING_SAMPLES:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf, {"liga": True, "calt": True})
        advances = [p.x_advance for p in buf.glyph_positions]
        if len(advances) != len(text) or not set(advances) <= {CELL, 2 * CELL}:
            raise AssertionError(f"{label}: {text!r} shapes to {len(advances)} glyphs, advances {advances}")


def verify(font: TTFont, label: str) -> str:
    cmap, hmtx, gs = font.getBestCmap(), font["hmtx"].metrics, font.getGlyphSet()
    advances = {hmtx[g][0] for g in set(cmap.values())}
    if not advances <= {0, CELL, 2 * CELL}:
        raise AssertionError(f"{label}: unexpected advances {sorted(advances - {0, CELL, 2 * CELL})}")
    widths, centres = [], []
    for c in range(0xAC00, 0xD7A4, 7):
        bp = BoundsPen(gs)
        gs[cmap[c]].draw(bp)
        x0, _, x1, _ = bp.bounds
        widths.append(x1 - x0)
        centres.append((x0 + x1) / 2 - CELL)
    gap, centre = 2 * CELL - statistics.mean(widths), statistics.mean(centres)
    if abs(centre) > 1:
        raise AssertionError(f"{label}: Hangul off centre by {centre:.1f}")
    return f"Hangul gap {gap / 1000:.3f}em"


# ---------------------------------------------------------------- main

def main() -> None:
    fetch_sources()
    DIST.mkdir(exist_ok=True)
    version = "Version 1.0.0"
    nerd = TTFont(SOURCES / NERD_FILE)
    for italic in (False, True):
        vf_upright = TTFont(geist_path(False))  # stems are measured upright; italics reuse the weights
        for weight, nominal in WEIGHTS:
            style = style_name(weight, italic)
            wght = compensated_weight(vf_upright, nominal)
            font = condensed_latin(italic, wght)
            n_hangul = add_hangul(font, TTFont(SOURCES / f"Pretendard-{weight}.ttf"))  # Hangul stays upright
            n_icons = add_nerd_icons(font, nerd)
            n_ligs = add_jamo_ccmp(font)
            n_arrows = terminalize_ligatures(font)
            fix_overlap_flags(font)
            set_names(font, weight, nominal, italic, version)
            out = DIST / f"{PS_FAMILY}-{style}.ttf"
            font.save(out)
            result = verify(TTFont(out), style)
            verify_shaping(out, style)
            print(f"{out.name}: wght {wght}, hangul {n_hangul}, icons {n_icons}, ccmp {n_ligs}, "
                  f"ligatures {n_arrows}, {result}")


if __name__ == "__main__":
    main()
