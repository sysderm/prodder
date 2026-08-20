# Copyright (c) 2026 Alexander Navarini
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard front-end (HTML + CSS + JS), served by web() in
prodtop.py. Kept in its own module so the ~500-line page is editable
without scrolling past it in the main file; __PRODDER_KEY__ is replaced
with the per-session token when the page is served.
"""

WEB_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>prodder</title>
<style>
:root{
  --bg0:#0e1218; --bg1:#161c26; --card:rgba(255,255,255,.045);
  --card-hi:rgba(255,255,255,.07); --line:rgba(255,255,255,.08);
  --ink:#e8ecf2; --ink2:#9aa5b4; --ink3:#5f6b7c;
  --accent:#f0862d; --accent2:#f59a4a; --good:#3fbf6f; --warn:#e8b93e;
  --bad:#e5484d; --r:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,sans-serif;
  color:var(--ink); background:var(--bg0); min-height:100vh;
  background-image:
    radial-gradient(900px 500px at 85% -10%, rgba(240,134,45,.13), transparent 60%),
    radial-gradient(700px 500px at -10% 110%, rgba(63,120,240,.08), transparent 60%),
    linear-gradient(180deg,var(--bg1),var(--bg0) 40%);
  background-attachment:fixed;
  font-variant-numeric:tabular-nums;
}
header{
  position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;
  padding:12px 20px;backdrop-filter:blur(18px) saturate(1.3);
  background:rgba(14,18,24,.72);border-bottom:1px solid var(--line);
}
.logo{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;
  letter-spacing:.2px}
.logo svg{filter:drop-shadow(0 1px 4px rgba(240,134,45,.5));
  transform-origin:80% 20%}
.logo.prodding svg{animation:swing .55s ease}
@keyframes swing{0%{transform:rotate(0)}25%{transform:rotate(-14deg)}
  60%{transform:rotate(9deg)}100%{transform:rotate(0)}}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;
  background:var(--card);border:1px solid var(--line);color:var(--ink2);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ink3)}
.dot.ok{background:var(--good);box-shadow:0 0 6px rgba(63,191,111,.7)}
.dot.err{background:var(--bad);box-shadow:0 0 6px rgba(229,72,77,.6)}
.dot.scan{background:var(--warn);animation:pulse 1s infinite alternate}
@keyframes pulse{from{opacity:.4}to{opacity:1}}
.spacer{flex:1}
.stat{color:var(--ink2);font-size:12px}
.stat b{color:var(--ink);font-size:14px}
.stat .stalled{color:var(--warn)}
.seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.seg button{background:transparent;border:0;color:var(--ink2);padding:5px 12px;
  font:inherit;font-size:12px;cursor:pointer}
.seg button.on{background:var(--card-hi);color:var(--ink)}
.switch{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--ink2);
  cursor:pointer;user-select:none;border:0;background:transparent;font:inherit}
.track{width:34px;height:20px;border-radius:99px;background:var(--card-hi);
  border:1px solid var(--line);position:relative;transition:.18s}
.track::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;
  border-radius:50%;background:var(--ink2);transition:.18s}
.switch.on .track{background:linear-gradient(135deg,var(--accent2),var(--accent));
  border-color:transparent;box-shadow:0 1px 10px rgba(240,134,45,.45)}
.switch.on .track::after{left:16px;background:#fff}
.iconbtn{background:transparent;border:1px solid var(--line);border-radius:9px;
  color:var(--ink2);width:30px;height:30px;cursor:pointer;font-size:14px}
.iconbtn:hover{color:var(--ink);background:var(--card)}
button:focus-visible,[role="button"]:focus-visible,input:focus-visible{
  outline:2px solid var(--accent2);outline-offset:2px
}
#ticker{padding:6px 22px;font-size:12px;color:var(--ink2);cursor:pointer;
  border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
#ticker:empty{display:none}
main{max-width:1180px;margin:18px auto 80px;padding:0 20px;display:flex;
  flex-direction:column;gap:10px}
.card{
  background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  transition:background .15s, transform .15s, box-shadow .15s;
  backdrop-filter:blur(14px);
}
.card:hover{background:var(--card-hi);transform:translateY(-1px);
  box-shadow:0 8px 28px rgba(0,0,0,.35)}
.rowmain{display:grid;grid-template-columns:minmax(180px,1.2fr) 74px minmax(150px,1fr)
  200px 130px minmax(140px,1fr);gap:14px;align-items:center;padding:12px 16px;
  cursor:pointer}
.listhead{display:grid;grid-template-columns:minmax(180px,1.2fr) 74px minmax(150px,1fr)
  200px 130px minmax(140px,1fr);gap:14px;align-items:center;padding:4px 17px 0;
  font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:var(--ink3)}
.listhead .counts{font-size:10px;color:var(--ink3)}
.listhead .counts .big{color:var(--ink2);font-weight:600}
.listhead .rt{text-align:right}
.pname{font-weight:600;display:flex;align-items:center;gap:9px;min-width:0}
.pname .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.host{font-size:11px;color:var(--ink3);border:1px solid var(--line);
  border-radius:6px;padding:1px 6px;flex:none}
.mode{font-size:11px;font-weight:700;letter-spacing:.6px;border-radius:99px;
  padding:4px 11px;border:1px solid var(--line);color:var(--ink3);background:transparent;
  cursor:pointer;font-family:inherit}
.mode.prod{background:linear-gradient(135deg,var(--accent2),var(--accent));
  color:#fff;border-color:transparent;box-shadow:0 1px 10px rgba(240,134,45,.4)}
.agent{font-size:12.5px;color:var(--ink2);display:flex;align-items:center;gap:7px;
  min-width:0}
.agent .st{flex:none;width:8px;height:8px;border-radius:50%}
.st.run{background:var(--good);box-shadow:0 0 6px rgba(63,191,111,.6)}
.st.stalled{background:var(--warn);box-shadow:0 0 6px rgba(232,185,62,.6)}
.st.off{background:var(--ink3)} .st.det{background:var(--bad)}
.st.term{background:transparent;box-shadow:inset 0 0 0 1.5px var(--ink3)}
.agent .txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.counts{display:flex;gap:0;color:var(--ink3);font-size:12px}
.counts div{width:46px;text-align:right}
.counts .big{color:var(--ink);font-weight:600}
.spark{display:block}
.spark rect{fill:var(--accent);opacity:.9}
.spark line{stroke:rgba(255,255,255,.12)}
.lastf{font-size:12px;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;text-align:right}
.lastf span{color:var(--ink3)}
.detail{border-top:1px solid var(--line);padding:12px 16px;display:flex;
  flex-direction:column;gap:10px}
.files{font-size:12px;color:var(--ink2);display:grid;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:3px 20px}
.files>div{display:flex;gap:6px;align-items:baseline;min-width:0}
.files .age{color:var(--ink3);flex:none;width:52px}
.files .fp{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;direction:rtl;text-align:left}
.aline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12.5px;
  color:var(--ink2);padding:7px 10px;border-radius:10px;background:rgba(0,0,0,.18)}
.btn{font:inherit;font-size:12px;font-weight:600;border-radius:8px;cursor:pointer;
  padding:5px 12px;border:1px solid var(--line);background:var(--card-hi);
  color:var(--ink);transition:.12s}
.btn:hover{background:rgba(255,255,255,.12)}
.btn.hot{background:linear-gradient(135deg,var(--accent2),var(--accent));
  border-color:transparent;color:#fff;box-shadow:0 1px 10px rgba(240,134,45,.4)}
.btn.hot:hover{filter:brightness(1.1)}
.btn.danger:hover{background:rgba(229,72,77,.25);border-color:rgba(229,72,77,.4)}
.inline-form{display:flex;gap:8px;width:100%}
.inline-form input{flex:1;font:inherit;font-size:13px;color:var(--ink);
  background:rgba(0,0,0,.3);border:1px solid var(--line);border-radius:8px;
  padding:6px 10px;outline:none}
.inline-form input:focus{border-color:var(--accent)}
#log{position:fixed;right:18px;bottom:18px;width:420px;max-height:300px;
  overflow:auto;background:rgba(14,18,24,.92);border:1px solid var(--line);
  border-radius:var(--r);backdrop-filter:blur(20px);padding:12px 14px;z-index:20;
  font-size:12px;color:var(--ink2);display:none;box-shadow:0 12px 40px rgba(0,0,0,.5)}
#log.show{display:block}
#log div{padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.empty{color:var(--ink3);text-align:center;padding:60px 0;font-size:13px}
#notice{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:30;
  max-width:min(680px,calc(100vw - 32px));padding:9px 13px;border-radius:9px;
  background:rgba(14,18,24,.96);border:1px solid var(--line);color:var(--ink);
  box-shadow:0 10px 28px rgba(0,0,0,.35);opacity:0;pointer-events:none;
  transition:opacity .16s}
#notice.show{opacity:1} #notice.error{border-color:rgba(229,72,77,.65);color:#ffd6d8}
/* Nudge Lab */
#lab{display:none;margin:0 0 4px}
#lab.show{display:block}
.lab-grid{display:grid;grid-template-columns:1.3fr 1fr;gap:14px}
@media(max-width:820px){.lab-grid{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  padding:14px 16px;backdrop-filter:blur(14px);min-width:0}
.panel h3{font-size:12px;font-weight:700;letter-spacing:.5px;color:var(--ink2);
  text-transform:uppercase;margin-bottom:12px;display:flex;gap:8px;align-items:center}
.panel h3 .hint{font-weight:400;text-transform:none;letter-spacing:0;
  color:var(--ink3)}
.bar{margin:9px 0}
.bar .top{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;
  margin-bottom:4px;min-width:0}
.bar .nudge{color:var(--ink);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;min-width:0}
.bar .pct{color:var(--ink);font-weight:700;flex:none}
.bar .track2{height:9px;border-radius:99px;background:rgba(255,255,255,.06);
  overflow:hidden}
.bar .fill{height:100%;border-radius:99px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  box-shadow:0 0 10px rgba(240,134,45,.35);transition:width .5s ease}
.bar .sub{font-size:11px;color:var(--ink3);margin-top:3px;display:flex;gap:10px}
.bar .sub .g{color:var(--good)} .bar .sub .r{color:var(--warn)}
.bar .sub .d{color:var(--bad)}
.ev{display:flex;align-items:center;gap:8px;padding:6px 0;font-size:12px;
  border-bottom:1px solid rgba(255,255,255,.04)}
.ev .oc{flex:none;width:9px;height:9px;border-radius:50%;background:var(--ink3)}
.oc.productive{background:var(--good)} .oc.restalled{background:var(--warn)}
.oc.dropped{background:var(--bad)}
.ev .txt{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--ink2)}
.ev .txt b{color:var(--ink);font-weight:600}
.tag{font-size:10px;font-weight:700;letter-spacing:.4px;border-radius:5px;
  padding:1px 5px;border:1px solid var(--line);color:var(--ink3);flex:none}
.tag.reassess{color:var(--accent);border-color:rgba(240,134,45,.4)}
.tag.custom{color:var(--good);border-color:rgba(63,191,111,.35)}
.vote{background:transparent;border:0;cursor:pointer;font-size:13px;opacity:.5;
  padding:0 2px}
.vote:hover{opacity:1}
.vote.on{opacity:1;filter:drop-shadow(0 0 4px rgba(240,134,45,.6))}
.lab-empty{color:var(--ink3);font-size:12px;padding:8px 0}
.workbench{padding:16px;display:flex;flex-direction:column;gap:14px}
.workbench-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.workbench-head h2{font-size:16px;letter-spacing:.1px}.workbench-head p{color:var(--ink2);font-size:12px;flex:1}
.task-form{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:start}
.task-form textarea{grid-column:1/-1;min-height:54px;resize:vertical;font:inherit;font-size:13px;color:var(--ink);
  background:rgba(0,0,0,.3);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.task-form select{font:inherit;font-size:13px;color:var(--ink);background:rgba(0,0,0,.3);
  border:1px solid var(--line);border-radius:8px;padding:7px 10px;min-width:0}
.queues{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.queue{border:1px solid var(--line);border-radius:10px;padding:10px;min-height:110px;background:rgba(0,0,0,.13)}
.queue h3{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--ink2);margin-bottom:8px}
.task{padding:9px 0;border-top:1px solid rgba(255,255,255,.07);display:flex;flex-direction:column;gap:6px}
.task:first-of-type{border-top:0}.task-title{font-size:13px;font-weight:650}.task-meta{font-size:11px;color:var(--ink3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-criteria{font-size:11px;color:var(--ink2);padding-left:16px}.task-actions{display:flex;gap:6px;flex-wrap:wrap}
.queue-empty{font-size:12px;color:var(--ink3);padding:6px 0}
@media(max-width:820px){.queues{grid-template-columns:1fr}.task-form{grid-template-columns:1fr}.task-form button{width:max-content}}
@media(max-width:900px){.rowmain{grid-template-columns:1fr 70px 1fr;row-gap:6px}
  .counts,.spark,.lastf,.listhead{display:none}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;
  transition:none!important;scroll-behavior:auto!important}}
</style></head><body>
<header>
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 24 24"><g>
      <path d="M3 21 19 4" stroke="#8b5e34" stroke-width="2.6" stroke-linecap="round"/>
      <path d="M19 4 c1.5 3.5 .5 5.5 -1.5 7" stroke="#d8d3c8" stroke-width="1.1"
        fill="none"/>
      <path d="M17.5 11 l2.6 1.4 -3.4 8 c-.5 1 -1.9 .6 -1.8 -.5 z" fill="#f0862d"/>
      <path d="M17.2 10.6 l1.6 -3 1 .5 -.2 3.4 z" fill="#5cab4a"/>
    </g></svg>
    prodder
  </div>
  <div class="pills" id="hosts"></div>
  <div class="spacer"></div>
  <div class="stat" id="stat"></div>
  <div class="seg" id="sortseg">
    <button data-s="recency" class="on">Recent</button>
    <button data-s="24h">24h</button>
  </div>
  <button class="switch" id="autoswitch" type="button" role="switch" aria-checked="false"
    aria-label="Toggle automatic nudging of stalled agents">auto-prod <span class="track"></span></button>
  <button class="iconbtn" id="labbtn" type="button" aria-label="Open nudge effectiveness lab">🥕</button>
  <button class="iconbtn" id="logbtn" type="button" aria-label="Open message log">≡</button>
  <button class="iconbtn" id="quitbtn" type="button" aria-label="Quit Prodder">⏻</button>
</header>
<div id="ticker"></div>
<main>
  <section class="panel workbench" id="workbench" aria-labelledby="workbench-title">
    <div class="workbench-head">
      <div><h2 id="workbench-title">Build with intent</h2><p>Define what good looks like, then verify before you ship.</p></div>
      <button class="btn hot" id="newtask" type="button">New task</button>
    </div>
    <div id="taskform"></div>
    <div class="queues" id="taskqueues"></div>
  </section>
  <div id="lab">
    <div class="lab-grid">
      <div class="panel">
        <h3>Nudge effectiveness <span class="hint" id="labhint"></span></h3>
        <div id="leaderboard"></div>
      </div>
      <div class="panel">
        <h3>Recent prods <span class="hint">👍/👎 to teach it</span></h3>
        <div id="events"></div>
      </div>
    </div>
  </div>
  <div class="listhead" aria-hidden="true">
    <div>project</div><div>mode</div><div>agent</div>
    <div class="counts">
      <div title="files written in the last 15 minutes">15m</div>
      <div title="files written in the last hour">1h</div>
      <div class="big" title="files written in the last 24 hours">24h</div>
      <div title="files written in the last 3 days">3d</div>
    </div>
    <div>activity 24h</div><div class="rt">recent file</div>
  </div>
  <div id="list"><div class="empty">waiting for first scan…</div></div>
</main>
<div id="log"></div>
<div id="notice" role="status" aria-live="polite"></div>
<script>
"use strict";
const KEY="__PRODDER_KEY__";   // per-session token, injected when this page is served
let SORT="recency", DATA=null;
const open=new Set(), forms={};   // key -> {kind:'type'|'nudge'}
const $=(s,p)=>(p||document).querySelector(s);
function el(tag,cls,text){const e=document.createElement(tag);
  if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function fmtAge(s){if(s==null||s<0)return"?";if(s<90)return Math.round(s)+"s";
  if(s<5400)return Math.round(s/60)+"m";if(s<129600)return Math.round(s/3600)+"h";
  return Math.round(s/86400)+"d"}
async function api(body){
  try{
    const r=await fetch("/api/action",{method:"POST",
      headers:{"Content-Type":"application/json","X-Prodder":"1","X-Prodder-Key":KEY},
      body:JSON.stringify(body)});
    const data=await r.json().catch(()=>({msg:"unexpected server response"}));
    notice(data.msg||("request failed ("+r.status+")"),!r.ok);refresh(true);return r.ok
  }catch(e){notice("server unreachable",true);return false}
}
let noticeTimer;
function notice(msg,error){const n=$("#notice");n.textContent=msg;
  n.classList.toggle("error",!!error);n.classList.add("show");clearTimeout(noticeTimer);
  noticeTimer=setTimeout(()=>n.classList.remove("show"),4200)}
function agentAction(action,a,extra={}){
  return {action,host:a.host,tty:a.tty,pid:a.pid,action_id:a.action_id,...extra}
}
async function refresh(force){
  try{
    const r=await fetch("/api/state?sort="+SORT); DATA=await r.json();
  }catch(e){ $("#stat").textContent="server unreachable"; return }
  render(force)
}
let hoverList=false, pendingRender=false;
function typingNow(){const a=document.activeElement;
  return a&&a.tagName==="INPUT"}
function spark(buckets){
  const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
  svg.setAttribute("class","spark");svg.setAttribute("width","120");
  svg.setAttribute("height","26");svg.setAttribute("viewBox","0 0 120 26");
  const b=[...buckets].reverse(), max=Math.max(1,...b);
  const base=document.createElementNS(svg.namespaceURI,"line");
  base.setAttribute("x1",0);base.setAttribute("y1",25.5);
  base.setAttribute("x2",119);base.setAttribute("y2",25.5);
  svg.appendChild(base);
  b.forEach((v,i)=>{
    if(v>0){
      const h=Math.max(2,Math.round(v/max*24));
      const r=document.createElementNS(svg.namespaceURI,"rect");
      r.setAttribute("x",i*5);r.setAttribute("y",26-h);r.setAttribute("width",4);
      r.setAttribute("height",h);r.setAttribute("rx",1);
      const ti=document.createElementNS(svg.namespaceURI,"title");
      ti.textContent=(23-i)+"h ago — "+v+" files";r.appendChild(ti);
      svg.appendChild(r);
    }
  });
  return svg
}
function agentLabel(a){
  if(a.web)return[["off"],"⊘ web tab"];
  if(a.recognized===false)return[["term"],"▹ "+a.name+" · terminal idle "+fmtAge(a.idle)];
  if(a.protected)return[["off"],"🔒 "+a.name+" (protected)"];
  if(a.policy==="ignore")return[["off"],"⊘ "+a.name+" "+fmtAge(a.idle)+" — left alone"];
  if(a.detached)return[["det"],"⊗ "+a.name+" — detached"];
  if(a.stalled){let s="⚠ "+a.name+" stalled "+fmtAge(a.idle);
    if(a.next_prod>0)s+=" · next prod "+fmtAge(a.next_prod);
    return[["stalled"],s]}
  return[["run"],a.name+" active · idle "+fmtAge(a.idle)]
}
function inlineForm(key,kind,row,agent){
  const f=el("div","inline-form");
  const inp=el("input");
  inp.placeholder=kind==="type"?("type into "+agent.name+" — Enter sends, Esc cancels")
    :("custom nudge for "+row.name+" — empty resets to default, Esc cancels");
  if(kind==="nudge"&&row.nudge)inp.value=row.nudge;
  const send=el("button","btn hot",kind==="type"?"Send":"Save");
  const cancel=el("button","btn","Cancel");
  const done=()=>{delete forms[key];render(true)};
  send.onclick=()=>{const v=inp.value;done();
    if(kind==="type"){if(v.trim())api(agentAction("type",agent,{text:v.trim()}))}
    else api({action:"nudge",host:row.host,path:row.path,text:v.trim()})};
  cancel.onclick=done;
  inp.onkeydown=e=>{if(e.key==="Enter")send.onclick();
    if(e.key==="Escape")cancel.onclick()};
  f.append(inp,send,cancel);
  setTimeout(()=>inp.focus(),0);
  return f
}
function detail(row,key){
  const d=el("div","detail");
  if(row.recent.length){
    const fl=el("div","files");
    row.recent.forEach(([age,rel])=>{
      const line=el("div");
      const fp=el("span","fp",rel);fp.title=rel;
      line.append(el("span","age",fmtAge(age)+" ago"),fp);
      fl.appendChild(line)});
    d.appendChild(fl);
  }
  row.agents.forEach(a=>{
    const line=el("div","aline");
    const [cls,txt]=agentLabel(a);
    const dot=el("span","st "+cls[0]);dot.className="st "+cls[0];
    line.append(dot,el("span","",txt+"  ·  pid "+a.pid+"  ·  "+a.where
      +"  ·  cpu "+a.cpu+"%"));
    const sp=el("span");sp.style.flex="1";line.appendChild(sp);
    if(!a.web&&!a.protected&&a.recognized===false){
      // a plain window with no known agent: only offer manual Type…, never a
      // blunt auto-prod or a Close on a shell we can't identify.
      const bt=el("button","btn","Type…");
      bt.onclick=e=>{e.stopPropagation();forms[key]={kind:"type",tty:a.tty};render(true)};
      line.append(bt);
    } else if(!a.web&&!a.protected){
      const bp=el("button","btn hot","Prod");
      bp.onclick=e=>{e.stopPropagation();
        api(agentAction("prod",a))};
      const bt=el("button","btn","Type…");
      bt.onclick=e=>{e.stopPropagation();forms[key]={kind:"type",tty:a.tty};render(true)};
      line.append(bp,bt);
      if(a.detached){
        const bo=el("button","btn","Reopen");
        bo.onclick=e=>{e.stopPropagation();
          if(confirm("Reopen "+a.name+" in a new window? Kills the detached "
            +"session and resumes it."))
            api(agentAction("reopen",a))};
        line.appendChild(bo);
      }
      const bx=el("button","btn danger","Close");
      bx.onclick=e=>{e.stopPropagation();
        if(confirm("Close "+a.name+" (pid "+a.pid+")? Resume info is saved to "
          +"closed-agents.md first."))
          api(agentAction("close",a))};
      line.appendChild(bx);
    }
    d.appendChild(line);
  });
  const bar=el("div","inline-form");
  const bn=el("button","btn","Nudge…");
  bn.onclick=e=>{e.stopPropagation();forms[key]={kind:"nudge"};render(true)};
  bar.appendChild(bn);
  if(row.nudge)bar.appendChild(el("span","stat",'custom nudge: "'+row.nudge+'"'));
  d.appendChild(bar);
  const f=forms[key];
  if(f){
    const agent=f.tty?row.agents.find(a=>a.tty===f.tty):null;
    d.appendChild(inlineForm(key,f.kind,row,agent||row.agents[0]||{}));
  }
  return d
}
const OUTCOME_LABEL={productive:"produced work",restalled:"stayed stuck",
  dropped:"agent left"};
function renderLab(d){
  if(!$("#lab").classList.contains("show"))return;
  const lb=d.leaderboard||[];
  const board=$("#leaderboard");board.textContent="";
  const scored=lb.filter(r=>r.sent>0);
  $("#labhint").textContent=scored.length
    ? "share of prods that led to real file output"
    : "";
  if(!scored.length){
    board.appendChild(el("div","lab-empty",
      "No prods yet — send a few (or let auto-prod run) and the phrasings "
      +"that actually resume work will rank here."));
  } else {
    scored.forEach(r=>{
      const bar=el("div","bar");
      const top=el("div","top");
      top.append(el("span","nudge",'"'+r.nudge+'"'),
        el("span","pct",Math.round(r.rate*100)+"%"));
      const track=el("div","track2");
      const fill=el("div","fill");fill.style.width=Math.round(r.rate*100)+"%";
      track.appendChild(fill);
      const sub=el("div","sub");
      sub.append(el("span","","n="+r.sent),
        el("span","g","✓ "+r.productive+" file activity"),
        el("span","r","· "+r.restalled+" stuck"),
        el("span","d","· "+r.dropped+" left"));
      bar.append(top,track,sub);board.appendChild(bar);
    });
  }
  const ev=$("#events");ev.textContent="";
  const evs=d.events||[];
  if(!evs.length){ev.appendChild(el("div","lab-empty","No prods yet."));}
  evs.forEach(e=>{
    const row=el("div","ev");
    row.appendChild(el("span","oc "+(e.outcome||"")));
    if(e.kind==="reassess"||e.kind==="custom")
      row.appendChild(el("span","tag "+e.kind,e.kind));
    const txt=el("div","txt");
    txt.append(el("b","",e.project),
      document.createTextNode('  "'+e.nudge+'"  ·  '+fmtAge(e.age)+" ago"
        +(e.outcome?"  →  "+OUTCOME_LABEL[e.outcome]:"  · pending")));
    row.appendChild(txt);
    const up=el("button","vote"+(e.human===true?" on":""),"👍");
    const dn=el("button","vote"+(e.human===false?" on":""),"👎");
    up.onclick=()=>api({action:"feedback",id:e.id,good:true});
    dn.onclick=()=>api({action:"feedback",id:e.id,good:false});
    row.append(up,dn);
    ev.appendChild(row);
  });
}
function taskForm(){
  const holder=$("#taskform");holder.textContent="";
  const form=el("div","task-form");
  const title=el("input");title.placeholder="What do you want to make, fix, or verify?";
  const project=document.createElement("select");
  const rows=(DATA&&DATA.rows)||[];
  rows.forEach(r=>{const o=document.createElement("option");o.value=r.host+"\u0000"+r.path;
    o.textContent=r.name+" · "+r.host;project.appendChild(o)});
  const criteria=document.createElement("textarea");
  criteria.placeholder="Acceptance criteria — one per line (for example: checkout works on mobile)";
  const create=el("button","btn hot","Create task");
  const cancel=el("button","btn","Cancel");
  create.onclick=()=>{const parts=project.value.split("\u0000");if(!title.value.trim()||parts.length!==2){
      notice("Choose a project and describe the task",true);return}
    api({action:"task_create",host:parts[0],path:parts[1],title:title.value.trim(),
      acceptance:criteria.value.split("\n").map(x=>x.trim()).filter(Boolean)}).then(ok=>{if(ok)holder.textContent=""})};
  cancel.onclick=()=>{holder.textContent=""};
  form.append(title,project,create,criteria,cancel);holder.appendChild(form);title.focus()
}
function taskCard(task){
  const card=el("article","task");
  card.append(el("div","task-title",task.title));
  card.append(el("div","task-meta",task.project_path));
  if(task.acceptance&&task.acceptance.length){const list=el("ul","task-criteria");
    task.acceptance.slice(0,3).forEach(c=>list.appendChild(el("li","",c)));card.appendChild(list)}
  if(task.latest_check){card.append(el("div","task-meta","latest check: "+task.latest_check.status+
    (task.latest_check.name?" · "+task.latest_check.name:"")))}
  const actions=el("div","task-actions");
  const set=(status,label)=>{const b=el("button","btn",label);b.onclick=()=>api({action:"task_status",task_id:task.id,status});actions.appendChild(b)};
  if(task.status==="planned")set("in_progress","Start");
  if(task.status==="in_progress"){set("blocked","Need input");set("needs_review","Ready to review")}
  if(task.status==="blocked")set("in_progress","Resume");
  if(task.status==="needs_review"){const verify=el("button","btn hot","Verify");
    verify.onclick=()=>api({action:"task_verify",task_id:task.id});actions.appendChild(verify);set("done","Mark done")}
  card.appendChild(actions);return card
}
function renderWorkbench(d){
  const queues=$("#taskqueues");queues.textContent="";
  const wb=d.workbench||{queues:{needs_you:[],working:[],ready:[]}};
  [["needs_you","Needs you","Nothing waiting for a decision."],
   ["working","Working","No active tasks yet."],
   ["ready","Ready to review","Nothing ready to verify."]].forEach(([key,label,empty])=>{
    const q=el("section","queue");q.appendChild(el("h3","",label));
    const tasks=(wb.queues&&wb.queues[key])||[];
    if(!tasks.length)q.appendChild(el("div","queue-empty",empty));
    tasks.forEach(t=>q.appendChild(taskCard(t)));queues.appendChild(q)})
}
function render(force){
  if(!DATA||typingNow())return;
  // never reshuffle rows under the pointer — a click must not land on a row
  // that just re-sorted itself; the update runs when the mouse leaves
  if(hoverList&&!force){pendingRender=true;return}
  pendingRender=false;
  const d=DATA;
  const hosts=$("#hosts");hosts.textContent="";
  Object.entries(d.hosts).forEach(([n,h])=>{
    const p=el("span","pill");
    const dot=el("span","dot "+(h.error?"err":h.scanning?"scan":h.ok?"ok":""));
    p.append(dot,document.createTextNode(n));
    if(h.error)p.title=h.error;
    hosts.appendChild(p)});
  const st=$("#stat");st.textContent="";
  st.append(el("b","",String(d.agent_count)),
    document.createTextNode(" agents"));
  if(d.stalled_count){const s=el("span","stalled",
    "  ·  "+d.stalled_count+" stalled");st.appendChild(s)}
  $("#autoswitch").classList.toggle("on",d.auto_prod);
  $("#autoswitch").setAttribute("aria-checked",String(!!d.auto_prod));
  const t=$("#ticker");t.textContent=d.msgs.length?d.msgs[d.msgs.length-1]:"";
  const lg=$("#log");lg.textContent="";
  [...d.msgs].reverse().forEach(m=>lg.appendChild(el("div","",m)));
  renderWorkbench(d);
  renderLab(d);
  const list=$("#list");list.textContent="";
  if(!d.rows.length){list.appendChild(el("div","empty","waiting for first scan…"));
    return}
  d.rows.forEach(row=>{
    const key=row.host+"|"+row.path;
    const card=el("div","card");
    const main=el("div","rowmain");main.tabIndex=0;main.setAttribute("role","button");
    main.setAttribute("aria-expanded",String(open.has(key)));
    main.setAttribute("aria-label","Show details for "+row.name);
    const pn=el("div","pname");
    pn.append(el("span","nm",row.name),el("span","host",row.host));
    const mode=el("button","mode"+(row.mode==="PROD"?" prod":""),row.mode);
    mode.title="click to switch between PROD (auto-nudge stalled agents) and leave";
    mode.onclick=e=>{e.stopPropagation();
      if(row.mode!=="PROD"&&!confirm("Set "+row.name+" to PROD? Stalled agents "
        +"there get nudged automatically."))return;
      api({action:"mode",host:row.host,path:row.path,
           value:row.mode==="PROD"?"ignore":"auto"})};
    const ag=el("div","agent");
    if(row.agents.length){
      const a=row.agents[0];const [cls,txt]=agentLabel(a);
      ag.append(el("span","st "+cls[0]),el("span","txt",
        txt+(row.agents.length>1?"  +"+(row.agents.length-1):"")));
    }
    const counts=el("div","counts");
    const CH={"15m":"last 15 minutes","1h":"last hour","24h":"last 24 hours","3d":"last 3 days"};
    ["15m","1h","24h","3d"].forEach(l=>{
      const c=el("div",l==="24h"?"big":"",String(row.counts[l]||0));
      c.title="files written in the "+CH[l];counts.appendChild(c)});
    const lf=el("div","lastf");
    lf.append(document.createTextNode(row.latest_file+" "),
      el("span","","("+fmtAge(row.latest_age)+" ago)"));
    main.append(pn,mode,ag,counts,spark(row.buckets),lf);
    const toggle=()=>{open.has(key)?open.delete(key):open.add(key);render(true)};
    main.onclick=toggle;
    main.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();toggle()}};
    card.appendChild(main);
    if(open.has(key))card.appendChild(detail(row,key));
    list.appendChild(card);
  });
}
$("#sortseg").onclick=e=>{const b=e.target.closest("button");if(!b)return;
  SORT=b.dataset.s;
  $("#sortseg").querySelectorAll("button").forEach(x=>
    x.classList.toggle("on",x===b));
  refresh()};
$("#autoswitch").onclick=()=>{const next=!DATA.auto_prod;
  if(next&&!confirm("Turn on automatic nudging? Prodder will only nudge quiet, unchanged, non-protected agent screens, but this can still spend tokens."))return;
  api({action:"autoprod",value:next})};
$("#newtask").onclick=()=>taskForm();
$("#logbtn").onclick=()=>$("#log").classList.toggle("show");
$("#ticker").onclick=()=>$("#log").classList.toggle("show");
$("#labbtn").onclick=()=>{$("#lab").classList.toggle("show");
  $("#labbtn").classList.toggle("on");if(DATA)renderLab(DATA)};
$("#quitbtn").onclick=()=>{if(confirm("Quit prodder? Scanning and auto-prodding "
  +"stop."))api({action:"quit"})};
const listEl=$("#list");
listEl.addEventListener("mouseenter",()=>{hoverList=true});
listEl.addEventListener("mouseleave",()=>{hoverList=false;
  if(pendingRender)render()});
// swing the carrot whenever a fresh prod lands in the message log
let lastProdMsg="";
function pulseOnProd(d){
  const last=(d.msgs||[]).filter(m=>/prodded|approved prompt/.test(m)).pop()||"";
  if(last&&last!==lastProdMsg){lastProdMsg=last;
    const lg=$(".logo");lg.classList.remove("prodding");void lg.offsetWidth;
    lg.classList.add("prodding")}
}
const _origRender=render;
render=function(f){_origRender(f);if(DATA)pulseOnProd(DATA)};
refresh();setInterval(()=>refresh(),2500);
</script></body></html>
"""
