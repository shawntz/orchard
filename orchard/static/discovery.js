'use strict';
// Repository-first discovery: search jobs, project records with an embedded reader, screening and reporting.
// Uses helpers defined in app.js (api, esc, badge, date, toast, header, empty, modal, bytes).
const D={project:null,timer:null,filters:{match:'with_files'},page:1,reader:null};
const SOURCE_LABELS={osf:'OSF',zenodo:'Zenodo',dryad:'Dryad',github:'GitHub'};
const MATCH_LABELS={eyelink_confirmed:'EyeLink header confirmed',candidate_files:'.edf/.asc names (header not confirmed)',no_study_recordings:'.edf/.asc but no study recordings',no_matching_files:'No .edf/.asc files',no_matching_files_incomplete:'No .edf/.asc in a partial listing',inventory_failed:'Listing failed',inspection_failed:'Inspection failed',not_inspected:'Not inspected yet'};
const REL_LABELS={likely_relevant:'Likely relevant',needs_review:'Needs review',possible_exclusion:'Possible exclusion',insufficient_information:'Insufficient information'};
const PUB_LABELS={confirmed:'Publication confirmed',proposed:'Candidates to review',none_identified:'No publication identified (reviewed)',not_found:'No candidate found',not_searched:'Not searched'};
const ART_LABELS={readable:'Article text available',pdf_only:'PDF without extractable text',inaccessible:'Article not accessible (unresolved)',not_attempted:'Retrieval not attempted',no_publication:'No publication linked',not_identified:'No publication identified'};
const DEC_LABELS={include:'Include',exclude:'Exclude',needs_review:'Needs review',awaiting_information:'Awaiting information',undecided:'Not decided'};
const ROLE_ORDER=['recording','recording_excluded_group','behavioral','code','documentation','processed','example_or_fixture','placeholder','not_eyelink','archive','other','unclassified'];
const reviewer=()=>localStorage.getItem('orchard-reviewer')||'';
const pill=(label,cls='')=>`<span class="pill ${cls}">${esc(label)}</span>`;
const relPill=x=>x?pill(REL_LABELS[x]||x,x==='likely_relevant'?'':x==='possible_exclusion'?'bad':'warn'):pill('Not assessed','warn');
const decPill=d=>{const x=(d&&d.decision)||'undecided';return pill(DEC_LABELS[x]||x,x==='include'?'':x==='exclude'?'bad':'warn');};
const matchPill=x=>pill(MATCH_LABELS[x]||x,x==='eyelink_confirmed'?'':/failed|no_/.test(x)?'bad':'warn');
const ext=url=>/^https?:\/\//i.test(url||'')?`<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`:esc(url||'');
const tabs=active=>`<div class="d-tabs">${[['repository-sweep','Search'],['repository-projects','Projects'],['repository-funnel','Funnel & export'],['repository-concepts','Screening concepts']].map(([h,l])=>`<a href="#${h}" class="${h===active?'active':''}">${l}</a>`).join('')}</div>`;

// ---------------------------------------------------------------- search and jobs
async function searchView(){
 const d=await api('discovery');
 const active=d.jobs.some(j=>['queued','running','inspecting'].includes(j.status));
 schedule(active?'repository-sweep':null);
 const c=d.counts;
 return header('REPOSITORY DISCOVERY','Find EyeLink data in public repositories.','Search OSF, Zenodo, Dryad and GitHub for projects with .edf/.asc files, read their documentation, link their publications and screen them.')+tabs('repository-sweep')+
 `<div class="stats">${[['Records examined',c.records_examined,'Search results across all strategies'],['Projects found',c.unique_projects_found,'Distinct repository projects'],['With .edf/.asc files',c.projects_with_matching_files,`${c.projects_header_confirmed} with EyeLink headers confirmed`],['Included',c.included_projects,`${c.reports_linked_to_included_projects} linked report(s)`]].map(([l,n,h])=>`<div class="stat"><small>${l}</small><b>${n}</b><small>${h}</small></div>`).join('')}</div>`+
 `<section class="card"><h2>New repository search</h2>
 <div class="d-form"><div><label>Sources</label><div class="row">${d.sources.map(x=>`<label class="check"><input type="checkbox" class="d-source" value="${x}" checked> ${SOURCE_LABELS[x]}</label>`).join('')}</div>
 <p class="muted"><small>Tokens: GitHub ${d.tokens.github?'configured':'<strong>not set</strong> (code search skipped)'} · Zenodo ${d.tokens.zenodo?'configured':'not set (25 results per page)'} · Dryad ${d.tokens.dryad?'configured':'not set (files listed, not downloadable)'}. See README → Configuration.</small></p>
 <label>File types</label><div class="row"><label class="check"><input type="checkbox" class="d-ext" value="asc" checked> .asc</label><label class="check"><input type="checkbox" class="d-ext" value="edf" checked> .edf</label></div></div>
 <div><label for="d-phrases">Metadata phrases (one per line)</label><textarea id="d-phrases" rows="5">${esc(d.default_phrases.join('\n'))}</textarea>
 <div class="row"><label>Pages per search <input id="d-pages" type="number" min="1" max="100" value="5"></label><label>Projects to inspect per run <input id="d-max" type="number" min="0" max="2000" value="100"></label></div>
 <label for="d-reviewer">Your name</label><input id="d-reviewer" value="${esc(reviewer())}"></div></div>
 <div class="row toolbar"><button data-d="start">Start search</button></div>
 <p class="muted"><small>File-index strategies (OSF file index, Zenodo <code>filetype:</code>, GitHub code search) look for file names or contents. Metadata strategies only match titles and descriptions; their projects count as having EyeLink files only after their full file listing is read. Each search records its exact query, page cursor, reported total and coverage limits. Long searches can be cancelled and resumed.</small></p></section>`+
 (d.jobs.length?d.jobs.map(jobCard).join(''):empty('No repository searches yet','Start one above. Nothing is downloaded during a search.'))+
 (d.legacy_sweeps.length?`<details class="card"><summary>Earlier prototype sweeps (${d.legacy_sweeps.length})</summary><p class="muted">Bounded title searches by the earlier version. Their projects are kept and can be re-inspected from the Projects tab.</p>${d.legacy_sweeps.map(x=>`<p>${esc(x.query)} · ${badge(x.status)} · ${date(x.started)}</p>`).join('')}</details>`:'');
}
function jobCard(j){
 const p=j.parameters||{},prog=j.progress||{};
 const legacy=j.version!==2;
 const status=Object.entries(j.project_status||{}).map(([k,n])=>`${n} ${esc((MATCH_LABELS[k]||k).toLowerCase())}`).join(' · ');
 return `<section class="card d-job" data-job="${esc(j.id)}"><div class="row between"><h2>${legacy?'Earlier prototype search':esc((p.sources||[]).map(x=>SOURCE_LABELS[x]).join(', '))} ${badge(j.status)}</h2><small>${date(j.created)}</small></div>
 ${legacy?`<p class="muted">Created by the earlier prototype; ${j.unique_projects} observation(s). It cannot be resumed.</p>`:`<p>${j.records_examined} records examined · ${j.unique_projects} unique projects${status?' · '+status:''}</p>
 ${prog.phase==='inspecting'?`<p><strong>Inspecting projects:</strong> ${prog.done} of ${prog.total}${prog.queued>prog.total?` (${prog.queued-prog.total} more queued for the next run)`:''}${prog.current?` — ${esc(prog.current)}`:''}</p><progress max="${prog.total||1}" value="${prog.done||0}"></progress>`:''}
 ${j.error?`<p class="notice">${esc(j.error)}</p>`:''}
 <div class="row">${['queued','running','inspecting'].includes(j.status)?`<button class="secondary small" data-d="cancel" data-id="${esc(j.id)}">Cancel</button>`:''}${['partial','failed','cancelled','interrupted'].includes(j.status)?`<button class="small" data-d="resume" data-id="${esc(j.id)}">Resume</button>`:''}<a class="button secondary small" href="#repository-projects">Review projects</a></div>
 <details><summary>Searches and coverage (${j.tasks.length})</summary><div class="table-wrap"><table><thead><tr><th>Search</th><th>Status</th><th>Pages</th><th>Records</th><th>Source total</th><th>Projects</th></tr></thead><tbody>${j.tasks.map(t=>`<tr><td><strong>${esc(t.label)}</strong><br><code>${esc(t.query)}</code><br><small>${esc(t.coverage)}</small>${t.error?`<br><small class="danger">${esc(t.error)}</small>`:''}</td><td>${badge(t.status)}</td><td>${t.pages}</td><td>${t.records}</td><td>${t.reported_total??'unknown'}</td><td>${t.projects}</td></tr>`).join('')}</tbody></table></div>
 <p class="muted"><small>Phrases: ${esc((p.phrases||[]).join('; '))} · file types: ${esc((p.extensions||[]).join(', '))} · pages per search: ${p.max_pages} · projects per run: ${p.max_projects}</small></p></details>`}</section>`;
}

// ---------------------------------------------------------------- project list
async function projectsView(){
 const f=D.filters;
 const d=await api('discovery/projects?'+new URLSearchParams({q:f.q||'',match:f.match||'',relevance:f.relevance||'',decision:f.decision||'',page:D.page}));
 const opt=(id,cur,map,all)=>`<select id="${id}"><option value="">${all}</option>${Object.entries(map).map(([k,v])=>`<option value="${k}" ${cur===k?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
 return header('REPOSITORY DISCOVERY','Repository projects','Each row is one repository project. Counts here are projects, not papers or independent studies.')+tabs('repository-projects')+
 `<div class="controls d-filters"><input id="dp-q" placeholder="Search titles, identifiers and descriptions" value="${esc(f.q||'')}">${opt('dp-match',f.match,{with_files:'Any .edf/.asc files',...MATCH_LABELS},'All projects')}${opt('dp-rel',f.relevance,REL_LABELS,'Any relevance')}${opt('dp-dec',f.decision,DEC_LABELS,'Any decision')}<button class="secondary" data-d="filter">Apply</button></div>
 <div class="card"><div class="row between"><h2>${d.total} project${d.total===1?'':'s'}</h2><small>50 per page</small></div>${d.items.length?`<div class="table-wrap"><table class="d-projects"><thead><tr><th>Project</th><th>Files</th><th>Relevance</th><th>Publication · article</th><th>Decision</th></tr></thead><tbody>${d.items.map(p=>`<tr><td><a href="#project/${p.id}"><strong>${esc(p.title||p.external_id)}</strong></a><br><small>${SOURCE_LABELS[p.repository]||esc(p.repository)} · ${esc(p.external_id)}${p.study?' · study: '+esc(p.study.label):''}</small></td><td>${matchPill(p.match_status)}<br><small>${p.eyelink_files??'?'} .edf/.asc · ${p.confirmed_files??0} confirmed</small></td><td>${relPill(p.relevance)}</td><td><small>${esc(PUB_LABELS[p.publication]||p.publication)}<br>${esc(ART_LABELS[p.article]||p.article)}</small></td><td>${decPill(p.decision)}${p.decision?`<br><small>v${p.decision.version} · ${esc(p.decision.reviewer)}</small>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('No projects match','Run a repository search, or change the filters.')}
 <div class="pager"><button class="secondary" data-d="page" data-step="-1">← Previous</button><span>Page ${d.page} of ${d.pages}</span><button class="secondary" data-d="page" data-step="1">Next →</button></div></div>`;
}

// ---------------------------------------------------------------- project record
async function projectView(id){
 const d=await api('discovery/project/'+id);
 D.project=d;D.reader=null;
 schedule(d.downloads.some(b=>['queued','running'].includes(b.status))?'project/'+id:null);
 return `<div class="topline"><a href="#repository-projects">← Repository projects</a><span>REPOSITORY PROJECT</span></div><div class="d-layout"><div id="d-left">${leftColumn(d)}</div><aside class="d-side"><section class="card d-reader" id="d-reader">${readerShell(d)}</section><section class="card" id="d-decision">${decisionPanel(d)}</section></aside></div>`;
}
function leftColumn(d){
 const p=d.project;
 return `<h1 class="paper-title">${esc(p.title||p.external_id)}</h1>
 <p class="row">${pill(SOURCE_LABELS[p.repository]||p.repository)} ${p.kind?pill(p.kind):''} <a class="button small" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Open on ${SOURCE_LABELS[p.repository]||'repository'} ↗</a> <button class="secondary small" data-d="inspect">Re-inspect</button></p>
 <p class="muted">${p.creators.length?esc(p.creators.join(', ')):'Contributors not listed'} · ${p.doi?'DOI '+esc(p.doi)+' (identifies this dataset/project) · ':''}Licence: ${esc(p.license||'not stated')} · Created ${esc((p.repo_created||'').slice(0,10)||'unknown')}</p>
 <div class="d-strip">${matchPill(p.match_status||'not_inspected')}${relPill(d.assessment&&d.assessment.status)}${pill(PUB_LABELS[d.publication_state]||d.publication_state,d.publication_state==='confirmed'?'':'warn')}${pill(ART_LABELS[d.article_status]||d.article_status,d.article_status==='readable'?'':'warn')}${decPill(d.decisions[0])}</div>
 ${p.inspection_steps.length?`<details class="d-steps"><summary>Inspection log · ${date(p.last_inspected)}</summary>${p.inspection_steps.map(x=>`<p><small>${esc(x.step)} · ${esc(x.status)} — ${esc(x.detail)}</small></p>`).join('')}</details>`:'<div class="notice">This project has not been inspected yet. Choose Re-inspect to list its files and read its documentation.</div>'}
 ${filesCard(d)}${docsCard(d)}${relevanceCard(d)}${publicationCard(d)}${checklistCard(d)}${studyCard(d)}${provenanceCard(d)}`;
}
function fileRow(f){
 const h=f.header_details||{};
 const info=[h.pupil?'PUPIL '+h.pupil:'',h.sample_rate_hz?h.sample_rate_hz+' Hz':'',h.eyes?h.eyes.join('+'):'',h.tracker||'',h.note||''].filter(Boolean).join(' · ');
 const avail={public:'Downloadable',auth_required:'Sign-in/token required',restricted:'Restricted',inside_archive:'Inside archive',lfs_object:'Git LFS object',unavailable:'No download URL'}[f.download]||f.download||'';
 const can=['public','lfs_object'].includes(f.download);
 return `<tr><td>${can?`<input type="checkbox" class="d-file" value="${esc(f.id)}" data-size="${f.size??''}" aria-label="Select ${esc(f.path)}">`:''}</td><td><span class="mono">${esc(f.path)}</span>${f.exclusion_group?`<br><small>Exclusion folder: ${esc(f.exclusion_group)}</small>`:''}<br><small>${esc(f.role_reason||'')}</small></td><td>${bytes(f.size)}</td><td>${f.header?badge(f.header):'<small>not read</small>'}${info?`<br><small>${esc(info)}</small>`:''}</td><td><small>${esc(avail)}</small>${f.url&&!f.in_archive?`<br><a class="link" href="${esc(f.url)}" target="_blank" rel="noopener noreferrer"><small>source ↗</small></a>`:''}</td></tr>`;
}
function filesCard(d){
 const groups={};d.files.forEach(f=>(groups[f.role||'unclassified']??=[]).push(f));
 const order=ROLE_ORDER.filter(r=>groups[r]);
 const total=d.files.reduce((n,f)=>n+(f.in_archive?0:(f.size||0)),0);
 return `<section class="card" id="d-files"><div class="row between"><h2>Files (${d.files.length})</h2><small>${bytes(total)} listed</small></div>
 <p class="muted"><small>Roles come from paths, sizes and header reads, and each shows its reason. Only a sample of recordings has its header read. Files in exclusion folders (participants left out of the original analysis) are listed separately, never mixed into the analysed sample.${d.stale_files?` ${d.stale_files} file(s) from an earlier listing are no longer listed.`:''}</small></p>
 ${order.length?order.map(role=>`<details ${['recording','recording_excluded_group'].includes(role)?'open':''} class="d-group"><summary><strong>${esc(d.role_labels[role]||role)}</strong> (${groups[role].length}) <label class="check d-select-group"><input type="checkbox" class="d-group-check" data-role="${role}"> select downloadable</label></summary><div class="table-wrap"><table class="d-files" data-role="${role}"><thead><tr><th></th><th>Path</th><th>Size</th><th>Header</th><th>Access</th></tr></thead><tbody>${groups[role].slice(0,400).map(fileRow).join('')}</tbody></table>${groups[role].length>400?`<p><small>First 400 of ${groups[role].length}; the export lists all.</small></p>`:''}</div></details>`).join(''):'<p class="muted">No files listed.</p>'}
 <div class="row toolbar"><span id="d-selection" class="muted">No files selected</span><button data-d="plan-download">Review download…</button></div>
 ${d.downloads.length?`<h3>Downloads</h3>${d.downloads.map(b=>`<div class="history-item"><div class="row between"><strong>${b.files} file(s) · ${bytes(b.total_bytes)}</strong>${badge(b.status)}</div><small>Destination: <span class="mono">${esc(b.dest)}</span>${b.manifest_path?`<br>Manifest: <span class="mono">${esc(b.manifest_path)}</span>`:''}</small>${b.items.map(i=>`<p><small>${esc(i.path)} — ${badge(i.status)} ${bytes(i.bytes_done)} of ${bytes(i.expected_size)}${i.detail?' · '+esc(i.detail):''}</small></p>`).join('')}<div class="row">${['queued','running'].includes(b.status)?`<button class="secondary small" data-d="download-cancel" data-id="${esc(b.id)}">Cancel</button>`:''}${['partial','cancelled','interrupted'].includes(b.status)?`<button class="small" data-d="download-resume" data-id="${esc(b.id)}">Resume</button>`:''}</div></div>`).join('')}`:''}</section>`;
}
function docsCard(d){
 return `<section class="card"><h2>Documentation</h2>${d.documents.length?d.documents.map(x=>`<div class="history-item row between"><span><strong>${esc(x.title||x.kind)}</strong> ${badge(x.status||'unknown')}<br><small>${esc(x.kind)} · ${x.words??0} words · ${esc(x.method||'')}${x.quality_reason?' · '+esc(x.quality_reason):''}</small>${x.url?`<br><small>${ext(x.url)}</small>`:''}</span>${x.status==='readable'||x.pdf_path?`<button class="secondary small" data-d="read-doc" data-id="${esc(x.id)}">Read here</button>`:''}</div>`).join(''):'<p class="muted">No documentation read yet.</p>'}</section>`;
}
function highlight(text,concepts){
 const words=Object.values(concepts||{}).flatMap(g=>Object.values(g).flat()).filter(Boolean).sort((a,b)=>b.length-a.length);
 let out=esc(text);
 for(const w of [...new Set(words)]){const e=esc(w).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');out=out.replace(new RegExp('(^|[^\\w>])('+e+')(?![\\w<])','g'),'$1<mark>$2</mark>');}
 return out;
}
function relevanceCard(d){
 const a=d.assessment;
 if(!a)return `<section class="card"><h2>Relevant context</h2><p class="muted">Not assessed yet.</p><button class="secondary small" data-d="assess">Assess now</button></section>`;
 const kinds={pupil_and_task:'Pupil measure and task in one passage',pupil_and_general_task:'Pupil measure with general task wording',exclusion:'Possible exclusion',pupil_only:'Pupil measure only',task_only:'Task only',acquisition:'Eye-tracking acquisition'};
 return `<section class="card"><div class="row between"><h2>Relevant context</h2>${relPill(a.status)}</div><p>${esc(a.reason)}</p>${(a.notes||[]).map(n=>`<p class="muted"><small>${esc(n)}</small></p>`).join('')}
 ${a.evidence.slice(0,12).map((e,i)=>`<div class="d-passage ${e.kind}"><small><strong>${esc(kinds[e.kind]||e.kind)}</strong> · ${esc(e.source_title||e.source_kind)}</small><p>${highlight(e.passage,e.concepts)}</p><label class="check"><input type="checkbox" class="d-cite" value="${i}"> cite in decision</label></div>`).join('')}
 ${a.evidence.length>12?`<details><summary>${a.evidence.length-12} more passage(s)</summary>${a.evidence.slice(12).map(e=>`<div class="d-passage ${e.kind}"><small>${esc(kinds[e.kind]||e.kind)}</small><p>${highlight(e.passage,e.concepts)}</p></div>`).join('')}</details>`:''}
 <details><summary>Method, sources and reproducibility</summary><p><small>Rule-based passage matching (${esc(a.reproducibility.method)} v${esc(a.reproducibility.method_version)}); no machine-learning or semantic model. Same text and concept version give the same result. <a href="#repository-concepts">Edit the concept lists</a>.</small></p><pre>${esc(JSON.stringify({sources:a.sources,counts:a.counts,reproducibility:a.reproducibility,assessed:a.created},null,2))}</pre></details>
 <button class="secondary small" data-d="assess">Re-assess</button></section>`;
}
function paperBlock(paper){
 const link=paper.link;
 return `<div class="d-paper ${link.status}"><div class="row between"><strong><a href="#paper/${esc(paper.id)}">${esc(paper.title||'Untitled publication')}</a></strong>${badge(link.status)}</div><small>${esc(paper.authors||'')} ${paper.year?'· '+esc(paper.year):''} ${paper.doi?'· '+ext('https://doi.org/'+paper.doi):''}</small>
 ${paper.other_projects.length?`<p><small>Also linked to: ${paper.other_projects.map(o=>`<a href="#project/${o.id}">${esc(o.title)}</a>`).join(', ')}</small></p>`:''}
 ${link.status==='confirmed'?`<div class="row toolbar"><button class="small" data-d="retrieve" data-id="${esc(paper.id)}">Retrieve article</button><label class="button secondary small">Upload PDF<input type="file" accept="application/pdf,.pdf" class="d-upload" data-id="${esc(paper.id)}" hidden></label><button class="secondary small" data-d="unlink" data-id="${esc(paper.id)}">Unlink</button></div>`:''}
 ${paper.pdfs.map(f=>`<p><button class="secondary small" data-d="read-pdf" data-id="${esc(f.id)}">Read PDF here</button> <small>${esc(f.name)} · ${bytes(f.size)} · ${esc(f.origin)}</small></p>`).join('')}
 ${paper.fulltexts.map(t=>`<p><button class="secondary small" data-d="read-text" data-id="${esc(t.id)}">Read text here</button> <small>${t.words} words · ${esc(t.origin)}</small></p>`).join('')}
 ${paper.attempts.length?`<details><summary>Retrieval attempts (${paper.attempts.length})</summary>${paper.attempts.map(a=>`<p><small>${badge(a.outcome)} ${esc(a.source)} ${a.url?'· '+ext(a.url):''}${a.reason?' — '+esc(a.reason):''} · ${date(a.created)}</small></p>`).join('')}<p class="muted"><small>Login pages, bot checks, JavaScript shells and abstract-only pages are recorded here and never stored as article text. Missing access is not a reason to exclude.</small></p></details>`:''}</div>`;
}
function publicationCard(d){
 const open=d.candidates.filter(c=>c.strength!=='legacy'&&!(c.decision&&c.decision.action==='confirm'));
 const legacy=d.candidates.filter(c=>c.strength==='legacy');
 const search=d.publication_search;
 const strength={explicit:'Explicit repository link',strong:'Same title and authors',moderate:'Shared authors and title words',weak:'Similar title only',mentioned:'Mentioned in documentation',manual:'Entered by reviewer'};
 return `<section class="card" id="d-publication"><div class="row between"><h2>Associated publication</h2>${pill(PUB_LABELS[d.publication_state]||d.publication_state,d.publication_state==='confirmed'?'':'warn')}</div>
 <p class="muted"><small>Repository links and DOIs are checked first. A search result is proposed only when it shares an author or a near-identical distinctive title; common words never count. Nothing is linked until you confirm it.</small></p>
 ${d.papers.map(paperBlock).join('')}
 ${open.map(c=>`<div class="d-candidate"><div class="row between"><strong>${esc(c.title||c.doi||c.url)}</strong>${pill(strength[c.strength]||c.strength,['explicit','strong'].includes(c.strength)?'':'warn')}</div><small>${esc(c.authors.join(', '))} ${c.year?'· '+esc(c.year):''} ${c.venue?'· '+esc(c.venue):''} ${c.doi?'· '+ext('https://doi.org/'+c.doi):c.url?'· '+ext(c.url):''} · ${esc(c.kind||'')}</small><ul>${c.evidence.map(e=>`<li><small>${esc(e)}</small></li>`).join('')}<li><small>Source: ${esc(c.origin)}</small></li></ul>${c.decision?`<p><small>${badge(c.decision.action)} ${esc(c.decision.reason||'')} — ${esc(c.decision.reviewer)}, ${date(c.decision.created)}</small></p>`:''}<div class="row"><button class="small" data-d="confirm" data-id="${esc(c.id)}">This is the publication</button><button class="secondary small" data-d="reject" data-id="${esc(c.id)}">Not this publication</button></div></div>`).join('')}
 ${!d.papers.length&&!open.length?`<p>No publication candidate${search?' passed the evidence checks':' yet'}.${d.publication_state==='none_identified'?' A reviewer recorded that no publication is identified; the dataset stays available for screening.':''}</p>`:''}
 <details><summary>Enter or correct the publication</summary><label>DOI</label><input id="d-man-doi" placeholder="10.xxxx/…"><label>…or title and URL</label><input id="d-man-title" placeholder="Title"><input id="d-man-url" placeholder="https://…"><label>Note</label><input id="d-man-reason" placeholder="Where you found it"><div class="row toolbar"><button class="small" data-d="manual">Link this publication</button></div></details>
 <details><summary>No publication identified</summary><label for="d-none-reason">What you checked</label><textarea id="d-none-reason" placeholder="e.g. README, OSF wiki, author's Google Scholar, preprint servers"></textarea><button class="secondary small" data-d="none">Record “no publication identified”</button></details>
 ${search?`<details><summary>Search record · ${date(search.created)}</summary>${search.searches.map(x=>`<p><small>${esc(x.source)}: ${x.error?'<span class="danger">'+esc(x.error)+'</span>':x.results+' result(s)'} · <span class="mono">${esc(x.url)}</span></small></p>`).join('')||'<p><small>No bibliographic search (an explicit link was found).</small></p>'}${search.rejected.length?`<p><strong><small>Rejected search results</small></strong></p>${search.rejected.map(r=>`<p><small>${esc(r.source)}: ${esc(r.title)} ${r.year?'('+esc(r.year)+')':''} — ${esc(r.reason)}</small></p>`).join('')}`:''}${search.non_publications.length?`<p><strong><small>Identifiers that are not papers</small></strong></p>${search.non_publications.map(n=>`<p><small>${esc(n.identifier)} — ${esc(n.note)}</small></p>`).join('')}`:''}</details>`:''}
 ${legacy.length?`<details><summary>Earlier suggestions by title similarity (${legacy.length})</summary><p class="muted"><small>Made by the earlier prototype from title words alone; they are not evidence.</small></p>${legacy.map(c=>`<p><small>${esc(c.title)} · ${esc(c.doi||'')} · ${esc(c.origin||'')}</small></p>`).join('')}</details>`:''}
 ${d.publication_history.length?`<details><summary>Publication decisions (${d.publication_history.length})</summary>${d.publication_history.map(h=>`<p><small>${badge(h.action)} ${esc(h.reason||'')} — ${esc(h.reviewer)}, ${date(h.created)}</small></p>`).join('')}</details>`:''}</section>`;
}
function checklistCard(d){
 return `<section class="card"><h2>Eyeris reuse: what is known</h2><p class="muted"><small>Observed = seen in file listings or header reads; documented = stated by the repository; unverified = not established. A .edf/.asc name alone does not make a dataset eyeris-compatible.</small></p><table><tbody>${d.checklist.map(c=>`<tr><td><strong>${esc(c.label)}</strong></td><td>${pill(c.status,c.status==='unverified'?'warn':'')}</td><td><small>${esc(c.detail)}</small></td></tr>`).join('')}</tbody></table></section>`;
}
function studyCard(d){
 return `<section class="card"><h2>Study and duplicates</h2><p class="muted"><small>Map projects to independent studies so that several projects of one study, or several papers about one study, are not counted twice.</small></p>
 <p>Study: ${d.study?`<strong>${esc(d.study.label)}</strong> <button class="secondary small" data-d="study-remove">Remove</button>`:'<em>not mapped</em>'}</p>
 <div class="row"><select id="d-study"><option value="">Choose an existing study…</option>${d.studies.map(s=>`<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('')}</select><button class="secondary small" data-d="study-assign">Assign</button></div>
 <div class="row"><input id="d-study-new" placeholder="New study label, e.g. Abbestee 2025 Exp. 1–2"><button class="secondary small" data-d="study-new">Create and assign</button></div>
 ${d.relations.length?`<h3>Possible duplicates</h3>${d.relations.map(r=>`<div class="history-item"><a href="#project/${esc(r.other.id)}">${esc(r.other.title)}</a> (${esc(SOURCE_LABELS[r.other.repository]||r.other.repository)}) · ${esc(r.basis)} · ${badge(r.status)}<div class="row"><button class="secondary small" data-d="relation" data-id="${esc(r.id)}" data-status="same_dataset">Same dataset</button><button class="secondary small" data-d="relation" data-id="${esc(r.id)}" data-status="related">Related</button><button class="secondary small" data-d="relation" data-id="${esc(r.id)}" data-status="distinct">Distinct</button></div></div>`).join('')}`:''}</section>`;
}
function provenanceCard(d){
 return `<details class="card"><summary><strong>How this project was found</strong> (${d.observations.length})</summary>${d.observations.map(o=>`<p><small>${date(o.created)} · ${esc(o.label||'earlier prototype search')}${o.matched_files&&o.matched_files!=='[]'?' · matched '+esc(JSON.parse(o.matched_files).slice(0,3).join(', ')):''}</small></p>`).join('')}${d.legacy_events.length?`<p><strong><small>Earlier prototype assessments</small></strong></p>${d.legacy_events.map(e=>`<p><small>${esc(e.stage)} · ${esc(e.decision)} · ${esc(e.reason)}</small></p>`).join('')}`:''}</details>`;
}

// ---------------------------------------------------------------- reader
function readerItems(d){
 const items=[];
 d.papers.filter(p=>p.link.status==='confirmed').forEach(p=>{p.pdfs.forEach(f=>items.push({kind:'pdf',id:f.id,label:'PDF · '+(p.title||f.name)}));p.fulltexts.forEach(t=>items.push({kind:'text',id:t.id,label:'Article text · '+t.words+' words'}));});
 d.documents.filter(x=>x.status==='readable').forEach(x=>items.push({kind:'doc',id:x.id,label:(x.title||x.kind)}));
 d.documents.filter(x=>x.pdf_path).forEach(x=>items.push({kind:'legacy-pdf',id:x.id,label:'Earlier PDF · '+(x.title||'')}));
 return items;
}
function readerShell(d){
 const items=readerItems(d);
 return `<div class="row between"><h2>Reader</h2><select id="d-reader-select" aria-label="Document to read">${items.length?items.map(i=>`<option value="${i.kind}:${esc(i.id)}">${esc(i.label)}</option>`).join(''):'<option value="">Nothing to read yet</option>'}</select></div><div id="d-reader-body">${items.length?'':'<p class="muted">Documentation and article text appear here once read. No article is linked yet; confirm a publication or upload a PDF you obtained through your library.</p>'}</div>`;
}
async function openReader(kind,id){
 const body=document.querySelector('#d-reader-body');if(!body)return;
 const sel=document.querySelector('#d-reader-select');if(sel)sel.value=kind+':'+id;
 D.reader=kind+':'+id;
 if(kind==='pdf'||kind==='legacy-pdf'){
  const src=kind==='pdf'?'/api/pdfs/'+encodeURIComponent(id):'/api/reader/pdf/'+encodeURIComponent(id);
  body.innerHTML=`<iframe class="d-pdf" title="Article PDF" src="${src}#view=FitH"></iframe><p><small><a href="${src}" target="_blank" rel="noopener">Open PDF in a new tab</a></small></p>`;
 }else{
  const x=kind==='text'?await api('fulltexts/'+id):await api('discovery/documents/'+id);
  body.innerHTML=`<p><small>${esc(x.origin||x.title||'')} ${x.url?'· '+ext(x.url):''}</small></p><pre class="d-text" tabindex="0">${esc(x.content||'')}</pre>`;
 }
}
function initReader(){
 const sel=document.querySelector('#d-reader-select');
 if(sel&&sel.value){const [k,...rest]=sel.value.split(':');openReader(k,rest.join(':')).catch(e=>toast(e.message,true));}
}

// ---------------------------------------------------------------- decision panel
function decisionPanel(d){
 const cur=d.decisions[0];
 return `<div class="row between"><h2>Screening decision</h2>${decPill(cur)}</div>
 ${d.criteria?`<details><summary>Eligibility criteria (version ${esc(d.criteria.id.slice(0,8))})</summary><p><small><strong>Include:</strong> ${esc(d.criteria.inclusion||'—')}</small></p><p><small><strong>Exclude:</strong> ${esc(d.criteria.exclusion||'—')}</small></p></details>`:'<div class="notice"><a href="#settings">Save eligibility criteria</a> before including or excluding.</div>'}
 <label for="d-dec">Decision</label><select id="d-dec">${Object.entries(DEC_LABELS).filter(([k])=>k!=='undecided').map(([k,v])=>`<option value="${k}" ${cur&&cur.decision===k?'selected':''}>${v}</option>`).join('')}</select>
 <div id="d-excl-wrap" ${cur&&cur.decision==='exclude'?'':'hidden'}><label for="d-excl">Exclusion reason</label><select id="d-excl">${d.exclusion_reasons.map(r=>`<option ${cur&&cur.exclusion_reason===r?'selected':''}>${esc(r)}</option>`).join('')}</select></div>
 <label for="d-reason">Reason and evidence</label><textarea id="d-reason" placeholder="What you read and why">${''}</textarea>
 <p class="muted"><small>Tick passages under “Relevant context” to cite them. Use “Awaiting information” when the article or data cannot be accessed: that is not an exclusion.</small></p>
 <label for="d-rev">Reviewer</label><input id="d-rev" value="${esc(reviewer())}">
 <div class="row toolbar"><button data-d="decide">Save as version ${d.current_version+1}</button></div>
 <details ${d.decisions.length?'':'hidden'}><summary>History (${d.decisions.length} version${d.decisions.length===1?'':'s'})</summary>${d.decisions.map(x=>`<div class="history-item"><strong>v${x.version}</strong> ${decPill(x)} ${x.exclusion_reason?`<small>${esc(x.exclusion_reason)}</small>`:''}<p>${esc(x.reason)}</p>${(x.evidence||[]).map(e=>`<blockquote><small>${esc(typeof e==='string'?e:e.passage)}</small></blockquote>`).join('')}<small>${esc(x.reviewer)} · ${date(x.created)} · criteria ${esc(x.criteria_id?x.criteria_id.slice(0,8):'not recorded')}${x.criteria&&x.criteria.automated_assessment?' · automated: '+esc(x.criteria.automated_assessment.status):''}</small></div>`).join('')}</details>`;
}
function refreshProject(d){
 D.project=d;
 const left=document.querySelector('#d-left'),dec=document.querySelector('#d-decision');
 if(left)left.innerHTML=leftColumn(d);
 if(dec)dec.innerHTML=decisionPanel(d);
 const sel=document.querySelector('#d-reader-select');
 const items=readerItems(d);
 if(sel){const keep=D.reader;sel.innerHTML=items.length?items.map(i=>`<option value="${i.kind}:${esc(i.id)}">${esc(i.label)}</option>`).join(''):'<option value="">Nothing to read yet</option>';if(keep)sel.value=keep;}
 updateSelection();
}
function updateSelection(){
 const boxes=[...document.querySelectorAll('.d-file:checked')];const el=document.querySelector('#d-selection');
 if(el)el.textContent=boxes.length?`${boxes.length} file(s) selected · ${bytes(boxes.reduce((n,b)=>n+(Number(b.dataset.size)||0),0))}`:'No files selected';
}

// ---------------------------------------------------------------- funnel and concepts
async function funnelView(){
 const f=await api('discovery/funnel');const s=f.summary;
 const line=(label,n,note='')=>`<div class="d-funnel-row"><span>${label}</span><b>${n===null||n===undefined?'unknown':n}</b>${note?`<small>${note}</small>`:''}</div>`;
 const obj=o=>Object.entries(o).map(([k,v])=>`${esc((REL_LABELS[k]||PUB_LABELS[k]||ART_LABELS[k]||DEC_LABELS[k]||k).toString())}: ${v}`).join(' · ')||'—';
 return header('REPOSITORY DISCOVERY','Funnel and export','From records examined to screening decisions. Repository projects, reports (papers) and independent studies are counted separately.')+tabs('repository-funnel')+
 `<div class="card green"><div class="row between"><div><h2>Export the review record</h2><p>CSV tables and JSON for every search, observation, file, document, publication link, assessment, decision version and download manifest, plus PRISMA counts with their unknowns.</p></div><div class="row"><a class="button" href="/api/export">Download export ZIP</a><a class="button secondary" href="/api/export?format=prisma">PRISMA counts (.txt)</a></div></div></div>
 <section class="card"><h2>Identification</h2>${line('Records examined',s.records_examined,s.legacy_records_examined?`+${s.legacy_records_examined} from earlier prototype searches`:'')}${line('Unique repository projects found',s.unique_projects_found)}${line('Projects inspected (full file listing)',s.projects_inspected)}</section>
 <section class="card"><h2>Screening</h2>${line('Projects with .edf/.asc file names',s.projects_with_matching_files,'Including members of ZIP archives')}${line('… with study-recording candidates',s.projects_with_study_recording_candidates)}${line('… with EyeLink headers confirmed in a sample',s.projects_header_confirmed)}${line('… whose .edf/.asc files are not study recordings',s.projects_without_study_recordings,'Not EyeLink, examples/tests or placeholders')}${line('Projects with readable documentation',s.projects_with_documentation)}${line('Automated relevant-context assessment','',obj(s.relevance))}${line('Publication status','',obj(s.publication))}${line('Article text','',obj(s.article_text))}${line('Human decisions','',obj(s.decisions))}${line('Exclusion reasons','',obj(s.exclusion_reasons))}${line('Confirmed duplicate projects',s.confirmed_duplicate_projects)}</section>
 <section class="card"><h2>Included</h2>${line('Repository projects',s.included_projects)}${line('Reports (papers) linked to them',s.reports_linked_to_included_projects)}${line('Independent studies',s.independent_studies,s.independent_studies===null?`unknown until every included project is mapped (mapped so far: ${s.independent_studies_mapped_so_far})`:'')}</section>
 <section class="card"><h2>Unknowns and coverage</h2>${f.unknowns.length?`<ul>${f.unknowns.map(u=>`<li>${esc(u)}</li>`).join('')}</ul>`:'<p>None recorded.</p>'}<p class="notice">${esc(f.bias_note)}</p>
 <details><summary>Every search (${f.searches.length})</summary><div class="table-wrap"><table><thead><tr><th>Search</th><th>Status</th><th>Records</th><th>Source total</th></tr></thead><tbody>${f.searches.map(x=>`<tr><td>${esc(x.source)} · ${esc(x.strategy)} · <code>${esc(x.query)}</code><br><small>${esc(x.coverage)}</small></td><td>${badge(x.status)}</td><td>${x.records_examined}</td><td>${x.reported_total??'unknown'}</td></tr>`).join('')}</tbody></table></div></details>
 <details><summary>Count definitions</summary>${Object.entries(f.count_definitions).map(([k,v])=>`<p><small><strong>${esc(k)}</strong>: ${esc(v)}</small></p>`).join('')}</details></section>`;
}
async function conceptsView(){
 const [c,docs]=await Promise.all([api('discovery/config'),api('documents')]);
 return header('REPOSITORY DISCOVERY','Screening concepts','The concept lists behind the automated relevant-context assessment. Every assessment records the version it used.')+tabs('repository-concepts')+
 `<div class="grid"><section class="card"><h2>Current concepts</h2><p class="muted"><small>${esc(c.origin)} · ${date(c.created)} · SHA-256 ${esc(c.sha256.slice(0,12))}…</small></p><textarea id="d-concepts" rows="24" class="mono">${esc(c.text)}</textarea><p class="muted"><small>Sections: [measurement], [acquisition], [tasks], [exclusion]. In tasks and exclusion, write “Group: term; term”. Variants are matched automatically (pupil size = pupil-size = pupil_size = pupilsize; plurals); end a term with * for prefixes.</small></p><button data-d="save-concepts">Save new version</button>
 ${docs.length?`<h3>Use task groups from an imported terms document</h3><div class="row"><select id="d-concept-doc">${docs.map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('')}</select><button class="secondary" data-d="concepts-from-doc">Build from document</button></div>`:''}</section>
 <section class="card"><h2>Method</h2><pre>${esc(c.method)}</pre><h3>Exclusion reasons offered</h3><ul>${c.exclusion_reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul><h3>Versions</h3>${c.history.map(h=>`<p><small>${date(h.created)} · ${esc(h.origin)} · ${esc(h.sha256.slice(0,12))}</small></p>`).join('')}</section></div>`;
}

// ---------------------------------------------------------------- routing, polling and events
window.discoveryRoutes={'repository-sweep':searchView,'repository-projects':projectsView,'repository-funnel':funnelView,'repository-concepts':conceptsView};
window.projectView=projectView;
window.afterDiscoveryRender=route=>{if(route.startsWith('project/')){initReader();updateSelection();}};
function schedule(route){
 clearTimeout(D.timer);
 if(!route)return;
 D.timer=setTimeout(async()=>{
  if(location.hash!=='#'+route||document.activeElement?.matches('input,textarea,select'))return schedule(route);
  if(route.startsWith('project/')){try{const d=await api('discovery/project/'+route.split('/')[1]);const files=document.querySelector('#d-files');if(files){const keep=[...document.querySelectorAll('.d-file:checked')].map(b=>b.value);files.outerHTML=filesCard(d);keep.forEach(v=>{const b=document.querySelector(`.d-file[value="${CSS.escape(v)}"]`);if(b)b.checked=true;});updateSelection();}schedule(d.downloads.some(b=>['queued','running'].includes(b.status))?route:null);}catch(e){}}
  else render();
 },3000);
}
async function project(path,body){const d=await api('discovery/'+path,body);if(d&&d.project)refreshProject(d);return d;}
function needReviewer(){const name=(document.querySelector('#d-rev')||{}).value||reviewer();if(!name.trim())throw new Error('Enter your name as reviewer (Screening decision panel).');localStorage.setItem('orchard-reviewer',name.trim());return name.trim();}
document.addEventListener('change',async e=>{
 if(e.target.id==='d-reader-select'&&e.target.value){const [k,...r]=e.target.value.split(':');openReader(k,r.join(':')).catch(x=>toast(x.message,true));}
 if(e.target.id==='d-dec'){const w=document.querySelector('#d-excl-wrap');if(w)w.hidden=e.target.value!=='exclude';}
 if(e.target.classList?.contains('d-group-check')){document.querySelectorAll(`table.d-files[data-role="${e.target.dataset.role}"] .d-file`).forEach(b=>b.checked=e.target.checked);updateSelection();}
 if(e.target.classList?.contains('d-file'))updateSelection();
 if(e.target.classList?.contains('d-upload')){
  const file=e.target.files[0];if(!file)return;
  try{if(file.size>60e6)throw new Error('PDFs are limited to 60 MB.');toast('Uploading PDF…');const r=await fetch(`/api/papers/${encodeURIComponent(e.target.dataset.id)}/pdf?`+new URLSearchParams({name:file.name}),{method:'POST',headers:{'Content-Type':'application/pdf'},body:file});const x=await r.json();if(!r.ok)throw new Error(x.error||'Upload failed');await project('assess',{project_id:D.project.project.id});openReader('pdf',x.pdf.id);toast(x.reused?'This PDF was already stored.':'PDF stored with its source and SHA-256; relevance re-assessed.');}catch(err){toast(err.message,true);}
 }
});
document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-d]');if(!b)return;
 const act=b.dataset.d,id=b.dataset.id,pid=D.project&&D.project.project.id;b.disabled=true;
 try{
  if(act==='start'){
   const name=document.querySelector('#d-reviewer').value.trim();if(name)localStorage.setItem('orchard-reviewer',name);
   await api('discovery/start',{sources:[...document.querySelectorAll('.d-source:checked')].map(x=>x.value),extensions:[...document.querySelectorAll('.d-ext:checked')].map(x=>x.value),phrases:document.querySelector('#d-phrases').value,max_pages:Number(document.querySelector('#d-pages').value),max_projects:Number(document.querySelector('#d-max').value),reviewer:name});
   toast('Search started. Progress updates here; you can leave this page.');await render();
  }
  else if(act==='cancel'){await api('discovery/cancel',{job_id:id});toast('Cancelling after the current request. Progress is kept.');await render();}
  else if(act==='resume'){await api('discovery/resume',{job_id:id});toast('Resuming from the saved cursors.');await render();}
  else if(act==='filter'){D.filters={q:document.querySelector('#dp-q').value,match:document.querySelector('#dp-match').value,relevance:document.querySelector('#dp-rel').value,decision:document.querySelector('#dp-dec').value};D.page=1;await render();}
  else if(act==='page'){D.page=Math.max(1,D.page+Number(b.dataset.step));await render();}
  else if(act==='inspect'){toast('Listing files, reading headers and documentation…');await project('inspect',{project_id:pid});toast('Inspection finished.');}
  else if(act==='assess'){await project('assess',{project_id:pid});toast('Relevant context re-assessed.');}
  else if(act==='read-doc'){await openReader('doc',id);document.querySelector('#d-reader').scrollIntoView({behavior:'smooth',block:'nearest'});}
  else if(act==='read-pdf'){await openReader('pdf',id);}
  else if(act==='read-text'){await openReader('text',id);}
  else if(act==='confirm'||act==='reject'){
   const reviewerName=needReviewer();let reason='';
   if(act==='reject'){reason=prompt('Why is this not the project’s publication?');if(reason===null)return;}
   else reason=prompt('Optional note: what confirms it (e.g. same authors and experiments)?')??'';
   await project('publication',{project_id:pid,action:act,candidate_id:id,reviewer:reviewerName,reason});toast(act==='confirm'?'Publication linked and added to the Paper library.':'Candidate rejected; the decision is recorded.');
  }
  else if(act==='manual'){
   const reviewerName=needReviewer();const body={project_id:pid,action:'manual',reviewer:reviewerName,doi:document.querySelector('#d-man-doi').value.trim()||null,title:document.querySelector('#d-man-title').value.trim()||null,url:document.querySelector('#d-man-url').value.trim()||null,reason:document.querySelector('#d-man-reason').value};
   try{await project('publication',body);}catch(err){if(err.status===400&&/not a paper/.test(err.message)&&confirm(err.message)){await project('publication',{...body,force:true});}else throw err;}
   toast('Publication linked.');
  }
  else if(act==='none'){await project('publication',{project_id:pid,action:'none_identified',reviewer:needReviewer(),reason:document.querySelector('#d-none-reason').value});toast('Recorded: no publication identified. The dataset stays in screening.');}
  else if(act==='unlink'){const reason=prompt('Why remove this link?');if(reason===null)return;await project('publication',{project_id:pid,action:'unlink',candidate_id:id,reviewer:needReviewer(),reason});toast('Link removed; the history is kept.');}
  else if(act==='retrieve'){toast('Trying PMC, open-access PDFs and the publisher page…');const d=await project('retrieve',{project_id:pid,paper_id:id});const r=d.retrieval;if(r&&r.pdf_id)openReader('pdf',r.pdf_id);else if(r){const t=d.papers.find(p=>p.id===id);if(t&&t.fulltexts[0])openReader('text',t.fulltexts[0].id);}toast(r?`Article retrieved (${r.format}${r.text_extracted?', text extracted':', no extractable text'}).`:'No readable article was retrieved. Each attempt and its reason is listed; upload a PDF if you have access.',!r);}
  else if(act==='decide'){
   const cites=[...document.querySelectorAll('.d-cite:checked')].map(c=>{const ev=D.project.assessment.evidence[Number(c.value)];return {kind:ev.kind,source:ev.source_title,passage:ev.passage};});
   const dec=document.querySelector('#d-dec').value;
   await project('decision',{project_id:pid,decision:dec,exclusion_reason:dec==='exclude'?document.querySelector('#d-excl').value:null,reason:document.querySelector('#d-reason').value,evidence:cites,reviewer:needReviewer(),version:D.project.current_version,criteria_id:D.project.criteria?.id});
   toast('Decision saved as a new version with its criteria.');
  }
  else if(act==='study-assign'){const sid=document.querySelector('#d-study').value;if(!sid)throw new Error('Choose a study.');await project('study',{project_id:pid,action:'assign',study_id:sid,reviewer:needReviewer()});}
  else if(act==='study-new'){await project('study',{project_id:pid,action:'new',label:document.querySelector('#d-study-new').value,reviewer:needReviewer()});toast('Study created and assigned.');}
  else if(act==='study-remove'){await project('study',{project_id:pid,action:'remove',reviewer:needReviewer()});}
  else if(act==='relation'){const reason=prompt('Note (optional)')??'';await api('discovery/relation',{relation_id:id,status:b.dataset.status,reviewer:needReviewer(),reason});refreshProject(await api('discovery/project/'+pid));}
  else if(act==='plan-download'){
   const ids=[...document.querySelectorAll('.d-file:checked')].map(x=>x.value);if(!ids.length)throw new Error('Select files first (tick them in the file groups).');
   const plan=await api('discovery/download-plan',{project_id:pid,file_ids:ids});D.plan=plan;
   modal(`<h2>Download ${plan.files.length} file(s)?</h2><p><strong>${bytes(plan.total_bytes)}</strong>${plan.unknown_sizes?` (+${plan.unknown_sizes} file(s) of unknown size)`:''} to<br><span class="mono">${esc(plan.destination)}</span></p>${plan.blocked.length?`<div class="notice">${plan.blocked.map(x=>esc(x.path)+': '+esc(x.reason)).join('<br>')}</div>`:''}<div class="table-wrap"><table><tbody>${plan.files.map(f=>`<tr><td class="mono">${esc(f.path)}</td><td>${bytes(f.size)}</td><td><small>${f.checksum?'checksum '+esc(f.checksum.split(':')[0]):'size check only'}</small></td></tr>`).join('')}</tbody></table></div>${plan.needs_large_confirmation?'<label class="check"><input type="checkbox" id="d-large"> I understand this is a large download.</label>':''}<p class="muted"><small>Files are verified against the listed size and repository checksum. Interrupted downloads resume where possible. A manifest is written next to the files.</small></p><button data-d="start-download">Start download</button>`);
  }
  else if(act==='start-download'){await api('discovery/download',{project_id:D.plan.project_id,file_ids:D.plan.files.map(f=>f.file_id),reviewer:reviewer(),confirm_large:!!document.querySelector('#d-large')?.checked});document.querySelector('#dialog').close();refreshProject(await api('discovery/project/'+D.plan.project_id));schedule('project/'+D.plan.project_id);toast('Download started.');}
  else if(act==='download-cancel'||act==='download-resume'){await api('discovery/download-action',{batch_id:id,action:act==='download-cancel'?'cancel':'resume'});refreshProject(await api('discovery/project/'+pid));schedule('project/'+pid);}
  else if(act==='save-concepts'){await api('discovery/config',{text:document.querySelector('#d-concepts').value});toast('New concept version saved. Re-assess projects to apply it.');await render();}
  else if(act==='concepts-from-doc'){await api('discovery/config',{document_id:document.querySelector('#d-concept-doc').value});toast('Task groups imported as a new concept version.');await render();}
 }catch(err){toast(err.message,true);}finally{b.disabled=false;}
});
