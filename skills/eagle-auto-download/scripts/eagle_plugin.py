#!/usr/bin/env python3
"""Read-only diagnostics for Eagle's optional official MCP/Skill HTTP service."""
import argparse
import json
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

READ_TOOLS = {'get_app_info', 'item_query', 'item_get', 'item_get_selected', 'item_count',
              'folder_get', 'tag_get', 'tag_count', 'tag_group_get', 'smart_folder_get',
              'smart_folder_get_items', 'smart_folder_get_rules', 'ai_search_status',
              'ai_search_by_text', 'ai_search_by_item'}


def checked_result(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        raise RuntimeError(str(result.get('error', '插件未返回成功状态')) if isinstance(result, dict)
                           else '插件响应结构异常')
    if ('successCount' in result and 'totalCount' in result and
            result['successCount'] != result['totalCount']):
        raise RuntimeError('插件批量操作只有部分项目成功')
    if isinstance(result.get('data'), list) and any(
            isinstance(row, dict) and row.get('success') is False for row in result['data']):
        raise RuntimeError('插件逐项结果含失败项')
    return result


class Plugin:
    def __init__(self, server='http://127.0.0.1:41596'):
        p = urlsplit(server)
        if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', 'localhost', '::1') or
                p.username or p.password or p.query or p.fragment or p.path not in ('', '/')):
            raise ValueError('插件诊断仅连接本机 HTTP 服务')
        self.server = server.rstrip('/')
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data=None):
        payload = json.dumps(data).encode() if data is not None else None
        req = Request(self.server + path, data=payload, headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=30) as response:
            if urlsplit(response.url).hostname != urlsplit(self.server).hostname:
                raise ValueError('本机插件请求发生了未预期跳转')
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('插件结果过大，请缩小查询范围')
        return json.loads(raw)

    def tools(self):
        data = self.request('/api/tools/list')
        if not isinstance(data.get('tools'), dict):
            raise ValueError('插件工具清单格式异常')
        return data['tools']

    def call(self, name, params):
        if name not in READ_TOOLS:
            raise ValueError('此诊断脚本仅提供只读工具；写操作仍需现有目标库保护及逐项验收')
        if name not in self.tools():
            raise ValueError('该工具当前未启用或当前插件未提供')
        return checked_result(self.request('/api/tools/call', {'tool': name, 'params': params}))

    def diagnose(self):
        status = self.request('/api/status')
        tools = self.tools()
        result = {'version': status.get('version'), 'status': status.get('status'),
                  'enabled': status.get('mcpEnabled'), 'enabled_tool_count': len(tools),
                  'disabled_tools': status.get('disabledTools', []),
                  'available_read_tools': sorted(set(tools) & READ_TOOLS)}
        if 'ai_search_status' in tools and status.get('mcpEnabled'):
            result['ai_search'] = checked_result(self.request('/api/tools/call',
                                      {'tool': 'ai_search_status', 'params': {}}))['data']
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--server', default='http://127.0.0.1:41596')
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('diagnose')
    sub.add_parser('tools')
    help_parser = sub.add_parser('help')
    help_parser.add_argument('tool')
    call = sub.add_parser('call')
    call.add_argument('tool')
    call.add_argument('--json', default='{}')
    args = ap.parse_args()
    try:
        client = Plugin(args.server)
        if args.command == 'diagnose':
            result = client.diagnose()
        elif args.command == 'tools':
            result = {'tools': sorted(client.tools())}
        elif args.command == 'help':
            result = client.tools()[args.tool]
        else:
            params = json.loads(args.json)
            if not isinstance(params, dict):
                raise ValueError('工具参数必须是 JSON 对象')
            result = client.call(args.tool, params)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
