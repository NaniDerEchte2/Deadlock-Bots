    const opLog = document.getElementById('operation-log');
    const treeContainer = document.getElementById('tree-container');
    const tokenInput = document.getElementById('token-input');
    const selectionTitle = document.getElementById('selection-title');
    const selectionDescription = document.getElementById('selection-description');
    const resetSelectionBtn = document.getElementById('reset-selection');
    const standaloneContainer = document.getElementById('standalone-container');
    const STANDALONE_COMMANDS = {
        rank: [
            { value: 'queue.daily', label: 'Daily Queue erstellen' },
            { value: 'system.start', label: 'Benachrichtigungen starten' },
            { value: 'system.stop', label: 'Benachrichtigungen stoppen' },
            { value: 'dm.cleanup', label: 'DM Cleanup durchführen' },
            { value: 'state.refresh', label: 'Status aktualisieren' },
        ],
        steam: [
            { value: 'status', label: 'Status aktualisieren' },
            { value: 'login', label: 'Login starten' },
            { value: 'logout', label: 'Logout durchführen' },
            { value: 'quick.ensure', label: 'Quick Invites auffüllen' },
            { value: 'quick.create', label: 'Quick Invite erstellen' },
        ],
    };
    const logOpenState = new Map();
    let isRefreshingStandalone = false;
    let authToken = localStorage.getItem('master-dashboard-token') || '';
    let selectedNode = null;
    tokenInput.value = authToken;

    function log(message, type='info') {
        const entry = document.createElement('div');
        entry.textContent = new Date().toLocaleTimeString() + ' - ' + message;
        entry.className = type === 'error' ? 'error' : 'success';
        opLog.prepend(entry);
        while (opLog.childElementCount > 40) {
            opLog.removeChild(opLog.lastChild);
        }
    }

    function headers() {
        const h = { 'Content-Type': 'application/json' };
        if (authToken) {
            h['Authorization'] = 'Bearer ' + authToken;
        }
        return h;
    }

    async function fetchJSON(url, options = {}) {
        const response = await fetch(url, { headers: headers(), ...options });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || response.statusText);
        }
        return response.json();
    }

    function renderStatus(status) {
        const dot = document.createElement('span');
        dot.classList.add('status-dot');
        const label = document.createElement('span');
        label.textContent = status;
        if (status === 'loaded' || status === 'reloaded') {
            dot.classList.add('status-loaded');
        } else if (status.startsWith('error')) {
            dot.classList.add('status-error');
        } else if (status === 'blocked') {
            dot.classList.add('status-blocked');
        } else if (status === 'unloaded') {
            dot.classList.add('status-unloaded');
        } else {
            dot.classList.add('status-unknown');
        }
        const container = document.createElement('span');
        container.appendChild(dot);
        container.appendChild(label);
        return container;
    }

    function getNodePath(node) {
        if (!node) {
            return '';
        }
        return node.path || node.namespace || node.name || '';
    }

    function isDirectoryLike(type) {
        return type === 'directory' || type === 'root' || type === 'package' || type === 'namespace';
    }

    function applySelection() {
        if (!treeContainer) {
            return false;
        }
        const current = treeContainer.querySelectorAll('.selected');
        current.forEach((el) => el.classList.remove('selected'));
        if (!selectedNode || !selectedNode.path) {
            return false;
        }
        let target = null;
        const nodes = treeContainer.querySelectorAll('[data-path]');
        for (const el of nodes) {
            if (el.dataset.path === selectedNode.path) {
                target = el;
                break;
            }
        }
        if (!target) {
            return false;
        }
        target.classList.add('selected');
        if (target.tagName === 'DETAILS') {
            target.open = true;
        }
        let parent = target.parentElement;
        while (parent) {
            if (parent.tagName === 'DETAILS') {
                parent.open = true;
            }
            parent = parent.parentElement;
        }
        return true;
    }

    function selectNode(node) {
        const path = getNodePath(node);
        const nodeType = node.type || (Array.isArray(node.children) ? 'directory' : 'module');
        selectedNode = {
            path,
            type: nodeType,
        };
        applySelection();
    }

    function clearSelection() {
        selectedNode = null;
        applySelection();
    }

    async function loadStatus() {
        try {
            const data = await fetchJSON('/api/status');
            document.getElementById('bot-user').textContent = data.bot.user || 'Unbekannt';
            document.getElementById('bot-uptime').textContent = 'Uptime: ' + data.bot.uptime;
            document.getElementById('bot-guilds').textContent = 'Guilds: ' + data.bot.guilds;
            document.getElementById('bot-latency').textContent = 'Latency: ' + data.bot.latency_ms + ' ms';

            const standalone = data.standalone || [];
            renderStandalone(standalone);
            const cogs = data.cogs || {};
            const tree = cogs.tree || null;
            renderTree(tree);
        } catch (err) {
            log('Status konnte nicht geladen werden: ' + err.message, 'error');
        }
    }

    function createTag(label, className) {
        const tag = document.createElement('span');
        tag.className = 'tag ' + className;
        tag.textContent = label;
        return tag;
    }

    function renderTree(root) {
        if (!treeContainer) {
            return;
        }
        treeContainer.innerHTML = '';
        if (!root) {
            const empty = document.createElement('div');
            empty.className = 'tree-empty';
            empty.textContent = 'Keine Daten';
            treeContainer.appendChild(empty);
            return;
        }
        const built = buildTreeNode(root, 0);
        if (built) {
            treeContainer.appendChild(built);
        }
        const hasSelection = applySelection();
        if (selectedNode && !hasSelection) {
            clearSelection();
        }
    }

function safeNumber(value, fallback = 0) {
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function formatSeconds(seconds) {
    const value = safeNumber(seconds);
    if (!value) {
        return '0s';
    }
    const parts = [];
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = Math.floor(value % 60);
    if (hours) {
        parts.push(`${hours}h`);
    }
    if (minutes) {
        parts.push(`${minutes}m`);
    }
    if (secs || parts.length === 0) {
        parts.push(`${secs}s`);
    }
    return parts.join(' ');
}

function formatTimestamp(value) {
    if (!value) {
        return '–';
    }
    try {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleString();
    } catch (err) {
        return value;
    }
}

function renderRankMetrics(container, metrics) {
    const state = metrics.state || {};
    const queue = state.queue || {};
    const queueToday = queue.today || {};
    const dmInfo = state.dm || {};
    container.innerHTML = '';
    container.appendChild(document.createElement('div')).innerHTML = `<strong>Queue heute:</strong> ${safeNumber(queueToday.pending)} offen / ${safeNumber(queueToday.total)} gesamt`;
    container.appendChild(document.createElement('div')).innerHTML = `<strong>Queue gesamt:</strong> ${safeNumber(queue.pending_total)} offen (${safeNumber(queue.total_entries)} Einträge)`;
    container.appendChild(document.createElement('div')).innerHTML = `<strong>DM offen:</strong> ${safeNumber(dmInfo.pending)}`;

    if (state.loops) {
        const loopLine = Object.entries(state.loops)
            .map(([name, active]) => `${active ? '✅' : '⚠️'} ${name.replace(/_/g, ' ')}`)
            .join(' • ');
        const loopDiv = document.createElement('div');
        loopDiv.innerHTML = `<strong>Loops:</strong> ${loopLine || 'Keine Daten'}`;
        container.appendChild(loopDiv);
    }
}

function renderSteamMetrics(container, metrics) {
    container.innerHTML = '';
    const runtime = metrics.runtime || {};
    const steamState = metrics.steam || {};
    const quick = metrics.quick_invites || {};
    const quickCounts = quick.counts || {};
    const tasks = metrics.tasks || {};
    const taskCounts = tasks.counts || {};
    const taskProcessor = metrics.task_processor || {};
    const components = metrics.components || {};
    const presence = metrics.presence || {};

    const statusParts = [];
    if (steamState.logged_on) {
        statusParts.push('✅ Eingeloggt');
    } else if (steamState.logging_in) {
        statusParts.push('⏳ Login läuft');
    } else {
        statusParts.push('❌ Abgemeldet');
    }
    if (steamState.has_pending_guard || (steamState.guard_required && (steamState.guard_required.type || steamState.guard_required.domain))) {
        const guard = steamState.guard_required || {};
        const guardLabel = guard.type || guard.domain || 'unbekannt';
        statusParts.push(`Guard: ${guardLabel}`);
    }
    if (runtime.is_running === false) {
        statusParts.push('⏸️ Gestoppt');
    }
    if (runtime.uptime_human) {
        statusParts.push(`Uptime ${runtime.uptime_human}`);
    }
    const statusDiv = document.createElement('div');
    statusDiv.innerHTML = `<strong>Status:</strong> ${statusParts.join(' • ')}`;
    container.appendChild(statusDiv);

    const steamId = steamState.steam_id64 || runtime.steam_id64;
    const accountName = steamState.account_name || runtime.account_name;
    if (steamId || accountName) {
        const accountDiv = document.createElement('div');
        accountDiv.innerHTML = `<strong>Konto:</strong> ${accountName || '–'} (${steamId || '–'})`;
        container.appendChild(accountDiv);
    }

    if (runtime.memory_usage_mb) {
        const memoryDiv = document.createElement('div');
        memoryDiv.innerHTML = `<strong>Speicher:</strong> ${safeNumber(runtime.memory_usage_mb)} MB`;
        container.appendChild(memoryDiv);
    }

    const errorMessage = (steamState.last_error && steamState.last_error.message)
        || (typeof runtime.last_error === 'string' ? runtime.last_error : runtime.last_error && runtime.last_error.message)
        || (components.steam_client && components.steam_client.last_error && components.steam_client.last_error.message)
        || (components.quick_invites && components.quick_invites.last_auto_ensure_error && components.quick_invites.last_auto_ensure_error.message)
        || null;
    if (errorMessage) {
        const errorDiv = document.createElement('div');
        errorDiv.innerHTML = `<strong>Fehler:</strong> ${errorMessage}`;
        container.appendChild(errorDiv);
    }

    const presenceLine = [];
    if (Number.isFinite(Number(presence.active_users))) {
        presenceLine.push(`aktive Spieler ${safeNumber(presence.active_users)}`);
    }
    if (Number.isFinite(Number(presence.pending_requests))) {
        presenceLine.push(`pending ${safeNumber(presence.pending_requests)}`);
    }
    if (presenceLine.length) {
        const presenceDiv = document.createElement('div');
        presenceDiv.innerHTML = `<strong>Presence:</strong> ${presenceLine.join(' • ')}`;
        container.appendChild(presenceDiv);
    }

    const quickDiv = document.createElement('div');
    quickDiv.innerHTML = `<strong>Quick Invites:</strong> ${safeNumber(quick.available)} verfügbar (gesamt ${safeNumber(quick.total)})`;
    container.appendChild(quickDiv);

    if (Object.keys(quickCounts).length) {
        const countsLine = Object.entries(quickCounts)
            .map(([label, count]) => `${label}: ${safeNumber(count)}`)
            .join(' • ');
        const countsDiv = document.createElement('div');
        countsDiv.innerHTML = `<strong>Invite-Status:</strong> ${countsLine}`;
        container.appendChild(countsDiv);
    }

    const pendingTasks = safeNumber(taskCounts.PENDING ?? taskCounts.pending);
    const runningTasks = safeNumber(taskCounts.RUNNING ?? taskCounts.running);
    const failedTasks = safeNumber(taskCounts.FAILED ?? taskCounts.failed);
    const doneTasks = safeNumber(taskCounts.DONE ?? taskCounts.done);
    const taskDiv = document.createElement('div');
    taskDiv.innerHTML = `<strong>Tasks:</strong> ${pendingTasks} pending • ${runningTasks} running • ${failedTasks} failed • ${doneTasks} done`;
    container.appendChild(taskDiv);

    const componentSection = document.createElement('div');
    componentSection.className = 'steam-component-section';
    const componentTitle = document.createElement('strong');
    componentTitle.textContent = 'Komponenten-Status';
    componentSection.appendChild(componentTitle);

    const appendComponent = (label, statusText, details = []) => {
        const row = document.createElement('div');
        row.className = 'steam-component-entry';
        const header = document.createElement('div');
        header.innerHTML = `<span class="steam-component-name">${label}:</span> <span class="steam-component-status-text">${statusText}</span>`;
        row.appendChild(header);
        if (details.length) {
            const detail = document.createElement('div');
            detail.className = 'steam-component-details';
            detail.textContent = details.join(' • ');
            row.appendChild(detail);
        }
        componentSection.appendChild(row);
    };

    if (components.steam_client) {
        const sc = components.steam_client;
        let statusText = sc.logged_on ? '✅ Online' : (sc.logging_in ? '⏳ Login läuft' : '❌ Offline');
        if (sc.has_pending_guard || sc.guard_required) {
            statusText += ' • Guard offen';
        }
        const details = [];
        if (sc.steam_id64) {
            details.push(`ID: ${sc.steam_id64}`);
        }
        if (typeof sc.login_attempts === 'number') {
            details.push(`Versuche: ${sc.login_attempts}`);
        }
        if (sc.last_error && sc.last_error.message) {
            details.push(`Fehler: ${sc.last_error.message}`);
        }
        appendComponent('Steam Client', statusText, details);
    }

    if (taskProcessor && Object.keys(taskProcessor).length) {
        const lastProcessed = taskProcessor.lastProcessedAt ?? taskProcessor.last_processed_at;
        let statusText = taskProcessor.is_processing ? '✅ Aktiv' : '⏸️ Angehalten';
        if (taskProcessor.circuit_breaker_open) {
            statusText += ' • ⚠️ Circuit-Breaker';
        }
        const details = [];
        if (taskProcessor.poll_interval_ms) {
            details.push(`Polling: ${safeNumber(taskProcessor.poll_interval_ms)} ms`);
        }
        if (lastProcessed) {
            details.push(`Letzter Durchlauf: ${formatTimestamp(lastProcessed)}`);
        }
        if (typeof taskProcessor.processed === 'number') {
            details.push(`Verarbeitet: ${safeNumber(taskProcessor.processed)}`);
        }
        if (typeof taskProcessor.failed === 'number') {
            details.push(`Fehler: ${safeNumber(taskProcessor.failed)}`);
        }
        appendComponent('Task Prozessor', statusText, details);
    }

    if (components.quick_invites) {
        const qi = components.quick_invites;
        let statusText = '⚙️ Auto-Ensure deaktiviert';
        if (qi.auto_ensure_enabled) {
            statusText = qi.auto_ensure_active ? '✅ Auto-Ensure aktiv' : '⏳ Auto-Ensure bereit';
        }
        if (qi.ensure_in_flight) {
            statusText += ' • Läuft';
        }
        const details = [];
        if (qi.pool) {
            details.push(`Pool: Ziel ${safeNumber(qi.pool.target)} / Minimum ${safeNumber(qi.pool.min_available)}`);
        }
        if (qi.last_auto_ensure_at) {
            details.push(`Letzter Lauf: ${formatTimestamp(qi.last_auto_ensure_at)}`);
        }
        if (qi.last_sync_at) {
            details.push(`Letzter Sync: ${formatTimestamp(qi.last_sync_at)}`);
        }
        if (qi.last_auto_ensure_error && qi.last_auto_ensure_error.message) {
            details.push(`Fehler: ${qi.last_auto_ensure_error.message}`);
        } else if (qi.last_sync_error && qi.last_sync_error.message) {
            details.push(`Sync-Fehler: ${qi.last_sync_error.message}`);
        }
        appendComponent('Quick Invites', statusText, details);
    }

    if (components.statusanzeige) {
        const sa = components.statusanzeige;
        let statusText = sa.running ? '✅ Läuft' : '⏸️ Angehalten';
        if (sa.last_snapshot_pending) {
            statusText += ' • Snapshot ausstehend';
        }
        const details = [];
        if (sa.poll_interval_ms) {
            details.push(`Intervall: ${safeNumber(sa.poll_interval_ms)} ms`);
        }
        if (sa.last_snapshot_requested_at) {
            details.push(`Letzte Anfrage: ${formatTimestamp(sa.last_snapshot_requested_at)}`);
        }
        if (sa.last_snapshot_completed_at) {
            details.push(`Letzte Antwort: ${formatTimestamp(sa.last_snapshot_completed_at)}`);
        }
        if (typeof sa.last_snapshot_count === 'number') {
            details.push(`Letzte Nutzer: ${safeNumber(sa.last_snapshot_count)}`);
        }
        if (typeof sa.tracked_users === 'number') {
            details.push(`Gespeichert: ${safeNumber(sa.tracked_users)}`);
        }
        appendComponent('Statusanzeige', statusText, details);
    }

    if (components.presence_tracker) {
        const pt = components.presence_tracker;
        const statusText = pt.timer_active ? '✅ Timer aktiv' : '⏸️ Timer aus';
        const details = [];
        if (typeof pt.active_users === 'number') {
            details.push(`Aktive Nutzer: ${safeNumber(pt.active_users)}`);
        }
        if (typeof pt.pending_requests === 'number') {
            details.push(`Pending Requests: ${safeNumber(pt.pending_requests)}`);
        }
        if (pt.last_check_at) {
            details.push(`Letzter Check: ${formatTimestamp(pt.last_check_at)}`);
        }
        if (pt.interval_ms) {
            details.push(`Intervall: ${safeNumber(pt.interval_ms)} ms`);
        }
        appendComponent('Presence-Tracker', statusText, details);
    }

    if (componentSection.children.length > 1) {
        container.appendChild(componentSection);
    }
}

function renderGenericMetrics(container, metrics) {
    container.innerHTML = '';
    if (!metrics || Object.keys(metrics).length === 0) {
        const empty = document.createElement('div');
        empty.textContent = 'Keine Metriken verfügbar.';
        container.appendChild(empty);
        return;
    }
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(metrics, null, 2);
    container.appendChild(pre);
}

function renderStandalone(bots) {
    if (!standaloneContainer) {
        return;
    }
    isRefreshingStandalone = true;
    try {
        const existingCards = Array.from(standaloneContainer.querySelectorAll('.standalone-card'));
        existingCards.forEach((card) => {
            const cardKey = card.dataset.key;
            if (!cardKey) {
                return;
            }
            const details = card.querySelector('details.standalone-logs');
            const pre = details ? details.querySelector('pre.standalone-log-view') : null;
            const state = logOpenState.get(cardKey) || {};
            if (details) {
                state.open = details.open;
            }
            if (pre) {
                state.expanded = pre.classList.contains('expanded');
                state.scrollTop = pre.scrollTop;
            }
            logOpenState.set(cardKey, state);
        });

        standaloneContainer.innerHTML = '';
        if (!Array.isArray(bots) || bots.length === 0) {
            const empty = document.createElement('p');
            empty.className = 'standalone-meta';
            empty.textContent = 'Keine Standalone-Bots registriert.';
            standaloneContainer.appendChild(empty);
            return;
        }

        bots.forEach((info) => {
            const card = document.createElement('div');
            card.className = 'standalone-card';
            card.dataset.key = info.key;

        const namespace = (info.config && info.config.command_namespace) ? info.config.command_namespace : info.key;

        const header = document.createElement('div');
        header.className = 'standalone-header';

        const title = document.createElement('h3');
        title.textContent = (info.config && info.config.name) ? info.config.name : info.key;
        header.appendChild(title);

        const statusSpan = document.createElement('span');
        statusSpan.className = 'standalone-status';
        const dot = document.createElement('span');
        dot.className = 'status-dot ' + (info.running ? 'status-loaded' : 'status-error');
        statusSpan.appendChild(dot);
        statusSpan.appendChild(document.createTextNode(info.running ? 'Online' : 'Offline'));
        header.appendChild(statusSpan);
        card.appendChild(header);

        const meta = document.createElement('div');
        meta.className = 'standalone-meta';
        const metaParts = [];
        metaParts.push(`PID: ${info.pid || '–'}`);
        metaParts.push(`Uptime: ${formatSeconds(info.uptime_seconds)}`);
        metaParts.push(`Autostart: ${info.autostart ? 'Ja' : 'Nein'}`);
        meta.innerHTML = metaParts.join(' • ');
        const metrics = info.metrics || {};
        if (metrics.updated_at) {
            meta.innerHTML += `<br>Heartbeat: ${formatTimestamp(metrics.updated_at)}`;
        } else if (Number.isFinite(metrics.heartbeat)) {
            meta.innerHTML += `<br>Heartbeat: ${formatTimestamp(metrics.heartbeat * 1000)}`;
        } else if (metrics.state && metrics.state.timestamp) {
            meta.innerHTML += `<br>Heartbeat: ${formatTimestamp(metrics.state.timestamp)}`;
        }
        card.appendChild(meta);

        const actions = document.createElement('div');
        actions.className = 'standalone-actions';

        const startBtn = document.createElement('button');
        startBtn.className = 'load';
        startBtn.textContent = 'Start';
        startBtn.disabled = !!info.running;
        startBtn.addEventListener('click', () => controlStandalone(info.key, 'start'));
        actions.appendChild(startBtn);

        const stopBtn = document.createElement('button');
        stopBtn.className = 'unload';
        stopBtn.textContent = 'Stop';
        stopBtn.disabled = !info.running;
        stopBtn.addEventListener('click', () => controlStandalone(info.key, 'stop'));
        actions.appendChild(stopBtn);

        const restartBtn = document.createElement('button');
        restartBtn.className = 'reload';
        restartBtn.textContent = 'Neustart';
        restartBtn.addEventListener('click', () => controlStandalone(info.key, 'restart'));
        actions.appendChild(restartBtn);

        const autostartBtn = document.createElement('button');
        autostartBtn.className = 'autostart-toggle';
        autostartBtn.textContent = info.autostart ? 'Autostart deaktivieren' : 'Autostart aktivieren';
        autostartBtn.addEventListener('click', async () => {
            autostartBtn.disabled = true;
            const targetState = !info.autostart;
            try {
                await setStandaloneAutostart(info.key, targetState);
                log(`Standalone ${info.key}: Autostart ${targetState ? 'aktiviert' : 'deaktiviert'}`, 'success');
                loadStatus();
            } catch (err) {
                log(`Standalone ${info.key}: Autostart konnte nicht aktualisiert werden (${err.message})`, 'error');
            } finally {
                autostartBtn.disabled = false;
            }
        });
        actions.appendChild(autostartBtn);

        card.appendChild(actions);

        const metricsContainer = document.createElement('div');
        metricsContainer.className = 'standalone-metrics';
        if (namespace === 'rank') {
            renderRankMetrics(metricsContainer, metrics);
        } else if (namespace === 'steam') {
            renderSteamMetrics(metricsContainer, metrics);
        } else {
            renderGenericMetrics(metricsContainer, metrics);
        }

        card.appendChild(metricsContainer);

        const commandSection = document.createElement('div');
        commandSection.className = 'standalone-commands';

        const form = document.createElement('form');
        const select = document.createElement('select');
        const defaultOption = document.createElement('option');
        defaultOption.value = '';
        defaultOption.textContent = 'Aktion auswählen…';
        select.appendChild(defaultOption);
        const commandOptions = STANDALONE_COMMANDS[namespace] || [];
        commandOptions.forEach((cmd) => {
            const option = document.createElement('option');
            option.value = cmd.value;
            option.textContent = cmd.label;
            select.appendChild(option);
        });
        form.appendChild(select);

        const submitBtn = document.createElement('button');
        submitBtn.type = 'submit';
        submitBtn.textContent = 'Ausführen';
        submitBtn.className = 'reload';
        form.appendChild(submitBtn);

        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const value = select.value;
            if (!value) {
                return;
            }
            submitBtn.disabled = true;
            try {
                await sendStandaloneCommand(info.key, value);
                log(`Standalone ${info.key}: '${value}' gesendet`, 'success');
                select.value = '';
            } catch (err) {
                log(`Standalone ${info.key}: ${err.message}`, 'error');
            } finally {
                submitBtn.disabled = false;
            }
        });

        commandSection.appendChild(form);

        const pendingCommands = metrics.pending_commands || [];
        if (pendingCommands.length) {
            const title = document.createElement('strong');
            title.textContent = 'Wartende Befehle';
            commandSection.appendChild(title);
            const list = document.createElement('ul');
            list.className = 'standalone-list';
            pendingCommands.slice(0, 5).forEach((cmd) => {
                const li = document.createElement('li');
                li.innerHTML = `<span>${cmd.command}</span><span>${formatTimestamp(cmd.created_at)}</span>`;
                list.appendChild(li);
            });
            commandSection.appendChild(list);
        }

    const recentCommands = metrics.recent_commands || [];
    const recentTasks = Array.isArray(tasks.recent) ? tasks.recent : [];

    const toMilliseconds = (value) => {
        if (!value && value !== 0) {
            return null;
        }
        if (typeof value === 'number') {
            if (!Number.isFinite(value)) {
                return null;
            }
            return value < 1e12 ? value * 1000 : value;
        }
        const numeric = Number(value);
        if (Number.isFinite(numeric)) {
            return numeric < 1e12 ? numeric * 1000 : numeric;
        }
        const parsed = Date.parse(value);
        return Number.isNaN(parsed) ? null : parsed;
    };

    const recentEntries = [];

    recentCommands.slice(0, 10).forEach((cmd, index) => {
        const statusText = (cmd.status || 'unbekannt').toString();
        const timeValue = cmd.finished_at || cmd.created_at || Date.now();
        const timestamp = toMilliseconds(timeValue) ?? Date.now();
        recentEntries.push({
            type: 'command',
            label: cmd.command || `(unbekannt-${index})`,
            status: statusText,
            displayTime: toMilliseconds(timeValue) ?? timeValue,
            sortValue: timestamp,
        });
    });

    recentTasks.slice(0, 10).forEach((task) => {
        const statusText = (task.status || task.state || 'unbekannt').toString();
        const updatedValue = task.updated_at ?? task.finished_at ?? task.created_at ?? Date.now();
        const timestamp = toMilliseconds(updatedValue) ?? Date.now();
        recentEntries.push({
            type: 'task',
            label: `Task #${safeNumber(task.id)} ${task.type || '-'}`,
            status: statusText,
            displayTime: toMilliseconds(updatedValue) ?? updatedValue,
            sortValue: timestamp,
        });
    });

    if (recentEntries.length) {
        recentEntries.sort((a, b) => b.sortValue - a.sortValue);
        const title = document.createElement('strong');
        title.textContent = 'Letzte Befehle';
        commandSection.appendChild(title);
        const list = document.createElement('ul');
        list.className = 'standalone-list command-history';
        recentEntries.slice(0, 8).forEach((entry) => {
            const li = document.createElement('li');
            const main = document.createElement('div');
            main.className = 'standalone-list-main';

            const titleSpan = document.createElement('span');
            titleSpan.className = 'standalone-list-title';
            titleSpan.textContent = entry.label;
            main.appendChild(titleSpan);

            const statusPill = document.createElement('span');
            const normalized = entry.status.trim().toLowerCase();
            statusPill.className = 'status-pill';
            if (normalized) {
                const classSafe = normalized.replace(/[^a-z0-9]+/g, '-');
                statusPill.dataset.status = normalized;
                statusPill.classList.add(`status-${classSafe}`);
            }
            statusPill.textContent = entry.status ? entry.status.toUpperCase() : 'UNBEKANNT';
            main.appendChild(statusPill);

            if (entry.type === 'task') {
                const typePill = document.createElement('span');
                typePill.className = 'status-pill status-task';
                typePill.textContent = 'TASK';
                main.appendChild(typePill);
            }

            li.appendChild(main);

            const timeSpan = document.createElement('span');
            timeSpan.className = 'standalone-list-time';
            const displayValue = entry.displayTime ?? entry.sortValue;
            timeSpan.textContent = formatTimestamp(displayValue);
            li.appendChild(timeSpan);
            list.appendChild(li);
        });
        commandSection.appendChild(list);
    }

        card.appendChild(commandSection);

        const logsSection = document.createElement('details');
        logsSection.className = 'standalone-logs';
        const logsSummary = document.createElement('summary');
        logsSummary.textContent = 'Logs anzeigen';
        logsSection.appendChild(logsSummary);
        const logsControls = document.createElement('div');
        logsControls.className = 'logs-controls';
        logsControls.hidden = true;
        logsSection.appendChild(logsControls);
        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'log-expand';
        expandBtn.setAttribute('aria-label', 'Log vergroessern');
        expandBtn.setAttribute('title', 'Log vergroessern');
        expandBtn.innerHTML = '<span class="expand-icon" aria-hidden="true"></span>';
        logsControls.appendChild(expandBtn);
        const logsBody = document.createElement('pre');
        logsBody.className = 'standalone-log-view';
        logsBody.textContent = 'Oeffnen zum Laden.';
        logsSection.appendChild(logsBody);

        const storedLogState = logOpenState.get(info.key) || {};
        const syncExpandLabels = (expanded) => {
            expandBtn.setAttribute('title', expanded ? 'Log verkleinern' : 'Log vergroessern');
            expandBtn.setAttribute('aria-label', expanded ? 'Log verkleinern' : 'Log vergroessern');
        };

        if (storedLogState.expanded) {
            logsBody.classList.add('expanded');
            expandBtn.classList.add('expanded');
        }
        syncExpandLabels(Boolean(storedLogState.expanded));

        expandBtn.addEventListener('click', () => {
            const expanded = !logsBody.classList.contains('expanded');
            logsBody.classList.toggle('expanded', expanded);
            expandBtn.classList.toggle('expanded', expanded);
            syncExpandLabels(expanded);
            const state = logOpenState.get(info.key) || {};
            state.expanded = expanded;
            state.scrollTop = logsBody.scrollTop;
            logOpenState.set(info.key, state);
        });

        let logsLoading = false;
        const loadLogs = async () => {
            if (logsLoading) {
                return;
            }
            logsLoading = true;
            logsBody.textContent = 'Lade Logs...';
            try {
                const data = await fetchStandaloneLogs(info.key, 200);
                const entries = Array.isArray(data.logs) ? data.logs : [];
                if (!entries.length) {
                    logsBody.textContent = 'Keine Logeintraege verfuegbar.';
                } else {
                    const lines = entries.map((entry) => {
                        const ts = entry.ts ? formatTimestamp(entry.ts) : '-';
                        const stream = entry.stream ? `[${entry.stream}]` : '';
                        const line = entry.line || '';
                        return `${ts} ${stream} ${line}`.trim();
                    });
                    logsBody.textContent = lines.join('\n');
                }
                const state = logOpenState.get(info.key) || {};
                if (typeof state.scrollTop === 'number') {
                    logsBody.scrollTop = state.scrollTop;
                }
            } catch (err) {
                logsBody.textContent = `Fehler beim Laden: ${err.message}`;
            } finally {
                logsLoading = false;
            }
        };

        logsSection.addEventListener('toggle', () => {
            if (!logsSection.open && isRefreshingStandalone) {
                return;
            }
            logsControls.hidden = !logsSection.open;
            const state = logOpenState.get(info.key) || {};
            state.open = logsSection.open;
            state.scrollTop = logsBody.scrollTop;
            state.expanded = logsBody.classList.contains('expanded');
            logOpenState.set(info.key, state);
            if (!logsSection.open) {
                return;
            }
            loadLogs();
        });

        logsBody.addEventListener('scroll', () => {
            if (!logsSection.open) {
                return;
            }
            const state = logOpenState.get(info.key) || {};
            state.scrollTop = logsBody.scrollTop;
            logOpenState.set(info.key, state);
        });

        if (storedLogState.open) {
            logsSection.open = true;
            logsControls.hidden = false;
            loadLogs();
        }

        card.appendChild(logsSection);

            standaloneContainer.appendChild(card);
        });
    } finally {
        isRefreshingStandalone = false;
    }
}

async function controlStandalone(key, action) {
    const endpoint = `/api/standalone/${key}/${action}`;
    try {
        await fetchJSON(endpoint, { method: 'POST', body: JSON.stringify({}) });
        log(`Standalone ${key}: ${action} ausgeführt`, 'success');
        loadStatus();
    } catch (err) {
        log(`Standalone ${key}: ${action} fehlgeschlagen (${err.message})`, 'error');
    }
}

async function sendStandaloneCommand(key, command) {
    await fetchJSON(`/api/standalone/${key}/command`, {
        method: 'POST',
        body: JSON.stringify({ command }),
    });
    loadStatus();
}

async function setStandaloneAutostart(key, enabled) {
    return fetchJSON(`/api/standalone/${key}/autostart`, {
        method: 'POST',
        body: JSON.stringify({ enabled: Boolean(enabled) }),
    });
}

async function fetchStandaloneLogs(key, limit = 200) {
    const params = new URLSearchParams({ limit: String(limit) });
    return fetchJSON(`/api/standalone/${key}/logs?${params.toString()}`);
}

    function buildTreeNode(node, depth = 0) {
        const nodePath = getNodePath(node);
        const nodeType = node.type || (Array.isArray(node.children) ? 'directory' : 'module');
        if (isDirectoryLike(nodeType)) {
            const details = document.createElement('details');
            details.className = 'directory tree-node';
            if (depth < 2) {
                details.open = true;
            }
            details.dataset.path = nodePath;
            details.dataset.nodeType = nodeType;
            const summary = document.createElement('summary');
            const label = document.createElement('div');
            label.className = 'tree-label';
            const title = document.createElement('span');
            title.textContent = node.name;
            label.appendChild(title);
            if (node.is_package) {
                label.appendChild(createTag('package', 'package'));
            }
            if (node.module_count > 0) {
                label.appendChild(createTag(node.loaded_count + '/' + node.module_count + ' geladen', 'count'));
            }
            if (node.module_count > node.discovered_count) {
                const hidden = node.module_count - node.discovered_count;
                label.appendChild(createTag(hidden + ' versteckt', 'partial'));
            }
            if (node.blocked) {
                label.appendChild(createTag('blockiert', 'blocked'));
            }
            if (node.status) {
                label.appendChild(renderStatus(node.status));
            }

            const actions = document.createElement('div');
            actions.className = 'tree-actions';
            if (node.manageable) {
                const reloadBtn = document.createElement('button');
                reloadBtn.textContent = 'Reload';
                reloadBtn.className = 'reload';
                reloadBtn.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    reloadPath(node.path);
                });
                actions.appendChild(reloadBtn);

                if (!node.blocked) {
                    const loadBtn = document.createElement('button');
                    loadBtn.textContent = 'Load';
                    loadBtn.className = 'load';
                    if (node.loaded) {
                        loadBtn.disabled = true;
                    }
                    loadBtn.addEventListener('click', (ev) => {
                        ev.preventDefault();
                        ev.stopPropagation();
                        loadCog(node.path);
                    });
                    actions.appendChild(loadBtn);

                    const unloadBtn = document.createElement('button');
                    unloadBtn.textContent = 'Unload';
                    unloadBtn.className = 'unload';
                    if (!node.loaded) {
                        unloadBtn.disabled = true;
                    }
                    unloadBtn.addEventListener('click', (ev) => {
                        ev.preventDefault();
                        ev.stopPropagation();
                        unloadCog(node.path);
                    });
                    actions.appendChild(unloadBtn);
                }
            }

            const blockBtn = document.createElement('button');
            if (node.blocked) {
                blockBtn.textContent = 'Unblock';
                blockBtn.className = 'unblock';
                blockBtn.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    unblockPath(node.path);
                });
            } else {
                blockBtn.textContent = 'Block';
                blockBtn.className = 'block';
                blockBtn.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    blockPath(node.path);
                });
            }
            actions.appendChild(blockBtn);

            summary.appendChild(label);
            summary.appendChild(actions);
            details.appendChild(summary);

            summary.addEventListener('click', (ev) => {
                if (ev.target.closest('button')) {
                    return;
                }
                selectNode(node);
            });

            const childrenContainer = document.createElement('div');
            childrenContainer.className = 'tree-children';
            if (node.children && node.children.length) {
                for (const child of node.children) {
                    childrenContainer.appendChild(buildTreeNode(child, depth + 1));
                }
            } else {
                const empty = document.createElement('div');
                empty.className = 'tree-empty';
                empty.textContent = 'Keine Einträge';
                childrenContainer.appendChild(empty);
            }
            details.appendChild(childrenContainer);
            details.title = node.path;
            return details;
        }

        const leaf = document.createElement('div');
        leaf.className = 'tree-leaf';
        leaf.dataset.path = nodePath;
        leaf.dataset.nodeType = nodeType;
        const meta = document.createElement('div');
        meta.className = 'leaf-meta';
        const title = document.createElement('span');
        title.textContent = node.name;
        meta.appendChild(title);
        if (node.blocked) {
            meta.appendChild(createTag('blockiert', 'blocked'));
        }
        if (!node.discovered) {
            meta.appendChild(createTag('nicht entdeckt', 'partial'));
        }
        if (!node.manageable) {
            meta.appendChild(createTag('intern', 'managed'));
        }
        if (node.status) {
            meta.appendChild(renderStatus(node.status));
        }
        leaf.appendChild(meta);

        const actions = document.createElement('div');
        actions.className = 'tree-actions';
        if (node.manageable && !node.blocked) {
            const reloadBtn = document.createElement('button');
            reloadBtn.textContent = 'Reload';
            reloadBtn.className = 'reload';
            reloadBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                reloadCog(node.path);
            });
            actions.appendChild(reloadBtn);

            const loadBtn = document.createElement('button');
            loadBtn.textContent = 'Load';
            loadBtn.className = 'load';
            if (node.loaded) {
                loadBtn.disabled = true;
            }
            loadBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                loadCog(node.path);
            });
            actions.appendChild(loadBtn);

            const unloadBtn = document.createElement('button');
            unloadBtn.textContent = 'Unload';
            unloadBtn.className = 'unload';
            if (!node.loaded) {
                unloadBtn.disabled = true;
            }
            unloadBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                unloadCog(node.path);
            });
            actions.appendChild(unloadBtn);
        } else if (!node.manageable) {
            const info = document.createElement('span');
            info.className = 'managed-info';
            info.textContent = 'Nur über Eltern-Cog verwaltbar';
            actions.appendChild(info);
        }
        const blockBtn = document.createElement('button');
        if (node.blocked) {
            blockBtn.textContent = 'Unblock';
            blockBtn.className = 'unblock';
            blockBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                unblockPath(node.path);
            });
        } else {
            blockBtn.textContent = 'Block';
            blockBtn.className = 'block';
            blockBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                blockPath(node.path);
            });
        }
        actions.appendChild(blockBtn);
        leaf.appendChild(actions);
        leaf.title = node.path;
        leaf.addEventListener('click', (ev) => {
            if (ev.target.closest('button')) {
                return;
            }
            selectNode(node);
        });
        return leaf;
    }

    async function reloadCog(name) {
        try {
            const res = await fetchJSON('/api/cogs/reload', {
                method: 'POST',
                body: JSON.stringify({ names: [name] }),
            });
            const result = res.results[name];
            log(result.message, result.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Reload failed: ' + err.message, 'error');
        }
    }

    async function loadCog(name) {
        try {
            const res = await fetchJSON('/api/cogs/load', {
                method: 'POST',
                body: JSON.stringify({ names: [name] }),
            });
            const result = res.results[name];
            log(result.message, result.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Load failed: ' + err.message, 'error');
        }
    }

    async function unloadCog(name) {
        try {
            const res = await fetchJSON('/api/cogs/unload', {
                method: 'POST',
                body: JSON.stringify({ names: [name] }),
            });
            const result = res.results[name];
            log(result.message, result.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Unload failed: ' + err.message, 'error');
        }
    }

    async function reloadPath(namespace) {
        try {
            const res = await fetchJSON('/api/cogs/reload-namespace', {
                method: 'POST',
                body: JSON.stringify({ namespace }),
            });
            log(res.message, res.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Namespace reload failed: ' + err.message, 'error');
        }
    }

    async function blockPath(path) {
        try {
            const res = await fetchJSON('/api/cogs/block', {
                method: 'POST',
                body: JSON.stringify({ path }),
            });
            log(res.message, res.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Block failed: ' + err.message, 'error');
        }
    }

    async function unblockPath(path) {
        try {
            const res = await fetchJSON('/api/cogs/unblock', {
                method: 'POST',
                body: JSON.stringify({ path }),
            });
            log(res.message, res.ok ? 'success' : 'error');
            await loadStatus();
        } catch (err) {
            log('Unblock failed: ' + err.message, 'error');
        }
    }

    if (resetSelectionBtn) {
        resetSelectionBtn.addEventListener('click', () => {
            clearSelection();
        });
    }

    document.getElementById('apply-token').addEventListener('click', () => {
        authToken = tokenInput.value.trim();
        localStorage.setItem('master-dashboard-token', authToken);
        loadStatus();
    });

    document.getElementById('reload-all').addEventListener('click', async () => {
        try {
            const res = await fetchJSON('/api/cogs/reload-all', { method: 'POST', body: JSON.stringify({}) });
            log('Reload all completed: ' + res.summary.loaded + '/' + res.summary.discovered + ' loaded', 'success');
            await loadStatus();
        } catch (err) {
            log('Reload all failed: ' + err.message, 'error');
        }
    });

    document.getElementById('discover').addEventListener('click', async () => {
        try {
            const res = await fetchJSON('/api/cogs/discover', { method: 'POST', body: JSON.stringify({}) });
            log('Discovery: +' + res.new.length + ' new cogs', 'success');
            await loadStatus();
        } catch (err) {
            log('Discovery failed: ' + err.message, 'error');
        }
    });

    loadStatus();
    setInterval(loadStatus, 15000);