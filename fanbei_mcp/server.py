"""翻呗文档翻译 · MCP Server。

一个 MCP 端点，把「上传 → 报价 → 下单翻译 → 查进度 → 下载成品」这条链路
暴露给任意支持 MCP 的 Agent（Claude、Cursor、Codex、自建 Agent 等）。

启动
----
    fanbei-mcp                       # stdio（本地客户端，如 Claude Desktop / Cursor）
    fanbei-mcp --transport http      # streamable-http，监听 127.0.0.1:9010/mcp

鉴权
----
三种任选，优先级从高到低：
    1. 请求头 `Authorization: Bearer <token>`（托管多租户时用）
    2. 环境变量 `FANBEI_TOKEN`
    3. 环境变量 `FANBEI_USERNAME` + `FANBEI_PASSWORD`（自动换取并缓存令牌）

令牌获取：`POST https://trans.sunnytranslation.com/trans/api/token`
"""
from __future__ import annotations

import argparse
import base64
import os
import time
from urllib.parse import unquote, urlsplit, parse_qsl, urlencode

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from .client import (DEFAULT_BASE, FanbeiClient, FanbeiError, decode_inline,
                     fetch_url, resolve_token)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                         idempotent_hint=False, open_world_hint=True)

INSTRUCTIONS = """\
翻呗（Fanbei）文档翻译服务。支持 DOCX / PPTX / PDF / XLSX / TXT，以及 ZIP/7Z/RAR 批量包；
老格式 .doc/.xls/.ppt 上传时自动转 OOXML。

典型流程：
  1. fanbei_engines       看有哪些翻译引擎与单价（1 积分/页起）
  2. fanbei_quote         按页数估积分，确认余额够用
  3. fanbei_translate_document  上传并下单（默认自动确认开始翻译）
  4. fanbei_task_status   轮询进度（status=3 即完成）
  5. fanbei_download      取 10 分钟有效的下载链接，或直接存到本地

规则提醒：
  · 每页扣积分，扣费发生在下单那一刻；失败任务不会自动退款，需走客服申诉。
  · 传统网页引擎（金山/阿里/有道）不支持术语表与全文上下文，长文质量低于模型档。
  · 下载链接 10 分钟过期，需要时重新调用 fanbei_download 换新链接。
"""

mcp = MCPServer(
    name='fanbei',
    title='翻呗文档翻译',
    description='文档翻译：上传 → 报价 → 下单 → 查进度 → 下载，支持指定翻译引擎。',
    instructions=INSTRUCTIONS,
    version='0.1.0',
    website_url='https://trans.sunnytranslation.com',
)


def _client(ctx: Context | None) -> FanbeiClient:
    headers = getattr(ctx, 'headers', None) if ctx is not None else None
    return FanbeiClient(resolve_token(headers))


def _brief_task(t: dict) -> dict:
    """工具返回要克制：一屏能读完，不要把整行原始数据全塞给模型。"""
    out = {
        'tranId': t.get('tranId'),
        'sourceFile': t.get('sourceFile'),
        'status': t.get('status'),
        'statusText': t.get('statusText'),
        'progressPercent': t.get('progressPercent'),
        'pages': t.get('pages'),
        'wordCount': t.get('wordCount'),
        'engine': t.get('engineName') or t.get('engine'),
        'downloadable': t.get('downloadable'),
        'updateTime': t.get('updateTime'),
    }
    if t.get('children'):
        out['children'] = t['children']
    return {k: v for k, v in out.items() if v is not None}


def _wait(client: FanbeiClient, tran_id: str, seconds: int, interval: float = 5.0) -> dict:
    """轮询到完成/失败/超时。返回最后一次任务快照。"""
    deadline = time.time() + max(0, seconds)
    last = None
    while True:
        last = client.task(tran_id)['task']
        if str(last.get('status')) in ('3', '5', '6', '9'):
            return last
        if time.time() >= deadline:
            return last
        time.sleep(min(interval, max(0.5, deadline - time.time())))


# ---------------------------------------------------------------- 工具
@mcp.tool(title='账户余额', annotations=READ_ONLY,
          description='查询当前令牌对应账号的积分余额、有效期与令牌到期时间。')
def fanbei_account(ctx: Context) -> dict:
    try:
        with _client(ctx) as c:
            d = c.whoami()
    except FanbeiError as e:
        return e.as_dict()
    return {'ok': True, **d['account'], 'tokenExpiresAt': d['token']['expiresAt']}


@mcp.tool(title='可选翻译引擎', annotations=READ_ONLY,
          description='列出可用于翻译的引擎（模型档与传统网页引擎）、每页积分与能力边界。'
                      'code 用于其它工具的 engine 参数。')
def fanbei_engines(ctx: Context) -> dict:
    try:
        with _client(ctx) as c:
            d = c.engines()
    except FanbeiError as e:
        return e.as_dict()
    return {'ok': True, 'count': d['count'],
            'engines': [{k: e[k] for k in ('code', 'name', 'group', 'kind',
                                           'pointsPerPage', 'supportsGlossary', 'desc')}
                        for e in d['engines']]}


@mcp.tool(title='翻译报价', annotations=READ_ONLY,
          description='按页数与选项估算积分消耗，并检查余额是否够用。下单前先报个价。')
def fanbei_quote(ctx: Context, pages: int, engine: str = 'BAIDU',
                 bilingual: bool = False, ocr: bool = False) -> dict:
    from .client import FanbeiError as _E
    compare = '1' if bilingual else '0'
    transfer = 'ocr_normal' if ocr else 'normal'
    with _client(ctx) as c:
        try:
            d = c.quote(pages, engine, compare, transfer)
        except _E as e:
            return e.as_dict()
    return {'ok': True, **d['quote'], 'balance': d['balance']}


@mcp.tool(title='翻译文档', annotations=CREATE,
          description='上传一个文档并下单翻译。文件来源三选一：file_path（本地路径，'
                      'stdio 模式用）、file_url（公网直链）、file_base64（≤50MB，远程模式用）。'
                      'auto_confirm=True 会立刻扣积分并开始翻译；False 只上传返回 tranId。'
                      'wait_seconds>0 会同步等待到完成或超时（超时仍可用 fanbei_task_status 继续查）。')
def fanbei_translate_document(
    ctx: Context,
    file_path: str = '',
    file_url: str = '',
    file_base64: str = '',
    filename: str = '',
    engine: str = 'BAIDU',
    source_lang: str = '1',
    target_lang: str = '2',
    bilingual: bool = False,
    ocr: bool = False,
    auto_confirm: bool = True,
    wait_seconds: int = 0,
) -> dict:
    name, content = '', b''
    try:
        if file_path:
            if not os.path.isfile(file_path):
                return {'ok': False, 'error': f'文件不存在：{file_path}', 'code': 'no_file'}
            name = filename or os.path.basename(file_path)
            with open(file_path, 'rb') as f:
                content = f.read()
        elif file_url:
            name, content = fetch_url(file_url)
            name = filename or name
        elif file_base64:
            content = decode_inline(file_base64)
            name = filename or 'document.docx'
        else:
            return {'ok': False, 'code': 'no_input',
                    'error': '请提供 file_path / file_url / file_base64 之一'}
        if len(content) > 300 * 1024 * 1024:
            return {'ok': False, 'code': 'file_too_large', 'error': '文件超过 300MB'}

        with _client(ctx) as c:
            up = c.upload(name, content)
            tran_id = up['tranId']
            pages, words = up.get('pages'), up.get('wordCount')
            result = {
                'ok': True, 'tranId': tran_id, 'sourceFile': up.get('sourceFile'),
                'pages': pages, 'wordCount': words,
                'engine': engine, 'bilingual': bilingual, 'ocr': ocr,
            }
            if not auto_confirm:
                result['status'] = 'uploaded'
                result['next'] = '调用 fanbei_translate_document 之外的确认接口需再次下单；' \
                                 '建议直接用 auto_confirm=True'
                return result

            c.confirm([tran_id], source_lang, target_lang, engine,
                      '1' if bilingual else '0',
                      'ocr_normal' if ocr else 'normal')
            result['status'] = 'submitted'
            result['statusText'] = '已提交翻译'
            result['chargedPoints'] = (pages or 0) * 1  # 精确值以下方 balance 为准
            try:
                result['balance'] = c.whoami()['account']['points']
            except FanbeiError:
                pass
            if wait_seconds > 0:
                t = _wait(c, tran_id, wait_seconds)
                result['task'] = _brief_task(t)
                result['status'] = t.get('status')
                result['statusText'] = t.get('statusText')
                result['finished'] = str(t.get('status')) == '3'
                if str(t.get('status')) == '3':
                    result['next'] = '已完成，调用 fanbei_download 取下载链接'
                elif str(t.get('status')) in ('5', '6', '9'):
                    result['next'] = '翻译失败：可调用 /trans/tran/reTran 重译（不扣积分）或联系客服'
                else:
                    result['next'] = '仍在进行，继续用 fanbei_task_status 轮询'
            else:
                result['next'] = '用 fanbei_task_status 轮询进度（status=3 即完成）'
            return result
    except FanbeiError as e:
        return e.as_dict()


@mcp.tool(title='查询任务进度', annotations=READ_ONLY,
          description='按 tranId 查单个任务的状态、进度百分比与子文件明细。'
                      'status=3 表示翻译完成、可以下载。')
def fanbei_task_status(ctx: Context, tran_id: str) -> dict:
    try:
        with _client(ctx) as c:
            d = c.task(tran_id)['task']
    except FanbeiError as e:
        return e.as_dict()
    out = {'ok': True, **_brief_task(d)}
    if d.get('files'):
        out['files'] = d['files'][:50]
    return out


@mcp.tool(title='任务列表', annotations=READ_ONLY,
          description='分页列出我的翻译任务（默认只看在用的）。scope 可选 active/trash/all。')
def fanbei_list_tasks(ctx: Context, page: int = 1, limit: int = 10,
                      keyword: str = '', scope: str = 'active') -> dict:
    try:
        with _client(ctx) as c:
            d = c.tasks(page, min(max(limit, 1), 50), keyword, scope)
    except FanbeiError as e:
        return e.as_dict()
    return {'ok': True, 'total': d['total'], 'page': d['page'],
            'tasks': [_brief_task(t) for t in d['tasks']]}


@mcp.tool(title='下载译文', annotations=READ_ONLY,
          description='取成品的下载链接（10 分钟有效）。save_to 给本地目录时，'
                      'stdio 模式下会直接把文件写盘并返回路径。')
def fanbei_download(ctx: Context, tran_id: str, save_to: str = '') -> dict:
    try:
        with _client(ctx) as c:
            d = c.download_ticket(tran_id)
            out = {'ok': True, 'tranId': tran_id, 'url': d['url'],
                   'expiresAt': d['expiresAt']}
            if save_to:
                os.makedirs(save_to, exist_ok=True)
                content, name = c.download_bytes(d['url'])
                path = os.path.join(save_to, name or f'{tran_id}_translated')
                with open(path, 'wb') as f:
                    f.write(content)
                out['savedTo'] = os.path.abspath(path)
                out['bytes'] = len(content)
            return out
    except FanbeiError as e:
        return e.as_dict()


# ---------------------------------------------------------------- 健康检查
@mcp.custom_route('/healthz', methods=['GET'])
async def _healthz(request):                                     # noqa: ARG001
    """给看门狗/负载均衡用：不需要令牌，不碰上游，只证明进程活着。"""
    return JSONResponse({'status': 'ok', 'service': 'fanbei-mcp'})


@mcp.custom_route('/.well-known/mcp/server-card.json', methods=['GET'])
async def _server_card(request):                                 # noqa: ARG001
    """静态服务卡（SEP-1649）：Smithery 等目录扫描时，Bearer 头鉴权会挡住
    协议级 tools/list，这里免鉴权吐一份工具清单兜底，目录页就不会空白。
    工具列表直接从注册表生成，与实际暴露的工具永远一致。"""
    try:
        tools = await mcp.list_tools()
        items = []
        for t in tools:
            d = t.model_dump(by_alias=True)          # wire 格式：inputSchema 等别名键
            items.append({
                'name': d.get('name'),
                'title': d.get('title') or d.get('name'),
                'description': d.get('description') or '',
                'inputSchema': d.get('inputSchema') or {'type': 'object', 'properties': {}},
            })
    except Exception:
        items = []
    return JSONResponse({
        'serverInfo': {'name': 'fanbei', 'title': '翻呗文档翻译', 'version': '0.1.0'},
        'authentication': {'required': True, 'schemes': ['bearer']},
        'tools': items,
        'resources': [],
        'prompts': [],
    })


# ---------------------------------------------------------------- 入口


class AuthGate:
    """HTTP 层兜底：没有 Authorization 直接 401，不进 MCP 协议栈。

    为什么要在传输层拦：工具里当然也会报「缺少令牌」，但那样客户端拿到的是一个
    **成功的 JSON-RPC 响应**，Claude/ChatGPT 的鉴权探测会认为「这个服务不需要登录」，
    目录审核也会判为未配置鉴权。401 + WWW-Authenticate 才是正确的握手信号。

    只拦 MCP 路径；/healthz 放行，否则看门狗和探活都会被 401 打红。
    """

    def __init__(self, app, path: str):
        self.app = app
        self.path = path.rstrip('/') or '/mcp'

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            path = scope.get('path') or ''
            method = scope.get('method') or 'GET'
            if (path == self.path or path.startswith(self.path + '/')) and method != 'OPTIONS':
                headers = {k.lower(): v for k, v in scope.get('headers') or []}
                auth = headers.get(b'authorization')
                if not auth:
                    # 兼容不支持自定义请求头的客户端（部分国内客户端、claude.ai 的
                    # 添加连接器弹窗等）：允许 URL 查询参数携带令牌 token= / access_token=。
                    # 命中后注入 Authorization 头，并把令牌从 query_string 里剥掉，
                    # 避免令牌继续留在下游日志里。
                    pairs = parse_qsl(scope.get('query_string') or b'', keep_blank_values=True)
                    tok = next((v for k, v in pairs if k in ('token', 'access_token') and v), None)
                    if tok:
                        auth = f'Bearer {unquote(tok)}'.encode('latin-1')
                        kept = [(k, v) for k, v in pairs if k not in ('token', 'access_token')]
                        scope = dict(scope)
                        scope['query_string'] = urlencode(kept).encode('latin-1')
                        headers = dict(headers)
                        headers[b'authorization'] = auth
                        scope['headers'] = list(headers.items())
                if not auth:
                    body = (b'{"error":"missing_token","message":'
                            b'"Authorization: Bearer <token> required. '
                            b'Get one at POST /trans/api/token"}')
                    await send({'type': 'http.response.start', 'status': 401,
                                'headers': [(b'content-type', b'application/json'),
                                            (b'www-authenticate',
                                             b'Bearer realm=\"fanbei\", error=\"invalid_token\"')]})
                    await send({'type': 'http.response.body', 'body': body})
                    return
        await self.app(scope, receive, send)
def main() -> None:
    ap = argparse.ArgumentParser(prog='fanbei-mcp', description='翻呗文档翻译 MCP Server')
    ap.add_argument('--transport', default='stdio', choices=['stdio', 'http'])
    ap.add_argument('--host', default=os.environ.get('FANBEI_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('FANBEI_PORT', '9010')))
    ap.add_argument('--path', default=os.environ.get('FANBEI_PATH', '/mcp'))
    ap.add_argument('--json-response', action='store_true',
                    help='streamable-http 用 JSON 响应（便于前置反向代理，不依赖 SSE）')
    args = ap.parse_args()

    if args.transport == 'stdio':
        mcp.run('stdio')
        return

    # 无状态 HTTP：每个请求自带 Authorization，实例不保存会话 —— 便于前置 Nginx/IIS
    # 反向代理与水平扩容；json_response 让代理不必处理 SSE 流。
    # 这里自己起 uvicorn（而不是 mcp.run），是为了在传输层套一层 AuthGate 并提供
    # /healthz；行为与 mcp.run('streamable-http') 一致。
    import uvicorn

    app = mcp.streamable_http_app(
        streamable_http_path=args.path,
        json_response=bool(args.json_response),
        stateless_http=True,
        host=args.host,
    )
    uvicorn.run(AuthGate(app, args.path), host=args.host, port=args.port,
                log_level=os.environ.get('FANBEI_LOG_LEVEL', 'info').lower())


if __name__ == '__main__':
    main()
