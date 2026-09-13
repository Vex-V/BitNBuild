"""
One process that serves the map and answers chat.

    python -m uvicorn server.app:app --port 8000 --reload

  /             -> BitNBuild/web/map.html
  /output/*     -> the generated files (the map's offline fallback)
  /api/chat     -> the agent; OpenAI and Tavily keys never leave this process

Run it from the repo root.
"""

import json
import os
import time
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

from .agent import CHAT_MODEL, run
from .tools import Tools

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "BitNBuild" / "web"
OUTPUT = ROOT / "BitNBuild" / "output"
DATA = ROOT / "BitNBuild" / "data"
ENV_FILE = ROOT / ".env"


def load_env(path):
    """Read .env without a python-dotenv dependency. Keys are stripped, so a
    stray space (TAVILY_API_KEY =…) still resolves."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = {**load_env(ENV_FILE), **os.environ}

SUPABASE_URL = ENV.get("SUPABASE_URL", "")
SERVICE_KEY = ENV.get("SUPABASE_SECRET_KEY", "")
OPENAI_KEY = ENV.get("OPENAI_API", "") or ENV.get("OPENAI_API_KEY", "")
TAVILY_KEY = ENV.get("TAVILY_API_KEY", "")

_client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
_tools = (Tools(SUPABASE_URL, SERVICE_KEY, _client, TAVILY_KEY)
          if (_client and SUPABASE_URL and SERVICE_KEY) else None)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _build_svg_for_ward(ward_feature):
    geometry = ward_feature.get("geometry", {})
    if geometry.get("type") == "Polygon":
        rings = [geometry.get("coordinates", [])[0]]
    elif geometry.get("type") == "MultiPolygon":
        rings = [poly[0] for poly in geometry.get("coordinates", [])]
    else:
        rings = []

    if not rings:
        return "<div class=\"map-empty\">Map unavailable for this ward.</div>"

    ring_points = []
    xs = []
    ys = []
    for ring in rings:
        for lon, lat in ring:
            xs.append(lon)
            ys.append(lat)
            ring_points.append((lon, lat))

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if max_x == min_x or max_y == min_y:
        return "<div class=\"map-empty\">Map unavailable for this ward.</div>"

    def scale(pt):
        lon, lat = pt
        x = 10 + ((lon - min_x) / (max_x - min_x)) * 80
        y = 90 - ((lat - min_y) / (max_y - min_y)) * 80
        return f"{x:.2f},{y:.2f}"

    polygons = []
    for ring in rings:
        points = " ".join(scale(pt) for pt in ring)
        polygons.append(f'<polygon points=\"{points}\" fill=\"#2b6cb0\" stroke=\"#0f172a\" stroke-width=\"1.2\" />')

    svg = (
        '<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Ward map">'
        + "".join(polygons) +
        '</svg>'
    )
    return svg


def _collect_ward_news(points_payload, ward_key):
    news_items = []
    seen = set()

    def add_item(item):
        if not item:
            return
        title = (item.get("title") or "Untitled update").strip()
        url = item.get("url") or ""
        source = (item.get("source") or "Local coverage").strip()
        date = (item.get("date") or "").strip()
        key = (title, url, source, date)
        if key in seen:
            return
        seen.add(key)
        news_items.append({
            "title": title,
            "url": url,
            "source": source,
            "date": date,
        })

    for station in points_payload.get("stations", []):
        if station.get("wardKey") == ward_key:
            for item in station.get("news", []) or []:
                add_item(item)

    for project in points_payload.get("projects", []):
        if project.get("wardKey") == ward_key:
            for item in project.get("news", []) or []:
                add_item(item)

    return news_items


def _ward_report_html(ward_feature, points_payload, web_results):
    props = ward_feature.get("properties", {})
    ward_name = props.get("wardName") or props.get("wardKey") or "Ward"
    ward_key = props.get("wardKey", "")

    local_news = _collect_ward_news(points_payload, ward_key)
    stations = [s for s in points_payload.get("stations", []) if s.get("wardKey") == ward_key]
    projects = [p for p in points_payload.get("projects", []) if p.get("wardKey") == ward_key]
    report_rows = props.get("reports") or []
    status_counts = props.get("statusCounts") or {}

    def fmt_number(v):
        if v in (None, ""):
            return "—"
        return str(v)

    def stat_rows(rows):
        html = []
        for label, value in rows:
            if value in (None, "", "—"):
                continue
            html.append(
                f'<div class="stat"><div class="label">{escape(label)}</div><div class="value">{escape(str(value))}</div></div>'
            )
        return "".join(html)

    def news_items(items, title, empty_note):
        if not items:
            return (
                f'<div class="section"><h3>{escape(title)}</h3>'
                f'<p class="empty-note">{escape(empty_note)}</p></div>'
            )
        blocks = []
        for item in items[:8]:
            meta = []
            if item.get("source"):
                meta.append(escape(item["source"]))
            if item.get("date"):
                meta.append(escape(item["date"]))
            meta_text = " · ".join(meta)
            title_text = escape(item.get("title") or "Untitled update")
            if item.get("url"):
                title_html = f'<a href="{escape(item["url"])}" target="_blank" rel="noopener noreferrer">{title_text}</a>'
            else:
                title_html = title_text
            blocks.append(
                f'<li><div class="news-title">{title_html}</div>'
                f'<div class="news-meta">{escape(meta_text)}</div></li>'
            )
        return (
            f'<div class="section"><h3>{escape(title)}</h3><ul class="news-list">'
            + "".join(blocks) +
            '</ul></div>'
        )

    def list_items(items, field_name):
        if not items:
            return '<p class="empty-note">No entries found in the current local dataset.</p>'
        rows = []
        for item in items:
            if field_name == "station":
                status = str(item.get("status", "")).replace("_", " ")
                lines = ", ".join(item.get("lines", []) or [])
                rows.append(
                    f'<li><strong>{escape(item.get("name", "Station"))}</strong><div>{escape(lines or "No line info")} · {escape(status)}</div></li>'
                )
            else:
                status = str(item.get("status", "")).replace("_", " ")
                rows.append(
                    f'<li><strong>{escape(item.get("name", "Project"))}</strong><div>{escape(item.get("type", "").replace("_", " "))} · {escape(status)}</div></li>'
                )
        return '<ul class="inline-list">' + "".join(rows) + '</ul>'

    def report_rows_html(rows):
        if not rows:
            return '<p class="empty-note">No itemised civic reports were captured for this ward in the local dataset.</p>'

        blocks = []
        for report in rows:
            location = escape(report.get("location") or "(no location given)")
            status = escape(report.get("status") or "")
            count = report.get("count")
            age = escape(report.get("age") or "")
            meta = []
            if status:
                meta.append(f'<span class="report-tag">{status}</span>')
            if count is not None:
                meta.append(f'<span>{fmt_number(count)} × reported</span>')
            if age:
                meta.append(f'<span>{age}</span>')
            blocks.append(
                f'<div class="report-item">'
                f'<div class="report-location">{location}</div>'
                f'<div class="report-meta">{"".join(meta)}</div>'
                f'</div>'
            )
        return '<div class="report-list">' + "".join(blocks) + '</div>'

    web_results_html = news_items(web_results, "Latest web search", "No live web results were returned. This report still includes the locally parsed ward data.")

    mla_name = props.get("representative") or props.get("mlaName") or "Information pending"
    mp_name = props.get("mpName") or "Information pending"

    html = f"""
    <!DOCTYPE html>
    <html lang=\"en\">
    <head>
      <meta charset=\"utf-8\" />
      <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
      <title>{escape(ward_name)} Ward Report</title>
      <style>
        :root {{
          color-scheme: light;
          --bg: #f3f5f8;
          --card: #ffffff;
          --ink: #0f172a;
          --muted: #475569;
          --soft: #e2e8f0;
          --accent: #0f4c81;
          --accent-2: #dbeafe;
          --badge: #edf2ff;
          --border: rgba(15, 23, 42, 0.08);
          --shadow: 0 16px 30px rgba(15, 23, 42, 0.08);
        }}
        * {{ box-sizing: border-box; }}
        body {{
          margin: 0; font-family: Arial, Helvetica, sans-serif; background: var(--bg); color: var(--ink);
        }}
        .report {{ max-width: 1200px; margin: 0 auto; padding: 32px 20px 48px; }}
        .header {{
          background: linear-gradient(135deg, var(--accent-2), #f8fafc);
          border: 1px solid var(--border); box-shadow: var(--shadow); padding: 24px; border-radius: 18px;
        }}
        .header h1 {{ margin: 0; font-size: clamp(28px, 3vw, 42px); }}
        .meta {{ margin-top: 8px; color: var(--muted); font-size: 14px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 14px; margin-top: 22px; }}
        .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; }}
        .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }}
        .value {{ margin-top: 6px; font-size: clamp(18px, 2vw, 28px); font-weight: 700; }}
        .layout {{ display: grid; grid-template-columns: minmax(320px, 1.2fr) minmax(260px, 0.8fr); gap: 20px; margin-top: 20px; }}
        .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 18px; box-shadow: var(--shadow); padding: 20px; }}
        .section h3 {{ margin: 0 0 16px; font-size: 18px; }}
        .details {{ display: grid; gap: 10px; }}
        .details-row {{ display: flex; justify-content: space-between; gap: 16px; padding: 8px 0; border-bottom: 1px solid var(--soft); }}
        .details-row:last-child {{ border-bottom: none; }}
        .details-row span:first-child {{ color: var(--muted); }}
        .two-column {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px; margin-top: 20px; }}
        .news-list, .inline-list {{ margin: 0; padding-left: 18px; display: grid; gap: 12px; }}
        .news-title {{ font-size: 15px; line-height: 1.4; }}
        .news-title a {{ color: var(--ink); text-decoration: none; font-weight: 600; }}
        .news-title a:hover {{ text-decoration: underline; }}
        .news-meta {{ margin-top: 4px; font-size: 12px; color: var(--muted); }}
        .inline-list li {{ margin-bottom: 8px; }}
        .inline-list strong {{ font-size: 15px; }}
        .inline-list div {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
        .empty-note {{ margin: 0; color: var(--muted); }}
        .report-list {{ display: grid; gap: 10px; }}
        .report-item {{ border: 1px solid var(--soft); border-radius: 12px; padding: 12px 14px; background: #fafcff; }}
        .report-location {{ font-weight: 700; margin-bottom: 6px; }}
        .report-meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 13px; color: var(--muted); }}
        .report-tag {{ display: inline-block; background: var(--badge); border: 1px solid var(--soft); border-radius: 999px; padding: 3px 8px; font-size: 12px; color: var(--ink); }}
        .contact-card {{ padding: 16px; border: 1px solid var(--soft); border-radius: 14px; background: linear-gradient(180deg, #ffffff, #f8fbff); }}
        .contact-card h4 {{ margin: 0 0 10px; font-size: 16px; }}
        .contact-row {{ margin-top: 8px; line-height: 1.5; color: var(--muted); }}
        .contact-row strong {{ color: var(--ink); }}
      </style>
    </head>
    <body>
      <div class=\"report\">
        <div class=\"header\">
          <h1>{escape(ward_name)}</h1>
          <div class=\"meta\">{escape(props.get('corporation', ''))} · Ward {escape(str(props.get('wardId', '')))} · {escape(props.get('assembly', ''))}</div>
          <div class=\"stats\">
            {stat_rows([
              ('Reports total', props.get('reportTotal')),
              ('Unresolved', props.get('reportUnresolved')),
              ('Population', props.get('population')),
              ('Area (sq km)', props.get('areaSqKm')),
              ('People / sq km', props.get('popDensity')),
              ('Civic pressure', props.get('civicPressureScore')),
            ])}
          </div>
        </div>

        <div class=\"layout\">
          <div class=\"section\">
            <h3>Ward context</h3>
            <div class=\"details\">
              <div class=\"details-row\"><span>Zone</span><strong>{escape(str(props.get('zoneName') or '—'))}</strong></div>
              <div class=\"details-row\"><span>RO division</span><strong>{escape(str(props.get('roDivision') or '—'))}</strong></div>
              <div class=\"details-row\"><span>Property type</span><strong>{escape(str(props.get('propertyType') or '—'))}</strong></div>
              <div class=\"details-row\"><span>Zoning class</span><strong>{escape(str(props.get('zoningClass') or '—'))}</strong></div>
              <div class=\"details-row\"><span>Allowed height</span><strong>{escape(str(props.get('allowedHeightM') or '—'))} m</strong></div>
              <div class=\"details-row\"><span>Campaign readiness</span><strong>{escape(str(props.get('campaignReadiness') or '—'))}</strong></div>
            </div>
          </div>

          <div class=\"section\">
            <h3>Officials & contacts</h3>
            <div class=\"contact-card\">
              <h4>Responsible representatives</h4>
              <div class=\"contact-row\"><strong>MLA / local representative:</strong> {escape(str(mla_name))}</div>
              <div class=\"contact-row\"><strong>MP:</strong> {escape(str(mp_name))}</div>

              <h4 style=\"margin-top: 18px;\">GBA / BBMP contact</h4>
              <div class=\"contact-row\"><strong>Sri. Maheshwar Rao M, IAS</strong></div>
              <div class=\"contact-row\">Chief Commissioner, Administrator to 5 City Corporations</div>
              <div class=\"contact-row\">Email: <a href=\"mailto:comm@bbmp.gov.in\">comm@bbmp.gov.in</a></div>
              <div class=\"contact-row\">Phone: 94480 67345</div>
            </div>
          </div>
        </div>

        <div class=\"two-column\">
          <div class=\"section\">
            <h3>Primary concerns</h3>
            {('<ul class="inline-list">' + ''.join(f'<li>{escape(item)}</li>' for item in (props.get('primaryConcerns') or [])) + '</ul>') if props.get('primaryConcerns') else '<p class="empty-note">No primary concerns listed.</p>'}
          </div>
          <div class=\"section\">
            <h3>Recommended next action</h3>
            <p class=\"empty-note\">{escape(str(props.get('recommendedNextAction') or 'No recommended action captured in the local dataset.'))}</p>
          </div>
        </div>

        <div class=\"two-column\">
          <div class=\"section\">
            <h3>Civic reports</h3>
            {report_rows_html(report_rows)}
            {('<div class="empty-note" style="margin-top: 10px;">Headline totals shown: ' + str(props.get('reportTotal', 0)) + ' total, ' + str(props.get('reportUnresolved', 0)) + ' unresolved.</div>') if props.get('reportTotal') or props.get('reportUnresolved') else ''}
          </div>
          <div class=\"section\">
            <h3>Status breakdown</h3>
            {('<ul class="inline-list">' + ''.join(f'<li><strong>{escape(status)}</strong> · {escape(str(count))}</li>' for status, count in sorted(status_counts.items())) + '</ul>') if status_counts else '<p class="empty-note">No status breakdown was available for this ward.</p>'}
          </div>
        </div>

        <div class=\"two-column\">
          {news_items(local_news, 'Local parsed news', 'No local news was found in the current dataset for this ward.')}
          {web_results_html}
        </div>

        <div class=\"two-column\">
          <div class=\"section\">
            <h3>Metro stations in this ward</h3>
            {list_items(stations, 'station')}
          </div>
          <div class=\"section\">
            <h3>Road projects in this ward</h3>
            {list_items(projects, 'project')}
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return html


app = FastAPI(title="Bengaluru Ward Atlas")


# In-memory, per-instance rate limit. On serverless each instance has its own
# counter and cold starts reset it, so this is a speed bump against accidental
# hammering — NOT access control. Put Vercel Deployment Protection in front of
# a public deploy if you care about who can spend your OpenAI credits.
_HITS: dict[str, list[float]] = {}
RATE_LIMIT = int(ENV.get("CHAT_RATE_LIMIT", "20"))     # requests
RATE_WINDOW = int(ENV.get("CHAT_RATE_WINDOW", "300"))  # seconds


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < RATE_WINDOW]
    hits.append(now)
    _HITS[ip] = hits[-RATE_LIMIT * 2:]
    return len(hits) > RATE_LIMIT


class ChatRequest(BaseModel):
    messages: list[dict]
    mode: str = "map"


@app.get("/api/health")
def health():
    """What the UI checks on load, so it can disable chat with a real reason."""
    return {
        "ok": bool(_tools),
        "chat_model": CHAT_MODEL,
        "supabase": bool(SUPABASE_URL and SERVICE_KEY),
        "openai": bool(OPENAI_KEY),
        "tavily": bool(TAVILY_KEY),
        "missing": [k for k, v in {
            "SUPABASE_URL": SUPABASE_URL, "SUPABASE_SECRET_KEY": SERVICE_KEY,
            "OPENAI_API": OPENAI_KEY, "TAVILY_API_KEY": TAVILY_KEY,
        }.items() if not v],
    }


@app.get("/api/config.js")
def config_js():
    """The browser's Supabase config, built from env vars at request time.

    A generated, gitignored config.js cannot exist on a deployed build, so the
    page asks for it here instead. Only the PUBLISHABLE key is ever sent — it
    is public by design and row level security is what protects the data. The
    service key stays in this process.
    """
    pub = ENV.get("SUPABASE_PUBLISHABLE_KEY", "")
    lines = ["// served by /api/config.js — built from environment variables"]
    if SUPABASE_URL and pub:
        lines.append("window.SUPABASE_CONFIG = "
                     + json.dumps({"url": SUPABASE_URL, "key": pub}) + ";")
    else:
        lines.append("// no Supabase credentials set; the map reads its local files")
    return Response(content="\n".join(lines) + "\n",
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


# A rewrite that does not preserve the path would land here; say so plainly
# rather than 404ing, so the first deploy is easy to diagnose.
@app.get("/api/index")
def api_index():
    return {"ok": True, "note": "API root. Try /api/health."}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    if rate_limited(ip):
        return JSONResponse(status_code=429, content={
            "error": f"Rate limit: {RATE_LIMIT} messages per "
                     f"{RATE_WINDOW // 60} minutes. Try again shortly."})
    if not _tools:
        return JSONResponse(status_code=503, content={
            "error": "Server is missing credentials. Check /api/health."})
    if not req.messages:
        return JSONResponse(status_code=400, content={"error": "no messages"})
    try:
        reply, trace = run(req.messages, req.mode, _tools, _client)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"})
    return {"reply": reply, "tools_used": trace, "mode": req.mode}


@app.get("/api/ward-report")
def ward_report(ward: str):
    if not ward:
        return JSONResponse(status_code=400, content={"error": "Missing ward query parameter."})

    wards_payload = _read_json(OUTPUT / "wards.geojson")
    feature = next(
        (f for f in wards_payload.get("features", []) if f.get("properties", {}).get("wardKey") == ward),
        None,
    )
    if not feature:
        return JSONResponse(status_code=404, content={"error": f"Ward {ward} not found."})

    points_payload = _read_json(OUTPUT / "points.json")
    web_results = []

    if TAVILY_KEY:
        props = feature.get("properties", {})
        query = (
            f"{props.get('wardName', 'Bengaluru ward')} {props.get('corporation', '')} "
            f"latest news civic issues development"
        )
        report_tools = Tools("", "", None, TAVILY_KEY)
        search = report_tools.web_search(query, max_results=5)
        if isinstance(search, dict) and "error" not in search:
            web_results = search.get("results", [])

    html = _ward_report_html(feature, points_payload, web_results)

    filename = f"ward-report-{ward.replace(':', '-')}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/")
def index():
    return RedirectResponse("/web/map.html")


# Mounted last so /api/* wins. /data carries the MLA and MP photographs, which
# the panel references as ../data/mla_photos/… — the same path a plain
# `python -m http.server` inside BitNBuild/ would serve them on.
app.mount("/output", StaticFiles(directory=str(OUTPUT)), name="output")
app.mount("/data", StaticFiles(directory=str(DATA)), name="data")
app.mount("/web", StaticFiles(directory=str(WEB), html=True), name="web")
