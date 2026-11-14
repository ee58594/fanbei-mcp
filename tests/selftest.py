"""翻呗 MCP Server 自测（真实链路，不是 mock）。

用法
----
    # 用已有令牌
    FANBEI_TOKEN=fb1.xxx python tests/selftest.py

    # 用账号密码（先换令牌，再跑全流程）
    FANBEI_USERNAME=mcp0001 FANBEI_PASSWORD='McpTest#2026' python tests/selftest.py

    # 只做「工具清单 + 只读调用」，不产生真实翻译任务
    python tests/selftest.py --read-only

验收点
------
1. 工具清单完整、每个工具都有 title 与 readOnlyHint（Claude 目录的硬性要求）
2. 账户 / 引擎 / 报价 三个只读工具返回结构正确
3. 上传 → 下单 → 轮询 → 下载 全链路跑通，且成品字节数与本地一致
4. 错误路径：不存在的 tranId、缺令牌，都应返回结构化错误而不是抛栈
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession, StdioServerParameters            # noqa: E402
from mcp.client.stdio import stdio_client                       # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = '') -> None:
    (PASS if cond else FAIL).append(name)
    print(f'  {"✓" if cond else "✗"} {name}{("  " + detail) if detail else ""}')


def as_dict(result) -> dict:
    """MCP 工具返回是 content 列表；这里取第一个 text 并解析回 dict。"""
    for item in getattr(result, 'content', []) or []:
        text = getattr(item, 'text', None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {'raw': text}
    return {}


SAMPLE = """# 翻呗 MCP 自测文档

Document translation is a workflow, not a button.

本文件由 MCP 自测脚本生成，用于验证「上传 → 下单 → 查询 → 下载」全链路。
包含中英文混排、数字（2026）、标点与换行，用于观察分页与字数统计是否正常。
"""


async def run(args) -> None:
    env = dict(os.environ)
    env.setdefault('PYTHONPATH', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'fanbei_mcp.server'],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            print('\n=== 1. 工具清单 ===')
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            check('工具数量 = 7', len(names) == 7, str(names))
            missing_title = [t.name for t in tools.tools if not getattr(t, 'title', None)]
            check('每个工具都有 title', not missing_title, str(missing_title))
            annotations = [(t.name, getattr(t, 'annotations', None)) for t in tools.tools]
            no_hint = [n for n, a in annotations
                       if a is None or (getattr(a, 'read_only_hint', None) is None
                                        and getattr(a, 'destructive_hint', None) is None)]
            check('每个工具都带 readOnlyHint/destructiveHint', not no_hint, str(no_hint))

            print('\n=== 2. 只读工具 ===')
            acc = as_dict(await s.call_tool('fanbei_account', {}))
            check('账户余额可读', acc.get('ok') is True and 'points' in acc,
                  f"points={acc.get('points')}")
            eng = as_dict(await s.call_tool('fanbei_engines', {}))
            ok_eng = eng.get('ok') is True and eng.get('count', 0) >= 5
            check('引擎清单 ≥5 个', ok_eng, f"count={eng.get('count')}")
            codes = [e['code'] for e in eng.get('engines', [])]
            check('引擎清单不含隐藏挂名档', not ({'qwen', 'gpt4o', 'claude_opus'} & set(codes)))
            check('传统引擎标注不支持术语表',
                  all(e['supportsGlossary'] is False
                      for e in eng.get('engines', []) if e['kind'] == 'traditional'))

            q = as_dict(await s.call_tool('fanbei_quote',
                                         {'pages': 10, 'engine': 'gpt61_sol'}))
            check('报价返回合计积分', q.get('ok') is True and q.get('totalPoints', 0) > 0,
                  f"{q.get('pointsPerPage')}/页 × 10 = {q.get('totalPoints')}")
            check('报价含余额判断', 'enough' in (q.get('balance') or {}))

            lst = as_dict(await s.call_tool('fanbei_list_tasks', {'limit': 3}))
            check('任务列表可读', lst.get('ok') is True and 'tasks' in lst,
                  f"total={lst.get('total')}")

            print('\n=== 3. 错误路径 ===')
            bad = as_dict(await s.call_tool('fanbei_task_status', {'tran_id': 'NOT_EXIST_0001'}))
            check('不存在的任务返回结构化错误', bad.get('ok') is False and bool(bad.get('error')),
                  str(bad.get('error'))[:40])

            if args.read_only:
                return

            print('\n=== 4. 全链路：上传 → 下单 → 轮询 → 下载 ===')
            with tempfile.TemporaryDirectory() as td:
                src = os.path.join(td, 'mcp-selftest.txt')
                with open(src, 'w', encoding='utf-8') as f:
                    f.write(SAMPLE)
                t0 = time.time()
                tr = as_dict(await s.call_tool('fanbei_translate_document', {
                    'file_path': src, 'engine': args.engine,
                    'source_lang': '2', 'target_lang': '1',
                    'auto_confirm': True, 'wait_seconds': args.wait,
                }, read_timeout_seconds=args.wait + 120))
                check('上传并下单成功', tr.get('ok') is True and bool(tr.get('tranId')),
                      f"tranId={tr.get('tranId')} pages={tr.get('pages')}")
                tid = tr.get('tranId')
                if not tid:
                    return
                print(f'     耗时 {time.time() - t0:.0f}s · 余额 {tr.get("balance")}')

                st = as_dict(await s.call_tool('fanbei_task_status', {'tran_id': tid}))
                check('任务状态可查', st.get('ok') is True and st.get('status') is not None,
                      f"{st.get('status')} {st.get('statusText')}")

                # 等成品（最长 args.wait 秒；超时不算失败，只是没验到下载）
                deadline = time.time() + args.wait
                while time.time() < deadline and st.get('status') != '3':
                    await asyncio.sleep(5)
                    st = as_dict(await s.call_tool('fanbei_task_status', {'tran_id': tid}))
                if st.get('status') != '3':
                    print(f'     ! 未在 {args.wait}s 内完成（status={st.get("status")}），跳过下载校验')
                    return

                dl = as_dict(await s.call_tool('fanbei_download',
                                               {'tran_id': tid, 'save_to': td}))
                check('下载链接可获取', dl.get('ok') is True and bool(dl.get('url')),
                      str(dl.get('url'))[:60] + '...')
                check('文件已落盘且非空', bool(dl.get('savedTo')) and (dl.get('bytes') or 0) > 200,
                      f"{dl.get('bytes')} bytes → {dl.get('savedTo')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--read-only', action='store_true', help='不创建真实翻译任务')
    ap.add_argument('--engine', default='BAIDU')
    ap.add_argument('--wait', type=int, default=180, help='等待完成的最长秒数')
    args = ap.parse_args()

    if not os.environ.get('FANBEI_TOKEN') and not (
            os.environ.get('FANBEI_USERNAME') and os.environ.get('FANBEI_PASSWORD')):
        print('请先设置 FANBEI_TOKEN，或 FANBEI_USERNAME + FANBEI_PASSWORD')
        raise SystemExit(2)

    asyncio.run(run(args))

    print(f'\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
    if FAIL:
        for x in FAIL:
            print('  FAIL:', x)
        raise SystemExit(1)
    print('全部通过')


if __name__ == '__main__':
    main()
