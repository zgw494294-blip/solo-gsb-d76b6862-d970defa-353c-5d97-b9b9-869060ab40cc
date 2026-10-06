"""样本片段服务。

单进程内存态的 HTTP 服务：按会话维护连续编号的整数样本，
支持以当前末条为中心触发片段（前 before 条 + 中心 + 后 after 条），
片段等后续样本到齐后完成；最近 H 条历史与所有未删除片段共同占用容量 C。
会话运行中可不停采样地调整 H 与 C：校验通过才生效并按新参数回收，
任一校验失败则拒绝且不改变任何状态。

可选的整数模式识别：会话可在首条样本写入前配置一次长度 1～32 的整数
pattern，此后每条成功写入的样本经流式 KMP 自动机精确匹配（允许重叠）。
自动机只保存标量状态，不保存原始样本、不占用样本容量，故样本回收与
片段增删均不影响识别进度；命中记录在样本回收后仍然保留。

会话可在任意时刻分叉为一条独立续采的新会话：复制当时的 H、C、存活样本、
全部未删除片段及其编号、下一样本与片段编号，以及模式配置、自动机进度、
累计命中与最近 100 次命中记录。分叉只复制存活状态：已回收样本不恢复，
也不重放样本重新识别（直接复制自动机标量状态）。分叉在来源会话锁内完成
取号与快照，等效于与来源已有操作串行执行的某个时刻；来源保持不变，
两支此后的写入、触发、删除、调参与模式操作互不影响。不提供合并与落盘。
"""
from __future__ import annotations

import asyncio
from collections import deque

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="sample-fragment-service", version="1.0.0")


# ---------------- 请求模型 ----------------

class CreateSessionRequest(BaseModel):
    history: int = Field(ge=1, le=1000, description="历史长度 H")
    capacity: int = Field(ge=1, le=1000, description="样本容量 C")

    @model_validator(mode="after")
    def _check_h_le_c(self) -> "CreateSessionRequest":
        if self.history > self.capacity:
            raise ValueError("必须满足 1 <= H <= C <= 1000")
        return self


class UpdateLimitsRequest(BaseModel):
    history: int = Field(description="新的历史长度 H")
    capacity: int = Field(description="新的样本容量 C")

    @model_validator(mode="before")
    @classmethod
    def _check_types(cls, data: object) -> object:
        # 显式校验，确保布尔值、字符串、浮点等不会被宽松转换为整数（返回 422）
        if not isinstance(data, dict):
            raise ValueError("请求体必须是包含 history 和 capacity 的对象")
        for key in ("history", "capacity"):
            if key not in data or type(data[key]) is not int:
                raise ValueError("history 和 capacity 必须是整数，不能为布尔值或字符串")
        return data

    @model_validator(mode="after")
    def _check_range(self) -> "UpdateLimitsRequest":
        if not (1 <= self.history <= self.capacity <= 1000):
            raise ValueError("必须满足 1 <= H <= C <= 1000")
        return self


class WriteSampleRequest(BaseModel):
    value: int = Field(description="整数样本值")


class TriggerRequest(BaseModel):
    before: int = Field(ge=0, description="中心之前的条数（非负整数）")
    after: int = Field(ge=0, description="中心之后的条数（非负整数）")


class ConfigurePatternRequest(BaseModel):
    pattern: list[int] = Field(description="待识别的整数模式，长度 1～32；元素必须为整数")

    @model_validator(mode="before")
    @classmethod
    def _check_pattern(cls, data: object) -> object:
        # 显式校验，确保布尔值、字符串、浮点等不会被宽松转换为整数（返回 422）
        if not isinstance(data, dict) or not isinstance(data.get("pattern"), list):
            raise ValueError("pattern 必须是整数数组，长度 1～32")
        pattern = data["pattern"]
        if not (1 <= len(pattern) <= 32):
            raise ValueError("pattern 长度必须在 1～32 之间")
        if any(type(x) is not int for x in pattern):
            raise ValueError("pattern 元素必须是整数，不能为布尔值或字符串")
        return data


# ---------------- 流式模式匹配（KMP 自动机） ----------------

class PatternMatcher:
    """只保存标量状态的流式 KMP：逐样本喂入，不保存原始样本。

    state 为当前匹配到的 pattern 前缀长度；feed 在恰好完整匹配时返回
    True，否则返回 False。失败回退允许重叠命中。pattern 可以长于 H 和 C：
    进度只依赖自动机状态，与样本是否被回收无关。
    """

    __slots__ = ("pattern", "failure", "state")

    def __init__(self, pattern: list[int]):
        self.pattern = pattern
        m = len(pattern)
        failure = [0] * m
        k = 0
        for i in range(1, m):
            while k > 0 and pattern[i] != pattern[k]:
                k = failure[k - 1]
            if pattern[i] == pattern[k]:
                k += 1
            failure[i] = k
        self.failure = failure
        self.state = 0

    def feed(self, value: int) -> bool:
        m = len(self.pattern)
        k = self.state
        while k > 0 and value != self.pattern[k]:
            k = self.failure[k - 1]
        if value == self.pattern[k]:
            k += 1
        if k == m:
            self.state = self.failure[k - 1]  # 回退以支持重叠命中
            return True
        self.state = k
        return False


# ---------------- 会话状态 ----------------

class Fragment:
    __slots__ = ("fragment_id", "center", "before", "after")

    def __init__(self, fragment_id: int, center: int, before: int, after: int):
        self.fragment_id = fragment_id
        self.center = center
        self.before = before
        self.after = after

    @property
    def start(self) -> int:
        return self.center - self.before

    @property
    def end(self) -> int:
        return self.center + self.after


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


class Session:
    """单个会话的全部内存状态；所有操作在 self.lock 下串行执行。"""

    def __init__(self, session_id: int, history: int, capacity: int):
        self.session_id = session_id
        self.history = history            # H
        self.capacity = capacity          # C
        self.next_seq = 1                 # 下一条样本的序号（从 1 连续编号）
        self.samples: dict[int, int] = {}  # 当前存活的样本：序号 -> 值
        self.fragments: dict[int, Fragment] = {}  # 未删除的片段
        self.next_fragment_id = 1
        # 模式识别：仅首条样本写入前可配置一次；状态不占用样本容量
        self.pattern: list[int] | None = None
        self.matcher: PatternMatcher | None = None
        self.pattern_configured = False
        self.hit_count = 0
        self.recent_hits: deque[dict] = deque(maxlen=100)  # 最近 100 次命中
        self.lock = asyncio.Lock()

    # ---- 容量核算 ----

    def needed_seqs(self, upto: int, history: int | None = None) -> set[int]:
        """假设已写入 upto 条时，历史窗口与所有未删除片段共同需要的序号集合。

        history 缺省为当前 H；显式传入时按该长度核算（用于调整参数的预检）。
        同序号只计一次（集合语义）；片段只统计已写入的部分，
        未写入的后续样本在将来写入时再参与核算。
        """
        h = self.history if history is None else history
        lo = max(1, upto - h + 1)
        needed = set(range(lo, upto + 1))
        for frag in self.fragments.values():
            hi = min(frag.end, upto)
            if frag.start <= hi:
                needed.update(range(frag.start, hi + 1))
        return needed

    def reclaim(self) -> None:
        """回收历史窗口与所有未删除片段都不再需要的样本。"""
        live = self.needed_seqs(self.next_seq - 1)
        for seq in list(self.samples):
            if seq not in live:
                del self.samples[seq]

    # ---- 运行中调整 H / C ----

    def adjust_limits(self, history: int, capacity: int) -> None:
        """不停采样地调整 H 与 C；任一校验失败抛 409 且不改变任何状态。

        全部通过后按新参数回收不再需要的样本。片段及其内容、样本与片段
        编号、模式配置与命中记录均不受影响；扩容不会恢复已回收的样本，
        旧片段的 before 即使不小于新 H 也原样保留（仅约束后续触发）。
        """
        upto = self.next_seq - 1
        # 新历史窗口要求的最近 min(H, 已写入条数) 条必须全部仍存活
        lo = max(1, upto - history + 1)
        if any(seq not in self.samples for seq in range(lo, upto + 1)):
            raise _conflict("新历史窗口包含已回收的样本，无法调整")
        # 未删除片段的总长不能超过新容量
        if any(f.before + 1 + f.after > capacity for f in self.fragments.values()):
            raise _conflict("存在总长超过新容量的未删除片段，无法调整")
        # 新历史窗口与片段已写入部分的序号并集不能超过新容量
        if len(self.needed_seqs(upto, history)) > capacity:
            raise _conflict("新历史与未删除片段所需序号的并集超过新容量，无法调整")
        self.history = history
        self.capacity = capacity
        self.reclaim()

    # ---- 模式识别 ----

    def configure_pattern(self, pattern: list[int]) -> None:
        """配置识别模式；重复配置或首条样本已写入后配置均抛 409。"""
        if self.pattern_configured:
            raise _conflict("该会话已配置过 pattern，不可重复配置")
        if self.next_seq > 1:
            raise _conflict("首条样本已写入，pattern 仅能在首条样本写入前配置一次")
        self.pattern = list(pattern)
        self.matcher = PatternMatcher(self.pattern)
        self.pattern_configured = True

    def observe(self, seq: int, value: int) -> None:
        """样本成功提交后喂入自动机；命中则追加记录（与写样本同一串行临界区）。"""
        if self.matcher is not None and self.matcher.feed(value):
            assert self.pattern is not None
            m = len(self.pattern)
            self.hit_count += 1
            self.recent_hits.append({"start_seq": seq - m + 1, "end_seq": seq})

    # ---- 操作 ----

    def write(self, value: int) -> int:
        """写入一条样本；容量不足时抛 409，不消耗序号、不改变历史或片段。

        容量拒绝时不推进匹配状态、不产生命中记录；只有成功提交才喂入自动机，
        因此同一值被拒绝后重试成功只会被识别一次。
        """
        n = self.next_seq
        needed = self.needed_seqs(n)
        if len(needed) > self.capacity:
            raise _conflict("容量不足：新样本无处容纳，序号未消耗，状态未改变")
        # 不变式：needed ⊆ 现有 samples ∪ {n}，故此处不会 KeyError
        kept = {seq: self.samples[seq] for seq in needed if seq != n}
        kept[n] = value
        self.samples = kept
        self.next_seq = n + 1
        self.observe(n, value)
        return n

    def trigger(self, before: int, after: int) -> Fragment:
        """以当前末条为中心创建片段；历史不足时抛 409。"""
        if before >= self.history:
            raise HTTPException(
                status_code=400,
                detail=f"before 必须小于历史长度 H={self.history}",
            )
        if before + 1 + after > self.capacity:
            raise HTTPException(
                status_code=400,
                detail=f"before+1+after 不能超过样本容量 C={self.capacity}",
            )
        center = self.next_seq - 1
        if center - before < 1:
            raise _conflict("历史不足：已有样本不足以覆盖中心及其前 before 条")
        frag = Fragment(self.next_fragment_id, center, before, after)
        self.fragments[frag.fragment_id] = frag
        self.next_fragment_id += 1
        return frag

    def status_of(self, frag: Fragment) -> str:
        return "completed" if frag.end < self.next_seq else "pending"

    # ---- 分叉 ----

    def fork(self, new_id: int) -> "Session":
        """以当前快照派生出一条独立续采的新会话。

        复制 H、C、存活样本、全部未删除片段及其编号、下一样本与片段编号，
        以及模式配置、自动机标量进度、累计命中与最近 100 次命中记录。
        只复制存活状态：已回收样本不恢复，也不重放样本重新识别——自动机的
        state/failure 与 hit_count、recent_hits 原样拷贝，两支后续识别各自独立。
        调用方保证在来源锁内取号并调用本方法，故快照对应与来源操作串行的
        某个确定时刻；来源自身不做任何修改。
        """
        child = Session(new_id, self.history, self.capacity)
        child.next_seq = self.next_seq
        child.samples = dict(self.samples)
        child.fragments = {
            fid: Fragment(f.fragment_id, f.center, f.before, f.after)
            for fid, f in self.fragments.items()
        }
        child.next_fragment_id = self.next_fragment_id
        child.pattern_configured = self.pattern_configured
        if self.pattern is not None:
            child.pattern = list(self.pattern)
            child.matcher = PatternMatcher(child.pattern)
            child.matcher.failure = list(self.matcher.failure)
            child.matcher.state = self.matcher.state
        child.hit_count = self.hit_count
        child.recent_hits = deque(self.recent_hits, maxlen=100)
        return child


# ---------------- 会话注册表（单进程内存） ----------------

class Registry:
    def __init__(self) -> None:
        self.sessions: dict[int, Session] = {}
        self.next_id = 1


registry = Registry()


def _get_session(session_id: int) -> Session:
    sess = registry.sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return sess


def _session_summary(sess: Session) -> dict:
    return {
        "session_id": sess.session_id,
        "history": sess.history,
        "capacity": sess.capacity,
        "next_seq": sess.next_seq,
        "used": len(sess.samples),
        "stored_seqs": sorted(sess.samples),
        "fragments": sorted(sess.fragments),
    }


def _fragment_summary(sess: Session, frag: Fragment) -> dict:
    return {
        "fragment_id": frag.fragment_id,
        "center": frag.center,
        "before": frag.before,
        "after": frag.after,
        "start": frag.start,
        "end": frag.end,
        "status": sess.status_of(frag),
    }


def _fragment_detail(sess: Session, frag: Fragment) -> dict:
    # 片段引用且已写入的样本一定仍被保留（未被回收），按序号升序返回
    hi = min(frag.end, sess.next_seq - 1)
    return {
        **_fragment_summary(sess, frag),
        "samples": [
            {"seq": seq, "value": sess.samples[seq]}
            for seq in range(frag.start, hi + 1)
        ],
    }


# ---------------- HTTP 接口 ----------------

@app.get("/")
async def root() -> dict:
    return {"service": "sample-fragment-service", "docs": "/docs"}


@app.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest) -> dict:
    # 单事件循环内无 await，创建过程天然原子
    sid = registry.next_id
    registry.next_id += 1
    registry.sessions[sid] = Session(sid, req.history, req.capacity)
    return {"session_id": sid, "history": req.history, "capacity": req.capacity}


@app.get("/sessions/{session_id}")
async def get_session(session_id: int) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        return _session_summary(sess)


@app.post("/sessions/{session_id}/fork", status_code=201)
async def fork_session(session_id: int) -> dict:
    """把来源会话分叉为可独立续采的新会话。

    取号与快照复制都在来源会话锁内完成：分叉等效于排在来源某个已完成操作
    之后执行，不会混合两个时刻的状态，来源保持不变。新 ID 与普通创建共用
    同一个递增计数器，故每次分叉都得到不同且不冲突的 ID。来源不存在返回 404。
    返回值是新会话的原会话查询结果（其中 session_id 为新 ID）。
    """
    sess = _get_session(session_id)
    async with sess.lock:
        new_id = registry.next_id
        registry.next_id += 1
        child = sess.fork(new_id)
        registry.sessions[new_id] = child
        return _session_summary(child)


@app.put("/sessions/{session_id}/limits")
async def update_limits(session_id: int, req: UpdateLimitsRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        sess.adjust_limits(req.history, req.capacity)
        return _session_summary(sess)


@app.post("/sessions/{session_id}/samples", status_code=201)
async def write_sample(session_id: int, req: WriteSampleRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        seq = sess.write(req.value)
        return {"seq": seq, "value": req.value}


@app.put("/sessions/{session_id}/pattern")
async def configure_pattern(session_id: int, req: ConfigurePatternRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        sess.configure_pattern(req.pattern)
        return {"pattern": sess.pattern}


@app.get("/sessions/{session_id}/pattern/hits")
async def get_pattern_hits(session_id: int) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        if not sess.pattern_configured:
            raise _conflict("该会话尚未配置 pattern")
        return {
            "pattern": sess.pattern,
            "hit_count": sess.hit_count,
            # 按命中产生顺序保存，即按 start_seq 升序；只保留最近 100 次
            "recent_hits": list(sess.recent_hits),
        }


@app.post("/sessions/{session_id}/triggers", status_code=201)
async def trigger(session_id: int, req: TriggerRequest) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        frag = sess.trigger(req.before, req.after)
        return _fragment_summary(sess, frag)


@app.get("/sessions/{session_id}/fragments")
async def list_fragments(session_id: int) -> list:
    sess = _get_session(session_id)
    async with sess.lock:
        return [
            _fragment_summary(sess, sess.fragments[fid])
            for fid in sorted(sess.fragments)
        ]


@app.get("/sessions/{session_id}/fragments/{fragment_id}")
async def get_fragment(session_id: int, fragment_id: int) -> dict:
    sess = _get_session(session_id)
    async with sess.lock:
        frag = sess.fragments.get(fragment_id)
        if frag is None:
            raise HTTPException(status_code=404, detail="片段不存在")
        return _fragment_detail(sess, frag)


@app.delete("/sessions/{session_id}/fragments/{fragment_id}", status_code=204)
async def delete_fragment(session_id: int, fragment_id: int) -> None:
    sess = _get_session(session_id)
    async with sess.lock:
        if sess.fragments.pop(fragment_id, None) is None:
            raise HTTPException(status_code=404, detail="片段不存在")
        # 解除引用后立刻回收不再被任何一方需要的样本
        sess.reclaim()
