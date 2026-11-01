"""Workflow 核心抽象：消息、消息总线、共享状态（黑板）、事件日志、Agent 基类。

本模块提供 Workflow 编排所需的全部基础设施：

- ``Message``      ：Agent 之间传递的结构化消息；
- ``MessageBus``   ：发布/订阅式消息总线（Agent 间通信）；
- ``SharedState``  ：跨节点共享的状态黑板（状态管理）；
- ``EventLog``     ：执行日志与状态追踪；
- ``BaseAgent``    ：所有 Agent 节点的基类。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
@dataclass
class Message:
    """Agent 之间传递的一条消息。"""

    sender: str
    receiver: str
    kind: str                      # 消息类型，如 requirement / design / code / review
    payload: Dict[str, Any]        # 消息内容
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "kind": self.kind,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }

    def __repr__(self) -> str:  # 便于日志阅读
        return f"Message({self.sender}->{self.receiver}, kind={self.kind})"


# ---------------------------------------------------------------------------
# 消息总线
# ---------------------------------------------------------------------------
class MessageBus:
    """进程内发布/订阅消息总线。

    每个 Agent 订阅自己关心的话题（topic = 消息 kind），
    上游 Agent 通过 ``publish`` 发出消息，总线负责投递给
    对应订阅者并记录全部消息历史（可用于调试与审计）。
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List["BaseAgent"]] = {}
        self.history: List[Message] = []

    def subscribe(self, topic: str, agent: "BaseAgent") -> None:
        self._subscribers.setdefault(topic, []).append(agent)

    def publish(self, message: Message) -> List[Message]:
        """发布消息，返回所有订阅者产生的回复消息。"""
        self.history.append(message)
        replies: List[Message] = []
        for agent in self._subscribers.get(message.kind, []):
            if agent.name == message.sender:
                continue  # 不把消息投递回发送者自己
            reply = agent.receive(message)
            if reply is not None:
                replies.append(reply)
        return replies

    def topics(self) -> List[str]:
        return sorted(self._subscribers.keys())


# ---------------------------------------------------------------------------
# 共享状态（黑板模式）
# ---------------------------------------------------------------------------
class SharedState:
    """Workflow 级别的共享状态黑板。

    - ``set/get/update``：按 key 读写；
    - ``snapshot``：导出当前全部状态（供追踪与序列化）；
    - 所有写入都会追加到 ``changes`` 历史，便于回溯。
    """

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self._data: Dict[str, Any] = dict(initial or {})
        self.changes: List[Dict[str, Any]] = []
        # Parallel 节点会在多线程中读写同一份状态，用可重入锁保证读改写原子性
        self._lock = threading.RLock()

    def set(self, key: str, value: Any, source: str = "system") -> None:
        with self._lock:
            self._data[key] = value
            self.changes.append({"key": key, "source": source, "at": _now()})

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, mapping: Dict[str, Any], source: str = "system") -> None:
        with self._lock:
            for k, v in mapping.items():
                self.set(k, v, source)

    def append_error(self, node: str, error: str) -> None:
        """原子追加一条错误记录。

        ``get -> append -> set`` 在多线程下会互相覆盖，因此错误汇总统一走这里。
        """
        with self._lock:
            errors = list(self._data.get("errors") or [])
            errors.append({"node": node, "error": error})
            self._data["errors"] = errors
            self.changes.append({"key": "errors", "source": node, "at": _now()})

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data


# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------
class EventLog:
    """执行日志与状态追踪。

    记录五类事件：``node_start`` / ``node_end`` / ``iteration`` / ``error`` / ``message``，
    支持导出为文本报告或 JSON。

    ``live=True`` 时事件会同时实时打印到控制台（CLI 用它避免长时间无输出）。
    """

    #: 类级开关：置 True 后所有实例都实时回显（CLI 启动时打开）
    live: bool = False

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def record(self, kind: str, node: str, detail: str = "", **extra: Any) -> None:
        event = {"at": _now(), "kind": kind, "node": node, "detail": detail, **extra}
        self.events.append(event)
        if self.live or type(self).live:
            try:
                print(self._format(event), flush=True)
            except Exception:  # noqa: BLE001 - 控制台编码异常不应影响流程
                pass

    @staticmethod
    def _mark(kind: str) -> str:
        return {
            "node_start": "->",
            "node_end": "<=",
            "iteration": "##",
            "error": "!!",
            "message": "~~",
        }.get(kind, "??")

    def _format(self, event: Dict[str, Any]) -> str:
        return (
            f"  {event['at']} {self._mark(event['kind'])} "
            f"[{event['node']}] {event['kind']}: {event['detail']}"
        )

    def node_start(self, node: str) -> None:
        self.record("node_start", node, "开始执行")

    def node_end(self, node: str, detail: str = "执行成功") -> None:
        self.record("node_end", node, detail)

    def error(self, node: str, detail: str) -> None:
        self.record("error", node, detail)

    def to_text(self) -> str:
        lines = ["[执行日志]"]
        for e in self.events:
            lines.append(self._format(e))
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(self.events, ensure_ascii=False, indent=2)

    def __len__(self) -> int:
        return len(self.events)


# ---------------------------------------------------------------------------
# Agent 基类
# ---------------------------------------------------------------------------
class BaseAgent:
    """所有 Workflow 节点的基类。

    子类只需实现 ``process(self, message, state)``：
    - 输入：收到的消息 + 共享状态；
    - 输出：``Message``（返回给下一个节点）或 ``None``（终止）。

    基类负责：消息订阅路由、异常兜底、日志记录。
    """

    name: str = "agent"
    role: str = "通用 Agent"

    def __init__(self, bus: Optional[MessageBus] = None, log: Optional[EventLog] = None) -> None:
        self.bus = bus
        self.log = log
        self.inbox: List[Message] = []

    # -- 通信 ---------------------------------------------------------------
    def subscribe_to(self, *topics: str) -> "BaseAgent":
        assert self.bus is not None, "未绑定 MessageBus，无法订阅"
        for topic in topics:
            self.bus.subscribe(topic, self)
        return self

    def send(self, kind: str, payload: Dict[str, Any], to: str = "next") -> Message:
        assert self.bus is not None, "未绑定 MessageBus，无法发送"
        return Message(sender=self.name, receiver=to, kind=kind, payload=payload)

    def receive(self, message: Message) -> Optional[Message]:
        """总线入口：记录日志并调用子类逻辑，异常在此兜底。"""
        self.inbox.append(message)
        if self.log is not None:
            self.log.record("message", self.name, f"收到 {message.kind} <- {message.sender}")
        try:
            return self.process(message, None)
        except Exception as exc:  # 单个 Agent 异常不应炸掉整个流程
            # 带上异常类型：NotImplementedError() 这类空消息否则会显示为空白
            detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
            if self.log:
                self.log.error(self.name, f"处理消息 {message.kind} 失败：{detail}")
            return self.send(
                kind="error",
                payload={"agent": self.name, "error": detail, "input_kind": message.kind},
            )

    # -- 业务逻辑（子类实现）-------------------------------------------------
    def process(self, message: Message, state: Any = None) -> Optional[Message]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


__all__ = ["Message", "MessageBus", "SharedState", "EventLog", "BaseAgent"]
