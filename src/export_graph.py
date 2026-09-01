#!/usr/bin/env python3
"""Export the knowledge graph in formats that can be visualised.

LightRAG's web UI needs its own server (lightrag-server) running, and that server assumes
LightRAG's storage layout.  We use LanceDB, so it cannot be used as is.
Instead this exports **standard graph formats** for off-the-shelf viewers.

Four formats are written:
  graphml   Gephi · Cytoscape · yEd · networkx.  GraphRAG uses this format too
  json      {nodes, links} for 3d-force-graph / vis.js / D3
  html      a self-contained 3D viewer (three.js inlined, no CDN) — a browser is all it needs
  obsidian  [[wikilink]] notes written into the vault so Obsidian's 3D Graph plugin reads them
"""
import os, sys, json, html, glob, argparse, collections

# Where ~/.kal lives.  Mounted at /data/kal inside the container (see docker-compose).
KAL_HOME = os.environ.get("KAL_HOME", os.path.expanduser("~/.kal"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lancedb

DB = os.environ.get("KAL_PATH", os.path.join(KAL_HOME, "db"))
OUT = os.environ.get("KAL_OUT", os.path.join(KAL_HOME, "graph_export"))
from schema_v3 import (TYPE_COLOR, IDENTITY_TYPES, count_identity,
                       llm_gate, REDACTED)   # one place for the policy and one for the colours



def _fixture_if_no_db():
    """Swap in a **fixture** when there is no real DB.  Returns a cleanup function.

    The self-check was opening the real DB at `~/.kal` —— it ran only on the author's machine
    and died in CI with `Table 'lr_entities' was not found`.  `just` stops at the first
    failure, so the 20-odd self-checks after it never ran (reproduced 2026-08-25, round 4).

    ⚠ **When a real DB exists, it is used.**  Always covering it with a fixture would hide
       defects that only real data reveals —— a check running is not a check guarding.
    """
    global DB
    import shutil
    import fixture_db
    if fixture_db.db_is_usable(DB):
        return lambda: None                         # the real DB is intact
    _was, DB = DB, fixture_db.build()
    print(f"  ⓘ no real DB, running against a fixture ({_was} → temporary)")
    def _cleanup(_p=DB, _o=_was):
        global DB
        DB = _o
        shutil.rmtree(_p, ignore_errors=True)
    return _cleanup


def _selftest():
    import schema_v3
    stray = set(IDENTITY_TYPES) - set(schema_v3.CANON_TYPES)
    assert not stray, f"IDENTITY_TYPES holds non-canonical types {stray} —— the warning becomes permanently 0"
    assert IDENTITY_TYPES, "identity types are empty —— the warning becomes permanently 0"
    E = [{"type": t} for t in IDENTITY_TYPES] + [{"type": "concept"}, {}, {"type": None}]
    assert count_identity(E) == len(IDENTITY_TYPES), "identity types are not counted"
    assert count_identity([{"type": "concept"}]) == 0, "the wrong type is counted"
    # ── Does the transmission gate **actually run** ────────────────────
    #    The vault has 0 no_llm documents, so on a normal run it never executes once.  Then
    #    deleting the gate entirely breaks nothing —— which is exactly why this file went
    #    without one.  One document is blocked **by force** and inspected.
    #    (The same approach as export_kal_graph._selftest.)
    import collections, lancedb as _l
    _real = _l.connect
    _db = _real(DB)
    _E = _db.open_table("lr_entities").search().limit(999999).to_list()
    _docs = _db.open_table("documents").search().limit(999999).to_list()
    _cnt = collections.Counter(x for e in _E for x in (e.get("doc_ids") or []))
    _tgt = _cnt.most_common(1)[0][0]
    _tgt_path = next(d["path"] for d in _docs if d["doc_id"] == _tgt)

    class _T:
        def __init__(s, rows): s.rows = rows
        def search(s): return s
        def limit(s, n): return s
        def to_list(s): return s.rows

    class _DB:
        def __init__(s, d): s.d = d
        def open_table(s, n):
            if n != "documents":
                return s.d.open_table(n)
            rows = [dict(r) for r in _docs]
            for r in rows:
                if r["doc_id"] == _tgt:
                    r["no_llm"] = True
            return _T(rows)

    _l.connect = lambda *a, **k: _DB(_db)
    try:
        _Eg, _Rg, _Dg = load(min_degree=1)
    finally:
        _l.connect = _real

    #  ① an entity sourced only from that document drops out entirely (its name is derived too)
    _should = {e["entity_id"] for e in _E if set(e.get("doc_ids") or ()) <= {_tgt}}
    _kept = {e["entity_id"] for e in _Eg}
    assert not (_should & _kept), \
        f"{len(_should & _kept)} entity(ies) sourced only from the blocked document still go out"
    assert _should, "the test does not hold on this vault (no sole-sourced entity)"
    #  ② an entity with other sources survives with its description redacted
    assert any(e["description"] == REDACTED for e in _Eg), "not one description was redacted"
    #  ③ the blocked document's **path** survives nowhere —— the path itself is information
    assert _tgt not in _Dg, "the blocked document survives in D —— its path goes out through docs"
    _j = json.dumps(to_json(_Eg, _Rg, _Dg), ensure_ascii=False)
    assert _tgt_path not in _j, "the blocked document's path leaked into the JSON"
    assert _tgt_path not in to_graphml(_Eg, _Rg, _Dg), "the blocked document's path leaked into the graphml"
    print(f"  ✅ export_graph identity warning ({'·'.join(IDENTITY_TYPES)}) · transmission gate"
          f" (1 forced → {len(_should)} entity(ies) dropped · no path leak)")


def load(min_degree=1, limit_nodes=None):
    db = lancedb.connect(DB)
    #  Only the columns read.  384-float vectors are not dragged along (the same list as its two siblings).
    #  ⚠ `doc_ids` is read by the transmission gate, so it is required on both sides —— without it everything is blocked.
    #  ⚠ `documents` is not projected (the self-check's fake table has no `.select()`).
    E = db.open_table("lr_entities").search().select(
        ["entity_id", "name", "type", "description", "doc_ids", "degree"]
    ).limit(999999).to_list()
    R = db.open_table("lr_relations").search().select(
        ["src_id", "tgt_id", "doc_ids", "description", "keywords"]
    ).limit(999999).to_list()
    D = {r["doc_id"]: r for r in db.open_table("documents").search().limit(99999).to_list()}

    #  ⛔ Anything drawn from a `no_llm: true` document is not exported.
    #
    #  This file had **no gate at all.**  Its sibling export_kal_graph.py filters the same rows
    #  at the same stage (:200-223) while this one let them through —— and in export_all.sh:23
    #  **this one runs first**.
    #
    #  What went out: entity descriptions and **vault paths**, into some 700 `$VAULT/kg/*.md`
    #  (tracked by git), into graph.graphml, and into the self-contained `graph3d.html`.  That
    #  last one is the file :32 copies into viewer/ —— built to be handed to people.
    #
    #  With 0 no_llm documents in the vault today it was latent —— a gate is quiet when the
    #  data holds no case for it.  The moment the first one appears, one side blocks and the
    #  other leaks.  `_selftest` creates one **by force** and walks this path.  (r4-sec, 2026-08-21)
    blocked = {d["doc_id"] for d in D.values() if d.get("no_llm")}
    if blocked:
        keep = []
        for e in E:
            v = llm_gate(e.get("doc_ids"), blocked)
            if v == "block":
                continue                      # drop the whole entity —— the name is derived too
            if v == "redact":
                e = dict(e); e["description"] = REDACTED
            keep.append(e)
        print(f"   {len(blocked)} document(s) excluded from transmission → {len(E) - len(keep)} entity(ies) dropped")
        E = keep
        kept = []
        for r in R:
            v = llm_gate(r.get("doc_ids"), blocked)
            if v == "block":
                continue
            if v == "redact":
                r = dict(r); r["description"] = REDACTED
            kept.append(r)
        R = kept
        #  The path is derived too —— it is stopped from going out through `docs`
        D = {k: v for k, v in D.items() if k not in blocked}

    E = [e for e in E if e["degree"] >= min_degree]
    if limit_nodes:
        E = sorted(E, key=lambda x: -x["degree"])[:limit_nodes]
    keep = {e["entity_id"] for e in E}
    R = [r for r in R if r["src_id"] in keep and r["tgt_id"] in keep]
    return E, R, D


def to_json(E, R, D):
    id2i = {e["entity_id"]: i for i, e in enumerate(E)}
    nodes = [{
        "id": e["entity_id"], "name": e["name"], "type": e["type"],
        "degree": e["degree"], "color": TYPE_COLOR.get(e["type"], TYPE_COLOR["other"]),   # one place for the default colour too
        "val": max(1, e["degree"]) ** 0.5,          # the 3d-force-graph node size
        "desc": (e["description"] or "")[:400],
        "docs": [D[x]["path"] for x in e["doc_ids"] if x in D][:8],
    } for e in E]
    links = [{
        "source": r["src_id"], "target": r["tgt_id"],
        "desc": (r["description"] or "")[:300],
        "keywords": list(r["keywords"])[:6],
        "docs": [D[x]["path"] for x in r["doc_ids"] if x in D][:6],
    } for r in R if r["src_id"] in id2i and r["tgt_id"] in id2i]
    return {"nodes": nodes, "links": links}


def to_graphml(E, R, D):
    """GraphML — the standard Gephi/Cytoscape/yEd/networkx read.  GraphRAG uses it too."""
    esc = lambda s: html.escape(str(s or ""), quote=True)
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">']
    for k, t in [("name", "string"), ("type", "string"), ("degree", "int"),
                 ("description", "string"), ("docs", "string")]:
        L.append(f'  <key id="n_{k}" for="node" attr.name="{k}" attr.type="{t}"/>')
    for k in ("description", "keywords", "docs"):
        L.append(f'  <key id="e_{k}" for="edge" attr.name="{k}" attr.type="string"/>')
    L.append('  <graph edgedefault="undirected">')       # ⑤ undirected, stated explicitly
    for e in E:
        L.append(f'    <node id="n{e["entity_id"]}">')
        L.append(f'      <data key="n_name">{esc(e["name"])}</data>')
        L.append(f'      <data key="n_type">{esc(e["type"])}</data>')
        L.append(f'      <data key="n_degree">{e["degree"]}</data>')
        L.append(f'      <data key="n_description">{esc((e["description"] or "")[:600])}</data>')
        L.append(f'      <data key="n_docs">{esc("; ".join(D[x]["path"] for x in e["doc_ids"] if x in D))}</data>')
        L.append('    </node>')
    for i, r in enumerate(R):
        L.append(f'    <edge id="e{i}" source="n{r["src_id"]}" target="n{r["tgt_id"]}">')
        L.append(f'      <data key="e_description">{esc((r["description"] or "")[:600])}</data>')
        L.append(f'      <data key="e_keywords">{esc(", ".join(r["keywords"]))}</data>')
        L.append(f'      <data key="e_docs">{esc("; ".join(D[x]["path"] for x in r["doc_ids"] if x in D))}</data>')
        L.append('    </edge>')
    L += ['  </graph>', '</graphml>']
    return "\n".join(L)


def to_obsidian(E, R, D, vault_sub):
    """Write entity notes into the Obsidian vault, joined by [[wikilink]].
    → Obsidian's built-in Graph View and the 3D Graph plugin read them as they are.
    Existing notes are never touched; it writes only into a dedicated subfolder."""
    # The notes a previous run made are deleted first.  When the KG changes, notes for
    # vanished entities are left orphaned and keep showing as ghost nodes in Obsidian's graph.
    # Only what we made is selected by frontmatter — the user's notes are untouched.
    if os.path.isdir(vault_sub):
        for f in glob.glob(os.path.join(vault_sub, "*.md")):
            if "type: kg-entity" in open(f, encoding="utf-8", errors="ignore").read(400):
                os.remove(f)
    os.makedirs(vault_sub, exist_ok=True)
    id2e = {e["entity_id"]: e for e in E}
    nb = collections.defaultdict(list)
    for r in R:
        nb[r["src_id"]].append((r["tgt_id"], r))
        nb[r["tgt_id"]].append((r["src_id"], r))
    safe = lambda s: "".join(c if c not in '\\/:*?"<>|#^[]' else "-" for c in s)[:80]
    written = 0
    for e in E:
        fn = safe(e["name"])
        lines = ["---", f'title: "{e["name"]}"', "type: kg-entity",
                 f'entity_type: {e["type"]}', f'degree: {e["degree"]}',
                 "tags: [kg, kg-" + e["type"] + "]", "---", "",
                 f'# {e["name"]}', "", f'> [!info] {e["type"]} · degree {e["degree"]}',
                 "", (e["description"] or "")[:900], "", "## Relations", ""]
        for oid, r in sorted(nb[e["entity_id"]], key=lambda x: -id2e[x[0]]["degree"])[:40]:
            if oid in id2e:
                lines.append(f'- [[{safe(id2e[oid]["name"])}]] — {(r["description"] or "")[:150]}')
        lines += ["", "## Source documents", ""]
        for x in e["doc_ids"]:
            if x in D:
                lines.append(f'- [[{os.path.basename(D[x]["path"])[:-3]}]]  `{D[x]["path"]}`')
        open(f"{vault_sub}/{fn}.md", "w", encoding="utf-8").write("\n".join(lines))
        written += 1
    return written


VIEWER = """<!doctype html><html><head><meta charset="utf-8"><title>Knowledge Graph</title>
<style>
 html,body{margin:0;height:100%;background:#0E1418;color:#E4EAEF;
   font:13px/1.5 -apple-system,'Pretendard','Apple SD Gothic Neo',sans-serif;overflow:hidden}
 #cv{display:block}
 #hud{position:fixed;top:0;left:0;padding:14px 16px;pointer-events:none}
 #hud h1{margin:0 0 4px;font-size:15px;letter-spacing:-.01em}
 #hud .s{color:#95A3AF;font-size:11.5px}
 #legend{position:fixed;bottom:14px;left:16px;display:flex;flex-wrap:wrap;gap:4px 12px;max-width:60%}
 #legend b{font-weight:500;font-size:11px;color:#95A3AF;display:flex;align-items:center;gap:5px}
 #legend i{width:9px;height:9px;border-radius:50%;display:inline-block}
 #tip{position:fixed;padding:9px 11px;background:#161E25;border:1px solid #35454F;
   border-radius:3px;max-width:340px;display:none;pointer-events:none;z-index:9}
 #tip b{color:#5AA9DA} #tip .d{color:#95A3AF;margin-top:5px;font-size:11.5px;line-height:1.5}
 #ui{position:fixed;top:14px;right:16px;display:flex;flex-direction:column;gap:6px;align-items:flex-end}
 #ui input{background:#161E25;border:1px solid #35454F;color:#E4EAEF;padding:5px 9px;
   border-radius:3px;width:180px;font:12px inherit}
 #ui button{background:#161E25;border:1px solid #35454F;color:#95A3AF;padding:4px 9px;
   border-radius:3px;cursor:pointer;font:11px inherit}
 #ui button:hover{color:#E4EAEF;border-color:#5AA9DA}
</style></head><body>
<canvas id="cv"></canvas>
<div id="hud"><h1>__TITLE__</h1><div class="s">__SUB__</div></div>
<div id="ui"><input id="q" placeholder="search entities…"><button id="rst">reset view</button></div>
<div id="legend"></div><div id="tip"></div>
<script>
const DATA=__DATA__;
const cv=document.getElementById('cv'),ctx=cv.getContext('2d'),tip=document.getElementById('tip');
let W,H;function rs(){W=cv.width=innerWidth*devicePixelRatio;H=cv.height=innerHeight*devicePixelRatio;
cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';}rs();addEventListener('resize',rs);
const N=DATA.nodes,L=DATA.links,idx=new Map(N.map((n,i)=>[n.id,i]));
// 3D placement — spherical initialisation, then force-directed
N.forEach((n,i)=>{const p=Math.acos(1-2*(i+.5)/N.length),t=Math.PI*(1+Math.sqrt(5))*i,r=260+Math.random()*90;
 n.x=r*Math.sin(p)*Math.cos(t);n.y=r*Math.sin(p)*Math.sin(t);n.z=r*Math.cos(p);n.vx=n.vy=n.vz=0;});
const E=L.map(l=>[idx.get(l.source),idx.get(l.target),l]).filter(e=>e[0]!=null&&e[1]!=null);
function step(){
 for(let i=0;i<N.length;i++){const a=N[i];
  for(let j=i+1;j<N.length;j++){const b=N[j];
   let dx=b.x-a.x,dy=b.y-a.y,dz=b.z-a.z,d2=dx*dx+dy*dy+dz*dz+.01;
   if(d2>90000)continue;const f=1400/d2,d=Math.sqrt(d2);
   dx/=d;dy/=d;dz/=d;a.vx-=dx*f;a.vy-=dy*f;a.vz-=dz*f;b.vx+=dx*f;b.vy+=dy*f;b.vz+=dz*f;}}
 for(const[i,j]of E){const a=N[i],b=N[j];
  let dx=b.x-a.x,dy=b.y-a.y,dz=b.z-a.z,d=Math.sqrt(dx*dx+dy*dy+dz*dz)+.01,f=(d-95)*.010;
  dx/=d;dy/=d;dz/=d;a.vx+=dx*f;a.vy+=dy*f;a.vz+=dz*f;b.vx-=dx*f;b.vy-=dy*f;b.vz-=dz*f;}
 for(const n of N){n.vx-=n.x*.0016;n.vy-=n.y*.0016;n.vz-=n.z*.0016;
  n.x+=n.vx*=.86;n.y+=n.vy*=.86;n.z+=n.vz*=.86;}}
let rx=.3,ry=.5,zoom=1,ox=0,oy=0,drag=null,hover=null,query='';
function proj(n){const cy=Math.cos(ry),sy=Math.sin(ry),cx=Math.cos(rx),sx=Math.sin(rx);
 let x=n.x*cy-n.z*sy,z=n.x*sy+n.z*cy,y=n.y*cx-z*sx;z=n.y*sx+z*cx;
 const s=760/(760+z)*zoom;return[W/2+(x*s+ox)*devicePixelRatio,H/2+(y*s+oy)*devicePixelRatio,s,z];}
function draw(){step();ctx.fillStyle='#0E1418';ctx.fillRect(0,0,W,H);
 const P=N.map(proj);const ord=N.map((_,i)=>i).sort((a,b)=>P[b][3]-P[a][3]);
 ctx.lineWidth=devicePixelRatio*.55;
 for(const[i,j]of E){const A=P[i],B=P[j];
  const on=hover!=null&&(i===hover||j===hover);
  ctx.strokeStyle=on?'rgba(90,169,218,.75)':'rgba(120,140,160,.13)';
  ctx.beginPath();ctx.moveTo(A[0],A[1]);ctx.lineTo(B[0],B[1]);ctx.stroke();}
 for(const i of ord){const n=N[i],p=P[i];
  const m=query&&n.name.toLowerCase().includes(query);
  const r=Math.max(2,(2.2+n.val*1.5)*p[2])*devicePixelRatio;
  ctx.globalAlpha=query?(m?1:.14):(hover==null||hover===i?1:.5);
  ctx.fillStyle=n.color;ctx.beginPath();ctx.arc(p[0],p[1],r,0,7);ctx.fill();
  if(i===hover||m||n.degree>=18){ctx.globalAlpha=1;ctx.fillStyle='#E4EAEF';
   ctx.font=`${11*devicePixelRatio}px -apple-system,sans-serif`;
   ctx.fillText(n.name.slice(0,26),p[0]+r+3*devicePixelRatio,p[1]+4*devicePixelRatio);}}
 ctx.globalAlpha=1;requestAnimationFrame(draw);}
cv.onmousedown=e=>drag={x:e.clientX,y:e.clientY,b:e.button};
onmouseup=()=>drag=null;
cv.oncontextmenu=e=>e.preventDefault();
cv.onmousemove=e=>{
 if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
  if(drag.b===2){ox+=dx;oy+=dy;}else{ry+=dx*.006;rx+=dy*.006;}
  drag.x=e.clientX;drag.y=e.clientY;return;}
 const P=N.map(proj);let best=null,bd=1e9;
 for(let i=0;i<N.length;i++){const d=Math.hypot(P[i][0]/devicePixelRatio-e.clientX,
   P[i][1]/devicePixelRatio-e.clientY);if(d<14&&d<bd){bd=d;best=i;}}
 hover=best;
 if(best!=null){const n=N[best];
  // ⚠ The names, descriptions and document paths all come from **the vault and a
  //   user-edited file (aliases.yml)**.  This HTML is self-contained and built to be handed
  //   on, so putting them into innerHTML directly turns one alias line into a script running
  //   in the recipient's browser.  (adversarial review 2026-08-18, security lens)
  tip.innerHTML=`<b>${esc(n.name)}</b> <span style="color:#6D7D8A">${esc(n.type)} · degree ${n.degree|0}</span>`+
   `<div class="d">${esc(n.desc||'')}</div>`+
   (n.docs.length?`<div class="d" style="color:#5AA9DA">${n.docs.map(esc).join('<br>')}</div>`:'');
  tip.style.display='block';
  tip.style.left=Math.min(e.clientX+14,innerWidth-360)+'px';tip.style.top=(e.clientY+14)+'px';
 }else tip.style.display='none';};
// HTML escaping.  Every string entering this viewer comes from the vault or from user input.
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
cv.onwheel=e=>{e.preventDefault();zoom*=e.deltaY<0?1.09:.92;zoom=Math.max(.15,Math.min(6,zoom));};
document.getElementById('q').oninput=e=>query=e.target.value.toLowerCase();
document.getElementById('rst').onclick=()=>{rx=.3;ry=.5;zoom=1;ox=oy=0;};
const seen=[...new Set(N.map(n=>n.type))].sort();
document.getElementById('legend').innerHTML=seen.map(t=>{
 const c=N.find(n=>n.type===t).color,k=N.filter(n=>n.type===t).length;
 // color is our own palette but type is a string an LLM wrote —— both are blocked
 return `<b><i style="background:${esc(String(c))}"></i>${esc(t)} ${k|0}</b>`;}).join('');
draw();
</script></body></html>"""


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _c = _fixture_if_no_db()
        try:
            _selftest()
        #  ⚠ **`sys.exit(0)` must not sit inside a `finally`.**  Leaving through a finally
        #     **replaces** the exception in flight —— a broken assert still exits 0, and the
        #     self-check becomes structurally unable to fail.  This repository really was in
        #     that state (round 4 created it and round 5 caught it): the same commit used the
        #     correct shape in `export_webgl` and `status`.
        finally:
            _c()
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-degree", type=int, default=2, help="exclude nodes below this degree")
    ap.add_argument("--max-nodes", type=int, default=600, help="the top N by degree only")
    ap.add_argument("--obsidian", metavar="DIR", help="write notes into a subfolder of the Obsidian vault")
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    E, R, D = load(a.min_degree, a.max_nodes)
    print(f"nodes {len(E)} · edges {len(R)}  (min-degree {a.min_degree}, max-nodes {a.max_nodes})")

    g = to_json(E, R, D)
    json.dump(g, open(f"{OUT}/graph.json", "w"), ensure_ascii=False)
    print(f"  graph.json     {os.path.getsize(f'{OUT}/graph.json')/1024:.0f} KB   3d-force-graph · vis.js · D3")

    open(f"{OUT}/graph.graphml", "w", encoding="utf-8").write(to_graphml(E, R, D))
    print(f"  graph.graphml  {os.path.getsize(f'{OUT}/graph.graphml')/1024:.0f} KB   Gephi · Cytoscape · yEd")

    # This HTML has 0 dependencies and is built to be handed on as it is —— the entity names go
    # inside it, and some of them are real people and organisations.  A build log disappears
    # when the terminal closes and does not travel with the file, so the notice is baked
    # **into the file**.  (adversarial review 2026-08-18, security lens.  The same as galaxy.html)
    n_id = count_identity(E)
    id_lbl = "·".join(IDENTITY_TYPES)
    note = (f"\n<!--\n  ⚠ This file is self-contained.  Handing it on carries {len(E)} entity names"
            f" with it\n     ({id_lbl} {n_id} — real names and organisations possible).\n"
            f"  Credential masking has been applied, but identity information is not checked.\n-->\n")
    hp = f"{OUT}/graph3d.html"
    open(hp, "w", encoding="utf-8").write(
        VIEWER.replace("__DATA__", json.dumps(g, ensure_ascii=False))
              .replace("__TITLE__", "Knowledge Graph")
              .replace("__SUB__", f"entities {len(E)} · relations {len(R)} · "
                                  f"left-drag rotate · right-drag pan · wheel zoom")
        + note)
    print(f"  graph3d.html   {os.path.getsize(hp)/1024:.0f} KB   a browser is all it needs (0 dependencies)")
    print(f"     ⚠ {len(E)} entity name(s) ({id_lbl} {n_id}) are inside the file")

    if a.obsidian:
        n = to_obsidian(E, R, D, a.obsidian)
        print(f"  obsidian       {n} note(s) → {a.obsidian}")
        print(f"                 Check with Graph View / the 3D Graph plugin in Obsidian")
