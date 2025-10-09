let ws = null;
let authToken = null;
let currentUser = ""; // Armazenar o nome do usuário logado

// Configurações do WebSocket
const WS_SCHEME = window.location.protocol === "https:" ? "wss" : "ws";
const WS_HOST = "127.0.0.1:8000";

function wsUrlWithToken(token) {
    const url = new URL(`${WS_SCHEME}://${WS_HOST}/ws`);
    url.searchParams.set("token", token);
    return url.toString();
}

// Executa quando o HTML da página terminar de carregar
document.addEventListener('DOMContentLoaded', () => {
    // Esconde o container do chat inicialmente
    const chatContainer = document.getElementById('chatContainer');
    chatContainer.style.display = 'none';

    // Desabilita o botão de enviar até o login ser feito
    document.getElementById('sendBtn').disabled = true;

    // Event Listeners
    document.getElementById('loginBtn').addEventListener('click', handleLogin);
    document.getElementById('logoutBtn').addEventListener('click', logout);
    document.getElementById('sendBtn').addEventListener('click', sendMessage);
    document.getElementById('clearBtn').addEventListener('click', () => {
        const activeChatBox = document.querySelector('.chat-window.is-active');
        if (activeChatBox) {
            activeChatBox.innerHTML = '';
        }
    });

    // Delegação de eventos para as abas (tabs)
    document.getElementById('chatTabs').addEventListener('click', (event) => {
        const target = event.target;
        const tabLi = target.closest('li');

        if (target.classList.contains('delete')) { // Se clicou no botão de fechar (x)
            const tabNameToClose = tabLi.dataset.target;
            closeTab(tabNameToClose);
        } else if (tabLi) { // Se clicou em uma aba
            const tabNameToActivate = tabLi.dataset.target;
            switchTab(tabNameToActivate);
        }
    });
});

// --- Funções de Lógica ---

function log(message) {
    const logEl = document.getElementById('log');
    const content = typeof message === 'string' ? message : JSON.stringify(message, null, 2);
    logEl.textContent += content + '\n';
    logEl.scrollTop = logEl.scrollHeight; // Auto-scroll
}

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

        log("Login bem-sucedido.");
        document.getElementById('loginContainer').style.display = 'none';
        document.getElementById('chatContainer').style.display = 'block';
        document.getElementById('sendBtn').disabled = false;

        connectWebSocket(token);
        refreshUsers();

    } catch (error) {
        console.error("Erro no login:", error);
        log("Falha no login.");
        messageEl.textContent = "Usuário ou senha inválidos.";
    }
}

function connectWebSocket(token) {
    ws = new WebSocket(wsUrlWithToken(token));

    ws.onopen = () => {
        log("Conectado ao WebSocket.");
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        log(msg); // Loga toda mensagem recebida para debug

        switch (msg.type) {
            case 'message':
                const sender = msg.sender || currentUser;
                // Se for DM, a aba alvo é o remetente. Se for broadcast, é 'broadcast'.
                const targetTab = msg.to ? sender : 'broadcast';
                
                // Abre a aba do chat se for uma DM de um novo usuário
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
            case 'system':
                refreshUsers();
                break;
            
            case 'typing':
                 setTyping(msg.users);
                 break;

            default:
                log("Mensagem de tipo desconhecido recebida.");
        }
    };

    ws.onerror = (event) => {
        console.error("WebSocket error:", event);
        log("Erro no WebSocket.");
        document.getElementById('sendBtn').disabled = true;
    };

    ws.onclose = (event) => {
        log(`WebSocket fechado: code=${event.code}, reason=${event.reason}`);
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
    document.getElementById('loginContainer').style.display = 'block';
    document.getElementById('chatContainer').style.display = 'none';
    document.getElementById('chatTabs').innerHTML = '<li class="is-active" data-target="broadcast"><a>Broadcast</a></li>'; // Reseta as abas
    document.getElementById('chatBoxes').innerHTML = '<div class="chat-window tab-content is-active" id="tab-broadcast"></div>'; // Reseta as caixas de chat
}

function sendMessage() {
    const msgInput = document.getElementById('msg');
    const text = msgInput.value;
    if (!text) return;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        log("WebSocket não está conectado.");
        return;
    }

    const activeTab = document.querySelector('#chatTabs li.is-active').dataset.target;
    const payload = (activeTab === 'broadcast')
        ? { type: 'message', text }
        : { type: 'message', text, to: activeTab };

    ws.send(JSON.stringify(payload));
    msgInput.value = ''; // Limpa o input

    // Se não for broadcast, adiciona a mensagem na sua própria tela imediatamente
    if (activeTab !== 'broadcast') {
        appendMessage(activeTab, {
            sender: currentUser,
            text,
            sent_at: new Date().toISOString(),
        });
    }
}

// --- Funções de Manipulação do DOM ---

async function refreshUsers() {
    if (!authToken) return;
    try {
        const [allUsers, onlineUsersSet] = await Promise.all([
            getUsersApi(authToken),
            getOnlineUsersApi(authToken).then(users => new Set(users))
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
    usersContainer.innerHTML = ''; // Limpa a lista antiga

    users.forEach(user => {
        if (user.username === currentUser) return; // Não exibe o próprio usuário na lista

        const userLink = document.createElement('a');
        userLink.className = `panel-block user ${user.online ? 'online' : 'offline'}`;
        userLink.innerHTML = `<span class="dot"></span> ${user.username}`;

        if (user.online) {
            userLink.onclick = () => ensureTab(user.username);
        }
        usersContainer.appendChild(userLink);
    });
}

function setTyping(typingUsers) {
    const typingDiv = document.getElementById("typing");
    // Filtra para não mostrar "Você está digitando"
    const otherTypingUsers = typingUsers.filter(u => u !== currentUser);

    typingDiv.textContent = otherTypingUsers.length
        ? `Digitando: ${otherTypingUsers.join(", ")}`
        : "";
}

function ensureTab(username) {
    if (username === currentUser) return;

    // Verifica se a aba já existe
    if (!document.querySelector(`#chatTabs li[data-target="${username}"]`)) {
        openUserTab(username);
    }
    switchTab(username);
}

function openUserTab(username) {
    // Cria a aba (li > a)
    const tabList = document.getElementById('chatTabs');
    const newTab = document.createElement('li');
    newTab.dataset.target = username;
    newTab.innerHTML = `<a>${username}</a><button class="delete is-small"></button>`;
    tabList.appendChild(newTab);

    // Cria a caixa de chat correspondente
    const chatBoxes = document.getElementById('chatBoxes');
    const newChatBox = document.createElement('div');
    newChatBox.className = 'chat-window tab-content';
    newChatBox.id = `tab-${username}`;
    chatBoxes.appendChild(newChatBox);
}

function switchTab(target) {
    // Desativa todas as abas e conteúdos
    document.querySelectorAll('#chatTabs li').forEach(tab => tab.classList.remove('is-active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('is-active'));

    // Ativa a aba e o conteúdo corretos
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

    // Se a aba fechada era a ativa, volta para o broadcast
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
    
    // <div class="message-container is-sender/is-receiver">
    const container = document.createElement('div');
    container.className = `message-container ${isMe ? 'is-sender' : 'is-receiver'}`;

    // <div class="message-meta">...</div>
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

    // Rola para a última mensagem
    box.scrollTop = box.scrollHeight;
}

// --- Funções de API (com fetch) ---

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