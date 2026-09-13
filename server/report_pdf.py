"""
The downloadable ward report, as a PDF.

It reads the same wards.geojson and points.json the map does, so a report shows
what the ward panel shows, plus any live web results the caller passes in.
DejaVu Sans is embedded because the core PDF fonts are Latin-1 only and the
data carries curly quotes, dashes and the rupee sign.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fpdf import FPDF
from fpdf.enums import XPos, YPos

FONTS = Path(__file__).resolve().parent / "fonts"
IST = timezone(timedelta(hours=5, minutes=30))

INK = (15, 23, 42)
MUTED = (71, 85, 105)
ACCENT = (15, 76, 129)
RULE = (203, 213, 225)
TINT = (239, 246, 255)

# The same administrator covers every ward, so this is fixed text, not data.
GBA_CONTACT = [
    ("Chief Commissioner", "Sri. Maheshwar Rao M, IAS"),
    ("Role", "Administrator to 5 City Corporations"),
    ("Email", "comm@bbmp.gov.in"),
    ("Phone", "94480 67345"),
]

NEWS_LIMIT = 8


def _num(v, digits=0):
    if v in (None, ""):
        return None
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _words(v):
    """snake_case codes from the data read better as words."""
    return str(v).replace("_", " ").strip() if v not in (None, "") else None


def _safe_link(url):
    return url if isinstance(url, str) and urlparse(url).scheme in ("http", "https") else ""


def _join(*parts):
    return " · ".join(str(p) for p in parts if p not in (None, ""))


def _renderable(*texts):
    """DejaVu has no Kannada glyphs, and Kannada also needs text shaping that
    fpdf2 only does with HarfBuzz installed, so a Kannada headline would print
    as blanks. Those items are left out rather than shown broken."""
    return not any("ಀ" <= ch <= "೿" for t in texts for ch in (t or ""))


def _note_skipped(pdf, n):
    if n:
        pdf.note(f"{n} Kannada-language item{'s' if n != 1 else ''} not shown — "
                 "the PDF font cannot display Kannada script.")


class _ReportPDF(FPDF):
    def __init__(self, ward_label, generated):
        super().__init__(format="A4")
        self.ward_label = ward_label
        self.generated = generated
        self.set_margins(15, 15, 15)
        self.set_auto_page_break(True, margin=18)
        self.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
        self.set_title(f"Ward report: {ward_label}")
        self.set_creator("Bengaluru Ward Atlas")

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.ward_label} · Ward report · Bengaluru Ward Atlas · generated {self.generated}")
        self.set_x(self.l_margin)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # -- building blocks ---------------------------------------------------
    def section(self, title):
        # never strand a heading at the foot of a page
        if self.get_y() > self.h - self.b_margin - 30:
            self.add_page()
        self.ln(3)
        y = self.get_y()
        self.set_fill_color(*ACCENT)
        self.rect(self.l_margin, y + 1, 1.2, 5.5, style="F")
        self.set_x(self.l_margin + 4)
        self.set_font("DejaVu", "B", 12)
        self.set_text_color(*INK)
        self.cell(0, 7.5, title, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.5)

    def subheading(self, title):
        self.ln(1)
        self.set_font("DejaVu", "B", 9.5)
        self.set_text_color(*MUTED)
        self.cell(0, 6, title.upper(), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def note(self, text):
        self.set_font("DejaVu", "", 9)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 5, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def paragraph(self, text):
        self.set_font("DejaVu", "", 9.5)
        self.set_text_color(*INK)
        self.multi_cell(0, 5.5, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def pairs(self, rows, empty="Nothing captured for this ward."):
        rows = [(label, value) for label, value in rows if value not in (None, "")]
        if not rows:
            self.note(empty)
            return
        label_w = 52
        for label, value in rows:
            if self.get_y() > self.h - self.b_margin - 8:
                self.add_page()
            y = self.get_y()
            self.set_font("DejaVu", "", 9.5)
            self.set_text_color(*MUTED)
            self.cell(label_w, 6.5, label)
            self.set_font("DejaVu", "B", 9.5)
            self.set_text_color(*INK)
            self.multi_cell(0, 6.5, str(value), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, max(self.get_y(), y + 6.5), self.l_margin + self.epw,
                      max(self.get_y(), y + 6.5))
        self.ln(1.5)

    def bullets(self, lines):
        self.set_font("DejaVu", "", 9.5)
        self.set_text_color(*INK)
        for line in lines:
            self.set_x(self.l_margin + 1.5)
            self.cell(4.5, 5.5, "•")
            self.multi_cell(0, 5.5, str(line), align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def item(self, title, meta=None, detail=None, link=""):
        if self.get_y() > self.h - self.b_margin - 14:
            self.add_page()
        self.set_font("DejaVu", "B", 9.5)
        self.set_text_color(*(ACCENT if link else INK))
        self.multi_cell(0, 5.2, title, link=link, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if meta:
            self.set_font("DejaVu", "", 8)
            self.set_text_color(*MUTED)
            self.multi_cell(0, 4.4, meta, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if detail:
            self.set_font("DejaVu", "", 8.5)
            self.set_text_color(*INK)
            self.multi_cell(0, 4.6, detail, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2.2)

    def tiles(self, items):
        items = [(label, value) for label, value in items if value is not None]
        if not items:
            return
        gap, height = 3, 18
        w = (self.epw - gap * (len(items) - 1)) / len(items)
        y = self.get_y()
        self.set_draw_color(*RULE)
        self.set_line_width(0.3)
        for i, (label, value) in enumerate(items):
            x = self.l_margin + i * (w + gap)
            self.rect(x, y, w, height)
            self.set_xy(x + 2.5, y + 2.5)
            self.set_font("DejaVu", "", 6.5)
            self.set_text_color(*MUTED)
            self.cell(w - 5, 4, label.upper())
            self.set_xy(x + 2.5, y + 8)
            self.set_font("DejaVu", "B", 13)
            self.set_text_color(*INK)
            self.cell(w - 5, 7, value)
        self.set_y(y + height + 4)

    def outline(self, geometry, x, y, size):
        """The ward's shape, scaled to fit a size-by-size box."""
        geometry = geometry or {}
        if geometry.get("type") == "Polygon":
            rings = geometry.get("coordinates", [])[:1]
        elif geometry.get("type") == "MultiPolygon":
            rings = [poly[0] for poly in geometry.get("coordinates", []) if poly]
        else:
            return
        pts = [p for ring in rings for p in ring]
        if not pts:
            return
        min_x, max_x = min(p[0] for p in pts), max(p[0] for p in pts)
        min_y, max_y = min(p[1] for p in pts), max(p[1] for p in pts)
        span = max(max_x - min_x, max_y - min_y)
        if not span:
            return
        # at 13°N a degree of longitude is 97% of a degree of latitude; close enough
        k = size / span
        ox = x + (size - (max_x - min_x) * k) / 2
        oy = y + (size - (max_y - min_y) * k) / 2
        self.set_fill_color(43, 108, 176)
        self.set_draw_color(*INK)
        self.set_line_width(0.3)
        for ring in rings:
            self.polygon([(ox + (p[0] - min_x) * k, oy + (max_y - p[1]) * k) for p in ring],
                         style="DF")


def _ward_news(stations, projects):
    items, seen = [], set()
    for thing in stations + projects:
        for n in thing.get("news") or []:
            key = (n.get("title"), n.get("url"))
            if key in seen:
                continue
            seen.add(key)
            items.append(n)
    return items


def ward_report_pdf(ward_feature, points_payload, web_results):
    """Return the report for one wards.geojson feature as PDF bytes."""
    p = ward_feature.get("properties", {}) or {}
    key = p.get("wardKey", "")
    name = p.get("wardName") or key or "Ward"
    generated = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")

    stations = [s for s in points_payload.get("stations", []) if s.get("wardKey") == key]
    projects = [x for x in points_payload.get("projects", []) if x.get("wardKey") == key]

    pdf = _ReportPDF(name, generated)
    pdf.add_page()

    # --- header band
    top, band, shape = pdf.get_y(), 44, 34
    pdf.set_fill_color(*TINT)
    pdf.rect(pdf.l_margin, top, pdf.epw, band, style="F")
    text_w = pdf.epw - shape - 18
    pdf.set_xy(pdf.l_margin + 6, top + 6)
    pdf.set_font("DejaVu", "", 8)
    pdf.set_text_color(*ACCENT)
    pdf.cell(text_w, 5, "WARD REPORT", new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "B", 22)
    pdf.set_text_color(*INK)
    pdf.multi_cell(text_w, 10, name, new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", 9.5)
    pdf.set_text_color(*MUTED)
    corp = f"{p['corporation']} corporation" if p.get("corporation") else None
    ward_no = f"Ward {p['wardId']}" if p.get("wardId") is not None else None
    pdf.multi_cell(text_w, 5.5, _join(corp, ward_no, p.get("assembly")),
                   new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.multi_cell(text_w, 5.5, f"Generated {generated}", new_x=XPos.LEFT, new_y=YPos.NEXT)
    pdf.outline(ward_feature.get("geometry"), pdf.l_margin + pdf.epw - shape - 5, top + 5, shape)
    pdf.set_y(top + band + 6)

    pdf.tiles([
        ("Reports", _num(p.get("reportTotal"))),
        ("Unresolved", _num(p.get("reportUnresolved"))),
        ("Population", _num(p.get("population"))),
        ("Area km²", _num(p.get("areaSqKm"), 2)),
        ("People / km²", _num(p.get("popDensity"))),
        ("Civic pressure", _num(p.get("civicPressureScore"))),
    ])

    # --- priorities
    pdf.section("Priorities")
    if p.get("primaryConcerns"):
        pdf.subheading("Primary concerns")
        pdf.bullets(p["primaryConcerns"])
    pdf.subheading("Recommended next action")
    pdf.paragraph(p.get("recommendedNextAction") or "No recommended action captured for this ward.")

    # --- civic reports
    pdf.section("Civic reports")
    if not p.get("hasReports"):
        pdf.note("This ward has no entry in the civic report dataset. "
                 "That is different from a ward with zero reports.")
    else:
        pdf.pairs([
            ("Total reports", _num(p.get("reportTotal"))),
            ("Unresolved", _num(p.get("reportUnresolved"))),
            ("Reports per km²", _num(p.get("reportDensity"), 1)),
            ("Reports per 1,000 people", _num(p.get("reportsPer1000"), 2)),
        ])
        status_counts = p.get("statusCounts") or {}
        if status_counts:
            pdf.subheading("Status breakdown")
            pdf.pairs([(_words(s), _num(c)) for s, c in sorted(status_counts.items())])
        reports = p.get("reports") or []
        if reports:
            pdf.subheading(f"Individual reports ({len(reports)})")
            for r in reports:
                count = f"{r['count']}× reported" if r.get("count") is not None else None
                location = r.get("location") or "(no location given)"
                if not _renderable(location):
                    location = "(location given in Kannada)"
                pdf.item(location,
                         _join(r.get("status"), count, r.get("age")))
        elif p.get("reportTotal"):
            pdf.note(f"Counts only — the source listed {_num(p.get('reportTotal'))} reports "
                     "for this ward but did not itemise them.")

    # --- people & area
    pdf.section("People & area")
    male, female = _num(p.get("popMale")), _num(p.get("popFemale"))
    sc, st = _num(p.get("popSC")), _num(p.get("popST"))
    pdf.pairs([
        ("Population", _num(p.get("population"))),
        ("Male / Female", f"{male} / {female}" if male and female else None),
        ("SC / ST", f"{sc} / {st}" if sc and st else None),
        ("Area", f"{_num(p.get('areaSqKm'), 2)} km²" if p.get("areaSqKm") else None),
        ("Density", f"{_num(p.get('popDensity'))} people / km²" if p.get("popDensity") else None),
        ("Reservation", p.get("reservation")),
    ])

    # --- ward context
    pdf.section("Zoning & administration")
    height = lambda v: f"{_num(v)} m" if v not in (None, "") else None  # noqa: E731
    pdf.pairs([
        ("Zone", p.get("zoneName")),
        ("RO division", p.get("roDivision")),
        ("ARO sub-division", p.get("aroSubDivision")),
        ("Property type", _words(p.get("propertyType"))),
        ("Zoning class", p.get("zoningClass")),
        ("Allowed height", height(p.get("allowedHeightM"))),
        ("Current average height", height(p.get("currentAverageHeightM"))),
        ("Height band", _words(p.get("heightBand"))),
        ("HAL clearance", p.get("halClearanceStatus")),
        ("Campaign readiness", _words(p.get("campaignReadiness"))),
    ])

    # --- representation
    pdf.section("Representatives & contacts")
    pdf.pairs([
        ("MLA", p.get("representative") or p.get("mlaName") or "Not available"),
        ("MP", p.get("mpName") or "Not available"),
        ("Assembly constituency", p.get("assembly")),
    ])
    pdf.subheading("GBA / BBMP contact")
    pdf.pairs(GBA_CONTACT)

    # --- infrastructure
    pdf.section(f"Metro stations in this ward ({len(stations)})")
    if not stations:
        pdf.note("No metro stations in this ward.")
    for s in stations:
        pdf.item(s.get("name") or "Station",
                 _join(", ".join(s.get("lines") or []), _words(s.get("status")), s.get("date")),
                 s.get("note"))

    pdf.section(f"Road & infrastructure projects ({len(projects)})")
    if not projects:
        pdf.note("No tracked projects in this ward.")
    for x in projects:
        length = f"{_num(x.get('lengthKm'), 1)} km" if x.get("lengthKm") else None
        cost = f"₹{_num(x.get('costCrore'))} crore" if x.get("costCrore") else None
        desc = x.get("description") or ""
        # expectedCompletion is free text ("Started Nov 2020; incomplete as of
        # 2026…"), not a date, so it is shown as written
        pdf.item(x.get("name") or "Project",
                 _join(_words(x.get("type")), _words(x.get("status")), x.get("authority"),
                       length, cost, x.get("expectedCompletion")),
                 desc[:400] + ("…" if len(desc) > 400 else ""))

    # --- news
    pdf.section("News")
    all_local = _ward_news(stations, projects)
    local = [n for n in all_local if _renderable(n.get("title"))]
    pdf.subheading("From the local dataset")
    if not all_local:
        pdf.note("No news in the local dataset is tied to this ward's stations or projects.")
    for n in local[:NEWS_LIMIT]:
        url = _safe_link(n.get("url"))
        pdf.item(n.get("title") or "Untitled", _join(n.get("source"), n.get("date")), link=url)
    _note_skipped(pdf, len(all_local) - len(local))

    pdf.subheading("Latest web search")
    all_web = web_results or []
    web = [n for n in all_web if _renderable(n.get("title"), n.get("content"))]
    if web_results is None:
        pdf.note("Live web search is not configured on this server.")
    elif not all_web:
        pdf.note("The web search returned no results.")
    for n in web[:NEWS_LIMIT]:
        url = _safe_link(n.get("url"))
        snippet = (n.get("content") or "").strip()
        pdf.item(n.get("title") or url or "Untitled",
                 _join(n.get("source") or urlparse(url).netloc, n.get("date")),
                 snippet[:260] + ("…" if len(snippet) > 260 else ""),
                 link=url)
    _note_skipped(pdf, len(all_web) - len(web))

    return bytes(pdf.output())
