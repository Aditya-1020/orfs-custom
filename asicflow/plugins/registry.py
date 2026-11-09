"""Entry-point based extension registry for additional ASIC flow commands."""
from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

PLUGIN_GROUP = "asicflow.plugins"


class PluginError(RuntimeError):
    """A third-party ASIC flow plugin is invalid or failed to load."""


def discover_plugins() -> list[tuple[str, Callable[..., Any]]]:
    """Load callables from the asicflow.plugins entry-point group.

    Each callable must accept (subparsers, add_common_arguments).
    """
    discovered = entry_points()
    selected = (
        discovered.select(group=PLUGIN_GROUP)
        if hasattr(discovered, "select")
        else discovered.get(PLUGIN_GROUP, [])
    )
    plugins: list[tuple[str, Callable[..., Any]]] = []
    for item in selected:
        try:
            register = item.load()
        except Exception as exc:
            raise PluginError(f"failed to load plugin {item.name!r}: {exc}") from exc
        if not callable(register):
            raise PluginError(f"plugin {item.name!r} must resolve to a callable")
        plugins.append((item.name, register))
    return plugins


def register_cli_plugins(subparsers: Any, add_common_arguments: Callable) -> None:
    """Register installed third-party commands with the built-in CLI."""
    for name, register in discover_plugins():
        try:
            register(subparsers, add_common_arguments)
        except Exception as exc:
            raise PluginError(f"plugin {name!r} failed during CLI registration: {exc}") from exc
