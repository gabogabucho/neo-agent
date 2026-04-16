"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from neo.core.brain import Brain
from neo.core.session import SessionManager


# Set at startup by CLI, before uvicorn starts
_brain: Brain | None = None
_locale: dict = {}
_config: dict = {}


def configure(brain: Brain, locale: dict, config: dict):
    """Configure the web channel with brain, locale, and config."""
    global _brain, _locale, _config
    _brain = brain
    _locale = locale
    _config = config


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize async resources (memory DB) on startup, clean up on shutdown."""
    if _brain:
        await _brain.memory.init()
    yield
    if _brain:
        await _brain.memory.close()


app = FastAPI(title="Neo", version="0.1.0", lifespan=lifespan)
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
session_manager = SessionManager()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the main dashboard — Neo's UI-FIRST experience."""
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "ui": ui,
            "model": _config.get("model", "not configured"),
            "language": _config.get("language", "en"),
            "version": "0.1.0",
            "connectors_count": len(_brain.connectors.list()) if _brain else 0,
            "flows_count": len(_brain.flows) if _brain else 0,
        },
    )


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """Real-time chat via WebSocket."""
    await websocket.accept()
    session = session_manager.get_or_create(session_id)

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_text = payload.get("content", "").strip()

            if not user_text or not _brain:
                continue

            # Typing indicator
            await websocket.send_text(
                json.dumps({"type": "typing", "status": True})
            )

            # Brain thinks
            result = await _brain.think(user_text, session)

            # Send response
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": result["message"],
                    }
                )
            )

            # Stop typing
            await websocket.send_text(
                json.dumps({"type": "typing", "status": False})
            )

    except WebSocketDisconnect:
        session_manager.remove(session_id)


@app.get("/api/status")
async def api_status():
    """API endpoint for Neo's current status."""
    flows_info = []
    if _brain:
        for flow in _brain.flows:
            flows_info.append(
                {
                    "intent": flow.get("intent", "unknown"),
                    "triggers": flow.get("triggers", []),
                    "slots": list(flow.get("slots", {}).keys()),
                }
            )

    return {
        "status": "active",
        "version": "0.1.0",
        "model": _config.get("model", "not configured"),
        "language": _config.get("language", "en"),
        "connectors": _brain.connectors.list() if _brain else [],
        "flows": flows_info,
        "skills": ["text-responder", "web-search", "file-reader"],
        "channels": [{"name": "web", "status": "active"}],
    }
