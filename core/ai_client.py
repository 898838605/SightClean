"""联网识别：只有用户自己点了才会发请求。

几条原则：
- 默认关着；没密钥、没网、对面抽风，都只回一句人话，绝不弹框、绝不抛异常。
- 发出去的内容只有「名称 + 路径」，可以选把用户名换成 %USER%，**绝不发文件内容**。
- 对面必须回一个 JSON；解析不出来就报「识别失败，请重试」，绝不自己编一段话。
- 一次只发一条（串行），带进度、能中断；一条失败不影响后面的。
- 结果按「完整路径 + 名称」存本地，下次先看本地再看网络。
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Deque, Sequence

from PySide6.QtCore import QObject, QThread, Signal

from core.config import CacheStore, Settings

# 预设服务商：(显示名, 接口地址, 默认模型)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
    "kimi": ("Kimi（月之暗面）", "https://api.moonshot.cn", "moonshot-v1-8k"),
    "openai": ("OpenAI", "https://api.openai.com", "gpt-4o-mini"),
    "custom": ("自定义", "", ""),
}

JSON_RULE = (
    "只输出一个 JSON 对象，不要任何多余文字、不要代码块、不要解释。格式如下：\n"
    '{"作用说明":"四十个字以内的中文，说清楚这是什么","删除影响":"二十个字以内，说处理掉会怎样","风险等级":"safe 或 caution 或 forbidden"}\n'
    "风险等级含义：safe=处理掉基本没影响；caution=要看情况；forbidden=处理掉会坏东西或者找不回来。"
)

SYSTEM_PROMPT = (
    "你在帮普通电脑用户判断某个文件或文件夹能不能删。"
    "你只能看到名称和路径，不要猜测文件内容，也不要编造不存在的信息。" + JSON_RULE
)

SOFTWARE_PROMPT = (
    "你在帮普通电脑用户判断一个已安装的软件能不能卸载。"
    "你只能看到软件名、发布者、版本和安装位置，不要猜测它做什么，也不要编造不存在的信息。"
    "注意：运行库、驱动、系统补丁这类组件卸掉会影响别的软件，要如实说明。" + JSON_RULE
)

REGISTRY_PROMPT = (
    "你在帮普通电脑用户判断一个注册表键能不能删。"
    "你只能看到键的完整路径和键名，不要猜测里面存了什么值，也不要编造不存在的信息。"
    "注意：注册表键可能控制软件启动、文件关联或系统行为，删错会让对应功能失效。" + JSON_RULE
)

PROCESS_PROMPT = (
    "你在帮普通电脑用户判断一个正在运行的程序能不能结束。"
    "你只能看到进程名、可执行文件路径和发布者，不要猜测它在做什么，也不要编造不存在的信息。"
    "注意：结束系统进程会让电脑蓝屏或重启；结束别的软件可能让它没保存的工作丢掉。" + JSON_RULE
)

SYSTEM_PROMPTS = {
    "file": SYSTEM_PROMPT,
    "software": SOFTWARE_PROMPT,
    "registry": REGISTRY_PROMPT,
    "process": PROCESS_PROMPT,
}

USER_TEMPLATE = "名称：{name}\n路径：{path}"
SOFTWARE_TEMPLATE = "名称：{name}\n发布者：{publisher}\n版本：{version}\n安装位置：{location}"
REGISTRY_TEMPLATE = "键名：{name}\n键路径：{path}"
PROCESS_TEMPLATE = "进程名：{name}\n可执行文件：{path}\n发布者：{publisher}"

AI_NOTE = "AI 判断，请确认"

ERR_NOT_CONFIGURED = "还没配置联网识别，先去设置里填一下"
ERR_NETWORK = "连不上，检查一下网络"
ERR_REFUSED = "这个服务拒绝了请求"
ERR_TIMEOUT = "等太久没回应"
ERR_SECURE = "安全连接没建起来，检查一下系统时间和网络"
ERR_REPLY = "识别失败，请重试"          # 对面回的不是约定的 JSON
ERR_UNKNOWN = "识别失败，请重试"
MAX_PURPOSE_LEN = 60
MAX_IMPACT_LEN = 40
MIN_TIMEOUT = 3.0
SHUTDOWN_WAIT_MS = 2000

RISK_ALIASES = {
    "safe": "safe", "ok": "safe", "low": "safe", "none": "safe",
    "安全": "safe", "放心删": "safe", "可以删": "safe", "低": "safe", "低风险": "safe",
    "caution": "caution", "warn": "caution", "medium": "caution", "unknown": "caution",
    "谨慎": "caution", "看情况": "caution", "注意": "caution", "中": "caution", "中风险": "caution",
    "forbidden": "forbidden", "danger": "forbidden", "high": "forbidden", "blocked": "forbidden",
    "禁止": "forbidden", "别删": "forbidden", "危险": "forbidden", "不可删": "forbidden",
    "高": "forbidden", "高风险": "forbidden",
}


@dataclass(frozen=True)
class AIResult:
    human_path: str
    name: str
    purpose: str
    impact: str
    risk: str          # safe / caution / forbidden
    note: str = AI_NOTE
    cached: bool = False
    risk_mapped: bool = True
    item_type: str = "file"

    def as_cache_payload(self) -> dict:
        return {
            "purpose": self.purpose,
            "impact": self.impact,
            "risk": self.risk,
            "risk_mapped": self.risk_mapped,
            "name": self.name,
        }


def _normalized_base(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def mask_path(path: str) -> str:
    """把用户目录换成 %USER%，不发真实用户名出去。"""
    home = os.environ.get("USERPROFILE") or ""
    text = str(path or "")
    if home and text.lower().startswith(home.lower()):
        return "%USER%" + text[len(home):]
    return text


def map_risk(value: object) -> tuple[str, bool]:
    """把对面回的风险等级映射成我们的三档。认不出来就按 caution。"""
    key = " ".join(str(value or "").replace("_", " ").split()).strip().lower()
    if key in RISK_ALIASES:
        return RISK_ALIASES[key], True
    for word, mapped in RISK_ALIASES.items():
        if word and word in key:
            return mapped, True
    return "caution", False


def _first_text(data: dict, keys: Sequence[str]) -> str:
    for key in keys:
        if key in data:
            value = data[key]
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
    return ""


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).replace("**", "").split())
    return flat[:limit].strip()


def parse_reply(raw: str) -> dict:
    """解析对面回的那段内容。认不出来就抛 ValueError，绝不猜。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("空的回复")
    fenced = re.search(r"\`\`\`(?:json)?\s*(.+?)\`\`\`", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start: end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("不是对象")

    purpose = _clip(_first_text(data, ("作用说明", "作用", "purpose", "effect", "说明")), MAX_PURPOSE_LEN)
    impact = _clip(_first_text(data, ("删除影响", "影响", "impact", "consequence")), MAX_IMPACT_LEN)
    risk_raw = _first_text(data, ("风险等级", "风险", "risk", "level"))
    if not purpose:
        raise ValueError("没有作用说明")
    risk, mapped = map_risk(risk_raw)
    return {"purpose": purpose, "impact": impact, "risk": risk, "risk_mapped": mapped}


def _human_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code == 429:
            return "对面太忙了，等一会儿再试"
        if code in (401, 403):
            return "这个服务拒绝了请求，可能密钥不对"
        if code >= 500:
            return "对面服务出问题了，等一会儿再试"
        return ERR_REFUSED
    reason = getattr(exc, "reason", None)
    if isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.SSLError):
        return ERR_SECURE
    if isinstance(exc, urllib.error.URLError):
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return ERR_TIMEOUT
        return ERR_NETWORK
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ERR_TIMEOUT
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return ERR_REPLY
    return ERR_UNKNOWN


@dataclass
class _Task:
    human_path: str
    name: str
    generation: int
    model: str
    base_url: str
    api_key: str
    timeout: float
    cache_key: str
    masked_send: bool = True
    # 带默认值的字段必须排在最后，否则 dataclass 直接报错
    item_type: str = "file"
    extra: dict | None = None
    temperature: float = 0.1


def _ask_service(task: _Task) -> AIResult:
    """真的去问一次。成功返回结构化结果，失败抛出去交给上层翻成人话。"""
    url = f"{task.base_url}/v1/chat/completions"
    item_type = task.item_type or "file"
    extra = task.extra or {}
    if item_type == "process":
        process_path = task.human_path or "读不到"
        if task.masked_send and process_path != "读不到":
            process_path = mask_path(process_path)
        user_text = PROCESS_TEMPLATE.format(
            name=task.name,
            path=process_path,
            publisher=extra.get("publisher") or "未知",
        )
    elif item_type == "registry":
        user_text = REGISTRY_TEMPLATE.format(name=task.name, path=task.human_path)
    elif item_type == "software":
        location = str(extra.get("location") or "")
        if task.masked_send and location:
            location = mask_path(location)
        user_text = SOFTWARE_TEMPLATE.format(
            name=task.name,
            publisher=extra.get("publisher") or "未知",
            version=extra.get("version") or "未知",
            location=location or "未知",
        )
    else:
        display_path = mask_path(task.human_path) if task.masked_send else task.human_path
        user_text = USER_TEMPLATE.format(name=task.name, path=display_path)
    body = {
        "model": task.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPTS.get(item_type, SYSTEM_PROMPT)},
            {"role": "user", "content": user_text},
        ],
        "temperature": task.temperature,
        "max_tokens": 300,
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {task.api_key}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=task.timeout) as response:
        raw = response.read()
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("回的不是那个样子")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("回的内容里找不到答案") from None
    if not isinstance(content, str):
        raise ValueError("回的不是文字")
    parsed = parse_reply(content)
    return AIResult(
        human_path=task.human_path,
        name=task.name,
        purpose=parsed["purpose"],
        impact=parsed["impact"],
        risk=parsed["risk"],
        note=AI_NOTE,
        risk_mapped=bool(parsed["risk_mapped"]),
        item_type=item_type,
    )


class _Runner(QThread):
    """一条常驻的干活线：一次只处理一条，排队来；没活就歇着。"""

    def __init__(self, client: "AIClient") -> None:
        super().__init__()
        self._client = client

    def run(self) -> None:
        while True:
            task = self._client._take_task()
            if task is None:
                if self._client._closed:
                    return
                self._client._wake.wait(0.5)
                self._client._wake.clear()
                continue
            try:
                self._client._handle(task)
            except BaseException:
                self._client._settle(task.human_path, False)


class AIClient(QObject):
    """问「这个东西是干什么的、删了会怎样」；出任何问题都只回一句人话。"""

    resultReady = Signal(object)      # AIResult
    failed = Signal(str, str)         # human_path, 人话原因
    progress = Signal(int, int, str)  # 已完成, 总数, 当前路径
    batchFinished = Signal(int, int)  # 成功数, 失败数
    busyChanged = Signal(bool)

    def __init__(self, settings: Settings, cache: CacheStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache = cache
        self._lock = threading.Lock()
        self._queue: Deque[_Task] = deque()
        self._generation = 0
        self._busy = False
        self._closed = False
        self._wake = threading.Event()
        self._thread: _Runner | None = None
        self._batch_total = 0
        self._batch_done = 0
        self._batch_ok = 0
        self._batch_fail = 0

    # 界面在设置改完之后会直接换一份设置进来
    @property
    def settings(self) -> Settings:
        return self._settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._settings = value

    # -- 配置 --------------------------------------------------------------
    def is_configured(self) -> bool:
        ai = getattr(self._settings, "ai", None)
        if ai is None:
            return False
        return bool(
            getattr(ai, "enabled", False)
            and str(getattr(ai, "base_url", "") or "").strip()
            and str(getattr(ai, "model", "") or "").strip()
            and str(getattr(ai, "api_key", "") or "").strip()
        )

    @property
    def in_batch(self) -> bool:
        return self._batch_total > 0

    # -- 对外 --------------------------------------------------------------
    def explain(
        self,
        human_path: str,
        name: str = "",
        existing_purpose: str = "",
        item_type: str = "file",
        extra: dict | None = None,
    ) -> bool:
        """识别一项。命中本地记录就直接回，否则排队。返回是否真的排上了队。

        item_type 决定发出去的字段和提示词：file 发名称+路径，software 发名称+发布者+版本+安装位置。
        """
        return self._enqueue(
            human_path,
            name or os.path.basename(str(human_path).rstrip("\\/")),
            item_type=item_type,
            extra=extra,
        )

    def explain_many(
        self,
        pairs: Sequence[tuple[str, str]],
        item_type: str = "file",
        extras: Sequence[dict] | None = None,
    ) -> int:
        """批量识别。pairs = [(identity, 名称)]。返回真正排进队列的条数。"""
        items = [(str(path or "").strip(), str(name or "").strip()) for path, name in pairs]
        items = [(path, name or os.path.basename(path.rstrip("\\/"))) for path, name in items if path]
        if not items:
            return 0
        if not self.is_configured():
            self.failed.emit("", ERR_NOT_CONFIGURED)
            return 0
        extra_list = list(extras or [])
        with self._lock:
            self._batch_total = 0
            self._batch_done = 0
            self._batch_ok = 0
            self._batch_fail = 0
        queued = 0
        for position, (path, name) in enumerate(items):
            extra = extra_list[position] if position < len(extra_list) else None
            if self._enqueue(path, name, count_batch=True, item_type=item_type, extra=extra):
                queued += 1
        with self._lock:
            self._batch_total = max(queued, 0)
            total = self._batch_total
            done = self._batch_done
        if total and done >= total:
            # 全部命中本地记录，不用真的发请求
            self.batchFinished.emit(self._batch_ok, self._batch_fail)
            with self._lock:
                self._batch_total = 0
        return queued

    def cancel_all(self) -> None:
        """清空排队；正在路上的那条回来也当没发。"""
        with self._lock:
            self._generation += 1
            self._queue.clear()
            self._batch_total = 0
            self._batch_done = 0
            self._busy = False
        self.busyChanged.emit(False)

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_all()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.quit()
            thread.wait(SHUTDOWN_WAIT_MS)
            self._thread = None

    # -- 内部 --------------------------------------------------------------
    def _settings_snapshot(self):
        ai = self._settings.ai
        try:
            timeout = max(MIN_TIMEOUT, float(getattr(ai, "timeout_seconds", 15) or 15))
        except (TypeError, ValueError):
            timeout = 15.0
        try:
            temperature = float(getattr(ai, "temperature", 0.1))
        except (TypeError, ValueError):
            temperature = 0.1
        if not (0.0 <= temperature <= 2.0):
            temperature = 0.1
        return (
            str(ai.model).strip(),
            _normalized_base(ai.base_url),
            str(ai.api_key).strip(),
            timeout,
            bool(getattr(ai, "mask_username", True)),
            temperature,
        )

    def _enqueue(
        self,
        human_path: str,
        name: str,
        count_batch: bool = False,
        item_type: str = "file",
        extra: dict | None = None,
    ) -> bool:
        path = str(human_path or "").strip()
        if not path or self._closed:
            return False
        if not self.is_configured():
            self.failed.emit(path, ERR_NOT_CONFIGURED)
            if count_batch:
                self._settle(path, False)
            return False

        model, base_url, api_key, timeout, masked, temperature = self._settings_snapshot()
        name = name or os.path.basename(path.rstrip("\\/"))
        item_type = str(item_type or "file").strip().lower()
        cache_key = ""
        try:
            cache_key = CacheStore.ai_key_item(item_type, path, name)
            cached = self._cache.get_ai_json(cache_key)
        except Exception:  # noqa: BLE001
            cached = None

        if isinstance(cached, dict) and cached.get("purpose"):
            result = AIResult(
                human_path=path,
                name=name,
                purpose=str(cached.get("purpose") or ""),
                impact=str(cached.get("impact") or ""),
                risk=str(cached.get("risk") or "caution"),
                note=AI_NOTE,
                cached=True,
                risk_mapped=bool(cached.get("risk_mapped", True)),
                item_type=item_type,
            )
            self.resultReady.emit(result)
            if count_batch:
                self._settle(path, True)
            return True

        with self._lock:
            for waiting in self._queue:
                if waiting.human_path == path and waiting.cache_key == cache_key:
                    return False        # 已经在排队了，不重复问
            task = _Task(
                human_path=path,
                name=name,
                generation=self._generation,
                item_type=item_type,
                extra=dict(extra or {}),
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                cache_key=cache_key,
                masked_send=masked,
                temperature=temperature,
            )
            self._queue.append(task)
            became_busy = not self._busy
            self._busy = True
        if became_busy:
            self.busyChanged.emit(True)
        self._wake.set()
        self._ensure_runner()
        return True

    def _take_task(self) -> _Task | None:
        with self._lock:
            if not self._queue:
                return None
            task = self._queue.popleft()
        if task.generation != self._generation:
            return None      # 已经作废的批次，安静丢掉
        return task

    def _stale(self, generation: int) -> bool:
        return generation != self._generation

    def _handle(self, task: _Task) -> None:
        if self._stale(task.generation):
            self._settle(task.human_path, False, silent=True)
            return
        try:
            result = _ask_service(task)
        except BaseException as exc:  # noqa: BLE001
            if not self._stale(task.generation):
                self.failed.emit(task.human_path, _human_error(exc))
            self._settle(task.human_path, False)
            return
        if self._stale(task.generation):
            self._settle(task.human_path, False, silent=True)
            return
        try:
            self._cache.put_ai_json(task.cache_key, result.as_cache_payload(), task.model)
        except Exception:  # noqa: BLE001
            pass
        self.resultReady.emit(result)
        self._settle(task.human_path, True)

    def _settle(self, human_path: str, ok: bool, silent: bool = False) -> None:
        """一条活干完了：更新批次进度，必要时报一段进度。"""
        with self._lock:
            batching = self._batch_total > 0
            done = total = ok_count = fail_count = 0
            finished = False
            if batching:
                self._batch_done += 1
                self._batch_ok += 1 if ok else 0
                self._batch_fail += 0 if ok else 1
                done, total = self._batch_done, self._batch_total
                ok_count, fail_count = self._batch_ok, self._batch_fail
                finished = done >= total
                if finished:
                    self._batch_total = 0
                    self._batch_done = 0
                idle = finished and not self._queue
            else:
                self._busy = bool(self._queue)
                idle = not self._queue

        if batching:
            if not silent:
                self.progress.emit(done, total, human_path)
            if finished:
                self.batchFinished.emit(ok_count, fail_count)
                if idle:
                    self.busyChanged.emit(False)
        elif idle:
            self.busyChanged.emit(False)

    def _ensure_runner(self) -> None:
        if self._thread is not None:
            return
        thread = _Runner(self)
        thread.start()
        self._thread = thread
