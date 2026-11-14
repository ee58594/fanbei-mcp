# 翻呗文档翻译 · MCP Server

<!-- mcp-name: com.sunnytranslation/fanbei -->

把翻呗的文档翻译能力接进任何支持 [MCP](https://modelcontextprotocol.io) 的 Agent：
Claude Desktop / Claude Code、Cursor、Cline、Codex，或你自己写的 Agent。

一个端点，七个工具：**列引擎 → 报价 → 上传下单 → 查进度 → 下载**。

- 服务地址：<https://trans.sunnytranslation.com>
- 支持格式：DOCX / PPTX / PDF / XLSX / TXT，ZIP / 7Z / RAR 批量包；`.doc/.xls/.ppt` 上传时自动转换
- 计价：按页扣积分，1 积分/页起（引擎与附加项不同）
- 引擎：通用模型 / DEEPL / GPT-6 系列 / Claude 5.5 / Gemini / Grok / 豆包 / DeepSeek / GLM / LongCat / 金山·阿里·有道

## 1. 拿一个访问令牌

```bash
curl -X POST https://trans.sunnytranslation.com/trans/api/token \
     -d "username=你的账号" -d "password=你的密码"
```

返回里的 `token`（`fb1.` 开头，默认 30 天有效）就是全部凭证。
**改密码会让旧令牌立即失效**——令牌签名的盐是该账号的密码哈希，服务端不存令牌表。

## 2. 配置客户端

### Claude Desktop / Claude Code（stdio）

```json
{
  "mcpServers": {
    "fanbei": {
      "command": "uvx",
      "args": ["fanbei-mcp"],
      "env": { "FANBEI_TOKEN": "fb1.xxxxx" }
    }
  }
}
```

也支持用账号密码，让服务自己换令牌（首次调用时登录一次，之后缓存到过期）：

```json
"env": { "FANBEI_USERNAME": "你的账号", "FANBEI_PASSWORD": "你的密码" }
```

### 远程接入（streamable-http）

```json
{
  "mcpServers": {
    "fanbei": {
      "type": "http",
      "url": "https://trans.sunnytranslation.com/mcp",
      "headers": { "Authorization": "Bearer fb1.xxxxx" }
    }
  }
}
```

客户端不支持自定义请求头时，也可以把令牌放在 URL 查询参数里（服务端会自动转成鉴权头，且不会把令牌继续往下游传）：

```
https://trans.sunnytranslation.com/mcp?token=fb1.xxxxx
```

自托管：

```bash
fanbei-mcp --transport http --host 127.0.0.1 --port 9010 --json-response
```

## 3. 工具

| 工具 | 说明 | 只读 |
|---|---|---|
| `fanbei_account` | 积分余额、有效期、令牌到期 | ✓ |
| `fanbei_engines` | 引擎清单、每页积分、能力边界 | ✓ |
| `fanbei_quote` | 按页数估积分，判断余额够不够 | ✓ |
| `fanbei_translate_document` | 上传并下单（可同步等待完成） | ✗ |
| `fanbei_task_status` | 单任务进度 + 子文件明细 | ✓ |
| `fanbei_list_tasks` | 任务列表（在用 / 回收站） | ✓ |
| `fanbei_download` | 10 分钟有效的下载链接，或直接落盘 | ✓ |

典型对话：

> 把 `/tmp/合同.docx` 用 GPT-6.1 Sol 翻成英文，双语对照，翻完给我下载链接。

Agent 会依次调用 `fanbei_engines` → `fanbei_quote` → `fanbei_translate_document`
→ `fanbei_task_status` → `fanbei_download`。

### 文件怎么传

| 场景 | 参数 | 说明 |
|---|---|---|
| 本地 stdio | `file_path` | 读本机文件，最大 300MB |
| 文件在公网 | `file_url` | 服务端拉取，最大 50MB |
| 远程 / 无路径 | `file_base64` + `filename` | 最大 50MB，注意上下文长度 |

## 4. 注意

- **扣费发生在下单那一刻**。`auto_confirm=false` 只上传不扣费，但也不会有进度。
- `status=3` 才是「翻译完成、可下载」；`7` 只是转换完成。
- 失败任务不会自动退款，走客服申诉。
- 传统网页引擎（金山/阿里/有道）不支持术语表与全文上下文。
- 下载链接 10 分钟过期，重新调用 `fanbei_download` 换新的。

## 5. 开发

```bash
python -m venv .venv && .venv/Scripts/pip install -e .
FANBEI_TOKEN=fb1.xxx python tests/selftest.py            # 全链路自测
FANBEI_TOKEN=fb1.xxx python tests/selftest.py --read-only # 只读自测
```

## 6. 许可与条款

- [隐私政策](PRIVACY.md) · [服务条款](TERMS.md)
- 客户文档仅用于本次翻译，翻译完成后按业务留存策略清理；不用于训练。
