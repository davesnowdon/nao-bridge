const $ = id => document.getElementById(id);
let state, documentRevision = -1, catalogKey = '', selectionKey = '', controlId = null;
let targets = {}, pendingTargets = null, sendingTargets = false, heartbeatPending = false;
let ownedControlId = null;
let latestError = '';
function message(error) { latestError = String(error.message || error); $('error').textContent = latestError; }
function element(tag, text, props = {}) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; Object.assign(e, props); return e; }
async function action(name, params = {}) {
  const response = await fetch('/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,params})});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  render(result); return result;
}
function bind(id, fn) { $(id).onclick = () => Promise.resolve().then(fn).catch(message); }
function selectedInputs() { return [...$('joints').querySelectorAll('input:checked')].map(e => e.value); }
function jointValue(name) { return state?.state?.groups?.joints?.value?.measured?.[name]; }
function renderCatalog(s) {
  const key = JSON.stringify(s.catalog);
  if (key !== catalogKey) {
    catalogKey = key; $('chains').replaceChildren(); $('joints').replaceChildren();
    for (const [chain, names] of Object.entries(s.catalog?.chains || {})) {
      const b = element('button', chain); b.onclick = () => { for (const input of $('joints').querySelectorAll('input')) input.checked = names.includes(input.value); }; $('chains').append(b);
    }
    for (const joint of s.catalog?.items || []) {
      const input = element('input', undefined, {type:'checkbox',value:joint.name,disabled:!joint.readable});
      const label = element('label'); label.append(input, document.createTextNode(` ${joint.name}`)); $('joints').append(label);
    }
    selectionKey = '';
  }
  const selection = JSON.stringify(s.selected);
  if (selection !== selectionKey) {
    selectionKey = selection;
    for (const input of $('joints').querySelectorAll('input')) input.checked = s.selected.includes(input.value);
    renderSliders(s);
  }
}
function renderSliders(s) {
  $('sliders').replaceChildren(); targets = {};
  for (const name of s.selected) {
    const joint = s.catalog?.items.find(j => j.name === name);
    if (!joint || joint.minimum === null || joint.maximum === null) continue;
    const value = jointValue(name); targets[name] = typeof value === 'number' ? value : 0;
    const row = element('div', undefined, {className:'slider-row'}), label = element('label', `${name} (${joint.unit})`), output = element('output', targets[name].toFixed(3));
    const slider = element('input', undefined, {type:'range', min:joint.minimum, max:joint.maximum, step:0.005, value:targets[name], disabled:!s.control_id || s.control_id !== ownedControlId});
    slider.dataset.joint = name;
    const change = value => {
      targets[name] = Math.max(joint.minimum, Math.min(joint.maximum, value));
      for (const partner of joint.coupled_with) if (partner in targets) targets[partner] = targets[name];
      for (const input of $('sliders').querySelectorAll('input')) { input.value = targets[input.dataset.joint]; input.previousElementSibling.querySelector('output').textContent = Number(input.value).toFixed(3); }
      pendingTargets = {...targets}; sendTargets().catch(message);
    };
    slider.oninput = () => change(Number(slider.value)); label.append(output); row.append(label, slider);
    const buttons = element('div', undefined, {className:'buttons'});
    for (const [text,delta] of [['−',-0.02],['+',0.02]]) { const b = element('button', text, {disabled:!s.control_id || s.control_id !== ownedControlId}); b.onclick = () => change(targets[name] + delta); buttons.append(b); }
    row.append(buttons); $('sliders').append(row);
  }
}
async function sendTargets() {
  if (sendingTargets) return;
  sendingTargets = true;
  try {
    while (pendingTargets && controlId && controlId === ownedControlId) {
      const positions = pendingTargets, id = controlId; pendingTargets = null;
      await action('targets', {control_id:id, positions});
    }
  } catch (error) { pendingTargets = null; throw error; }
  finally { sendingTargets = false; }
}
async function edit(poses, name = state.document.name) { await action('edit', {poses,name,revision:state.document_revision}); }
function renderPoses(s) {
  if (s.document_revision === documentRevision) return;
  documentRevision = s.document_revision;
  $('motion-name').value = s.document.name; $('poses').replaceChildren();
  s.document.poses.forEach((pose, index) => {
    const item = element('li'), row = element('div', undefined, {className:'row'});
    const name = element('input', undefined, {value:pose.name,maxLength:120,ariaLabel:'Pose name'});
    const duration = element('input', undefined, {type:'number',min:0.05,max:600,step:0.1,value:Number(pose.duration.toFixed(3)),ariaLabel:'Seconds to pose',title:'Seconds to reach this pose'});
    const update = () => { const poses = structuredClone(state.document.poses); poses[index] = {...poses[index],name:name.value,duration:Number(duration.value)}; edit(poses).catch(message); };
    name.onchange = update; duration.onchange = update; row.append(name,duration);
    for (const [text,delta] of [['↑',-1],['↓',1],['Delete',0]]) {
      const b = element('button',text); b.onclick = () => { const poses = structuredClone(state.document.poses); if (delta === 0) poses.splice(index,1); else if (index + delta >= 0 && index + delta < poses.length) [poses[index],poses[index+delta]] = [poses[index+delta],poses[index]]; edit(poses).catch(message); }; row.append(b);
    }
    item.append(row, element('small', Object.keys(pose.positions).join(', '))); $('poses').append(item);
  });
}
function render(s) {
  state = s; renderCatalog(s); renderPoses(s);
  if (controlId !== s.control_id) { controlId = s.control_id; pendingTargets = null; renderSliders(s); }
  $('connection').textContent = s.connected ? `${s.robot?.name || 'Robot'} · ${s.robot?.backend} · ${s.robot?.model || 'unknown model'} · connection generation ${s.state?.connection_generation} · hardware verified: ${s.robot?.hardware_verified}` : 'Disconnected. Connect explicitly to create a new session.';
  $('error').textContent = latestError || s.error || '';
  const group = s.state?.groups?.joints;
  $('freshness').textContent = `Joints: ${group?.quality || 'unavailable'} · source age ${group?.age_ms === null ? '?' : Math.round(group?.age_ms || 0)} ms · last app observation ${(s.observation_age_seconds ?? 0).toFixed(1)} s ago`;
  const values = {}; for (const j of s.selected) values[j] = {measured:jointValue(j),commanded:group?.value?.commanded?.[j],stiffness:group?.value?.stiffness?.[j]};
  $('measured').textContent = JSON.stringify(values,(key,value) => typeof value === 'number' ? Number(value.toFixed(3)) : value,2);
  $('manual-settings').textContent = JSON.stringify(s.manual_settings,null,2);
  $('operation').textContent = s.operation ? JSON.stringify({kind:s.operation.kind,status:s.operation.status,result:s.operation.result,error:s.operation.error},null,2) : 'No operation yet.';
  $('input-status').textContent = `${s.armed ? 'Capture inputs armed' : 'Capture inputs disarmed'}${s.listener ? ' · voice ' + s.listener.status : ''}`;
  const support = s.capabilities?.features?.['motion.joint_setpoints'];
  $('control-support').textContent = support?.status === 'available' ? 'Latest targets only. Switching away or losing the browser heartbeat ends this control.' : `Unavailable: ${support?.reason || 'controlled hold has not been verified'}`;
  for (const id of ['select','capture','arm','manual','wake','set-stiffness','play','convert']) $(id).disabled = !s.connected || s.busy;
  $('arm').disabled ||= !s.stream_ready;
  $('play').disabled ||= !s.document.poses.length;
  $('convert').disabled ||= !s.samples?.segments?.length;
  $('control-start').disabled = !s.connected || s.busy || support?.status !== 'available';
  $('control-end').disabled = !controlId;
  $('connect').disabled = s.connected; $('disconnect').disabled = !s.connected;
  $('record').disabled = !s.connected || Boolean(s.recording && !['completed','failed'].includes(s.recording.status));
  $('record-stop').disabled = !s.recording || ['completed','failed'].includes(s.recording.status);
  $('inspect').disabled = !s.recording?.asset_id;
  $('download').classList.toggle('disabled-link', !s.recording?.asset_id);
  $('record-status').textContent = s.recording ? JSON.stringify({status:s.recording.status,source:s.recording.source,asset_id:s.recording.asset_id,error:s.recording.error},null,2) : 'No recording yet.';
  $('gaps').textContent = s.samples ? JSON.stringify(s.samples.gaps,null,2) : '';
  const options = JSON.stringify(s.samples?.segments || []);
  if ($('segment').dataset.options !== options) {
    $('segment').dataset.options = options; $('segment').replaceChildren();
    for (const segment of s.samples?.segments || []) $('segment').append(element('option', `${segment.index + 1}: ${segment.samples} samples, ${((segment.end_ms-segment.start_ms)/1000).toFixed(2)} seconds`, {value:segment.index}));
  }
  $('notices').replaceChildren(...s.notices.slice().reverse().map(n => element('li',n)));
}
bind('connect', () => { latestError = ''; return action('connect'); }); bind('disconnect', () => action('disconnect'));
for (const id of ['cancel','stop','wake','manual','play','disarm','record-stop','inspect']) bind(id, () => action(id.replace('-','_')));
bind('select', () => action('select',{joints:selectedInputs()}));
bind('capture', () => action('capture',{name:$('pose-name').value,duration:Number($('pose-duration').value)}));
bind('arm', () => action('arm',{voice:$('voice').checked}));
bind('set-stiffness', () => action('stiffness',{value:Number($('stiffness').value)}));
bind('control-start', async () => { const result = await action('control_start'); ownedControlId = result.control_id; renderSliders(result); }); bind('control-end', () => action('control_end',{control_id:controlId}));
bind('record', () => action('record',{duration:Number($('record-duration').value),rate_hz:Number($('record-rate').value)}));
bind('convert', () => action('convert',{segment:Number($('segment').value),spacing:Number($('spacing').value),first_duration:Number($('first-duration').value)}));
$('motion-name').onchange = () => edit(state.document.poses,$('motion-name').value).catch(message);
$('load').onchange = async () => { try { const file = $('load').files[0]; if (!file) return; if (file.size > 1048576) throw new Error('Trajectory file exceeds 1 MiB'); await action('load',{json:await file.text()}); } catch(error) { message(error); } finally { $('load').value = ''; } };
function leaveControl() {
  if (!ownedControlId) return;
  const id = ownedControlId; ownedControlId = null; pendingTargets = null;
  fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'control_end',params:{control_id:id}}),keepalive:true}).catch(message);
}
document.addEventListener('visibilitychange', () => { if (document.hidden) leaveControl(); }); window.addEventListener('pagehide',leaveControl);
setInterval(async () => {
  if (!controlId || controlId !== ownedControlId || document.hidden || heartbeatPending) return;
  heartbeatPending = true;
  try { await action('heartbeat',{control_id:controlId}); } catch(error) { leaveControl(); message(error); } finally { heartbeatPending = false; }
},200);
async function poll() {
  try { const response = await fetch('/state'); if (!response.ok) throw new Error('Example server is unavailable'); render(await response.json()); }
  catch(error) { message(error); leaveControl(); }
  finally { setTimeout(poll,500); }
}
poll();
