"""单条清单审计的共享处理流水线（同步接口与异步作业后台共用）。

只做内存中的校验与求解，不触碰数据库；任何“预期内的单项失败”
（清单非法、根插件不存在、求解器异常）都归一为 :class:`ItemError`，
由调用方决定如何持久化，从而保证单项失败不影响其他项。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from .models import ManifestIn
from .solver import Solver

logger = logging.getLogger(__name__)


class RootPluginNotFound(Exception):
    """根插件版本不存在于注册表（单条接口映射为 422，批量/作业映射为单项错误）。"""


def root_label(manifest: ManifestIn) -> Optional[str]:
    if not manifest.plugin_name:
        return None
    if manifest.plugin_version:
        return f"{manifest.plugin_name}@{manifest.plugin_version}"
    return manifest.plugin_name


def solve_manifest(
    manifest: ManifestIn, registry: dict
) -> tuple[Any, Optional[str], Optional[str]]:
    """校验清单并运行求解器，返回 (result, root_label, root_version)。"""
    manifest.validate_root_consistency()

    # 根插件版本必须存在于注册表（若声明了根插件）
    if (
        manifest.plugin_name is not None
        and manifest.plugin_version not in registry.get(manifest.plugin_name, {})
    ):
        raise RootPluginNotFound(
            f"根插件 {manifest.plugin_name}@{manifest.plugin_version} 不存在于注册表；"
            f"可用版本：{sorted(registry.get(manifest.plugin_name, {}), reverse=True)}"
        )

    solver = Solver(registry)
    result = solver.solve(
        root_dependencies=[(d.name, d.range) for d in manifest.dependencies],
        root_name=manifest.plugin_name,
        root_version=manifest.plugin_version,
    )
    return result, root_label(manifest), manifest.plugin_version


def build_audit_body(
    manifest: ManifestIn, result: Any, root_label_value: Optional[str], root_version: Optional[str]
) -> tuple[str, str, dict]:
    """组装待持久化/返回的审计体，返回 (audit_id, created_at, body)。"""
    audit_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    body = {
        "id": audit_id,
        "created_at": created_at,
        "status": result.status,
        "root_plugin": root_label_value,
        "root_version": root_version,
        "explanation": result.explanation,
        "node_count": result.node_count,
        "resolution": result.resolution,
        "sources": result.sources,
        "conflicts": result.conflicts,
    }
    return audit_id, created_at, body


@dataclass
class ItemOk:
    """单项求解成功：携带落库所需的全部信息。"""

    manifest: ManifestIn
    result: Any
    audit_id: str
    created_at: str
    body: dict
    root_label: Optional[str]
    root_version: Optional[str]


@dataclass
class ItemError:
    """单项失败：结构化错误码与消息。"""

    code: str  # invalid_manifest | solver_error
    message: str
    details: Optional[list] = None


def process_manifest_raw(raw: Any, registry: dict) -> ItemOk | ItemError:
    """对一个原始（未校验）清单跑完整校验 + 求解流水线，异常全部归一。"""
    try:
        manifest = ManifestIn.model_validate(raw)
    except ValidationError as exc:
        return ItemError(
            code="invalid_manifest",
            message="清单校验失败",
            details=jsonable_encoder(exc.errors()),
        )

    try:
        result, label, version = solve_manifest(manifest, registry)
    except (RootPluginNotFound, ValueError) as exc:
        return ItemError(code="invalid_manifest", message=str(exc))
    except Exception:  # noqa: BLE001 - 单项求解异常不得影响其他项
        logger.exception("单项审计求解失败")
        return ItemError(code="solver_error", message="求解过程中发生内部错误")

    audit_id, created_at, body = build_audit_body(manifest, result, label, version)
    return ItemOk(
        manifest=manifest,
        result=result,
        audit_id=audit_id,
        created_at=created_at,
        body=body,
        root_label=label,
        root_version=version,
    )
