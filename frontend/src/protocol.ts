export type JsonObject = Record<string, unknown>;

// 后端推送给前端的一条运行时事件，前端据此更新消息、状态和审批界面。
export interface InteractionEvent {
  sequence: number;
  id: string;
  session_id: string | null;
  generation: number;
  turn_id: string | null;
  source: string;
  type: string;
  payload: JsonObject;
  scope: "session" | "runtime";
  at: string;
}

export interface DisplayRecord {
  id: string;
  at: string;
  kind: "display";
  type: string;
  source: string;
  turn_id: string | null;
  payload: JsonObject;
}

export interface ApprovalRequest {
  request_id: string;
  session_id: string | null;
  generation: number;
  turn_id: string;
  source: string;
  tool_name: string;
  tool_call_id: string;
  arguments: JsonObject;
}

export interface SessionEntry {
  id: string;
  updated_at: string;
  summary: string;
  message_count: number;
  invalid?: boolean;
}

export interface LocalCommand {
  name: string;
  description: string;
  usage: string;
  requires_argument: boolean;
}

export interface DiagnosticRecord {
  id: string;
  at: string;
  session_id: string;
  turn_id: string | null;
  source: string;
  origin: string;
  text: string;
}

// 浏览器可以提交输入、停止回合、确认事件、处理审批或切换权限模式。
export type ClientMessage =
  | { type: "submit"; text: string }
  | { type: "stop"; turn_id: string }
  | { type: "ack"; sequence: number }
  | {
      type: "approval_decision";
      request_id: string;
      generation: number;
      decision: "approve" | "deny";
    }
  | { type: "permission_mode"; mode: "default" | "no_approval" };

// 后端通过同一条 WebSocket 返回事件、审批请求和协议层结果。
export type ServerMessage =
  | { type: "event"; event: InteractionEvent }
  | { type: "approval_request"; request: ApprovalRequest }
  | { type: "submission_result"; result: JsonObject }
  | { type: "action_result"; action: string; accepted: boolean; reason?: string; mode?: string }
  | { type: "connection_rejected"; reason: string }
  | { type: "protocol_error"; reason: string };

// 运行时事件的 payload 来自 JSON，先确认它是对象再读取字段。
export function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 从不确定的 JSON 值中安全取出字符串，避免渲染阶段出现类型错误。
export function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

// 把工具参数、工具结果等任意 JSON 值格式化成可读文本。
export function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value, null, 2);
}
