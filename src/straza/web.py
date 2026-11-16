"""Status page and JSON API, on the standard library's HTTP server.

Routes
    GET  /                      status page (HTML)
    GET  /api/status            the same data as JSON
    GET  /api/incidents/<id>    one incident with its event log
    POST /incidents/<id>/ack    acknowledge (only if web.allow_ack is true)
    GET  /healthz               "ok" - so something else can monitor the monitor

The page is rendered on the server and refreshes itself; it works with
JavaScript disabled.  Every dynamic value goes through html.escape, because
check names and failure details end up on the page and a failure detail can
contain whatever a broken server chose to send back.
"""
from __future__ import annotations

import json
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict
from urllib.parse import parse_qs, urlparse

from .config import Config
from .incidents import format_duration
from .status import WINDOWS, snapshot
from .store import Store

LABELS = {"up": "Operational", "degraded": "Degraded", "down": "Down", "unknown": "No data"}
GLYPHS = {"up": "✓", "degraded": "!", "down": "✕", "unknown": "?"}
HEADLINES = {
    "up": "All systems operational",
    "degraded": "Some systems are responding slowly",
    "down": "There is an ongoing incident",
    "unknown": "Waiting for the first results",
}


def make_server(config: Config, store: Store,
                clock: Callable[[], float] = time.time) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "straza"

        def log_message(self, fmt, *args):  # keep the terminal for alerts
            pass

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            window = query.get("window", ["24h"])[0]
            if url.path == "/":
                snap = snapshot(config, store, clock(), window)
                self._send(200, render_page(config, snap), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self._json(200, snapshot(config, store, clock(), window))
            elif url.path.startswith("/api/incidents/"):
                incident = self._incident(url.path.rsplit("/", 1)[-1])
                if incident is None:
                    self._json(404, {"error": "no such incident"})
                else:
                    incident["events"] = store.events(incident["id"])
                    self._json(200, incident)
            elif url.path == "/healthz":
                self._send(200, "ok\n", "text/plain; charset=utf-8")
            else:
                self._send(404, "not found\n", "text/plain; charset=utf-8")

        def do_POST(self):
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "incidents" or parts[2] != "ack":
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            if not config.web.allow_ack:
                self._send(403, "acknowledging from the web is disabled; use `straza ack`\n",
                           "text/plain; charset=utf-8")
                return
            incident = self._incident(parts[1])
            if incident is None:
                self._send(404, "no such incident\n", "text/plain; charset=utf-8")
                return
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            who = (form.get("who", [""])[0].strip() or "web")[:60]
            store.ack_incident(incident["id"], clock(), who)
            self.send_response(303)  # back to the page, so a reload does not re-post
            self.send_header("Location", "/")
            self.end_headers()

        def _incident(self, raw_id: str):
            return store.incident(int(raw_id)) if raw_id.isdigit() else None

        def _json(self, code: int, payload: Any):
            self._send(code, json.dumps(payload, indent=2), "application/json")

        def _send(self, code: int, body: str, content_type: str):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((config.web.host, config.web.port), Handler)


# --------------------------------------------------------------------------
# rendering


def render_page(config: Config, snap: Dict[str, Any]) -> str:
    now = snap["generated_at"]
    parts = [_HEAD.replace("{{title}}", escape(config.web.title))]
    parts.append('<header><h1>%s</h1><nav aria-label="Time window">%s</nav></header>' % (
        escape(config.web.title),
        "".join('<a href="/?window=%s"%s>%s</a>' % (
            w, ' aria-current="true"' if w == snap["window"] else "", w) for w in WINDOWS)))

    parts.append('<section class="banner %s"><span class="glyph %s" aria-hidden="true">%s</span>'
                 '<div><strong>%s</strong><small>Updated %s &middot; refreshes every 15 s</small>'
                 '</div></section>' % (
                     snap["overall"], snap["overall"], GLYPHS[snap["overall"]],
                     escape(HEADLINES[snap["overall"]]), _clock(now)))

    if snap["open_incidents"]:
        parts.append('<h2>Open incidents</h2>')
        for inc in snap["open_incidents"]:
            parts.append(_incident_card(config, inc, now))

    parts.append('<h2>Checks <span class="muted">last %s</span></h2>' % snap["window"])
    parts.append('<ul class="checks">')
    for c in snap["checks"]:
        parts.append(_check_row(c, snap))
    parts.append('</ul>')
    parts.append(_LEGEND)

    parts.append('<h2>Incident history <span class="muted">last %s</span></h2>' % snap["window"])
    history = [i for i in snap["recent_incidents"] if i["resolved_at"] is not None]
    if history:
        parts.append('<div class="scroll"><table><thead><tr><th>#</th><th>Check</th><th>Severity</th>'
                     '<th>Opened</th><th>Duration</th><th>Acknowledged</th><th>Cause</th>'
                     '</tr></thead><tbody>')
        for inc in history:
            acked = ("%s after %s" % (escape(inc["acked_by"] or ""),
                                      format_duration(inc["acked_at"] - inc["opened_at"]))
                     if inc["acked_at"] else "&ndash;")
            parts.append('<tr><td>%d</td><td>%s</td><td><span class="sev">%s</span></td><td>%s</td>'
                         '<td class="num">%s</td><td>%s</td><td>%s</td></tr>' % (
                             inc["id"], escape(inc["check_name"]), escape(inc["severity"].upper()),
                             _stamp(inc["opened_at"]),
                             format_duration(inc["resolved_at"] - inc["opened_at"]),
                             acked, escape(inc["cause"] or "")))
        parts.append('</tbody></table></div>')
    else:
        parts.append('<p class="muted">No resolved incidents in this window.</p>')

    parts.append(_FOOT)
    return "".join(parts)


def _check_row(c: Dict[str, Any], snap: Dict[str, Any]) -> str:
    cells = []
    start = snap["generated_at"] - snap["window_s"]
    for i, b in enumerate(c["timeline"]):
        t0 = start + i * snap["bucket_s"]
        span = "%s – %s" % (_stamp(t0), _stamp(t0 + snap["bucket_s"]))
        if b["total"] == 0:
            cls, tip = "none", "no data"
        else:
            ok = b["total"] - b["down"]
            if b["down"] * 2 >= b["total"]:
                cls = "down"
            elif b["down"]:
                cls = "partial"
            elif b["degraded"] * 5 >= b["total"]:
                # one slow answer among many is not worth colouring a whole
                # bucket; a fifth of them is
                cls = "degraded"
            else:
                cls = "up"
            tip = "%d of %d probes ok" % (ok, b["total"])
            if b["down"]:
                tip += ", %d failed" % b["down"]
            if b["degraded"]:
                tip += ", %d slow" % b["degraded"]
        cells.append('<i class="%s" data-tip="%s"></i>' % (cls, escape(span + " · " + tip)))

    uptime = "%.2f%%" % c["uptime_pct"] if c["uptime_pct"] is not None else "–"
    p95 = "%.0f ms" % c["p95_latency_ms"] if c["p95_latency_ms"] is not None else "–"
    return ('<li><div class="state"><span class="glyph %s" aria-hidden="true">%s</span>%s</div>'
            '<div class="who"><strong>%s</strong><small>%s &middot; %s</small></div>'
            '<div class="strip" role="img" aria-label="%s: uptime %s over the last %s">%s</div>'
            '<dl><div><dt>Uptime</dt><dd>%s</dd></div><div><dt>p95</dt><dd>%s</dd></div></dl></li>' % (
                c["status"], GLYPHS[c["status"]], LABELS[c["status"]],
                escape(c["name"]), escape(c["target"]), escape(c["detail"]),
                escape(c["name"]), uptime, snap["window"], "".join(cells), uptime, p95))


def _incident_card(config: Config, inc: Dict[str, Any], now: float) -> str:
    if inc["acked_at"]:
        state = "Acknowledged by %s" % escape(inc["acked_by"] or "")
        action = ""
    else:
        state = "Not acknowledged"
        level = inc["escalation_level"]
        if level >= 0:
            state += " &middot; escalation level %d of %d" % (level + 1, len(config.escalation))
        action = ('<form method="post" action="/incidents/%d/ack">'
                  '<input name="who" placeholder="Your name" aria-label="Your name" required>'
                  '<button>Acknowledge</button></form>' % inc["id"]
                  if config.web.allow_ack else
                  '<code>straza ack %d</code>' % inc["id"])
    return ('<article class="incident"><div><span class="sev">%s</span> <strong>#%d %s</strong>'
            '<p>%s</p><small>Open for %s &middot; %s</small></div>%s</article>' % (
                escape(inc["severity"].upper()), inc["id"], escape(inc["check_name"]),
                escape(inc["cause"] or ""), format_duration(now - inc["opened_at"]), state, action))


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _stamp(ts: float) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts))


_LEGEND = ('<p class="legend" aria-label="Legend">'
           '<span><i class="up"></i>Operational</span><span><i class="degraded"></i>Often slow</span>'
           '<span><i class="partial"></i>Some probes failed</span>'
           '<span><i class="down"></i>Most probes failed</span>'
           '<span><i class="none"></i>No data</span></p>')

_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>{{title}}</title>
<style>
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--line:#e1e0d9;--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;--none:#e1e0d9}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--line:#2c2c2a;--none:#383835}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}
h2{font-size:15px;margin:32px 0 10px}
.muted,small{color:var(--muted);font-weight:400}
small{display:block;font-size:13px}
nav{display:flex;gap:2px;border:1px solid var(--line);border-radius:8px;padding:2px}
nav a{padding:4px 10px;border-radius:6px;color:var(--ink2);text-decoration:none;font-size:13px}
nav a[aria-current]{background:var(--ink);color:var(--page)}
.glyph{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;
font-size:12px;font-weight:700;color:#fff;flex:none}
.glyph.up{background:var(--good)}.glyph.degraded{background:var(--warn);color:#0b0b0b}
.glyph.down{background:var(--crit)}.glyph.unknown{background:var(--muted)}
.banner{display:flex;align-items:center;gap:12px;margin-top:20px;padding:16px;
background:var(--surface);border:1px solid var(--line);border-radius:12px}
.banner .glyph{width:28px;height:28px;font-size:15px}
.banner strong{font-size:17px;display:block}
.checks{list-style:none;margin:0;padding:0;background:var(--surface);
border:1px solid var(--line);border-radius:12px}
.checks li{display:grid;grid-template-columns:120px minmax(0,1fr) 150px;gap:6px 16px;
align-items:center;padding:14px 16px;border-top:1px solid var(--line)}
.checks li:first-child{border-top:0}
.state{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink2)}
.who small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.strip{grid-column:1/3;display:flex;gap:2px;height:28px}
.strip i{flex:1;border-radius:2px;min-width:0}
i.up{background:var(--good)}i.degraded{background:var(--warn)}i.partial{background:var(--serious)}
i.down{background:var(--crit)}i.none{background:var(--none)}
.strip i:hover{outline:2px solid var(--ink);outline-offset:1px}
dl{grid-row:1/3;grid-column:3;display:grid;grid-template-columns:76px 58px;gap:16px;margin:0;
text-align:right}
dt{font-size:12px;color:var(--muted)}
dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--ink2);margin:10px 2px}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{width:10px;height:10px;border-radius:2px}
.incident{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;
background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--crit);
border-radius:12px;padding:14px 16px;margin-bottom:10px}
.incident p{margin:4px 0}
.sev{font-size:11px;font-weight:700;letter-spacing:.04em;border:1px solid var(--ink2);
color:var(--ink2);border-radius:4px;padding:1px 5px}
form{display:flex;gap:8px}
input,button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);
background:var(--page);color:var(--ink)}
button{background:var(--ink);color:var(--page);border-color:var(--ink);cursor:pointer}
code{font:13px ui-monospace,Menlo,monospace;background:var(--page);border:1px solid var(--line);
border-radius:6px;padding:4px 8px}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:10px 14px;border-top:1px solid var(--line);white-space:nowrap}
td:last-child{white-space:normal;min-width:200px}
thead th{border-top:0;font-size:12px;color:var(--muted);font-weight:500}
.num{font-variant-numeric:tabular-nums}
footer{margin-top:32px;font-size:12px;color:var(--muted)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--page);font-size:12px;
padding:5px 8px;border-radius:6px;white-space:nowrap;display:none;z-index:2}
@media (max-width:640px){.checks li{grid-template-columns:1fr}.strip,dl{grid-column:1;grid-row:auto}
dl{text-align:left}.strip{height:22px}}
</style></head><body><main>
"""

_FOOT = """<footer>straza &middot; JSON at <a href="/api/status" style="color:inherit">/api/status</a></footer>
</main><div id="tip" role="tooltip"></div>
<script>
// Tooltip for the timeline cells.  Purely an enhancement: the same numbers
// (uptime, p95) are printed next to each strip.
(function(){var tip=document.getElementById('tip');
document.addEventListener('mousemove',function(e){var t=e.target;
if(!t.dataset||!t.dataset.tip){tip.style.display='none';return}
tip.textContent=t.dataset.tip;tip.style.display='block';
var w=tip.offsetWidth,x=Math.min(Math.max(8,e.clientX-w/2),innerWidth-w-8);
tip.style.left=x+'px';tip.style.top=(e.clientY-34)+'px'})})();
</script></body></html>
"""
