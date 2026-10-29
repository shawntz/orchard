'use strict';
let discoverySelected=null, discoveryDocument=null, discoveryTimer=null;
function discoveryControls(){return `<section class="card"><h2>Search repository projects</h2><label for="discovery-query">Search phrase</label><input id="discovery-query" value="pupillometry"><label for="discovery-terms">Relevance phrases (one per line)</label><textarea id="discovery-terms">pupillometry\npupil diameter\nattention\nmemory\nworking memory\nemotion\ncognitive\ndecision\nlearning\narousal</textarea><label for="discovery-pages">Search pages per source (1–10)</label><input id="discovery-pages" type="number" value="1" min="1" max="10"><div class="row">${['osf','dryad','zenodo','github'].map(r=>`<label><input class="discovery-source" type="checkbox" value="${r}" checked> ${r}</label>`).join('')}</div><button data-discovery="start">Search &amp; read project documentation</button><p>Searches public metadata, then reads READMEs and looks for linked papers. Results are limited by the query, API availability, pages and traversal caps. No automatic exclusions.</p></section>`;}
window.discoveryView=async function(){
 const jobs=await api('discovery');
 const old=await api('repository-sweeps');
 let output=header('DATASET DISCOVERY','Repository sweep','Read documentation, find publications, inspect articles here and record your decisions.')+discoveryControls();
 for(const job of jobs){
  const result=job.results, projects=result?.projects||[];
  const decisions=projects.map(p=>p.workflow.events.find(e=>e.stage==='human'));
  output+=`<section class="card"><h2>${esc(JSON.parse(job.parameters).query)} ${badge(job.status)}</h2><p>${job.observed} project records examined · ${projects.length} unique projects with .edf/.asc filenames · ${decisions.filter(d=>d?.decision==='include').length} included · ${decisions.filter(d=>d?.decision==='exclude').length} excluded · ${decisions.filter(d=>!d||d.decision==='review').length} awaiting review</p><p>${esc(job.error||'')}</p>${result?JSON.parse(result.sweep.errors).map(e=>`<p class="danger">${esc(e.repository)}: ${esc(e.error)}</p>`).join(''):''}${projects.map(p=>`<p><button class="secondary" data-discovery="inspect" data-project="${esc(p.id)}">${esc(p.title||p.external_id)}</button> · ${esc(p.repository)} · ${p.files.length} file names</p>`).join('')}<p class="muted">Counts describe repository projects, not independent studies. Paper duplicates and study relationships require review. Exclusion reasons are in the decision history and export.</p></section>`;
 }
 if(old.length){const existing=await api('repository-sweeps/'+old[0].id);output+=`<section class="card"><h2>Previously found projects</h2><p>Read their documentation and search for associated articles without repeating the repository search.</p>${existing.projects.map(p=>`<p><button class="secondary" data-discovery="inspect" data-project="${esc(p.id)}">${esc(p.title||p.external_id)}</button></p>`).join('')}</section>`;}
 output+=`<section class="card"><h2>Export review record</h2><a href="/api/export">Download CSV tables and JSON</a><p>Includes search parameters, repository observations, documentation, proposed papers, evidence and every manual decision. It supports reporting your search; it does not establish exhaustive coverage or remove selection bias.</p></section><section id="discovery-detail"></section>`;
 clearTimeout(discoveryTimer);
 if(jobs.some(j=>['queued','searching','reading_documentation'].includes(j.status)))discoveryTimer=setTimeout(()=>{if(location.hash==='#repository-sweep'&&!discoverySelected)render();},6000);
 return output;
};
function discoveryTerms(){return (document.querySelector('#discovery-terms')?.value||'pupillometry\nattention\nmemory').split('\n').map(t=>t.trim()).filter(Boolean);}
async function showDiscovery(project,inspect=false){
 discoverySelected=project;
 const d=await api(inspect?'discovery/inspect':'discovery/project/'+project,inspect?{project_id:project,terms:discoveryTerms()}:undefined);
 document.querySelector('#discovery-detail').innerHTML=`<div class="card"><h2>${esc(d.project.title)}</h2><p>Repository: ${esc(d.project.repository)}. Proposed articles require your confirmation; title similarity alone is not proof.</p>${d.candidates.map(c=>`<p><strong>${esc(c.title)}</strong><br>${esc(c.doi)} · ${esc(c.origin)}<br><button data-discovery="retrieve" data-candidate="${esc(c.id)}">This is the paper — retrieve here</button></p>`).join('')||'<p>No publication identified yet. You can provide its URL or upload a PDF below.</p>'}<label for="discovery-url">Article or PDF URL</label><input id="discovery-url" type="url"><button data-discovery="url">Retrieve article here</button><label for="discovery-upload">Or upload your authorized PDF (up to 8 MB)</label><input id="discovery-upload" type="file" accept="application/pdf"><button data-discovery="upload">Read uploaded PDF</button><h3>Saved documents</h3>${d.documents.map(doc=>`<p><button class="secondary" data-discovery="read" data-document="${esc(doc.id)}">Read ${esc(doc.kind)} ${esc(doc.title)}</button></p>`).join('')||'<p>No readable documents yet.</p>'}<section id="discovery-reader"></section><h3>Evidence and decisions</h3>${d.events.map(e=>`<details><summary>${esc(e.stage)} · ${badge(e.decision)} · ${date(e.created)}</summary><p>${esc(e.reason)}</p><pre>${esc(JSON.parse(e.evidence).join('\n\n'))}</pre></details>`).join('')}<label for="discovery-decision">Your decision</label><select id="discovery-decision"><option value="review">Needs review</option><option value="include">Include</option><option value="exclude">Exclude</option></select><label for="discovery-reason">Reason / evidence (required)</label><textarea id="discovery-reason"></textarea><button data-discovery="decide">Save decision</button></div>`;
 document.querySelector('#discovery-detail').scrollIntoView({behavior:'smooth'});
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-discovery]');if(!b)return;b.disabled=true;
 try{
  const action=b.dataset.discovery;
  if(action==='start'){
   await api('discovery/start',{query:document.querySelector('#discovery-query').value,terms:discoveryTerms(),max_pages:Number(document.querySelector('#discovery-pages').value),repositories:[...document.querySelectorAll('.discovery-source:checked')].map(e=>e.value)});
   discoverySelected=null;await render();toast('Search started. Progress updates here.');
  }else if(action==='inspect'){toast('Reading documentation and searching publication metadata…');await showDiscovery(b.dataset.project,true);}
  else if(action==='read'){
   const d=await api('reader/text/'+b.dataset.document);discoveryDocument=d.id;
   document.querySelector('#discovery-reader').innerHTML=`<h3>Article / document reader</h3><p>Source: ${esc(d.url)}</p>${d.pdf_path?`<iframe title="Article PDF" class="discovery-pdf" src="/api/reader/pdf/${esc(d.id)}"></iframe>`:''}<details ${d.pdf_path?'':'open'}><summary>Extracted text</summary><pre class="discovery-text">${esc(d.content||'No extractable text. Inspect the PDF above.')}</pre></details>`;
   document.querySelector('#discovery-reader').scrollIntoView({behavior:'smooth'});
  }else if(action==='decide'){
   await api('discovery/decision',{project_id:discoverySelected,decision:document.querySelector('#discovery-decision').value,reason:document.querySelector('#discovery-reason').value});await showDiscovery(discoverySelected);toast('Decision saved with its reason.');
  }else{
   let body={project_id:discoverySelected,terms:discoveryTerms()};
   if(action==='retrieve')body.candidate_id=b.dataset.candidate;
   if(action==='url')body.url=document.querySelector('#discovery-url').value;
   if(action==='upload'){
    const file=document.querySelector('#discovery-upload').files[0];if(!file||file.size>8000000)throw new Error('Choose a PDF up to 8 MB.');
    body.upload=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});
   }
   toast('Retrieving article…');await api('discovery/retrieve',body);await showDiscovery(discoverySelected);toast('Retrieval finished. Select a saved document to read it here.');
  }
 }catch(err){toast(err.message,true);}finally{b.disabled=false;}
});
