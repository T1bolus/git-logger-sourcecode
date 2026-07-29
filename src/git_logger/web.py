import asyncio
import json
import logging
from typing import Optional

from aiohttp import web

from .state import StateStore
from .tracker import Tracker

LOG = logging.getLogger(__name__)

DASHBOARD_HTML = r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Git Logger</title>
  <style>
    :root{color-scheme:dark;--bg:#0b1020;--card:#151d32;--line:#26324f;--text:#e9eefc;--muted:#8fa0c4;--ok:#43d17c;--bad:#ff657a;--warn:#ffca64;--accent:#7aa2ff}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:24px}
    h1{font-size:24px;margin:0 0 8px}.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
    .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
    .url-grid{display:grid;gap:10px}.url-entry{border:1px solid var(--line);border-radius:9px;padding:12px;display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;background:#0f1729}
    .url-entry.disabled{opacity:.5}.url-name{font-weight:700;min-width:140px}.url-link{color:var(--accent);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px;flex:1;min-width:200px}
    .url-status{font-size:12px;display:flex;gap:12px;flex-wrap:wrap}.url-actions{display:flex;gap:6px;margin-left:auto}
    .badge{display:inline-block;border-radius:999px;padding:3px 9px;font-size:11px;font-weight:700;letter-spacing:.04em}
    .badge.ok{background:#163a2a;color:var(--ok)}.badge.bad{background:#40202a;color:var(--bad)}.badge.warn{background:#463718;color:var(--warn)}
    .badge.muted{background:#253557;color:var(--muted)}
    button,.btn{cursor:pointer;border:1px solid var(--line);border-radius:7px;padding:6px 14px;font-size:12px;font-weight:600;background:#1a2340;color:var(--text);transition:background .15s}
    button:hover,.btn:hover{background:#26355a}button.primary{background:#2a4580;border-color:#3b5790}button.primary:hover{background:#3558a0}
    button.danger{background:#40202a;border-color:#66333d;color:var(--bad)}button.danger:hover{background:#552b36}
    input,select{background:#0a1120;border:1px solid var(--line);border-radius:7px;padding:8px 12px;color:var(--text);font-size:13px;width:100%}
    input:focus{outline:none;border-color:var(--accent)}
    .add-form{display:flex;flex-wrap:wrap;gap:10px;align-items:end}.add-form .field{flex:1;min-width:180px}.add-form label{color:var(--muted);font-size:11px;display:block;margin-bottom:4px}
    .add-form button{height:36px;white-space:nowrap}
    #events{max-height:200px;overflow:auto;margin-top:8px;font-family:ui-monospace,monospace;font-size:11px;line-height:1.6}
    .event{color:var(--muted);border-bottom:1px solid var(--line);padding:4px 0}.event .time{color:var(--text);margin-right:8px}.event.changed{color:var(--ok)}
    .event.error{color:var(--bad)}.event.added{color:var(--accent)}
    .flex-between{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
    .toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--ok);border-radius:9px;padding:12px 20px;font-size:13px;opacity:0;transition:opacity .3s;z-index:99}
    .toast.show{opacity:1}
  </style>
</head>
<body><main>
  <div class="flex-between"><div><h1>Git Logger</h1><div class="sub">Automatische Website-Änderungen verfolgen und per Git speichern</div></div><div><button onclick="checkAll()" style="margin-right:8px">Alle prüfen</button><span id="repoStatus" class="badge mute" style="font-size:12px">…</span></div></div>

  <div class="card"><div class="label">Neue URL hinzufügen</div>
    <div class="add-form">
      <div class="field"><label>URL</label><input id="newUrl" placeholder="https://example.com/page"></div>
      <div class="field" style="min-width:120px"><label>Name (optional)</label><input id="newName" placeholder="Meine Seite"></div>
      <div class="field" style="min-width:100px"><label>Intervall (s)</label><input id="newInterval" type="number" value="3600" min="60"></div>
      <button class="primary" onclick="addUrl()">Hinzufügen</button>
    </div>
  </div>

  <div class="card"><div class="flex-between"><div class="label">Verfolgte URLs</div><span id="urlCount" class="badge muted">0</span></div>
    <div id="urlList" class="url-grid"><div class="empty" style="color:var(--muted);padding:12px 0;text-align:center">Keine URLs konfiguriert.</div></div>
  </div>

  <div class="card"><div class="label">Git Log</div><pre id="gitLog" style="font-family:ui-monospace,monospace;font-size:12px;color:var(--muted);overflow:auto;max-height:200px;margin:0">Lade…</pre></div>

  <div class="card"><div class="label">Ereignisse</div><div id="events"><div class="event" style="color:var(--muted)">Starte…</div></div></div>
</main>
<div id="toast" class="toast"></div>
<script>
const $=id=>document.getElementById(id);let lastSeq=0;

function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.style.borderColor=ok?'var(--ok)':'var(--bad)';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3000);}

function fmtTime(ts){if(!ts)return'—';const d=new Date(ts*1000);return d.toLocaleString();}

function timeAgo(ts){if(!ts)return'—';const s=Math.floor(Date.now()/1000-ts);if(s<60)return'vor '+s+'s';if(s<3600)return'vor '+Math.floor(s/60)+'min';if(s<86400)return'vor '+Math.floor(s/3600)+'h';return'vor '+Math.floor(s/86400)+'d';}

function renderUrls(urls){
  const list=$('urlList');$('urlCount').textContent=urls.length;
  if(!urls.length){list.innerHTML='<div style="color:var(--muted);padding:12px 0;text-align:center">Keine URLs konfiguriert.</div>';return;}
  list.innerHTML=urls.map(u=>{
    const cls=u.enabled?'url-entry':'url-entry disabled';
    const statusCls=u.error?'bad':(u.last_hash?'ok':'');
    const statusText=u.error||(u.last_hash?'OK':'nie geprüft');
    const changed=u.last_changed?timeAgo(u.last_changed):'—';
    const checked=u.last_checked?timeAgo(u.last_checked):'—';
    return `<div class="${cls}" data-id="${u.id}">
      <div class="url-name">${esc(u.name)}</div>
      <div class="url-link" title="${esc(u.url)}">${esc(u.url)}</div>
      <div class="url-status">
        <span class="badge ${statusCls}">${esc(statusText)}</span>
        <span class="badge muted">${u.interval}s</span>
        <span>👁 ${checked}</span>
        <span>✏️ ${changed}</span>
      </div>
      <div class="url-actions">
        <button onclick="toggleUrl(${u.id})">${u.enabled?'Pause':'Aktiv'}</button>
        <button onclick="checkUrl(${u.id})">Prüfen</button>
        <button class="danger" onclick="removeUrl(${u.id})">Löschen</button>
      </div>
    </div>`;
  }).join('');
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

async function addUrl(){
  const url=$('newUrl').value.trim();const name=$('newName').value.trim();const interval=parseInt($('newInterval').value)||3600;
  if(!url){toast('URL eingeben',false);return;}
  try{
    const r=await fetch('/api/urls',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,name,interval})});
    if(!r.ok){const e=await r.json();toast(e.detail||'Fehler',false);return;}
    $('newUrl').value='';$('newName').value='';toast('URL hinzugefügt');
  }catch(e){toast('Fehler: '+e.message,false);}
}

async function removeUrl(id){
  if(!confirm('URL wirklich löschen?'))return;
  try{
    const r=await fetch('/api/urls/'+id,{method:'DELETE'});
    if(!r.ok){toast('Fehler beim Löschen',false);return;}
    toast('URL gelöscht');
  }catch(e){toast('Fehler: '+e.message,false);}
}

async function toggleUrl(id){
  try{
    const r=await fetch('/api/urls/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!r.ok){toast('Fehler',false);return;}
  }catch(e){toast('Fehler: '+e.message,false);}
}

async function checkUrl(id){
  try{
    await fetch('/api/urls/'+id+'/check',{method:'POST'});
    toast('Prüfung gestartet');
  }catch(e){toast('Fehler: '+e.message,false);}
}

async function checkAll(){
  try{
    await fetch('/api/check-all',{method:'POST'});
    toast('Alle werden geprüft');
  }catch(e){toast('Fehler: '+e.message,false);}
}

async function loadGitLog(){
  try{
    const r=await fetch('/api/git-log');const data=await r.text();
    $('gitLog').textContent=data||'(Keine Commits)';
  }catch(e){$('gitLog').textContent='Fehler beim Laden des Git-Logs';}
}

function renderSnapshot(s){
  renderUrls(s.urls||[]);
  loadGitLog();
  lastSeq=s.sequence;
}

function addEvent(e){
  const box=$('events');const div=document.createElement('div');div.className='event';
  const time=new Date((e.time||0)*1000).toLocaleTimeString();
  let text='';
  if(e.type==='url_checked')text=(e.changed?'✅ Geändert':'➖ Keine Änderung')+' — '+esc(e.name)+(e.error?' ⚠️ '+esc(e.error):'');
  else if(e.type==='url_added')text='➕ Hinzugefügt — '+esc(e.name||e.url);
  else if(e.type==='url_removed')text='🗑 Entfernt — ID '+e.url_id;
  else text=e.type+' '+JSON.stringify(e);
  div.innerHTML=`<span class="time">${time}</span> ${text}`;
  box.prepend(div);
  while(box.children.length>100)box.lastChild.remove();
}

fetch('/api/snapshot').then(r=>r.json()).then(s=>{
  renderSnapshot(s);s.events.slice(-30).forEach(addEvent);
});

const es=new EventSource('/events');
es.onmessage=e=>{
  const p=JSON.parse(e.data);
  if(p.snapshot)renderSnapshot(p.snapshot);
  if(p.event){lastSeq=p.event.sequence;addEvent(p.event);}
};
</script></body></html>"""


class WebServer:
    def __init__(self, state: StateStore, tracker: Tracker, host: str, port: int) -> None:
        self.state = state
        self.tracker = tracker
        self.host = host
        self.port = port
        self.app = web.Application()
        self.app.router.add_get("/", self.dashboard)
        self.app.router.add_get("/api/snapshot", self.snapshot)
        self.app.router.add_get("/events", self.events)
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/api/urls", self.list_urls)
        self.app.router.add_post("/api/urls", self.add_url)
        self.app.router.add_delete("/api/urls/{id}", self.remove_url)
        self.app.router.add_put("/api/urls/{id}", self.update_url)
        self.app.router.add_post("/api/urls/{id}/check", self.check_url)
        self.app.router.add_post("/api/check-all", self.check_all)
        self.app.router.add_get("/api/git-log", self.git_log)
        self.runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        LOG.info("Dashboard listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def dashboard(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def snapshot(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot())

    async def health(self, request: web.Request) -> web.Response:
        urls = self.state.urls
        enabled = sum(1 for u in urls if u["enabled"])
        errors = sum(1 for u in urls if u.get("error"))
        healthy = errors == 0
        return web.json_response(
            {"ok": healthy, "urls_total": len(urls), "urls_enabled": enabled, "urls_error": errors},
            status=200 if healthy else 503,
        )

    async def list_urls(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.urls)

    async def add_url(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"detail": "Invalid JSON"}, status=400)
        url = (body.get("url") or "").strip()
        if not url:
            return web.json_response({"detail": "URL is required"}, status=400)
        name = (body.get("name") or "").strip()
        interval = int(body.get("interval", 0))
        if interval <= 0:
            interval = self.tracker.default_interval
        existing = [u for u in self.state.urls if u["url"] == url]
        if existing:
            return web.json_response({"detail": "URL already tracked"}, status=409)
        entry = self.state.add_url(url, name, interval)
        asyncio.create_task(self.tracker.trigger_add(entry))
        return web.json_response(entry, status=201)

    async def remove_url(self, request: web.Request) -> web.Response:
        url_id = int(request.match_info["id"])
        if self.state.remove_url(url_id):
            return web.json_response({"ok": True})
        return web.json_response({"detail": "Not found"}, status=404)

    async def update_url(self, request: web.Request) -> web.Response:
        url_id = int(request.match_info["id"])
        try:
            body = await request.json()
        except Exception:
            body = {}
        entry = self.state.get_url(url_id)
        if not entry:
            return web.json_response({"detail": "Not found"}, status=404)
        updates = {}
        if "enabled" in body:
            updates["enabled"] = bool(body["enabled"])
        else:
            updates["enabled"] = not entry["enabled"]
        self.state.update_url(url_id, **updates)
        return web.json_response({"ok": True, "enabled": updates.get("enabled", entry["enabled"])})

    async def check_url(self, request: web.Request) -> web.Response:
        url_id = int(request.match_info["id"])
        entry = self.state.get_url(url_id)
        if not entry:
            return web.json_response({"detail": "Not found"}, status=404)
        asyncio.create_task(self.tracker.check_url_now(url_id))
        return web.json_response({"ok": True})

    async def check_all(self, request: web.Request) -> web.Response:
        asyncio.create_task(self.tracker.check_all_now())
        return web.json_response({"ok": True})

    async def git_log(self, request: web.Request) -> web.Response:
        log = self.tracker.repo.get_log(30)
        return web.Response(text=log or "(Keine Commits)", content_type="text/plain")

    async def events(self, request: web.Request) -> web.StreamResponse:
        try:
            last_sequence = int(request.headers.get("Last-Event-ID") or request.query.get("last", "0"))
        except ValueError:
            last_sequence = 0
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        heartbeat = 0
        try:
            while True:
                new_events = self.state.events_since(last_sequence)
                for event in new_events:
                    last_sequence = event["sequence"]
                    data = json.dumps({"event": event}, ensure_ascii=False, separators=(",", ":"))
                    await response.write("id: {}\ndata: {}\n\n".format(last_sequence, data).encode("utf-8"))
                heartbeat += 1
                if heartbeat >= 2:
                    heartbeat = 0
                    snapshot = json.dumps({"snapshot": self.state.snapshot()}, ensure_ascii=False, separators=(",", ":"))
                    await response.write("data: {}\n\n".format(snapshot).encode("utf-8"))
                await asyncio.sleep(1.0)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        return response
