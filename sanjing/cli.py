"""终端是薄入口，核心函数可被未来网页调用。"""

import argparse
import os
from pathlib import Path

from . import store, model
from .agent import run_turn
from .config import DEFAULT_DB, PROVIDER_NAME
from .offline import ask_offline
from .professional_demo import ask_professional_demo, demo_data_client
from .web_demo import ask_web_demo, demo_search_client


def main():
    parser = argparse.ArgumentParser(description="三镜 Agent：聊天与可解释性账本")
    parser.add_argument("--offline", action="store_true", help="无需Key的确定性模拟模型")
    parser.add_argument("--professional-demo", action="store_true", help="专业查询与逐条引用的离线练习，不读Key")
    parser.add_argument("--web-demo", action="store_true", help="豆包搜索与字段引用的离线练习，不读Key")
    parser.add_argument("--trace", action="store_true", help="显示脱敏的消息层级和调用配对")
    parser.add_argument("--provider", choices=['ark', 'deepseek'], default=PROVIDER_NAME,
                        help="真实模式供应商；默认 Ark Responses")
    parser.add_argument("--session", help="恢复已存在的会话ID")
    parser.add_argument("--resume-prompt", action="store_true", help="在Python内读取会话ID，避免批处理拼接输入")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", help="只运行一次，便于测试")
    parser.add_argument("--show-latest", action="store_true", help="打印最近账本位置")
    parser.add_argument("--open-latest", action="store_true", help="仅用户明确选择时打开最近账本")
    args = parser.parse_args()
    if args.show_latest or args.open_latest:
        pages = sorted((args.db.parent / "ledger").glob("*.html"), key=lambda p: p.stat().st_mtime)
        print(str(pages[-1]) if pages else "还没有账本，请先运行一轮")
        if pages and args.open_latest and hasattr(os, "startfile"):
            os.startfile(str(pages[-1].resolve()))
        return
    if args.resume_prompt:
        args.session = input("请输入要恢复的会话 ID：").strip()
        if not args.session:
            print("会话 ID 不能为空")
            return
    try:
        session_id = store.ensure_session(args.db, args.session)
    except ValueError as error:
        print(str(error))
        return
    offline = args.offline or args.professional_demo or args.web_demo
    print("三镜 Agent | " + ("离线模拟：资料为合成样例" if offline else
          ('Ark ark-code-latest（Responses）' if args.provider == 'ark' else 'DeepSeek（Chat Completions）')
          + "＋DataPro＋豆包网页搜索（会产生服务用量）"))
    print("会话ID：", session_id)
    print("命令：/exit退出，/session查看ID，/why查看最近账本")
    print("示例：根据演示测评比较用户研究和产品经理助理，并显示依据。")
    print("专业查询示例：核查目标公司的登记主体和风险记录，再结合岗位要求给出待验证事项。")
    ledger_path = None
    while True:
        try:
            question = args.once if args.once is not None else input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break
        if question == "/exit":
            break
        if question == "/session":
            print(session_id)
        elif question == "/why":
            if ledger_path:
                print(ledger_path)
            else:
                turns = store.list_turns(args.db, session_id)
                from .viewer import _safe_turn_filename
                print(str(args.db.parent / "ledger" / _safe_turn_filename(turns[-1]["id"])) if turns else "本会话还没有账本")
        elif question:
            def show_progress(event):
                print(f"[{event['elapsed_seconds']:.1f}s] {event['stage']}", flush=True)
            result = run_turn(session_id, question, db_path=args.db,
                              client=ask_web_demo if args.web_demo else ask_professional_demo if args.professional_demo else
                              (ask_offline if args.offline else lambda messages, definitions: model.ask_llm(
                                  messages, definitions, provider=args.provider)),
                              data_client=demo_data_client if offline else None,
                              search_client=demo_search_client if offline else None,
                              trace=args.trace, on_progress=show_progress)
            ledger_path = result["ledger_path"]
            print("\nAgent：", result["answer"])
            print("状态：", result["status"], "｜为什么：", ledger_path)
            print("本轮用量：", result["usage"])
        if args.once is not None:
            break


if __name__ == "__main__":
    main()
