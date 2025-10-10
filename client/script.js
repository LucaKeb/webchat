let ws = null;
let authToken = null;
let currentUser = ""; 

// Configuracoes do WebSocket
const WS_SCHEME = window.location.protocol === "https:" ? "wss" : "ws";
const WS_HOST = "127.0.0.1:8000";

function wsUrlWithToken(token) {
    const url = new URL(`${WS_SCHEME}://${WS_HOST}/ws`);
    url.searchParams.set("token", token);
    return url.toString();
}

document.addEventListener('DOMContentLoaded', () => {

    // Habilita apenas quando usuaprio for inserido 
    document.getElementById('sendBtn').disabled = true;

    document.getElementById('loginBtn').addEventListener('click', handleLogin);
    document.getElementById('logoutBtn').addEventListener('click', logout);
    document.getElementById('sendBtn').addEventListener('click', sendMessage);
    document.getElementById('clearBtn').addEventListener('click', () => {
        const activeChatBox = document.querySelector('.chat-window.is-active');
        if (activeChatBox) {
            activeChatBox.innerHTML = '';
        }
    });

    //Eventos para as abas 
    document.getElementById('chatTabs').addEventListener('click', (event) => {
        const target = event.target;
        const tabLi = target.closest('li');

        if (target.classList.contains('delete')) { 
            const tabNameToClose = tabLi.dataset.target;
            closeTab(tabNameToClose);
        } else if (tabLi) { 
            const tabNameToActivate = tabLi.dataset.target;
            switchTab(tabNameToActivate);
        }
    });
});

// --- Funcoes de Logica ---**
async function handleLogin() {
    currentUser = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    const messageEl = document.getElementById('message');

    if (!currentUser || !password) {
        messageEl.textContent = "Preencha usuário e senha.";
        return;
    }
    messageEl.textContent = "Conectando...";

    try {
        const token = await loginApi(currentUser, password);
        authToken = token;

        document.getElementById('loginContainer').classList.add('is-hidden');
        document.getElementById('chatContainer').classList.remove('is-hidden');
        document.getElementById('sendBtn').disabled = false;

        connectWebSocket(token);
        refreshUsers();

    } catch (error) {
        console.error("Erro no login:", error);
        messageEl.textContent = "Usuário ou senha inválidos.";
    }
}

function connectWebSocket(token) {
    ws = new WebSocket(wsUrlWithToken(token));

    ws.onopen = () => {
        console.log("Conectado ao WebSocket.");
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        console.log(msg); 

        switch (msg.type) {
            case 'message':
                const sender = msg.sender || currentUser;
                const targetTab = msg.to ? sender : 'broadcast';
                
                // Abre a aba do usuario se for DM
                if (msg.to && sender !== currentUser) {
                    ensureTab(sender);
                }

                appendMessage(targetTab, {
                    sender,
                    text: msg.text,
                    sent_at: msg.sent_at,
                });
                break;
            
            case 'presence':
                refreshUsers();
                break;
            case 'system':
                refreshUsers();
                break;
            case 'typing':
                 break;
            default:
                console.log("Mensagem de tipo desconhecido recebida.");
        }
    };

    ws.onerror = (event) => {
        console.error("WebSocket error:", event);
        console.log("Erro no WebSocket.");
        document.getElementById('sendBtn').disabled = true;
    };

    ws.onclose = (event) => {
        console.log(`WebSocket fechado: code=${event.code}, reason=${event.reason}`);
        document.getElementById('sendBtn').disabled = true;
    };
}

function logout() {
    if (ws && ws.readyState !== WebSocket.CLOSED) {
        ws.close();
    }
    ws = null;
    authToken = null;
    currentUser = "";

    document.getElementById('sendBtn').disabled = true;
    document.getElementById('loginContainer').classList.remove('is-hidden');
    document.getElementById('chatContainer').classList.add('is-hidden');    
    document.getElementById('chatTabs').innerHTML = '<li class="is-active" data-target="broadcast"><a>Broadcast</a></li>'; // Reseta as abas
    document.getElementById('chatBoxes').innerHTML = '<div class="chat-window tab-content is-active" id="tab-broadcast"></div>'; // Reseta as caixas de chat
}

function sendMessage() {
    const msgInput = document.getElementById('msg');
    const text = msgInput.value;
    if (!text) return;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        console.log("WebSocket não está conectado.");
        return;
    }

    const activeTab = document.querySelector('#chatTabs li.is-active').dataset.target;
    const payload = (activeTab === 'broadcast')
        ? { type: 'message', text }
        : { type: 'message', text, to: activeTab };

    ws.send(JSON.stringify(payload));
    msgInput.value = ''; 

    if (activeTab !== 'broadcast') {
        appendMessage(activeTab, {
            sender: currentUser,
            text,
            sent_at: new Date().toISOString(),
        });
    }
}

// --- Funcoes de Manipulacao do DOM ---**
async function refreshUsers() {
    if (!authToken) return;
    try {
        const [allUsers, onlineUsersSet] = await Promise.all([
            getUsersApi(authToken),
            getOnlineUsersApi(authToken).then(users => new Set(users.map(user => user.username)))
        ]);

        const usersWithStatus = allUsers
            .map(user => ({ username: user.username, online: onlineUsersSet.has(user.username) }))
            .sort((a, b) => {
                if (a.online === b.online) return a.username.localeCompare(b.username);
                return a.online ? -1 : 1;
            });
        
        renderUsers(usersWithStatus);
        
        const onlineUsernames = Array.from(onlineUsersSet);
        document.getElementById('presence').textContent = `Online: ${onlineUsernames.length ? onlineUsernames.join(', ') : 'nenhum'}`;

    } catch (error) {
        console.error("Erro ao atualizar usuários:", error);
    }
}

function renderUsers(users) {
    const usersContainer = document.getElementById('users');
    usersContainer.innerHTML = ''; 

    users.forEach(user => {
        if (user.username === currentUser) return; 

        const userLink = document.createElement('a');
        userLink.className = `panel-block user ${user.online ? 'online' : 'offline'}`;
        userLink.innerHTML = `<span class="dot"></span> ${user.username}`;

        if (user.online) {
            userLink.onclick = () => ensureTab(user.username);
        }
        usersContainer.appendChild(userLink);
    });
}

function ensureTab(username) {
    if (username === currentUser) return;

    // Verifica se a aba ja existe
    if (!document.querySelector(`#chatTabs li[data-target="${username}"]`)) {
        openUserTab(username);
    }
    switchTab(username);
}

function openUserTab(username) {
    const tabList = document.getElementById('chatTabs');
    const newTab = document.createElement('li');
    newTab.dataset.target = username;
    newTab.innerHTML = `<a>${username}</a><button class="delete is-small"></button>`;
    tabList.appendChild(newTab);

    // Cria a caixa de chat
    const chatBoxes = document.getElementById('chatBoxes');
    const newChatBox = document.createElement('div');
    newChatBox.className = 'chat-window tab-content';
    newChatBox.id = `tab-${username}`;
    chatBoxes.appendChild(newChatBox);
}

function switchTab(target) {
    // Desativa todas as abas
    document.querySelectorAll('#chatTabs li').forEach(tab => tab.classList.remove('is-active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('is-active'));

    // Ativa a aba e o conteudo correto
    const tabToActivate = document.querySelector(`#chatTabs li[data-target="${target}"]`);
    const contentToActivate = document.getElementById(`tab-${target}`);

    if (tabToActivate) tabToActivate.classList.add('is-active');
    if (contentToActivate) contentToActivate.classList.add('is-active');
}

function closeTab(target) {
    if (target === 'broadcast') return;

    const tabToRemove = document.querySelector(`#chatTabs li[data-target="${target}"]`);
    const contentToRemove = document.getElementById(`tab-${target}`);

    if (tabToRemove) tabToRemove.remove();
    if (contentToRemove) contentToRemove.remove();

    // Se a aba fechada era a ativa volta para o broadcast
    if (tabToRemove && tabToRemove.classList.contains('is-active')) {
        switchTab('broadcast');
    }
}

function appendMessage(targetTab, { sender, text, sent_at }) {
    const box = document.getElementById(`tab-${targetTab}`);
    if (!box) {
        console.error(`Caixa de chat para "${targetTab}" não encontrada.`);
        return;
    }

    const isMe = (sender === currentUser);
    
    const container = document.createElement('div');
    container.className = `message-container ${isMe ? 'is-sender' : 'is-receiver'}`;

    const meta = document.createElement('div');
    meta.className = 'message-meta';
    const time = sent_at ? new Date(sent_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    meta.textContent = `${sender} • ${time}`;

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = text;
    
    container.appendChild(meta);
    container.appendChild(bubble);
    box.appendChild(container);

    // Rola para a ultima mensagem
    box.scrollTop = box.scrollHeight;
}

// --- Funcoes da API ---**
async function loginApi(username, password) {
    const response = await fetch("http://127.0.0.1:8000/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username, password })
    });
    if (!response.ok) throw new Error("Falha na autenticação: " + response.status);
    const data = await response.json();
    return data.access_token;
}

async function getUsersApi(token) {
    const response = await fetch("http://127.0.0.1:8000/users", {
        headers: { "Authorization": `Bearer ${token}` }
    });
    if (!response.ok) throw new Error("Erro ao buscar usuários: " + response.status);
    return await response.json();
}

async function getOnlineUsersApi(token) {
    const response = await fetch("http://127.0.0.1:8000/users/online", {
        headers: { "Authorization": `Bearer ${token}` }
    });
    if (!response.ok) throw new Error("Erro ao buscar usuários online: " + response.status);
    return await response.json();
}