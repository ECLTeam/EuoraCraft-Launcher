# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：生成不可变受信壳及 opaque iframe 的有限消息端口，不提供通用原生 IPC。
#
# 公开接口：
#   - build_custom_ui_shell(html, secret) — 返回只用于原生专属窗口的数据 URL。
# ============================================================

from __future__ import annotations

import base64
import json

from ECL.plugin_api.contracts import ProtocolError


def build_custom_ui_shell(html: str, secret: str) -> str:
    """
    将插件文件放进不共享来源的 iframe，壳凭据始终留在顶层闭包。

    SDK 只保留有限请求，逐个等待宿主回复；重导航关闭旧端口和窗口。原生层在精确
    顶层导航完成后调用 __eclStart，因此插件语句不会抢在原生策略之前执行。

    :param html: 已按批准快照复核的单文件 HTML
    :param secret: 宿主生成的单窗口凭据，不交付 iframe
    :return: 原生策略要求的精确顶层 URL
    :raises ProtocolError: 组合后的壳超过原生上限时抛出
    """
    sdk = """<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<script>(()=>{
 let port=null;const waiting=new Map(),outbox=[];
 const id=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
 const api=Object.freeze({call(method,params={}){return new Promise((resolve,reject)=>{
   if(waiting.size>=16)return reject(new Error('PLUGIN_RATE_LIMITED'));
   const request={id:id(),method,params,mutation_key:id()};waiting.set(request.id,{resolve,reject});
   if(port)port.postMessage(request);else outbox.push(request);
 });}});Object.defineProperty(globalThis,'ECL',{value:api});
 addEventListener('message',event=>{
   if(port||event.source!==parent||event.data!=='ecl-host-port'||event.ports.length!==1)return;
   port=event.ports[0];port.onmessage=event=>{const result=event.data,pending=waiting.get(result?.id);
     if(!pending)return;waiting.delete(result.id);if(result.ok)pending.resolve(result.data);else pending.reject(new Error(result.error_code));};
   port.start();for(const request of outbox.splice(0))port.postMessage(request);
 });
})();</script>"""
    guest = json.dumps(sdk + html, ensure_ascii=True).replace("<", "\\u003c")
    shell = """<!doctype html><meta charset="utf-8"><title>Plugin</title>
<style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block}html{overflow:hidden}</style>
<iframe id="guest" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe><script type="module">
const secret=SECRET,guest=document.getElementById('guest'),queue=[];
let port=null,current=null,uri=null,started=false,loads=0;
const id=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
function advance(){if(current||!queue.length)return;current=queue.shift();
 uri='ecl-plugin-message://request/'+secret+'?payload='+encodeURIComponent(JSON.stringify(current));
 document.title='ecl-plugin-message://request/'+secret+'?id='+current.id;}
function close(){queue.length=0;current=null;queue.push({id:id(),method:'window.close',params:{},mutation_key:id()});advance();}
Object.defineProperty(globalThis,'__eclTakeRequest',{value:()=>{const value=uri;uri=null;return value;}});
Object.defineProperty(globalThis,'__eclReply',{value:result=>{if(!current||result.id!==current.id)return;port?.postMessage(result);current=null;advance();}});
Object.defineProperty(globalThis,'__eclStart',{value:()=>{if(started)return;started=true;
 guest.onload=()=>{if(++loads!==1){port?.close();close();return;}
 const channel=new MessageChannel();port=channel.port1;port.onmessage=event=>{
   if(queue.length>=16||new TextEncoder().encode(JSON.stringify(event.data)).length>32768){close();return;}
   queue.push(event.data);advance();};port.start();guest.contentWindow.postMessage('ecl-host-port','*',[channel.port2]);};
 guest.srcdoc=GUEST;
}});
</script>""".replace("SECRET", json.dumps(secret)).replace("GUEST", guest)
    url = "data:text/html;base64," + base64.b64encode(shell.encode()).decode("ascii")
    if len(url) > 2_097_152:
        raise ProtocolError("自定义前端超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
    return url
