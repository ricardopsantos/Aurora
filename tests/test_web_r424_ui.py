"""R424: every screen of the chat page at 1280 and 390 px — vertical gaps on 0/8/16/32/64,
buttons on one line one height, dialog padding and bar layout the same everywhere."""
import pytest

pw = pytest.importorskip("playwright.sync_api")
from tests.test_web import app, call  # noqa
from tests.test_web_mobile import browser  # noqa
from tests.test_web_r421 import _chat

JS = r"""(scope) => {
 const OK=new Set([0,8,16,32,64]), out=[];
 const vis=e=>{const s=getComputedStyle(e),b=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&b.width>0&&b.height>0&&s.position!=='fixed'&&s.position!=='absolute'};
 const name=e=>{let n=e.tagName.toLowerCase();if(e.id)n+='#'+e.id;else if(e.className&&typeof e.className==='string')n+='.'+e.className.trim().split(/\s+/).slice(0,2).join('.');return n};
 const root=document.querySelector(scope)||document.body;
 for(const p of [root,...root.querySelectorAll('*')]){ if(!vis(p))continue;
   const kids=[...p.children].filter(vis); 
   for(let i=1;i<kids.length;i++){const a=kids[i-1].getBoundingClientRect(),b=kids[i].getBoundingClientRect();
     if(b.top<a.bottom-1)continue; const g=Math.round(b.top-a.bottom); if(!OK.has(g))out.push('gap '+g+'px '+name(kids[i-1])+' -> '+name(kids[i])+' in '+name(p));}}
 // buttons/inputs sharing a row inside the same parent: one height
 for(const p of root.querySelectorAll('*')){ if(!vis(p))continue;
   const ctl=[...p.children].filter(e=>vis(e)&&e.matches('button,.btn,select,input:not([type=checkbox]):not([type=radio]),.tag'));
   const rows={}; for(const c of ctl){const b=c.getBoundingClientRect();const k=Math.round(b.top+b.height/2);(rows[k]=rows[k]||[]).push([name(c),Math.round(b.height)]);}
   for(const r of Object.values(rows)){const hs=new Set(r.map(x=>x[1]));if(hs.size>1)out.push('heights '+r.map(x=>x[0]+'='+x[1]).join(' | '));}}
 const d=root.closest('dialog')||(root.tagName==='DIALOG'?root:null);
 if(d){const s=getComputedStyle(d);out.push('~dialog padding '+s.padding+' width '+Math.round(d.getBoundingClientRect().width));}
 return out;}"""

def audit(page, scope, tag, res):
    for f in page.evaluate(JS, scope):
        res.append(f"{tag}: {f}")

@pytest.mark.parametrize("w,h,mobile", [(1280, 800, False), (390, 800, True)])
def test_r424_every_screen_on_the_spacing_scale_and_one_height_per_row(browser, app, w, h, mobile, tmp_path):
    _chat(app, tmp_path)
    from aurora.web import attach
    (attach.outputs_dir() / "a.txt").write_text("x"); (attach.outputs_dir() / "b.md").write_text("# b")
    call(app, "/api/notes/create", {"title": "n1", "text": "t"})
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    page = ctx.new_page(); page.add_init_script("localStorage.setItem('aurora_web_token', 't0k');")
    page.goto(app.base + "/"); page.wait_for_selector("#model:not(:empty)"); page.wait_for_timeout(500)
    res = []
    side = lambda: (mobile and not page.is_visible("#notes-btn")) and page.click("#sb-toggle")
    audit(page, ".main", "chat", res)
    if not mobile: audit(page, "#side", "sidebar", res)
    else:
        page.click("#sb-toggle"); page.wait_for_timeout(400); audit(page, "#side", "sidebar", res); page.click("#sb-close"); page.wait_for_timeout(400)
    side(); page.click("#notes-btn"); page.wait_for_selector("#note-list .n"); audit(page, "#notes", "notes-list", res)
    page.click("#note-list .n"); page.wait_for_selector("#note-edit:not([hidden])"); page.wait_for_timeout(300); audit(page, "#notes", "notes-edit", res)
    side(); page.click("#files-btn"); page.click("#fl-tabs .tab[data-kind=outputs]"); page.wait_for_selector("#fl-list .fl-row"); audit(page, "#files", "files", res)
    page.click("#fl-list .fl-open"); page.wait_for_selector("#pv-body pre, #pv-body .pv-md"); audit(page, "#preview", "preview", res)
    page.keyboard.press("Escape"); page.keyboard.press("Escape"); page.wait_for_selector("#log", state="visible")
    side(); page.click("#settings-btn"); page.wait_for_selector("#settings.panel[open]"); audit(page, "#settings", "settings", res)   # R431
    page.keyboard.press("Escape"); page.wait_for_selector("#log", state="visible")
    page.click("#model"); page.click("#models-edit"); page.wait_for_function("document.getElementById('cfg-text').value.length > 0")
    audit(page, "#models", "models-raw", res); page.click("#md-close")   # R427, R429
    (mobile and not page.is_visible("#sessions")) and page.click("#sb-toggle"); row = page.locator("#sessions .chat").first
    (row.locator(".cm").click() if mobile else row.hover()); row.locator(".cd").click(); page.wait_for_selector("#export[open]")
    audit(page, "#export", "export", res); page.click("#ex-no")
    (mobile and not page.is_visible("#sessions")) and page.click("#sb-toggle"); row = page.locator("#sessions .chat").first
    (row.locator(".cm").click() if mobile and not row.locator(".cx").is_visible() else row.hover()); row.locator(".cx").click(); page.wait_for_selector("#confirm[open]")
    audit(page, "#confirm", "confirm", res); page.click("#cf-no")
    bad = [r for r in res if "~dialog" not in r and "button.fl-open" not in r]   # a Files row is a list row, taller than its actions on purpose
    assert not bad, "\n".join(bad)
    widths = {r.split(": ")[0]: r.rsplit(" ", 1)[1] for r in res if "~dialog" in r}
    assert widths["confirm"] == widths["export"]                       # the small question dialogs share one width
    pads = {r.split("padding ")[1].split(" ")[0] for r in res if "~dialog" in r and not r.startswith("notes")}
    assert pads == {"16px"}
    ctx.close()
