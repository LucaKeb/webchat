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

# --- (NOVO) Imports para RabbitMQ e Threading ---
import pika
import threading
import asyncio
import functools
import time
# --- Fim (NOVO) ---


# =========================
# Configuração
# =========================
JWT_SECRET = os.getenv("JWT_SECRET", "troque-este-segredo")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
ACCESS_TTL_MIN = int(os.getenv("ACCESS_TTL_MIN", "60"))

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

_raw_users = {
    "alice": "alice@123",
    "bob":   "bob@123",
    "carol": "carol@123",
}
USERS: Dict[str, Dict[str, str]] = {
    u: {"username": u, "password_hash": pwd.hash(p)} for u, p in _raw_users.items()
}

# --- (NOVO) Constantes do RabbitMQ ---
RABBIT_HOST = os.getenv("RABBIT_HOST", "localhost")
EX_BROADCAST = 'chat.broadcast' # Exchange para todos
EX_DIRECT = 'chat.direct'     # Exchange para DMs
# --- Fim (NOVO) ---


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
    allow_origins=["*"],
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
# (NOVO) Publisher (Produtor) RabbitMQ
# =========================
# Esta é uma função BLOQUEANTE, ela será executada em uma thread
# para não travar o loop async do FastAPI.
def publish_message(payload: dict, to_user: Optional[str] = None):
    """Publica uma mensagem no RabbitMQ."""
    try:
        # Cria uma nova conexão (pika não é thread-safe entre threads)
        conn = pika.BlockingConnection(pika.ConnectionParameters(RABBIT_HOST))
        ch = conn.channel()

        # Garante que os exchanges existam
        ch.exchange_declare(exchange=EX_BROADCAST, exchange_type='fanout')
        ch.exchange_declare(exchange=EX_DIRECT, exchange_type='direct')
        
        body = json.dumps(payload)
        
        if to_user:
            # MENSAGEM DIRETA (DM)
            # Garante que a fila do usuário exista e seja durável (para msgs offline)
            ch.queue_declare(queue=to_user, durable=True)
            ch.queue_bind(exchange=EX_DIRECT, queue=to_user, routing_key=to_user)
            
            ch.basic_publish(
                exchange=EX_DIRECT,
                routing_key=to_user,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2 # Torna a mensagem persistente
                )
            )
            print(f"[Publisher] Mensagem DM para '{to_user}' enviada.")
        else:
            # MENSAGEM BROADCAST
            ch.basic_publish(
                exchange=EX_BROADCAST,
                routing_key='', # Routing key é ignorada em fanout
                body=body
            )
            print(f"[Publisher] Mensagem broadcast enviada.")
            
        conn.close()
    except Exception as e:
        print(f"[Publisher] Erro ao publicar: {e}")

# =========================
# (NOVO) Consumer (Consumidor) Thread
# =========================
class UserConsumerThread(threading.Thread):
    """
    Uma thread por usuário conectado, que ouve broadcast e DMs
    para aquele usuário.
    """
    def __init__(self, username: str, manager: ConnectionManager, loop: asyncio.AbstractEventLoop):
        super().__init__(daemon=True)
        self.username = username
        self.manager = manager  # O ConnectionManager compartilhado
        self.loop = loop        # O loop asyncio principal
        self._conn = None
        self._ch = None

    def stop(self):
        """Sinaliza para a thread parar."""
        if self._conn and self._conn.is_open:
            # Pede para o pika fechar a conexão de forma thread-safe
            self._conn.add_callback_threadsafe(self._conn.close)

    def on_message(self, ch, method, properties, body):
        """Callback executado quando uma mensagem chega do RabbitMQ."""
        try:
            payload = json.loads(body)
            
            # (IMPORTANTE) Estamos em uma Thread, mas o manager.send_to é ASYNC.
            # Usamos run_coroutine_threadsafe para agendar a execução da
            # corrotina no loop asyncio principal.
            asyncio.run_coroutine_threadsafe(
                self.manager.send_to(self.username, payload),
                self.loop
            )
            
        except Exception as e:
            print(f"[Consumer {self.username}] Erro ao processar msg: {e}")
        finally:
            # Confirma o recebimento da mensagem
            ch.basic_ack(delivery_tag=method.delivery_tag)

    def run(self):
        """O corpo principal da thread."""
        try:
            self._conn = pika.BlockingConnection(pika.ConnectionParameters(RABBIT_HOST))
            self._ch = self._conn.channel()

            # Garante que os exchanges existam
            self._ch.exchange_declare(exchange=EX_BROADCAST, exchange_type='fanout')
            self._ch.exchange_declare(exchange=EX_DIRECT, exchange_type='direct')

            # --- Configura Consumidor de Broadcast ---
            # 1. Declara uma fila exclusiva e anônima (será deletada ao fechar)
            res_b = self._ch.queue_declare(queue='', exclusive=True)
            q_b = res_b.method.queue
            # 2. Liga a fila ao exchange fanout
            self._ch.queue_bind(exchange=EX_BROADCAST, queue=q_b)
            # 3. Começa a consumir
            self._ch.basic_consume(queue=q_b, on_message_callback=self.on_message)

            # --- Configura Consumidor de DM (Mensagens Diretas) ---
            # 1. Declara a fila durável do usuário (pelo username)
            self._ch.queue_declare(queue=self.username, durable=True)
            # 2. Liga ao exchange direto com o username como routing key
            self._ch.queue_bind(exchange=EX_DIRECT, queue=self.username, routing_key=self.username)
            # 3. Começa a consumir
            self._ch.basic_consume(queue=self.username, on_message_callback=self.on_message)

            print(f"[Consumer {self.username}] Aguardando mensagens...")
            # Começa o loop de consumo (bloqueante)
            self._ch.start_consuming()

        except pika.exceptions.ConnectionClosedByBroker:
            print(f"[Consumer {self.username}] Conexão fechada pelo broker.")
        except pika.exceptions.AmqpConnectionError as e:
            print(f"[Consumer {self.username}] Erro de conexão AMQP: {e}")
        finally:
            print(f"[Consumer {self.username}] Encerrando.")
            
# =========================
# Camada WebSocket (Modificada)
# =========================
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}  # username -> ws
        self.typing: Set[str] = set()

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
    await websocket.accept()
    if not token:
        await websocket.close(code=4401); return

    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401); return
        username = payload.get("sub")
        if username not in USERS:
            await websocket.close(code=4403); return
    except HTTPException:
        await websocket.close(code=4401); return

    # (NOVO) Referência para a thread do consumidor
    consumer_thread: Optional[UserConsumerThread] = None
    
    try:
        # Conecta e anuncia presença (via WebSocket, como antes)
        await manager.connect(username, websocket)
        await manager.broadcast({"type": "system", "text": f"{username} entrou no chat.", "timestamp": _iso()})
        await manager.broadcast(manager.roster_payload())
        await manager.broadcast(manager.typing_payload())

        # (NOVO) Inicia a thread do consumidor RabbitMQ para este usuário
        main_loop = asyncio.get_running_loop()
        consumer_thread = UserConsumerThread(username, manager, main_loop)
        consumer_thread.start()
        # --- Fim (NOVO) ---

        while True:
            raw = await websocket.receive_text()
            try:
                obj = json.loads(raw)
            except Exception:
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

                # (NOVO) Em vez de enviar, publica no RabbitMQ
                # Usamos run_in_executor para rodar a função 'publish_message'
                # (que é bloqueante) em uma thread separada, sem travar o FastAPI.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,  # Usa o ThreadPoolExecutor padrão
                    publish_message, # A função bloqueante
                    payload,         # 1º argumento da função
                    to_user          # 2º argumento da função
                )
                
                # (MODIFICADO) Confirma ao remetente que a msg foi *enfileirada*
                if to_user:
                    ack = {"type": "delivery", "to": to_user, "status": "queued", "timestamp": _iso()}
                    await manager.send_to(username, ack)
                
                continue # Pula a lógica antiga de broadcast/send_to

            # Mensagens desconhecidas
            await manager.send_to(username, {"type": "error", "message": "invalid_payload", "timestamp": _iso()})

    except WebSocketDisconnect:
        print(f"WebSocketDisconnect para {username}")
    except Exception as e:
        print(f"Erro inesperado no WebSocket de {username}: {e}")
    finally:
        # (NOVO) Garante que a thread do consumidor pare ao desconectar
        if consumer_thread:
            print(f"Parando consumer thread para {username}...")
            consumer_thread.stop()
            # Não esperamos o join() aqui para não bloquear a desconexão

        # Lógica de desconexão (sempre executada)
        manager.disconnect(username)
        # Precisamos rodar isso no loop, caso não esteja rodando
        loop = asyncio.get_event_loop()
        if loop.is_running():
            await manager.broadcast({"type": "system", "text": f"{username} saiu do chat.", "timestamp": _iso()})
            await manager.broadcast(manager.roster_payload())
            await manager.broadcast(manager.typing_payload())
        else:
            print(f"{username} desconectado, mas loop não está rodando para broadcast.")