const $ = (id) => document.getElementById(id);
const state = { access: null, runId: sessionStorage.getItem("finscope.run"), timer: null };
const labels = { running:"运行中", pause_requested:"待暂停", paused:"已暂停", resuming:"恢复中", failed:"执行失败", completed:"执行完成" };

async function request(path, options = {}, retry = true) {
  const headers = { "Content-Type":"application/json", ...(options.headers || {}) };
  if (state.access) headers.Authorization = `Bearer ${state.access}`;
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (response.status === 401 && retry) {
    const rotated = await fetch("/api/auth/refresh", { method:"POST", credentials:"same-origin" });
    if (rotated.ok) { saveTokens(await rotated.json()); return request(path, options, false); }
  }
  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail || `请求失败 (${response.status})`);
  return body;
}
function saveTokens(tokens){ state.access=tokens.access_token; }
function clearSession(){ sessionStorage.removeItem("finscope.run"); state.access=state.runId=null; clearInterval(state.timer); location.reload(); }
function showConsole(){ $("login-view").hidden=true; $("console-view").hidden=false; $("logout").hidden=false; }
function renderRun(run){
  $("run-company").textContent=run.company; $("run-question").textContent=run.question; $("progress").textContent=`${run.progress}%`; $("state-version").textContent=run.state_version; $("budget-used").textContent=run.budget_used;
  const chip=$("status-chip"); chip.textContent=labels[run.status] || run.status; chip.className=`status ${run.status}`;
  document.querySelectorAll("[data-state]").forEach(node => node.classList.toggle("active",node.dataset.state===run.status));
  $("pause").disabled=run.status!=="running"; $("resume").disabled=run.status!=="paused";
  if(run.report){ $("report").textContent=run.report.markdown; $("report-hash").textContent=run.report.content_hash.slice(0,12); }
  else $("report").textContent="尚无报告。运行完成后将展示经 citation gate 验证的输出。";
}
async function loadRun(){ if(!state.runId)return; try{const run=await request(`/research/${state.runId}`); renderRun(run); if(["completed","failed"].includes(run.status)) clearInterval(state.timer);}catch(error){$("research-error").textContent=error.message;} }
function startPolling(){ clearInterval(state.timer); loadRun(); state.timer=setInterval(loadRun,1500); }

$("login-form").addEventListener("submit",async(event)=>{event.preventDefault();$("login-error").textContent="";try{const tokens=await request("/auth/login",{method:"POST",body:JSON.stringify({tenant_id:$("tenant-id").value.trim(),email:$("email").value.trim(),password:$("password").value})},false);saveTokens(tokens);showConsole();startPolling();}catch(error){$("login-error").textContent=error.message;}});
$("research-form").addEventListener("submit",async(event)=>{event.preventDefault();$("research-error").textContent="";try{const run=await request("/research",{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({company:$("company").value.trim(),symbol:$("symbol").value.trim(),market:$("market").value,question:$("question").value.trim(),depth:$("depth").value,budget_limit:Number($("budget").value)})});state.runId=run.run_id;sessionStorage.setItem("finscope.run",state.runId);startPolling();}catch(error){$("research-error").textContent=error.message;}});
$("pause").addEventListener("click",async()=>{try{renderRun(await request(`/research/${state.runId}/pause`,{method:"POST"}));startPolling();}catch(error){$("research-error").textContent=error.message;}});
$("resume").addEventListener("click",async()=>{try{await request(`/research/${state.runId}/resume`,{method:"POST"});startPolling();}catch(error){$("research-error").textContent=error.message;}});
$("logout").addEventListener("click",async()=>{try{await request("/auth/logout",{method:"POST"},false);}finally{clearSession();}});

(async()=>{try{const health=await request("/health",{},false);$("runtime-label").textContent=`${health.database} · ${health.research_executor}`;$("runtime-label").parentElement.classList.add("ok");}catch{$("runtime-label").textContent="正式运行时不可用";}try{const rotated=await fetch("/api/auth/refresh",{method:"POST",credentials:"same-origin"});if(rotated.ok){saveTokens(await rotated.json());await request("/auth/me");showConsole();startPolling();}}catch{state.access=null;}})();
