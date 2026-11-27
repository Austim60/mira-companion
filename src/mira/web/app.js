const $ = id => document.getElementById(id);
const state = { csrf: '', setup: false, page: 'overview', data: null, memories: [], selected: null, onboardTimer: null, logTab: 'runtime', logTimer: null, runtimeLines: [] };
const pageNames = {overview:'总览',bot:'机器人接入',character:'角色与模型',memory:'记忆',logs:'日志'};
const statuses = {connected:'已连接',connecting:'正在连接',reconnecting:'正在重连',disconnected:'未连接',error:'连接错误'};
const logCategories = {qq:'QQ 连接',chat:'私聊',proactive:'主动联系',worker:'后台任务',memory:'记忆',admin:'管理端',model:'模型',character:'角色'};
const logCodeDescriptions = {
  recent_conversation:'最近六小时聊过天，暂不主动联系。',
  awaiting_reply:'上一条主动消息尚未得到回复，暂不重复联系。',
  daily_limit:'今天已达到主动联系上限。',
  paused:'主动联系已暂停。',skipped:'模型决定这次不联系。',
  delivery_unknown:'发送结果不确定，本次不会自动重发。'
};
const errors = {
  invalid_admin_password:'请输入管理密码。',invalid_credentials:'密码不正确。',
  authentication_required:'请重新登录。',invalid_csrf:'页面已过期，请刷新后重试。',
  invalid_bot_id:'请填写有效的 AppID。',invalid_bot_secret:'请填写有效的 AppSecret。',
  invalid_user_id:'请填写有效的 QQ 用户 ID。',bot_not_configured:'请先保存机器人接入信息。',
  bot_replacement_requires_confirmation:'更换机器人需要先确认。',disconnect_bot_before_edit:'请先停止连接，再修改接入信息。',
  qq_disconnected:'QQ 尚未连接或未绑定私聊账号。',onboarding_in_progress:'扫码接入正在进行。',
  qq_token_failed:'获取 QQ 令牌失败，请检查 AppID、AppSecret 和网络。',
  qq_gateway_failed:'获取 QQ 网关地址失败，请稍后重试。',
  qq_websocket_failed:'启动 QQ 长连接失败，请稍后重试。',
  invalid_character:'请完整填写角色设定。',character_id_mismatch:'角色 ID 已固定，不能更改。',
  invalid_model_settings:'请检查模型地址、温度和输出上限。',invalid_memory_correction:'请填写不超过 300 字的更正内容。',
  memory_not_found:'这条记忆已不存在，请刷新列表。',
  invalid_memory_organization:'类别或类型无效。',invalid_memory_confirmation:'这条记忆的表达性质需要先核对并更正内容，才能确认。',
  invalid_memory_merge:'只能合并同一类型、表达性质相同的已确认记忆。'
};
const memoryKinds={daily_fragment:'日常片段',interest:'兴趣',user_fact:'用户事实',episode:'普通事件',important_event:'重要事件',shared_experience:'共同经历',plan:'计划'};
const memoryStatuses={active:'有效',candidate:'待核对',disputed:'有争议',open:'进行中',resolved:'已结束'};

async function request(path, method='GET', body=null, auth=true) {
  const headers = {};
  if (body !== null) headers['Content-Type'] = 'application/json';
  if (auth && method !== 'GET') headers['X-Mira-CSRF'] = state.csrf;
  const response = await fetch(path, {method, headers, body:body === null ? null : JSON.stringify(body), credentials:'same-origin'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = typeof data.error === 'string' ? data.error : data.detail;
    throw new Error(errors[code] || (typeof code === 'string' ? code : `请求失败 (${response.status})`));
  }
  return data;
}

function toast(message, error=false) {
  const node=$('toast'); node.textContent=message; node.classList.toggle('error',error); node.classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer=setTimeout(()=>node.classList.add('hidden'),4200);
}
function showAuth(setup) {
  state.setup=setup; $('auth').classList.remove('hidden'); $('shell').classList.add('hidden');
  $('auth-title').textContent=setup?'设置管理密码':'登录 Mira';
  $('auth-description').textContent=setup?'首次使用，请设置仅自己知道的管理密码。':'管理你的机器人、角色与记忆。';
  $('auth-submit').textContent=setup?'完成设置':'进入管理端';
  $('auth-password').autocomplete=setup?'new-password':'current-password';
  $('auth-error').textContent='';
}
function showShell() { $('auth').classList.add('hidden'); $('shell').classList.remove('hidden'); navigate(state.page); }
function navigate(page) {
  if(state.page==='logs'&&page!=='logs') stopLogPolling();
  state.page=page;
  document.querySelectorAll('.nav-item').forEach(node=>node.classList.toggle('active',node.dataset.page===page));
  document.querySelectorAll('.page').forEach(node=>node.classList.toggle('active',node.id===`page-${page}`));
  $('top-title').textContent=pageNames[page];
  if(page==='memory') refreshMemoryPage(); if(page==='logs') showLogTab(state.logTab);
}
const expandedMemoryCategories = new Set();
let historyCursor = null;
let historyRequest = 0;
function refreshMemoryPage() {
  expandedMemoryCategories.clear();
  loadMemories();
  if ($('history-panel').open) loadHistory();
}
async function loadHistory(before = null) {
  const version = ++historyRequest;
  const list = $('history-list'), more = $('history-more');
  more.disabled = true;
  if (before === null) {
    list.textContent = '正在读取对话…';
    more.classList.add('hidden');
  }
  try {
    const data = await request('/api/conversations' + (before === null ? '' : '?before=' + before));
    if (version !== historyRequest) return;
    if (before === null) list.replaceChildren();
    if (!data.items.length && !list.childElementCount) list.textContent = '暂无原始对话';
    for (const item of data.items) {
      const turn = document.createElement('article');
      turn.className = 'history-turn';
      const meta = document.createElement('small');
      const status = item.suppressed ? '已停止用于记忆' : item.job_error ? '整理待重试' :
        item.job_status === 'completed' ? '已整理' : item.job_status === 'pending' ? '待整理' :
        item.status === 'failed' ? '回复失败' : '处理中';
      meta.textContent = new Date(item.occurred_at).toLocaleString('zh-CN') + ' · ' + status;
      turn.append(meta);
      for (const [label, text] of [['用户', item.user_text], ['角色', item.reply]]) {
        if (text === null) continue;
        const name = document.createElement('strong'), body = document.createElement('p');
        name.textContent = label;
        body.textContent = text;
        turn.append(name, body);
      }
      list.append(turn);
    }
    historyCursor = data.next_before;
    more.classList.toggle('hidden', historyCursor === null);
  } catch (error) {
    if (version === historyRequest) {
      if (before === null) list.textContent = '读取失败，请刷新重试';
      toast(error.message, true);
    }
  } finally {
    if (version === historyRequest) more.disabled = false;
  }
}
function textLine(container,label,value) {
  const line=document.createElement('div'); line.className='detail-item';
  const name=document.createElement('span'); name.textContent=label;
  const data=document.createElement('strong'); data.textContent=value||'—';
  line.append(name,data); container.append(line);
}
function setPill(node,status) {
  node.textContent=statuses[status]||'未连接';
  node.className='status-pill '+(status==='connected'?'ok':status==='error'?'error':status==='connecting'||status==='reconnecting'?'warn':'');
}

async function refreshState(fillForms=false) {
  try {
    const data=await request('/api/state'); state.data=data;
    const bot=data.bot, character=data.character, counts=data.counts;
    setPill($('top-status'),bot.status); setPill($('bot-status'),bot.status);
    $('stat-connection').textContent=statuses[bot.status]||'未连接';
    $('stat-app-id').textContent=bot.app_id?`AppID · ${bot.app_id}`:'尚未配置';
    $('stat-turns').textContent=counts.turns; $('stat-memories').textContent=counts.memories;
    $('stat-pending').textContent=counts.pending_jobs; $('stat-failed').textContent=`失败 ${counts.failed_jobs} 条`;
    $('overview-bot').replaceChildren();
    textLine($('overview-bot'),'机器人 AppID',bot.app_id);
    textLine($('overview-bot'),'私聊账号',bot.allowed_user_id?'已绑定':'未绑定');
    textLine($('overview-bot'),'连接',statuses[bot.status]);
    $('overview-character').replaceChildren();
    textLine($('overview-character'),'角色名称',character?.display_name);
    textLine($('overview-character'),'角色 ID',character?.character_id);
    textLine($('overview-character'),'模型',data.model.model||'未配置');
    $('bot-details').replaceChildren();
    textLine($('bot-details'),'AppID',bot.app_id);
    textLine($('bot-details'),'密钥',bot.has_secret?'已保存':'未设置');
    textLine($('bot-details'),'绑定账号',bot.allowed_user_id||'未绑定');
    $('bot-error').textContent=bot.last_error?`最近错误：${bot.last_error}`:'';
    $('bot-toggle').textContent=bot.enabled?'停止连接':'启动连接';
    $('bot-toggle').disabled=!bot.has_secret;
    $('bot-test').disabled=bot.status!=='connected'||!bot.allowed_user_id;
    $('onboard-start').disabled=bot.status==='connected'||bot.status==='connecting';
    const pending=bot.pending_user;
    $('pending-user').textContent=pending?`${pending.name} · ${pending.user_id}`:'暂无待绑定账号';
    $('bind-pending').disabled=!pending;
    if(fillForms) {
      const bf=$('bot-form'); bf.elements.app_id.value=bot.app_id||'';
      bf.elements.allowed_user_id.value=bot.allowed_user_id||'';
      bf.elements.secret.placeholder=bot.has_secret?'已保存，留空则不修改':'请输入 AppSecret';
      const cf=$('character-form');
      if(character) Object.keys(character).forEach(key=>{if(cf.elements[key]) cf.elements[key].value=character[key];});
      if(character) cf.elements.character_id.readOnly=true;
      const mf=$('model-form'); mf.elements.base_url.value=data.model.base_url||'';
      mf.elements.model.value=data.model.model||''; mf.elements.temperature.value=data.model.temperature;
      mf.elements.max_output_tokens.value=data.model.max_output_tokens;
      mf.elements.api_key.placeholder=data.model.has_api_key?'已保存，留空则不修改':'请输入 API Key';
    }
  } catch(error) { toast(error.message,true); }
}

async function loadMemories() {
  try {
    const data=await request('/api/memories'); state.memories=data.items;
    $('memory-count').textContent=`${data.items.length} 条`;
    const categories=[...new Set(data.items.map(item=>item.category).filter(Boolean))];
    $('memory-category-options').replaceChildren();
    for(const category of categories) {const option=document.createElement('option');option.value=category;$('memory-category-options').append(option);}
    renderMemories();
    if(state.selected&&!data.items.some(item=>item.id===state.selected)) {state.selected=null;$('memory-detail').classList.add('hidden');$('memory-empty').classList.remove('hidden');}
  } catch(error) {toast(error.message,true);}
}
function renderMemories() {
    const list=$('memory-list'); list.replaceChildren();
    const search=$('memory-search').value.trim().toLocaleLowerCase();
    const filter=$('memory-status-filter').value;
    const groups=new Map();
    for(const item of state.memories) {
      if(filter&&item.status!==filter) continue;
      const category=item.category||memoryKinds[item.kind]||item.kind;
      if(search&&!((item.content+' '+category).toLocaleLowerCase().includes(search))) continue;
      if(!groups.has(category)) groups.set(category,[]);
      groups.get(category).push(item);
    }
    if(!groups.size) {const empty=document.createElement('div');empty.className='empty';empty.textContent='没有符合条件的记忆';list.append(empty);return;}
    for(const [category,items] of [...groups].sort(([a],[b])=>a.localeCompare(b,'zh-CN'))) {
      const expanded=expandedMemoryCategories.has(category);
      const visible=expanded?items:items.slice(0,5);
      const heading=document.createElement('h3');heading.className='memory-group';heading.textContent=`${category} · ${items.length}`;list.append(heading);
      for(const item of visible) {
      const button=document.createElement('button');button.className='memory-item'+(item.id===state.selected?' selected':'');button.type='button';
      button.dataset.memoryId=item.id;
      const title=document.createElement('strong');title.textContent=item.content;
      const meta=document.createElement('span');meta.textContent=`${memoryKinds[item.kind]||item.kind} · ${memoryStatuses[item.status]||item.status} · ${new Date(item.occurred_at).toLocaleDateString('zh-CN')}`;
      button.append(title,meta);button.addEventListener('click',()=>selectMemory(item.id));list.append(button);
      }
      if(items.length>5) {
        const toggle=document.createElement('button');toggle.type='button';toggle.className='secondary memory-expand';
        toggle.textContent=expanded?'收起此类，只看前 5 条':`展开此类其余 ${items.length-5} 条`;
        toggle.addEventListener('click',()=>{
          if(expanded) expandedMemoryCategories.delete(category);
          else expandedMemoryCategories.add(category);
          renderMemories();
        });
        list.append(toggle);
      }
    }
}
async function selectMemory(id) {
  state.selected=id;const item=state.memories.find(row=>row.id===id);if(!item)return;
  document.querySelectorAll('.memory-item').forEach(node=>node.classList.toggle('selected',node.dataset.memoryId===id));
  $('memory-empty').classList.add('hidden');$('memory-detail').classList.remove('hidden');
  $('memory-meta').textContent=`${memoryKinds[item.kind]||item.kind} · ${memoryStatuses[item.status]||item.status} · 重要性 ${Number(item.importance).toFixed(2)} · ${new Date(item.occurred_at).toLocaleString('zh-CN')}`;
  $('memory-content').textContent=item.content;$('memory-correction').value=item.content;
  $('memory-category').value=item.category||'';$('memory-kind').value=item.kind;
  $('memory-confirm').checked=false;
  $('memory-confirm-label').classList.toggle('hidden',!['candidate','disputed'].includes(item.status));
  $('memory-merge-source').replaceChildren();
  const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='选择要并入的记忆';$('memory-merge-source').append(placeholder);
  for(const other of state.memories.filter(other=>other.id!==id&&['active','open','resolved'].includes(other.status)&&other.kind===item.kind&&other.claim_mode===item.claim_mode)) {
    const option=document.createElement('option');option.value=other.id;
    option.textContent=`${other.category||memoryKinds[other.kind]||other.kind} · ${other.content.slice(0,70)}`;
    $('memory-merge-source').append(option);
  }
  $('memory-merge').disabled=!['active','open','resolved'].includes(item.status);
  $('memory-sources').replaceChildren();
  try {
    const data=await request(`/api/memories/${encodeURIComponent(id)}/sources`);
    for(const row of data.sources) {const node=document.createElement('div');node.className='source';const time=document.createElement('time');time.textContent=new Date(row.occurred_at).toLocaleString('zh-CN');const text=document.createElement('span');text.textContent=row.text;node.append(time,text);$('memory-sources').append(node);}
  } catch(error) {toast(error.message,true);}
}
async function loadLogs() {
  try {
    const data=await request('/api/logs'); const list=$('log-list');list.replaceChildren();
    if(!data.events.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='暂无运行记录';list.append(empty);return;}
    for(const item of data.events){
      const entry=document.createElement('details');entry.className='log-entry';
      const row=document.createElement('summary');row.className='log-row';
      for(const [tag,cls,value] of [
        ['time','',new Date(item.at).toLocaleString('zh-CN')],
        ['span','category',logCategories[item.category]||item.category],
        ['span','message',item.message],['span','code',item.code||''],
        ['span','log-action','详情']
      ]){const node=document.createElement(tag);node.className=cls;node.textContent=value;row.append(node);}
      const detail=document.createElement('div');detail.className='log-detail';
      const addField=(label,value)=>{if(!value)return;const field=document.createElement('div');field.className='log-detail-field';const name=document.createElement('strong');name.textContent=label;const text=document.createElement('span');text.textContent=value;field.append(name,text);detail.append(field);};
      addField('时间',new Date(item.at).toLocaleString('zh-CN',{hour12:false}));
      addField('类别',logCategories[item.category]||item.category);
      addField('事件',item.message);
      addField('结果代码',item.code);
      addField('说明',item.detail?.reason||logCodeDescriptions[item.code]);
      addField('出错阶段',item.detail?.stage);
      addField('异常类型',item.detail?.exception);
      if(item.detail?.stack?.length){
        const heading=document.createElement('strong');heading.className='log-stack-title';heading.textContent='调用位置';detail.append(heading);
        const stack=document.createElement('pre');stack.className='log-stack';stack.textContent=item.detail.stack.map(frame=>`${frame.file}:${frame.line}  ${frame.function}`).join('\n');detail.append(stack);
      }else if(!item.detail){
        const note=document.createElement('p');note.className='log-legacy-note';note.textContent='这条旧日志只保存了摘要，无法还原当时的调用位置。';detail.append(note);
      }
      entry.append(row,detail);list.append(entry);
    }
  } catch(error){toast(error.message,true);}
}
function stopLogPolling(){clearInterval(state.logTimer);state.logTimer=null;}
function startLogPolling(){
  stopLogPolling();
  if(state.page==='logs'&&state.logTab==='runtime'&&$('runtime-auto').checked){
    state.logTimer=setInterval(loadRuntimeLogs,3000);
  }
}
function showLogTab(tab){
  state.logTab=tab;
  $('runtime-panel').classList.toggle('hidden',tab!=='runtime');
  $('events-panel').classList.toggle('hidden',tab!=='events');
  for(const [name,id] of [['runtime','runtime-tab'],['events','events-tab']]){
    $(id).classList.toggle('active',name===tab);
    $(id).setAttribute('aria-selected',String(name===tab));
  }
  if(tab==='runtime'){loadRuntimeLogs();startLogPolling();}
  else{stopLogPolling();loadLogs();}
}
function renderRuntimeLogs(){
  const consoleNode=$('runtime-console');
  const nearBottom=consoleNode.scrollHeight-consoleNode.scrollTop-consoleNode.clientHeight<60;
  const level=$('runtime-level').value;
  const query=$('runtime-search').value.trim().toLocaleLowerCase();
  consoleNode.replaceChildren();
  const lines=state.runtimeLines.filter(line=>(!level||line.includes(` ${level.padEnd(5)} [`))&&(!query||line.toLocaleLowerCase().includes(query)));
  if(!lines.length){const empty=document.createElement('div');empty.className='runtime-empty';empty.textContent=state.runtimeLines.length?'没有匹配的日志':'暂无运行日志';consoleNode.append(empty);return;}
  for(const line of lines){
    const row=document.createElement('div');row.className='runtime-line';
    if(line.includes(' ERROR ['))row.classList.add('error');
    else if(line.includes(' WARN  ['))row.classList.add('warn');
    row.textContent=line;consoleNode.append(row);
  }
  if(nearBottom)consoleNode.scrollTop=consoleNode.scrollHeight;
}
async function loadRuntimeLogs(){
  try{
    const data=await request('/api/runtime-logs?limit=500');
    state.runtimeLines=data.lines;
    $('runtime-file').textContent=data.file;
    renderRuntimeLogs();
  }catch(error){stopLogPolling();toast(error.message,true);}
}
async function pollOnboard() {
  try {
    const result=await request('/api/onboard/status');
    const active=['starting','waiting_for_scan'].includes(result.status);
    if(active&&!state.onboardTimer) state.onboardTimer=setInterval(pollOnboard,2000);
    $('onboard-progress').classList.toggle('hidden',!active);
    if(result.qr_ready){$('onboard-qr').src=`/api/onboard/qr?t=${Date.now()}`;$('onboard-message').textContent='用手机 QQ 扫码并确认，等待接入完成。';}
    else $('onboard-message').textContent='正在生成二维码…';
    if(!active){clearInterval(state.onboardTimer);state.onboardTimer=null;if(result.status==='completed'){toast('扫码接入完成，可启动连接');await refreshState(true);}else if(result.status==='failed')toast(`接入失败：${result.error}`,true);}
  }catch(error){clearInterval(state.onboardTimer);state.onboardTimer=null;toast(error.message,true);}
}

function wireEvents() {
  document.querySelectorAll('.nav-item').forEach(node=>node.addEventListener('click',()=>navigate(node.dataset.page)));
  document.querySelectorAll('[data-go]').forEach(node=>node.addEventListener('click',()=>navigate(node.dataset.go)));
  $('refresh').addEventListener('click',()=>refreshState(true));
  $('auth-form').addEventListener('submit',async event=>{event.preventDefault();$('auth-error').textContent='';try{const result=await request(state.setup?'/api/setup':'/api/login','POST',{password:$('auth-password').value},false);state.csrf=result.csrf;$('auth-password').value='';showShell();await refreshState(true);await pollOnboard();}catch(error){$('auth-error').textContent=error.message;}});
  $('logout').addEventListener('click',async()=>{try{await request('/api/logout','POST');}finally{stopLogPolling();state.csrf='';showAuth(false);}});
  $('run-check').addEventListener('click',async()=>{try{await request('/api/worker/check','POST');toast('后台任务已检查');await refreshState();}catch(error){toast(error.message,true);}});
  $('bot-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.target;const appId=form.elements.app_id.value.trim();const replace=Boolean(state.data?.bot.app_id&&state.data.bot.app_id!==appId);if(replace&&!confirm('更换机器人后，旧机器人将停止使用。当前角色和记忆会保留。继续吗？'))return;try{await request('/api/bot','POST',{app_id:appId,secret:form.elements.secret.value||null,allowed_user_id:form.elements.allowed_user_id.value.trim(),replace});form.elements.secret.value='';toast('接入信息已保存');await refreshState(true);}catch(error){toast(error.message,true);}});
  $('bot-toggle').addEventListener('click',async()=>{try{const enabled=!state.data.bot.enabled;await request('/api/bot/enabled','POST',{enabled});toast(enabled?'连接已启动':'连接已停止');await refreshState();}catch(error){toast(error.message,true);await refreshState();}});
  $('bot-test').addEventListener('click',async()=>{try{await request('/api/bot/test','POST');toast('测试消息已提交，请在 QQ 中确认收到');}catch(error){toast(error.message,true);}});
  $('bind-pending').addEventListener('click',async()=>{const user=state.data.bot.pending_user;if(!user)return;try{await request('/api/bot/bind','POST',{user_id:user.user_id});toast('私聊账号已绑定');await refreshState(true);}catch(error){toast(error.message,true);}});
  $('onboard-start').addEventListener('click',async()=>{const replace=Boolean(state.data?.bot.app_id);if(replace&&!confirm('扫码接入会替换当前机器人连接，角色和记忆会保留。继续吗？'))return;try{await request('/api/onboard/start','POST',{replace});$('onboard-progress').classList.remove('hidden');await pollOnboard();}catch(error){toast(error.message,true);}});
  $('character-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.target));try{await request('/api/character','POST',data);toast('角色设定已保存');await refreshState(true);}catch(error){toast(error.message,true);}});
  $('model-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.target));try{await request('/api/model','POST',data);event.target.elements.api_key.value='';toast('模型配置已保存');await refreshState(true);}catch(error){toast(error.message,true);}});
  $('memory-refresh').addEventListener('click',refreshMemoryPage);$('logs-refresh').addEventListener('click',loadLogs);
  $('memory-search').addEventListener('input',()=>{expandedMemoryCategories.clear();renderMemories();});
  $('memory-status-filter').addEventListener('change',()=>{expandedMemoryCategories.clear();renderMemories();});
  $('memory-organize-form').addEventListener('submit',async event=>{event.preventDefault();if(!state.selected)return;try{const id=state.selected;await request(`/api/memories/${encodeURIComponent(id)}/organize`,'POST',{category:$('memory-category').value.trim(),kind:$('memory-kind').value,confirm:$('memory-confirm').checked});toast('记忆已整理');await loadMemories();await selectMemory(id);await refreshState();}catch(error){toast(error.message,true);}});
  $('memory-merge').addEventListener('click',async()=>{const sourceId=$('memory-merge-source').value;if(!state.selected||!sourceId)return;const source=state.memories.find(item=>item.id===sourceId);if(!confirm(`把“${source.content}”并入当前记忆？两条来源都会保留，原记录将退出列表。`))return;try{const id=state.selected;await request(`/api/memories/${encodeURIComponent(id)}/merge`,'POST',{source_id:sourceId});toast('重复记忆已合并');await loadMemories();await selectMemory(id);await refreshState();}catch(error){toast(error.message,true);}});
  $('history-panel').addEventListener('toggle',()=>{if ($('history-panel').open) loadHistory();});
  $('history-more').addEventListener('click',()=>loadHistory(historyCursor));
  $('runtime-tab').addEventListener('click',()=>showLogTab('runtime'));
  $('events-tab').addEventListener('click',()=>showLogTab('events'));
  $('runtime-refresh').addEventListener('click',loadRuntimeLogs);
  $('runtime-level').addEventListener('change',renderRuntimeLogs);
  $('runtime-search').addEventListener('input',renderRuntimeLogs);
  $('runtime-auto').addEventListener('change',startLogPolling);
  $('memory-correct-form').addEventListener('submit',async event=>{event.preventDefault();if(!state.selected)return;const current=state.memories.find(item=>item.id===state.selected);const content=$('memory-correction').value.trim();if(content===current.content)return;try{const result=await request(`/api/memories/${encodeURIComponent(state.selected)}/correct`,'POST',{content});toast('记忆已更正');await loadMemories();await selectMemory(result.id);await refreshState();}catch(error){toast(error.message,true);}});
  $('memory-forget').addEventListener('click',async()=>{if(!state.selected||!confirm('停止使用这条记忆及其来源？原始聊天仍保留，但不会再次用于回复。'))return;try{await request(`/api/memories/${encodeURIComponent(state.selected)}/forget`,'POST');toast('已停止使用这条记忆');state.selected=null;await loadMemories();$('memory-detail').classList.add('hidden');$('memory-empty').classList.remove('hidden');await refreshState();}catch(error){toast(error.message,true);}});
}

async function boot(){wireEvents();try{const result=await request('/api/session','GET',null,false);if(result.authenticated){state.csrf=result.csrf;showShell();await refreshState(true);await pollOnboard();}else showAuth(result.setup_required);}catch(error){showAuth(false);$('auth-error').textContent=error.message;}setInterval(()=>{if(state.csrf)refreshState();},15000);}
boot();
