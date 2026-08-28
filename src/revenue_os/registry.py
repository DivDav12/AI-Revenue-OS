"""A generic in-memory agent registry.

Maps agent names to instances and selects a capable agent for a Task.
"""

from __future__ import annotations

from .agent import Agent
from .messages import Task


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if agent.name in self._agents:
            raise ValueError(f"agent already registered: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    def find_for(self, task: Task) -> Agent | None:
        """Return the first agent able to handle the task, or None."""
        for agent in self._agents.values():
            if agent.can_handle(task):
                return agent
        return None
