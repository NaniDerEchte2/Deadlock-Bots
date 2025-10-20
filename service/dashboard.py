from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from aiohttp import web


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>Master Bot Dashboard</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <style>
        :root {
            color-scheme: dark light;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;
        }
        body {
            margin: 0 auto;
            padding: 1.5rem;
            max-width: 1100px;
            background: #111;
            color: #f5f5f5;
        }
        h1, h2 {
            font-weight: 600;
        }
        section {
            margin-bottom: 2rem;
            background: #1c1c1c;
            border-radius: 10px;
            padding: 1.5rem;
            box-shadow: 0 6px 24px rgba(0,0,0,0.35);
        }
        .top-nav {
            display: flex;
            justify-content: flex-end;
            margin-bottom: 1rem;
        }
        .top-nav a {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            text-decoration: none;
            background: #1c7ed6;
            color: #fff;
            padding: 0.45rem 0.9rem;
            border-radius: 999px;
            font-weight: 600;
            border: 1px solid rgba(255,255,255,0.2);
            transition: background 0.15s ease;
        }
        .top-nav a:hover {
            background: #1971c2;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 1rem;
        }
        .cog-management {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }
        .cog-management .card {
            height: 100%;
        }
        .card h3 {
            margin-top: 0;
        }
        .card {
            background: #161616;
            border-radius: 8px;
            padding: 1rem;
            border: 1px solid rgba(255,255,255,0.05);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        th, td {
            padding: 0.55rem 0.75rem;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        tbody tr:hover {
            background: rgba(255,255,255,0.04);
        }
        button {
            border: none;
            border-radius: 6px;
            padding: 0.35rem 0.7rem;
            margin-right: 0.4rem;
            margin-bottom: 0.2rem;
            cursor: pointer;
            font-weight: 600;
        }
        button.reload { background: #1c7ed6; color: #fff; }
        button.unload { background: #e8590c; color: #fff; }
        button.load { background: #37b24d; color: #fff; }
        button.namespace { background: #7048e8; color: #fff; }
        button.block { background: #c92a2a; color: #fff; }
        button.unblock { background: #66d9e8; color: #111; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .status-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 0.35rem;
        }
        .status-loaded { background: #37b24d; }
        .status-error { background: #e03131; }
        .status-unloaded { background: #fab005; }
        .status-unknown { background: #868e96; }
        .status-blocked { background: #e8590c; }
        .toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            align-items: center;
        }
        .toolbar input {
            padding: 0.45rem 0.65rem;
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.1);
            background: #1f1f1f;
            color: inherit;
        }
        .log {
            font-family: ui-monospace, SFMono-Regular, SFMono, Menlo, Monaco, Consolas, \"Liberation Mono\", monospace;
            font-size: 0.85rem;
            background: #101010;
            padding: 0.75rem;
            border-radius: 8px;
            max-height: 240px;
            overflow-y: auto;
        }
        .tree-container {
            max-height: 420px;
            overflow-y: auto;
            padding-right: 0.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }
        details.directory {
            background: #141414;
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 8px;
            padding: 0.35rem 0.6rem;
        }
        details.directory > summary {
            list-style: none;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            cursor: pointer;
            font-weight: 600;
        }
        details.directory > summary::-webkit-details-marker {
            display: none;
        }
        .tree-label {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
        }
        .tree-actions {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            flex-wrap: wrap;
        }
        .tree-children {
            margin-left: 1rem;
            margin-top: 0.4rem;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .tree-leaf {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            background: #101010;
            padding: 0.35rem 0.5rem;
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .tree-leaf .leaf-meta {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            flex-wrap: wrap;
        }
        .tree-empty {
            font-style: italic;
            color: #868e96;
            padding-left: 0.5rem;
        }
        .tag {
            display: inline-flex;
            align-items: center;
            padding: 0.1rem 0.5rem;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 600;
            background: rgba(255,255,255,0.12);
            color: #f5f5f5;
        }
        .tag.blocked { background: rgba(233, 30, 99, 0.25); color: #ff8787; }
        .tag.count { background: rgba(64, 192, 87, 0.18); color: #c0ffc0; }
        .tag.partial { background: rgba(250, 176, 5, 0.25); color: #ffd43b; }
        .tag.package { background: rgba(112, 72, 232, 0.2); color: #d0bfff; }
        .error { color: #ff8787; }
        .success { color: #69db7c; }
    </style>
</head>
<body>
    <div class="top-nav">
        <a href="{{TWITCH_URL}}" target="_blank" rel="noopener">Twitch Dashboard öffnen</a>
    </div>
    <h1>Master Bot Dashboard</h1>
    <section>
        <div class=\"toolbar\">
            <div>
                <strong>Auth Token:</strong>
                <input id=\"token-input\" type=\"password\" placeholder=\"Bearer Token\">
                <button id=\"apply-token\">Apply</button>
            </div>
            <div>
                <button class=\"namespace\" id=\"reload-all\">Reload &amp; Discover All</button>
                <button class=\"namespace\" id=\"discover\">Discover Cogs</button>
            </div>
        </div>
        <div class=\"grid\" style=\"margin-top:1rem;\">
            <div class=\"card\">
                <h2>Bot</h2>
                <p id=\"bot-user\">-</p>
                <p id=\"bot-uptime\">-</p>
                <p id=\"bot-guilds\">-</p>
                <p id=\"bot-latency\">-</p>
            </div>
        </div>
    </section>

    <section>
        <h2>Cog Management</h2>
        <div class=\"cog-management\">
            <div class=\"card\">
                <h3>Cog Explorer</h3>
                <div id=\"tree-container\" class=\"tree-container\"></div>
            </div>
            <div class=\"card\">
                <h3>Cogs</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Status</th>
                            <th>Namespace</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id=\"cog-table\"></tbody>
                </table>
            </div>
        </div>
    </section>

    <section>
        <h2>Recent Operations</h2>
        <div class=\"log\" id=\"operation-log\"></div>
    </section>

    <script>
    const opLog = document.getElementById('operation-log');
    const tableBody = document.getElementById('cog-table');
    const treeContainer = document.getElementById('tree-container');
    const tokenInput = document.getElementById('token-input');
    let authToken = localStorage.getItem('master-dashboard-token') || '';
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

    async function loadStatus() {
        try {
            const data = await fetchJSON('/api/status');
            document.getElementById('bot-user').textContent = data.bot.user || 'Unbekannt';
            document.getElementById('bot-uptime').textContent = 'Uptime: ' + data.bot.uptime;
            document.getElementById('bot-guilds').textContent = 'Guilds: ' + data.bot.guilds;
            document.getElementById('bot-latency').textContent = 'Latency: ' + data.bot.latency_ms + ' ms';

            tableBody.innerHTML = '';
            for (const cog of data.cogs.items) {
                const tr = document.createElement('tr');
                const nameTd = document.createElement('td');
                nameTd.textContent = cog.name;
                tr.appendChild(nameTd);

                const statusTd = document.createElement('td');
                statusTd.appendChild(renderStatus(cog.status));
                tr.appendChild(statusTd);

                const nsTd = document.createElement('td');
                nsTd.textContent = cog.namespace;
                tr.appendChild(nsTd);

                const actionTd = document.createElement('td');
                const reloadBtn = document.createElement('button');
                reloadBtn.textContent = 'Reload';
                reloadBtn.className = 'reload';
                reloadBtn.addEventListener('click', () => reloadCog(cog.name));
                actionTd.appendChild(reloadBtn);

                const unloadBtn = document.createElement('button');
                unloadBtn.textContent = 'Unload';
                unloadBtn.className = 'unload';
                unloadBtn.addEventListener('click', () => unloadCog(cog.name));
                actionTd.appendChild(unloadBtn);

                if (!cog.loaded) {
                    unloadBtn.disabled = true;
                }

                const loadBtn = document.createElement('button');
                loadBtn.textContent = 'Load';
                loadBtn.className = 'load';
                loadBtn.addEventListener('click', () => loadCog(cog.name));
                actionTd.appendChild(loadBtn);
                if (cog.loaded) {
                    loadBtn.disabled = true;
                }

                tr.appendChild(actionTd);
                tableBody.appendChild(tr);
            }

            renderTree(data.cogs.tree);
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
        treeContainer.appendChild(buildTreeNode(root, 0));
    }

    function buildTreeNode(node, depth = 0) {
        if (node.type === 'directory') {
            const details = document.createElement('details');
            details.className = 'directory tree-node';
            if (depth < 2) {
                details.open = true;
            }
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
            const reloadBtn = document.createElement('button');
            reloadBtn.textContent = 'Reload';
            reloadBtn.className = 'reload';
            reloadBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                reloadPath(node.path);
            });
            actions.appendChild(reloadBtn);

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
        if (node.status) {
            meta.appendChild(renderStatus(node.status));
        }
        leaf.appendChild(meta);

        const actions = document.createElement('div');
        actions.className = 'tree-actions';
        if (!node.blocked) {
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
    </script>
</body>
</html>
"""


class DashboardServer:
    """Simple aiohttp based dashboard for managing the master bot."""

    def __init__(
        self,
        bot: "MasterBot",
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        token: Optional[str] = None,
    ) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.token = token or os.getenv("MASTER_DASHBOARD_TOKEN")
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock = asyncio.Lock()
        self._started = False
        scheme_env = (os.getenv("MASTER_DASHBOARD_SCHEME") or "").strip().lower()
        self._scheme = scheme_env or "http"
        self._listen_base_url = self._format_base_url(self.host, self.port, self._scheme)
        public_env = (os.getenv("MASTER_DASHBOARD_PUBLIC_URL") or "").strip()
        if public_env:
            try:
                self._public_base_url = self._normalize_public_url(
                    public_env,
                    default_scheme=self._scheme,
                )
            except Exception as exc:
                logging.warning(
                    "MASTER_DASHBOARD_PUBLIC_URL '%s' invalid (%s) – falling back to listen URL",
                    public_env,
                    exc,
                )
                self._public_base_url = self._listen_base_url
        else:
            self._public_base_url = self._listen_base_url

        self._twitch_dashboard_href = self._resolve_twitch_dashboard_href()

    async def _cleanup(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return

            app = web.Application()
            app["dashboard"] = self
            app.add_routes(
                [
                    web.get("/", self._handle_index),
                    web.get("/admin", self._handle_index),
                    web.get("/api/status", self._handle_status),
                    web.post("/api/cogs/reload", self._handle_reload),
                    web.post("/api/cogs/load", self._handle_load),
                    web.post("/api/cogs/unload", self._handle_unload),
                    web.post("/api/cogs/reload-all", self._handle_reload_all),
                    web.post("/api/cogs/reload-namespace", self._handle_reload_namespace),
                    web.post("/api/cogs/block", self._handle_block),
                    web.post("/api/cogs/unblock", self._handle_unblock),
                    web.post("/api/cogs/discover", self._handle_discover),
                ]
            )

            addr_in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)}
            win_access = {getattr(errno, "WSAEACCES", 10013), errno.EACCES}

            async def _start_with(reuse_address: Optional[bool]) -> str:
                runner = web.AppRunner(app)
                await runner.setup()

                site_kwargs: Dict[str, Any] = {}
                if reuse_address:
                    site_kwargs["reuse_address"] = True

                try:
                    site = web.TCPSite(runner, self.host, self.port, **site_kwargs)
                    await site.start()
                except OSError as e:
                    await runner.cleanup()
                    if reuse_address and os.name == "nt" and e.errno in win_access:
                        logging.warning(
                            "reuse_address konnte auf Windows nicht aktiviert werden (%s). "
                            "Starte Dashboard ohne reuse_address.",
                            e,
                        )
                        return "retry_without_reuse"
                    if e.errno in addr_in_use:
                        return "addr_in_use"
                    raise

                self._runner = runner
                self._site = site
                return "started"

            async def _start_without_reuse_with_retries() -> None:
                retries = 3
                delay = 0.5
                for attempt in range(retries):
                    attempt_result = await _start_with(reuse_address=False)
                    if attempt_result == "started":
                        return
                    if attempt_result == "addr_in_use" and attempt < retries - 1:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if attempt_result == "addr_in_use":
                        raise RuntimeError(
                            f"Dashboard-Port {self.host}:{self.port} ist bereits belegt"
                        )
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")
                raise RuntimeError("Dashboard konnte nicht gestartet werden")

            if os.name != "nt":
                result = await _start_with(reuse_address=True)
                if result == "addr_in_use":
                    raise RuntimeError(
                        f"Dashboard-Port {self.host}:{self.port} ist bereits belegt"
                    )
                if result != "started":
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")
            else:
                result = await _start_with(reuse_address=True)
                if result == "started":
                    pass
                elif result == "retry_without_reuse":
                    await _start_without_reuse_with_retries()
                elif result == "addr_in_use":
                    # reuse_address hat trotzdem einen Konflikt ausgelöst – wir warten
                    # kurz und versuchen den Start ohne reuse_address erneut.
                    await asyncio.sleep(0.5)
                    await _start_without_reuse_with_retries()
                else:
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")

            self._started = True
            base_no_slash = self._public_base_url.rstrip("/")
            if base_no_slash.lower().endswith("/admin"):
                admin_path = base_no_slash
            else:
                admin_path = base_no_slash + "/admin"
            logging.info("Master dashboard listening on %s", self._listen_base_url)
            if self._public_base_url != self._listen_base_url:
                logging.info("Master dashboard public URL set to %s", self._public_base_url)
            logging.info("Master dashboard admin UI: %s", admin_path)

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            try:
                await self._cleanup()
            finally:
                self._started = False
                logging.info("Master dashboard stopped")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_auth(self, request: web.Request, *, required: bool = True) -> None:
        if not self.token:
            return
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            provided = header.split(" ", 1)[1]
        else:
            provided = header
        if not provided:
            provided = request.query.get("token", "")
        if provided != self.token:
            if required:
                raise web.HTTPUnauthorized(text="Missing or invalid dashboard token", headers={"WWW-Authenticate": "Bearer"})
            raise web.HTTPUnauthorized(text="Missing or invalid dashboard token", headers={"WWW-Authenticate": "Bearer"})

    def _normalize_names(self, items: Iterable[str]) -> List[str]:
        normalized: List[str] = []
        for raw in items:
            resolved, matches = self.bot.resolve_cog_identifier(raw)
            if resolved:
                normalized.append(resolved)
                continue
            if matches:
                raise web.HTTPBadRequest(text=f"Identifier '{raw}' is ambiguous: {', '.join(matches)}")
            raise web.HTTPBadRequest(text=f"Cog '{raw}' not found")
        return normalized

    @staticmethod
    def _format_netloc(host: str, port: Optional[int], scheme: str) -> str:
        safe_host = host.strip() or "127.0.0.1"
        if ":" in safe_host and not (safe_host.startswith("[") and safe_host.endswith("]")):
            safe_host = f"[{safe_host}]"
        default_ports = {"http": 80, "https": 443}
        default_port = default_ports.get(scheme, None)
        if port is None or (default_port is not None and port == default_port):
            return safe_host
        return f"{safe_host}:{port}"

    @staticmethod
    def _format_base_url(host: str, port: Optional[int], scheme: str) -> str:
        netloc = DashboardServer._format_netloc(host, port, scheme)
        return urlunparse((scheme, netloc, "", "", "", ""))

    @staticmethod
    def _normalize_public_url(value: str, *, default_scheme: str) -> str:
        raw = value.strip()
        if not raw:
            raise ValueError("Dashboard public URL must not be empty")
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            try:
                parsed_port: Optional[int] = parsed.port
            except ValueError:
                parsed_port = None
            netloc = DashboardServer._format_netloc(
                parsed.hostname or parsed.netloc,
                parsed_port,
                parsed.scheme,
            )
            path = parsed.path.rstrip("/")
            return urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))

        if parsed.netloc and not parsed.scheme:
            scheme = default_scheme
            try:
                parsed_port = parsed.port
            except ValueError:
                parsed_port = None
            netloc = DashboardServer._format_netloc(parsed.hostname or parsed.netloc, parsed_port, scheme)
            path = parsed.path.rstrip("/")
            return urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))

        fallback = urlparse(f"{default_scheme}://{raw}")
        try:
            fallback_port = fallback.port
        except ValueError:
            fallback_port = None
        netloc = DashboardServer._format_netloc(
            fallback.hostname or fallback.netloc or fallback.path,
            fallback_port,
            fallback.scheme,
        )
        path = fallback.path.rstrip("/")
        return urlunparse(
            (fallback.scheme, netloc, path, fallback.params, fallback.query, fallback.fragment)
        )

    def _resolve_twitch_dashboard_href(self) -> str:
        explicit = (
            os.getenv("MASTER_TWITCH_DASHBOARD_URL")
            or os.getenv("TWITCH_DASHBOARD_URL")
            or ""
        ).strip()
        if explicit:
            try:
                return self._normalize_public_url(explicit, default_scheme=self._scheme)
            except Exception as exc:
                logging.warning(
                    "Twitch dashboard URL '%s' invalid (%s) – falling back to derived host/port",
                    explicit,
                    exc,
                )

        host = (os.getenv("TWITCH_DASHBOARD_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        scheme = (os.getenv("TWITCH_DASHBOARD_SCHEME") or self._scheme).strip() or self._scheme
        port_value = (os.getenv("TWITCH_DASHBOARD_PORT") or "").strip()
        port: Optional[int] = None
        if port_value:
            try:
                port = int(port_value)
            except ValueError:
                logging.warning(
                    "TWITCH_DASHBOARD_PORT '%s' invalid – using default 8765",
                    port_value,
                )
        if port is None:
            port = 8765

        base = self._format_base_url(host, port, scheme)
        return f"{base.rstrip('/')}/twitch"

    async def _handle_index(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        html_text = _HTML_TEMPLATE.replace("{{TWITCH_URL}}", self._twitch_dashboard_href)
        return web.Response(text=html_text, content_type="text/html")

    async def _handle_status(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))

        bot = self.bot
        tz = bot.startup_time.tzinfo
        now = _dt.datetime.now(tz=tz) if tz else _dt.datetime.now()
        uptime_delta = now - bot.startup_time
        uptime = str(uptime_delta).split(".")[0]

        discovered = bot.cogs_list
        status_map = bot.cog_status.copy()
        active = set(bot.active_cogs())

        items: List[Dict[str, Any]] = []
        for cog in discovered:
            status = status_map.get(cog, "loaded" if cog in active else "unloaded")
            items.append(
                {
                    "name": cog,
                    "status": status,
                    "loaded": cog in active,
                    "namespace": self._namespace_for(cog),
                }
            )

        namespaces = self._namespace_summary(discovered)

        payload = {
            "bot": {
                "user": str(bot.user) if bot.user else None,
                "id": getattr(bot.user, "id", None),
                "uptime": uptime,
                "guilds": len(bot.guilds),
                "latency_ms": round(bot.latency * 1000, 2) if bot.latency is not None else None,
            },
            "cogs": {
                "items": items,
                "active": sorted(active),
                "namespaces": namespaces,
                "discovered": discovered,
                "tree": self._build_tree(),
                "blocked": sorted(self.bot.blocked_namespaces),
            },
            "dashboard": {
                "listen_url": self._listen_base_url,
                "public_url": self._public_base_url,
            },
            "settings": {
                "per_cog_unload_timeout": bot.per_cog_unload_timeout,
            },
        }
        return web.json_response(payload)

    def _namespace_for(self, module: str) -> str:
        parts = module.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:3])
        if len(parts) >= 2:
            return ".".join(parts[:2])
        return module

    def _namespace_summary(self, modules: Iterable[str]) -> List[Dict[str, Any]]:
        counter: Dict[str, int] = {}
        for mod in modules:
            ns = self._namespace_for(mod)
            counter[ns] = counter.get(ns, 0) + 1
        return [
            {"namespace": ns, "count": counter[ns]}
            for ns in sorted(counter.keys())
        ]

    def _build_tree(self) -> Dict[str, Any]:
        bot = self.bot
        root_dir = bot.cogs_dir
        active = set(bot.active_cogs())
        discovered = set(bot.cogs_list)
        status_map = bot.cog_status.copy()

        def node_status(path: str, *, blocked: bool) -> Optional[str]:
            status = status_map.get(path)
            if status:
                return status
            if blocked:
                return "blocked"
            if path in active:
                return "loaded"
            if path in discovered:
                return "unloaded"
            return None

        def walk(directory: Path, parts: List[str]) -> Dict[str, Any]:
            module_path = "cogs"
            if parts:
                module_path = "cogs." + ".".join(parts)

            blocked_dir = bot.is_namespace_blocked(module_path, assume_normalized=True)
            status = node_status(module_path, blocked=blocked_dir)
            is_package = (
                module_path in discovered
                or module_path in status_map
                or module_path in active
            ) and module_path != "cogs"

            module_count = 1 if is_package else 0
            loaded_count = 1 if is_package and module_path in active else 0
            discovered_count = 1 if is_package and module_path in discovered else 0

            children: List[Dict[str, Any]] = []
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
            except FileNotFoundError:
                entries = []

            for entry in entries:
                if entry.name.startswith("__pycache__"):
                    continue
                if entry.is_dir():
                    child = walk(entry, parts + [entry.name])
                    children.append(child)
                    module_count += child.get("module_count", 0)
                    loaded_count += child.get("loaded_count", 0)
                    discovered_count += child.get("discovered_count", 0)
                    continue
                if entry.suffix != ".py" or entry.name == "__init__.py":
                    continue
                if parts:
                    mod_path = "cogs." + ".".join(parts + [entry.stem])
                else:
                    mod_path = f"cogs.{entry.stem}"
                blocked_child = bot.is_namespace_blocked(mod_path, assume_normalized=True)
                loaded_child = mod_path in active
                discovered_child = mod_path in discovered
                status_child = node_status(mod_path, blocked=blocked_child) or "not_discovered"
                child = {
                    "type": "module",
                    "name": entry.stem,
                    "path": mod_path,
                    "blocked": blocked_child,
                    "loaded": loaded_child,
                    "discovered": discovered_child,
                    "status": status_child,
                }
                children.append(child)
                module_count += 1
                if loaded_child:
                    loaded_count += 1
                if discovered_child:
                    discovered_count += 1

            return {
                "type": "directory",
                "name": directory.name if parts else "cogs",
                "path": module_path,
                "blocked": blocked_dir,
                "status": status,
                "is_package": is_package,
                "module_count": module_count,
                "loaded_count": loaded_count,
                "discovered_count": discovered_count,
                "children": children,
            }

        if not root_dir.exists():
            return {
                "type": "directory",
                "name": "cogs",
                "path": "cogs",
                "blocked": bot.is_namespace_blocked("cogs"),
                "status": None,
                "is_package": False,
                "module_count": 0,
                "loaded_count": 0,
                "discovered_count": 0,
                "children": [],
            }

        return walk(root_dir, [])

    async def _handle_reload(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        normalized = self._normalize_names(names)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            for name in normalized:
                if self.bot.is_namespace_blocked(name, assume_normalized=True):
                    results[name] = {
                        "ok": False,
                        "message": f"🚫 {name} ist blockiert",
                    }
                    continue
                if name not in self.bot.extensions:
                    results[name] = {
                        "ok": False,
                        "message": f"{name} is not loaded",
                    }
                    continue
                ok, message = await self.bot.reload_cog(name)
                results[name] = {"ok": ok, "message": message}
        return web.json_response({"results": results})

    async def _handle_load(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        self.bot.auto_discover_cogs()
        normalized = self._normalize_names(names)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            for name in normalized:
                if self.bot.is_namespace_blocked(name, assume_normalized=True):
                    results[name] = {
                        "ok": False,
                        "message": f"🚫 {name} ist blockiert",
                    }
                    continue
                ok, message = await self.bot.reload_cog(name)
                results[name] = {"ok": ok, "message": message}
        return web.json_response({"results": results})

    async def _handle_unload(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        normalized = self._normalize_names(names)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            unload_result = await self.bot.unload_many(normalized)
            for name in normalized:
                status = unload_result.get(name, "unknown")
                if status == "unloaded":
                    results[name] = {"ok": True, "message": f"✅ Unloaded {name}"}
                elif status == "timeout":
                    results[name] = {"ok": False, "message": f"⏱️ Timeout unloading {name}"}
                elif status.startswith("error"):
                    results[name] = {"ok": False, "message": status}
                else:
                    results[name] = {"ok": False, "message": status}
        return web.json_response({"results": results})

    async def _handle_reload_all(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        async with self._lock:
            ok, summary = await self.bot.reload_all_cogs_with_discovery()
        if ok:
            return web.json_response({"ok": True, "summary": summary})
        raise web.HTTPInternalServerError(text=str(summary))

    async def _handle_reload_namespace(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        namespace = payload.get("namespace")
        if not namespace:
            raise web.HTTPBadRequest(text="'namespace' is required")

        try:
            normalized = self.bot.normalize_namespace(namespace)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid namespace")

        if self.bot.is_namespace_blocked(normalized, assume_normalized=True):
            return web.json_response(
                {
                    "ok": False,
                    "results": {},
                    "message": f"{normalized} ist blockiert",
                }
            )

        async with self._lock:
            results = await self.bot.reload_namespace(normalized)
        ok = all(v in ("loaded", "reloaded") for v in results.values())
        if not results:
            message = f"Keine Cogs unter {normalized} gefunden"
        else:
            message = f"Reloaded {len(results)} cogs under {normalized}"
        return web.json_response({"ok": ok, "results": results, "message": message})

    async def _handle_discover(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        before = set(self.bot.cogs_list)
        self.bot.auto_discover_cogs()
        after = set(self.bot.cogs_list)
        new = sorted(after - before)
        return web.json_response({"ok": True, "new": new, "count": len(after)})

    async def _handle_block(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        path = payload.get("path")
        if not path:
            raise web.HTTPBadRequest(text="'path' is required")
        async with self._lock:
            try:
                result = await self.bot.block_namespace(path)
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid namespace")
        namespace = result.get("namespace", path)
        changed = result.get("changed", False)
        unloaded = result.get("unloaded", {})
        message = (
            f"🚫 {namespace} blockiert" if changed else f"{namespace} war bereits blockiert"
        )
        return web.json_response(
            {
                "ok": True,
                "namespace": namespace,
                "changed": changed,
                "unloaded": unloaded,
                "message": message,
            }
        )

    async def _handle_unblock(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        path = payload.get("path")
        if not path:
            raise web.HTTPBadRequest(text="'path' is required")
        async with self._lock:
            try:
                result = await self.bot.unblock_namespace(path)
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid namespace")
        namespace = result.get("namespace", path)
        changed = result.get("changed", False)
        message = (
            f"✅ {namespace} freigegeben" if changed else f"{namespace} war nicht blockiert"
        )
        return web.json_response(
            {
                "ok": True,
                "namespace": namespace,
                "changed": changed,
                "message": message,
            }
        )

if TYPE_CHECKING:  # pragma: no cover - avoid runtime dependency cycle
    from main_bot import MasterBot

