"""Agent with Goal - 在 tool agent 外层增加一个简单 goal loop"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.llm import call_llm
from core.node import Node, Flow, shared
from tools import get_tools, Tool, ToolExecutor

SYSTEM_PROMPT = (
    "你是一个会调用工具的助手。"
    "当问题涉及最新信息、模型版本、产品发布时间或事实核验时，优先先调用 search 工具，再基于搜索结果回答。"
    "若问题是本地文件/代码相关，优先使用 read/grep/find/ls 等本地工具。"
    "如果一轮回复中既需要向用户展示文字又需要继续调用工具，可以同时返回 content 和 tool_calls。"
)


@dataclass
class GoalState:
    """教学版 goal 状态：只保留最核心的字段。"""

    text: str | None = None
    active: bool = False


def goal_message(goal: GoalState) -> dict[str, str]:
    """创建写入历史的 goal 提醒消息。"""
    return {
        "role": "user",
        "content": (
            "Complete this goal fully:\n\n"
            f"{goal.text}\n\n"
            "Treat the goal text above as the whole task. Do not infer extra file, code, "
            "or project work unless the goal explicitly asks for it. Do not stop at only "
            "a plan, partial progress, or suggested next steps. Use tools only when the "
            "goal explicitly requires them. If this is a simple chat goal, reply directly. "
            "When the goal is fully complete and verified, call goal_complete."
        ),
    }


class ChatNode(Node):
    """调用 LLM，打印 assistant content，并按 tool_calls 决定 agent loop 是否继续。"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        messages = shared["messages"]

        assistant_message = call_llm(
            messages=messages,
            tools=shared["tools"],
            system_prompt=SYSTEM_PROMPT,
        )
        messages.append(assistant_message)

        content = assistant_message["content"]
        tool_calls = assistant_message.get("tool_calls")

        if content:
            print(f"\n🤖 Assistant: {content}\n")

        if tool_calls:
            return "tool_call", assistant_message

        return "done", assistant_message


class ToolCallNode(Node):
    """执行 LLM 返回的 tool_calls。"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        response = payload
        messages = shared["messages"]
        executor = shared["tool_executor"]

        tool_calls = executor.parse_tool_calls(response)
        results = executor.execute_all(tool_calls)

        for tc, result in zip(tool_calls, results):
            print(f"  [Tool] 执行: {tc.name}({tc.arguments})")
            print(f"  [Tool] 结果: {result.content[:100]}...")
            messages.append(result.to_message())

        return "chat", None


def make_goal_complete_tool(goal: GoalState) -> Tool:
    """创建 goal_complete 工具。"""

    def goal_complete() -> str:
        if not goal.active:
            return "No active goal"
        goal.text = None
        goal.active = False
        return "Goal complete"

    return Tool(
        name="goal_complete",
        description=(
            "Mark the active goal as complete. "
            "Only call this after the goal is fully finished and verified. "
            "If no goal is active, this tool does nothing."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=goal_complete,
    )


def run_goal(flow: Flow, goal: GoalState) -> None:
    """一直运行 agent loop，直到 goal_complete 把 goal.active 改成 False。"""
    while goal.active:
        shared["messages"].append(goal_message(goal))
        flow.run(None)


def run_chat() -> None:
    """运行对话循环。"""
    print("=" * 60)
    print("🤖 Agent with Goal")
    print("=" * 60)
    print("可用工具: read, write, edit, bash, grep, find, ls, search")
    print("Goal 命令: /goal <goal>, /goal status, /goal clear")
    print("输入 'quit' 或 'exit' 退出\n")

    goal = GoalState()
    executor = ToolExecutor()
    goal_tool = make_goal_complete_tool(goal)
    executor.tools.append(goal_tool)
    executor.tool_map[goal_tool.name] = goal_tool

    shared.clear()
    shared["messages"] = []
    shared["goal"] = goal
    shared["tools"] = [t.to_llm_format() for t in get_tools()]
    shared["tools"].append(goal_tool.to_llm_format())
    shared["tool_executor"] = executor

    chat = ChatNode()
    tool_call = ToolCallNode()

    chat - "tool_call" >> tool_call
    tool_call - "chat" >> chat
    flow = Flow(chat)

    while True:
        user_input = input("👤 You: ").strip()

        if user_input.lower() in ("quit", "exit", "q"):
            print("\n再见！")
            break

        if not user_input:
            continue

        if user_input.startswith("/goal"):
            command = user_input.removeprefix("/goal").strip()

            if not command or command == "status":
                if goal.text:
                    print(f"\n🎯 Goal: {goal.text}\nActive: {goal.active}\n")
                else:
                    print("\n🎯 No active goal. Use /goal <goal> to start one.\n")
                continue

            if command == "clear":
                goal.text = None
                goal.active = False
                print("\n🎯 Goal cleared.\n")
                continue

            goal.text = command
            goal.active = True
            print(f"\n🎯 Goal started: {goal.text}\n")
            run_goal(flow, goal)
            continue

        shared["messages"].append({"role": "user", "content": user_input})
        flow.run(None)


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        print("⚠️  提示：请先设置环境变量 OPENAI_API_KEY 和 OPENAI_BASE_URL")
        return

    run_chat()


if __name__ == "__main__":
    main()
