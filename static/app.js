'use strict';
const $ = (id) => document.getElementById(id);
let details;
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error(`请求失败 (${response.status})`); }
  if (!response.ok) throw new Error(data.error || data.message || '请求失败');
  return data;
}
function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').classList.toggle('failure', error);
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('toast').hidden = true; }, 6000);
}
function tab(name) {
  document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.id === name));
  document.querySelectorAll('nav button').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
  if (name === 'dashboard' && !$('dashboard-frame').getAttribute('src')) $('dashboard-frame').src = '/dashboard';
}
async function load() {
  details = await api('/manage/info');
  $('login').hidden = true;
  $('console').hidden = false;
  $('vless').value = details.vless_uri;
  $('proxy-user').value = details.proxy_user;
  $('proxy-password').value = details.proxy_password;
  $('api-token').value = details.api_token;
  $('http-proxy').value = details.http_proxy_uri;
  $('https-proxy').value = details.https_proxy_uri;
  $('copy-http').disabled = !details.http_proxy_uri;
  $('copy-https').disabled = !details.https_proxy_uri;
  $('proxy-hint').textContent = (details.https_proxy_self_signed
    ? 'HTTPS 代理使用自动生成的自签名证书，客户端需信任下载的代理证书。'
    : 'HTTPS 代理使用自定义证书，请确保客户端信任其签发机构。')
    + ' HTTP(S) 链接仅在配置实际公网 TCP 地址后生成；管理页登录密码不用于代理认证。';
  $('api-url').textContent = details.public_url + '/api';
  $('subscription').value = (details.subscription_urls || (details.subscription_url ? [details.subscription_url] : [])).join('\n');
  $('core-version').textContent = details.versions.mihomo.version;
  $('ui-version').textContent = details.versions.zashboard.version;
  $('status').textContent = details.status === 'ready' ? '● 内核运行中' : '○ 内核启动中';
  const config = await api('/manage/config');
  $('yaml').value = config.text;
}
async function apply(button, action) {
  const text = button.textContent;
  $('toast').hidden = true;
  button.disabled = true;
  button.textContent = '检查与应用中…';
  try {
    await action();
    await load();
    $('dashboard-frame').removeAttribute('src');
    if ($('dashboard').classList.contains('active')) $('dashboard-frame').src = '/dashboard';
    toast('配置已生效');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = text; }
}
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $('login-error').textContent = '';
  try {
    await api('/manage/login', { method: 'POST', body: JSON.stringify({ password: $('password').value }) });
    $('password').value = '';
    await load();
  } catch (error) { $('login-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('logout').onclick = async () => { await api('/manage/logout', { method: 'POST', body: '{}' }); location.reload(); };
document.querySelectorAll('[data-tab]').forEach(el => { el.onclick = () => tab(el.dataset.tab); });
document.querySelectorAll('[data-go]').forEach(el => { el.onclick = () => tab(el.dataset.go); });
$('open-dashboard').onclick = () => tab('dashboard');
for (const [button, field] of [['copy-vless', 'vless'], ['copy-http', 'http-proxy'], ['copy-https', 'https-proxy']]) {
  $(button).onclick = async () => {
    try { await navigator.clipboard.writeText($(field).value); toast('连接链接已复制'); }
    catch {
      if ($(field).tagName === 'INPUT') $(field).type = 'text';
      $(field).select();
      toast('请复制选中的链接');
    }
  };
}
$('show-secrets').onclick = () => {
  for (const id of ['proxy-password', 'api-token', 'http-proxy', 'https-proxy']) $(id).type = $(id).type === 'password' ? 'text' : 'password';
};
$('url-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const urls = $('subscription').value.split(/\r?\n/).map(url => url.trim()).filter(Boolean);
  apply(event.submitter, () => api('/manage/config', { method: 'POST', body: JSON.stringify({ urls }) }));
});
$('refresh').onclick = () => apply($('refresh'), () => api('/manage/refresh', { method: 'POST', body: '{}' }));
$('yaml-form').addEventListener('submit', (event) => {
  event.preventDefault();
  apply(event.submitter, () => api('/manage/config', { method: 'POST', body: JSON.stringify({ text: $('yaml').value }) }));
});
$('file').onchange = async () => {
  const file = $('file').files[0];
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) { toast('配置超过 5 MiB', true); return; }
  $('yaml').value = await file.text();
};
load().catch(() => { $('login').hidden = false; $('console').hidden = true; });
