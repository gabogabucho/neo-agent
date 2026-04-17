"""Brain (the Mind) — HOW Lumen thinks. The context assembler.

The brain is NOT intelligent. The LLM is intelligent. The brain assembles
the right context and lets the LLM decide everything.

The brain combines three sources into one prompt:
  - Consciousness (who I am — immutable soul)
  - Personality (who I am in this context — swappable)
  - Body/Registry (what I have — discovered at startup)
  + Active flow, memories, conversation history
"""

import json
from pathlib import Path

import yaml
from litellm import acompletion

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.consciousness import Consciousness
from lumen.core.memory import Memory
from lumen.core.personality import Personality
from lumen.core.registry import CapabilityKind, Registry
from lumen.core.session import Session


class Brain:
    """Lumen's brain — a context assembler, not a routing engine.

    Combines Consciousness + Personality + Body + Catalog into a prompt,
    lets the LLM decide, and executes tool calls.
    """

    def __init__(
        self,
        consciousness: Consciousness,
        personality: Personality,
        memory: Memory,
        connectors: ConnectorRegistry,
        registry: Registry,
        catalog: Catalog | None = None,
        model: str = "deepseek/deepseek-chat",
        flows: list[dict] | None = None,
        mcp_manager=None,
    ):
        self.consciousness = consciousness
        self.personality = personality
        self.memory = memory
        self.connectors = connectors
        self.registry = registry
        self.catalog = catalog or Catalog()
        self.model = model
        self.flows = flows or []
        self.mcp_manager = mcp_manager

    async def think(self, message: str, session: Session) -> dict:
        """Receive message -> assemble context -> LLM decides -> response."""

        # 1. Recall relevant memories
        memories = await self.memory.recall(message, limit=5)

        # 2. Check for flow triggers if no active flow
        if not session.active_flow:
            triggered = self._match_flow_trigger(message)
            if triggered:
                session.start_flow(triggered)

        # 3. Build context — Consciousness + Personality + Body + Catalog + State
        context = {
            "consciousness": self.consciousness.as_context(),
            "personality": self.personality.as_context(),
            "body": self.registry.as_context(),
            "catalog": self.catalog.as_context(
                installed_names={
                    c.name for c in self.registry.list_by_kind(CapabilityKind.MODULE)
                },
                registry=self.registry,
                connectors=self.connectors,
            ),
            "active_flow": session.active_flow,
            "filled_slots": session.slots,
            "pending_slots": session.get_pending_slots(),
            "memories": memories,
            "available_flows": self.flows,
        }

        # 4. Build prompt
        messages = self._build_prompt(context, message, session)

        # 5. LLM decides everything — connectors as tools + introspection
        tools = self.connectors.as_tools() or []

        # Add read_skill tool — progressive disclosure (from OpenClaw pattern)
        # The Body lists skill names/descriptions. This tool loads the full content.
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__read_skill",
                    "description": (
                        "Read the full instructions of one of my installed skills. "
                        "Use this when you need detailed guidance on how to perform "
                        "a specific task that matches a skill."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "description": "Name of the skill to read",
                            }
                        },
                        "required": ["skill_name"],
                    },
                },
            }
        )

        # Add module search tool — Lumen recommends modules for gaps
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__search_modules",
                    "description": (
                        "Search the module catalog for modules that can add "
                        "a capability I don't have yet. Use this when the user "
                        "asks for something I cannot do — find a module that "
                        "fills the gap and recommend installing it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What capability is needed",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        )

        tools = tools if tools else None

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

        # 7. Update session history (RAM) + persistent memory (SQLite)
        session.add_message("user", message)
        session.add_message("assistant", result["message"])

        # Persist conversation to SQLite — survives refresh and restart
        try:
            await self.memory.save_conversation_turn(
                session.session_id, "user", message
            )
            if result["message"]:
                await self.memory.save_conversation_turn(
                    session.session_id, "assistant", result["message"]
                )
        except Exception:
            pass  # Don't fail the response if persistence fails

        return result

    def _read_skill(self, arguments: str) -> dict:
        """Read full SKILL.md content for on-demand loading (progressive disclosure).

        The Body lists skill names and descriptions. This loads the full
        markdown instructions when the LLM decides it needs them.
        """
        params = json.loads(arguments) if arguments else {}
        skill_name = params.get("skill_name", "")

        cap = self.registry.get(CapabilityKind.SKILL, skill_name)
        if not cap:
            return {"error": f"Skill '{skill_name}' not found"}

        skill_path = cap.metadata.get("path")
        if not skill_path:
            return {"error": f"Skill '{skill_name}' has no file path"}

        try:
            content = Path(skill_path).read_text(encoding="utf-8")
            return {"skill": skill_name, "content": content}
        except Exception as e:
            return {"error": f"Cannot read skill '{skill_name}': {e}"}

    def _search_modules(self, arguments: str) -> dict:
        """Search the module catalog for modules that fill a capability gap."""
        params = json.loads(arguments) if arguments else {}
        query = params.get("query", "")

        results = self.catalog.find_for_gap(
            query,
            registry=self.registry,
            connectors=self.connectors,
        )
        if not results:
            return {
                "found": 0,
                "message": "No modules found for this capability. "
                "It could be built as a custom module.",
            }

        modules = []
        for mod in results[:3]:
            modules.append(
                {
                    "name": mod["name"],
                    "display_name": mod.get("display_name", mod["name"]),
                    "description": mod.get("description", ""),
                    "price": mod.get("price", "free"),
                    "compatibility": (mod.get("compatibility") or {}).get("status"),
                    "install_hint": f"Install from the Modules panel in the dashboard, "
                    f"or tell me 'install {mod['name']}' and I'll guide you.",
                }
            )

        return {"found": len(modules), "modules": modules}

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
        """Assemble the system prompt from Consciousness + Personality + Body.

        The prompt has clear sections:
        1. Consciousness — who I am (immutable soul)
        2. Personality — who I am in this context (swappable)
        3. Body — what I have (discovered at startup)
        4. Current state — active flow, memories, conversation
        """
        system_parts = [
            # CRITICAL RULES — the LLM MUST obey these
            "## RULES (you MUST follow these exactly)",
            "",
            "1. Your capabilities are EXACTLY what the Body section lists.",
            "2. If something is listed under 'What I CAN do' — you CAN do it. Do NOT say you need to install it.",
            "3. If something is listed under 'What I CANNOT do' — you CANNOT do it. Do NOT claim you can.",
            "4. NEVER invent capabilities not listed in the Body.",
            "5. When asked what you can do, ONLY list what the Body says.",
            "",
            # 1. CONSCIOUSNESS — the soul (never changes)
            context["consciousness"],
            "",
            # 2. PERSONALITY — context identity (changes per module)
            "## Personality (who I am in this context)",
            "",
            context["personality"],
            "",
            # 3. BODY — discovered capabilities (changes per install)
            context["body"],
        ]

        # 4. CATALOG — what modules exist to fill gaps
        if context.get("catalog"):
            system_parts.append("")
            system_parts.append(context["catalog"])

        # 4. CURRENT STATE — what's happening right now

        # Active flow context — slot filling instructions
        if context["active_flow"]:
            flow = context["active_flow"]
            system_parts.append(f"\n## Current Task: {flow.get('intent', 'unknown')}")
            system_parts.append(f"Filled slots: {json.dumps(context['filled_slots'])}")

            pending = context["pending_slots"]
            if pending:
                next_slot = pending[0]
                system_parts.append(
                    f"Next slot to fill: '{next_slot['name']}' "
                    f'— ask: "{next_slot.get("ask", "")}"'
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
            system_parts.append("\n## Memories (what I remember)")
            for mem in context["memories"]:
                system_parts.append(f"- [{mem['category']}] {mem['content']}")

        system_message = "\n".join(system_parts)

        # Build messages array
        messages = [{"role": "system", "content": system_message}]

        # Conversation history (last 10 messages)
        for msg in session.history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

        # Current message
        messages.append({"role": "user", "content": message})

        return messages

    @staticmethod
    def _coerce_args(params: dict, tool_name: str, tools: list[dict] | None) -> dict:
        """Coerce LLM arguments to match the declared schema types.

        LLMs frequently return "5" (string) when the schema says integer,
        or "true" (string) when it should be boolean. This prevents crashes.
        Learned from Hermes — 10 lines that save hours of debugging.
        """
        if not tools:
            return params

        # Find the schema for this tool
        schema = None
        for t in tools:
            if t.get("function", {}).get("name") == tool_name:
                schema = t["function"].get("parameters", {}).get("properties", {})
                break

        if not schema:
            return params

        coerced = {}
        for key, value in params.items():
            expected = schema.get(key, {}).get("type")
            if expected == "integer" and isinstance(value, str):
                try:
                    value = int(value)
                except ValueError:
                    pass
            elif expected == "number" and isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            elif expected == "boolean" and isinstance(value, str):
                value = value.lower() in ("true", "1", "yes")
            coerced[key] = value
        return coerced

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
                    # Introspection tools (neo__*) are handled by the brain
                    if func.name == "neo__read_skill":
                        tool_result = self._read_skill(func.arguments)
                        all_tool_calls.append(
                            {"name": func.name, "result": tool_result}
                        )
                    elif func.name == "neo__search_modules":
                        tool_result = self._search_modules(func.arguments)
                        all_tool_calls.append(
                            {"name": func.name, "result": tool_result}
                        )
                    elif self.connectors.has_tool(func.name):
                        params = json.loads(func.arguments) if func.arguments else {}
                        params = self._coerce_args(params, func.name, tools)
                        tool_result = await self.connectors.execute_tool(
                            func.name, params
                        )
                        all_tool_calls.append(
                            {"name": func.name, "result": tool_result}
                        )
                    else:
                        # Connector tools
                        connector_name, action = self.connectors.parse_tool_name(
                            func.name
                        )
                        params = json.loads(func.arguments) if func.arguments else {}
                        params = self._coerce_args(params, func.name, tools)
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
                    all_tool_calls.append({"name": func.name, "error": str(e)})

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
