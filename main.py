import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
RAW_ALLOWED_MODELS = os.getenv("ALLOWED_MODELS", "anthropic/claude-haiku-4-5-20251001,anthropic/claude-sonnet-5")
ALLOWED_MODELS = [m.strip() for m in RAW_ALLOWED_MODELS.split(",") if m.strip()]
PREVIEW_LENGTH = 60

# Roles the LLM understands. Anything else stored in the DB (e.g. notifications) stays out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
NOTE_ROLE = "note"
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PROMPT_LABELS = {
    "system.md": "Nouveau prompt (Tuteur Python)",
    "old.md": "Ancien prompt (Généraliste)",
}


class PromptOption(BaseModel):
    id: str
    name: str


def load_prompt_by_name(name: str) -> str:
    # Whitelist against path traversal
    file_path = (PROMPTS_DIR / name).resolve()
    if not file_path.is_file() or file_path.parent != PROMPTS_DIR.resolve():
        raise HTTPException(status_code=400, detail=f"Fichier de prompt non valide ou introuvable : '{name}'.")
    return file_path.read_text(encoding="utf-8")


app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first user message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None
    prompt: str = "system.md"


class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None  # set when this turn also stored a system-notification


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


class NoteRequest(BaseModel):
    content: str


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # Construit l'historique envoyé au LLM.
    # On filtre explicitement les messages ayant le rôle "note" :
    # Il s'agit d'une note privée de l'étudiant, inutile et potentiellement parasite
    # pour le LLM, et son exclusion permet également d'économiser des tokens.
    # Les autres rôles non reconnus par le LLM (ex: "system-notification") restent exclus via LLM_ROLES.
    return [
        {"role": m.role, "content": m.content}
        for m in rows
        if m.role in LLM_ROLES and m.role != NOTE_ROLE
    ]


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1, always the user's) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)) -> MessageResponse:
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Le contenu de la note ne peut pas être vide.")

    # Vérifie que la conversation existe et charge ses messages existants
    rows = load_messages(db, conversation_id)
    next_seq = rows[-1].seq + 1 if rows else 1

    note_message = Message(
        conversation_id=conversation_id,
        seq=next_seq,
        role=NOTE_ROLE,
        content=req.content.strip(),
    )
    db.add(note_message)
    try:
        db.commit()
        db.refresh(note_message)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="La conversation a été modifiée simultanément, veuillez réessayer."
        )

    # Note enregistrée : aucun appel au LLM n'est effectué
    return MessageResponse(
        seq=note_message.seq,
        role=note_message.role,
        content=note_message.content,
        created_at=note_message.created_at,
    )


@app.get("/models")
def get_models() -> list[str]:
    return ALLOWED_MODELS


@app.get("/prompts")
def list_prompts() -> list[PromptOption]:
    if not PROMPTS_DIR.exists():
        return []
    # system.md first, then old.md or other prompts
    files = sorted([f.name for f in PROMPTS_DIR.glob("*.md")], reverse=True)
    return [PromptOption(id=f, name=PROMPT_LABELS.get(f, f)) for f in files]


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    if not req.model or not isinstance(req.model, str) or req.model.strip() not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Modèle non autorisé ou manquant : '{req.model}'. Modèles valides : {ALLOWED_MODELS}",
        )
    chosen_model = req.model.strip()

    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Le message ne peut pas être vide.")

    system_prompt = load_prompt_by_name(req.prompt)

    # Charge la conversation pour construire l'historique et calculer le numéro de séquence
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    user_seq = rows[-1].seq + 1 if rows else 1
    assistant_seq = user_seq + 1

    # 1. Enregistre le message de l'utilisateur AVANT d'appeler le LLM
    user_msg = Message(
        conversation_id=req.conversation_id,
        seq=user_seq,
        role="user",
        content=req.message,
    )
    db.add(user_msg)
    try:
        db.commit()
        db.refresh(user_msg)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="La conversation a été modifiée en parallèle, veuillez réessayer."
        )
    user_msg_id = user_msg.id

    # Messages pour le LLM (sans les notes ni les notifications)
    user_message = {"role": "user", "content": req.message}
    messages = [{"role": "system", "content": system_prompt}, *history, user_message]

    # 2. Ouvre le flux vers l'API LLM pour vérifier le statut HTTP AVANT de streamer au client
    client = httpx.Client(timeout=60.0)
    stream_cm = client.stream(
        "POST",
        RODIUMAI_URL,
        headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
        json={
            "model": chosen_model,
            "messages": messages,
            "max_tokens": 512,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    try:
        response = stream_cm.__enter__()
        if response.status_code != 200:
            err_text = response.read().decode("utf-8", errors="replace")
            stream_cm.__exit__(None, None, None)
            client.close()
            # Nettoyage : suppression du message utilisateur orphelin en cas d'échec initial
            with SessionLocal() as clean_db:
                row_to_del = clean_db.get(Message, user_msg_id)
                if row_to_del:
                    clean_db.delete(row_to_del)
                    clean_db.commit()
            raise HTTPException(status_code=502, detail=f"The LLM API call failed: {err_text}")
    except HTTPException:
        raise
    except Exception as exc:
        stream_cm.__exit__(*sys.exc_info())
        client.close()
        with SessionLocal() as clean_db:
            row_to_del = clean_db.get(Message, user_msg_id)
            if row_to_del:
                clean_db.delete(row_to_del)
                clean_db.commit()
        raise HTTPException(status_code=502, detail="The LLM API call failed.") from exc

    # 3. Générateur qui transmet les morceaux et gère la persistance finale
    def stream_generator():
        collected_chunks = []
        has_error = False
        was_stopped = False
        try:
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data_json = json.loads(data_str)
                        # Transmet les métadonnées de consommation de tokens
                        if "usage" in data_json and data_json["usage"]:
                            yield f"\n[[USAGE:{json.dumps(data_json['usage'])}]]"
                        choices = data_json.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content")
                            if content:
                                collected_chunks.append(content)
                                yield content
                    except json.JSONDecodeError:
                        continue
        except GeneratorExit:
            # L'utilisateur a cliqué sur le bouton "Arrêter" (déconnexion du client)
            was_stopped = True
        except Exception:
            has_error = True
            yield "\n[[ERREUR_STREAM]]"
        finally:
            stream_cm.__exit__(None, None, None)
            client.close()

            # Utilise une session de base de données dédiée pour éviter les sessions fermées trop tôt
            with SessionLocal() as stream_db:
                if was_stopped:
                    # En cas d'arrêt volontaire : on sauvegarde ce qui a déjà été généré
                    if collected_chunks:
                        partial_reply = "".join(collected_chunks) + " *(génération interrompue)*"
                        stream_db.add(
                            Message(
                                conversation_id=req.conversation_id,
                                seq=assistant_seq,
                                role="assistant",
                                content=partial_reply,
                            )
                        )
                        try:
                            stream_db.commit()
                        except Exception:
                            stream_db.rollback()
                    else:
                        try:
                            user_to_delete = stream_db.get(Message, user_msg_id)
                            if user_to_delete:
                                stream_db.delete(user_to_delete)
                                stream_db.commit()
                        except Exception:
                            stream_db.rollback()
                elif not has_error and collected_chunks:
                    full_reply = "".join(collected_chunks)
                    stream_db.add(
                        Message(
                            conversation_id=req.conversation_id,
                            seq=assistant_seq,
                            role="assistant",
                            content=full_reply,
                        )
                    )
                    try:
                        stream_db.commit()
                    except Exception:
                        stream_db.rollback()
                elif has_error:
                    # En cas d'erreur réseau en cours de route : pas de message corrompu
                    try:
                        user_to_delete = stream_db.get(Message, user_msg_id)
                        if user_to_delete:
                            stream_db.delete(user_to_delete)
                            stream_db.commit()
                    except Exception:
                        stream_db.rollback()

    return StreamingResponse(stream_generator(), media_type="text/plain; charset=utf-8")


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "bootcamp-chatbot-frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
