"""Brain — the context assembler. Neo's heart.

The brain is NOT intelligent. The LLM is intelligent. The brain assembles
the right context (personality, active flow, slots, connectors, memory)
and lets the LLM decide everything.

~200 lines. That's all it takes.
"""

import json
from pathlib import Path

import yaml
from litellm import acompletion

from neo.core.connectors import ConnectorRegistry
from neo.core.consciousness import Consciousness
from neo.core.memory import Memory
from neo.core.personality import Personality
from neo.core.session import Session


class Brain:
    """Neo's brain — a context assembler, not a routing engine.

    think() does 4 things:
    1. Assemble context (personality + flow + slots + connectors + memory)
    2. Build a dynamic prompt
    3. Let the LLM decide (with connectors exposed as tools)
    4. Process the structured response
    """

    def __init__(
        self,
        consciousness: Consciousness,
        personality: Personality,
        memory: Memory,
        connectors: ConnectorRegistry,
        model: str = "deepseek/deepseek-chat",
        flows: list[dict] | None = None,
    ):
        self.consciousness = consciousness
        self.personality = personality
        self.memory = memory
        self.connectors = connectors
        self.model = model
        self.flows = flows or []

    async def think(self, message: str, session: Session) -> dict:
        """Receive message -> assemble context -> LLM decides -> response."""

        # 1. Recall relevant memories
        memories = await self.memory.recall(message, limit=5)

        # 2. Check for flow triggers if no active flow
        if not session.active_flow:
            triggered = self._match_flow_trigger(message)
            if triggered:
                session.start_flow(triggered)

        # 3. Build context
        context = {
            "consciousness": self.consciousness.as_context(),
            "personality": self.personality.as_context(),
            "active_flow": session.active_flow,
            "filled_slots": session.slots,
            "pending_slots": session.get_pending_slots(),
            "memories": memories,
            "available_flows": self.flows,
        }

        # 4. Build prompt
        messages = self._build_prompt(context, message, session)

        # 5. LLM decides everything — connectors as tools
        tools = self.connectors.as_tools() or None

        try:
            response = await acompletion(
                model=self.model,
                messages=messages,
                tools=tools if tools else None,
                temperature=0.7,
                max_tokens=1024,
            )
        except Exception as e:
            return {"message": f"I had trouble thinking: {e}", "tool_calls": []}

        # 6. Tool use loop — if LLM called tools, execute and send results back
        result = await self._tool_use_loop(response, messages, tools)

        # 7. Update session history
        session.add_message("user", message)
        session.add_message("assistant", result["message"])

        return result

    def _match_flow_trigger(self, message: str) -> dict | None:
        """Check if a message matches any flow trigger."""
        msg_lower = message.lower()
        for flow in self.flows:
            for trigger in flow.get("triggers", []):
                if trigger.lower() in msg_lower:
                    return flow
        return None

    def _build_prompt(
        self, context: dict, message: str, session: Session
    ) -> list[dict]:
        """Assemble the system prompt dynamically from context."""
        system_parts = [
            context["consciousness"],
            "",
            context["personality"],
        ]

        # Active flow context — slot filling instructions
        if context["active_flow"]:
            flow = context["active_flow"]
            system_parts.append(
                f"\n## Active Flow: {flow.get('intent', 'unknown')}"
            )
            system_parts.append(
                f"Filled slots: {json.dumps(context['filled_slots'])}"
            )

            pending = context["pending_slots"]
            if pending:
                next_slot = pending[0]
                system_parts.append(
                    f"Next slot to fill: '{next_slot['name']}' "
                    f"— ask: \"{next_slot.get('ask', '')}\""
                )
                system_parts.append(
                    "Extract slot values from the user's message if possible. "
                    "If the message fills a slot, acknowledge it and move on. "
                    "If the message is off-topic, answer briefly and return "
                    "to the pending slot."
                )
            else:
                system_parts.append(
                    "All slots are filled. Execute the flow action and confirm."
                )

        # Available flows — for trigger detection
        elif context["available_flows"]:
            triggers = []
            for flow in context["available_flows"]:
                intent = flow.get("intent", "unknown")
                flow_triggers = flow.get("triggers", [])
                triggers.append(f"- {intent}: {flow_triggers}")
            system_parts.append("\n## Available Flows")
            system_parts.append(
                "If the user's message matches an intent, start the flow "
                "by asking for the first required slot."
            )
            system_parts.extend(triggers)

        # Relevant memories
        if context["memories"]:
            system_parts.append("\n## Relevant Memories")
            for mem in context["memories"]:
                system_parts.append(f"- [{mem['category']}] {mem['content']}")

        # Available connectors
        connectors = self.connectors.list()
        if connectors:
            system_parts.append("\n## Available Connectors")
            for conn in connectors:
                system_parts.append(
                    f"- {conn['name']}: {conn['actions']} — {conn['description']}"
                )

        system_message = "\n".join(system_parts)

        # Build messages array
        messages = [{"role": "system", "content": system_message}]

        # Conversation history (last 10 messages)
        for msg in session.history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

        # Current message
        messages.append({"role": "user", "content": message})

        return messages

    async def _tool_use_loop(
        self,
        response,
        messages: list[dict],
        tools: list[dict] | None,
        max_iterations: int = 3,
    ) -> dict:
        """Execute the tool use loop.

        Standard flow:
        1. LLM responds with tool_calls
        2. Execute tools, collect results
        3. Send tool results back to LLM
        4. LLM generates final text response
        5. Repeat if LLM calls more tools (up to max_iterations)

        Without this loop, tool results are lost and the user gets no response.
        """
        all_tool_calls = []

        for _ in range(max_iterations):
            choice = response.choices[0]
            msg = choice.message

            # No tool calls — we have the final text response
            if not msg.tool_calls:
                return {
                    "message": msg.content or "",
                    "tool_calls": all_tool_calls,
                }

            # Execute all tool calls
            # First, add the assistant's message (with tool calls) to context
            messages.append(msg.model_dump())

            for tool_call in msg.tool_calls:
                func = tool_call.function
                try:
                    connector_name, action = self.connectors.parse_tool_name(
                        func.name
                    )
                    params = (
                        json.loads(func.arguments)
                        if func.arguments
                        else {}
                    )
                    tool_result = await self.connectors.execute(
                        connector_name, action, params
                    )
                    all_tool_calls.append(
                        {
                            "connector": connector_name,
                            "action": action,
                            "result": tool_result,
                        }
                    )
                except Exception as e:
                    tool_result = {"error": str(e)}
                    all_tool_calls.append(
                        {"name": func.name, "error": str(e)}
                    )

                # Add tool result to messages for the LLM
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result),
                    }
                )

            # Send tool results back to LLM for final response
            try:
                response = await acompletion(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    temperature=0.7,
                    max_tokens=1024,
                )
            except Exception as e:
                return {
                    "message": f"I completed the action but had trouble responding: {e}",
                    "tool_calls": all_tool_calls,
                }

        # Max iterations reached — return what we have
        final_msg = response.choices[0].message.content or ""
        return {"message": final_msg, "tool_calls": all_tool_calls}

    def load_flows(self, flows_dir: str | Path):
        """Load flow definitions from a directory of YAML files."""
        flows_path = Path(flows_dir)
        if not flows_path.exists():
            return
        for flow_file in flows_path.glob("*.yaml"):
            with open(flow_file, encoding="utf-8") as f:
                flow = yaml.safe_load(f)
                if flow:
                    self.flows.append(flow)
