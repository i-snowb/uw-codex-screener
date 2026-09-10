"""Local-only research explanation; never embedded in frozen publication data."""

SIGNAL_RESEARCH_JS = r'''
function signalResearchMarkup(report,target){
  const labels={trend:'Trend added to volatility',volatility:'Volatility added to trend',market_relative:'Market-relative added to price model'};
  const rows=report.comparisons.filter(row=>row.target===target);
  const format=value=>n(value)?(value>0?'+':'')+(value*10000).toFixed(3):'—';
  return `<div class="me-table-wrap"><table><thead><tr><th>Feature test</th><th>Horizon</th><th>Error reduction*</th><th>Direction change</th><th>Shared sample</th><th>Observed result</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(labels[row.ablation_group]||row.ablation_group)}</td><td>${esc(row.horizon)}D</td><td>${format(row.mae_improvement)}</td><td>${n(row.accuracy_lift)?pp(row.accuracy_lift*100):'—'}</td><td>${esc(row.matched_rows)} rows · ${esc(row.origin_dates)} dates</td><td>${!n(row.mae_improvement)?'Not tested':row.mae_improvement>0?'Lower error in development':row.mae_improvement<0?'Higher error in development':'No difference'}</td></tr>`).join('')}</tbody></table></div><p class="me-table-note">*Basis points of return; positive means lower mean absolute error. Date-weighted, identical test samples within each comparison. Direction uses paired nonneutral rows and can have a smaller sample. These are exploratory results, not proven accuracy gains or causal contributions.</p><p class="me-table-note"><b>Not tested:</b> ${report.untested.map(row=>esc(row.signal)+': '+esc(row.reason)).join(' ')}<br><b>What would strengthen the result:</b> fixed prospective testing, consistent later-period improvement, and verified data. No model weights changed.</p>`;
}
async function installSignalResearch(){
  const section=document.createElement('section');
  section.id='me-signal-research';section.className='me-system';
  section.setAttribute('aria-label','Offline signal contribution research');
  section.hidden=true;
  document.querySelector('.me-system').after(section);
  try{
    const metaResponse=await fetch('./data/signal-research-manifest.json',{cache:'no-store'});
    if(!metaResponse.ok)return;
    const meta=await metaResponse.json();
    if(!/^[a-f0-9]{64}$/.test(meta.sha256))throw new Error('Invalid research manifest');
    const response=await fetch('./data/signal-research.json',{cache:'no-store'});
    if(!response.ok)throw new Error('Research unavailable');
    const bytes=await response.arrayBuffer();
    if(bytes.byteLength>100000)throw new Error('Research summary is too large');
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
    if(hash!==meta.sha256)throw new Error('Research changed during load');
    const report=JSON.parse(new TextDecoder().decode(bytes));
    if(report.schema_version!=='signal-contribution-v1'||report.promotion_eligible!==false||!Array.isArray(report.comparisons)||report.comparisons.length>30||!Array.isArray(report.untested)||report.holdout?.status!=='SEALED_NOT_SCORED')throw new Error('Invalid research summary');
    const availableAt=Date.parse(report.reviewed_at),captureAt=Date.parse(report.capture_cutoff);
    if(!Number.isFinite(availableAt)||!Number.isFinite(captureAt))throw new Error('Missing research timestamps');
    section.innerHTML=`<div class="me-scope-head"><span class="me-scope-name">SIGNAL RESEARCH · ALL TICKERS</span><span class="me-muted">offline · exploratory · no promotion</span></div><details><summary>Which signals help predict returns?</summary><div class="me-detail-body"><p>Stored inputs through ${esc(timeLabel(report.capture_cutoff))}. Reviewed ${esc(timeLabel(report.reviewed_at))}. Holdout: ${esc(report.holdout.origin_dates)} origin dates remain unscored. This is not the active forecast or a fresh test of previously unseen development data.</p><label>Research target <select id="me-research-target" aria-label="Signal research target"><option value="absolute_return">Stock return</option><option value="excess_return">Stock return minus ${esc(report.benchmark)}</option></select></label><div id="me-research-comparisons"></div></div></details>`;
    const render=()=>{section.querySelector('#me-research-comparisons').innerHTML=signalResearchMarkup(report,section.querySelector('select').value)};
    section.querySelector('select').addEventListener('change',render);render();
    const visibility=()=>{section.hidden=replayActive;};visibility();
    document.getElementById('me-replay')?.addEventListener('change',()=>setTimeout(visibility,0));
  }catch(error){section.hidden=false;section.textContent='Offline signal research unavailable; reload to retry. Forecast data are unaffected.';}
}
installSignalResearch();
'''
