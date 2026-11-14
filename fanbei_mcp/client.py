"""翻呗开放 API 的 HTTP 客户端。

只要拿到一个 Bearer 令牌，这个类不依赖任何数据库/内网 —— 因此 MCP Server 可以跑在
客户自己的机器上（stdio），也可以托管在公网（streamable-http）。
"""
from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import unquote

import httpx

DEFAULT_BASE = os.environ.get('FANBEI_BASE', 'https://trans.sunnytranslation.com')
def _default_prefix() -> str:
    """接口前缀。

    公网（默认域名）走 IIS 网关，所有接口都在 `/trans` 下；
    直连本机 9002 时没有这个前缀。

    `set FANBEI_PREFIX=` 在 .bat 里等于**删除**变量，读出来是 None 而不是空串，
    所以这里不能只靠 `os.environ.get(..., '/trans')` —— 否则本机部署会拼出
    `/trans/api/...` 打到 9002 上直接 404（踩过）。改为按 base 推断默认值，
    显式设置（非空）时以设置为准。
    """
    raw = os.environ.get('FANBEI_PREFIX')
    if raw is not None and raw.strip() != '':
        return '/' + raw.strip().strip('/')
    base = (os.environ.get('FANBEI_BASE') or DEFAULT_BASE).rstrip('/')
    local = base.startswith(('http://127.0.0.1', 'http://localhost',
                             'http://[::1]'))
    return '' if local else '/trans'


API_PREFIX = _default_prefix()
TIMEOUT = float(os.environ.get('FANBEI_TIMEOUT', '120'))
MAX_INLINE_BYTES = int(os.environ.get('FANBEI_MAX_INLINE_MB', '50')) * 1024 * 1024

# 默认**忽略** HTTP(S)_PROXY。Agent 宿主机上常见一个只服务本地端口的小代理，
# 它会把去 trans.sunnytranslation.com 的请求也接管掉（实测 502），
# 而这条链路本来就是直连公网 HTTPS。确实需要代理的环境设 FANBEI_TRUST_ENV=1。
TRUST_ENV = os.environ.get('FANBEI_TRUST_ENV', '0').strip() not in ('0', '', 'false')


class FanbeiError(RuntimeError):
    """上游返回的业务错误。带 code 便于调用方分支处理。"""

    def __init__(self, message: str, code: str = '', http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {'ok': False, 'error': str(self), 'code': self.code or 'upstream_error',
                'httpStatus': self.http_status}


def _token_from_headers(headers) -> str:
    raw = ''
    try:
        raw = headers.get('authorization') or headers.get('Authorization') or ''
    except Exception:                                            # noqa: BLE001
        raw = ''
    if raw.lower().startswith('bearer '):
        return raw[7:].strip()
    return ''


class FanbeiClient:
    """一个令牌对应一个客户账号；每个工具调用新建（成本很低，便于并发）。"""

    def __init__(self, token: str, base: str | None = None):
        if not token:
            raise FanbeiError('未配置访问令牌：请设置 FANBEI_TOKEN，'
                              '或在请求头里带 Authorization: Bearer <token>',
                              'missing_token', 401)
        self.token = token
        self.base = (base or DEFAULT_BASE).rstrip('/')
        self.prefix = API_PREFIX
        self._http = httpx.Client(
            base_url=self.base,
            timeout=TIMEOUT,
            trust_env=TRUST_ENV,
            headers={'Authorization': f'Bearer {token}',
                     'User-Agent': 'fanbei-mcp/0.1 (+https://trans.sunnytranslation.com)'},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ 基础
    def _json(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, path, **kw)
        try:
            body = r.json()
        except Exception:                                        # noqa: BLE001
            raise FanbeiError(f'上游返回非 JSON（HTTP {r.status_code}）',
                              'bad_upstream', r.status_code) from None
        if r.status_code >= 400 or body.get('ok') is False or body.get('success') is False:
            msg = body.get('error') or body.get('msg') or f'HTTP {r.status_code}'
            raise FanbeiError(msg, body.get('code') or '', r.status_code)
        return body

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ 账号
    def whoami(self) -> dict:
        return self._json('GET', f'{self.prefix}/api/whoami')

    def engines(self) -> dict:
        return self._json('GET', f'{self.prefix}/api/engines')

    def quote(self, pages: int, engine: str, compare_type: str = '',
              transfer_type: str = '') -> dict:
        return self._json('POST', f'{self.prefix}/api/quote', data={
            'pages': pages, 'engine': engine,
            'compareType': compare_type, 'transferType': transfer_type})

    # ------------------------------------------------------------------ 任务
    def tasks(self, page: int = 1, limit: int = 10, keyword: str = '',
              scope: str = 'active') -> dict:
        return self._json('POST', f'{self.prefix}/api/tasks', data={
            'page': page, 'limit': limit, 'keyword': keyword, 'scope': scope})

    def task(self, tran_id: str) -> dict:
        return self._json('POST', f'{self.prefix}/api/task', data={'tranId': tran_id})

    # ------------------------------------------------------------------ 上传/下单
    def upload(self, filename: str, content: bytes, mime: str = '') -> dict:
        r = self._http.post(f'{self.prefix}/tran/upload',
                            files={'file': (filename, content,
                                            mime or 'application/octet-stream')})
        try:
            body = r.json()
        except Exception:                                        # noqa: BLE001
            raise FanbeiError(f'上传失败（HTTP {r.status_code}）', 'upload_failed',
                              r.status_code) from None
        if not body.get('success'):
            raise FanbeiError(body.get('msg') or '上传失败', 'upload_failed', r.status_code)
        data = body.get('data')
        return data[0] if isinstance(data, list) else data

    def confirm(self, tran_ids: list[str], source_lang: str, target_lang: str,
                engine: str, compare_type: str = '0',
                transfer_type: str = 'normal') -> dict:
        body = self._json('POST', f'{self.prefix}/tranmul/confirm', data={
            'tranIds': '@'.join(tran_ids),
            'sourLang': source_lang,
            'targLang': target_lang,
            'compareType': compare_type,
            'engine': engine,
            'transferType': transfer_type,
            'defConf': '1',
        })
        return body

    # ------------------------------------------------------------------ 下载
    def download_ticket(self, tran_id: str) -> dict:
        return self._json('POST', f'{self.prefix}/api/download-ticket', data={'tranId': tran_id})

    def download_bytes(self, url: str) -> tuple[bytes, str]:
        """票据 URL 自带凭证，不要再带 Authorization（避免令牌进日志）。

        返回 (内容, 文件名)。文件名取自 Content-Disposition，拿不到就返回空串。
        """
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          trust_env=TRUST_ENV) as c:
            r = c.get(url)
            if r.status_code != 200:
                raise FanbeiError(f'下载失败（HTTP {r.status_code}）', 'download_failed',
                                  r.status_code)
            name = ''
            cd = r.headers.get('content-disposition') or ''
            if 'filename=' in cd:
                name = cd.split('filename=', 1)[1].split(';')[0].strip().strip('"')
                name = unquote(name)
            return r.content, name


# ---------------------------------------------------------------------- 令牌解析
def resolve_token(headers=None) -> str:
    """优先级：请求头 > 环境变量 FANBEI_TOKEN > 账号密码自动换令牌。

    请求头优先，是为了让一个托管实例同时服务多个客户（Claude/ChatGPT 的 OAuth
    分发最终也会走到这条路上）。
    """
    t = _token_from_headers(headers)
    if t:
        return t
    t = (os.environ.get('FANBEI_TOKEN') or '').strip()
    if t:
        return t
    user = (os.environ.get('FANBEI_USERNAME') or '').strip()
    pwd = (os.environ.get('FANBEI_PASSWORD') or '').strip()
    if user and pwd:
        return _login_cached(user, pwd)
    raise FanbeiError(
        '未配置翻呗账号：请设置环境变量 FANBEI_TOKEN（推荐），'
        '或 FANBEI_USERNAME + FANBEI_PASSWORD 由服务自动换取令牌。'
        '获取令牌：POST /trans/api/token（username/password）。',
        'missing_token', 401)


_LOGIN_CACHE: dict[str, tuple[str, float]] = {}


def _login_cached(username: str, password: str) -> str:
    import time
    hit = _LOGIN_CACHE.get(username)
    if hit and hit[1] > time.time() + 300:
        return hit[0]
    base = DEFAULT_BASE.rstrip('/')
    prefix = API_PREFIX
    r = httpx.post(f'{base}{prefix}/api/token',
                   trust_env=TRUST_ENV,
                   data={'username': username, 'password': password,
                         # 渠道随令牌一起签发，后台按它区分 MCP / Claude / Cursor
                         'channel': os.environ.get('FANBEI_CHANNEL', 'mcp'),
                         'label': os.environ.get('FANBEI_LABEL', '')},
                   timeout=30, follow_redirects=True)
    try:
        body = r.json()
    except Exception:                                            # noqa: BLE001
        raise FanbeiError('换取令牌失败：上游返回非 JSON', 'login_failed',
                          r.status_code) from None
    if not body.get('ok'):
        raise FanbeiError(body.get('error') or body.get('msg') or '换取令牌失败',
                          'login_failed', r.status_code)
    token = body['token']
    # 缓存到过期前 5 分钟，避免每次工具调用都登录一次
    _LOGIN_CACHE[username] = (token, float(body.get('expiresAtTs') or 0))
    return token


def decode_inline(content_b64: str) -> bytes:
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except Exception:                                            # noqa: BLE001
        raise FanbeiError('file_base64 不是合法的 base64', 'bad_base64', 400) from None
    if len(raw) > MAX_INLINE_BYTES:
        raise FanbeiError(f'内联文件超过 {MAX_INLINE_BYTES // 1024 // 1024}MB，'
                          '请改用 file_path（本地模式）或 file_url', 'file_too_large', 413)
    return raw


def fetch_url(url: str) -> tuple[str, bytes]:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=TRUST_ENV) as c:
        r = c.get(url)
        if r.status_code != 200:
            raise FanbeiError(f'下载源文件失败（HTTP {r.status_code}）', 'fetch_failed',
                              r.status_code)
        data = r.content
    if len(data) > MAX_INLINE_BYTES:
        raise FanbeiError(f'远程文件超过 {MAX_INLINE_BYTES // 1024 // 1024}MB',
                          'file_too_large', 413)
    name = url.split('?')[0].rstrip('/').split('/')[-1] or 'document'
    return name, data
