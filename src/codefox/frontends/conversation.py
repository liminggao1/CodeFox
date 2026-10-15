"""CLI 与 TUI 共用的对话展示状态，不执行工具或确认事件交付。"""

from copy import deepcopy

from .presentation import full_text, operation_key


class ConversationProjection:
    def __init__(self):
        self._records = []
        self._record_ids = set()
        self._entries = {}
        self._streams = {}
        self._closed_calls = set()
        self._session_id = None
        self._generation = 0

    @property
    def records(self):
        return deepcopy(self._records)

    @property
    def entries(self):
        return deepcopy(list(self._entries.values()) + list(self._streams.values()))

    def _stream_key(self, turn, source, data):
        return (self._session_id, self._generation, turn, source,
                data.get("model_call_id"), data.get("block_index", 0))

    def _bind_scope(self, session_id, generation):
        if generation < self._generation:
            return False
        if generation == self._generation and self._session_id is not None and session_id != self._session_id:
            return False
        if generation > self._generation and self._generation:
            self._streams.clear()
            self._closed_calls.clear()
        elif generation > self._generation:
            # 首次离线直接渲染记录也可以在首个事件到达时绑定会话。
            self._streams = {(session_id, generation, *key[2:]): dict(stream, key=(session_id, generation, *key[2:]))
                             for key, stream in self._streams.items()}
            self._closed_calls = {(session_id, generation, *key[2:]) for key in self._closed_calls}
        self._session_id, self._generation = session_id, generation
        return True

    def apply_delta(self, event):
        if not self._bind_scope(event.session_id, event.generation):
            return []
        data = event.payload
        key = self._stream_key(event.turn_id, event.source, data)
        if key[:5] in self._closed_calls:
            return []
        stream = self._streams.setdefault(key, {"key": key, "kind": "stream", "source": event.source,
            "text": "", "sequence": 0, "confirmed": False, "discarded": False})
        if stream["confirmed"] or stream["discarded"] or data["sequence"] <= stream["sequence"]:
            return []
        stream["sequence"] = data["sequence"]
        stream["text"] += data["delta"]
        return [{"kind": "stream", "entry": deepcopy(stream), "delta": data["delta"]}]

    def apply_record(self, record, *, session_id=None, generation=None):
        if generation is not None and not self._bind_scope(session_id, generation):
            return []
        if record["id"] in self._record_ids:
            return []
        record = deepcopy(record)
        self._record_ids.add(record["id"])
        self._records.append(record)
        data, kind = record["payload"], record["type"]
        changes = []
        key = operation_key(record)
        operation = kind == "execution_status" or kind in {"tool_call", "tool_result"}
        entry = self._entries.setdefault(key if operation else ("record", record["id"]), {
            "key": key if operation else ("record", record["id"]), "kind": "operation" if operation else "record",
            "tool_name": "工具", "arguments": {}, "state": "pending", "output": "",
            "started_at": None, "finished_at": None, "elapsed_seconds": None})
        entry["record"] = record
        if kind == "assistant_message" and data.get("model_call_id"):
            key = self._stream_key(record.get("turn_id"), record["source"], data)
            stream = self._streams.get(key)
            if stream is not None:
                stream.update(text=data["text"], confirmed=True, discarded=False)
                return [{"kind": "confirmed", "entry": deepcopy(stream)}]
            self._streams[key] = {"key": key, "kind": "stream", "source": record["source"],
                "text": data["text"], "sequence": 0, "confirmed": True, "discarded": False}
        if operation:
            entry["started_at"] = entry["started_at"] or record.get("at")
            if kind == "tool_call":
                entry.update(tool_name=data["name"], arguments=data.get("input", {}))
                if data.get("result_missing"):
                    entry.update(state="interrupted", finished_at=record.get("at"))
            elif kind == "tool_result":
                entry.update(state=data.get("status", "returned"), output=full_text(data.get("content", "")),
                             finished_at=record.get("at"))
            else:
                state = "interrupted" if data.get("historical") and data["state"] == "running" else data["state"]
                output = data.get("reason", "")
                # 工具结果已经带有原始正文时，生命周期事件只能更新状态，
                # 不能用通用 reason 覆盖工具返回的错误码和正文。
                if data.get("phase") == "tool" and entry["output"]:
                    output = entry["output"]
                entry.update(state=state, output=output, elapsed_seconds=data.get("elapsed_seconds"))
                if state in {"completed", "success", "returned", "failed", "stopped", "denied", "discarded", "interrupted"}:
                    entry["finished_at"] = record.get("at")
                if data["phase"] == "model" and state in {"failed", "stopped", "discarded"}:
                    self._closed_calls.add((self._session_id, self._generation, record.get("turn_id"), record["source"], data.get("model_call_id")))
                    # 失败仅作废同回合、同来源、同调用尚未被接受的正文。
                    for stream in self._streams.values():
                        if stream["key"][2:5] == (record.get("turn_id"), record["source"], data.get("model_call_id")) and not stream["confirmed"] and not stream["discarded"]:
                            stream["discarded"] = True
                            changes.append({"kind": "discarded", "entry": deepcopy(stream)})
        changes.insert(0, {"kind": "record", "entry": deepcopy(entry), "record": record})
        return changes

    def restore(self, records, session_id=None, generation=0):
        self.__init__()
        self._session_id, self._generation = session_id, generation
        changes = []
        for record in records:
            changes.extend(self.apply_record(record))
        return changes

    def entry_for(self, record):
        key = operation_key(record) if record["type"] in {"tool_call", "tool_result", "execution_status"} else ("record", record["id"])
        return deepcopy(self._entries[key])
