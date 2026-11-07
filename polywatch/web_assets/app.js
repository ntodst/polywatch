'use strict';
const paths = {
  users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  radar:'M12 2a10 10 0 1 0 10 10M12 6a6 6 0 1 0 6 6M12 12l9-9M18 3h3v3',
  activity:'M2 12h4l3-9 6 18 3-9h4', flask:'M9 3h6M10 3v6L4 19a1.4 1.4 0 0 0 1.2 2h13.6A1.4 1.4 0 0 0 20 19L14 9V3M7 14h10',
  archive:'M3 3h18v5H3zM5 8v13h14V8M10 12h4', settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M10 2h4l1 3 3-1 2 3-2 3 3 2-1 4-3 1-1 3h-4l-1-3-3 1-2-3 2-3-3-2 1-4 3-1z',
  layers:'m12 3 10 6-10 6L2 9zM2 13l10 6 10-6M2 17l10 6 10-6', server:'M3 3h18v7H3zM3 14h18v7H3zM7 6h.01M7 17h.01',
  refresh:'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2l1 4M4 12l1 4a8 8 0 0 0 13 2', download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  arrow:'M5 12h14m-5-5 5 5-5 5', trend:'m3 17 6-6 4 4 8-11M15 4h6v6', shield:'M12 3 3 7v5c0 6 9 10 9 10s9-4 9-10V7zM8 12l3 3 5-6',
  clock:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0M12 6v6l4 2', search:'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0m-2 5 6 6',
  book:'M12 5C8 2 3 3 3 3v17s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2zm0 0v17', chart:'M3 3v18h18M7 14l4-5 4 3 5-7',
  check:'m5 12 4 4L19 6', external:'M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7', filter:'M3 5h18M6 12h12M9 19h6', plus:'M12 5v14M5 12h14'
};
const icon = n => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[n] || paths.chart}"/></svg>`;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (n, compact=false) => n == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:compact?1:0,notation:compact?'compact':'standard'}).format(n);
const signed = n => n == null ? '—' : `${n>0?'+':''}${money(n)}`;
const count = n => new Intl.NumberFormat('en-US').format(n || 0);
const percent = n => n == null ? '—' : `${(n*100).toFixed(0)}%`;
const short = a => a ? `${a.slice(0,6)}…${a.slice(-4)}` : '—';
const date = at => at ? new Date(at*1000).toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'}) : '—';
const dateTime = at => at ? new Date(at*1000).toLocaleString() : '—';
const ago = at => { if(!at) return 'Awaiting data'; const d=Math.max(0,Date.now()/1000-at);return d<60?`${Math.floor(d)}s ago`:d<3600?`${Math.floor(d/60)}m ago`:d<86400?`${Math.floor(d/3600)}h ago`:`${Math.floor(d/86400)}d ago`; };
const signClass = n => n==null?'muted':n<0?'negative':n>0?'positive':'';
const validAddress = value => /^0x[a-fA-F0-9]{40}$/.test(value);
const names = {traders:'Top traders',markets:'Market radar',activity:'Activity log',paper:'Paper lab',archive:'Research archive',system:'System'};
const state = {data:null,page:'traders',filter:'all',search:'',sort:'evidence',chartWallet:'',chartDetail:null,chartRequest:0,archiveKind:'trades',archiveWallet:'',archiveOffset:0,archive:null,archiveRequest:0,detailWallet:'',fetching:false,toastTimer:null,drafts:{}};

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-Polywatch':'1'},body:JSON.stringify(body)});
  if(!r.ok) {let msg=`Request failed (${r.status})`;try{msg=(await r.json()).error||msg;}catch{}throw new Error(msg);}
  return r.json();
}
function toast(message) { $('toast').textContent=message;$('toast').hidden=false;clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>{$('toast').hidden=true;},4200); }
function fillIcons(root=document) {root.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);});}
function head(title,description,actions='') {return `<div class="page-head"><div><div class="eyebrow">POLYMARKET INTELLIGENCE</div><h1>${title}</h1><p>${description}</p></div><div class="head-actions">${actions}</div></div>`;}
function metric(title,value,note,name='chart',cls='') {return `<div class="metric"><div class="metric-top"><span>${title}</span>${icon(name)}</div><div class="metric-value ${cls}">${value}</div><div class="metric-bottom">${note}</div></div>`;}
function empty(title,message,name='book',action='') {return `<div class="empty">${icon(name)}<h3>${title}</h3><p>${message}</p>${action}</div>`;}
function badge(stage) {const values={long_term:['green','Long-term screen'],building:['amber','Building evidence'],pending:['','Verification pending'],excluded:['red','Excluded · audit only'],open:['green','Open simulation'],closed:['','Closed'],queued:['amber','Awaiting quote'],skipped:['','Skipped']};const [cls,label]=values[stage]||['',stage];return `<span class="badge ${cls}"><i class="dot"></i>${esc(label)}</span>`;}
function avatar(row,index=0) {return `<span class="avatar color${index%5}">${esc((row.name||row.address||'?').replace(/^0x/,'').slice(0,2).toUpperCase())}</span>`;}
function traderName(row,index=0) {return `<div class="trader-cell">${avatar(row,index)}<button class="trader-link" data-trader="${esc(row.address)}"><strong>${esc(row.name)}</strong><small>${esc(short(row.address))}</small></button></div>`;}
function nameFor(address) {return state.data?.traders.find(r=>r.address===address)?.name || short(address);}
function stageTabs() {
 const d=state.data; const tabs=[['all','All candidates',d.stages.building+d.stages.long_term],['historical','Historical pass',d.traders.filter(r=>r.consistent&&r.stage==='building').length],['long_term','Long-term',d.stages.long_term],['shortlist','Shortlist',d.settings.shortlist.length],['excluded','Audit',d.stages.excluded]];
 return tabs.map(([key,label,n])=>`<button class="tab ${state.filter===key?'active':''}" data-filter="${key}" aria-pressed="${state.filter===key}">${label}<span class="tab-count">${count(n)}</span></button>`).join('');
}
function chartSVG(points,options={}) {
 let list=points.filter(p=>Number.isFinite(p.at)&&Number.isFinite(p.pnl));
 if(options.days&&list.length) {const cutoff=Date.now()/1000-options.days*86400;list=list.filter(p=>p.at>=cutoff);}
 const w=640,h=188,left=51,right=15,top=20,bottom=30;
 const insufficient=list.length<2;
 let labels='',grid='';
 const base=list[0]?.pnl || 0;
 const values=list.map(p=>p.pnl-base);
 let low=insufficient?-100:Math.min(0,...values),high=insufficient?100:Math.max(0,...values);
 if(high-low<1){high+=1;low-=1;}
 const pad=(high-low)*.14;high+=pad;low-=pad;
 const y=v=>top+(high-v)/(high-low)*(h-top-bottom);
 for(let i=0;i<4;i++){const v=high-(high-low)*i/3,yp=y(v);grid+=`<path class="chart-grid" d="M${left} ${yp}H${w-right}"/>`;labels+=`<text class="chart-axis" x="${left-9}" y="${yp+3}" text-anchor="end">${esc(money(v,true))}</text>`;}
 let line='';
 if(!insufficient){const start=list[0].at,end=list.at(-1).at;const x=t=>left+(t-start)/(end-start||1)*(w-left-right);const d=list.map((p,i)=>`${i?'L':'M'}${x(p.at).toFixed(2)} ${y(p.pnl-base).toFixed(2)}`).join(' ');line=`<path d="${d} L${x(end)} ${h-bottom} L${left} ${h-bottom} Z" fill="url(#${options.id||'pnl-fill'})" stroke="none"/><path class="chart-line" d="${d}"/>`;for(let i=0;i<4;i++){const at=start+(end-start)*i/3;labels+=`<text class="chart-axis" x="${x(at)}" y="${h-7}" text-anchor="${i===0?'start':i===3?'end':'middle'}">${esc(new Date(at*1000).toLocaleDateString(undefined,{day:'numeric',month:'short'}))}</text>`;}}
 return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${insufficient?'No sufficient recorded PnL series yet':'Change in recorded position PnL in US dollars'}"><defs><linearGradient id="${options.id||'pnl-fill'}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#abc997" stop-opacity=".28"/><stop offset="100%" stop-color="#abc997" stop-opacity="0"/></linearGradient></defs>${grid}${line}${labels}</svg>${insufficient?`<div class="chart-empty">${icon('chart')}<strong>Good research starts with a baseline.</strong><span>Daily observations will build this chart. No performance is assumed before data arrives.</span></div>`:''}`;
}
function tradersPage() {
 const d=state.data,all=d.traders;
 const active=all.filter(r=>!['excluded','pending'].includes(r.stage));
 const maxDays=Math.max(0,...all.map(r=>r.local.observation_days));
 const maxSamples=Math.max(0,...all.map(r=>r.local.daily_samples));
 const chartRow=all.find(r=>r.address===state.chartWallet);
 const options=active.slice(0,100).map(r=>`<option value="${esc(r.address)}" ${r.address===state.chartWallet?'selected':''}>${esc(r.name)}</option>`).join('');
 let rows=all.filter(r=>state.filter==='shortlist'?d.settings.shortlist.includes(r.address):state.filter==='excluded'?r.stage==='excluded':state.filter==='long_term'?r.stage==='long_term':state.filter==='historical'?r.consistent&&r.stage==='building':!['excluded','pending'].includes(r.stage));
 const query=state.search.toLowerCase();rows=rows.filter(r=>`${r.name} ${r.address}`.toLowerCase().includes(query));
 if(state.sort==='paper')rows.sort((a,b)=>(b.paper.pnl??-Infinity)-(a.paper.pnl??-Infinity));
 if(state.sort==='observed')rows.sort((a,b)=>(b.local.net_pnl??-Infinity)-(a.local.net_pnl??-Infinity));
 if(state.sort==='profit')rows.sort((a,b)=>(b.account_pnl??-Infinity)-(a.account_pnl??-Infinity));
 const totalRows=rows.length;rows=rows.slice(0,100);
 return head('Find the traders worth following.','Follow consistency over time. Build a shortlist backed by evidence.',`<button class="button" data-method>${icon('book')}Our methodology</button><a class="button primary" href="/api/traders.csv">${icon('download')}Export research</a>`)+
 `<div class="metrics">${metric('Verified candidates',count(d.stages.building+d.stages.long_term),`${count(d.stages.pending)} awaiting verification`,'users')}${metric('Long-term screen',count(d.stages.long_term),'All evidence requirements met','shield')}${metric('Longest observation',`${Math.floor(maxDays)}<span class="subvalue">days observed locally</span>`,`${count(maxSamples)} covered daily comparisons at most`,'clock')}${metric('Simulated copy PnL',signed(d.paper_totals.priced?d.paper_totals.pnl:null),`${count(d.paper_totals.priced)} priced closes · all sources included`,'trend',signClass(d.paper_totals.priced?d.paper_totals.pnl:null))}</div>`+
 `<div class="overview-grid"><section class="panel"><div class="panel-head"><div><h2 class="chart-heading"><i class="dot"></i>The consistency curve</h2><p>Observed account PnL change · USD · last 90 days</p></div><label><span class="visually-hidden">Trader shown on chart</span><select id="chart-wallet" class="select-plain">${options||'<option value="">No verified traders yet</option>'}</select></label></div><div class="chart-wrap">${chartSVG(state.chartDetail?.series||[],{days:90})}</div><div class="chart-caption"><span class="legend"><i></i>${esc(chartRow?.name||'Locally observed PnL')}</span><span>Daily samples · includes open PnL</span></div></section><aside class="evidence"><span class="evidence-mark">${icon('layers')}</span><div class="eyebrow">EVIDENCE, NOT A LUCKY STREAK</div><h2>A bigger sample.<br>A clearer picture.</h2><p>Let results survive time, losing trades, and the cost of following them.</p><div class="progress">${Array.from({length:12},(_,i)=>`<span class="${i<Math.min(12,Math.floor(maxDays/90*12))?'filled':''}"></span>`).join('')}</div><div class="evidence-detail"><span>Longest local observation</span><strong>${Math.floor(maxDays)} / 90 days</strong></div><button class="text-button" data-method>See the full screening criteria ${icon('arrow')}</button></aside></div>`+
 `<div class="section-top"><div><h2>Your trader discovery desk</h2><p>Ranked by evidence completeness, observed coverage, then simulated and account profit.</p></div><span>${count(d.trader_count)} discovered wallets</span></div>`+
 `<section class="panel table-panel"><div class="table-toolbar"><div class="tabs" aria-label="Trader filters">${stageTabs()}</div><label class="search">${icon('search')}<span class="visually-hidden">Search traders</span><input id="trader-search" placeholder="Search name or wallet…" value="${esc(state.search)}" autocomplete="off"></label></div><div class="table-scroll"><table><thead><tr><th>#</th><th>Trader</th><th><button class="text-button" data-sort="profit" title="Sort by verified lifetime account PnL">Overall PnL</button></th><th><button class="text-button" data-sort="observed">Observed change</button></th><th>Positive days</th><th><button class="text-button" data-sort="paper">Paper net</button></th><th><button class="text-button" data-sort="evidence">Evidence status</button></th><th><span class="visually-hidden">Save trader</span></th></tr></thead><tbody>${rows.map((r,i)=>`<tr class="trader-row"><td class="rank">${String(i+1).padStart(2,'0')}</td><td>${traderName(r,i)}</td><td><span class="num ${signClass(r.account_pnl)}">${signed(r.account_pnl)}</span><span class="subvalue">${esc(ago(r.pnl_observed_at))} · account total</span></td><td><span class="num ${signClass(r.local.net_pnl)}">${signed(r.local.net_pnl)}</span><span class="subvalue">${Math.floor(r.local.observation_days)}d observed · ${r.local.daily_samples} daily pairs</span></td><td class="num">${percent(r.local.positive_days)}<span class="subvalue">${r.local.active_days} active days</span></td><td><span class="num ${signClass(r.paper.pnl)}">${signed(r.paper.pnl)}</span><span class="subvalue">${r.paper.priced} priced / ${r.paper.closed} closes</span></td><td>${badge(r.stage)}<span class="subvalue">${r.passed} of ${r.checks.length} requirements</span></td><td><button class="star ${d.settings.shortlist.includes(r.address)?'saved':''}" data-save="${esc(r.address)}" aria-label="${d.settings.shortlist.includes(r.address)?'Remove from':'Add to'} shortlist" aria-pressed="${d.settings.shortlist.includes(r.address)}">${d.settings.shortlist.includes(r.address)?'★':'☆'}</button></td></tr>`).join('')}</tbody></table></div>${rows.length?'':empty(state.filter==='long_term'?'Let the evidence earn its place.':state.filter==='shortlist'?'Your shortlist starts here.':state.search?'No matching traders.':'Your scanner is building the picture.',state.filter==='long_term'?'No trader currently meets every long-term requirement. Open a profile to see exactly what is missing.':state.filter==='shortlist'?'Save a trader with the star, or add a wallet address from System. Your shortlist survives restarts.':state.search?'Try another name or wallet address.':`${count(d.stages.pending)} wallets awaiting account verification. Candidates appear when their account PnL is verified; scanner status is available in System.`,'users')}<div class="table-footer"><span>Showing ${count(rows.length)} of ${count(totalRows)} matching wallets${d.trader_count>500?' · first 500 ranked wallets loaded':''}</span><span class="muted">Research view · no live orders</span><button class="text-button" data-page="system">Add a wallet ${icon('plus')}</button></div></section>`;
}
function marketsPage() {
 const d=state.data; const query=state.search.toLowerCase();const rows=d.markets.filter(r=>`${r.title} ${r.outcome} ${r.category}`.toLowerCase().includes(query));
 return head('Where conviction is building.','Verified, meaningful holdings across admitted traders. Freshness stays visible.')+
 `<div class="metrics">${metric('Active outcomes',count(d.market_count),'Verified tradable exposure','radar')}${metric('Verified candidates',count(d.stages.building+d.stages.long_term),'Nonnegative account PnL','users')}${metric('Pending position checks',count(d.verification.pending),'Balance changes are verified first','clock')}${metric('Position scanner',ago(d.verification.last_ok),'Last successful verification','activity')}</div>`+
 `<div class="filters"><label class="search">${icon('search')}<span class="visually-hidden">Search markets</span><input id="market-search" value="${esc(state.search)}" placeholder="Search markets or categories…"></label><span class="muted">Largest exposure first · up to 100 outcomes</span></div>`+
 (rows.length?`<div class="page-grid">${rows.map(r=>`<article class="panel market-card card-body"><div class="market-top"><span>${esc(r.category)}</span><span class="badge green">${esc(r.outcome)}</span></div><h3>${esc(r.title)}</h3><div class="market-stats"><div><small>NET HELD EXPOSURE</small><strong>${money(r.held)}</strong></div><div><small>ADMITTED HOLDERS</small><strong>${r.holders}</strong></div><div><small>PROFITABLE HOLDERS</small><strong>${r.profitable}</strong></div></div><div class="market-foot"><span>Oldest verification ${esc(ago(r.verified_at))}</span><span>${esc(r.condition_id.slice(0,12))}…</span></div><div class="dialog-actions">${[...new Set(r.positions.map(p=>p.proxy_wallet))].slice(0,4).map(a=>`<button class="text-button" data-trader="${esc(a)}">${esc(nameFor(a))} ${icon('arrow')}</button>`).join('')}</div></article>`).join('')}</div>`):`<section class="panel">${empty('Waiting for verified holdings.','Holdings appear after account verification, complete balance coverage, and market availability checks. Old or unknown positions are kept out of the live radar.','radar')}</section>`);
}
function activityPage() {
 const d=state.data;const rows=d.activity.filter(r=>`${r.title} ${r.kind} ${nameFor(r.proxy_wallet)}`.toLowerCase().includes(state.search.toLowerCase()));
 return head('Every signal has a paper trail.','Recent admitted-wallet alerts and confirmed position changes.',`<button class="button" data-page="archive">${icon('archive')}Browse full archive</button>`)+
 `<div class="filters"><label class="search">${icon('search')}<span class="visually-hidden">Search recent activity</span><input id="activity-search" value="${esc(state.search)}" placeholder="Search signals or markets…"></label></div><section class="panel"><div class="table-scroll"><table><thead><tr><th>Detected</th><th>Signal</th><th>Trader</th><th>Market / outcome</th><th>Value</th></tr></thead><tbody>${rows.map(r=>`<tr><td title="${esc(dateTime(r.at))}">${esc(ago(r.at))}</td><td><span class="badge ${r.kind==='REDUCED'||r.kind==='EXITED'?'amber':'green'}">${esc(r.kind)}</span></td><td><button class="text-button" data-trader="${esc(r.proxy_wallet)}">${esc(nameFor(r.proxy_wallet))}</button></td><td class="wrap">${esc(r.title||r.condition_id)}<span class="subvalue">${esc(r.outcome||'')} ${esc(r.side||'')}</span></td><td class="num">${money(r.current_value??r.bundle_amount??(r.size!=null?r.size*r.price:null))}</td></tr>`).join('')}</tbody></table></div>${rows.length?'':empty('Quiet, for now.','New alerts and verified holding changes will appear here. Initial baselines are not presented as new entries.','activity')}<div class="table-footer"><span>Latest 100 admitted-wallet events</span><span>Excluded sources remain in the research archive.</span></div></section>`;
}
function paperPage() {
 const d=state.data,p=d.paper_totals;
 return head('Test the follow. Keep the evidence.','Prospective simulations using post-detection order books and estimated fees.',`<button class="button" data-page="system">${icon('settings')}Paper settings</button><a class="button primary" href="/api/export?kind=paper">${icon('download')}Export all signals</a>`)+
 `<div class="metrics">${metric('Net closed PnL',signed(p.priced?p.pnl:null),`${p.priced} priced results · includes losing sources`,'trend',signClass(p.priced?p.pnl:null))}${metric('Open simulations',count(p.open),'Unpriced exits stay explicitly unknown','flask')}${metric('Completed simulations',count(p.closed),'Stored permanently','check')}${metric('Skipped signals',count(p.skipped),'Unavailable quotes and capacity included','clock')}</div>`+
 `<div class="notice"><strong>Current order budget: ${money(d.settings.budget)} including entry fees.</strong> Applies to future signals. Up to 25 open simulations; exits follow a confirmed source exit, disqualification, or 24 hours. This measures this copy rule, not every trader’s full strategy.</div><section class="panel"><div class="table-scroll"><table><thead><tr><th>Detected</th><th>Source / market</th><th>Status</th><th>Budget</th><th>Net PnL</th><th>Observation</th></tr></thead><tbody>${d.paper_records.map(r=>`<tr><td title="${esc(dateTime(r.detected_at))}">${esc(ago(r.detected_at))}</td><td class="wrap"><button class="text-button" data-trader="${esc(r.wallet)}">${esc(nameFor(r.wallet))}</button><div>${esc(r.title||r.condition)}</div><span class="subvalue">${esc(r.outcome||'')}</span></td><td>${badge(r.status)}</td><td>${money(r.budget)}</td><td class="num ${signClass(r.pnl)}">${signed(r.pnl)}<span class="subvalue">${r.status==='open'?'Executable mark':r.status==='closed'?'Closed result':'No fill'}</span></td><td class="wrap muted">${esc(r.mark_warning||r.reason||(r.status==='open'?'Monitoring executable exit depth':'Awaiting a fresh quote'))}</td></tr>`).join('')}</tbody></table></div>${d.paper_records.length?'':empty('The next signal starts the experiment.','New qualifying BUY alerts create prospective simulations. A usable quote must arrive after detection; historical prices cannot create a backfilled win.','flask')}<div class="table-footer"><span>Latest 100 signals shown · totals include all retained history</span><span>Estimated fills · no wallet connected</span></div></section>`;
}
const archiveLabels={trades:'Tracked trades',alerts:'Trade alerts',changes:'Position changes',paper:'Paper simulations',accounts:'Local account observations',history:'Imported PnL history',journal:'Position, profile & market journal'};
function archiveUrl(base='/api/archive') {const params=new URLSearchParams({kind:state.archiveKind});if(state.archiveWallet)params.set('wallet',state.archiveWallet);if(base==='/api/archive'){params.set('offset',state.archiveOffset);params.set('limit',50);}return `${base}?${params}`;}
function archivePage() {
 const a=state.archive; const rows=a?.records||[];
 return head('Nothing useful gets forgotten.','Permanent research history, including losses, skips, and excluded sources.',`<a class="button primary" href="${esc(archiveUrl('/api/export'))}">${icon('download')}Export complete selection</a>`)+
 `<form id="archive-filter" class="filters"><label for="archive-kind">Dataset</label><select id="archive-kind">${Object.entries(archiveLabels).map(([key,label])=>`<option value="${key}" ${key===state.archiveKind?'selected':''}>${label} (${count(state.data.storage.counts[key])})</option>`).join('')}</select><label class="visually-hidden" for="archive-wallet">Filter by wallet address</label><input id="archive-wallet" value="${esc(state.archiveWallet)}" placeholder="Wallet address (optional)" autocomplete="off"><button class="button" type="submit">${icon('filter')}Apply filter</button></form>`+
 `<section class="panel"><div class="table-scroll"><table><thead><tr><th>Recorded / observed</th><th>Wallet</th><th>Record</th></tr></thead><tbody>${rows.map(r=>{const m=r.meta,raw=r.data,at=m.recorded_at??m.detected_at??m.timestamp??m.observed_at;return `<tr><td>${esc(dateTime(at))}${m.observed_at?`<span class="subvalue">Source ${esc(dateTime(m.observed_at))}</span>`:''}</td><td class="mono">${esc(short(m.wallet))}</td><td class="archive-record"><strong>${esc(raw.title||raw.message||m.kind||raw.status||archiveLabels[state.archiveKind])}</strong><span class="subvalue">${esc(raw.outcome||'')} ${esc(raw.side||'')} ${raw.account_pnl!=null?` · account ${money(raw.account_pnl)}`:''}</span><details><summary>View original record</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></td></tr>`;}).join('')}</tbody></table></div>${rows.length?'':empty(a?'No records in this selection.':'Loading the archive…',a?'Try another dataset or wallet. Earlier history removed by previous retention rules cannot be recovered.':'Reading a page of records from SQLite.','archive')}<div class="table-footer"><span>${a?`${count(a.total)} records · ${count(a.total?state.archiveOffset+1:0)}–${count(Math.min(state.archiveOffset+50,a.total))}`:'Loading…'}</span><div class="pagination"><button class="button" data-archive-prev ${state.archiveOffset===0?'disabled':''}>Previous</button><button class="button" data-archive-next ${!a||state.archiveOffset+50>=a.total?'disabled':''}>Next</button></div></div></section><div class="notice">Snapshots are journaled when their economic state changes, plus one unchanged daily baseline. Identical polling responses are deduplicated. Untracked global-feed rows are a temporary 15-minute aggregation buffer. Exports include complete matching research history as newline-delimited JSON.</div>`;
}
function systemPage() {
 const d=state.data;const bytes=d.storage.database_bytes;const mb=(bytes/1024/1024).toFixed(1);
 return head('A small node. A lasting record.','Scanner health, local storage, and the settings behind your research.')+
 `<div class="page-grid"><section class="panel card-body"><h2>Research node</h2><div class="system-row"><span>Process uptime</span><strong>${esc(ago(d.started_at).replace(' ago',''))}</strong></div><div class="system-row"><span>Research started</span><strong>${date(d.storage.started_at)}</strong></div><div class="system-row"><span>SQLite database</span><strong>${mb} MiB + WAL</strong></div><div class="system-row"><span>Research retention</span><strong>Permanent</strong></div><div class="system-row"><span>Trade / position threshold</span><strong>${money(d.settings.threshold)}</strong></div><div class="system-row"><span>Feed poll target</span><strong>${d.settings.interval}s</strong></div><div class="system-row"><span>Pending / excluded accounts</span><strong>${d.stages.pending} / ${d.stages.excluded}</strong></div><p class="muted intro">The same process scans and serves this dashboard. Closing the browser leaves collection running. Keep the service running to build local evidence.</p></section><section class="panel card-body"><h2>Paper simulation settings</h2><form id="budget-form"><div class="form-field"><label for="paper-budget">Budget per future signal · USD</label><div class="form-inline"><input class="form-input" id="paper-budget" type="number" min="1" max="1000000" step="0.01" value="${d.settings.budget}" required><button class="button primary" type="submit">Save budget</button></div><small>Includes estimated entry fees. Existing simulations retain their original budgets.</small></div></form><h2 class="detail-section">Add a trader to your shortlist</h2><form id="wallet-form"><div class="form-field"><label for="new-wallet">Public wallet address</label><input class="form-input mono" id="new-wallet" placeholder="0x…" pattern="0x[a-fA-F0-9]{40}" required autocomplete="off"><small>The scanner verifies this account before including it in active research. Existing disqualifications remain in force.</small></div><button type="submit" class="button">${icon('plus')}Track this trader</button></form></section><section class="panel card-body"><h2>Feed health</h2>${Object.values(d.feeds).map(f=>`<div class="detail-section"><h3>${esc(f.name)}</h3><div class="system-row"><span>Last successful request</span><strong>${esc(ago(f.last_ok))}</strong></div><div class="system-row"><span>Newest observed trade</span><strong>${esc(ago(f.latest))}</strong></div><div class="system-row"><span>Catch-up / pages read</span><strong>${f.backlog?'Catching up':'At feed head'} · ${count(f.pages)}</strong></div>${f.error?`<p class="feed-error">${esc(f.error)}</p>`:''}</div>`).join('')}</section><section class="panel card-body"><h2>Verification & diagnostics</h2><div class="system-row"><span>Last position verification</span><strong>${esc(ago(d.verification.last_ok))}</strong></div><div class="system-row"><span>Pending position jobs</span><strong>${count(d.verification.pending)}</strong></div>${[d.verification.error,d.verification.market_error,...d.notices].filter(Boolean).map(n=>`<p class="feed-error">${esc(n)}</p>`).join('')||'<p class="intro muted">No scanner errors reported.</p>'}<div class="notice">A successful request confirms connectivity. The newest trade and source PnL timestamps show how fresh the upstream data actually is.</div></section></div>`;
}
function render(force=false) {
 if(!state.data)return;
 const focused=document.activeElement;
 if(!force&&focused&&$('content').contains(focused)&&['INPUT','SELECT','TEXTAREA'].includes(focused.tagName))return;
 const focusId=focused?.id;const selection=focused?.tagName==='INPUT'&&focused.type==='text'?[focused.selectionStart,focused.selectionEnd]:null;
 const focusKey=focused&&$('content').contains(focused)?['data-trader','data-save','data-filter','data-sort'].find(key=>focused.hasAttribute(key)):null;
 const focusValue=focusKey?focused.getAttribute(focusKey):null;
 const scrolls=[...$('content').querySelectorAll('.table-scroll,.tabs')].map(el=>el.scrollLeft);
 const html={traders:tradersPage,markets:marketsPage,activity:activityPage,paper:paperPage,archive:archivePage,system:systemPage}[state.page]();
 $('content').innerHTML=html;
 for(const [id,value] of Object.entries(state.drafts)){if($(id))$(id).value=value;}
 [...$('content').querySelectorAll('.table-scroll,.tabs')].forEach((el,i)=>{el.scrollLeft=scrolls[i]||0;});
 if(focusId&&$(focusId)&&$('content').contains($(focusId))){$(focusId).focus({preventScroll:true});if(selection)$(focusId).setSelectionRange(...selection);}
 else if(focusKey){[...$('content').querySelectorAll(`[${focusKey}]`)].find(el=>el.getAttribute(focusKey)===focusValue)?.focus({preventScroll:true});}
}
function navigate(page) {
 if(!names[page])page='traders';const changed=state.page!==page;
 state.page=page;if(changed)state.search='';
 document.querySelectorAll('[data-page].nav-item').forEach(el=>{el.classList.toggle('active',el.dataset.page===page);if(el.dataset.page===page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');el.title=names[el.dataset.page];el.setAttribute('aria-label',names[el.dataset.page]);});
 $('crumb').textContent=names[page];document.title=`Polywatch · ${names[page]}`;
 if(location.hash!==`#${page}`)history.replaceState(null,'',`#${page}`);
 render(true);if(page==='archive')loadArchive();if(changed)window.scrollTo({top:0});
}
async function refresh(manual=false) {
 if(state.fetching)return;state.fetching=true;
 try {
  state.data=await api('/api/summary');
  $('connection-error').hidden=true;
  const ok=state.data.connected;
  $('connection').innerHTML=`<i class="dot ${ok?'':'amber'}"></i>${ok?'Scanner connected':'Scanner warming / delayed'}`;
  $('node-state').textContent=ok?'Collecting research':'Checking upstream data';$('node-dot').className=`dot ${ok?'':'amber'}`;
  $('nav-count').textContent=count(state.data.stages.building+state.data.stages.long_term);
  if(!state.chartWallet||!state.data.traders.some(r=>r.address===state.chartWallet)){state.chartWallet=state.data.traders.find(r=>['building','long_term'].includes(r.stage))?.address||'';state.chartDetail=null;}
  if(state.page!=='archive'||manual||!state.archive)render(manual);
  if(state.page==='traders'&&state.chartWallet)loadChart();
  if(manual&&state.page==='archive')loadArchive();
 } catch(error) {
  $('connection-error').textContent=`Connection interrupted. ${state.data?'Showing the last saved screen; values may be stale. ':''}${error.message} Use the refresh button to retry.`;$('connection-error').hidden=false;
  $('connection').innerHTML='<i class="dot red"></i>Disconnected';$('node-state').textContent='Dashboard disconnected';$('node-dot').className='dot red';
  if(!state.data)$('content').innerHTML=empty('Your research node is unavailable.','Check that the watcher service is running, then refresh this page.','server',`<button class="button" data-retry>${icon('refresh')}Retry connection</button>`);
 } finally {state.fetching=false;}
}
async function loadChart() {
 const address=state.chartWallet,request=++state.chartRequest;
 try{const detail=await api(`/api/trader/${encodeURIComponent(address)}`);if(request!==state.chartRequest||state.chartWallet!==address)return;state.chartDetail=detail;if(state.page==='traders')render();}catch{}
}
async function loadArchive() {
 const request=++state.archiveRequest;
 try{const result=await api(archiveUrl());if(request!==state.archiveRequest)return;state.archive=result;if(state.page==='archive')render();}catch(e){if(request===state.archiveRequest)toast(e.message);}
}
async function saveTrader(address,saved) {
 const result=await api('/api/shortlist',{wallet:address,saved});
 if(state.data)state.data.settings.shortlist=result.shortlist;
 render(true);toast(saved?'Trader saved. Evidence will keep collecting.':'Trader removed from shortlist.');
 if($('detail').open&&state.detailWallet===address)openTrader(address,true);
 await refresh();
}
function detailHTML(r) {
 const saved=state.data.settings.shortlist.includes(r.address);
 return `<div class="dialog-header"><div><div class="eyebrow">TRADER RESEARCH PROFILE</div><h2 id="detail-title">${esc(r.name)}</h2><p class="mono muted">${esc(r.address)}</p>${badge(r.stage)}</div><button class="icon-button" data-close="detail" aria-label="Close trader profile">✕</button></div><div class="dialog-body"><div class="dialog-actions"><button class="button ${saved?'':'primary'}" data-save="${esc(r.address)}">${saved?'★ Saved to shortlist':'☆ Add to shortlist'}</button><a class="button" href="https://polymarket.com/profile/${encodeURIComponent(r.address)}" target="_blank" rel="noopener noreferrer">View on Polymarket ${icon('external')}</a><button class="button" data-copy="${esc(r.address)}">Copy address</button></div><div class="stat-list"><div><small>OVERALL ACCOUNT PNL</small><strong class="${signClass(r.account_pnl)}">${signed(r.account_pnl)}</strong></div><div><small>MONTHLY MARKED PNL · API</small><strong class="${signClass(r.month_pnl)}">${signed(r.month_pnl)}</strong></div><div><small>LOCAL OBSERVED CHANGE · ${Math.floor(r.local.observation_days)} DAYS</small><strong class="${signClass(r.local.net_pnl)}">${signed(r.local.net_pnl)}</strong></div><div><small>DAILY-SAMPLED DRAWDOWN · USD</small><strong>${money(r.local.drawdown)}</strong></div></div><h3>Locally observed PnL change</h3><div class="detail-chart">${chartSVG(r.series,{id:'detail-fill'})}</div><p class="muted">${r.local.daily_samples} covered daily comparisons · ${r.local.active_days} active days · ${percent(r.local.positive_days)} positive active days. Coverage: ${date(r.local.first_at)} to ${date(r.local.last_at)}. Missing days stay missing; drawdown can understate intraday losses.</p><h3 class="detail-section">The long-term screen · ${r.passed} / ${r.checks.length}</h3><ul class="check-list">${r.checks.map(c=>`<li class="${c.pass?'passed':''}"><span class="check-symbol">${c.pass?'✓':'○'}</span>${esc(c.label)}</li>`).join('')}</ul><p class="muted">${esc(r.pnl_reason||'Awaiting account verification.')} Source observation ${esc(ago(r.pnl_observed_at))}; historical analysis ${esc(ago(r.updated))}.</p><h3 class="detail-section">Historical context · imported from API</h3><div class="stat-list"><div><small>CLOSED MARKETS / WIN RATE</small><strong>${r.closed_markets??'—'}${r.closed_complete===false?'+':''} / ${percent(r.win_rate)}</strong></div><div><small>ALL-TIME REALIZED · API BOARD</small><strong class="${signClass(r.all_pnl)}">${signed(r.all_pnl)}</strong></div></div>${r.backfill.length>1?`<div class="detail-chart">${chartSVG(r.backfill,{id:'import-fill'})}</div>`:'<p class="muted">No historical PnL series has been imported yet.</p>'}<p class="muted">Imported history is separate from observations made by this node. It never extends the local observation clock.</p><h3 class="detail-section">Prospective copy results</h3><div class="stat-list"><div><small>NET PRICED CLOSES · ESTIMATED FEES INCLUDED</small><strong class="${signClass(r.paper.pnl)}">${signed(r.paper.pnl)}</strong></div><div><small>CLOSED / PRICED / SKIPPED</small><strong>${r.paper.closed} / ${r.paper.priced} / ${r.paper.skipped}</strong></div></div><p class="muted">${r.paper.open} open · ${r.paper.queued} queued · ${Math.floor(r.paper.days)} days between first closed signal detection and latest close. All archived results count, including losing trades. This is the app’s copy rule, not the source’s full strategy.</p>${r.paper_records.length?`<div class="table-scroll"><table><thead><tr><th>Simulation</th><th>Status</th><th>Net PnL</th></tr></thead><tbody>${r.paper_records.slice(0,15).map(p=>`<tr><td class="wrap">${esc(p.title||p.condition)}<span class="subvalue">${esc(p.reason||p.mark_warning||date(p.detected_at))}</span></td><td>${badge(p.status)}</td><td class="${signClass(p.pnl)}">${signed(p.pnl)}</td></tr>`).join('')}</tbody></table></div>`:''}<div class="notice">${r.audit_only?'This source is excluded. Historical evidence remains available for audit; no new dedicated source polling or copy signals are generated.':'A passing screen identifies evidence to investigate. It does not establish that future copies will be profitable.'}</div></div>`;
}
async function openTrader(address,update=false) {
 if(!validAddress(address))return;
 state.detailWallet=address;
 if(!update){$('detail-content').innerHTML=`<div class="dialog-header"><h2 id="detail-title">Loading trader research…</h2><button class="icon-button" data-close="detail" aria-label="Close profile">✕</button></div><div class="dialog-body"><p class="intro">Reading recorded evidence.</p></div>`;if(!$('detail').open)$('detail').showModal();}
 try{const r=await api(`/api/trader/${encodeURIComponent(address)}`);if(state.detailWallet===address&&$('detail').open){const y=$('detail').scrollTop;$('detail-content').innerHTML=detailHTML(r);if(update)$('detail').scrollTop=y;else $('detail').scrollTop=0;}}
 catch(e){if(state.detailWallet===address)$('detail-content').innerHTML=`<div class="dialog-header"><h2 id="detail-title">Research unavailable</h2><button class="icon-button" data-close="detail" aria-label="Close profile">✕</button></div><div class="dialog-body"><p>${esc(e.message)}</p></div>`;}
}

document.addEventListener('click',async event=>{
 const el=event.target.closest('button,a');if(!el)return;
 try{
  if(el.dataset.page){navigate(el.dataset.page);return;}
  if(el.hasAttribute('data-method')){$('methodology').showModal();return;}
  if(el.dataset.close){$(el.dataset.close).close();return;}
  if(el.dataset.trader){await openTrader(el.dataset.trader);return;}
  if(el.dataset.save){el.disabled=true;try{await saveTrader(el.dataset.save,!state.data.settings.shortlist.includes(el.dataset.save));}finally{el.disabled=false;}return;}
  if(el.dataset.copy){try{await navigator.clipboard.writeText(el.dataset.copy);toast('Wallet address copied.');}catch{toast('Copy the full address shown at the top of this profile.');}return;}
  if(el.dataset.filter){state.filter=el.dataset.filter;render(true);return;}
  if(el.dataset.sort){state.sort=el.dataset.sort;render(true);return;}
  if(el.hasAttribute('data-archive-prev')||el.hasAttribute('data-archive-next')){state.archiveOffset=Math.max(0,state.archiveOffset+(el.hasAttribute('data-archive-next')?50:-50));state.archive=null;render(true);await loadArchive();return;}
  if(el.id==='refresh'||el.hasAttribute('data-retry')){await refresh(true);return;}
 }catch(e){toast(e.message);}
});
document.addEventListener('input',event=>{
 if(['paper-budget','new-wallet','archive-wallet'].includes(event.target.id))state.drafts[event.target.id]=event.target.value;
 if(['trader-search','market-search','activity-search'].includes(event.target.id)){state.search=event.target.value;render(true);}
});
document.addEventListener('change',event=>{
 if(event.target.id==='chart-wallet'){state.chartWallet=event.target.value;state.chartDetail=null;render(true);loadChart();}
 if(event.target.id==='archive-kind'){state.archiveKind=event.target.value;state.archiveOffset=0;state.archive=null;render(true);loadArchive();}
});
document.addEventListener('submit',async event=>{
 const form=event.target;if(!['budget-form','wallet-form','archive-filter'].includes(form.id))return;event.preventDefault();
 const submit=form.querySelector('[type="submit"]');if(submit)submit.disabled=true;
 try{
  if(form.id==='budget-form'){await api('/api/settings',{budget:Number($('paper-budget').value)});delete state.drafts['paper-budget'];toast('Budget saved for future simulations.');await refresh(true);}
  if(form.id==='wallet-form'){const address=$('new-wallet').value.trim().toLowerCase();if(!validAddress(address))throw new Error('Enter a valid 0x-prefixed 40-digit wallet address.');await saveTrader(address,true);delete state.drafts['new-wallet'];if($('new-wallet'))$('new-wallet').value='';}
  if(form.id==='archive-filter'){const address=$('archive-wallet').value.trim().toLowerCase();if(address&&!validAddress(address))throw new Error('Enter a complete wallet address or leave the filter empty.');state.archiveWallet=address;state.archiveKind=$('archive-kind').value;state.archiveOffset=0;state.archive=null;render(true);await loadArchive();}
 }catch(e){toast(e.message);}finally{if(submit)submit.disabled=false;}
});
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
$('detail').addEventListener('close',()=>{state.detailWallet='';});
[$('detail'),$('methodology')].forEach(dialog=>{dialog.addEventListener('click',event=>{if(event.target!==dialog)return;const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();});});
fillIcons();$('today').textContent=new Date().toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'});
navigate(location.hash.slice(1)||'traders');refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
