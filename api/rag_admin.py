"""Authenticated RAG observability API and a lightweight operator dashboard."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from api.security import require_api_token

router = APIRouter()


@router.get("/admin/rag/summary", dependencies=[Depends(require_api_token)])
async def rag_summary(request: Request, guild_id: int):
    return request.app.state.bot.corpus.rag_store.summary(guild_id)


@router.get("/admin/rag/queries", dependencies=[Depends(require_api_token)])
async def rag_queries(
    request: Request,
    guild_id: int,
    limit: int = Query(default=50, ge=1, le=200),
):
    entries = request.app.state.bot.corpus.rag_store.recent_queries(guild_id, limit)
    return {"entries": entries, "total": len(entries)}


@router.get("/admin/rag-ui", response_class=HTMLResponse)
async def rag_dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YagaPon RAG Quality</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0d12; --panel:#151922; --line:#293140;
      --text:#f5f7fb; --muted:#9aa6b6; --blue:#4d8dff; --green:#45d483;
      --yellow:#f4c45e; --red:#ff6b74; }
    * { box-sizing:border-box } body { margin:0; font-family:Inter,system-ui,sans-serif;
      background:var(--bg); color:var(--text) }
    main { max-width:1180px; margin:auto; padding:32px 20px 64px }
    header { display:flex; justify-content:space-between; gap:20px; align-items:end; flex-wrap:wrap }
    h1 { margin:0; font-size:30px } .sub { color:var(--muted); margin-top:6px }
    .controls { display:flex; gap:8px; flex-wrap:wrap }
    input,button { border:1px solid var(--line); background:var(--panel); color:var(--text);
      border-radius:10px; padding:10px 12px; font:inherit }
    input[type=password] { width:260px } button { background:var(--blue); border-color:var(--blue);
      font-weight:700; cursor:pointer }
    .notice { margin:20px 0; padding:12px 14px; background:#13223d; border:1px solid #23457d;
      border-radius:12px; color:#cbdcff }
    .grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:20px 0 }
    .card,.panel { background:var(--panel); border:1px solid var(--line); border-radius:16px }
    .card { padding:18px } .label { color:var(--muted); font-size:13px }
    .value { font-size:28px; font-weight:800; margin-top:8px }
    .panels { display:grid; grid-template-columns:1fr 1fr; gap:12px }
    .panel { padding:18px; overflow:auto } h2 { font-size:17px; margin:0 0 14px }
    table { width:100%; border-collapse:collapse; font-size:13px }
    th,td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top }
    th { color:var(--muted); font-weight:600 } .wide { grid-column:1/-1 }
    .pill { display:inline-block; border-radius:999px; padding:3px 8px; background:#252b37; margin:2px }
    .good { color:var(--green) } .warn { color:var(--yellow) } .bad { color:var(--red) }
    .empty { color:var(--muted); padding:20px 0 } code { color:#bfd4ff }
    @media(max-width:800px){.grid{grid-template-columns:1fr 1fr}.panels{grid-template-columns:1fr}}
  </style>
</head>
<body><main>
  <header><div><h1>YagaPon RAG Quality</h1>
    <div class="sub">検索品質・情報なし判定・利用者フィードバック</div></div>
    <div class="controls"><input id="guild" inputmode="numeric" placeholder="Discord Guild ID">
      <input id="token" type="password" placeholder="API token（保存しません）">
      <button onclick="loadData()">読み込む</button></div></header>
  <div class="notice" id="notice">トークンはこのページのメモリ内だけで利用し、ブラウザへ保存しません。</div>
  <section class="grid" id="cards"></section>
  <section class="panels">
    <div class="panel"><h2>年度別ローカル索引</h2><div id="festivals"></div></div>
    <div class="panel"><h2>データ種別</h2><div id="sources"></div></div>
    <div class="panel wide"><h2>最近の回答</h2><div id="queries"></div></div>
  </section>
</main><script>
let token = '';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path){const res=await fetch(path,{headers:{Authorization:`Bearer ${token}`}});
  if(!res.ok) throw new Error(`${res.status} ${await res.text()}`); return res.json()}
function card(label,value,klass=''){return `<div class="card"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`}
async function loadData(){
  token=document.getElementById('token').value; const guild=document.getElementById('guild').value.trim();
  if(!token||!guild){document.getElementById('notice').textContent='Guild IDとAPI tokenを入力してください。';return}
  document.getElementById('notice').textContent='読み込み中…';
  try { const [s,q]=await Promise.all([api(`/admin/rag/summary?guild_id=${encodeURIComponent(guild)}`),api(`/admin/rag/queries?guild_id=${encodeURIComponent(guild)}`)]);
    const rated=Object.values(s.feedback).reduce((a,b)=>a+b,0);
    document.getElementById('cards').innerHTML=card('回答数',s.queries)+card('情報なし',s.no_answer,s.no_answer?'warn':'')+card('平均応答',`${s.average_latency_ms} ms`)+card('高評価率',s.positive_rate===null?'未評価':`${Math.round(s.positive_rate*100)}%`,'good');
    document.getElementById('festivals').innerHTML=Object.entries(s.documents_by_festival).map(([k,v])=>`<span class="pill">${esc(k)}th: ${v}</span>`).join('')||'<div class="empty">索引なし</div>';
    document.getElementById('sources').innerHTML=s.documents_by_source.map(x=>`<span class="pill">${esc(x.source_type)}/${esc(x.status)}: ${x.count}</span>`).join('')||'<div class="empty">索引なし</div>';
    document.getElementById('queries').innerHTML=q.entries.length?`<table><thead><tr><th>日時</th><th>年度</th><th>品質</th><th>出典</th><th>応答</th><th>内容</th></tr></thead><tbody>${q.entries.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${x.festival??'—'}</td><td><span class="good">✅${x.positive}</span> <span class="warn">⚠️${x.partial}</span> <span class="bad">❌${x.negative}</span>${x.no_answer?' <span class="pill">情報なし</span>':''}</td><td>${x.citations.length}</td><td>${x.latency_ms}ms</td><td>${x.question_preview?esc(x.question_preview):'<span class="label">記録OFF</span>'}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">回答履歴なし</div>';
    document.getElementById('notice').textContent=`評価 ${rated}件・ローカル索引 ${s.local_documents}文書`;
  } catch(e){document.getElementById('notice').textContent=`取得失敗: ${e.message}`}
}
</script></body></html>"""
