"""Engineering AI Studio V0.1: local Streamlit client for the FastAPI service.

Run from the repository root with:
  python -m streamlit run ui/streamlit_app.py --server.address 127.0.0.1
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

# Streamlit also supports invoking this file directly, without an installed ui package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_component import render_uploader  # noqa: E402

API_URL = os.environ.get("HW_API_URL", "http://127.0.0.1:8000").rstrip("/")
BROWSER_API_URL = os.environ.get("HW_BROWSER_API_URL", API_URL).rstrip("/")
ROLE_LABELS = {
    "schematic": "原理图识别",
    "schematic_vision": "原理图识别",
    "vision": "原理图识别",
    "power_reasoning": "电源拓扑与故障推理",
    "hardware_analysis": "硬件分析",
    "software": "嵌入式软件（接口预留）",
    "embedded_software": "嵌入式软件（接口预留）",
    "datasheet": "芯片资料分析",
    "calculation": "工程计算与复核",
    "verification": "独立复核",
    "execution": "自动执行（接口预留）",
    "agent_runtime": "Agent 运行时（接口预留）",
    "batch_check": "低成本批量检查",
    "image_generation": "生成式技术配图（接口预留）",
    "summary": "报告汇总",
    "orchestrator": "总控模型",
}
STATUS_LABELS = {
    "queued": "等待执行",
    "running": "执行中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断，可恢复",
    "awaiting_review": "等待人工审核",
}


class ApiError(Exception):
    """A user-displayable API failure with no credential values."""


def api(path: str, method: str = "GET", payload: dict | None = None) -> Any:
    try:
        with httpx.Client(base_url=API_URL, timeout=60, trust_env=False) as client:
            response = client.request(method, path, json=payload)
        if not response.is_success:
            try:
                detail = response.json().get("detail", f"HTTP {response.status_code}")
            except (ValueError, AttributeError):
                detail = f"HTTP {response.status_code}"
            raise ApiError(str(detail))
        if response.status_code == 204:
            return None
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text
    except httpx.HTTPError as exc:
        raise ApiError("无法连接本地 FastAPI 服务。请检查启动终端、API 地址与8000端口。") from exc


def fetch(path: str, default: Any = None) -> Any:
    try:
        return api(path)
    except ApiError as exc:
        st.warning(str(exc))
        return default


def pretty_bytes(value: int | None) -> str:
    if value is None:
        return "未设置产品固定上限"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def model_label(model: dict) -> str:
    state = "已验证" if model.get("verified") else "未验证"
    return f"{model.get('id')} · {model.get('model')} · {state}"


def show_task(record: dict, *, controls: bool = True) -> None:
    task_id = record["id"]
    status = record.get("status", "unknown")
    st.subheader(STATUS_LABELS.get(status, status))
    st.caption(f"任务 {task_id} · {record.get('created_at', '')}")
    usage = record.get("usage", {})
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("输入 Token", usage.get("input_tokens", 0))
    col2.metric("输出 Token", usage.get("output_tokens", 0))
    col3.metric("费用 USD", str(usage.get("cost_usd", "0")))
    col4.metric("费用依据", "模拟 / 本地估算" if usage.get("simulated", True) else "供应商返回 / 定价")
    if record.get("error"):
        st.error(record["error"])
    if controls:
        left, right = st.columns(2)
        with left:
            if st.button("取消此任务", key=f"cancel-{task_id}", disabled=status not in {"queued", "running"}):
                try:
                    api(f"/api/tasks/{task_id}/cancel", "POST", {})
                    st.rerun()
                except ApiError as exc:
                    st.error(str(exc))
        with right:
            if st.button("恢复 / 重试失败任务", key=f"retry-{task_id}", disabled=status not in {"failed", "cancelled", "interrupted"}):
                try:
                    retried = api(f"/api/tasks/{task_id}/retry", "POST", {})
                    st.session_state["active_task"] = retried.get("id", task_id)
                    st.rerun()
                except ApiError as exc:
                    st.error(str(exc))
    events = record.get("events", [])
    if events:
        st.write("阶段与进度")
        st.dataframe(events, width="stretch", hide_index=True)
    calls = record.get("calls", [])
    if calls:
        st.write("模型调用与阶段输出")
        st.dataframe([
            {key: call.get(key) for key in ("stage", "provider", "model", "attempt", "status", "input_tokens", "output_tokens", "cached_tokens", "cost_usd", "latency_ms")}
            for call in calls
        ], width="stretch", hide_index=True)
        with st.expander("查看各阶段原始输入、输出与错误"):
            st.json(calls)
    result = record.get("result", {})
    if result.get("summary"):
        st.markdown("#### 工程汇总")
        # Model Markdown is rendered without unsafe HTML support.
        st.markdown(str(result["summary"]))
    if result:
        with st.expander("完整结构化结果与任务规划"):
            st.json(result)
    with st.expander("任务配置与附件引用"):
        st.json({"prompt": record.get("prompt"), "file_ids": record.get("file_ids", []), "workflow": record.get("workflow", {}), "plan": record.get("plan")})


def workbench(registry: dict, capabilities: dict) -> None:
    st.subheader("AI 工程工作台")
    st.write("描述设计目标、工作条件和已有证据，由可配置模型角色完成有限阶段的工程协作。")
    mode = registry.get("mode", capabilities.get("mode", "unknown"))
    if mode == "mock":
        st.info("当前为 Mock 模式：响应、Token 与费用明确标注模拟。不会假装已识别原理图、解析整份资料或获得真实测量数据。")
    else:
        st.warning("当前为真实调用模式。提交前核对下方目标供应商；提示词和适用的附件内容将发送到这些供应商。未经连接测试验证的目标模型不能视为可用。")
    limits = capabilities.get("limits", {})
    storage = capabilities.get("storage", {})
    col1, col2 = st.columns(2)
    col1.metric("单文件产品限制", pretty_bytes(limits.get("max_upload_bytes")))
    col2.metric("当前磁盘可用空间", pretty_bytes(storage.get("free_bytes")) if storage.get("free_bytes") is not None else "后端未报告")
    render_uploader(BROWSER_API_URL, limits.get("max_upload_bytes"))
    if st.button("刷新文件列表", key="workbench-refresh-files"):
        st.rerun()
    files = fetch("/api/uploads", [])
    file_map = {record["id"]: record for record in files}
    selected_files = st.multiselect(
        "选择本次任务的原始附件",
        options=list(file_map),
        format_func=lambda key: f"{file_map[key]['filename']} · {pretty_bytes(file_map[key]['size'])}",
        help="只传递已保存原件的引用。解析器和外部模型上下文仍有各自限制。",
    )
    models = {model["id"]: model for model in registry.get("models", []) if model.get("enabled", True)}
    if not models:
        st.warning("模型注册表为空；请先启动后端并完成模型配置。")
        return
    roles = registry.get("roles", {})
    with st.form("create-task"):
        prompt = st.text_area("工程需求与可验证的完成条件", height=180, placeholder="例如：24V转5V/3A Buck，计算占空比并列出器件裕量与待实测项目。")
        col1, col2 = st.columns(2)
        profile = col1.selectbox("任务模式", ["quick", "professional", "expert"], index=1, format_func=lambda key: {"quick": "快速：低成本优先", "professional": "专业：主要模型与必要审核", "expert": "专家：复杂分析与独立复核"}[key])
        strategy = col2.selectbox("调度策略", ["auto", "manual", "hybrid"], format_func=lambda key: {"auto": "自动调度", "manual": "手动指定角色", "hybrid": "混合：锁定部分角色"}[key])
        st.caption("下面的角色选择用于手动策略；混合策略只使用勾选锁定的角色。自动策略使用后端规划，不强制经过全部角色。")
        overrides: dict[str, str] = {}
        locked: list[str] = []
        with st.expander("模型角色与锁定配置"):
            for role, default in roles.items():
                candidates = list(models)
                if not candidates:
                    continue
                left, right = st.columns([4, 1])
                selected = left.selectbox(ROLE_LABELS.get(role, role), candidates, index=candidates.index(default) if default in candidates else 0, format_func=lambda key: model_label(models[key]), key=f"role-{role}")
                is_locked = right.checkbox("锁定", key=f"lock-{role}")
                overrides[role] = selected
                if is_locked:
                    locked.append(role)
            orchestrator = st.selectbox("可配置总控模型", [""] + list(models), format_func=lambda key: "使用后端默认" if not key else model_label(models[key]))
        col1, col2, col3 = st.columns(3)
        budget = col1.number_input("任务预算上限 USD", min_value=0.0, value=1.0, step=0.1, format="%.4f")
        max_calls = col2.number_input("最大模型调用次数", min_value=1, max_value=100, value=16)
        retries = col3.number_input("单阶段最大重试次数", min_value=0, max_value=5, value=2)
        destination_ids = set(roles.values()) | set(overrides.values())
        destinations = sorted({models[key].get("provider", key) for key in destination_ids if key in models})
        st.caption("当前角色配置的目标供应商：" + "、".join(destinations) + "。自动规划会依据任务选择其中适用角色；Mock模式不发送网络请求。")
        consent = st.checkbox("已核对目标供应商，并同意在真实模式下发送本任务的必要内容", value=mode == "mock")
        submitted = st.form_submit_button("创建并执行工程任务", type="primary")
    if submitted:
        if not prompt.strip():
            st.error("请输入工程目标。")
            return
        if mode != "mock" and not consent:
            st.error("真实调用前必须确认第三方供应商与资料发送。")
            return
        workflow: dict[str, Any] = {
            "strategy": strategy,
            "profile": profile,
            "role_overrides": overrides if strategy == "manual" else {role: model for role, model in overrides.items() if role in locked} if strategy == "hybrid" else {},
            "locked_roles": locked if strategy == "hybrid" else [],
            "max_calls": int(max_calls),
            "max_cost_usd": str(budget),
            "max_retries": int(retries),
            "max_rounds": 1,
        }
        if orchestrator:
            workflow["orchestrator"] = orchestrator
        try:
            task = api("/api/tasks", "POST", {"prompt": prompt.strip(), "file_ids": selected_files, "workflow": workflow})
            st.session_state["active_task"] = task["id"]
            st.success("任务已保存并提交。打开「执行监控」查看实时阶段和中间结果。")
        except ApiError as exc:
            st.error(str(exc))


def model_management(registry: dict) -> None:
    st.subheader("模型管理与连接测试")
    st.caption("目标名称不代表已验证存在。真实 API model ID、账号权限和能力需通过供应商连接测试确认。测试会发起一次低 Token 的真实请求，可能产生费用；Mock流程不替代真实连接测试。")
    st.info("密钥只在本机 .env 或安全环境变量中设置。此界面不接收、不保存也不显示 API Key 值；修改后重启后端进程使环境配置生效。")
    entries = registry.get("models", [])
    st.dataframe([
        {"配置ID": model["id"], "供应商": model.get("provider"), "目标模型ID": model.get("model"), "协议能力": ", ".join(model.get("capabilities", [])), "连接已验证": model.get("verified", False), "密钥变量": model.get("credential_env"), "密钥已配置": model.get("credential_present", False), "类型": model.get("kind", "text")}
        for model in entries
    ], width="stretch", hide_index=True)
    if not entries:
        return
    ids = [model["id"] for model in entries]
    selected = st.selectbox("编辑模型配置", ids)
    model = next(item for item in entries if item["id"] == selected)
    with st.form("edit-model"):
        base_url = st.text_input("供应商 Base URL（非密钥）", value=model.get("base_url", ""))
        model_id = st.text_input("真实 API Model ID（需要测试验证）", value=model.get("model", ""))
        st.code(f"{model.get('credential_env', 'HW_PROVIDER_API_KEY')}=<在本机 .env 中填写，不要提交到 Git>", language="text")
        pricing = model.get("pricing", {})
        left, right = st.columns(2)
        input_price = left.number_input("输入价格 USD / 百万 Token", min_value=0.0, value=float(pricing.get("input_per_million", 0)), format="%.6f")
        output_price = right.number_input("输出价格 USD / 百万 Token", min_value=0.0, value=float(pricing.get("output_per_million", 0)), format="%.6f")
        enabled = st.checkbox("允许路由选择此模型", value=model.get("enabled", True))
        parameter_json = st.text_area("供应商调用参数 JSON（非密钥）", value=json.dumps(model.get("parameters", {}), ensure_ascii=False, indent=2), help="例如 temperature、max_tokens。不同供应商仅接受各自已支持的参数；不得在此写 API Key。")
        save = st.form_submit_button("保存非密钥配置")
    if save:
        try:
            parameters = json.loads(parameter_json)
            if not isinstance(parameters, dict):
                raise ValueError
        except (ValueError, TypeError):
            st.error("调用参数必须是有效 JSON 对象。")
            return
        edited = []
        for entry in entries:
            clean = {key: value for key, value in entry.items() if key != "credential_present"}
            if entry["id"] == selected:
                clean.update({"base_url": base_url.strip(), "model": model_id.strip(), "enabled": enabled, "verified": False, "parameters": parameters})
                clean["pricing"] = {**pricing, "input_per_million": str(input_price), "output_per_million": str(output_price)}
            edited.append(clean)
        try:
            api("/api/models", "PUT", {"models": edited, "roles": registry.get("roles", {})})
            st.success("已保存；修改的模型需重新测试连接。")
        except ApiError as exc:
            st.error(str(exc))
    confirmed = st.checkbox("确认向该供应商发送一次真实连接测试请求（可能收费）", key="confirm-test")
    if st.button("测试所选模型的真实 API 连接", disabled=not confirmed):
        try:
            result = api(f"/api/models/{selected}/test", "POST", {})
            st.json(result)
        except ApiError as exc:
            st.error(str(exc))
    with st.expander("八类工程工作默认路由（可配置，不代表效果评测）"):
        st.dataframe([{"工程角色": ROLE_LABELS.get(role, role), "配置ID": target} for role, target in registry.get("roles", {}).items()], width="stretch", hide_index=True)


@st.fragment(run_every="2s")
def execution_monitor() -> None:
    st.subheader("智能体执行监控")
    task_id = st.session_state.get("active_task")
    if not task_id:
        st.info("尚未选择任务。先创建任务，或从历史页面打开已有任务。")
        return
    record = fetch(f"/api/tasks/{task_id}")
    if record:
        show_task(record)


def history() -> None:
    st.subheader("任务历史、恢复与本地删除")
    tasks = fetch("/api/tasks", [])
    if not tasks:
        st.info("暂无任务记录。")
        return
    st.dataframe([{"任务ID": task["id"], "状态": STATUS_LABELS.get(task.get("status"), task.get("status")), "需求": task.get("prompt", "")[:120], "创建时间": task.get("created_at"), "费用USD": task.get("usage", {}).get("cost_usd", 0)} for task in tasks], width="stretch", hide_index=True)
    ids = [task["id"] for task in tasks]
    selected = st.selectbox("选择历史任务", ids)
    record = next(task for task in tasks if task["id"] == selected)
    if st.button("在执行监控打开此任务"):
        st.session_state["active_task"] = selected
        st.success("已选中，请打开「执行监控」。")
    show_task(record, controls=False)
    report = f"# Engineering AI Studio 工程报告\n\n任务: {selected}\n\n状态: {record.get('status')}\n\n## 原始需求\n\n{record.get('prompt', '')}\n\n## 结果（Mock/真实依据见费用与调用记录）\n\n{record.get('result', {}).get('summary', '尚无汇总')}\n\n## 结构化结果与证据\n\n```json\n{json.dumps(record, ensure_ascii=False, indent=2)}\n```\n"
    st.download_button("导出 Markdown 报告", report, file_name=f"engineering-{selected}.md", mime="text/markdown")
    confirm = st.checkbox("确认从本机删除此任务记录；上传原件将单独保留", key=f"delete-task-confirm-{selected}")
    if st.button("删除任务记录", disabled=not confirm):
        try:
            api(f"/api/tasks/{selected}", "DELETE")
            if st.session_state.get("active_task") == selected:
                del st.session_state["active_task"]
            st.rerun()
        except ApiError as exc:
            st.error(str(exc))
    st.caption("人工纠错案例库、审核工作流和模型效果评测尚未实现；导出任务不代表自动训练模型。")


def costs() -> None:
    st.subheader("成本控制台")
    st.write("费用记录来自实际已保存的调用；模拟估算与供应商返回需分别查看。预算用于预检、预留和后续调用停止，不能保证外部供应商账单绝对不超估计。")
    report = fetch("/api/costs", {})
    st.json(report)
    if st.button("准备费用 CSV 导出"):
        try:
            exported = api("/api/costs/export")
            st.download_button("下载费用 CSV", str(exported), file_name="engineering-costs.csv", mime="text/csv")
        except ApiError as exc:
            st.error(str(exc))
    st.caption("每任务预算、最大调用次数和重试次数在工作台设定；供应商定价在模型管理中更新。")


def file_management(capabilities: dict) -> None:
    st.subheader("文件管理中心")
    st.info("原件与解析状态分开保存。未知格式允许安全存储；上传不赋予执行权限，不自动解压，不声称模型可一次读取完整大文件。")
    storage = capabilities.get("storage", {})
    if storage:
        st.json(storage)
    files = fetch("/api/uploads", [])
    if not files:
        st.info("暂无上传原件。")
        return
    st.dataframe([{"文件ID": file["id"], "文件名": file["filename"], "大小": pretty_bytes(file["size"]), "解析状态": file.get("parser_status", "unparsed"), "当前支持解析": file.get("parser_supported", False), "存储位置": file.get("storage_location", f"local:{file['id']}"), "SHA256": file.get("sha256")} for file in files], width="stretch", hide_index=True)
    file_map = {file["id"]: file for file in files}
    selected = st.selectbox("选择要管理的原始文件", list(file_map), format_func=lambda key: file_map[key]["filename"])
    st.json(file_map[selected])
    confirm = st.checkbox("确认删除本地原件及其元数据", key="delete-file-confirm")
    if st.button("删除选中文件", disabled=not confirm):
        try:
            api(f"/api/uploads/{selected}", "DELETE")
            st.rerun()
        except ApiError as exc:
            st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="Engineering AI Studio", page_icon="⚡", layout="wide")
    st.title("Engineering AI Studio")
    st.caption("V0.1 · 嵌入式硬件与电源工程 · 本机工作台")
    with st.sidebar:
        st.subheader("本地服务")
        st.caption("FastAPI " + API_URL)
        st.caption("浏览器上传 " + BROWSER_API_URL)
        if st.button("刷新后端状态"):
            st.rerun()
        st.warning("当前界面只适用于可信本机使用；未提供多用户鉴权。请保持127.0.0.1绑定。")
    health = fetch("/health")
    if not health:
        st.error("后端尚未就绪。请在 PowerShell 运行 scripts/start.ps1，并查看 FastAPI 启动错误。")
        st.stop()
    registry = fetch("/api/models", {"models": [], "roles": {}, "mode": health.get("mode")})
    capabilities = fetch("/api/capabilities", {})
    tabs = st.tabs(["AI 工程工作台", "模型管理", "执行监控", "任务历史", "成本控制台", "文件管理"])
    with tabs[0]:
        workbench(registry, capabilities)
    with tabs[1]:
        model_management(registry)
    with tabs[2]:
        execution_monitor()
    with tabs[3]:
        history()
    with tabs[4]:
        costs()
    with tabs[5]:
        file_management(capabilities)


if __name__ == "__main__":
    main()
