import { CircleAlert, FileDiff, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  isObject,
  stringValue,
  type DiffErrorPayload,
  type DiffFile,
  type DiffSnapshot,
} from "./protocol";

interface DiffWorkspaceProps {
  refreshKey: number;
}

function isDiffFile(value: unknown): value is DiffFile {
  return isObject(value)
    && typeof value.path === "string"
    && typeof value.status === "string"
    && typeof value.additions === "number"
    && typeof value.deletions === "number"
    && typeof value.binary === "boolean"
    && typeof value.truncated === "boolean"
    && typeof value.patch === "string";
}

function parseSnapshot(value: unknown): DiffSnapshot | null {
  if (!isObject(value) || !isObject(value.workdir) || !Array.isArray(value.files)
      || typeof value.generated_at !== "string") return null;
  const workdir = value.workdir;
  if (typeof workdir.name !== "string" || typeof workdir.branch !== "string") return null;
  const files = value.files.filter(isDiffFile).map((file) => ({
    ...file,
    previous_path: typeof file.previous_path === "string" ? file.previous_path : null,
    size_bytes: typeof file.size_bytes === "number" ? file.size_bytes : null,
  }));
  return {
    workdir: {
      name: workdir.name,
      branch: workdir.branch,
      commit: typeof workdir.commit === "string" ? workdir.commit : null,
    },
    files,
    generated_at: value.generated_at,
  };
}

function parseError(value: unknown): string {
  if (!isObject(value) || !isObject(value.error)) return "无法读取工作区变更";
  const payload = value as unknown as DiffErrorPayload;
  return stringValue(payload.error.message, "无法读取工作区变更");
}

function statusLabel(status: string): string {
  return {
    modified: "修改",
    added: "新增",
    deleted: "删除",
    renamed: "重命名",
    copied: "复制",
    untracked: "未跟踪",
  }[status] ?? status;
}

function fileSize(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function DiffPreview({ file }: { file: DiffFile }) {
  if (file.binary) {
    return <div className="diff-placeholder">二进制文件，无法预览 Diff。</div>;
  }
  if (file.truncated && !file.patch) {
    return <div className="diff-placeholder">文件内容过大，暂不加载完整 Diff。</div>;
  }
  return (
    <div className="diff-preview-wrap">
      {file.truncated && <div className="diff-warning">Diff 内容已截断，仅显示前部分内容。</div>}
      <pre className="diff-preview">{file.patch || "该文件没有可显示的文本 Diff。"}</pre>
    </div>
  );
}

export function DiffWorkspace({ refreshKey }: DiffWorkspaceProps) {
  const [snapshot, setSnapshot] = useState<DiffSnapshot | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  async function refresh() {
    setRefreshing(true);
    try {
      const response = await fetch("/api/diff");
      const payload = await response.json() as unknown;
      if (!response.ok) throw new Error(parseError(payload));
      const next = parseSnapshot(payload);
      if (!next) throw new Error("服务器返回的 Diff 数据格式无效");
      setSnapshot(next);
      setError("");
      setSelectedPath((current) =>
        current && next.files.some((file) => file.path === current)
          ? current : next.files[0]?.path ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取工作区变更");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [refreshKey]);

  const selectedFile = useMemo(
    () => snapshot?.files.find((file) => file.path === selectedPath) ?? null,
    [selectedPath, snapshot],
  );

  if (loading) {
    return <section className="diff-workspace diff-state">正在读取工作区变更…</section>;
  }

  if (error && !snapshot) {
    return (
      <section className="diff-workspace diff-state diff-state-error">
        <CircleAlert size={16} />
        <span>{error}</span>
        <button className="icon-button" type="button" title="重试读取 Diff"
                aria-label="重试读取 Diff" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "spin" : ""} size={15} />
        </button>
      </section>
    );
  }

  if (!snapshot) return null;

  return (
    <section className="diff-workspace" aria-label="工作区变更">
      <header className="diff-header">
        <div className="diff-heading">
          <div className="diff-title-row">
            <FileDiff size={16} />
            <strong>工作区变更</strong>
            <span>{snapshot.files.length} 个文件</span>
          </div>
          <div className="diff-meta">
            <span>{snapshot.workdir.name}</span>
            <span>{snapshot.workdir.branch}</span>
            {snapshot.workdir.commit && <code>{snapshot.workdir.commit.slice(0, 8)}</code>}
          </div>
        </div>
        <button className="icon-button" type="button" title="刷新工作区变更"
                aria-label="刷新工作区变更" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "spin" : ""} size={15} />
        </button>
      </header>
      {error && <div className="diff-inline-error"><CircleAlert size={14} />{error}</div>}
      {!snapshot.files.length ? (
        <div className="diff-clean-state">工作区干净，没有待查看的变更。</div>
      ) : (
        <div className="diff-content">
          <nav className="diff-file-list" aria-label="变更文件列表">
            {snapshot.files.map((file) => (
              <button key={file.path} type="button"
                      className={"diff-file-item" + (file.path === selectedPath ? " is-selected" : "")}
                      onClick={() => setSelectedPath(file.path)}>
                <span className="diff-file-main">
                  <span className="diff-file-status">{statusLabel(file.status)}</span>
                  <span className="diff-file-path" title={file.path}>{file.path}</span>
                </span>
                <span className="diff-file-stats">
                  <span className="diff-additions">+{file.additions}</span>
                  <span className="diff-deletions">−{file.deletions}</span>
                </span>
              </button>
            ))}
          </nav>
          <div className="diff-detail">
            {selectedFile ? (
              <>
                <div className="diff-detail-header">
                  <div>
                    <strong>{selectedFile.path}</strong>
                    {selectedFile.previous_path && <span>来自 {selectedFile.previous_path}</span>}
                  </div>
                  <div className="diff-detail-stats">
                    {selectedFile.size_bytes !== null && <span>{fileSize(selectedFile.size_bytes)}</span>}
                    <span className="diff-additions">+{selectedFile.additions}</span>
                    <span className="diff-deletions">−{selectedFile.deletions}</span>
                  </div>
                </div>
                <DiffPreview file={selectedFile} />
              </>
            ) : <div className="diff-placeholder">选择一个文件查看 Diff。</div>}
          </div>
        </div>
      )}
    </section>
  );
}
