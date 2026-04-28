"""Brain (the Mind) — HOW Lumen thinks. The context assembler.

The brain is NOT intelligent. The LLM is intelligent. The brain assembles
the right context and lets the LLM decide everything.

The brain combines three sources into one prompt:
  - Consciousness (who I am — immutable soul)
  - Personality (who I am in this context — swappable)
  - Body/Registry (what I have — discovered at startup)
  - Awareness (what just changed in my body)
  + Active flow, memories, conversation history
"""

import json
import logging
import re
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import yaml
from litellm import acompletion

from lumen.core.artifact_setup import parse_artifact_action
from lumen.core.awareness import CapabilityAwareness
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.consciousness import Consciousness
from lumen.core.memory import Memory
from lumen.core.personality import Personality
from lumen.core.registry import CapabilityKind, Registry
from lumen.core.model_router import ModelRouter, ModelRouterConfig
from lumen.core.provider_health import ProviderHealthTracker
from lumen.core.tool_policy import ToolPolicy, ToolRisk
from lumen.core.confirmation_gate import ConfirmationGate, ConfirmDecision
from lumen.core.session import Session


logger = logging.getLogger(__name__)


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
        marketplace=None,
        capability_awareness: CapabilityAwareness | None = None,
        language: str = "en",
        api_key_env: str | None = None,
        flow_action_handler=None,
        config: dict | None = None,
        model_router: ModelRouter | None = None,
        provider_health: ProviderHealthTracker | None = None,
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
        self.marketplace = marketplace
        self.capability_awareness = capability_awareness
        self.language = (language or "en").lower()
        self.api_key_env = api_key_env
        self.flow_action_handler = flow_action_handler
        self.config = config or {}
        self.model_router = model_router or ModelRouter(
            ModelRouterConfig.from_config(config)
        )
        self.provider_health = provider_health or ProviderHealthTracker.from_config(config)
        self.tool_policy = ToolPolicy()
        self.tool_policy.load_defaults()
        self.tool_policy.load_config(config)
        self.confirmation_gate = ConfirmationGate()
        self._last_detected_language: str = self.language
        self._current_messages: list[dict] | None = None  # For contradiction retry
        self._cached_lessons_text: str = ""  # Pre-loaded lessons for prompt injection

    async def _persist_tool_output(
        self, tool_name: str, result: Any, session_id: str = ""
    ) -> None:
        """Persist tool results as structured outputs when they look like rich data.

        Only persists results that are non-trivial (dicts with content, lists, etc.)
        to avoid cluttering the output store with simple status strings.
        """
        if not self.memory or not result:
            return

        # Skip simple status strings and errors
        if isinstance(result, str) and len(result) < 20:
            return
        if isinstance(result, dict) and result.get("error"):
            return

        from lumen.core.output_types import OutputType, StructuredOutput

        # Detect output type from tool name
        output_type = OutputType.TEXT
        metadata = {"source_tool": tool_name}

        if "plot" in tool_name or "chart" in tool_name or "graph" in tool_name:
            output_type = OutputType.PLOT
        elif "image" in tool_name or "screenshot" in tool_name:
            output_type = OutputType.IMAGE
        elif "web" in tool_name or "html" in tool_name or "render" in tool_name:
            output_type = OutputType.WEB
        elif "doc" in tool_name or "report" in tool_name or "pdf" in tool_name:
            output_type = OutputType.DOCUMENT

        content = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result

        output = StructuredOutput(
            type=output_type,
            content=content[:10_000],  # Truncate very large outputs
            metadata=metadata,
            session_id=session_id,
        )
        try:
            await self.memory.save_output(output)
        except Exception as e:
            logger.debug(f"Failed to persist tool output: {e}")

    async def _check_tool_confirmation(
        self, tool_name: str, action: str, params: dict | None = None
    ) -> ConfirmDecision | None:
        """Check if a tool requires confirmation and ask the user.

        Returns None if no confirmation needed, or the decision if asked.
        """
        # Determine the tool key for policy lookup
        # Try connector.action format first, then standalone tool
        policy = self.tool_policy.get_policy(tool_name, action)
        if not self.tool_policy.requires_confirmation(policy):
            return None

        logger.info(
            "Tool requires confirmation: %s__%s (risk=%s)",
            tool_name, action, policy.risk,
        )

        response = await self.confirmation_gate.ask(
            tool_name=tool_name,
            action=action,
            risk=policy.risk,
            params=params,
            description=policy.description,
        )
        return response.decision

    def _resolved_model(self, role: str = "main") -> str:
        """Route model through OpenRouter when OpenRouter creds are active.

        Uses model_router for role-based routing if available.
        """
        model = self.model_router.get_model(role) if self.model_router else (self.model or "")
        if self.api_key_env == "OPENROUTER_API_KEY" and not model.startswith("openrouter/"):
            return f"openrouter/{model}"
        return model

    def _infer_current_provider_name(self, model: str) -> str:
        """Infer provider name from model string for health tracking."""
        if not self.provider_health or not model:
            return "unknown"
        # Check if any registered provider matches this model
        for name, entry in self.provider_health._providers.items():
            if entry.model == model:
                return name
        # Fallback: extract from model prefix
        if "/" in model:
            return model.split("/")[0]
        return "unknown"

    def _guard_capability_claims(self, response: str) -> str:
        """Verify the LLM response doesn't claim capabilities Lumen doesn't have.

        Only corrects when the response AFFIRMS having/using a capability
        that's not installed or ready. Passes through responses that are
        already truthful (acknowledging it's missing) or about installing.
        """
        claimed = self._detect_capability_mentions(response)
        if not claimed:
            return response

        false_claims = []
        for name in claimed:
            cap = self._find_capability(name)
            if cap is not None and cap.is_ready():
                continue
            # Only flag if the response AFFIRMS having/using it
            if self._is_affirmative_claim(response, name):
                false_claims.append(name)

        if not false_claims:
            return response

        correction_parts = []
        for name in false_claims:
            cap = self._find_capability(name)
            if cap is None:
                correction_parts.append(
                    f"No tengo {name} instalado. ¿Querés que lo instale?"
                )
            else:
                correction_parts.append(
                    f"{name} no está listo para usar todavía."
                )

        return " ".join(correction_parts)

    _AFFIRMATIVE_PATTERNS = (
        r"(?:tengo|I have |I can |puedo usar|puedo enviar|está configurado|"
        r"is configured|está listo|is ready|está disponible|is available|"
        r"ya puedo|funciona|works|ready to use|"
        r"^sí,|^yes,|^si,|^claro)"
    )

    _NEGATIVE_PATTERNS = (
        r"(?:no tengo|I don't have|I cannot|no puedo|no está|is not |"
        r"instalar|install|quiero instalar|want to install|"
        r"configurar|setup|falta|missing|need to|necesito)"
    )

    def _is_affirmative_claim(self, response: str, capability_name: str) -> bool:
        """Check if the response AFFIRMS having a capability (vs just mentioning it)."""
        text_lower = response.lower()
        sentences = re.split(r"[.!?]", text_lower)
        for sentence in sentences:
            if capability_name not in sentence:
                continue
            if re.search(self._NEGATIVE_PATTERNS, sentence):
                continue
            if re.search(self._AFFIRMATIVE_PATTERNS, sentence):
                return True
        return False

    # Patterns that indicate the model is denying it has a specific capability
    _DENIAL_PATTERNS = (
        r"(?:no tengo|I don't have|I cannot|no puedo|I cannot use|"
        r"I can't|no está|no está disponible|is not available|"
        r"no dispongo|I don't have access|no tengo acceso|"
        r"no está instalado|is not installed|not available|not ready)"
    )

    # Patterns that indicate the model is claiming it can do something.
    # These are pure affirmative markers without negation handling —
    # negation is handled separately in _is_denial_of_capability.
    _ABILITY_PATTERNS = (
        r"(?:puedo(?!\s+usar)|I can(?!\s+use)|I could|can use|would be able|"
        r"tengo(?!\s+el?|un)|I have|"
        r"está disponible(?!\s+para)|is available|listo|ready to|going to)"
    )

# Negation patterns — if these phrases appear near a capability name,
    # the response is a denial. Use IGNORECASE flag when matching.
    _NEGATION_WORDS = (
        r"(?:no\s+(?:tengo|puedo|esta|tiene|dispongo)|"
        r"i\s+(?:do['']?n['']?t\s+have|dont\s+have|can['']?t(?:\s+(?:use|have|access|run|execute))?|cant(?:\s+(?:use|have|access|run|execute))?|cannot)|"
        r"is\s+not\s+(?:available|installed|ready)|"
        r"no\s+esta\s+(?:disponible|instalado|listo))"
    )

    # Affirmation words — pure affirmative markers near a capability name.
    # These indicate the model is claiming it has the capability.
    _ABILITY_PATTERNS = (
        r"(?:puedo(?!\s+(?:usar|ejecutar|hacer))|"
        r"I\s+can(?!\s+(?:use|execute|do|run|have|access))|"
        r"I\s+could|can\s+use|would\s+be\s+able|"
        r"tengo(?!\s+(?:el?|un|una)\s)|"
        r"I\s+have|"
        r"está\s+disponible(?!\s+para)|"
        r"is\s+available(?!\s+for)|"
        r"listo|ready\s+to|going\s+to)"
    )

    def _detect_capability_denial(self, response: str) -> list[dict]:
        """Detect when the LLM wrongly denies a capability it actually has READY.

        Returns a list of dicts with the capability name and a truth correction.
        Only flags denials of capabilities that are actually READY.
        """
        denied = self._detect_capability_mentions(response)
        if not denied:
            return []

        denials: list[dict] = []
        for name in denied:
            cap = self._find_capability(name)
            # Only flag if the capability IS READY (the contradiction)
            if cap is not None and cap.is_ready():
                if self._is_denial_of_capability(response, name):
                    dn = cap.metadata.get("display_name") or cap.name
                    denials.append({
                        "name": name,
                        "display_name": dn,
                        "capability": cap,
                    })

        return denials

    def _is_denial_of_capability(self, response: str, capability_name: str) -> bool:
        """Check if the response DENIES having a specific capability.

        Uses proximity: if negation phrases ("no tengo", "I don't have")
        appear near the capability name, the response is a denial.
        Pure affirmations ("tengo", "I have") cancel the denial only if
        they appear WITHOUT a nearby negation.
        """
        text_lower = response.lower()
        sentences = re.split(r"[.!?]", text_lower)
        for sentence in sentences:
            if capability_name not in sentence:
                continue
            name_pos = sentence.find(capability_name)
            # Build proximity window around the name (±20 chars)
            start = max(0, name_pos - 20)
            end = name_pos + len(capability_name) + 20
            nearby = sentence[start:end]

            has_negation = bool(re.search(self._NEGATION_WORDS, nearby, re.IGNORECASE))
            has_affirmation = bool(re.search(self._ABILITY_PATTERNS, nearby, re.IGNORECASE))

            # Denial if negation is near, regardless of affirmation.
            # An affirmation only cancels a denial when it appears in a
            # SEPARATE clause without negation (the overall sentence check
            # already handles this by splitting on [.!?]).
            if has_negation:
                return True
        return False

    async def _retry_with_contradiction_evidence(
        self,
        messages: list[dict],
        denials: list[dict],
        original_response: str,
        tools: list[dict] | None,
    ) -> dict:
        """Retry the LLM with direct evidence about READY capabilities.

        If the LLM denied a READY capability, show it the truth and
        ask it to reformulate. One retry only — if it persists, correct directly.
        """
        directive = self._build_contradiction_directive(denials)
        retry_messages = messages[:-1] if messages else []  # Remove the wrong response
        retry_messages.append({
            "role": "user",
            "content": (
                f"{directive}\n\n"
                f"Your previous response contained a contradiction:\n"
                f'"{original_response}"\n\n'
                f"Reformulate your answer acknowledging that you do have "
                f"the capability mentioned above, and complete the user's request."
            ),
        })

        try:
            options = self._completion_options(purpose="contradiction", tools=tools)
            response = await acompletion(
                model=options["model"],
                messages=retry_messages,
                tools=options.get("tools"),
                temperature=options["temperature"],
                max_tokens=options["max_tokens"],
            )
            return self._safe_extract_content(response)
        except Exception:
            return ""

    def _build_contradiction_directive(self, denials: list[dict]) -> str:
        """Build a directive that shows the LLM its actual READY capabilities."""
        lines = [
            "## Contradiction Correction — READ THESE FACTS:",
            "The capability you just denied is actually READY right now. "
            "Here is the exact status from your live Registry:",
        ]
        for item in denials:
            cap = item["capability"]
            dn = item["display_name"]
            provides = cap.provides[:3] if cap.provides else []
            provides_str = ", ".join(provides) if provides else "general capability"
            lines.append(
                f"- **{dn}**: READY (status={cap.status.value}) "
                f"→ provides: {provides_str}"
            )

        lines.append(
            "\nDo NOT repeat the previous wrong answer. "
            "Use this tool now and respond correctly to the user."
        )
        return "\n".join(lines)
        """Check if the response AFFIRMS having a capability (vs just mentioning it)."""
        import re
        text_lower = response.lower()
        # Find sentences containing the capability name
        sentences = re.split(r'[.!?]', text_lower)
        for sentence in sentences:
            if capability_name not in sentence:
                continue
            # If the sentence already denies having it, it's truthful
            if re.search(self._NEGATIVE_PATTERNS, sentence):
                continue
            # If the sentence affirms having/using it, it's a false claim
            if re.search(self._AFFIRMATIVE_PATTERNS, sentence):
                return True
        return False

    def _detect_capability_mentions(self, text: str) -> list[str]:
        """Detect capability/channel names mentioned in a response."""
        text_lower = text.lower()
        found = []
        for name in self._known_capability_names():
            if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
                found.append(name)
        return found

    def _known_capability_names(self) -> list[str]:
        """Collect all known capability names from registry and catalog."""
        names = set()
        for cap in self.registry.all():
            names.add(cap.name.lower())
            dn = cap.metadata.get("display_name")
            if dn:
                names.add(dn.lower())
        for mod in self.catalog.modules:
            names.add(mod.get("name", "").lower())
            dn = mod.get("display_name")
            if dn:
                names.add(dn.lower())
        # Remove empty and very short names (would cause false positives)
        return [n for n in names if len(n) >= 3]

    def _find_capability(self, name: str):
        """Find a capability by name or display_name (case-insensitive)."""
        name_lower = name.lower()
        for cap in self.registry.all():
            if cap.name.lower() == name_lower:
                return cap
            dn = cap.metadata.get("display_name")
            if dn and dn.lower() == name_lower:
                return cap
        return None

    def _language_directive(
        self, message: str | None = None, session: Session | None = None,
        *,
        language_override: str | None = None,
    ) -> str:
        if language_override:
            resolved_language = language_override
        else:
            resolved_language = self._resolve_conversation_language(message, session)
        mapping = {
            "es": "Respond in Spanish (español rioplatense, natural y cálido).",
            "en": "Respond in English.",
            "pt": "Respond in Portuguese.",
            "fr": "Respond in French.",
            "it": "Respond in Italian.",
            "de": "Respond in German.",
        }
        directive = mapping.get(
            resolved_language,
            f"Respond in the user's language naturally (code: {resolved_language}).",
        )
        if resolved_language != self.language:
            config_language = mapping.get(
                self.language,
                f"the configured default locale ({self.language})",
            )
            return (
                f"Default locale hint: {config_language} "
                f"But in this conversation, follow the user's actual language. {directive}"
            )
        return directive

    def _resolve_conversation_language(
        self, message: str | None = None, session: Session | None = None
    ) -> str:
        detected = self._detect_obvious_language(message)
        if detected:
            self._last_detected_language = detected
            return detected

        if session:
            recent_user_messages = [
                item.get("content", "")
                for item in reversed(session.history)
                if item.get("role") == "user"
            ]
            for previous_message in recent_user_messages[:3]:
                detected = self._detect_obvious_language(previous_message)
                if detected:
                    self._last_detected_language = detected
                    return detected

        return self.language

    @classmethod
    def _detect_obvious_language(cls, message: str | None) -> str | None:
        text = str(message or "").strip()
        if not text:
            return None

        lowered = text.lower()
        normalized = cls._normalize_message(text)
        tokens = set(re.findall(r"[a-z]+", normalized))
        if not tokens:
            return None

        spanish_markers = {
            "hola",
            "buenas",
            "gracias",
            "quiero",
            "necesito",
            "puedo",
            "puedes",
            "ayuda",
            "ayudame",
            "ayudar",
            "configurar",
            "telegram",
            "como",
            "que",
        }
        english_markers = {
            "hello",
            "hi",
            "thanks",
            "thank",
            "please",
            "want",
            "need",
            "help",
            "configure",
            "setup",
            "what",
            "how",
        }

        spanish_score = len(tokens & spanish_markers)
        english_score = len(tokens & english_markers)

        if any(char in lowered for char in "¿¡ñáéíóú"):
            spanish_score += 1

        if spanish_score > english_score and spanish_score >= 1:
            return "es"
        if english_score > spanish_score and english_score >= 1:
            return "en"
        return None

    async def _prepare_think_context(self, message: str, session: Session):
        """Prepare context, messages, and tools for the LLM call.

        Returns (context, messages, tools) tuple.
        """
        # 1. Recall relevant memories
        memories = await self.memory.recall(message, limit=5)

        # 2. Build context — Consciousness + Personality + Body + Catalog + State
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

        # 3. Build tools
        tools = self.connectors.as_tools() or []

        # Add read_skill tool — progressive disclosure (from OpenClaw pattern)
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

        # Add module setup save tool — persists collected env values
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__save_module_setup",
                    "description": (
                        "Save configuration values for a module that needs setup. "
                        "Call this ONLY when the user has explicitly provided concrete "
                        "values (tokens, IDs, keys) during a setup conversation. "
                        "Do NOT call this with conversational text or acknowledgments. "
                        "Values are validated and normalized automatically."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "module_name": {
                                "type": "string",
                                "description": "The module name (e.g. 'x-lumen-comunicacion-telegram')",
                            },
                            "values": {
                                "type": "object",
                                "description": "Map of env var names to their values (e.g. {\"TELEGRAM_BOT_TOKEN\": \"123:ABC\"})",
                            },
                        },
                        "required": ["module_name", "values"],
                    },
                },
            }
        )

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__save_artifact_setup",
                    "description": (
                        "Save configuration values for any pending artifact setup flow "
                        "(for example MCP servers or native modules)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "description": "Artifact kind (native, mcp, manual, external)",
                            },
                            "artifact_id": {
                                "type": "string",
                                "description": "Artifact identifier (module/server id)",
                            },
                            "values": {
                                "type": "object",
                                "description": "Map of setup variable names to their values",
                            },
                        },
                        "required": ["kind", "artifact_id", "values"],
                    },
                },
            }
        )

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__check_capability",
                    "description": (
                        "Check whether I actually have a specific capability right now. "
                        "Searches my Registry for matching skills, connectors, modules, "
                        "channels, and MCP servers — across all statuses. "
                        "If nothing matches in the Registry, also checks the Catalog "
                        "for installable options. "
                        "Call this BEFORE claiming you can or cannot do something, "
                        "especially when a user asks about a specific capability by name."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "What capability to look for. "
                                    "Can be a name (e.g. 'telegram', 'web-search'), "
                                    "a description (e.g. 'send messages'), "
                                    "or a concept (e.g. 'email', 'calendar')."
                                ),
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        )

        # 4. Build prompt (tools must be built first for Phase 2.4)
        messages = self._build_prompt(context, message, session, tools)

        # Store messages for contradiction retry (Phase 2.3)
        self._current_messages = messages[:]

        tools = tools if tools else None

        return context, messages, tools

    async def think(self, message: str, session: Session) -> dict:
        """Receive message -> assemble context -> LLM decides -> response."""

        offer_result = await self._maybe_handle_pending_setup_offer(message, session)
        if offer_result is not None:
            return await self._finalize_turn(session, message, offer_result)

        flow_result = await self._maybe_handle_runtime_flow(message, session)
        if flow_result is not None:
            return await self._finalize_turn(session, message, flow_result)

        # 2. Check for flow triggers if no active flow
        if not session.active_flow:
            triggered = self._match_flow_trigger(message)
            if triggered:
                session.start_flow(triggered)
                flow_result = await self._maybe_handle_runtime_flow(message, session)
                if flow_result is not None:
                    return await self._finalize_turn(session, message, flow_result)

            # 2b. Natural-language setup: if the user mentions a module that
            # has pending setup (by alias), start the flow directly. This
            # prevents the LLM from trying to handle setup conversationally
            # and failing to persist values.
            setup_match = self._match_natural_setup(message)
            if setup_match is not None:
                session.start_flow(setup_match)
                flow_result = await self._maybe_handle_runtime_flow(message, session)
                if flow_result is not None:
                    return await self._finalize_turn(session, message, flow_result)

            # 2c. User explicitly requests setup ("configurémolo", "setup")
            # and there's exactly one pending flow — start it without making
            # the user name the module.
            if self._message_requests_setup(message):
                setup_flows = [
                    f for f in self.flows if self._supports_runtime_flow(f)
                ]
                if len(setup_flows) == 1:
                    session.start_flow(setup_flows[0])
                    flow_result = await self._maybe_handle_runtime_flow(
                        message, session
                    )
                    if flow_result is not None:
                        return await self._finalize_turn(session, message, flow_result)
                elif len(setup_flows) > 1:
                    # Multiple flows — ask which one, don't guess
                    artifacts = []
                    for flow in setup_flows:
                        aid = self._flow_artifact_id(flow)
                        if aid:
                            artifacts.append({
                                "id": aid,
                                "display_name": str(
                                    flow.get("display_name") or aid
                                ),
                                "kind": str(flow.get("kind") or "native"),
                            })
                    return await self._finalize_turn(
                        session,
                        message,
                        {
                            "message": self._render_setup_offer_clarification(
                                artifacts
                            ),
                        },
                    )

        context, messages, tools = await self._prepare_think_context(message, session)

        try:
            options = self._completion_options(purpose="main", tools=tools)
            start_time = time.monotonic()
            response = await acompletion(
                model=options["model"],
                messages=messages,
                tools=options.get("tools"),
                temperature=options["temperature"],
                max_tokens=options["max_tokens"],
            )
            elapsed = time.monotonic() - start_time
            # Record provider health if available
            if self.provider_health:
                provider_name = self._infer_current_provider_name(options["model"])
                self.provider_health.record_success(provider_name, latency=elapsed)
        except Exception as e:
            # Record failure if provider health is tracking
            if self.provider_health:
                provider_name = self._infer_current_provider_name(
                    self._resolved_model() if hasattr(self, '_resolved_model') else ""
                )
                self.provider_health.record_failure(provider_name, error=str(e))
            return {"message": f"I had trouble thinking: {e}", "tool_calls": []}

        # 6. Tool use loop — if LLM called tools, execute and send results back
        result = await self._tool_use_loop(response, messages, tools)

        return await self._finalize_turn(session, message, result)

    def _build_synthetic_response(self, content: str, buffered_tool_calls: list[dict]):
        """Build a synthetic response object from streamed tool call fragments."""
        from types import SimpleNamespace

        tool_calls = []
        for tc in buffered_tool_calls:
            if tc["function"]["name"]:
                func = SimpleNamespace(
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                )
                tool_calls.append(
                    SimpleNamespace(
                        id=tc["id"] or f"stream-{uuid.uuid4()}",
                        type="function",
                        function=func,
                    )
                )

        msg = SimpleNamespace(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            model_dump=lambda: {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
                if tool_calls
                else None,
            },
        )

        choice = SimpleNamespace(message=msg)
        return SimpleNamespace(choices=[choice])

    async def think_stream(self, message: str, session: Session):
        """Stream LLM response as an async generator."""

        offer_result = await self._maybe_handle_pending_setup_offer(message, session)
        if offer_result is not None:
            result = await self._finalize_turn(session, message, offer_result)
            yield {"type": "delta", "content": result.get("message", "")}
            return

        flow_result = await self._maybe_handle_runtime_flow(message, session)
        if flow_result is not None:
            result = await self._finalize_turn(session, message, flow_result)
            yield {"type": "delta", "content": result.get("message", "")}
            return

        # Check for flow triggers if no active flow
        if not session.active_flow:
            triggered = self._match_flow_trigger(message)
            if triggered:
                session.start_flow(triggered)
                flow_result = await self._maybe_handle_runtime_flow(message, session)
                if flow_result is not None:
                    result = await self._finalize_turn(session, message, flow_result)
                    yield {"type": "delta", "content": result.get("message", "")}
                    return

            setup_match = self._match_natural_setup(message)
            if setup_match is not None:
                session.start_flow(setup_match)
                flow_result = await self._maybe_handle_runtime_flow(message, session)
                if flow_result is not None:
                    result = await self._finalize_turn(session, message, flow_result)
                    yield {"type": "delta", "content": result.get("message", "")}
                    return

            if self._message_requests_setup(message):
                setup_flows = [
                    f for f in self.flows if self._supports_runtime_flow(f)
                ]
                if len(setup_flows) == 1:
                    session.start_flow(setup_flows[0])
                    flow_result = await self._maybe_handle_runtime_flow(
                        message, session
                    )
                    if flow_result is not None:
                        result = await self._finalize_turn(session, message, flow_result)
                        yield {"type": "delta", "content": result.get("message", "")}
                        return
                elif len(setup_flows) > 1:
                    artifacts = []
                    for flow in setup_flows:
                        aid = self._flow_artifact_id(flow)
                        if aid:
                            artifacts.append({
                                "id": aid,
                                "display_name": str(
                                    flow.get("display_name") or aid
                                ),
                                "kind": str(flow.get("kind") or "native"),
                            })
                    result = await self._finalize_turn(
                        session,
                        message,
                        {
                            "message": self._render_setup_offer_clarification(
                                artifacts
                            ),
                        },
                    )
                    yield {"type": "delta", "content": result.get("message", "")}
                    return

        context, messages, tools = await self._prepare_think_context(message, session)

        try:
            options = self._completion_options(purpose="main", tools=tools, stream=True)
            response = await acompletion(
                model=options["model"],
                messages=messages,
                tools=options.get("tools"),
                temperature=options["temperature"],
                max_tokens=options["max_tokens"],
                stream=True,
            )
        except Exception as e:
            yield {"type": "error", "content": f"I had trouble thinking: {e}"}
            return

        # Stream processing
        full_content = ""
        buffered_tool_calls = []
        has_tool_calls = False

        async for chunk in response:
            choice = chunk.choices[0]
            delta = choice.delta
            finish_reason = choice.finish_reason

            if delta.content:
                full_content += delta.content
                yield {"type": "delta", "content": delta.content}

            if delta.tool_calls:
                has_tool_calls = True
                for tc in delta.tool_calls:
                    idx = tc.index
                    while len(buffered_tool_calls) <= idx:
                        buffered_tool_calls.append(
                            {"id": "", "function": {"name": "", "arguments": ""}}
                        )
                    if getattr(tc, "id", None):
                        buffered_tool_calls[idx]["id"] = tc.id
                    func = getattr(tc, "function", None)
                    if func:
                        if getattr(func, "name", None):
                            buffered_tool_calls[idx]["function"]["name"] = func.name
                        if getattr(func, "arguments", None):
                            buffered_tool_calls[idx]["function"]["arguments"] += func.arguments

            if finish_reason == "tool_calls":
                has_tool_calls = True

        if has_tool_calls and buffered_tool_calls:
            # Build synthetic response and run tool use loop (streaming version)
            synthetic_response = self._build_synthetic_response(
                full_content, buffered_tool_calls
            )
            result = None
            async for event in self._tool_use_loop_streaming(
                synthetic_response, messages, tools
            ):
                if event.get("type") == "delta":
                    yield event  # Final response text
                    result = event.get("_result")
                elif event.get("type") == "tool_progress":
                    yield event  # "About to run tool X..."
                elif event.get("type") == "tool_result":
                    yield event  # "Tool X completed: ..."
                elif event.get("type") == "tool_status":
                    yield event  # "Iteration 2/3..."

            # Fallback: if streaming didn't produce a result, run the blocking version
            if result is None:
                result = await self._tool_use_loop(
                    synthetic_response, messages, tools
                )
                if result.get("message"):
                    yield {"type": "delta", "content": result["message"]}
        else:
            result = {"message": full_content, "tool_calls": []}

        # Finalize turn
        await self._finalize_turn(session, message, result)

    async def think_proactive(self) -> str | None:
        """Generate a proactive announcement when capabilities changed.

        Called by the heartbeat when awareness has pending changes.
        Uses consciousness + awareness only — no conversation context needed.
        The LLM decides whether and how to announce. Returns None if
        awareness is empty or the LLM has nothing to say.
        """
        if not self.capability_awareness or not self.capability_awareness.has_pending_proactive():
            return None

        awareness_text = self.capability_awareness.format_for_proactive()
        if not awareness_text:
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    f"{self.consciousness.as_context()}\n\n"
                    f"{self._language_directive(language_override=self._last_detected_language)}\n\n"
                    f"{awareness_text}\n\n"
                    "You just noticed something changed in your capabilities. "
                    "Briefly and naturally tell the user about it. "
                    "One or two sentences max. Use your own words. "
                    "If the change seems minor or irrelevant, respond with "
                    "an empty string — don't force an announcement."
                ),
            }
        ]

        try:
            options = self._completion_options(purpose="proactive")
            response = await acompletion(
                model=options["model"],
                messages=messages,
                max_tokens=options["max_tokens"],
                temperature=options["temperature"],
            )
            content = self._safe_extract_content(response)
            return content.strip() if content.strip() else None
        except Exception:
            return None

    def _read_skill(self, arguments: str) -> dict:
        """Read full SKILL.md content for on-demand loading (progressive disclosure).

        The Body lists skill names and descriptions. This loads the full
        markdown instructions when the LLM decides it needs them.
        """
        params = json.loads(arguments) if arguments else {}
        skill_name = params.get("skill_name", "")

        cap = self.registry.get(CapabilityKind.SKILL, skill_name)
        if not cap:
            for candidate in self.registry.list_by_kind(CapabilityKind.SKILL):
                aliases = (candidate.metadata or {}).get("aliases", [])
                if skill_name in aliases:
                    cap = candidate
                    break
        if not cap:
            return {"error": f"Skill '{skill_name}' not found"}

        skill_path = cap.metadata.get("path")
        if not skill_path:
            return {"error": f"Skill '{skill_name}' has no file path"}

        try:
            content = Path(skill_path).read_text(encoding="utf-8")
            content = self._interpolate_skill_content(content, cap)
            return {"skill": skill_name, "content": content}
        except Exception as e:
            return {"error": f"Cannot read skill '{skill_name}': {e}"}

    def _interpolate_skill_content(self, content: str, cap) -> str:
        """Replace {KEY} placeholders with safe public module config only."""
        metadata = getattr(cap, "metadata", {}) or {}
        module_name = metadata.get("module_name")

        values: dict[str, str] = {}
        cfg = self.config or {}
        if isinstance(cfg, dict):
            for key, value in cfg.items():
                if isinstance(value, (str, int, float, bool)):
                    values[str(key)] = str(value)

            if module_name:
                module_cfg = (cfg.get("secrets") or {}).get(module_name) or {}
                if isinstance(module_cfg, dict):
                    public_values = module_cfg.get("public")
                    if isinstance(public_values, dict):
                        for key, value in public_values.items():
                            if isinstance(value, (str, int, float, bool)):
                                values[str(key)] = str(value)
                    else:
                        # Legacy flat module config: interpolate only clearly non-sensitive keys.
                        for key, value in module_cfg.items():
                            if self._is_safe_public_skill_value(key, value):
                                values[str(key)] = str(value)

        def replace(match):
            key = match.group(1)
            return values.get(key, match.group(0))

        return re.sub(r"\{([A-Z0-9_]+)\}", replace, content)

    def _is_safe_public_skill_value(self, key: str, value) -> bool:
        if not isinstance(value, (str, int, float, bool)):
            return False
        key_upper = str(key).upper()
        sensitive_markers = (
            "_TOKEN",
            "_KEY",
            "_SECRET",
            "PASSWORD",
            "BEARER",
            "CLIENT_SECRET",
        )
        return not any(marker in key_upper for marker in sensitive_markers)

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

    def _check_capability(self, arguments: str) -> dict:
        """Verify whether a specific capability exists and is ready.

        Searches the Registry for fuzzy matches across name, description,
        provides, tags, and display_name. Returns structured results
        separating READY from not-ready matches. Falls back to Catalog
        search when nothing is found in the Registry.
        """
        params = json.loads(arguments) if arguments else {}
        query = str(params.get("query", "")).strip().lower()
        if not query:
            return {"error": "query is required"}

        matches: list[dict] = []
        for cap in self.registry.all():
            score = self._score_capability_match(query, cap)
            if score > 0:
                matches.append({"score": score, "capability": cap})

        matches.sort(key=lambda m: m["score"], reverse=True)

        if matches:
            ready = []
            not_ready = []
            for entry in matches[:10]:
                cap = entry["capability"]
                display_name = cap.metadata.get("display_name") or cap.name
                item = {
                    "name": cap.name,
                    "display_name": display_name,
                    "kind": cap.kind.value,
                    "status": cap.status.value,
                    "description": cap.description,
                    "provides": cap.provides,
                }
                if cap.is_ready():
                    ready.append(item)
                else:
                    item["blocker"] = self._describe_blocker(cap)
                    not_ready.append(item)

            result: dict = {"found": len(matches), "query": query}
            if ready:
                result["ready"] = ready
            if not_ready:
                result["not_ready"] = not_ready
            return result

        catalog_results = self.catalog.find_for_gap(
            query,
            registry=self.registry,
            connectors=self.connectors,
        )
        if catalog_results:
            installable = []
            for mod in catalog_results[:3]:
                installable.append({
                    "name": mod["name"],
                    "display_name": mod.get("display_name", mod["name"]),
                    "description": mod.get("description", ""),
                })
            return {
                "found": 0,
                "query": query,
                "message": (
                    "No matching capability is installed or registered. "
                    "However, these modules from the catalog could provide it."
                ),
                "installable": installable,
            }

        return {
            "found": 0,
            "query": query,
            "message": (
                "No matching capability found — not installed and not "
                "available in the catalog. This would need to be built "
                "as a custom module."
            ),
        }

    @staticmethod
    def _score_capability_match(query: str, cap) -> int:
        """Score how well a capability matches a query. 0 = no match."""
        score = 0
        display_name = str(cap.metadata.get("display_name") or "").lower()
        tags = [str(t).lower() for t in cap.metadata.get("tags", [])]
        # Normalize separators so "send email" matches "send_email"
        query_norm = query.replace(" ", "_").replace("-", "_")

        if query in cap.name.lower() or query_norm in cap.name.lower():
            score += 10
        if query in display_name:
            score += 8
        for prov in cap.provides:
            prov_lower = prov.lower()
            if query in prov_lower or query_norm in prov_lower or prov_lower in query_norm:
                score += 7
        if query in cap.description.lower():
            score += 5
        for tag in tags:
            if query in tag or query_norm in tag or tag in query_norm:
                score += 3

        return score

    @staticmethod
    def _describe_blocker(cap) -> str:
        """Human-readable blocker reason for a not-ready capability."""
        pending_setup = cap.metadata.get("pending_setup") or {}
        env_specs = pending_setup.get("env_specs") or []
        if env_specs:
            names = ", ".join(
                spec.get("label") or spec.get("name") or "unknown"
                for spec in env_specs
            )
            return f"needs setup: {names}"
        if cap.metadata.get("error"):
            return f"error: {cap.metadata['error']}"
        if cap.status.value == "missing_deps":
            return "missing dependencies"
        if cap.status.value == "no_handler":
            return "missing handler implementation"
        if cap.status.value == "available":
            return "not configured yet"
        return "not ready"

    async def _save_module_setup(self, arguments: str) -> dict:
        """Save collected module setup values through the normalization pipeline."""
        if self.flow_action_handler is None:
            return {"error": "Setup persistence is not available in this mode."}

        params = json.loads(arguments) if arguments else {}
        module_name = str(params.get("module_name") or "").strip()
        values = params.get("values")
        if not module_name:
            return {"error": "module_name is required"}
        if not isinstance(values, dict) or not values:
            return {"error": "values must be a non-empty object with env var names as keys"}

        return await self.flow_action_handler(
            f"save_module_env:{module_name}", values
        )

    async def _save_artifact_setup(self, arguments: str) -> dict:
        """Save collected setup values for any artifact kind."""
        if self.flow_action_handler is None:
            return {"error": "Setup persistence is not available in this mode."}

        params = json.loads(arguments) if arguments else {}
        kind = str(params.get("kind") or "").strip()
        artifact_id = str(params.get("artifact_id") or "").strip()
        values = params.get("values")
        if parse_artifact_action(f"save_artifact_env:{kind}:{artifact_id}") is None:
            return {"error": "kind and artifact_id must describe a valid setup target"}
        if not isinstance(values, dict) or not values:
            return {"error": "values must be a non-empty object with setup keys as names"}

        return await self.flow_action_handler(
            f"save_artifact_env:{kind}:{artifact_id}", values
        )

    def _match_flow_trigger(self, message: str) -> dict | None:
        """Check if a message matches any flow trigger."""
        msg_lower = message.lower()
        for flow in self.flows:
            for trigger in flow.get("triggers", []):
                if trigger.lower() in msg_lower:
                    return flow
        return None

    def _match_natural_setup(self, message: str) -> dict | None:
        """Detect when the user mentions a module with pending setup by name.

        This bridges natural-language mentions ("hablar por telegram",
        "conectarme a github") into the deterministic setup flow, preventing
        the LLM from trying to handle setup conversationally and failing
        to persist values.

        Returns the flow if exactly one module matches, None otherwise.
        """
        matches: list[dict] = []
        for flow in self.flows:
            if not self._supports_runtime_flow(flow):
                continue
            artifact_id = self._flow_artifact_id(flow)
            if not artifact_id:
                continue
            if self._message_mentions_module(message, artifact_id):
                matches.append(flow)
        return matches[0] if len(matches) == 1 else None

    async def _maybe_handle_runtime_flow(
        self, message: str, session: Session
    ) -> dict | None:
        flow = session.active_flow
        if not flow or not self._supports_runtime_flow(flow):
            return None

        pending = session.get_pending_slots()
        if not pending:
            return await self._complete_runtime_flow(session)

        if not session.flow_prompted:
            session.flow_prompted = True
            return {
                "message": self._render_flow_prompt(flow, pending[0]),
                "user_message_for_history": message,
            }

        current_slot = pending[0]
        value = str(message or "").strip()
        if not value:
            ask = str(current_slot.get("ask") or "").strip() or "Necesito ese dato."
            return {
                "message": ask,
                "user_message_for_history": "",
            }
        session.fill_slot(current_slot["name"], value)

        remaining = session.get_pending_slots()
        if remaining:
            return {
                "message": self._render_next_slot_message(current_slot, remaining[0]),
                "user_message_for_history": self._flow_history_value(
                    current_slot, value
                ),
            }

        completed = await self._complete_runtime_flow(session)
        completed["user_message_for_history"] = self._flow_history_value(
            current_slot, value
        )
        return completed

    async def _complete_runtime_flow(self, session: Session) -> dict:
        flow = session.active_flow or {}
        slots = dict(session.slots)
        action = str(flow.get("on_complete") or "").strip()
        session.complete_flow()

        if not action:
            return {"message": "Listo."}
        if self.flow_action_handler is None:
            return {"message": "Listo."}

        outcome = await self.flow_action_handler(action, slots, session=session)
        if isinstance(outcome, dict):
            message = str(outcome.get("message") or "").strip()
            if message:
                return {"message": message, "action_result": outcome}
        return {"message": "Listo.", "action_result": outcome}

    async def _maybe_handle_pending_setup_offer(
        self, message: str, session: Session
    ) -> dict | None:
        if session.active_flow:
            return None

        offer = session.pending_setup_offer or {}
        artifacts = self._offer_artifacts(offer)
        artifacts = [
            a for a in artifacts
            if self._find_setup_flow(a["id"]) is not None
        ]
        if not artifacts:
            session.pending_setup_offer = None
            return None

        module_ids = [a["id"] for a in artifacts]

        if self._match_flow_trigger(message):
            return None

        explicit_setup_request = self._message_requests_setup(message)
        selected = self._match_setup_offer_module(message, module_ids)
        if selected:
            return await self._start_pending_setup_flow(selected, session)

        if explicit_setup_request:
            if len(artifacts) == 1:
                return await self._start_pending_setup_flow(module_ids[0], session)
            session.pending_setup_offer = {"artifacts": artifacts, "turns_remaining": 1}
            return {
                "message": self._render_setup_offer_clarification(artifacts),
                "preserve_pending_setup_offer": True,
            }

        allow_affirmative_reply = int(offer.get("turns_remaining") or 0) > 0
        if len(artifacts) == 1 and allow_affirmative_reply and self._is_affirmative_reply(message):
            return await self._start_pending_setup_flow(module_ids[0], session)

        if len(artifacts) > 1 and allow_affirmative_reply and self._is_affirmative_reply(message):
            session.pending_setup_offer = {"artifacts": artifacts, "turns_remaining": 1}
            return {
                "message": self._render_setup_offer_clarification(artifacts),
                "preserve_pending_setup_offer": True,
            }

        return None

    async def _start_pending_setup_flow(
        self, module_name: str, session: Session, *, trigger_message: str | None = None
    ) -> dict:
        flow = self._find_setup_flow(module_name)
        if flow is None:
            session.pending_setup_offer = None
            return {"message": "No encontré esa configuración pendiente."}
        session.start_flow(flow)
        # If the user already provided a value (trigger_message), skip the
        # initial prompt and capture it as the first slot value directly.
        if trigger_message:
            session.flow_prompted = True
            return await self._maybe_handle_runtime_flow(
                trigger_message, session
            ) or {"message": "Listo."}
        return await self._maybe_handle_runtime_flow(module_name, session) or {
            "message": "Listo."
        }

    async def _finalize_turn(self, session: Session, message: str, result: dict) -> dict:
        original_message = result["message"]
        result["message"] = self._guard_capability_claims(result["message"])
        self._update_pending_setup_offer(session, result)

        # Phase 2.3: Contradiction retry — when LLM denies a READY capability
        if self._current_messages is not None:
            denials = self._detect_capability_denial(original_message)
            if denials:
                tools = self.connectors.as_tools() or []
                tools = tools if tools else None
                corrected = await self._retry_with_contradiction_evidence(
                    self._current_messages,
                    denials,
                    original_message,
                    tools,
                )
                if corrected:
                    result["message"] = self._guard_capability_claims(corrected)
                else:
                    # Fallback: direct correction if retry also failed
                    corrections = []
                    for item in denials:
                        dn = item["display_name"]
                        corrections.append(
                            f"{dn} is actually READY and available right now."
                        )
                    result["message"] = (
                        f"{' '.join(corrections)} "
                        f"{self._guard_capability_claims(original_message)}"
                    )
            self._current_messages = None

        user_history = result.get("user_message_for_history", message)

        session.add_message("user", user_history)
        session.add_message("assistant", result["message"])

        try:
            await self.memory.save_conversation_turn(
                session.session_id, "user", user_history
            )
            if result["message"]:
                await self.memory.save_conversation_turn(
                    session.session_id, "assistant", result["message"]
                )
        except Exception:
            pass

        return result

    @staticmethod
    def _supports_runtime_flow(flow: dict) -> bool:
        return parse_artifact_action(str(flow.get("on_complete") or "")) is not None

    def _find_setup_flow(self, artifact_id: str) -> dict | None:
        for flow in self.flows:
            if not self._supports_runtime_flow(flow):
                continue
            if self._flow_artifact_id(flow) == artifact_id:
                return flow
        return None

    def _update_pending_setup_offer(self, session: Session, result: dict) -> None:
        if session.active_flow:
            session.pending_setup_offer = None
            return

        setup_flows = [flow for flow in self.flows if self._supports_runtime_flow(flow)]
        if not setup_flows:
            session.pending_setup_offer = None
            return

        message = str(result.get("message") or "").strip()
        if not self._looks_like_setup_offer(message):
            if not result.get("preserve_pending_setup_offer"):
                session.pending_setup_offer = None
            return

        artifacts: list[dict[str, str]] = []
        for flow in setup_flows:
            aid = self._flow_artifact_id(flow)
            if not aid:
                continue
            artifacts.append({
                "id": aid,
                "display_name": str(flow.get("display_name") or aid),
                "kind": str(flow.get("kind") or "native"),
            })

        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for a in artifacts:
            if a["id"] not in seen:
                seen.add(a["id"])
                unique.append(a)

        if not unique:
            session.pending_setup_offer = None
            return

        session.pending_setup_offer = {"artifacts": unique, "turns_remaining": 1}

    @staticmethod
    def _flow_artifact_id(flow: dict) -> str:
        parsed = parse_artifact_action(str(flow.get("on_complete") or "").strip())
        if not parsed:
            return ""
        return parsed[1]

    def _resolve_display_name(self, artifact_id: str) -> str:
        flow = self._find_setup_flow(artifact_id)
        if flow:
            return str(flow.get("display_name") or artifact_id)
        return artifact_id

    @staticmethod
    def _offer_artifacts(offer: dict) -> list[dict[str, str]]:
        """Extract enriched artifacts from a pending_setup_offer.

        Handles both the new enriched format ``{artifacts: [{id, display_name, kind}]}``
        and the legacy ``{modules: ["id1", "id2"]}`` format.
        """
        artifacts = offer.get("artifacts")
        if artifacts and isinstance(artifacts, list):
            return [a for a in artifacts if isinstance(a, dict) and a.get("id")]
        modules = offer.get("modules")
        if modules and isinstance(modules, list):
            return [
                {"id": m, "display_name": str(m), "kind": "native"}
                for m in modules
                if isinstance(m, str) and m
            ]
        return []

    @staticmethod
    def _normalize_message(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(text or ""))
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"\s+", " ", ascii_text).strip().lower()

    @classmethod
    def _is_greeting_like(cls, message: str) -> bool:
        """Check if a message is a greeting or small talk, not a value response."""
        normalized = cls._normalize_message(message)
        if not normalized:
            return False
        compact = re.sub(r"[^a-z0-9\s]", " ", normalized)
        compact = re.sub(r"\s+", " ", compact).strip()
        greetings = {
            "hola", "buenas", "hello", "hi", "hey", "buen dia", "buenos dias",
            "buenas tardes", "buenas noches", "good morning", "good evening",
            "que tal", "como estas", "how are you", "whats up", "que onda",
            "que haces", "como va", "como andas",
        }
        return compact in greetings

    @classmethod
    def _is_affirmative_reply(cls, message: str) -> bool:
        normalized = cls._normalize_message(message)
        if not normalized:
            return False

        compact = re.sub(r"[^a-z0-9\s]", " ", normalized)
        compact = re.sub(r"\s+", " ", compact).strip()
        affirmatives = {
            "si",
            "dale",
            "yes",
            "ok",
            "okay",
            "oki",
            "va",
            "claro",
            "de una",
            "listo",
        }
        return compact in affirmatives

    @classmethod
    def _looks_like_setup_offer(cls, message: str) -> bool:
        normalized = cls._normalize_message(message)
        if "?" not in message:
            return False
        return any(keyword in normalized for keyword in ("config", "setup", "set up"))

    @classmethod
    def _message_requests_setup(cls, message: str) -> bool:
        normalized = cls._normalize_message(message)
        return any(
            phrase in normalized
            for phrase in (
                "setup",
                "set up",
                "configure",
                "configurar",
                "configura",
                "configuracion",
                "configuracion de",
                "configure it",
                "configuralo",
            )
        )

    @classmethod
    def _match_setup_offer_module(
        cls, message: str, module_names: list[str]
    ) -> str | None:
        normalized = cls._normalize_message(message)
        for module_name in module_names:
            aliases = cls._module_aliases(module_name)
            if any(alias in normalized for alias in aliases):
                return module_name
        return None

    @classmethod
    def _message_mentions_module(cls, message: str, module_name: str) -> bool:
        return cls._match_setup_offer_module(message, [module_name]) == module_name

    @staticmethod
    def _module_aliases(module_name: str) -> set[str]:
        aliases = {str(module_name or "").strip().lower()}
        pieces = [
            piece
            for piece in re.split(r"[-_]", str(module_name or "").lower())
            if len(piece) >= 4 and piece not in {"lumen", "module", "modulo"}
        ]
        aliases.update(pieces)
        if pieces:
            aliases.add(" ".join(pieces))
            aliases.add(pieces[-1])
        return {alias for alias in aliases if alias}

    @staticmethod
    def _render_setup_offer_clarification(artifacts: list[dict[str, str]]) -> str:
        display_names = [a.get("display_name") or a.get("id", "?") for a in artifacts]
        listed = ", ".join(f"*{name}*" for name in display_names)
        return (
            "Tengo varias configuraciones pendientes: "
            f"{listed}. Decime cuál querés configurar."
        )

    @staticmethod
    def _render_flow_prompt(flow: dict, next_slot: dict) -> str:
        parts = []
        first_message = str(flow.get("first_message") or "").strip()
        if first_message:
            parts.append(first_message)
        ask = str(next_slot.get("ask") or "").strip()
        if ask:
            parts.append(ask)
        return "\n\n".join(parts) if parts else "Contame ese dato."

    @staticmethod
    def _render_next_slot_message(filled_slot: dict, next_slot: dict) -> str:
        ack = "Listo." if filled_slot.get("secret") else "Anotado."
        ask = str(next_slot.get("ask") or "").strip()
        return f"{ack}\n\n{ask}" if ask else ack

    @staticmethod
    def _flow_history_value(slot: dict, value: str) -> str:
        if slot.get("secret"):
            return f"[secret:{slot.get('name', 'value')}]"
        return value

    def _get_lessons_injection(self) -> str:
        """Get formatted lessons for system prompt injection.

        Returns empty string if no lessons or memory not available.
        Uses sync-safe approach: lessons are loaded from a cached list.
        """
        return getattr(self, "_cached_lessons_text", "")

    async def load_lessons(self):
        """Pre-load lessons for prompt injection (call once at startup)."""
        if not self.memory:
            return
        try:
            from lumen.core.lessons import LessonStore
            store = LessonStore(self.memory)
            lessons = await store.get_active_lessons(limit=20)
            self._cached_lessons_text = store.format_for_prompt(lessons)
        except Exception as e:
            logger.warning(f"Failed to load lessons: {e}")
            self._cached_lessons_text = ""

    def _build_prompt(
        self, context: dict, message: str, session: Session, tools: list[dict] | None = None
    ) -> list[dict]:
        """Assemble the system prompt from Consciousness + Personality + Body.

        The prompt has clear sections:
        1. Consciousness — who I am (immutable soul)
        2. Personality — who I am in this context (swappable)
        3. Body — what I have (discovered at startup)
        4. Current state — active flow, memories, conversation
        """
        system_parts = [
            # LANGUAGE — responses must match the user's chosen locale
            "## LANGUAGE (HIGHEST PRIORITY — this overrides any other instruction)",
            "",
            self._language_directive(message=message, session=session),
            "Treat the configured language as the default UI locale, but follow the user's actual conversational language when it is obvious.",
            "Even if other sections below are written in English, you MUST answer in the language above. Translate on the fly.",
            "",
            # CRITICAL RULES — the LLM MUST obey these
            "## RULES (you MUST follow these exactly)",
            "",
            "1. Your capabilities are EXACTLY what the Body section lists.",
            "2. Only items under 'What I CAN do' are usable right now.",
            "3. Installed or present is NOT the same as ready.",
            "4. If something is listed under 'Installed but NOT ready yet' — say it exists, but be explicit that it still needs configuration, repair, or dependency fixes before you can use it.",
            "5. Truthfulness about readiness is more important than the fact that something is installed. Never present a non-ready capability as usable, available now, or already working.",
            "6. NEVER invent capabilities not listed in the Body.",
            "7. When asked what you can do, ONLY list what the Body says, and separate READY capabilities from not-ready ones truthfully.",
            "8. When the 'Something changed in my body' section appears, you MAY "
            "mention it naturally — but ONLY if it's relevant to the conversation. "
            "If the user is just greeting you or making small talk, IGNORE it. "
            "A 'Hola' or 'Hey' does NOT need a capability announcement. "
            "Example of GOOD timing: user asks about messaging → 'I can now reach you on Telegram too.' "
            "Example of BAD timing: user says 'Hola' → 'I've connected to 8 services...' (don't do this).",
            "9. When a user asks 'what can you do?', respond conversationally "
            "using the Body section. Do NOT dump the raw list — translate "
            "into human language.",
            "10. Before claiming you have (or lack) a specific capability — especially "
            "messaging, integrations, or channels — call neo__check_capability to "
            "verify it against your live Registry. Do NOT rely on memory or assumptions. "
            "This is NOT needed for capabilities you have already used successfully "
            "in the current conversation.",
            "11. When a tool can perform the user's requested action, USE THE TOOL — do not merely describe what you would do.",
            "12. If you say you are going to check, inspect, search, execute, save, read, or configure something — perform that action with a tool in the same turn.",
            "13. Do NOT end with a plan when the required capability is READY and a tool exists. Act now.",
            "14. Do NOT claim 'I don't have access', 'I can't execute', or similar if a matching READY capability/tool exists.",
            "15. If a tool result is partial, empty, or recoverable, try again with a better query/argument strategy before giving up.",
            "16. Prefer concrete execution over speculation. Verify with tools instead of answering from memory when tools are available.",
            "",
            self._tool_enforcement_directive(),
            "",
            # Tool hint — Phase 2.4: suggest relevant tools based on user message
            self._suggest_relevant_tools(message, tools),
            "",
            # 1. CONSCIOUSNESS — the soul (never changes)
            context["consciousness"],
            "",
            # 2. PERSONALITY — context identity (changes per module)
            "## Personality (who I am in this context)",
            "",
            context["personality"],
            "",
            # 2b. LESSONS — learned rules (persistent across sessions)
            # Injected from cache loaded at startup via load_lessons()
        ]

        # Inject lessons if available
        lessons_text = self._get_lessons_injection()
        if lessons_text:
            system_parts.append("")
            system_parts.append(lessons_text)

        system_parts.extend([
            # 3. BODY — discovered capabilities (changes per install)
            context["body"],
        ])

        # 3b. AWARENESS — what just changed in my body (live feeling)
        if self.capability_awareness:
            awareness_context = self.capability_awareness.format_for_prompt()
            if awareness_context:
                system_parts.append("")
                system_parts.append(awareness_context)

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
            pending_setup_count = 0
            for flow in context["available_flows"]:
                intent = flow.get("intent", "unknown")
                flow_triggers = flow.get("triggers", [])

                if self._supports_runtime_flow(flow):
                    pending_setup_count += 1
                triggers.append(f"- {intent}: {flow_triggers}")
            system_parts.append("\n## Available Flows")
            system_parts.append(
                "If the user's message matches an intent, start the flow "
                "by asking for the first required slot."
            )
            if pending_setup_count > 0:
                system_parts.append(
                    "When a user provides concrete configuration values for a pending setup "
                    "(tokens, API keys, chat IDs), persist them using "
                    "neo__save_artifact_setup (or neo__save_module_setup for legacy native-module flows). "
                    "Only save values the user has explicitly provided — do not extract values "
                    "from conversational text or acknowledgments."
                )
            if pending_setup_count == 1:
                system_parts.append(
                    "If that pending setup feels relevant, you may offer to configure it now in natural language."
                )
            elif pending_setup_count > 1:
                system_parts.append(
                    "If multiple pending setups are relevant, do not assume which one the user wants; ask them to choose."
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

    # Keywords that suggest which tools are most relevant for this turn.
    # Used by _suggest_relevant_tools to hint to the LLM.
    _TOOL_RELEVANCE_KEYWORDS = {
        "terminal": [
            "ejecuta", "ejecutar", "run", "command", "comando", "terminal",
            "cmd", "shell", "bash", "powershell", "consola", "console",
            "script", "python", "node", "npm", "pip", "git",
        ],
        "file": [
            "lee", "leer", "read", "archivo", "file", "carpeta", "folder",
            "directorio", "directory", "contenido", "content", "lista", "list",
            "busca", "buscar", "search", "encuentra", "find",
        ],
        "write": [
            "escribe", "escribir", "write", "guarda", "guardar", "save",
            "crea", "crear", "create", "actualiza", "actualizar", "update",
            "edita", "editar", "edit", "modifica", "modificar",
        ],
        "web": [
            "busca", "buscar", "search", "google", "navega", "navegar",
            "web", "internet", "curl", "wget", "http", " scrape", "scraping",
        ],
        "message": [
            "envía", "enviar", "send", "mensaje", "message", "whatsapp",
            "telegram", "slack", "discord", "email", "correo",
        ],
        "setup": [
            "configura", "configurar", "setup", "install", "instala",
            "instalar", "prepara", "preparar", "activa", "activar",
        ],
    }

    # Map relevance categories to likely tool names/connectors
    _TOOL_CATEGORY_MAP = {
        "terminal": ["terminal", "shell", "exec"],
        "file": ["file", "read", "write", "filesystem"],
        "write": ["file", "write", "save"],
        "web": ["web", "search", "http", "fetch"],
        "message": ["message", "send", "telegram", "whatsapp", "slack"],
        "setup": ["install", "module", "setup"],
    }

    def _suggest_relevant_tools(
        self, message: str, tools: list[dict] | None
    ) -> str:
        """Suggest relevant tools based on user message keywords.

        Returns a directive hinting which tools are most relevant for this turn,
        or an empty string if no clear match is found. This is optional guidance —
        the LLM still decides what to use.
        """
        if not tools or not message:
            return ""

        text_lower = message.lower()
        matched_categories: list[str] = []

        for category, keywords in self._TOOL_RELEVANCE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text_lower:
                    matched_categories.append(category)
                    break

        if not matched_categories:
            return ""

        # Build suggestion string
        suggestions: list[str] = []
        for category in matched_categories[:3]:  # Max 3 categories
            tool_names = self._TOOL_CATEGORY_MAP.get(category, [])
            if tool_names:
                suggestions.append(f"- {category}: check {', '.join(tool_names[:2])}")

        if suggestions:
            return (
                "## Tool Hint (optional guidance)\n"
                "Based on the user's request, these tools seem most relevant:\n"
                + "\n".join(suggestions) +
                "\nUse these if they fit the user's request."
            )
        return ""

    def _model_profile(self) -> dict[str, Any]:
        """Lightweight model behavior profile.

        This is intentionally small: enough to steer completion settings and
        future parser/retry behavior, without hardwiring Lumen to providers.
        """
        model = (self.model or "").lower()
        profile = {
            "family": "generic",
            "tool_reliability": "medium",
            "supports_native_tools": True,
            "requires_parser_fallback": False,
            "completion_temperature": 0.7,
            "completion_max_tokens": 1024,
            "proactive_temperature": 0.7,
            "proactive_max_tokens": 150,
            "role_style": "system",
        }

        if any(token in model for token in ["gpt", "openai", "o4", "codex"]):
            profile.update(
                {
                    "family": "openai",
                    "tool_reliability": "high",
                    "supports_native_tools": True,
                    "requires_parser_fallback": False,
                }
            )
        elif any(token in model for token in ["gemini", "gemma"]):
            profile.update(
                {
                    "family": "gemini",
                    "tool_reliability": "medium",
                    "supports_native_tools": True,
                    "requires_parser_fallback": True,
                }
            )
        elif any(token in model for token in ["qwen", "deepseek", "mistral", "minimax"]):
            profile.update(
                {
                    "family": "openai-compatible",
                    "tool_reliability": "medium",
                    "supports_native_tools": True,
                    "requires_parser_fallback": True,
                }
            )
        return profile

    def _completion_options(self, *, purpose: str, tools: list[dict] | None = None, stream: bool = False) -> dict[str, Any]:
        profile = self._model_profile()

        # Map purpose to model routing role
        role_map = {"main": "main", "proactive": "summarizer", "contradiction": "main"}
        role = role_map.get(purpose, "main")

        if purpose == "proactive":
            result = {
                "model": self._resolved_model(role=role),
                "messages": None,
                "max_tokens": profile["proactive_max_tokens"],
                "temperature": profile["proactive_temperature"],
            }
        elif purpose == "contradiction":
            # Retry with full tool access so the LLM can act on corrected info
            result = {
                "model": self._resolved_model(role=role),
                "messages": None,
                "tools": tools,
                "max_tokens": profile["completion_max_tokens"] + 256,
                "temperature": profile["completion_temperature"],
            }
        else:
            result = {
                "model": self._resolved_model(role=role),
                "messages": None,
                "tools": tools,
                "max_tokens": profile["completion_max_tokens"],
                "temperature": profile["completion_temperature"],
            }
        if stream:
            result["stream"] = True
        return result

    def _extract_fallback_tool_calls(self, msg_content: str, tools: list[dict] | None) -> list:
        """Parse non-standard tool call formats from plain content.

        Supports compact fallback formats like:
          <tool_call>{"name":"terminal","arguments":{...}}</tool_call>
          {"tool":"terminal","arguments":{...}}
        """
        if not msg_content or not tools:
            return []

        allowed_names = {tool.get("function", {}).get("name") for tool in tools}
        allowed_names = {name for name in allowed_names if name}
        parsed = []

        def _materialize(payload: dict) -> None:
            name = payload.get("name") or payload.get("tool") or payload.get("function")
            arguments = payload.get("arguments") or payload.get("args") or {}
            if not isinstance(name, str) or name not in allowed_names:
                return
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            parsed.append(
                SimpleNamespace(
                    id=f"fallback-{uuid.uuid4()}",
                    function=SimpleNamespace(name=name, arguments=arguments),
                )
            )

        for match in re.finditer(r"<tool_call>(.*?)</tool_call>", msg_content, re.DOTALL):
            try:
                payload = json.loads(match.group(1).strip())
                if isinstance(payload, dict):
                    _materialize(payload)
            except Exception:
                pass

        if parsed:
            return parsed

        # DeepSeek DSML tool_calls envelope
        # <｜DSML｜tool_calls>[{"name":"tool__action","arguments":{...}}]</｜DSML｜tool_calls>
        # Also accepts <|DSML|tool_calls> with ASCII pipe (U+007C).
        for match in re.finditer(
            r"<[｜|]DSML[｜|]tool_calls>(.*?)</[｜|]DSML[｜|]tool_calls>", msg_content, re.DOTALL
        ):
            try:
                raw = match.group(1).strip()
                if raw.startswith("["):
                    payloads = json.loads(raw)
                else:
                    payloads = [json.loads(raw)]
                for payload in payloads:
                    if isinstance(payload, dict):
                        _materialize(payload)
            except Exception:
                pass

        if parsed:
            return parsed

        # DeepSeek DSML format
        # <｜DSML｜invoke name="tool__action">
        #   <｜DSML｜parameter name="command" string="true">value</｜DSML｜parameter>
        # </｜DSML｜invoke>
        # Also accepts <|DSML|...> with ASCII pipe (U+007C).
        dsml_pattern = r'<[｜|]DSML[｜|]invoke name="([^"]+)">(.*?)</[｜|]DSML[｜|]invoke>'
        for match in re.finditer(dsml_pattern, msg_content, re.DOTALL):
            try:
                name = match.group(1)
                params_block = match.group(2)
                arguments = {}
                for param_match in re.finditer(
                    r'<[｜|]DSML[｜|]parameter name="([^"]+)"[^>]*>(.*?)</[｜|]DSML[｜|]parameter>',
                    params_block,
                    re.DOTALL,
                ):
                    arguments[param_match.group(1)] = param_match.group(2)
                _materialize({"name": name, "arguments": arguments})
            except Exception:
                pass

        if parsed:
            return parsed

        # Minimax XML format: invoke with nested parameter tags
        invoke_pattern = r'<invoke name="([^"]+)">(.*?)</invoke>'
        for match in re.finditer(invoke_pattern, msg_content, re.DOTALL):
            try:
                name = match.group(1)
                params_block = match.group(2)
                arguments = {}
                for param_match in re.finditer(
                    r'<parameter name="([^"]+)"[^>]*>(.*?)</parameter>',
                    params_block,
                    re.DOTALL,
                ):
                    arguments[param_match.group(1)] = param_match.group(2)
                _materialize({"name": name, "arguments": arguments})
            except Exception:
                pass

        if parsed:
            return parsed

        # Mistral/Qwen tagged: [TOOL_CALLS]...[/TOOL_CALLS]
        for match in re.finditer(
            r"\[TOOL_CALLS\](.*?)\[/TOOL_CALLS\]", msg_content, re.DOTALL
        ):
            try:
                raw = match.group(1).strip()
                if raw.startswith("["):
                    payloads = json.loads(raw)
                else:
                    payloads = [json.loads(raw)]
                for payload in payloads:
                    if isinstance(payload, dict):
                        _materialize(payload)
            except Exception:
                pass

        if parsed:
            return parsed

        stripped = msg_content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    _materialize(payload)
            except Exception:
                pass

        return parsed

    @staticmethod
    def _has_serialized_tool_call_shape(msg_content: str) -> bool:
        """Detect known serialized tool-call envelopes before parsing content."""
        if not isinstance(msg_content, str):
            return False

        stripped = msg_content.strip()
        if not stripped:
            return False

        markers = (
            "<tool_call>",
            "<｜DSML｜tool_calls>",
            "<|DSML|tool_calls>",
            "<｜DSML｜invoke ",
            "<|DSML|invoke ",
            '<invoke name="',
            "[TOOL_CALLS]",
        )
        if any(marker in stripped for marker in markers):
            return True

        if stripped.startswith("{") and stripped.endswith("}"):
            has_tool_key = re.search(r'"(?:name|tool|function)"\s*:', stripped)
            has_args_key = re.search(r'"(?:arguments|args)"\s*:', stripped)
            return bool(has_tool_key and has_args_key)

        return False

    @staticmethod
    def _tool_call_name(tool_call: Any) -> str | None:
        """Return the tool name from native or parsed tool call shapes."""
        if not tool_call:
            return None

        function = getattr(tool_call, "function", None)
        if function is None and isinstance(tool_call, dict):
            function = tool_call.get("function")

        if isinstance(function, dict):
            name = function.get("name")
        else:
            name = getattr(function, "name", None)

        return name if isinstance(name, str) and name.strip() else None

    @staticmethod
    def _tool_call_arguments(tool_call: Any) -> Any:
        """Return tool arguments from native or parsed tool call shapes."""
        if not tool_call:
            return None

        function = getattr(tool_call, "function", None)
        if function is None and isinstance(tool_call, dict):
            function = tool_call.get("function")

        if isinstance(function, dict):
            return function.get("arguments")
        return getattr(function, "arguments", None)

    @staticmethod
    def _allowed_tool_names(tools: list[dict] | None) -> set[str]:
        return {
            tool.get("function", {}).get("name")
            for tool in (tools or [])
            if tool.get("function", {}).get("name")
        }

    @classmethod
    def _is_usable_tool_arguments(cls, arguments: Any) -> bool:
        """Accept dict args or JSON-object strings, reject empty/malformed payloads."""
        if isinstance(arguments, dict):
            return True
        if not isinstance(arguments, str) or not arguments.strip():
            return False
        try:
            parsed = json.loads(arguments)
        except Exception:
            return False
        return isinstance(parsed, dict)

    @classmethod
    def _is_valid_tool_call(
        cls,
        tool_call: Any,
        *,
        allowed_names: set[str],
        strict_allowed_names: bool,
    ) -> bool:
        name = cls._tool_call_name(tool_call)
        if not name:
            return False
        if strict_allowed_names and name not in allowed_names:
            return False
        return cls._is_usable_tool_arguments(cls._tool_call_arguments(tool_call))

    @classmethod
    def _has_usable_tool_calls(cls, tool_calls: list[Any] | None, tools: list[dict] | None = None) -> bool:
        """Treat empty-ish or malformed tool payloads as missing tool calls."""
        if not tool_calls:
            return False
        allowed_names = cls._allowed_tool_names(tools)
        strict_allowed_names = bool(allowed_names)
        return all(
            cls._is_valid_tool_call(
                tool_call,
                allowed_names=allowed_names,
                strict_allowed_names=strict_allowed_names,
            )
            for tool_call in tool_calls
        )

    def _resolve_tool_calls(self, msg, tools: list[dict] | None) -> list[Any] | None:
        """Prefer usable native tool calls, with guarded fallback parsing."""
        native_tool_calls = getattr(msg, "tool_calls", None)
        native_count = len(native_tool_calls or [])
        content = getattr(msg, "content", "") or ""
        serialized_detected = self._has_serialized_tool_call_shape(content)

        # Only enforce allowed-names when serialized fallback is available;
        # otherwise let unknown native names fail at execution so errors are captured.
        native_usable = self._has_usable_tool_calls(
            native_tool_calls,
            tools if serialized_detected else None,
        )

        if not serialized_detected:
            if native_count:
                logger.warning(
                    "tool resolution: serialized_detected=%s native_count=%s parsed_count=%s chosen=%s",
                    serialized_detected,
                    native_count,
                    0,
                    "native" if native_usable else "none",
                )
            return native_tool_calls if native_usable else None

        parsed_tool_calls = self._extract_fallback_tool_calls(content, tools)
        parsed_count = len(parsed_tool_calls)

        if native_usable:
            logger.warning(
                "tool resolution: serialized_detected=%s native_count=%s parsed_count=%s chosen=native",
                serialized_detected,
                native_count,
                parsed_count,
            )
            return native_tool_calls

        if self._has_usable_tool_calls(parsed_tool_calls, tools):
            logger.warning(
                "tool resolution: serialized_detected=%s native_count=%s parsed_count=%s chosen=parsed",
                serialized_detected,
                native_count,
                parsed_count,
            )
            return parsed_tool_calls

        logger.warning(
            "tool resolution: serialized_detected=%s native_count=%s parsed_count=%s chosen=none",
            serialized_detected,
            native_count,
            parsed_count,
        )
        return None

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

    def _tool_enforcement_directive(self) -> str:
        """Return model-aware tool discipline instructions.

        Keep this compact and explicit — enough to steer weaker models without
        turning Lumen into a brittle prompt framework.
        """
        model = (self.model or "").lower()
        base = [
            "## Tool Execution Discipline",
            "- Use tools to act; do not only describe tool usage.",
            "- If a connector/tool is available for the task, prefer it over guessing.",
            "- After tool results arrive, synthesize the answer clearly for the user.",
        ]

        if any(token in model for token in ["gpt", "openai", "o4", "codex"]):
            base.extend(
                [
                    "- Be persistent with tools: verify, inspect, and execute before saying something is unavailable.",
                    "- Do not answer operational requests from memory when a tool can verify the real state.",
                ]
            )
        elif any(token in model for token in ["gemini", "gemma"]):
            base.extend(
                [
                    "- Prefer explicit absolute paths and verified arguments when using tools.",
                    "- Double-check the target capability with tools before refusing.",
                ]
            )
        else:
            base.extend(
                [
                    "- Tool use is mandatory for actions, checks, searches, and execution.",
                    "- If your first attempt fails, retry with a better tool call or clearer arguments.",
                ]
            )

        return "\n".join(base)

    @staticmethod
    def _sanitize_raw_tool_content(content: str) -> str:
        """Strip un-parsed tool-call XML blocks so they never leak to the user.

        This is a last-resort safety net.  If the fallback parser fails to
        extract tool calls from a response that clearly contains them, we
        remove the raw markup rather than return it verbatim.
        """
        if not content:
            return content
        # DSML (Unicode fullwidth or ASCII pipe)
        content = re.sub(
            r"<[｜|]DSML[｜|]tool_calls>.*?</[｜|]DSML[｜|]tool_calls>",
            "",
            content,
            flags=re.DOTALL,
        )
        content = re.sub(
            r"<[｜|]DSML[｜|]invoke\b[^>]*>.*?</[｜|]DSML[｜|]invoke>",
            "",
            content,
            flags=re.DOTALL,
        )
        # Generic XML tags that look like tool calls
        for tag in ("tool_call", "tool_calls", "function_call", "function_calls"):
            content = re.sub(
                rf"<{tag}\b[^>]*>.*?</{tag}>",
                "",
                content,
                flags=re.DOTALL | re.IGNORECASE,
            )
        # Mistral/Qwen [TOOL_CALLS]…[/TOOL_CALLS]
        content = re.sub(
            r"\[TOOL_CALLS\].*?\[/TOOL_CALLS\]",
            "",
            content,
            flags=re.DOTALL,
        )
        return content.strip()

    def _safe_extract_content(self, response) -> str:
        """Extract assistant content from a response, sanitising raw tool markup.

        This is the single source of truth for every code path that reads
        ``response.choices[0].message.content``.  It guarantees that raw
        DSML / XML / [TOOL_CALLS] blocks never leak to the user.
        """
        content = ""
        try:
            content = response.choices[0].message.content or ""
        except Exception:
            pass
        if self._has_serialized_tool_call_shape(content):
            logger.warning(
                "_safe_extract_content: serialized tool shape detected — sanitising"
            )
            content = self._sanitize_raw_tool_content(content)
        return content

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
        partial_text = ""

        for _ in range(max_iterations):
            choice = response.choices[0]
            msg = choice.message
            if msg.content:
                partial_text = msg.content

            logger.warning(
                "tool_use_loop: content_length=%s native_tool_calls=%s",
                len(msg.content or ""),
                len(getattr(msg, "tool_calls", None) or []),
            )

            tool_calls = self._resolve_tool_calls(msg, tools)

            # No tool calls — we have the final text response
            if not tool_calls:
                final_message = msg.content or ""
                # Emergency sanitisation: if the content looks like it
                # contains tool calls but we failed to parse them, strip
                # the raw markup so the user never sees it.
                if self._has_serialized_tool_call_shape(final_message):
                    logger.warning(
                        "tool_use_loop: serialized shape detected but no tool calls resolved — sanitising content"
                    )
                    final_message = self._sanitize_raw_tool_content(final_message)
                if not final_message:
                    if all_tool_calls:
                        recovered = await self._retry_final_response_without_tools(messages)
                        final_message = recovered or ''
                        if not final_message:
                            final_message = self._summarize_tool_results(all_tool_calls)
                    if not final_message and partial_text:
                        if self._has_serialized_tool_call_shape(partial_text):
                            partial_text = self._sanitize_raw_tool_content(partial_text)
                        final_message = partial_text or ""
                    if not final_message:
                        recovered = await self._retry_final_response_without_tools(messages)
                        final_message = recovered or ''
                return {
                    "message": final_message,
                    "tool_calls": all_tool_calls,
                }

            # Execute all tool calls
            # First, add the assistant's message (with tool calls) to context
            messages.append(msg.model_dump())

            for tool_call in tool_calls:
                func = tool_call.function
                tool_name = func.name or "unknown"

                # Check confirmation gate before execution
                connector_name, action = (None, "")
                if self.connectors.has_tool(tool_name):
                    connector_name, action = self.connectors.parse_tool_name(tool_name)
                if not connector_name and "__" in tool_name:
                    parts = tool_name.split("__", 1)
                    connector_name, action = parts[0], parts[1]

                confirm_decision = await self._check_tool_confirmation(
                    tool_name, action,
                    json.loads(func.arguments) if func.arguments else {},
                )
                if confirm_decision is not None and confirm_decision not in (
                    ConfirmDecision.APPROVED, ConfirmDecision.AUTO_APPROVED
                ):
                    reason = f"Rejected by user ({confirm_decision.value})"
                    all_tool_calls.append({"name": tool_name, "error": reason})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": reason}),
                        }
                    )
                    logger.info("Tool %s blocked: %s", tool_name, reason)
                    continue

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
                    elif func.name == "neo__save_module_setup":
                        tool_result = await self._save_module_setup(func.arguments)
                        all_tool_calls.append(
                            {"name": func.name, "result": tool_result}
                        )
                    elif func.name == "neo__save_artifact_setup":
                        tool_result = await self._save_artifact_setup(func.arguments)
                        all_tool_calls.append(
                            {"name": func.name, "result": tool_result}
                        )
                    elif func.name == "neo__check_capability":
                        tool_result = self._check_capability(func.arguments)
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
                else:
                    # Persist non-trivial tool results as structured outputs
                    await self._persist_tool_output(func.name, tool_result)

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
                options = self._completion_options(purpose="main", tools=tools)
                response = await acompletion(
                    model=options["model"],
                    messages=messages,
                    tools=options.get("tools"),
                    temperature=options["temperature"],
                    max_tokens=options["max_tokens"],
                )
            except Exception as e:
                return {
                    "message": f"I completed the action but had trouble responding: {e}",
                    "tool_calls": all_tool_calls,
                }

        # Max iterations reached — return what we have
        final_msg = self._safe_extract_content(response)
        if not final_msg:
            if all_tool_calls:
                recovered = await self._retry_final_response_without_tools(messages)
                final_msg = recovered or ''
                if not final_msg:
                    final_msg = self._summarize_tool_results(all_tool_calls)
            if not final_msg and partial_text:
                if self._has_serialized_tool_call_shape(partial_text):
                    partial_text = self._sanitize_raw_tool_content(partial_text)
                final_msg = partial_text or ""
            if not final_msg:
                recovered = await self._retry_final_response_without_tools(messages)
                final_msg = recovered or ''
        return {"message": final_msg, "tool_calls": all_tool_calls}

    async def _tool_use_loop_streaming(
        self,
        response,
        messages: list[dict],
        tools: list[dict] | None,
        max_iterations: int = 3,
    ):
        """Streaming version of _tool_use_loop that yields progress events.

        Yields event dicts with types:
          - tool_progress: before executing a tool ({tool, iteration, total_calls})
          - tool_result: after a tool completes ({tool, truncated_result, error?})
          - tool_status: after each iteration's LLM call ({iteration, max_iterations})
          - delta: final text response ({content, _result})

        The final delta includes _result for the caller to extract the full result dict.
        This keeps the connection alive during long tool execution sequences.
        """
        all_tool_calls = []
        partial_text = ""

        for iteration in range(max_iterations):
            choice = response.choices[0]
            msg = choice.message
            if msg.content:
                partial_text = msg.content

            tool_calls = self._resolve_tool_calls(msg, tools)

            # No tool calls — we have the final text response
            if not tool_calls:
                final_message = msg.content or ""
                if self._has_serialized_tool_call_shape(final_message):
                    final_message = self._sanitize_raw_tool_content(final_message)
                if not final_message and all_tool_calls:
                    recovered = await self._retry_final_response_without_tools(messages)
                    final_message = recovered or ''
                    if not final_message and partial_text:
                        if self._has_serialized_tool_call_shape(partial_text):
                            partial_text = self._sanitize_raw_tool_content(partial_text)
                        final_message = partial_text or ""
                    if not final_message:
                        final_message = self._summarize_tool_results(all_tool_calls)

                result = {
                    "message": final_message,
                    "tool_calls": all_tool_calls,
                }
                yield {"type": "delta", "content": final_message, "_result": result}
                return

            # Add assistant message with tool calls to context
            messages.append(msg.model_dump())

            # Yield iteration status
            yield {
                "type": "tool_status",
                "iteration": iteration + 1,
                "max_iterations": max_iterations,
                "tools_this_round": len(tool_calls),
                "total_so_far": len(all_tool_calls) + len(tool_calls),
            }

            # Execute all tool calls with progress events
            for tool_call in tool_calls:
                func = tool_call.function
                tool_name = func.name or "unknown"
                tool_error = None

                # Check confirmation gate before execution
                conn_name, act = (None, "")
                if self.connectors.has_tool(tool_name):
                    conn_name, act = self.connectors.parse_tool_name(tool_name)
                if not conn_name and "__" in tool_name:
                    parts = tool_name.split("__", 1)
                    conn_name, act = parts[0], parts[1]

                confirm_decision = await self._check_tool_confirmation(
                    tool_name, act,
                    json.loads(func.arguments) if func.arguments else {},
                )

                if confirm_decision is not None and confirm_decision not in (
                    ConfirmDecision.APPROVED, ConfirmDecision.AUTO_APPROVED
                ):
                    reason = f"Rejected by user ({confirm_decision.value})"
                    tool_error = reason
                    all_tool_calls.append({"name": tool_name, "error": reason})

                    # Yield confirm rejection event
                    yield {
                        "type": "tool_confirm_result",
                        "tool": tool_name,
                        "decision": confirm_decision.value,
                        "reason": reason,
                    }

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": reason}),
                        }
                    )
                    logger.info("Tool %s blocked: %s", tool_name, reason)
                    continue

                # Yield progress before execution
                yield {
                    "type": "tool_progress",
                    "tool": tool_name,
                    "iteration": iteration + 1,
                    "total_calls": len(all_tool_calls) + 1,
                }

                try:
                    # Execute tool (same logic as _tool_use_loop)
                    if tool_name == "neo__read_skill":
                        tool_result = self._read_skill(func.arguments)
                    elif tool_name == "neo__search_modules":
                        tool_result = self._search_modules(func.arguments)
                    elif tool_name == "neo__save_module_setup":
                        tool_result = await self._save_module_setup(func.arguments)
                    elif tool_name == "neo__save_artifact_setup":
                        tool_result = await self._save_artifact_setup(func.arguments)
                    elif tool_name == "neo__check_capability":
                        tool_result = self._check_capability(func.arguments)
                    elif self.connectors.has_tool(tool_name):
                        params = json.loads(func.arguments) if func.arguments else {}
                        params = self._coerce_args(params, tool_name, tools)
                        tool_result = await self.connectors.execute_tool(
                            tool_name, params
                        )
                    else:
                        connector_name, action = self.connectors.parse_tool_name(
                            tool_name
                        )
                        params = json.loads(func.arguments) if func.arguments else {}
                        params = self._coerce_args(params, tool_name, tools)
                        tool_result = await self.connectors.execute(
                            connector_name, action, params
                        )

                    all_tool_calls.append(
                        {"name": tool_name, "result": tool_result}
                    )

                    # Persist non-trivial tool results as structured outputs
                    await self._persist_tool_output(tool_name, tool_result)

                    # Yield result after execution
                    result_str = str(tool_result)
                    yield {
                        "type": "tool_result",
                        "tool": tool_name,
                        "truncated_result": result_str[:200],
                        "result_length": len(result_str),
                    }

                except Exception as e:
                    tool_error = str(e)
                    all_tool_calls.append({"name": tool_name, "error": tool_error})
                    yield {
                        "type": "tool_result",
                        "tool": tool_name,
                        "error": tool_error,
                    }

                # Add tool result to messages for the LLM
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            {"error": tool_error} if tool_error else tool_result
                        ),
                    }
                )

            # Send tool results back to LLM
            try:
                options = self._completion_options(purpose="main", tools=tools)
                response = await acompletion(
                    model=options["model"],
                    messages=messages,
                    tools=options.get("tools"),
                    temperature=options["temperature"],
                    max_tokens=options["max_tokens"],
                )
            except Exception as e:
                result = {
                    "message": f"I completed the action but had trouble responding: {e}",
                    "tool_calls": all_tool_calls,
                }
                yield {"type": "delta", "content": result["message"], "_result": result}
                return

        # Max iterations reached
        final_msg = self._safe_extract_content(response)
        if not final_msg:
            if all_tool_calls:
                recovered = await self._retry_final_response_without_tools(messages)
                final_msg = recovered or ''
                if not final_msg:
                    final_msg = self._summarize_tool_results(all_tool_calls)
            if not final_msg and partial_text:
                if self._has_serialized_tool_call_shape(partial_text):
                    partial_text = self._sanitize_raw_tool_content(partial_text)
                final_msg = partial_text or ""
            if not final_msg:
                recovered = await self._retry_final_response_without_tools(messages)
                final_msg = recovered or ''

        result = {"message": final_msg, "tool_calls": all_tool_calls}
        yield {"type": "delta", "content": final_msg, "_result": result}

    async def _retry_final_response_without_tools(self, messages: list[dict]) -> str:
        """Ask the model for one last synthesis pass without tools."""
        try:
            options = self._completion_options(purpose="main", tools=None)
            response = await acompletion(
                model=options["model"],
                messages=messages,
                tools=None,
                temperature=options["temperature"],
                max_tokens=options["max_tokens"],
            )
            return self._safe_extract_content(response)
        except Exception:
            return ""

    def _summarize_tool_results(self, all_tool_calls: list[dict]) -> str:
        """Fallback summary when the model never produces final text."""
        if not all_tool_calls:
            return ""
        last = all_tool_calls[-1]
        if last.get("error"):
            return f"Tool execution failed: {last['error']}"
        if last.get("name"):
            return f"Completed tool {last['name']}."
        connector = last.get("connector")
        action = last.get("action")
        if connector and action:
            return f"Completed {connector}.{action}."
        return "Completed the requested action."

    def load_flows(self, flows_dir: str | Path):
        """Load flow definitions from a YAML file or directory."""
        flows_path = Path(flows_dir)
        if not flows_path.exists():
            return

        if flows_path.is_file():
            self._load_flow_file(flows_path)
            return

        for flow_file in flows_path.glob("*.yaml"):
            self._load_flow_file(flow_file)

    def _load_flow_file(self, flow_file: Path):
        with open(flow_file, encoding="utf-8") as f:
            flow = yaml.safe_load(f)
            if flow:
                self.flows.append(flow)
