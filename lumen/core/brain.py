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
import re
import unicodedata
from pathlib import Path

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
        marketplace=None,
        capability_awareness: CapabilityAwareness | None = None,
        language: str = "en",
        api_key_env: str | None = None,
        flow_action_handler=None,
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

    def _resolved_model(self) -> str:
        """Route model through OpenRouter when OpenRouter creds are active."""
        model = self.model or ""
        if self.api_key_env == "OPENROUTER_API_KEY" and not model.startswith("openrouter/"):
            return f"openrouter/{model}"
        return model

    def _language_directive(
        self, message: str | None = None, session: Session | None = None
    ) -> str:
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

    async def think(self, message: str, session: Session) -> dict:
        """Receive message -> assemble context -> LLM decides -> response."""

        offer_result = await self._maybe_handle_pending_setup_offer(message, session)
        if offer_result is not None:
            return await self._finalize_turn(session, message, offer_result)

        flow_result = await self._maybe_handle_runtime_flow(message, session)
        if flow_result is not None:
            return await self._finalize_turn(session, message, flow_result)

        # 1. Recall relevant memories
        memories = await self.memory.recall(message, limit=5)

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

        # Add module setup save tool — persists collected env values
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "neo__save_module_setup",
                    "description": (
                        "Save configuration values for a module that needs setup. "
                        "Call this when you have collected the required values "
                        "(tokens, IDs, keys) from the user during a setup conversation. "
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

        tools = tools if tools else None

        try:
            response = await acompletion(
                model=self._resolved_model(),
                messages=messages,
                tools=tools if tools else None,
                temperature=0.7,
                max_tokens=1024,
            )
        except Exception as e:
            return {"message": f"I had trouble thinking: {e}", "tool_calls": []}

        # 6. Tool use loop — if LLM called tools, execute and send results back
        result = await self._tool_use_loop(response, messages, tools)

        return await self._finalize_turn(session, message, result)

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
                    f"{self._language_directive()}\n\n"
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
            response = await acompletion(
                model=self._resolved_model(),
                messages=messages,
                max_tokens=150,
                temperature=0.7,
            )
            content = response.choices[0].message.content or ""
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
        self._update_pending_setup_offer(session, result)
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
                    "CRITICAL: When a user provides configuration values for a pending setup "
                    "(tokens, API keys, chat IDs), you MUST persist them with "
                    "neo__save_artifact_setup (or neo__save_module_setup for legacy native-module flows). "
                    "to persist them. Do NOT just acknowledge the values — actually save them "
                    "with the tool. Do NOT ask the user to manually configure env vars."
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
                    model=self._resolved_model(),
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
