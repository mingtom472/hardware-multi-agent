"""Conservative role-based V0.1 routing; not a trained optimal-model selector."""

from .registry import ModelRegistry, RegistryError
from .schemas import WorkflowConfig


def resolve_workflow(config: WorkflowConfig, prompt: str, attachments: list[dict], registry: ModelRegistry) -> WorkflowConfig:
    resolved = config.model_copy(deep=True)
    roles = dict(registry.roles)
    if config.strategy == "manual":
        roles.update(config.role_overrides)
    elif config.strategy == "hybrid":
        missing = set(config.locked_roles) - set(config.role_overrides)
        if missing:
            raise RegistryError("Each locked role requires a model override")
        roles.update({role: config.role_overrides[role] for role in config.locked_roles})
    elif config.role_overrides or config.locked_roles:
        raise RegistryError("Role overrides require manual or hybrid strategy")
    if set(roles) - (set(registry.roles) | {"orchestrator", "validator", "summarizer"}):
        raise RegistryError("Unknown engineering role")
    for model_id in roles.values():
        registry.get(model_id)
    resolved.role_models = roles
    if config.strategy != "manual" or config.role_overrides:
        profile_roles = {
            "quick": ["batch"],
            "professional": ["power_reasoning", "batch"],
            "expert": ["power_reasoning", "calculation", "datasheet"],
        }
        reviewers = [roles[key] for key in profile_roles[config.profile]]
        if any(item.get("content_type") == "application/pdf" for item in attachments):
            reviewers.append(roles["datasheet"])
        resolved.reviewers = list(dict.fromkeys(reviewers))
        resolved.vision_provider = roles["schematic"]
        resolved.validator = roles.get("validator", roles["calculation"])
        resolved.summarizer = roles.get("summarizer", roles["power_reasoning"])
    resolved.orchestrator = config.orchestrator or roles.get("orchestrator", roles["batch"])
    return resolved
