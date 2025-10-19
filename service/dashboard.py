from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

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
        .error { color: #ff8787; }
        .success { color: #69db7c; }
    </style>
</head>
<body>
    <div class="top-nav">
        <a href="/twitch">Twitch Dashboard öffnen</a>
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
            <div class=\"card\">
                <h2>Discovered Namespaces</h2>
                <ul id=\"namespace-list\"></ul>
            </div>
        </div>
    </section>

    <section>
        <h2>Cogs</h2>
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
    </section>

    <section>
        <h2>Recent Operations</h2>
        <div class=\"log\" id=\"operation-log\"></div>
    </section>

    <script>
    const opLog = document.getElementById('operation-log');
    const tableBody = document.getElementById('cog-table');
    const namespaceList = document.getElementById('namespace-list');
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

    function namespaceFromCog(name) {
        const parts = name.split('.');
        if (parts.length >= 3) {
            return parts.slice(0, 3).join('.');
        }
        return parts.slice(0, 2).join('.');
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

            namespaceList.innerHTML = '';
            for (const ns of data.cogs.namespaces) {
                const li = document.createElement('li');
                li.textContent = ns.namespace + ' (' + ns.count + ')';
                const btn = document.createElement('button');
                btn.textContent = 'Reload';
                btn.className = 'namespace';
                btn.addEventListener('click', () => reloadNamespace(ns.namespace));
                li.appendChild(document.createTextNode(' '));
                li.appendChild(btn);
                namespaceList.appendChild(li);
            }

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
        } catch (err) {
            log('Status konnte nicht geladen werden: ' + err.message, 'error');
        }
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

    async function reloadNamespace(namespace) {
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

    def __init__(self, bot: "MasterBot", *, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.token = token or os.getenv("MASTER_DASHBOARD_TOKEN")
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock = asyncio.Lock()
        self._started = False

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
                    web.post("/api/cogs/discover", self._handle_discover),
                ]
            )

            self._runner = web.AppRunner(app)
            await self._runner.setup()

            # Unter Windows bleibt der Port häufig kurzzeitig im TIME_WAIT-Zustand.
            # reuse_address ermöglicht schnelle Neustarts ohne Fehlermeldung.
            #
            # Allerdings führt reuse_address auf Windows-Installationen (insbesondere
            # seit Python 3.11) zu "WinError 10013". Daher aktivieren wir die Option
            # nur auf Plattformen, die sie sicher unterstützen.
            site_kwargs: Dict[str, Any] = {}
            if os.name != "nt":
                site_kwargs["reuse_address"] = True

            try:
                self._site = web.TCPSite(self._runner, self.host, self.port, **site_kwargs)
                await self._site.start()
            except OSError as e:
                await self._cleanup()
                if e.errno in {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)}:
                    raise RuntimeError(
                        f"Dashboard-Port {self.host}:{self.port} ist bereits belegt"
                    ) from e
                raise

            self._started = True
            logging.info("Master dashboard available on http://%s:%s", self.host, self.port)

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

    async def _handle_index(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        return web.Response(text=_HTML_TEMPLATE, content_type="text/html")

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

        async with self._lock:
            results = await self.bot.reload_namespace(namespace)
        ok = all(v in ("loaded", "reloaded") for v in results.values())
        message = f"Reloaded {len(results)} cogs under {namespace}"
        return web.json_response({"ok": ok, "results": results, "message": message})

    async def _handle_discover(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        before = set(self.bot.cogs_list)
        self.bot.auto_discover_cogs()
        after = set(self.bot.cogs_list)
        new = sorted(after - before)
        return web.json_response({"ok": True, "new": new, "count": len(after)})

if TYPE_CHECKING:  # pragma: no cover - avoid runtime dependency cycle
    from main_bot import MasterBot

