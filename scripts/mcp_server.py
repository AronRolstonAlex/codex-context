#!/usr/bin/env python3
"""Experimental local MCP Apps entry point; statistics only, no network.

Only generic text is model-visible. The small numeric view is carried in MCP
result _meta for the host UI. No tool can enable detailed analysis or return
message, command, path, or tool-output content.
"""
from __future__ import annotations

import json
import sys

from context_core import aggregate, codex_home, load_sessions

URI = 'ui://codex-context/overview-v1.html'
WIDGET = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>body{font:15px/1.5 system-ui,sans-serif;color:#e6e8ef;background:#171a23;margin:0;padding:20px}
h1{font-size:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.card{background:#242939;border-radius:9px;padding:12px}.value{font-size:22px}button{padding:8px;margin-top:14px}
small{color:#b3b9c8}</style></head><body><h1>上下文洞察 · 统计</h1>
<p>仅显示本地汇总；不读取对话正文、命令参数、工具输出或工作目录。</p>
<div class="grid"><div class="card">会话<div class="value" id="sessions">—</div></div>
<div class="card">轮次<div class="value" id="turns">—</div></div>
<div class="card">累计 token<div class="value" id="tokens">—</div></div>
<div class="card">工具调用<div class="value" id="tools">—</div></div></div>
<button id="refresh" disabled>刷新统计</button><p><small id="note">等待宿主加载数据。数据仅为已读取文件的统计，不代表实时窗口。更详细分析请在本机运行报告命令并逐次确认。</small></p>
<script>
(function(){let next=0;const pending=new Map();
function send(method,params){return new Promise(function(resolve,reject){const id=++next;pending.set(id,{resolve,reject});window.parent.postMessage({jsonrpc:'2.0',id:id,method:method,params:params},'*')})}
function notify(method,params){window.parent.postMessage({jsonrpc:'2.0',method:method,params:params},'*')}
function display(result){const data=result&&result._meta&&result._meta['context-insight'];if(!data)return;
for(const key of ['sessions','turns','tokens','tools'])document.getElementById(key).textContent=data[key]===null?'—':String(data[key]);
document.getElementById('note').textContent='统计模式；累计 token 不是上下文窗口占用。更详细分析请在本机运行报告命令并逐次确认。'}
window.addEventListener('message',function(event){if(event.source!==window.parent)return;const msg=event.data;if(!msg||msg.jsonrpc!=='2.0')return;
if(typeof msg.id==='number'&&pending.has(msg.id)){const p=pending.get(msg.id);pending.delete(msg.id);if(msg.error)p.reject(msg.error);else p.resolve(msg.result)}
else if(msg.method==='ui/notifications/tool-result')display(msg.params)});
const button=document.getElementById('refresh');button.addEventListener('click',function(){button.disabled=true;send('tools/call',{name:'context_dashboard',arguments:{}}).then(display).catch(function(){document.getElementById('note').textContent='宿主未能刷新；请使用离线报告。'}).finally(function(){button.disabled=false})});
send('ui/initialize',{appInfo:{name:'codex-context',version:'0.2.0'},appCapabilities:{},protocolVersion:'2026-01-26'}).then(function(){notify('ui/notifications/initialized',{});button.disabled=false}).catch(function(){document.getElementById('note').textContent='此宿主未提供 MCP App 界面；请使用离线报告。'});
})();</script></body></html>'''

TOOL = {
    'name': 'context_dashboard',
    'title': '上下文洞察（统计）',
    'description': '打开本地统计面板。仅汇总元数据；不支持详细分析或读取会话正文。',
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
    '_meta': {
        'ui': {'resourceUri': URI},
        'openai/ui': {'entrypoints': [{'type': 'global'}, {'type': 'thread'}]},
    },
}


def dashboard() -> dict:
    agg = aggregate(load_sessions(codex_home(), limit=30))
    available = any(session.snapshots or session.deltas for session in agg.sessions)
    return {
        'content': [{'type': 'text', 'text': '本地统计面板已请求。详细内容不会通过此工具提供。'}],
        '_meta': {'context-insight': {
            'sessions': agg.session_count, 'turns': agg.turns_total,
            'tokens': agg.totals['total'] if available else None,
            'tools': agg.tool_calls_total,
        }},
    }


def dispatch(method: str, params: object) -> dict:
    p = params if isinstance(params, dict) else {}
    if method == 'initialize':
        requested = p.get('protocolVersion')
        protocol = requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-06-18'
        return {'protocolVersion': protocol, 'capabilities': {'tools': {}, 'resources': {}},
                'serverInfo': {'name': 'codex-context-insight', 'version': '0.2.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [TOOL]}
    if method == 'resources/list':
        return {'resources': [{'uri': URI, 'name': '上下文洞察', 'mimeType': 'text/html;profile=mcp-app'}]}
    if method == 'resources/read' and p.get('uri') == URI:
        return {'contents': [{'uri': URI, 'mimeType': 'text/html;profile=mcp-app', 'text': WIDGET,
                              '_meta': {'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}}}}]}
    if method == 'tools/call' and p.get('name') == 'context_dashboard' and p.get('arguments', {}) == {}:
        return dashboard()
    raise ValueError('Method or arguments not supported')


def main() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or not isinstance(req.get('method'), str):
                continue
            if 'id' not in req:
                continue
            try:
                result = dispatch(req['method'], req.get('params'))
                reply = {'jsonrpc': '2.0', 'id': req['id'], 'result': result}
            except Exception:
                # Do not print exception paths or local data into MCP responses.
                reply = {'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32601, 'message': 'Method unavailable'}}
            print(json.dumps(reply, ensure_ascii=False), flush=True)
        except (ValueError, TypeError):
            continue


if __name__ == '__main__':
    main()
