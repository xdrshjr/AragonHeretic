# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026  Philipp Emanuel Weidmann <pew@worldwidemann.com> + contributors

"""Projection discovery for heterogeneous transformer layer families."""

from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from torch.nn import Module


def _resolve_attribute(root: Any, path: str) -> Any | None:
    value = root
    for name in path.split("."):
        try:
            value = getattr(value, name)
        except (AttributeError, TypeError):
            return None
    return value


def _append_module(
    modules: dict[str, list[Module]], component: str, candidate: Any
) -> None:
    if candidate is None:
        return
    if not isinstance(candidate, Module):
        raise TypeError(f"Unexpected value for {component}: expected nn.Module")
    if all(id(existing) != id(candidate) for existing in modules[component]):
        modules[component].append(candidate)


def _append_expert_modules(layer: Module, modules: dict[str, list[Module]]) -> None:
    if "mlp.down_proj" not in modules:
        return
    expert_paths = (
        ("mlp.experts", "down_proj"),
        ("block_sparse_moe.experts", "w2"),
        ("feed_forward.experts", "w2"),
        ("moe.experts", "output_linear"),
    )
    for collection_path, projection_name in expert_paths:
        experts = _resolve_attribute(layer, collection_path)
        if experts is None:
            continue
        with suppress(TypeError):
            for expert in experts:
                _append_module(
                    modules,
                    "mlp.down_proj",
                    _resolve_attribute(expert, projection_name),
                )


def discover_layer_modules(
    layer: Module, target_components: Sequence[str]
) -> dict[str, list[Module]]:
    """Return supported projection modules grouped by logical component.

    Args:
        layer: Transformer layer whose known projection paths are inspected.
        target_components: Logical component names requested by configuration.

    Returns:
        Non-empty component-to-module groups with duplicate objects removed.

    Raises:
        TypeError: A known projection path resolves to a non-module value.
    """
    modules = {component: [] for component in dict.fromkeys(target_components)}
    direct_paths = (
        ("attn.o_proj", "self_attn.o_proj"),
        ("attn.o_proj", "linear_attn.out_proj"),
        ("mlp.down_proj", "mlp.down_proj"),
        ("attn.o_proj", "conv.out_proj"),
        ("mlp.down_proj", "feed_forward.w2"),
        ("attn.o_proj", "self_attn.out_proj"),
        ("mlp.down_proj", "shared_mlp.output_linear"),
    )
    for component, path in direct_paths:
        if component in modules:
            _append_module(modules, component, _resolve_attribute(layer, path))
    _append_expert_modules(layer, modules)
    return {component: values for component, values in modules.items() if values}
