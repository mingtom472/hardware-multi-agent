"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Provider = { id: string; name: string; protocol: string; capabilities: string[]; configured: boolean; mode: string };
type Workflow = { reviewers: string[]; vision_provider: string; validator: string; summarizer: string; strategy: "manual"; profile: "professional"; max_retries: number; max_parallel: number; timeout_seconds: number; name?: string; version?: number };
type Upload = { id: string; filename: string; size: number; content_type: string; sha256: string };
type ModelCall = { stage: string; provider: string; attempt: number; status: string; input_tokens: number; output_tokens: number; cost_usd: number | string; latency_ms: number; error?: string; output?: string };
type Task = { id: string; status: "queued" | "running" | "completed" | "failed" | "cancelled" | "awaiting_review"; events: { stage: string; message: string; timestamp: string }[]; calls: ModelCall[]; result: Record<string, unknown> | null; usage: { input_tokens: number; output_tokens: number; cost_usd: number | string; simulated: boolean }; error?: string | null };
type Capabilities = { limits?: { max_upload_bytes?: number | null; max_files_per_task?: number } };
type ModelCatalog = { mode: string; models: { id: string; provider: string; model: string; capabilities: string[]; enabled: boolean; kind: string; verified: boolean }[] };

const defaults: Workflow = { reviewers: ["openai", "deepseek", "doubao"], vision_provider: "openai", validator: "claude", summarizer: "glm", strategy: "manual", profile: "professional", max_retries: 2, max_parallel: 3, timeout_seconds: 30 };
const fallbackProviders: Provider[] = [
  { id: "openai", name: "OpenAI", protocol: "Responses / Chat Completions", capabilities: ["text", "vision"], configured: false, mode: "mock" },
  { id: "claude", name: "Claude", protocol: "Anthropic Messages", capabilities: ["text", "vision"], configured: false, mode: "mock" },
  { id: "deepseek", name: "DeepSeek", protocol: "DeepSeek API", capabilities: ["text"], configured: false, mode: "mock" },
  { id: "doubao", name: "豆包", protocol: "Volcengine Ark", capabilities: ["text", "vision"], configured: false, mode: "mock" },
  { id: "glm", name: "GLM", protocol: "智谱 API", capabilities: ["text", "vision"], configured: false, mode: "mock" },
];
const stages: Record<string, string> = { classification: "任务分类", vision: "图像识别", image_recognition: "图像识别", hardware_analysis: "硬件分析", analysis: "并行分析", validation: "交叉验证", summary: "结果汇总", summarization: "结果汇总" };
const statusNames = { queued: "等待调度", running: "协作运行中", completed: "分析完成", failed: "执行失败", cancelled: "已取消", awaiting_review: "等待人工审核" };

function Icon({ name, size = 20 }: { name: "chip" | "arrow" | "upload" | "check" | "file" | "pulse" | "layers"; size?: number }) {
  const paths = {
    chip: <><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 1v5m6-5v5M9 18v5m6-5v5M1 9h5m-5 6h5m12-6h5m-5 6h5"/><rect x="9" y="9" width="6" height="6" rx="1"/></>,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6"/></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8m-8 4h5"/></>,
    pulse: <path d="M2 12h4l3-8 6 16 3-8h4"/>,
    layers: <><path d="m12 3 10 6-10 6L2 9Zm-9 11 9 5 9-5M3 18l9 5 9-5"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const data: { detail?: unknown } = await response.json();
      if (data.detail) message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch { /* proxy may return a plain text error */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function readable(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KiB`;
  return `${Number((bytes / 1024 / 1024).toFixed(1))} MiB`;
}

export default function Home() {
  const [providers, setProviders] = useState<Provider[]>(fallbackProviders);
  const [workflow, setWorkflow] = useState<Workflow>(defaults);
  const [prompt, setPrompt] = useState("请评审一款 24V 输入、5V / 3A 输出的 Buck 电源方案，重点检查拓扑、器件裕量、损耗与稳定性，并列出仍需数据手册和实测验证的项目。");
  const [files, setFiles] = useState<File[]>([]);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [maxBytes, setMaxBytes] = useState<number | null>(null);
  const [maxFiles, setMaxFiles] = useState(8);
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState("");
  const [pollRetry, setPollRetry] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const submitController = useRef<AbortController | null>(null);
  const busyTask = task?.status === "queued" || task?.status === "running";

  const loadConfiguration = useCallback(async (signal?: AbortSignal) => {
    setConnectionError("");
    try {
      const [catalog, supplierCatalog, config, capabilities] = await Promise.all([
        request<ModelCatalog>("/api/models", { signal }),
        request<Provider[]>("/api/providers", { signal }),
        request<Workflow>("/api/workflows/default", { signal }),
        request<Capabilities>("/api/capabilities", { signal }),
      ]);
      if (catalog.mode !== "mock") {
        setReady(false);
        setConnectionError("此工作台用于模拟流程。请将后端 HW_MODEL_MODE 设置为 mock 后重新连接；真实模型请使用对应的已验证配置入口。");
        return;
      }
      const registeredModels = catalog.models.filter(model => model.enabled && model.kind === "text").map(model => ({
        id: model.id, name: model.model, protocol: model.provider,
        capabilities: model.capabilities, configured: model.verified, mode: catalog.mode,
      }));
      setProviders([...supplierCatalog, ...registeredModels.filter(model => !supplierCatalog.some(provider => provider.id === model.id))]);
      // This workbench exposes explicit model checkboxes and role selects. Keep
      // their choices authoritative instead of silently rerouting them in auto mode.
      setWorkflow({ ...config, strategy: "manual", profile: "professional" });
      setMaxBytes(capabilities.limits?.max_upload_bytes ?? null);
      if (capabilities.limits?.max_files_per_task) setMaxFiles(capabilities.limits.max_files_per_task);
      setReady(true);
    } catch (err) {
      if (!signal?.aborted) { setReady(false); setConnectionError(`后端连接失败：${err instanceof Error ? err.message : "未知错误"}。请检查 FastAPI 服务并重新连接。`); }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadConfiguration(controller.signal);
    return () => { controller.abort(); submitController.current?.abort(); };
  }, [loadConfiguration]);

  useEffect(() => {
    if (!task || !["queued", "running"].includes(task.status)) return;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const updated = await request<Task>(`/api/tasks/${task.id}`, { signal: controller.signal });
        setTask(updated);
        setError("");
        if (["queued", "running"].includes(updated.status)) timeout = setTimeout(poll, 700);
      } catch (err) {
        if (!controller.signal.aborted) setError(`获取运行状态失败：${err instanceof Error ? err.message : "未知错误"}。可点击「重新获取状态」，当前任务不会重复提交。`);
      }
    };
    timeout = setTimeout(poll, 300);
    return () => { controller.abort(); clearTimeout(timeout); };
  }, [task?.id, task?.status, pollRetry]); // polling retains one loop per active task

  function addFiles(incoming: File[]) {
    setError("");
    const combined = [...files, ...incoming];
    if (combined.length > maxFiles) { setError(`每个任务最多上传 ${maxFiles} 个文件。`); return; }
    const tooLarge = maxBytes === null ? undefined : incoming.find(file => file.size > maxBytes);
    if (tooLarge && maxBytes !== null) { setError(`${tooLarge.name} 超过单文件 ${formatBytes(maxBytes)} 资源限制。`); return; }
    setFiles(combined);
    setUploads([]);
  }

  async function submit() {
    if (!prompt.trim() || busy || busyTask || !ready) return;
    if (!workflow.reviewers.length) { setError("请至少选择一个并行分析模型。"); return; }
    const controller = new AbortController();
    submitController.current = controller;
    setBusy(true); setError(""); setTask(null);
    try {
      // Successful uploads remain reusable across task submission retries.
      // Changing the file selection clears this cache to preserve index identity.
      const uploaded: Upload[] = [...uploads];
      for (let i = uploaded.length; i < files.length; i++) {
        const file = files[i];
        setUploading(`上传 ${i + 1} / ${files.length}：${file.name}`);
        const result = await request<Upload>(`/api/uploads?filename=${encodeURIComponent(file.name)}`, {
          method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file, signal: controller.signal,
        });
        uploaded.push(result); setUploads([...uploaded]);
      }
      setUploading("创建协作任务…");
      const created = await request<Task>("/api/tasks", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
        body: JSON.stringify({ prompt: prompt.trim(), file_ids: uploaded.map(file => file.id), workflow }),
      });
      setTask(created);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "提交失败，请稍后重试");
    } finally { setBusy(false); setUploading(""); }
  }

  function setReviewer(id: string) {
    if (!workflow.reviewers.includes(id) && workflow.reviewers.length >= 5) {
      setError("每个任务最多选择 5 个并行分析模型，请先取消一个选择。");
      return;
    }
    setError("");
    setWorkflow(current => ({ ...current, reviewers: current.reviewers.includes(id) ? current.reviewers.filter(item => item !== id) : [...current.reviewers, id] }));
  }

  return <div className="shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="Circuit Council 首页"><span className="brand-icon"><Icon name="chip" size={25}/></span><span>Circuit<span className="brand-second">Council</span></span></a>
      <div className="workspace-label">ENGINEERING WORKSPACE</div>
      <div className="nav-item active"><Icon name="layers"/>协作工作台<span className="nav-dot"/></div>
      <div className="sidebar-section"><span className="eyebrow">当前工作流</span><h3>硬件方案评审</h3><p>多模型分析、交叉验证与结果汇总。</p><div className="workflow-trace">分类<span>↓</span>图像识别（如有图片）<span>↓</span>并行硬件分析<span>↓</span>验证与汇总</div></div>
      <div className="sidebar-bottom"><div className="mock-dot"/> MOCK SANDBOX<p>模拟模型 · 无需 API Key</p><span>v0.1 · 本地开发 MVP</span></div>
    </aside>

    <main>
      <header className="topbar"><div className="breadcrumb">工作空间 <span>/</span> 硬件协作</div><span className={`connection ${ready ? "connected" : ""}`}><i/>{ready ? "后端已连接" : "等待后端连接"}</span></header>
      <div className="content">
        <div className="page-heading"><div><div className="eyebrow green">MULTI-MODEL ENGINEERING</div><h1>让每个设计，多一份验证。</h1><p>从电源方案到硬件细节，组织多个模型共同分析。</p></div><span className="version-tag">MVP / 模拟模式</span></div>
        <div className="notice"><Icon name="pulse"/><p><strong>当前使用模拟响应。</strong>用于验证协作流程，不会真正识别图片或解析附件，费用为模拟估算。工程结论需由数据手册、计算与实测确认。</p></div>
        {connectionError && <div className="error" role="alert">{connectionError}<button className="text-button" onClick={() => void loadConfiguration()}>重新连接</button></div>}

        <div className="workbench">
          <section className="panel composer" aria-labelledby="task-heading">
            <div className="section-heading"><span className="step-number">01</span><h2 id="task-heading">定义工程任务</h2><span className="muted">TASK BRIEF</span></div>
            <label className="field-label" htmlFor="prompt">设计背景与分析目标</label>
            <textarea id="prompt" value={prompt} onChange={e => setPrompt(e.target.value)} maxLength={20000} disabled={busy || busyTask} placeholder="描述输入输出指标、已知器件、约束与希望验证的问题…"/>
            <div className="prompt-footer"><span>越具体的设计约束，越清晰的评审路径</span><span>{prompt.length.toLocaleString()} / 20,000</span></div>

            <div className="field-header"><label className="field-label" htmlFor="files">工程附件</label><span>可选</span></div>
            <div className="dropzone" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!busy && !busyTask) addFiles(Array.from(e.dataTransfer.files)); }}>
              <input ref={input} id="files" type="file" multiple onChange={e => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} disabled={busy || busyTask}/>
              <span className="upload-icon"><Icon name="upload" size={24}/></span>
              <strong>拖入原理图、数据手册或代码</strong><span>图片、PDF、代码及一般二进制文件</span>
              <button className="secondary-button" type="button" disabled={busy || busyTask} onClick={() => input.current?.click()}>选择文件</button>
              <small>最多 {maxFiles} 个 · {maxBytes === null ? "大小受可用磁盘与运行环境约束" : `单文件资源配额 ≤ ${formatBytes(maxBytes)}`} · 分片与对象存储接口预留</small>
            </div>
            {files.length > 0 && <ul className="file-list">{files.map((file, index) => <li key={`${file.name}-${index}`}><Icon name="file" size={16}/><span>{file.name}<small>{formatBytes(file.size)} {uploads[index] ? "· 已上传" : ""}</small></span><button aria-label={`移除 ${file.name}`} disabled={busy || busyTask} onClick={() => { setFiles(current => current.filter((_, i) => i !== index)); setUploads([]); }}>×</button></li>)}</ul>}

            <div className="section-heading workflow-heading"><span className="step-number">02</span><h2>配置协作模型</h2><span className="muted">WORKFLOW</span></div>
            <p className="helper">选择并行分析模型。供应商保留独立协议适配边界。</p>
            <div className="model-grid">{providers.map(provider => <label className={`model-option ${workflow.reviewers.includes(provider.id) ? "selected" : ""}`} key={provider.id}><input type="checkbox" checked={workflow.reviewers.includes(provider.id)} onChange={() => setReviewer(provider.id)} disabled={busy || busyTask}/><span className="model-monogram">{provider.name.slice(0, 1)}</span><span><strong>{provider.name}</strong><small title={provider.protocol}>{provider.protocol}</small></span><span className="checkmark">{workflow.reviewers.includes(provider.id) && <Icon name="check" size={13}/>}</span></label>)}</div>
            <details className="advanced"><summary>高级工作流配置<span>超时 / 重试 / 角色</span></summary><div className="advanced-grid">
              {([ ["vision_provider", "图像识别模型"], ["validator", "验证模型"], ["summarizer", "汇总模型"] ] as const).map(([key, label]) => <label key={key}>{label}<select aria-label={label} value={workflow[key]} onChange={e => setWorkflow(current => ({ ...current, [key]: e.target.value }))} disabled={busy || busyTask}>{providers.filter(p => key !== "vision_provider" || p.capabilities.includes("vision")).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>)}
              {([ ["max_retries", "最大重试次数", 0, 5], ["max_parallel", "并行上限", 1, 5], ["timeout_seconds", "单次超时（秒）", 1, 120] ] as const).map(([key, label, min, max]) => <label key={key}>{label}<input type="number" value={workflow[key]} min={min} max={max} disabled={busy || busyTask} onChange={e => setWorkflow(current => ({ ...current, [key]: Number(e.target.value) }))}/></label>)}
            </div></details>
            {error && <div className="error" role="alert">{error}{busyTask && <button className="text-button" onClick={() => setPollRetry(current => current + 1)}>重新获取状态</button>}</div>}
            <button className="run-button" onClick={() => void submit()} disabled={!ready || busy || busyTask || !prompt.trim()}><span>{busy ? uploading : busyTask ? "模型正在协作…" : "开始协作分析"}</span>{busy || busyTask ? <span className="spinner"/> : <Icon name="arrow"/>}</button>
            <p className="execution-note">附件通过流式接口上传；不会执行代码或二进制文件。</p>
          </section>

          <section className="results" aria-labelledby="results-heading" aria-live="polite">
            <div className="panel result-panel"><div className="section-heading"><span className="step-number">03</span><h2 id="results-heading">协作观察台</h2>{task && <span className={`status-tag ${task.status}`}>{statusNames[task.status]}</span>}</div>
              {!task ? <div className="empty-state"><div className="circuit-art"><span/><Icon name="chip" size={46}/><span/></div><h3>下一次设计评审，从这里开始</h3><p>提交任务后，在这里查看模型调用、<br/>协作进度和可追溯的汇总结果。</p><div className="empty-steps"><span>分类</span><i>→</i><span>分析</span><i>→</i><span>验证</span></div></div> : <>
                <div className="task-id">任务 {task.id}</div>
                <div className="metrics"><div><span>输入 Token</span><strong>{(task.usage?.input_tokens ?? 0).toLocaleString()}</strong></div><div><span>输出 Token</span><strong>{(task.usage?.output_tokens ?? 0).toLocaleString()}</strong></div><div><span>模拟费用 · USD</span><strong>${Number(task.usage?.cost_usd ?? 0).toFixed(6)}</strong></div></div>
                <ol className="event-list">{(task.events ?? []).map((event, index) => <li key={index}><span className="event-dot"/><div><strong>{stages[event.stage] ?? event.stage}</strong><p>{event.message}</p></div><time>{new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</time></li>)}</ol>
                {task.error && <div className="error" role="alert">{task.error}</div>}
                {task.result?.summary != null && <div className="summary"><div className="eyebrow green">ENGINEERING SUMMARY · 模拟</div><h3>协作汇总</h3><pre>{readable(task.result.summary)}</pre></div>}
                {task.result && Object.entries(task.result).filter(([key, value]) => key !== "summary" && value != null).map(([key, value]) => <details className="result-detail" key={key}><summary>{stages[key] ?? ({ analyses: "各模型分析", image_analysis: "图像识别结果" }[key] ?? key)}</summary><pre>{readable(value)}</pre></details>)}
              </>}
            </div>
            {task && task.calls?.length > 0 && <div className="panel call-panel"><div className="section-heading"><Icon name="pulse"/><h2>模型调用记录</h2><span className="muted">{task.calls.length} 次尝试</span></div><div className="call-list">{task.calls.map((call, index) => <details key={index}><summary><span><strong>{providers.find(p => p.id === call.provider)?.name ?? call.provider}</strong><small>{stages[call.stage] ?? call.stage} · 尝试 {call.attempt}</small></span><span className={`call-status ${call.status}`}>{call.status}</span></summary><p>{call.input_tokens + call.output_tokens} Token · {Number(call.latency_ms).toFixed(0)} ms · ${Number(call.cost_usd).toFixed(6)}（模拟）</p>{call.error && <p className="error-text">{call.error}</p>}{call.output && <pre>{call.output}</pre>}</details>)}</div></div>}
            <div className="scope-card"><div className="eyebrow">工程工具边界</div><h3>可靠的流程，从明确边界开始。</h3><p>电源拓扑、器件检查、BOM、数据手册和波形工具保留扩展接口。当前模型输出为模拟；嵌入式软件 Agent、真实供应商调用与生产级存储尚未启用。</p><div><span>PostgreSQL</span><span>Redis</span><span>Object Storage</span><span>接口预留</span></div></div>
          </section>
        </div>
        <footer>CIRCUIT COUNCIL <span>·</span> Embedded hardware & power engineering <span className="footer-note">人机协作，工程师负责最终决策。</span></footer>
      </div>
    </main>
  </div>;
}
