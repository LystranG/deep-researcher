import hashlib
import json
from typing import Final

SOURCE_COMPARISON_MANIFEST: Final[dict[str, object]] = {
    "schema": "deep-researcher.skill/v1",
    "executable": False,
    "instructions": "比较同一主张的多份来源，分别列出一致点、差异点和证据不足处。",
    "input_schema": {"type": "object", "required": ["question"]},
    "output_schema": {"type": "object", "required": ["comparison", "citations"]},
    "allowed_tools": ["document_search"],
    "required_capabilities": ["workspace_documents.read"],
}


def manifest_hash(manifest: dict[str, object]) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise ValueError("Skill manifest 必须是对象")
    if manifest.get("schema") != "deep-researcher.skill/v1":
        raise ValueError("Skill manifest schema 不受支持")
    if manifest.get("executable") is not False:
        raise ValueError("Skill 不允许包含可执行内容")
    required_keys = (
        "instructions",
        "input_schema",
        "output_schema",
        "allowed_tools",
        "required_capabilities",
    )
    for key in required_keys:
        if key not in manifest:
            raise ValueError(f"Skill manifest 缺少 {key}")
    if not isinstance(manifest["allowed_tools"], list) or not all(
        isinstance(tool, str) for tool in manifest["allowed_tools"]
    ):
        raise ValueError("Skill allowed_tools 必须是字符串列表")
    return manifest
