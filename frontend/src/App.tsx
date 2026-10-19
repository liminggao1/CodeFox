import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  CircleAlert,
  Clock3,
  LoaderCircle,
  MessageSquarePlus,
  PanelRight,
  RefreshCw,
  Send,
  Settings2,
  ShieldAlert,
  Square,
  X,
} from "lucide-react";
import mark from "../../assets/logo/1F98A.svg";
import {
  formatValue,
  isObject,
  type ApprovalRequest,
  type ClientMessage,
  type DiagnosticRecord,
  type DisplayRecord,
  type InteractionEvent,
  type LocalCommand,
  type ServerMessage,
  type SessionEntry,
  stringValue,
} from "./protocol";

interface StreamBlock {
  key: string;
  callId: string;
  text: string;
  sequence: number;
  turnId: string | null;
  source: string;
}

interface PendingAck {
  sequence: number;
  refreshSessions: boolean;
}

interface Suggestion {
  value: string;
  label: string;
  detail: string;
}

/** 从后端快照中筛出可以直接渲染的会话展示记录。 */
function displayRecords(value: unknown): DisplayRecord[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is DisplayRecord =>
    isObject(item) && typeof item.id === "string" && isObject(item.payload));
}

/** 从后端返回值中筛出结构完整的历史会话条目。 */
function sessionEntries(value: unknown): SessionEntry[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is SessionEntry =>
    isObject(item) && typeof item.id === "string");
}

/** 从运行时事件中筛选诊断日志，日志不混入模型对话记录。 */
function diagnosticEntries(value: unknown): DiagnosticRecord[] {
  if (!Array.isArray(value)) return [];
  const result: DiagnosticRecord[] = [];
  for (const item of value) {
    if (!isObject(item) || typeof item.id !== "string" || typeof item.at !== "string"
        || typeof item.session_id !== "string" || typeof item.source !== "string"
        || typeof item.origin !== "string" || typeof item.text !== "string") continue;
    result.push({
      id: item.id,
      at: item.at,
      session_id: item.session_id,
      turn_id: typeof item.turn_id === "string" ? item.turn_id : null,
      source: item.source,
      origin: item.origin,
      text: item.text,
    });
  }
  return result;
}

/** 把内部事件转换成用户能看懂的一行活动状态。 */
function recordLabel(record: DisplayRecord): string {
  const payload = record.payload;
  if (record.type === "execution_status") {
    const phases: Record<string, string> = {
      model: "模型", tool: "工具", hook: "Hook", goal: "目标检查", memory: "记忆整理",
    };
    const states: Record<string, string> = {
      running: "进行中", postprocessing: "后置处理中", completed: "已完成",
      failed: "失败", denied: "已拒绝", stopped: "已停止",
      discarded: "未采用", truncated: "输出截断",
    };
    return [
      phases[stringValue(payload.phase)] ?? stringValue(payload.phase),
      stringValue(payload.name),
      states[stringValue(payload.state)] ?? stringValue(payload.state),
    ].filter(Boolean).join(" · ");
  }
  if (record.type === "subagent_status") {
    return stringValue(payload.name) + " · " + stringValue(payload.status);
  }
  if (record.type === "background_status") {
    return "后台任务 · " + stringValue(payload.status);
  }
  if (record.type === "approval_requested") return "需要批准工具调用";
  if (record.type === "approval_decided") {
    return stringValue(payload.decision) === "approve" ? "已批准工具调用" : "已拒绝工具调用";
  }
  return record.type;
}

/** 根据记录类型选择消息、工具结果或状态行的展示结构。 */
function RecordView({ record }: { record: DisplayRecord }) {
  const payload = record.payload;
  if (record.type === "user_message") {
    return (
      <article className="message message-user">
        <div className="message-label">你</div>
        <div className="message-body">{stringValue(payload.text)}</div>
      </article>
    );
  }
  if (record.type === "assistant_message") {
    return (
      <article className="message message-assistant">
        <div className="message-label">CodeFox</div>
        <div className="message-body">{stringValue(payload.text)}</div>
      </article>
    );
  }
  if (record.type === "tool_call") {
    return (
      <details className="tool-record" open>
        <summary>
          <span className="tool-state tool-state-running"><LoaderCircle size={14} />工具调用</span>
          <strong>{stringValue(payload.name, "工具")}</strong>
        </summary>
        <pre>{formatValue(payload.input)}</pre>
      </details>
    );
  }
  if (record.type === "tool_result") {
    return (
      <details className="tool-record">
        <summary>
          <span className="tool-state"><Check size={14} />工具结果</span>
          <strong>{stringValue(payload.status, "已返回")}</strong>
        </summary>
        <pre>{formatValue(payload.content)}</pre>
      </details>
    );
  }
  if (record.type === "system_error") {
    return (
      <div className="system-error">
        <CircleAlert size={16} />
        <span>{stringValue(payload.error)}</span>
      </div>
    );
  }
  if (record.type === "todo_updated") {
    const todos = Array.isArray(payload.todos) ? payload.todos : [];
    return (
      <details className="tool-record">
        <summary><span className="tool-state">任务清单</span><strong>{todos.length} 项</strong></summary>
        <ul className="todo-list">
          {todos.map((todo, index) => (
            <li key={index}>{isObject(todo) ? stringValue(todo.status) + " · " + stringValue(todo.content) : formatValue(todo)}</li>
          ))}
        </ul>
      </details>
    );
  }
  if (["execution_status", "subagent_status", "background_status",
       "approval_requested", "approval_decided"].includes(record.type)) {
    return <div className="activity-record"><Clock3 size={14} /><span>{recordLabel(record)}</span></div>;
  }
  return null;
}

/** 把会话更新时间格式化为中文日期和时间。 */
function formatSessionDate(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

/** 格式化运行时用量字段；缺失的数据不伪造默认值。 */
function tokenValue(value: unknown): string {
  return typeof value === "number" ? new Intl.NumberFormat("zh-CN").format(value) : "—";
}

/** 与 TUI 保持一致地压缩 token 数字，避免底栏挤占输入区。 */
function compactTokenValue(value: unknown): string {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return "—";
  if (value >= 1000) {
    if (value % 1000 === 0) return `${value / 1000}k`;
    return value >= 10000 ? `${(value / 1000).toFixed(1)}k` : `${(value / 1000).toFixed(2)}k`;
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

/** 判断输入是否为可在上下文满载时执行的压缩命令。 */
function isCompactCommand(value: string): boolean {
  return value.trim().split(/\s+/, 1)[0] === "/compact";
}

/** 底部摘要只读显示当前模型和最近一次请求的上下文窗口占用。 */
function contextSummary(snapshot: Record<string, unknown>): {
  model: string;
  used: string;
  limit: string;
  percent: number | null;
} {
  const model = stringValue(snapshot.model, "未开始");
  const used = snapshot.context_input_tokens;
  const limit = snapshot.context_limit_tokens;
  if (typeof used !== "number" || !Number.isInteger(used) || used < 0
      || typeof limit !== "number" || !Number.isInteger(limit) || limit <= 0) {
    return { model, used: "暂无", limit: "暂无", percent: null };
  }
  const shown = Math.min(used, limit);
  return {
    model,
    used: compactTokenValue(shown),
    limit: compactTokenValue(limit),
    percent: Math.min(100, (shown * 100) / limit),
  };
}

/** 用于状态抽屉中展示目标、任务清单等结构化快照。 */
function stateValue(value: unknown): string {
  if (typeof value === "string") return value || "—";
  if (value === null || value === undefined) return "—";
  return formatValue(value);
}

/** Web 工作台根组件：维护连接、会话、消息流和输入区状态。 */
export function App() {
  const socketRef = useRef<WebSocket | null>(null);
  const seenEventsRef = useRef(new Set<number>());
  const finalizedSubmissionsRef = useRef(new Set<string>());
  const sessionListRefreshPendingRef = useRef(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const [connected, setConnected] = useState(false);
  const [connectionRejected, setConnectionRejected] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState("正在连接");
  const [records, setRecords] = useState<DisplayRecord[]>([]);
  const [streams, setStreams] = useState<Record<string, StreamBlock>>({});
  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [approvalPending, setApprovalPending] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [turnId, setTurnId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState("");
  const [pendingAck, setPendingAck] = useState<PendingAck | null>(null);
  const [runStatus, setRunStatus] = useState("");
  const [commands, setCommands] = useState<LocalCommand[]>([]);
  const [snapshot, setSnapshot] = useState<Record<string, unknown>>({});
  const [diagnostics, setDiagnostics] = useState<DiagnosticRecord[]>([]);
  const [showDiagnostics, setShowDiagnostics] = useState(
    () => window.localStorage.getItem("codefox.showDiagnostics") !== "false",
  );
  const [stateOpen, setStateOpen] = useState(false);
  const [permissionPending, setPermissionPending] = useState(false);
  const [commandFeedback, setCommandFeedback] = useState("");
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false);

  useEffect(() => {
    let disposed = false;
    fetch("/api/commands")
      .then((response) => {
        if (!response.ok) throw new Error("命令列表请求失败");
        return response.json() as Promise<{ commands?: unknown }>;
      })
      .then((data) => {
        if (disposed || !Array.isArray(data.commands)) return;
        setCommands(data.commands.filter((item): item is LocalCommand =>
          isObject(item) && typeof item.name === "string"
          && typeof item.description === "string" && typeof item.usage === "string"
          && typeof item.requires_argument === "boolean"));
      })
      .catch(() => {
        if (!disposed) setNotice("无法读取斜杠命令列表");
      });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    window.localStorage.setItem("codefox.showDiagnostics", String(showDiagnostics));
  }, [showDiagnostics]);

  /** 通过当前 WebSocket 向 FastAPI 发送一条类型化消息。 */
  function sendMessage(message: ClientMessage): boolean {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setNotice("连接尚未就绪");
      return false;
    }
    // WebSocket 只能发送字符串，因此先把 TypeScript 对象编码成 JSON。
    socket.send(JSON.stringify(message));
    return true;
  }

  /** 把后端事件投影到 React 状态，并返回是否需要刷新会话列表。 */
  function applyEvent(event: InteractionEvent): boolean {
    if (seenEventsRef.current.has(event.sequence)) return false;
    seenEventsRef.current.add(event.sequence);
    if (seenEventsRef.current.size > 3000) {
      seenEventsRef.current = new Set(Array.from(seenEventsRef.current).slice(-1500));
    }

    const payload = event.payload;
    let refreshSessions = false;
    if (event.type === "display_record") {
      const record = isObject(payload.record) ? payload.record as unknown as DisplayRecord : null;
      if (record && typeof record.id === "string") {
        if (record.type === "assistant_message") {
          const callId = stringValue(record.payload.model_call_id);
          if (callId) {
            // 收到完整助手消息后，删除同一次模型调用的临时流式文本。
            setStreams((current) => Object.fromEntries(
              Object.entries(current).filter(([, stream]) => stream.callId !== callId),
            ));
          }
        }
        if (record.type === "approval_decided") {
          setApproval((current) =>
            current?.request_id === stringValue(record.payload.request_id) ? null : current);
          setApprovalPending(false);
        }
        if (record.type !== "command_result" && record.type !== "turn_finished") {
          setRecords((current) =>
            current.some((item) => item.id === record.id) ? current : [...current, record]);
        }
      }
    } else if (event.type === "assistant_text_delta") {
      const callId = stringValue(payload.model_call_id);
      const blockIndex = typeof payload.block_index === "number" ? payload.block_index : 0;
      const key = [event.session_id ?? "", event.generation, event.turn_id ?? "",
        event.source, callId, blockIndex].join(":");
      const sequence = typeof payload.sequence === "number" ? payload.sequence : 0;
      setStreams((current) => {
        const previous = current[key];
        if (previous && previous.sequence >= sequence) return current;
        return {
          ...current,
          [key]: {
            key,
            callId,
            text: (previous?.text ?? "") + stringValue(payload.delta),
            sequence,
            turnId: event.turn_id,
            source: event.source,
          },
        };
      });
    } else if (event.type === "session_changed") {
      setSessionId(event.session_id);
      setRecords(displayRecords(payload.records));
      setDiagnostics(diagnosticEntries(payload.diagnostics));
      setSnapshot({ session_id: event.session_id });
      setStreams({});
      setApproval(null);
      setApprovalPending(false);
      setRunStatus("会话已切换");
    } else if (event.type === "state_snapshot") {
      setSessionId(event.session_id);
      setSnapshot({ ...payload, session_id: event.session_id });
      setBusy(Boolean(payload.busy));
      setTurnId(event.turn_id);
      if (payload.busy) setRunStatus("处理中");
    } else if (event.type === "log") {
      const entry = diagnosticEntries([{
        id: event.id,
        at: event.at,
        session_id: event.session_id,
        turn_id: event.turn_id,
        source: event.source,
        origin: payload.origin ?? "background",
        text: payload.text,
      }])[0];
      if (entry) setDiagnostics((current) => current.some((item) => item.id === entry.id)
        ? current : [...current, entry]);
    } else if (event.type === "turn_started") {
      setBusy(true);
      setTurnId(event.turn_id);
      setStopping(false);
      setRunStatus("处理中");
    } else if (event.type === "turn_finished") {
      refreshSessions = sessionListRefreshPendingRef.current;
      if (typeof payload.submission_id === "string") {
        finalizedSubmissionsRef.current.add(payload.submission_id);
      }
      const outcome = isObject(payload.outcome) ? payload.outcome : {};
      const labels: Record<string, string> = {
        completed: "已完成", failed: "执行失败", stopped: "已停止", truncated: "输出已截断",
      };
      setBusy(false);
      setTurnId(null);
      setStopping(false);
      setApproval(null);
      setApprovalPending(false);
      setRunStatus(labels[stringValue(outcome.status)] ?? "已结束");
    } else if (event.type === "command_result") {
      const result = isObject(payload.data) ? payload.data : {};
      if (Array.isArray(result.sessions)) {
        setSessions(sessionEntries(result.sessions));
        sessionListRefreshPendingRef.current = false;
      } else if (stringValue(payload.status) === "failed") {
        setNotice(stringValue(result.error, "命令执行失败"));
      } else {
        if (typeof result.text === "string") setCommandFeedback(result.text);
        if (["/new", "/resume", "/delete"].includes(stringValue(payload.command))) {
          sessionListRefreshPendingRef.current = true;
          refreshSessions = true;
        }
      }
      if (!event.turn_id) {
        if (typeof payload.submission_id === "string") {
          finalizedSubmissionsRef.current.add(payload.submission_id);
        }
        setBusy(false);
        setTurnId(null);
      }
    } else if (event.type === "runtime_closed") {
      setConnectionMessage("运行时已关闭");
    } else if (event.type === "runtime_error") {
      setNotice(stringValue(payload.error, "运行时错误"));
    }
    return refreshSessions;
  }

  useEffect(() => {
    let retryTimer = 0;
    let retryDelay = 400;
    let disposed = false;
    let rejected = false;

    /** 建立同源 WebSocket；页面使用 HTTPS 时自动切换为 wss。 */
    function connect() {
      if (disposed || connectionRejected) return;
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      // 使用当前页面的 host，让 Vite 开发代理把 /api/runtime 转给 FastAPI。
      const socket = new WebSocket(scheme + "//" + window.location.host + "/api/runtime");
      socketRef.current = socket;
      socket.onopen = () => {
        retryDelay = 400;
        setConnected(true);
        setConnectionMessage("已连接");
        setNotice("");
        sessionListRefreshPendingRef.current = true;
        socket.send(JSON.stringify({ type: "submit", text: "/resume" }));
      };
      socket.onmessage = (raw) => {
        let message: ServerMessage;
        try {
          message = JSON.parse(raw.data) as ServerMessage;
        } catch {
          setNotice("收到无法识别的服务器消息");
          return;
        }
        if (message.type === "event") {
          setNotice("");
          const refreshSessions = applyEvent(message.event);
          setPendingAck({ sequence: message.event.sequence, refreshSessions });
        } else if (message.type === "approval_request") {
          setApproval(message.request);
          setApprovalPending(false);
        } else if (message.type === "submission_result") {
          const result = message.result;
          if (stringValue(result.status) === "accepted") {
            const submissionId = stringValue(result.submission_id);
            if (!finalizedSubmissionsRef.current.has(submissionId)) setBusy(true);
            setNotice("");
          } else {
            setNotice(stringValue(result.reason, "任务未能提交"));
          }
        } else if (message.type === "action_result") {
          if (message.action === "stop") {
            if (message.accepted) setStopping(true);
            else {
              setStopping(false);
              setNotice("当前回合已结束，无法停止");
            }
          }
          if (message.action === "approval_decision") {
            if (message.accepted) setApprovalPending(true);
            else {
              setApprovalPending(false);
              setNotice("审批请求已失效，请查看最新状态");
            }
          }
          if (message.action === "permission_mode") {
            setPermissionPending(false);
            if (message.accepted && (message.mode === "default" || message.mode === "no_approval")) {
              setSnapshot((current) => ({ ...current, permission_mode: message.mode }));
              setNotice("");
            } else {
              setNotice(message.reason || "权限模式切换失败");
            }
          }
        } else if (message.type === "connection_rejected") {
          rejected = true;
          setConnectionRejected(true);
          setConnectionMessage(message.reason);
          socket.close();
        } else if (message.type === "protocol_error") {
          setNotice(message.reason);
        }
      };
      socket.onclose = () => {
        if (socketRef.current === socket) socketRef.current = null;
        setConnected(false);
        if (!disposed && !rejected) {
          setConnectionMessage("连接中断，正在重连");
          retryTimer = window.setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 1.7, 5000);
        }
      };
      socket.onerror = () => socket.close();
    }

    retryTimer = window.setTimeout(connect, 0);
    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [connectionRejected]);

  useEffect(() => {
    if (!pendingAck) return;
    const refreshSessions = pendingAck.refreshSessions;
    if (sendMessage({ type: "ack", sequence: pendingAck.sequence }) && refreshSessions) {
      sendMessage({ type: "submit", text: "/resume" });
    }
    setPendingAck(null);
  }, [pendingAck]);

  useEffect(() => {
    const container = scrollRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [records, streams, approval, diagnostics]);

  const orderedStreams = useMemo(() => Object.values(streams), [streams]);
  const suggestions = useMemo<Suggestion[]>(() => {
    const sessionQuery = input.match(/^\/(resume|delete)\s+([^\s]*)$/);
    if (sessionQuery) {
      const [, command, query] = sessionQuery;
      return sessions
        .filter((session) => command !== "delete" || session.id !== sessionId)
        .filter((session) => session.id.startsWith(query))
        .slice(0, 8)
        .map((session) => ({
          value: `/${command} ${session.id}`,
          label: session.summary || "空会话",
          detail: command === "resume" ? "恢复此会话" : "删除此会话",
        }));
    }
    if (!/^\/[a-z]*$/.test(input)) return [];
    return commands
      .filter((command) => command.name.startsWith(input))
      .map((command) => ({
        value: command.name + " ",
        label: command.description,
        detail: command.usage.startsWith(command.name)
          ? command.usage.slice(command.name.length).trim() : command.usage,
      }));
  }, [commands, input, sessionId, sessions]);

  const selectedPermissionMode = snapshot.permission_mode === "no_approval"
    ? "no_approval" : "default";
  const usage = isObject(snapshot.lead_main_model_usage)
    ? snapshot.lead_main_model_usage : {};
  const goal = isObject(snapshot.goal) ? snapshot.goal : {};
  const activeGoal = isObject(goal.active) ? goal.active : null;
  const lastGoalStatus = isObject(goal.last_status) ? goal.last_status : null;
  const todos = Array.isArray(snapshot.todos) ? snapshot.todos : [];
  const subagents = Array.isArray(snapshot.subagents) ? snapshot.subagents : [];
  const context = contextSummary(snapshot);
  const contextClass = context.percent !== null
    ? context.percent >= 100 ? " context-full"
      : context.percent >= 80 ? " context-warning" : ""
    : "";
  const contextFull = context.percent === 100;
  const compactDraft = isCompactCommand(input);
  const sendDisabled = !connected || busy || !input.trim() || (contextFull && !compactDraft);

  /** 校验输入状态后，把用户消息提交给后端运行时。 */
  function submit(text = input) {
    const value = text.trim();
    if (!value || busy || !connected) return;
    if (contextFull && !isCompactCommand(value)) {
      setNotice("上下文已满，请先执行 /compact");
      return;
    }
    if (sendMessage({ type: "submit", text: value })) {
      setInput("");
      setNotice("");
      setCommandFeedback("");
      setSuggestionsDismissed(false);
      setRunStatus("");
      setBusy(true);
    }
  }

  /** 选中注册命令或匹配的会话 ID，并将焦点留在输入框。 */
  function chooseSuggestion(suggestion: Suggestion) {
    setInput(suggestion.value);
    setSuggestionIndex(0);
    setSuggestionsDismissed(false);
    setStateOpen(false);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }

  /** 权限策略由运行时持久化，前端只发送模式并等待确认结果。 */
  function changePermissionMode(mode: "default" | "no_approval") {
    if (mode === selectedPermissionMode || permissionPending || busy) return;
    if (sendMessage({ type: "permission_mode", mode })) {
      setPermissionPending(true);
      setNotice("");
    }
  }

  /** 把用户对工具调用的批准或拒绝发送给后端。 */
  function decideApproval(decision: "approve" | "deny") {
    if (!approval || approvalPending) return;
    if (sendMessage({
      type: "approval_decision",
      request_id: approval.request_id,
      generation: approval.generation,
      decision,
    })) {
      setApprovalPending(true);
      setNotice("");
    }
  }

  const currentTitle = sessionId ? "会话 " + sessionId : "新会话";

  return (
    <main className="workbench">
      <aside className="sidebar">
        <div className="brand">
          <img src={mark} alt="" />
          <div className="brand-name">CodeFox</div>
          <div className={"connection-dot" + (connected ? " is-connected" : "")}
               title={connectionMessage} aria-label={connectionMessage} />
        </div>
        <button className="new-session" onClick={() => submit("/new")} disabled={!connected || busy}>
          <MessageSquarePlus size={17} />
          <span>新建会话</span>
        </button>
        <div className="session-heading">
          <span>最近会话</span>
          <button className="icon-button refresh-button" title="刷新会话列表"
                  aria-label="刷新会话列表" onClick={() => submit("/resume")} disabled={!connected || busy}>
            <RefreshCw size={15} />
          </button>
        </div>
        <nav className="session-list" aria-label="会话列表">
          {sessions.length ? sessions.map((session) => (
            <button
              key={session.id}
              className={"session-row" + (session.id === sessionId ? " is-active" : "")}
              onClick={() => submit("/resume " + session.id)}
              disabled={!connected || busy}
              title={session.summary || session.id}
            >
              <span className="session-summary">{session.summary || "空会话"}</span>
              <span className="session-meta">
                <span>{session.message_count} 条消息</span>
                <span>{formatSessionDate(session.updated_at)}</span>
              </span>
            </button>
          )) : (
            <div className="session-empty">{connected ? "暂无历史会话" : "等待连接"}</div>
          )}
        </nav>
        <div className="sidebar-footer">本地工作区</div>
      </aside>

      <section className="conversation">
        <header className="conversation-header">
          <div className="conversation-heading">
            <span className="eyebrow">当前会话</span>
            <h1 title={currentTitle}>{currentTitle}</h1>
          </div>
          <div className="header-actions">
            <div className="header-state" aria-live="polite">
              {busy ? <LoaderCircle className="spin" size={15} /> : <span className="ready-mark" />}
              <span>{stopping ? "正在停止" : busy ? "处理中" : runStatus || connectionMessage}</span>
            </div>
            <button className={"icon-button header-toggle" + (stateOpen ? " is-active" : "")}
                    title="打开运行状态" aria-label="打开运行状态"
                    aria-expanded={stateOpen} onClick={() => setStateOpen((open) => !open)}>
              <PanelRight size={17} />
            </button>
          </div>
        </header>

        {stateOpen && (
          <aside className="state-drawer" aria-label="运行状态">
            <div className="drawer-heading">
              <div>
                <span className="eyebrow">运行状态</span>
                <h2>会话与运行时</h2>
              </div>
              <button className="icon-button" title="关闭运行状态" aria-label="关闭运行状态"
                      onClick={() => setStateOpen(false)}>
                <X size={16} />
              </button>
            </div>

            <section className="drawer-section">
              <div className="drawer-section-title"><Settings2 size={15} /><span>权限策略</span></div>
              <label className="permission-option">
                <input type="radio" name="permission-mode" value="default"
                       checked={selectedPermissionMode === "default"}
                       onChange={() => changePermissionMode("default")}
                       disabled={!connected || busy || permissionPending} />
                <span><strong>默认审批</strong><small>需要时询问是否允许工具调用</small></span>
              </label>
              <label className="permission-option">
                <input type="radio" name="permission-mode" value="no_approval"
                       checked={selectedPermissionMode === "no_approval"}
                       onChange={() => changePermissionMode("no_approval")}
                       disabled={!connected || busy || permissionPending} />
                <span><strong>免审批</strong><small>运行时仍会拒绝禁止的工具调用</small></span>
              </label>
              {permissionPending && <div className="drawer-hint">正在保存权限策略…</div>}
            </section>

            <section className="drawer-section">
              <div className="drawer-section-title"><span>运行摘要</span></div>
              <div className="state-grid">
                <div><span>模型</span><strong>{stateValue(snapshot.model)}</strong></div>
                <div><span>任务清单</span><strong>{todos.length} 项</strong></div>
                <div><span>子 agent</span><strong>{subagents.length} 个</strong></div>
                <div><span>上下文</span><strong>{tokenValue(snapshot.context_input_tokens)} / {tokenValue(snapshot.context_limit_tokens)}</strong></div>
                <div><span>输入 token</span><strong>{tokenValue(usage.input_tokens)}</strong></div>
                <div><span>输出 token</span><strong>{tokenValue(usage.output_tokens)}</strong></div>
              </div>
            </section>

            <section className="drawer-section drawer-details">
              <details>
                <summary>目标与任务详情</summary>
                <div className="drawer-detail-content">
                  <strong>目标</strong>
                  <p>{activeGoal
                    ? stateValue(activeGoal.condition)
                    : lastGoalStatus ? stateValue(lastGoalStatus.condition) : "未设置目标"}</p>
                  {lastGoalStatus && (
                    <p className="drawer-hint">{stateValue(lastGoalStatus.reason)}</p>
                  )}
                  <strong>任务清单</strong>
                  {todos.length ? (
                    <ul>
                      {todos.map((todo, index) => (
                        <li key={index}>{isObject(todo)
                          ? [stringValue(todo.status), stringValue(todo.content)].filter(Boolean).join(" · ")
                          : formatValue(todo)}</li>
                      ))}
                    </ul>
                  ) : <p>暂无任务</p>}
                  <strong>子 agent</strong>
                  {subagents.length ? (
                    <ul>
                      {subagents.map((agent, index) => (
                        <li key={isObject(agent) ? stringValue(agent.agent_id, String(index)) : index}>
                          {isObject(agent)
                            ? [stringValue(agent.name), stringValue(agent.status)].filter(Boolean).join(" · ")
                            : formatValue(agent)}
                        </li>
                      ))}
                    </ul>
                  ) : <p>暂无运行中的子 agent</p>}
                </div>
              </details>
              <details>
                <summary>本轮任务</summary>
                <div className="drawer-detail-content">
                  <p>{stateValue(snapshot.active_user_request)}</p>
                </div>
              </details>
            </section>

            <section className="drawer-section">
              <div className="drawer-section-title"><span>斜杠命令</span></div>
              <div className="command-list">
                {commands.map((command) => (
                  <button key={command.name} type="button" className="command-list-item"
                          onClick={() => chooseSuggestion({
                            value: command.name + " ", label: command.name,
                            detail: command.description + " · " + command.usage,
                          })}>
                    <code>{command.name}</code><span>{command.description}</span>
                  </button>
                ))}
              </div>
            </section>

            <label className="diagnostics-toggle">
              <input type="checkbox" checked={showDiagnostics}
                     onChange={(event) => setShowDiagnostics(event.target.checked)} />
              <span>显示运行日志（排查问题）</span>
            </label>
          </aside>
        )}

        <div className="transcript" ref={scrollRef} aria-live="polite">
          {records.filter((record) => record.type !== "command_result").map((record) => (
            <RecordView key={record.id} record={record} />
          ))}
          {orderedStreams.map((stream) => (
            <article className="message message-assistant message-stream" key={stream.key}>
              <div className="message-label">CodeFox</div>
              <div className="message-body">{stream.text}<span className="stream-caret" /></div>
            </article>
          ))}
          {approval && (
            <section className="approval-card" aria-label="工具审批">
              <div className="approval-heading">
                <ShieldAlert size={18} />
                <div>
                  <strong>需要批准工具调用</strong>
                  <span>{approval.tool_name}</span>
                </div>
              </div>
              <pre>{formatValue(approval.arguments)}</pre>
              <div className="approval-actions">
                <button className="approve-button" onClick={() => decideApproval("approve")} disabled={approvalPending}>
                  <Check size={15} />批准
                </button>
                <button className="deny-button" onClick={() => decideApproval("deny")} disabled={approvalPending}>
                  <X size={15} />拒绝
                </button>
                {approvalPending && <span className="approval-wait">正在提交</span>}
              </div>
            </section>
          )}
          {showDiagnostics && diagnostics.length > 0 && (
            <details className="diagnostics-panel">
              <summary>运行日志 · {diagnostics.length} 条</summary>
              <div className="diagnostics-list">
                {diagnostics.slice(-80).map((entry) => (
                  <div className="diagnostic-row" key={entry.id}>
                    <span className="diagnostic-time">{formatSessionDate(entry.at)}</span>
                    <span className="diagnostic-origin">{entry.origin}</span>
                    <span className="diagnostic-text">{entry.text}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {!records.length && !orderedStreams.length && !busy && (
            <div className="empty-conversation">
              <img src={mark} alt="" />
              <p>发送一条消息开始工作</p>
            </div>
          )}
        </div>

        <footer className="composer-area">
          {notice && <div className="notice" role="status"><CircleAlert size={15} />{notice}</div>}
          {commandFeedback && <div className="command-feedback" role="status"><Check size={14} />{commandFeedback}</div>}
          {!connected && !connectionRejected && <div className="reconnect-line">{connectionMessage}</div>}
          {suggestions.length > 0 && !suggestionsDismissed && !busy && !stateOpen && (
            <div className="command-suggestions" role="listbox" aria-label="命令建议">
              {suggestions.map((suggestion, index) => (
                <button key={suggestion.value} type="button"
                        className={index === suggestionIndex ? "is-selected" : ""}
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => chooseSuggestion(suggestion)}>
                  <code>{suggestion.value}</code>
                  <span>{suggestion.label}</span>
                  {suggestion.detail && <small>{suggestion.detail}</small>}
                </button>
              ))}
            </div>
          )}
          <form className="composer" onSubmit={(event) => { event.preventDefault(); submit(); }}>
            <textarea
              ref={inputRef}
              value={input}
              onFocus={() => setStateOpen(false)}
              onChange={(event) => {
                setInput(event.target.value);
                setSuggestionIndex(0);
                setSuggestionsDismissed(false);
              }}
              onKeyDown={(event) => {
                if (suggestions.length > 0 && !suggestionsDismissed) {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setSuggestionIndex((index) => (index + 1) % suggestions.length);
                    return;
                  }
                  if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setSuggestionIndex((index) => (index - 1 + suggestions.length) % suggestions.length);
                    return;
                  }
                  if (event.key === "Tab") {
                    event.preventDefault();
                    chooseSuggestion(suggestions[suggestionIndex]);
                    return;
                  }
                  if (event.key === "Escape") {
                    event.preventDefault();
                    setSuggestionsDismissed(true);
                    return;
                  }
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    chooseSuggestion(suggestions[suggestionIndex]);
                    return;
                  }
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
              placeholder={connected ? "发送消息..." : "等待连接..."}
              aria-label="发送消息"
              disabled={!connected || busy}
              rows={2}
            />
            <div className="composer-actions">
              <span className="input-hint">Enter 发送 · Shift+Enter 换行</span>
              {busy && turnId ? (
                <button type="button" className="stop-button" title="停止当前回合"
                        aria-label="停止当前回合" onClick={() => {
                          if (sendMessage({ type: "stop", turn_id: turnId })) setStopping(true);
                        }}>
                  <Square size={15} />
                </button>
              ) : (
                <button type="submit" className="send-button" title="发送消息"
                        aria-label="发送消息" disabled={sendDisabled}>
                  <Send size={16} />
                </button>
              )}
            </div>
          </form>
          <div className={"composer-status" + contextClass}
               title={`模型：${context.model} · 已使用上下文窗口：${context.used}/${context.limit}`}
               aria-label={`模型：${context.model}，已使用上下文窗口：${context.used}/${context.limit}${context.percent === null ? "" : `（${context.percent.toFixed(0)}%）`}`}>
            <span className="composer-status-model">模型：{context.model}</span>
            <span className="composer-status-separator" aria-hidden="true">·</span>
            <span className="composer-status-context">
              已使用上下文窗口：{context.used}/{context.limit}
              {context.percent === null ? "" : `（${context.percent.toFixed(0)}%）`}
            </span>
          </div>
          {connectionRejected && <div className="connection-blocked">此工作台只接受一个浏览器连接。</div>}
        </footer>
      </section>
    </main>
  );
}
