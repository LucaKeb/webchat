# chat_server.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set, List
import os, json

import jwt  # pip install pyjwt
from passlib.context import CryptContext  # pip install passlib[bcrypt]
from fastapi import (
    FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import asyncio
import aio_pika
from aio_pika import ExchangeType, Message, DeliveryMode


# =========================
# Configuração AMQP
# =========================
AMQP_URL = os.getenv("AMQP_URL", "amqp://guest:guest@localhost/")

amqp_conn = None
amqp_chan = None
ex_direct = None       # para DMs 
ex_broadcast = None    # para broadcast (fanout)

user_consumer_tasks = {}  # username -> asyncio.Task

# =========================
# Configuração
# =========================
JWT_SECRET = os.getenv("JWT_SECRET", "troque-este-segredo")  # use Secret Manager/variável de ambiente em produção
JWT_ALG = os.getenv("JWT_ALG", "HS256")
ACCESS_TTL_MIN = int(os.getenv("ACCESS_TTL_MIN", "60"))

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Usuários pré-definidos (apenas estes podem usar o chat)
_raw_users = {
    "alice": "alice@123",
    "bob":   "bob@123",
    "carol": "carol@123",
}
USERS: Dict[str, Dict[str, str]] = {
    u: {"username": u, "password_hash": pwd.hash(p)} for u, p in _raw_users.items()
}

# =========================
# Modelos HTTP
# =========================
class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int

class MeOut(BaseModel):
    username: str

class PublicUser(BaseModel):
    username: str
    online: bool = Field(description="Se o usuário está conectado via WebSocket")

# =========================
# Helpers de tempo/JWT
# =========================
def _now():
    return datetime.now(timezone.utc)

def _iso():
    return _now().isoformat()

def make_access_token(sub: str) -> TokenOut:
    exp = _now() + timedelta(minutes=ACCESS_TTL_MIN)
    payload = {
        "sub": sub,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    return TokenOut(access_token=token, expires_at=int(exp.timestamp()))

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def current_username(token: str = Depends(oauth2_scheme)) -> str:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token não é de acesso")
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=403, detail="Usuário não autorizado")
    return username

# =========================
# App & CORS
# =========================
app = FastAPI(title="Chat em FastAPI + JWT (HTTP + WebSocket)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restrinja em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Camada API (auth, perfil, listagem)
# =========================
@app.post("/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends()):
    user = USERS.get(form.username)
    if not user or not pwd.verify(form.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    return make_access_token(form.username)

@app.get("/me", response_model=MeOut)
def me(username: str = Depends(current_username)):
    return MeOut(username=username)

@app.get("/users", response_model=List[PublicUser])
def list_users(_: str = Depends(current_username)):
    return [PublicUser(username=u, online=u in manager.active) for u in sorted(USERS.keys())]

@app.get("/users/online", response_model=List[PublicUser])
def list_online(_: str = Depends(current_username)):
    return [PublicUser(username=u, online=True) for u in sorted(manager.active.keys())]

@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": _iso()}

# =========================
# Auxiliares mensageiro
# =========================
async def amqp_publish_dm(to_user: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    await ex_direct.publish(
        Message(body=body, delivery_mode=DeliveryMode.PERSISTENT),
        routing_key=to_user,
    )

async def amqp_publish_broadcast(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    await ex_broadcast.publish(
        Message(body=body, delivery_mode=DeliveryMode.PERSISTENT),
        routing_key="",
    )

async def amqp_start_user_consumer(username: str, manager):
    """
    Garante a fila do usuário, faz os binds e inicia um consumer assíncrono
    que repassa o payload pro WebSocket do próprio usuário.
    """
    queue = await amqp_chan.declare_queue(
        f"q.user.{username}", durable=True, auto_delete=False, exclusive=False
    )
    await queue.bind(ex_direct, routing_key=username)
    await queue.bind(ex_broadcast)

    async def _run():
        async with queue.iterator() as it:
            async for msg in it:
                async with msg.process():
                    try:
                        obj = json.loads(msg.body.decode("utf-8"))
                    except Exception:
                        continue
                    # empurra pro websocket do usuário via manager existente
                    await manager.send_to(username, obj)

    task = asyncio.create_task(_run(), name=f"amqp-consumer-{username}")
    user_consumer_tasks[username] = task

def amqp_stop_user_consumer(username: str):
    task = user_consumer_tasks.pop(username, None)
    if task:
        task.cancel()

# =========================
# Cria exchanges
# =========================
@app.on_event("startup")
async def _amqp_startup():
    global amqp_conn, amqp_chan, ex_direct, ex_broadcast
    amqp_conn = await aio_pika.connect_robust(AMQP_URL)
    amqp_chan = await amqp_conn.channel()
    ex_direct = await amqp_chan.declare_exchange("ex.direct", ExchangeType.DIRECT, durable=True)
    ex_broadcast = await amqp_chan.declare_exchange("ex.broadcast", ExchangeType.FANOUT, durable=True)

    for username in USERS.keys(): # pré criando os canais, e fazendo bind, já que os usuarios sao conhecidos
        q = await amqp_chan.declare_queue(
            f"q.user.{username}",
            durable=True,
            auto_delete=False,
            exclusive=False,
        )
        await q.bind(ex_direct, routing_key=username)
        await q.bind(ex_broadcast)  
    print("Criado Exchanges e filas")


@app.on_event("shutdown")
async def _amqp_shutdown(): 
    global amqp_conn
    if amqp_conn:
        await amqp_conn.close()
        amqp_conn = None


# =========================
# Camada WebSocket (chat, presença, digitação)
# FastAPI não tem suporte nativo a sessões ou usuários em WebSocket, 
# mas tem um modulo WebSocket simples implementado sobre ASGI.
# https://www.starlette.io/
# =========================
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}  # username -> ws
        self.typing: Set[str] = set()           # usuários atualmente digitando

    async def connect(self, username: str, websocket: WebSocket):
        old = self.active.get(username)
        if old:
            try:
                await old.close(code=4001)
            except Exception:
                pass
        self.active[username] = websocket

    def disconnect(self, username: str):
        self.active.pop(username, None)
        self.typing.discard(username)

    async def send_to(self, username: str, payload: dict) -> bool:
        ws = self.active.get(username)
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.disconnect(username)
            return False

    async def broadcast(self, payload: dict):
        dead: Set[str] = set()
        for user, ws in self.active.items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(user)
        for u in dead:
            self.disconnect(u)

    def roster_payload(self) -> dict:
        return {
            "type": "presence",
            "online": sorted(self.active.keys()),
            "timestamp": _iso(),
        }

    def typing_payload(self) -> dict:
        return {
            "type": "typing",
            "users": sorted(self.typing),
            "timestamp": _iso(),
        }

manager = ConnectionManager()

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    # Aceita para poder fechar com códigos customizados
    await websocket.accept()
    if not token:
        await websocket.close(code=4401)  # Unauthorized
        return

    # Autenticação via JWT
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401); return
        username = payload.get("sub")
        if username not in USERS:
            await websocket.close(code=4403); return
    except HTTPException:
        await websocket.close(code=4401); return

    # Conecta e anuncia presença
    await manager.connect(username, websocket)

    await amqp_start_user_consumer(username, manager) #inicia o consumer do usuario  

    await manager.broadcast({"type": "system", "text": f"{username} entrou no chat.", "timestamp": _iso()})
    await manager.broadcast(manager.roster_payload())
    await manager.broadcast(manager.typing_payload())  # mantém cliente em sincronia

    try:
        while True:
            raw = await websocket.receive_text()
            # Protocolo simples baseado em "type"
            # - {"type":"who"}                    -> devolve presença atual
            # - {"type":"typing","state":"start"} -> usuário começou a digitar
            # - {"type":"typing","state":"stop"}  -> usuário parou de digitar
            # - {"type":"message","text":"..."}   -> broadcast
            # - {"type":"message","text":"...","to":"bob"} -> DM para bob
            try:
                obj = json.loads(raw)
            except Exception:
                # Texto puro -> trata como mensagem broadcast
                obj = {"type": "message", "text": raw}

            kind = obj.get("type")

            if kind == "who":
                await websocket.send_json(manager.roster_payload())
                await websocket.send_json(manager.typing_payload())
                continue

            if kind == "typing":
                state = str(obj.get("state", "")).lower()
                if state == "start":
                    manager.typing.add(username)
                elif state == "stop":
                    manager.typing.discard(username)
                await manager.broadcast(manager.typing_payload())
                continue

            if kind == "message":
                text = str(obj.get("text", "")).strip()
                if not text:
                    continue
                to_user = obj.get("to")
                payload = {
                    "type": "message",
                    "sender": username,
                    "text": text,
                    "to": to_user,
                    "sent_at": _iso(),
                }
                
                try:
                    if to_user:
                        await amqp_publish_dm(to_user, payload) # DM
                    else:
                        await amqp_publish_broadcast(payload) # Broadcast

                    await manager.send_to(username, {"type":"delivery", "status":"queued", "to":to_user, "timestamp": _iso()})

                except Exception as e:
                    print(); print(e); print()

                continue

                # if to_user:
                #     ok = await manager.send_to(to_user, payload)
                #     # confirma ao remetente (e informa offline)
                #     ack = {"type": "delivery", "to": to_user, "status": "delivered" if ok else "offline", "timestamp": _iso()}
                #     await manager.send_to(username, ack)
                # else:
                #     await manager.broadcast(payload)
                # continue

            # Mensagens desconhecidas: ignore ou logue
            await manager.send_to(username, {"type": "error", "message": "invalid_payload", "timestamp": _iso()})

    except WebSocketDisconnect:
        manager.disconnect(username)

        amqp_stop_user_consumer(username) # Finaliza o consumer do usuario

        await manager.broadcast({"type": "system", "text": f"{username} saiu do chat.", "timestamp": _iso()})
        await manager.broadcast(manager.roster_payload())
        await manager.broadcast(manager.typing_payload())
    except Exception:
        manager.disconnect(username)
        await websocket.close(code=1011)