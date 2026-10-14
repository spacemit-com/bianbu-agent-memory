"""Standalone MCP server exposing the bianbu.memory AgentMemory tools."""

from .server import create_server, main

__all__ = ["create_server", "main"]
