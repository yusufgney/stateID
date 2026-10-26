"""OnlyOffice: its editors run in an embedded Chromium; the installer opens a debug port on 127.0.0.1."""
import asyncio
import json
import os
import re
import time
import urllib.request

from ..procs import desktop_command, spawn
from .base import Adapter

PORT = int(os.environ.get("STATEID_ONLYOFFICE_PORT", 9333))
DESKTOP = "onlyoffice-desktopeditors_onlyoffice-desktopeditors.desktop"


def targets():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
            return [t for t in json.load(r) if t.get("type") == "page" and "api/documents" in t["url"]]
    except Exception:
        return None


def evaluate(ws_url, expr, timeout=30):
    import websockets

    async def run():
        async with websockets.connect(ws_url, max_size=50_000_000, open_timeout=5) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": expr, "returnByValue": True, "awaitPromise": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == 1:
                    r = m.get("result", {})
                    if "exceptionDetails" in r:
                        raise RuntimeError(r["exceptionDetails"].get("text"))
                    return r.get("result", {}).get("value")
    return asyncio.run(asyncio.wait_for(run(), timeout))


PRELUDE = r"""
const f = [...document.querySelectorAll('iframe')].find(i => /editor\/main\//.test(i.src));
const w = f && f.contentWindow, a = w && (w.editor || (w.Asc && w.Asc.editor));
const app = w && (w.PE || w.DE || w.SSE || w.PDFE || w.VE);
const ctl = n => { try { return app.getController(n); } catch (e) { return null; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
"""

CAPTURE = "(() => {" + PRELUDE + r"""
if (!a) return null;
const s = {path: w.AscDesktopEditor ? w.AscDesktopEditor.LocalFileGetSourcePath() : null,
           saved: w.AscDesktopEditor ? w.AscDesktopEditor.LocalFileGetSaved() : null};
try { s.page = a.getCurrentPage(); s.pages = a.getCountPages(); } catch (e) {}
try { s.sheet = a.asc_getActiveWorksheetIndex(); } catch (e) {}
try { const tb = ctl('Toolbar').toolbar;
      s.tab = [...new Set([...w.document.querySelectorAll('[data-tab]')].map(e => e.getAttribute('data-tab')))]
                .find(t => tb.isTabActive(t)) || null; } catch (e) {}
try {
  const v = ctl('Common.Controllers.Draw').view, i = v.btnsPen.findIndex(b => b.pressed);
  if (i >= 0) { const o = v.btnsPen[i].options.penOptions;
    s.pen = {index: i, color: o.color, sizeIdx: o.size.idx, size: o.size.arr[o.size.idx], opacity: o.opacity}; }
  if (v.btnEraser && v.btnEraser.pressed) s.eraser = true;
} catch (e) {}
try {
  const dp = ctl('Viewport').getView('DocumentPreview');
  if (dp && dp.isVisible()) s.slideshow = {slide: dp.pages.get('current'), tool: dp.currentDrawTool || null,
                                           color: dp.currentDrawColor};
} catch (e) {}
return s; })()"""

APPLY = "(async (s) => {" + PRELUDE + r"""
for (let i = 0; i < 100 && !(a && a.getCountPages && a.getCountPages() > 0); i++) await sleep(200);
if (!a) return 'editor not ready';
const done = [];
const dp = ctl('Viewport') && ctl('Viewport').getView('DocumentPreview');
if (dp && dp.isVisible() && !s.slideshow) { a.EndDemonstration && a.EndDemonstration(); await sleep(500); }
if (s.sheet != null && a.asc_showWorksheet) { a.asc_showWorksheet(s.sheet); done.push('sheet ' + (s.sheet + 1)); }
if (s.page != null && a.goToPage) { a.goToPage(s.page); done.push('page ' + (s.page + 1)); }
if (s.tab) { try { ctl('Toolbar').toolbar.setTab(s.tab); done.push('tab ' + s.tab); } catch (e) {} }
const draw = ctl('Common.Controllers.Draw');
if (s.pen && draw && draw.view) {
  const b = draw.view.btnsPen[s.pen.index], o = b.options.penOptions;
  o.color = s.pen.color; if (s.pen.sizeIdx != null) o.size.idx = s.pen.sizeIdx;
  draw.view.updateButtonHint && draw.view.updateButtonHint(b);
  b.toggle(true, true); draw.view.depressButtons(b); draw.onDrawPen(b);
  done.push('pen ' + (s.pen.index + 1) + ' color ' + JSON.stringify(s.pen.color));
} else if (s.eraser && draw && draw.view) {
  draw.view.btnEraser.toggle(true, true); draw.onEraser(draw.view.btnEraser); done.push('eraser');
} else if (draw && draw.view && (draw.view.btnsPen.some(b => b.pressed) || (draw.view.btnEraser && draw.view.btnEraser.pressed))) {
  a.asc_StopInkDrawer(); draw.view.depressButtons(); done.push('drawing tool off');
}
if (s.slideshow) {
  if (!(dp && dp.isVisible())) { w.Common.NotificationCenter.trigger('preview:start', s.slideshow.slide, false, false, false); }
  for (let i = 0; i < 50 && !(dp && dp.isVisible()); i++) await sleep(100);
  await sleep(800);
  if (a.DemonstrationGoToSlide) a.DemonstrationGoToSlide(s.slideshow.slide);
  done.push('slideshow at slide ' + (s.slideshow.slide + 1));
  if (s.slideshow.color) dp.currentDrawColor = s.slideshow.color;
  if (s.slideshow.tool) {
    const it = dp.btnDraw.menu.items.find(i => i.value === s.slideshow.tool);
    if (it && dp.currentDrawTool !== it.value) { it.setChecked(true); dp.onDrawMenuItemClick(null, it); }
    done.push(s.slideshow.tool + ' #' + s.slideshow.color);
  }
}
return done.join(', '); })"""


def open_documents():
    """{path: (target, state)} for every document open in OnlyOffice, or None if the port is closed."""
    tgts = targets()
    if tgts is None:
        return None
    out = {}
    for t in tgts:
        try:
            s = evaluate(t["webSocketDebuggerUrl"], CAPTURE, timeout=5)
        except Exception:
            continue
        if s and s.get("path"):
            out[s["path"]] = (t, s)
    return out


def _open(paths):
    return spawn(desktop_command(DESKTOP, paths) or ["onlyoffice-desktopeditors"] + paths)


class OnlyOffice(Adapter):
    pattern = re.compile(r"onlyoffice|desktopeditors", re.I)
    single_window = True

    def capture(self, w, st):
        docs = open_documents()
        active = re.sub(r"\s*[—-]\s*ONLYOFFICE.*$", "", w["title"] or "").strip()
        if docs is None:
            st["app_state"] = {"warning": f"OnlyOffice debug port {PORT} is closed; start OnlyOffice from the "
                                          "app menu (see install.sh) so slides and pens can be read"}
            return
        state = [dict(s, active=os.path.basename(p) == active) for p, (_, s) in docs.items()]
        state.sort(key=lambda s: not s["active"])
        st["app_state"] = {"documents": state}
        st["files"] = list(dict.fromkeys([s["path"] for s in state] + st["files"]))

    def launch(self, st):
        docs = [d["path"] for d in (st["app_state"] or {}).get("documents", []) if os.path.exists(d["path"])]
        return [_open(docs[::-1])]

    def apply(self, st, win, shell):
        docs_state = (st["app_state"] or {}).get("documents", [])
        if not docs_state:
            return []
        done = []
        deadline = time.time() + 60
        pending = {d["path"]: d for d in docs_state if os.path.exists(d["path"])}
        missing = [p for p in pending if p not in (open_documents() or {})]
        if missing:
            done.append("opened " + _open(missing))
        while pending and time.time() < deadline:
            docs = open_documents()
            if docs is None:
                time.sleep(1)
                continue
            for path in [p for p in pending if p in docs]:
                d = pending.pop(path)
                try:
                    r = evaluate(docs[path][0]["webSocketDebuggerUrl"], f"{APPLY}({json.dumps(d)})")
                    done.append(f"{os.path.basename(path)}: {r}")
                except Exception as e:
                    done.append(f"{os.path.basename(path)}: failed ({e})")
            time.sleep(1)
        done += [f"{os.path.basename(p)}: did not open in time" for p in pending]
        active = next((d["path"] for d in docs_state if d.get("active")), None)
        if active:  # opening an already-open file switches OnlyOffice to its tab
            _open([active])
        return done
