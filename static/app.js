"use strict";
const $ = (id) => document.getElementById(id);
const els = {};
for (const id of ["source","sourceChecklist","sourceDescription","sourceWarning","authFields","extraFields","xLegacySettings","xLegacyAuthFields","xLegacyExtraFields","pixivLegacySettings","pixivLegacyAuthFields","xSessionPanel","xCookieMode","openXSessionBtn","checkXSessionBtn","xSessionResult","xLegacyHint","pixivSessionPanel","pixivCookieMode","openPixivSessionBtn","checkPixivSessionBtn","pixivSessionResult","xOpenRow","openXBtn","xOpenResult","testSourceBtn","sourceTestResult","artistQuery","searchArtistBtn","artistCandidates","selectedArtist","sourceArtist","sourceArtistHint","canonicalArtist","canonicalArtistId","xUserId","count","rating","taggerType","tagMergeMode","llmFields","llmBaseUrl","llmApiKey","llmModel","llmPromptPreset","llmPrompt","onnxFields","onnxModelPath","onnxTagsPath","onnxThreshold","testTaggerBtn","taggerTestResult","includeArtist","includeMeta","skipVideo","proxy","startBtn","stopBtn","shutdownBtn","shutdownResult","statusPill","errorBox","identityLine","sourceStates","progressFill","progressText","statTotal","statDone","statSkipped","statFailed","folderLine","workspaceSubtitle","gallery","galleryCount","logBox","lightbox","lightboxMedia","lightboxTitle","lightboxMeta","lightboxOpen","lightboxPrev","lightboxNext","lightboxClose","tagDrawer","tagDrawerTitle","tagDrawerMeta","tagDrawerBody","tagDrawerClose","copyTagsBtn","queryType","queryTypeHint"]) els[id] = $(id);

const STORE_KEY = "artist_grabber_settings_v2";
const STATUS_TEXT = {idle:"空闲",pending:"等待中",testing:"连接测试中",preparing:"准备中…",running:"下载中",done:"已完成",skipped:"已跳过",stopped:"已停止",error:"出错"};
const VIDEO_EXTS = new Set(["mp4","webm","zip","gif"]);
const LLM_PROMPT_PRESETS = {
  general:"Analyze the image and produce concise Danbooru/WD14-style English tags for an image-training caption. Cover visible subject count, people or creatures, appearance, clothing, accessories, pose, expression, action, framing, background, lighting, colors, and visual medium/style. Prefer canonical lowercase underscore tags. Avoid redundant synonyms and do not guess artist, character identity, or copyright.",
  character:"Focus on clearly visible characters and their attributes using concise Danbooru/WD14-style English tags. Include subject count, apparent gender presentation, hair, eyes, expression, body pose, hand position, clothing layers, footwear, accessories, interaction, and camera-facing direction. Add only essential background/composition tags. Do not infer identity, artist, or copyright.",
  composition:"Focus on the whole scene and composition using concise Danbooru/WD14-style English tags. Include environment, foreground/background elements, action, viewpoint, shot type, camera angle, depth, lighting, weather, time of day, dominant colors, visual effects, and medium/style; still include essential visible subject and clothing tags. Do not infer identity, artist, or copyright."
};
let sources = [], currentSource = null, pollTimer = null, logCount = 0, itemCount = 0;
let selectedSourceIds = new Set(), sourceConfigCache = {};
let galleryItems = [], lightboxIndex = -1, activeTagItem = null;

async function postJSON(url, body) { const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}); return r.json(); }
function getTagFormat(){return document.querySelector('input[name="tagFormat"]:checked')?.value||"comma";}
function showError(msg){els.errorBox.textContent=msg;els.errorBox.classList.remove("hidden");}
function hideError(){els.errorBox.classList.add("hidden");}
function resultText(el, ok, msg){el.className="test-result "+(ok?"ok":"err");el.textContent=(ok?"✔ ":"✘ ")+msg;}
function escapeHtml(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

function saveSettings(){
  const mappings={};document.querySelectorAll("[data-source-map]").forEach(e=>mappings[e.dataset.sourceMap]=e.value.trim());
  const data={source:els.source.value,queryType:els.queryType.value,selectedSources:sources.filter(s=>selectedSourceIds.has(s.id)).map(s=>s.id),sourceMappings:mappings,xCookieMode:els.xCookieMode.value,pixivCookieMode:els.pixivCookieMode.value,artistQuery:els.artistQuery.value,sourceArtist:els.sourceArtist.value,canonicalArtist:els.canonicalArtist.value,canonicalArtistId:els.canonicalArtistId.value,xUserId:els.xUserId.value,count:els.count.value,proxy:els.proxy.value,taggerType:els.taggerType.value,tagMergeMode:els.tagMergeMode.value,llmBaseUrl:els.llmBaseUrl.value,llmModel:els.llmModel.value,llmPromptPreset:els.llmPromptPreset.value,llmPrompt:els.llmPrompt.value,onnxModelPath:els.onnxModelPath.value,onnxTagsPath:els.onnxTagsPath.value,onnxThreshold:els.onnxThreshold.value,tagFormat:getTagFormat(),includeArtist:els.includeArtist.checked,includeMeta:els.includeMeta.checked,skipVideo:els.skipVideo.checked};
  try{localStorage.setItem(STORE_KEY,JSON.stringify(data));}catch(_e){}
}
function loadSettings(){let d={};try{d=JSON.parse(localStorage.getItem(STORE_KEY)||"{}");}catch(_e){};return d;}

async function loadSources(){
  const data=await fetch("/api/sources").then(r=>r.json()); sources=data.sources||[];
  const saved=loadSettings(); els.source.innerHTML=sources.map(s=>`<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`).join("");
  els.source.value=saved.source&&sources.some(s=>s.id===saved.source)?saved.source:"twitter";
  const defaults=Array.isArray(saved.selectedSources)&&saved.selectedSources.length?saved.selectedSources:[els.source.value];
  selectedSourceIds=new Set(defaults.filter(id=>sources.some(s=>s.id===id)));
  els.sourceChecklist.innerHTML=sources.map(s=>`<div class="source-choice"><input type="checkbox" data-source-check="${escapeHtml(s.id)}" ${selectedSourceIds.has(s.id)?"checked":""}><span class="source-choice-name">${escapeHtml(s.label)}${s.needs_auth?"<small>需要登录，可跳过</small>":""}</span><input type="text" data-source-map="${escapeHtml(s.id)}" value="${escapeHtml(saved.sourceMappings?.[s.id]||"")}" placeholder="${s.id==="twitter"?"X handle":"来源内画师 tag"}" ${selectedSourceIds.has(s.id)?"":"disabled"}></div>`).join("");
  els.sourceChecklist.querySelectorAll("[data-source-check]").forEach(box=>box.addEventListener("change",()=>{const id=box.dataset.sourceCheck;if(box.checked)selectedSourceIds.add(id);else selectedSourceIds.delete(id);const input=els.sourceChecklist.querySelector(`[data-source-map="${id}"]`);if(input)input.disabled=!box.checked;saveSettings();}));
  els.sourceChecklist.querySelectorAll("[data-source-map]").forEach(input=>input.addEventListener("input",()=>{if(input.dataset.sourceMap===els.source.value)els.sourceArtist.value=input.value;saveSettings();}));
  renderSource(false); applySaved(saved);
}
function applySaved(d){
  for(const [key,id] of [["artistQuery","artistQuery"],["sourceArtist","sourceArtist"],["count","count"],["proxy","proxy"],["taggerType","taggerType"],["tagMergeMode","tagMergeMode"],["llmBaseUrl","llmBaseUrl"],["llmModel","llmModel"],["llmPromptPreset","llmPromptPreset"],["llmPrompt","llmPrompt"],["onnxModelPath","onnxModelPath"],["onnxTagsPath","onnxTagsPath"],["onnxThreshold","onnxThreshold"]]) if(d[key]!==undefined) els[id].value=d[key];
  if(d.queryType)els.queryType.value=d.queryType;renderQueryType();
  if(d.tagMergeMode===undefined) els.tagMergeMode.value=els.taggerType.value==="none"?"native_only":"tagger_only";
  if(!els.llmPrompt.value.trim()){els.llmPromptPreset.value="general";els.llmPrompt.value=LLM_PROMPT_PRESETS.general;}
  else if(LLM_PROMPT_PRESETS[els.llmPromptPreset.value]!==els.llmPrompt.value)els.llmPromptPreset.value="custom";
  const currentMap=sourceMapInput(els.source.value);if(currentMap){if(!currentMap.value&&d.sourceArtist)currentMap.value=d.sourceArtist;els.sourceArtist.value=currentMap.value;}
  if(d.canonicalArtist){els.canonicalArtist.value=d.canonicalArtist;els.canonicalArtistId.value=d.canonicalArtistId||"";els.xUserId.value=d.xUserId||"";els.selectedArtist.innerHTML=`已恢复：<b>${escapeHtml(d.canonicalArtist)}</b>${els.source.value==="twitter"&&d.sourceArtist?` → <b>X @${escapeHtml(d.sourceArtist)}</b>`:""}`;els.selectedArtist.classList.remove("hidden");}
  if(d.tagFormat) document.querySelector(`input[name="tagFormat"][value="${d.tagFormat}"]`)?.click();
  for(const k of ["includeArtist","includeMeta","skipVideo"]) if(typeof d[k]==="boolean") els[k].checked=d[k];
  els.xCookieMode.value=d.xCookieMode==="legacy"?"legacy":"managed";
  els.pixivCookieMode.value=d.pixivCookieMode==="legacy"?"legacy":"managed";
  renderXCookieMode();
  renderPixivCookieMode();
  renderTagger();
}
function sourceMapInput(id){return els.sourceChecklist?.querySelector(`[data-source-map="${id}"]`);}
function stashCurrentSourceConfig(){
  if(!currentSource)return;
  sourceConfigCache[currentSource.id]={...collectDynamic(true),rating:els.rating.value,x_cookie_mode:els.xCookieMode.value,pixiv_cookie_mode:els.pixivCookieMode.value};
  const map=sourceMapInput(currentSource.id);if(map)map.value=els.sourceArtist.value.trim();
}
function restoreCurrentSourceConfig(){
  const cached=sourceConfigCache[currentSource.id]||{};
  document.querySelectorAll("[data-auth-field]").forEach(e=>{if(cached[e.dataset.authField]!=null)e.value=cached[e.dataset.authField];});
  document.querySelectorAll("[data-extra-field]").forEach(e=>{if(cached[e.dataset.extraField]!=null)e.value=cached[e.dataset.extraField];});
  if(cached.rating!=null&&[...els.rating.options].some(o=>o.value===cached.rating))els.rating.value=cached.rating;
  if(currentSource.id==="twitter")els.xCookieMode.value=cached.x_cookie_mode||els.xCookieMode.value||"managed";
  if(currentSource.id==="pixiv")els.pixivCookieMode.value=cached.pixiv_cookie_mode||els.pixivCookieMode.value||"managed";
  const map=sourceMapInput(currentSource.id);els.sourceArtist.value=map?.value||"";
}
function renderSource(clearMapping=true){
  currentSource=sources.find(s=>s.id===els.source.value)||sources[0]; if(!currentSource)return;
  els.sourceDescription.textContent=currentSource.description||"";
  els.sourceWarning.textContent=currentSource.warning||"";els.sourceWarning.classList.toggle("hidden",!currentSource.warning);
  const fieldHtml=f=>f.type==="select"?`<label>${escapeHtml(f.label)}<select data-extra-field="${escapeHtml(f.id)}">${(f.options||[]).map(o=>`<option value="${escapeHtml(o[0])}">${escapeHtml(o[1])}</option>`).join("")}</select></label>`:f.type==="text"?`<label>${escapeHtml(f.label)}<input data-extra-field="${escapeHtml(f.id)}" type="text" placeholder="${escapeHtml(f.placeholder||"")}" autocomplete="off"></label>`:"";
  const authHtml=(currentSource.auth_fields||[]).map(f=>`<label>${escapeHtml(f.label)}${f.required?' *':''}<input data-auth-field="${escapeHtml(f.id)}" type="${f.secret?'password':'text'}" autocomplete="off"></label>`).join("");
  const legacyIds=new Set(["x_cookies_from_browser","x_browser_profile"]), extra=currentSource.extra_fields||[];
  els.authFields.innerHTML=["twitter","pixiv"].includes(currentSource.id)?"":authHtml;
  els.xLegacyAuthFields.innerHTML=currentSource.id==="twitter"?authHtml:"";
  els.pixivLegacyAuthFields.innerHTML=currentSource.id==="pixiv"?authHtml:"";
  els.extraFields.innerHTML=extra.filter(f=>currentSource.id!=="twitter"||!legacyIds.has(f.id)).map(fieldHtml).join("");
  els.xLegacyExtraFields.innerHTML=currentSource.id==="twitter"?extra.filter(f=>legacyIds.has(f.id)).map(fieldHtml).join(""):"";
  els.rating.innerHTML=(currentSource.ratings||[["","全部"]]).map(o=>`<option value="${escapeHtml(o[0])}">${escapeHtml(o[1])}</option>`).join("");
  if(currentSource.id==="twitter") els.sourceArtist.placeholder="X handle（选择带 X 链接的候选可自动填写）";
  else els.sourceArtist.placeholder="来源内的画师 tag / 用户 ID";
  els.sourceArtistHint.classList.toggle("hidden",currentSource.id!=="twitter");
  els.xSessionPanel.classList.toggle("hidden",currentSource.id!=="twitter");
  els.pixivSessionPanel.classList.toggle("hidden",currentSource.id!=="pixiv");
  els.xOpenRow.classList.toggle("hidden",currentSource.id!=="twitter");
  restoreCurrentSourceConfig();
  renderXCookieMode();
  renderPixivCookieMode();
  saveSettings();
}
function renderTagger(){els.llmFields.classList.toggle("hidden",els.taggerType.value!=="openai");els.onnxFields.classList.toggle("hidden",els.taggerType.value!=="onnx");saveSettings();}
function renderQueryType(){
  const mode=els.queryType.value, isArtist=mode==="artist";
  els.queryTypeHint.textContent=isArtist?"以 Danbooru 画师记录确认同名身份，自动带出 X、Pixiv 等关联主页。":"直接输入角色名或标签，点搜索可校验 Danbooru 中是否存在；X/Pixiv 不支持此模式。";
  els.artistQuery.placeholder=isArtist?"如 kantoku、Askzy 或 https://x.com/...":"如 hatsune_miku、touhou 或 1girl";
  if(!isArtist){els.artistCandidates.classList.add("hidden");}
  const userSources=["twitter","pixiv"];
  els.sourceChecklist.querySelectorAll("[data-source-check]").forEach(box=>{
    const id=box.dataset.sourceCheck;
    if(userSources.includes(id)){
      if(!isArtist&&box.checked){box.checked=false;selectedSourceIds.delete(id);const input=els.sourceChecklist.querySelector(`[data-source-map="${id}"]`);if(input)input.disabled=true;}
      box.disabled=!isArtist;
    }
  });
  saveSettings();
}
function applyLlmPromptPreset(){const prompt=LLM_PROMPT_PRESETS[els.llmPromptPreset.value];if(prompt!==undefined)els.llmPrompt.value=prompt;saveSettings();}
function detectCustomLlmPrompt(){const match=Object.entries(LLM_PROMPT_PRESETS).find(([,prompt])=>prompt===els.llmPrompt.value);els.llmPromptPreset.value=match?match[0]:"custom";saveSettings();}
function renderXCookieMode(){
  const managed=currentSource?.id==="twitter"&&els.xCookieMode.value==="managed";
  const isTwitter=currentSource?.id==="twitter";
  els.xLegacyHint.classList.toggle("hidden",managed);
  els.xLegacySettings.classList.toggle("hidden",!isTwitter);
  if(isTwitter)els.xLegacySettings.open=!managed;
  els.xLegacySettings.querySelectorAll("input,select").forEach(e=>e.disabled=managed);
  saveSettings();
}
function renderPixivCookieMode(){
  const isPixiv=currentSource?.id==="pixiv", managed=isPixiv&&els.pixivCookieMode.value==="managed";
  els.pixivLegacySettings.classList.toggle("hidden",!isPixiv);
  if(isPixiv)els.pixivLegacySettings.open=!managed;
  els.pixivLegacySettings.querySelectorAll("input,select").forEach(e=>e.disabled=managed);
  saveSettings();
}

function collectDynamic(includeDisabled=false){const out={};document.querySelectorAll("[data-auth-field]").forEach(e=>{if(includeDisabled||!e.disabled)out[e.dataset.authField]=e.value.trim();});document.querySelectorAll("[data-extra-field]").forEach(e=>{if(includeDisabled||!e.disabled)out[e.dataset.extraField]=e.value;});return out;}
function collectConfig(){
  stashCurrentSourceConfig();
  const common={source:els.source.value,artist:els.sourceArtist.value.trim(),query_type:els.queryType.value,canonical_artist:els.canonicalArtist.value,canonical_artist_id:els.canonicalArtistId.value,count:parseInt(els.count.value,10)||0,tag_format:getTagFormat(),include_artist:els.includeArtist.checked,include_meta:els.includeMeta.checked,skip_video:els.skipVideo.checked,proxy:els.proxy.value.trim(),tagger_type:els.taggerType.value,tag_merge_mode:els.tagMergeMode.value,llm_base_url:els.llmBaseUrl.value.trim(),llm_api_key:els.llmApiKey.value.trim(),llm_model:els.llmModel.value.trim(),llm_prompt:els.llmPrompt.value.trim(),onnx_model_path:els.onnxModelPath.value.trim(),onnx_tags_path:els.onnxTagsPath.value.trim(),onnx_threshold:parseFloat(els.onnxThreshold.value)||0.35};
  Object.assign(common,sourceConfigCache[els.source.value]||{},collectDynamic(),{rating:els.rating.value,x_cookie_mode:els.source.value==="twitter"?els.xCookieMode.value:"legacy",pixiv_cookie_mode:els.source.value==="pixiv"?els.pixivCookieMode.value:"legacy",x_handle:els.source.value==="twitter"?els.sourceArtist.value.trim():"",x_user_id:els.source.value==="twitter"?els.xUserId.value:""});
  const source_configs={};
  for(const id of selectedSourceIds){
    const source=sources.find(item=>item.id===id);let artist=sourceMapInput(id)?.value.trim()||"";
    if(!artist&&id!=="twitter")artist=els.canonicalArtist.value.trim();
    const cfg={...(sourceConfigCache[id]||{}),artist,rating:(sourceConfigCache[id]||{}).rating||""};
    if(id==="twitter")Object.assign(cfg,{x_handle:artist,x_user_id:els.xUserId.value,x_cookie_mode:(sourceConfigCache[id]||{}).x_cookie_mode||"managed"});
    if(id==="pixiv")cfg.pixiv_cookie_mode=(sourceConfigCache[id]||{}).pixiv_cookie_mode||"managed";
    if(source?.needs_auth&&!artist)Object.assign(cfg,{skip:true,skip_reason:"未填写画师映射，认证来源已跳过"});
    source_configs[id]=cfg;
  }
  return {...common,sources:sources.filter(source=>selectedSourceIds.has(source.id)).map(source=>source.id),source_configs};
}

async function searchArtists(){
  const q=els.artistQuery.value.trim();if(!q){showError(els.queryType.value==="artist"?"请输入 Danbooru 画师名、别名或主页 URL":"请输入角色名或标签名");return;}
  hideError();els.searchArtistBtn.disabled=true;els.artistCandidates.classList.remove("hidden");els.artistCandidates.innerHTML='<div class="candidate-empty">搜索中…</div>';
  const cfg=collectConfig();let data;try{data=await postJSON("/api/artists/search",{...cfg,source:"danbooru",query:q,artist:q,query_type:cfg.query_type});}catch(_e){data={ok:false,error:"无法连接本地服务"};}
  els.searchArtistBtn.disabled=false;if(!data.ok){els.artistCandidates.innerHTML=`<div class="candidate-empty">${escapeHtml(data.error||"搜索失败")}</div>`;return;}
  const list=data.artists||[];if(!list.length){els.artistCandidates.innerHTML=`<div class="candidate-empty">${els.queryType.value==="artist"?"没有候选，请尝试别名、X 主页 URL 或手动填写账号":"未找到匹配标签，可尝试部分名称或直接手动输入"}</div>`;return;}
  els.artistCandidates.innerHTML=list.map((a,i)=>`<button class="candidate" data-index="${i}" type="button"><span><b>${escapeHtml(a.name)}</b>${a.other_names?`<small>${escapeHtml(Array.isArray(a.other_names)?a.other_names.join(" · "):a.other_names)}</small>`:""}</span><span class="candidate-meta">${a.x_handles?.length?`X @${escapeHtml(a.x_handles[0])}`:a.post_count!=null?`${a.post_count} 作品`:"无 X 链接"}${a.score!=null?` · ${Math.round(a.score*100)}%`:""}</span></button>`).join("");
  els.artistCandidates.querySelectorAll(".candidate").forEach(btn=>btn.addEventListener("click",()=>selectArtist(list[Number(btn.dataset.index)])));
}
function selectArtist(a){
  els.canonicalArtist.value=a.name||"";els.canonicalArtistId.value=a.id||"";
  els.xUserId.value=(a.x_user_ids||[])[0]||"";
  const handle=(a.x_handles||[])[0],pixivId=(a.pixiv_user_ids||[])[0]||"";
  sources.forEach(source=>{const input=sourceMapInput(source.id);if(!input)return;if(source.id==="twitter")input.value=handle||"";else if(source.id==="pixiv")input.value=pixivId;else input.value=a.name||"";});
  if(els.source.value==="twitter")els.sourceArtist.value=handle||"";else if(els.source.value==="pixiv")els.sourceArtist.value=pixivId;else els.sourceArtist.value=a.name||"";
  const links=[handle?`X @${escapeHtml(handle)}`:"",pixivId?`Pixiv #${escapeHtml(pixivId)}`:""].filter(Boolean).join(" · ");
  els.selectedArtist.innerHTML=`已确认：<b>${escapeHtml(a.name)}</b>${links?` → <b>${links}</b>`:" <span class='muted'>（Danbooru 未登记 X / Pixiv 链接，相关来源需手动填写）</span>"}`;
  els.selectedArtist.classList.remove("hidden");els.artistCandidates.classList.add("hidden");saveSettings();
}
function clearArtistSelection(clearInputs=true){els.canonicalArtist.value="";els.canonicalArtistId.value="";els.xUserId.value="";els.selectedArtist.classList.add("hidden");els.artistCandidates.classList.add("hidden");if(clearInputs)els.sourceArtist.value="";}

async function testSource(){els.testSourceBtn.disabled=true;els.sourceTestResult.textContent="测试中…";const d=await postJSON("/api/source/test",collectConfig()).catch(()=>({ok:false,error:"无法连接本地服务"}));resultText(els.sourceTestResult,d.ok,d.message||d.error||"失败");els.testSourceBtn.disabled=false;}
async function openXPage(){els.openXBtn.disabled=true;els.xOpenResult.textContent="正在打开…";const d=await postJSON("/api/x/open",{artist:els.sourceArtist.value.trim()}).catch(()=>({ok:false,error:"无法连接本地服务"}));resultText(els.xOpenResult,d.ok,d.message||d.error||"打开失败");els.openXBtn.disabled=false;}
async function openXSession(){els.openXSessionBtn.disabled=true;els.xSessionResult.textContent="正在打开专用窗口…";const d=await postJSON("/api/x/session/open",{artist:els.sourceArtist.value.trim()}).catch(()=>({ok:false,error:"无法连接本地服务"}));resultText(els.xSessionResult,d.ok,d.message||d.error||"打开失败");els.openXSessionBtn.disabled=false;}
async function checkXSession(){els.checkXSessionBtn.disabled=true;els.xSessionResult.textContent="正在检查登录…";const d=await postJSON("/api/x/session/check",{}).catch(()=>({ok:false,error:"无法连接本地服务"}));const ok=Boolean(d.ok&&d.logged_in);resultText(els.xSessionResult,ok,d.message||d.error||(d.running?"窗口已打开，但尚未检测到登录":"专用窗口未运行"));els.checkXSessionBtn.disabled=false;}
async function openPixivSession(){els.openPixivSessionBtn.disabled=true;els.pixivSessionResult.textContent="正在打开专用窗口…";const d=await postJSON("/api/pixiv/session/open",{}).catch(()=>({ok:false,error:"无法连接本地服务"}));resultText(els.pixivSessionResult,d.ok,d.message||d.error||"打开失败");els.openPixivSessionBtn.disabled=false;}
async function checkPixivSession(){els.checkPixivSessionBtn.disabled=true;els.pixivSessionResult.textContent="正在检查并恢复登录…";const d=await postJSON("/api/pixiv/session/check",{}).catch(()=>({ok:false,error:"无法连接本地服务"}));const ok=Boolean(d.ok&&d.logged_in);resultText(els.pixivSessionResult,ok,d.message||d.error||(d.running?"窗口已打开，但尚未检测到登录":"专用窗口未运行"));els.checkPixivSessionBtn.disabled=false;}
async function testTagger(){els.testTaggerBtn.disabled=true;els.taggerTestResult.textContent="测试中…";const d=await postJSON("/api/tagger/test",collectConfig()).catch(()=>({ok:false,error:"无法连接本地服务"}));resultText(els.taggerTestResult,d.ok,d.message||d.error||"失败");els.testTaggerBtn.disabled=false;}

async function startTask(){const cfg=collectConfig();if(!cfg.sources.length){showError("请至少选择一个来源");return;}const missing=cfg.sources.filter(id=>!cfg.source_configs[id]?.artist&&!sources.find(s=>s.id===id)?.needs_auth);if(missing.length){showError("请填写这些来源的画师映射："+missing.join("、"));sourceMapInput(missing[0])?.focus();return;}saveSettings();hideError();els.startBtn.disabled=true;const d=await postJSON("/api/start",cfg).catch(()=>({ok:false,error:"无法连接本地服务"}));if(!d.ok){showError(d.error||"启动失败");els.startBtn.disabled=false;return;}resetTaskView();startPolling();}
async function stopTask(){els.stopBtn.disabled=true;await postJSON("/api/stop",{}).catch(()=>{});}
async function shutdownApp(){
  if(!window.confirm("确定关闭后台程序吗？正在运行的下载会先停止。"))return;
  els.shutdownBtn.disabled=true;
  const d=await postJSON("/api/shutdown",{}).catch(()=>({ok:false,error:"无法连接本地服务"}));
  if(!d.ok){showError(d.error||"关闭失败");els.shutdownBtn.disabled=false;return;}
  if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
  els.shutdownResult.textContent=d.message||"后台已关闭，可关闭页面";
  els.shutdownResult.classList.remove("hidden");
  els.statusPill.className="pill stopped";els.statusPill.textContent="后台已关闭";
  document.querySelectorAll("button,input,select,textarea").forEach(control=>control.disabled=true);
}
function renderGalleryEmpty(){
  els.gallery.innerHTML='<div class="gallery-empty" id="galleryEmpty"><div class="empty-mark">AS</div><strong>作品会出现在这里</strong><span>选择并确认画师后开始任务</span></div>';
  els.galleryCount.textContent="0 张";
}
function closeOverlays(){
  if(els.lightbox.open)els.lightbox.close();
  if(els.tagDrawer.open)els.tagDrawer.close();
  els.lightboxMedia.replaceChildren();
  activeTagItem=null;lightboxIndex=-1;
}
function resetTaskView(){
  logCount=0;itemCount=0;galleryItems=[];closeOverlays();renderGalleryEmpty();
  els.logBox.innerHTML="";els.sourceStates.innerHTML="";els.sourceStates.classList.add("hidden");
  els.progressFill.style.width="0%";els.progressText.textContent="0 / 0";
  for(const id of ["statDone","statSkipped","statFailed"])els[id].textContent="0";
  els.statTotal.textContent="–";els.folderLine.classList.add("hidden");
}
function startPolling(){if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(poll,900);poll();}
async function poll(){let d;try{d=await fetch(`/api/progress?logs=${logCount}&items=${itemCount}`).then(r=>r.json());}catch(_e){return;}render(d);const active=["preparing","running"].includes(d.status);els.startBtn.disabled=active;els.stopBtn.disabled=!active;if(!active&&pollTimer){clearInterval(pollTimer);pollTimer=null;}}
function render(d){
  const s=d.status||"idle";els.statusPill.className="pill "+s;els.statusPill.textContent=STATUS_TEXT[s]||s;
  if(s==="error"&&d.error)showError(d.error);if(s==="idle")return;
  els.statTotal.textContent=d.site_total>=0?d.site_total:"–";els.statDone.textContent=d.done??0;els.statSkipped.textContent=d.skipped??0;els.statFailed.textContent=d.failed??0;
  const target=d.target||0,done=d.done||0,processed=done+(d.skipped||0)+(d.failed||0),pct=target?Math.min(100,processed/target*100):0;
  els.progressFill.style.width=pct.toFixed(1)+"%";els.progressText.textContent=`${done} 成功 · ${processed} / ${target||"?"} 已处理`;
  if(d.canonical_artist){els.identityLine.innerHTML=`<b>${escapeHtml(d.canonical_artist)}</b> · ${d.sources?.length||1} 个来源`;els.identityLine.classList.remove("hidden");}
  if(d.sources?.length){els.sourceStates.innerHTML=d.sources.map(src=>`<div class="source-state ${src.status==="error"?"error":""}"><span><b>${escapeHtml(src.label)}</b> · ${escapeHtml(src.artist_key||src.artist||"未配置")} · ${escapeHtml(STATUS_TEXT[src.status]||src.status)}</span><span>${src.done} 成功 / ${src.skipped} 跳过 / ${src.failed} 失败</span></div>`).join("");els.sourceStates.classList.remove("hidden");}
  if(d.folder){els.folderLine.innerHTML=`保存目录：<b>downloads/${escapeHtml(d.folder)}/</b>`;els.folderLine.classList.remove("hidden");els.workspaceSubtitle.textContent=`${d.canonical_artist||d.artist||"当前画师"} · 保存到 downloads/${d.folder}`;}
  if(d.logs?.length){for(const line of d.logs){const div=document.createElement("div");div.textContent=line;els.logBox.appendChild(div);}logCount=d.log_count;els.logBox.scrollTop=els.logBox.scrollHeight;}
  if(d.items?.length){$("galleryEmpty")?.remove();for(const item of d.items)addCard(item);itemCount=d.item_count;}
}

function mediaUrl(item){return item.preview_url||item.image_url||item.url||"";}
function toTags(value){return Array.isArray(value)?value.filter(Boolean).map(String):[];}
function modeLabel(mode){return {tagger_only:"仅使用打标器标签",native_plus_tagger:"来源标签 + 打标器标签",native_only:"仅使用来源标签"}[mode]||mode||"未记录";}
function taggerLabel(id){return {none:"未启用",openai_compatible:"视觉 LLM",openai:"视觉 LLM",local_onnx:"本地 WD14 ONNX",onnx:"本地 WD14 ONNX"}[id]||id||"未记录";}
function tagStatusLabel(status){return {native:"来源标签",generated:"打标完成",failed:"打标失败",merged:"重复项已合并",skipped:"已跳过"}[status]||status||"未记录";}

function createTagGroup(title,tags,kind,detail){
  const section=document.createElement("section");section.className="tag-group";
  const heading=document.createElement("h3"),label=document.createElement("span");heading.textContent=title;label.textContent=detail||`${tags.length} 个`;heading.appendChild(label);section.appendChild(heading);
  if(!tags.length){const empty=document.createElement("div");empty.className="tag-empty";empty.textContent="没有记录到标签";section.appendChild(empty);return section;}
  const list=document.createElement("div");list.className="tag-list";
  for(const tag of tags){const chip=document.createElement("span");chip.className="tag-chip"+(kind?` ${kind}`:"");chip.textContent=tag;list.appendChild(chip);}
  section.appendChild(list);return section;
}
function openTagDrawer(item){
  activeTagItem=item;const nativeTags=toTags(item.native_tags),generatedTags=toTags(item.generated_tags),finalTags=toTags(item.final_tags);
  els.tagDrawerTitle.textContent="打标详情";els.tagDrawerMeta.textContent=`${item.source||"source"} · #${item.id||"?"}${item.filename?` · ${item.filename}`:""}`;
  els.tagDrawerBody.replaceChildren();
  const mode=document.createElement("div");mode.className="tag-mode";const modeName=document.createElement("span"),modeValue=document.createElement("strong");modeName.textContent="当前标签策略";modeValue.textContent=modeLabel(item.tag_merge_mode);mode.append(modeName,modeValue);els.tagDrawerBody.appendChild(mode);
  const summary=document.createElement("div");summary.className="tag-summary";
  const tagger=document.createElement("span");tagger.textContent=`打标器：${taggerLabel(item.tagger_id)}`;
  const status=document.createElement("span");status.className=item.tag_status==="failed"?"err":"ok";status.textContent=tagStatusLabel(item.tag_status);
  summary.append(tagger,status);
  if(item.caption_url){const caption=document.createElement("a");caption.href=item.caption_url;caption.target="_blank";caption.rel="noopener";caption.textContent="打开标签文件";summary.appendChild(caption);}
  els.tagDrawerBody.appendChild(summary);
  if(item.tag_error){const error=document.createElement("div");error.className="tag-error";error.textContent=item.tag_error;els.tagDrawerBody.appendChild(error);}
  els.tagDrawerBody.appendChild(createTagGroup("打标器结果",generatedTags,"generated"));
  els.tagDrawerBody.appendChild(createTagGroup("来源标签",nativeTags,"",item.tag_merge_mode==="tagger_only"?`${nativeTags.length} 个 · 未采用`:`${nativeTags.length} 个`));
  els.tagDrawerBody.appendChild(createTagGroup("最终写入",finalTags,""));
  els.copyTagsBtn.disabled=!finalTags.length;
  if(!els.tagDrawer.open)els.tagDrawer.showModal();
}
async function copyFinalTags(){
  if(!activeTagItem)return;const tags=toTags(activeTagItem.final_tags);if(!tags.length)return;
  const textValue=getTagFormat()==="space"?tags.map(tag=>tag.replace(/ /g,"_")).join(" "):tags.map(tag=>tag.replace(/_/g," ")).join(", ");
  try{await navigator.clipboard.writeText(textValue);els.copyTagsBtn.textContent="已复制";setTimeout(()=>{els.copyTagsBtn.textContent="复制最终标签";},1200);}catch(_e){showError("复制失败，请打开标签文件后手动复制");}
}

function showLightbox(index){
  if(index<0||index>=galleryItems.length)return;const item=galleryItems[index],url=mediaUrl(item);if(!url)return;
  lightboxIndex=index;els.lightboxMedia.replaceChildren();
  let media;if(["mp4","webm"].includes(String(item.ext||"").toLowerCase())){media=document.createElement("video");media.src=url;media.controls=true;media.autoplay=true;media.playsInline=true;}else{media=document.createElement("img");media.src=url;media.alt=`${item.source||"source"} #${item.id||""} 原图`;}
  els.lightboxMedia.appendChild(media);els.lightboxTitle.textContent=`${item.source||"source"} #${item.id||""}`;els.lightboxMeta.textContent=item.filename||"原始尺寸预览";els.lightboxOpen.href=item.image_url||item.url||url;
  els.lightboxPrev.disabled=index<=0;els.lightboxNext.disabled=index>=galleryItems.length-1;
  if(!els.lightbox.open)els.lightbox.showModal();
}
function moveLightbox(delta){showLightbox(lightboxIndex+delta);}

function addCard(item){
  const card=document.createElement("article");card.className="card";card.dataset.status=item.status||"";card.dataset.tagStatus=item.tag_status||"";
  const url=mediaUrl(item);
  if(item.status==="failed"||!url){card.classList.add("failed");const title=document.createElement("strong"),detail=document.createElement("span");title.textContent=`${item.source||"source"} #${item.id||"?"} 处理失败`;detail.textContent=item.tag_error||item.error||"没有可显示的媒体文件";card.append(title,detail);if(item.native_tags||item.final_tags){const button=document.createElement("button");button.className="card-action";button.type="button";button.textContent="查看标签";button.addEventListener("click",()=>openTagDrawer(item));card.appendChild(button);}els.gallery.appendChild(card);els.galleryCount.textContent=`${galleryItems.filter(entry=>mediaUrl(entry)).length} 张`;return;}
  const index=galleryItems.push(item)-1;
  const mediaButton=document.createElement("button");mediaButton.className="card-media";mediaButton.type="button";mediaButton.setAttribute("aria-label",`查看 ${item.source||"source"} #${item.id||""} 原图`);mediaButton.addEventListener("click",()=>showLightbox(index));
  let media;if(["mp4","webm"].includes(String(item.ext||"").toLowerCase())){media=document.createElement("video");media.muted=true;media.loop=true;media.playsInline=true;media.preload="metadata";media.addEventListener("loadeddata",()=>media.classList.add("loaded"),{once:true});media.src=url;}else{media=document.createElement("img");media.loading="lazy";media.decoding="async";media.alt=`${item.source||"source"} #${item.id||""}`;media.addEventListener("load",()=>media.classList.add("loaded"),{once:true});media.src=url;if(media.complete)media.classList.add("loaded");}
  mediaButton.appendChild(media);card.appendChild(mediaButton);
  const badge=document.createElement("span");badge.className="badge";badge.textContent=`${String(item.source||"").toUpperCase()} · #${item.id||""}`;card.appendChild(badge);
  if(item.tag_status==="generated"||item.status==="duplicate"){const mark=document.createElement("span");mark.className="tag-mark";mark.textContent=item.status==="duplicate"?"重复已合并":"AI";card.appendChild(mark);}
  const overlay=document.createElement("div");overlay.className="card-overlay";card.appendChild(overlay);
  const actions=document.createElement("div");actions.className="card-actions";
  const tagsButton=document.createElement("button");tagsButton.className="card-action";tagsButton.type="button";const tagCount=toTags(item.final_tags).length;tagsButton.textContent=tagCount?`标签 ${tagCount}`:"标签";tagsButton.addEventListener("click",()=>openTagDrawer(item));
  actions.appendChild(tagsButton);card.appendChild(actions);els.gallery.appendChild(card);els.galleryCount.textContent=`${galleryItems.filter(entry=>mediaUrl(entry)).length} 张`;
}

els.source.addEventListener("change",()=>{stashCurrentSourceConfig();renderSource(true);});
els.sourceArtist.addEventListener("input",()=>{const input=sourceMapInput(els.source.value);if(input)input.value=els.sourceArtist.value;});
els.xCookieMode.addEventListener("change",renderXCookieMode);els.pixivCookieMode.addEventListener("change",renderPixivCookieMode);
els.taggerType.addEventListener("change",()=>{els.tagMergeMode.value=els.taggerType.value==="none"?"native_only":"tagger_only";renderTagger();});
els.queryType.addEventListener("change",renderQueryType);
els.tagMergeMode.addEventListener("change",saveSettings);els.llmPromptPreset.addEventListener("change",applyLlmPromptPreset);els.llmPrompt.addEventListener("input",detectCustomLlmPrompt);
els.searchArtistBtn.addEventListener("click",searchArtists);els.artistQuery.addEventListener("keydown",e=>{if(e.key==="Enter")searchArtists();});
els.openXSessionBtn.addEventListener("click",openXSession);els.checkXSessionBtn.addEventListener("click",checkXSession);els.openPixivSessionBtn.addEventListener("click",openPixivSession);els.checkPixivSessionBtn.addEventListener("click",checkPixivSession);els.openXBtn.addEventListener("click",openXPage);
els.testSourceBtn.addEventListener("click",testSource);els.testTaggerBtn.addEventListener("click",testTagger);els.startBtn.addEventListener("click",startTask);els.stopBtn.addEventListener("click",stopTask);els.shutdownBtn.addEventListener("click",shutdownApp);
els.lightboxClose.addEventListener("click",()=>els.lightbox.close());els.lightboxPrev.addEventListener("click",()=>moveLightbox(-1));els.lightboxNext.addEventListener("click",()=>moveLightbox(1));
els.lightbox.addEventListener("click",event=>{if(event.target===els.lightbox)els.lightbox.close();});
els.lightbox.addEventListener("close",()=>els.lightboxMedia.replaceChildren());
els.tagDrawerClose.addEventListener("click",()=>els.tagDrawer.close());els.tagDrawer.addEventListener("click",event=>{if(event.target===els.tagDrawer)els.tagDrawer.close();});els.copyTagsBtn.addEventListener("click",copyFinalTags);
document.addEventListener("keydown",event=>{if(!els.lightbox.open)return;if(event.key==="ArrowLeft")moveLightbox(-1);if(event.key==="ArrowRight")moveLightbox(1);});
window.addEventListener("beforeunload",saveSettings);
loadSources().catch(e=>showError("加载来源失败："+e.message));startPolling();
