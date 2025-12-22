from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import importlib
import json
import math
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aiohttp import ClientSession, ClientTimeout, web

from service import db

try:
    from service.standalone_manager import (
        StandaloneAlreadyRunning,
        StandaloneConfigNotFound,
        StandaloneManagerError,
        StandaloneNotRunning,
    )
except Exception:
    StandaloneAlreadyRunning = StandaloneConfigNotFound = StandaloneManagerError = StandaloneNotRunning = None  # type: ignore

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>Master Bot Dashboard</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
    <style>
        :root {
            color-scheme: dark light;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;
        }
        body {
            margin: 0 auto;
            padding: 1.5rem;
            max-width: 1650px;
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
            margin-top: 1rem;
        }
        .cog-management h3,
        .cog-management h4 {
            margin-top: 0;
        }
        .cog-management h4 {
            font-weight: 600;
        }
        .management-columns {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            margin-top: 1rem;
        }
        .tree-panel {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
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
        .dashboard-meta {
            margin-top: 0.6rem;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .dashboard-row {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.5rem;
        }
        .dashboard-url,
        .dashboard-last {
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .standalone-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 1rem;
        }
        .standalone-card {
            background: #161616;
            border-radius: 8px;
            padding: 1rem;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .standalone-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
            margin-bottom: 0.75rem;
        }
        .standalone-status {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            font-weight: 600;
        }
        .standalone-meta {
            font-size: 0.85rem;
            color: #adb5bd;
            margin-bottom: 0.75rem;
        }
        .standalone-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin-bottom: 0.75rem;
        }
        .standalone-actions button {
            padding: 0.35rem 0.7rem;
        }
        .standalone-metrics {
            display: grid;
            gap: 0.4rem;
            font-size: 0.85rem;
        }
        .standalone-metrics strong {
            font-weight: 600;
        }
        .standalone-commands {
            margin-top: 0.75rem;
            border-top: 1px solid rgba(255,255,255,0.06);
            padding-top: 0.75rem;
        }
        .standalone-commands form {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }
        .standalone-commands select {
            background: #1f1f1f;
            color: inherit;
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px;
            padding: 0.35rem 0.6rem;
        }
        .standalone-logs {
            margin-top: 0.75rem;
        }
        .standalone-log-view {
            margin-top: 0.5rem;
            background: #0f0f0f;
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 6px;
            padding: 0.75rem;
            max-height: 320px;
            min-height: 150px;
            overflow-y: auto;
            white-space: pre-wrap;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.84rem;
            line-height: 1.4;
        }
        .standalone-log-view.expanded {
            max-height: 640px;
        }
        .logs-controls {
            display: flex;
            justify-content: flex-end;
            gap: 0.5rem;
            margin-top: 0.4rem;
        }
        .log-expand {
            background: #343a40;
            color: #f5f5f5;
            padding: 0.25rem;
            margin-right: 0;
            margin-bottom: 0;
            width: 34px;
            height: 34px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .log-expand .expand-icon {
            position: relative;
            width: 14px;
            height: 14px;
            border: 2px solid currentColor;
            border-radius: 3px;
        }
        .log-expand .expand-icon::after {
            content: '';
            position: absolute;
            inset: -4px;
            border: 2px solid currentColor;
            border-radius: 3px;
            opacity: 0.45;
            transform: translate(4px, -4px);
        }
        .log-expand.expanded .expand-icon::after {
            opacity: 0.9;
            transform: translate(-2px, 2px);
        }
        .standalone-list {
            list-style: none;
            margin: 0.5rem 0 0;
            padding: 0;
            font-size: 0.8rem;
        }
        .standalone-list li {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 0.5rem;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 0.35rem 0;
        }
        .standalone-list-main {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
        }
        .standalone-list-title {
            font-weight: 600;
        }
        .standalone-list-time {
            color: #adb5bd;
            font-size: 0.75rem;
            white-space: nowrap;
            font-variant-numeric: tabular-nums;
        }
        .health-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 1rem;
        }
        .health-card {
            background: #161616;
            border-radius: 8px;
            padding: 1rem;
            border: 1px solid rgba(255,255,255,0.05);
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }
        .health-status {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
            font-weight: 600;
        }
        .health-status-headline {
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 999px;
            display: inline-block;
        }
        .status-dot.ok {
            background: #51cf66;
            box-shadow: 0 0 8px rgba(81,207,102,0.85);
        }
        .status-dot.fail {
            background: #ff6b6b;
            box-shadow: 0 0 8px rgba(255,107,107,0.85);
        }
        .health-status-code {
            font-size: 0.8rem;
            padding: 0.15rem 0.5rem;
            border-radius: 999px;
            background: rgba(255,255,255,0.12);
        }
        .health-status-code.ok {
            color: #c0ffc0;
        }
        .health-status-code.fail {
            color: #ffc9c9;
        }
        .health-url {
            font-size: 0.85rem;
            color: #74c0fc;
            text-decoration: none;
            word-break: break-word;
        }
        .health-url:hover {
            text-decoration: underline;
        }
        .health-meta {
            font-size: 0.8rem;
            color: #adb5bd;
        }
        .health-error {
            font-size: 0.85rem;
            color: #ff8787;
        }
        .status-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            padding: 0.15rem 0.55rem;
            font-size: 0.7rem;
            font-weight: 600;
            background: rgba(255,255,255,0.08);
            color: #f5f5f5;
            text-transform: uppercase;
            letter-spacing: 0.02em;
        }
        .status-pill.status-success { background: rgba(55, 178, 77, 0.22); color: #8ce99a; }
        .status-pill.status-error,
        .status-pill.status-failed { background: rgba(201, 42, 42, 0.22); color: #ff8787; }
        .status-pill.status-pending { background: rgba(250, 176, 5, 0.25); color: #ffd43b; }
        .status-pill.status-running { background: rgba(51, 154, 240, 0.22); color: #74c0fc; }
        .status-pill.status-task { background: rgba(112, 72, 232, 0.22); color: #d0bfff; }
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
        button.autostart-toggle { background: #495057; color: #fff; }
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
            max-height: 600px;
            overflow-y: auto;
            padding-right: 0.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }
        .tree-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
        }
        .tree-header-text {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
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
        .tree-node.selected,
        .tree-leaf.selected {
            border-color: rgba(51, 154, 240, 0.55);
            box-shadow: 0 0 0 1px rgba(51, 154, 240, 0.35);
        }
        .tree-node.selected > summary {
            background: rgba(51, 154, 240, 0.12);
            border-radius: 4px;
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
        .tree-leaf.selected {
            background: rgba(51, 154, 240, 0.12);
        }
        .tree-leaf .leaf-meta {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            flex-wrap: wrap;
        }
        .tree-actions .managed-info {
            font-size: 0.75rem;
            opacity: 0.7;
            padding: 0.2rem 0;
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
        .tag.managed { background: rgba(51, 154, 240, 0.22); color: #74c0fc; }
        .error { color: #ff8787; }
        .success { color: #69db7c; }
        .selection-info {
            color: #adb5bd;
            font-size: 0.85rem;
            margin: 0;
        }
        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            flex-wrap: wrap;
        }
        .section-header h2 {
            margin: 0;
        }
        .section-actions {
            display: inline-flex;
            align-items: center;
            gap: 0.6rem;
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 0.75rem;
            margin-top: 1rem;
        }
        .stat-card {
            background: #161616;
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 10px;
            padding: 0.9rem;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .stat-label {
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .stat-value {
            font-size: 1.4rem;
            font-weight: 700;
        }
        .stat-sub {
            color: #868e96;
            font-size: 0.8rem;
        }
        .voice-columns {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }
        .voice-table {
            width: 100%;
            overflow-x: auto;
        }
        .voice-table table {
            width: 100%;
            border-collapse: collapse;
        }
        .voice-table th, .voice-table td {
            padding: 0.45rem 0.4rem;
            text-align: left;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .voice-table th {
            font-size: 0.85rem;
            color: #adb5bd;
            font-weight: 600;
        }
        .voice-table td {
            font-size: 0.92rem;
        }
        .voice-meta {
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .filter-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.6rem;
            align-items: center;
            margin-bottom: 0.75rem;
        }
        .filter-row select,
        .filter-row input {
            padding: 0.45rem 0.65rem;
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.1);
            background: #1f1f1f;
            color: inherit;
        }
        .segmented {
            display: inline-flex;
            align-items: center;
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            overflow: hidden;
        }
        .segmented button {
            border: none;
            background: transparent;
            color: inherit;
            padding: 0.45rem 0.75rem;
            cursor: pointer;
            font-weight: 600;
        }
        .segmented button.active {
            background: #3b82f6;
            color: #0b1021;
        }
        .user-picker {
            background: #121212;
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 8px;
            padding: 0.75rem;
            margin-top: 0.75rem;
            display: flex;
            flex-direction: column;
            gap: 0.6rem;
        }
        .user-picker-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
        }
        .user-chip-list {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
        }
        .user-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.35rem 0.7rem;
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.08);
            background: #1f1f1f;
            color: #f5f5f5;
            cursor: pointer;
            font-weight: 600;
            transition: border-color 0.15s ease, background 0.15s ease;
        }
        .user-chip:hover {
            border-color: rgba(59,130,246,0.75);
        }
        .user-chip.active {
            background: #3b82f6;
            color: #0b1021;
            border-color: #3b82f6;
        }
        .user-chip .meta {
            color: #adb5bd;
            font-size: 0.8rem;
        }
        .user-insights {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 0.6rem;
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .user-insights strong {
            color: #f5f5f5;
            display: block;
            margin-bottom: 0.15rem;
        }
        .button-ghost {
            background: transparent;
            border: 1px solid rgba(255,255,255,0.25);
            color: inherit;
        }
        .chart-container {
            position: relative;
            width: 100%;
            min-height: 420px;
        }
        .bar-row {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin: 0.25rem 0;
        }
        .bar-label {
            width: 44px;
            font-variant-numeric: tabular-nums;
            color: #adb5bd;
            font-size: 0.9rem;
        }
        .bar {
            flex: 1;
            height: 10px;
            border-radius: 999px;
            background: linear-gradient(90deg, #4dabf7, #845ef7);
            position: relative;
        }
        .bar-value {
            width: 82px;
            text-align: right;
            font-variant-numeric: tabular-nums;
            color: #adb5bd;
            font-size: 0.85rem;
        }
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
                <div class=\"dashboard-meta\">
                    <div class=\"dashboard-row\">
                        <span class=\"status-pill\" id=\"dashboard-status-pill\">Status unbekannt</span>
                        <span class=\"dashboard-last\" id=\"dashboard-restart-info\">Letzter Restart: \u2013</span>
                    </div>
                    <div class=\"dashboard-row\">
                        <span class=\"dashboard-url\" id=\"dashboard-listen\">Listen: \u2013</span>
                        <span class=\"dashboard-url\" id=\"dashboard-public\">Public: \u2013</span>
                    </div>
                    <div class=\"dashboard-row\">
                        <button class=\"namespace\" id=\"dashboard-restart\">Dashboard neu starten</button>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section>
        <h2>Site Health</h2>
        <div id="health-container" class="health-grid"></div>
    </section>

    

    <section>
        <div class="section-header">
            <h2>Voice Historie</h2>
            <div class="section-actions">
                <button class="reload" id="voice-history-refresh">Neu laden</button>
                <span class="voice-meta" id="voice-history-updated">Letzte Aktualisierung: -</span>
            </div>
        </div>
        <div class="voice-columns">
            <div class="card">
                <h3>Aktivität nach Stunde</h3>
                <div class="filter-row">
                    <div class="segmented" id="voice-mode-buttons">
                        <button class="voice-mode-btn active" data-mode="hour">Stunde</button>
                        <button class="voice-mode-btn" data-mode="day">Tag</button>
                        <button class="voice-mode-btn" data-mode="month">Monat</button>
                        <button class="voice-mode-btn" data-mode="user">User</button>
                    </div>
                    <input id="voice-history-user" type="text" placeholder="User ID (optional)" />
                    <button class="reload" id="voice-user-apply">Anzeigen</button>
                    <button class="button-ghost" id="voice-user-reset">Alle User</button>
                </div>
                <div class="user-picker" id="voice-user-mode" style="display: none">
                    <div class="user-picker-header">
                        <div class="voice-meta">Aktivste User (letzte 14 Tage)</div>
                        <div class="voice-meta" id="voice-user-count"></div>
                    </div>
                    <div id="voice-user-list" class="user-chip-list"></div>
                    <div id="voice-user-insights" class="user-insights"></div>
                </div>
                <div class="chart-container">
                    <canvas id="voice-hourly-chart"></canvas>
                </div>
            </div>
        </div>
    </section>

    <section>
        <h2>Standalone Dienste</h2>
        <div id="standalone-container" class="standalone-grid"></div>
    </section>

    <section>
        <h2>User Retention</h2>
        <div class="card">
            <h3>Statistiken</h3>
            <div id="retention-stats" class="stats-grid">
                <div class="stat-item"><span class="stat-value" id="ret-total">-</span><span class="stat-label">Getrackte User</span></div>
                <div class="stat-item"><span class="stat-value" id="ret-regular">-</span><span class="stat-label">Reguläre User</span></div>
                <div class="stat-item"><span class="stat-value" id="ret-inactive">-</span><span class="stat-label">Inaktiv (eligible)</span></div>
                <div class="stat-item"><span class="stat-value" id="ret-optout">-</span><span class="stat-label">Opted-out</span></div>
                <div class="stat-item"><span class="stat-value" id="ret-sent">-</span><span class="stat-label">Nachrichten gesendet</span></div>
                <div class="stat-item"><span class="stat-value" id="ret-feedback">-</span><span class="stat-label">Feedbacks erhalten</span></div>
            </div>
            <div style="margin-top: 1rem;">
                <button class="namespace" id="retention-refresh">Aktualisieren</button>
                <button class="namespace" id="retention-run-check" style="background:#e67e22;">Jetzt Check ausführen</button>
            </div>
        </div>
        <div class="card" style="margin-top: 1rem;">
            <h3>Inaktive User (eligible)</h3>
            <div id="retention-users" style="max-height: 300px; overflow-y: auto;"></div>
        </div>
        <div class="card" style="margin-top: 1rem;">
            <h3>Feedback</h3>
            <div id="retention-feedback-list" style="max-height: 300px; overflow-y: auto;"></div>
        </div>
    </section>

    <section>
        <h2>Cog Management</h2>
        <div class=\"card cog-management\">
            <h3>Management Tools</h3>
            <div class=\"management-columns\">
                    <div class=\"tree-panel\">
                    <div class=\"tree-header\">
                        <div class=\"tree-header-text\">
                            <h4>Namespaces &amp; Cogs</h4>
                            <span class=\"selection-info\">Explorer mit direkter Steuerung</span>
                        </div>
                        <button class=\"namespace\" id=\"toggle-unmanageable\">Nicht-managebare Cogs einblenden</button>
                    </div>
                    <div id=\"tree-container\" class=\"tree-container\"></div>
                </div>
            </div>
        </div>
    </section>

    <section>
        <h2>Recent Operations</h2>
        <div class=\"log\" id=\"operation-log\"></div>
    </section>

    <script>
    const opLog = document.getElementById('operation-log');
    const treeContainer = document.getElementById('tree-container');
    const tokenInput = document.getElementById('token-input');
    const selectionTitle = document.getElementById('selection-title');
    const selectionDescription = document.getElementById('selection-description');
    const resetSelectionBtn = document.getElementById('reset-selection');
    const standaloneContainer = document.getElementById('standalone-container');
    const healthContainer = document.getElementById('health-container');
    const voiceSummary = document.getElementById('voice-summary');
    const voiceTopTime = document.getElementById('voice-top-time');
    const voiceTopPoints = document.getElementById('voice-top-points');
    const voiceLive = document.getElementById('voice-live');
    const voiceUpdated = document.getElementById('voice-updated');
    const voiceRefreshButton = document.getElementById('voice-refresh');
    const voiceHourlyChartCanvas = document.getElementById('voice-hourly-chart');
    let voiceHourlyChart = null;
    const voiceHistoryUpdated = document.getElementById('voice-history-updated');
    const voiceHistoryRefreshButton = document.getElementById('voice-history-refresh');
    const voiceHistoryUser = document.getElementById('voice-history-user');
    const voiceUserApply = document.getElementById('voice-user-apply');
    const voiceUserReset = document.getElementById('voice-user-reset');
    const voiceUserList = document.getElementById('voice-user-list');
    const voiceUserInsights = document.getElementById('voice-user-insights');
    const voiceUserCount = document.getElementById('voice-user-count');
    const voiceUserMode = document.getElementById('voice-user-mode');
    const voiceModeButtons = document.querySelectorAll('.voice-mode-btn');
    const dashboardRestartBtn = document.getElementById('dashboard-restart');
    const dashboardStatusPill = document.getElementById('dashboard-status-pill');
    const dashboardRestartInfo = document.getElementById('dashboard-restart-info');
    const dashboardListen = document.getElementById('dashboard-listen');
    const dashboardPublic = document.getElementById('dashboard-public');
    let currentVoiceMode = 'hour';
    let currentVoiceUser = '';
    let voiceActiveUsers = [];
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
    const toggleUnmanageableButton = document.getElementById('toggle-unmanageable');
    const logOpenState = new Map();
    let isRefreshingStandalone = false;
    let authToken = localStorage.getItem('master-dashboard-token') || '';
    let selectedNode = null;
    let showHiddenCogs = false;
    let lastTreeData = null;
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

    // Alias for existing fetch helper so retention dashboard calls work
    const apiFetch = fetchJSON;

    if (toggleUnmanageableButton) {
        toggleUnmanageableButton.addEventListener('click', () => {
            showHiddenCogs = !showHiddenCogs;
            updateHiddenToggleButton();
            renderTree(lastTreeData);
        });
    }

    if (voiceRefreshButton) {
        voiceRefreshButton.addEventListener('click', () => {
            loadVoiceStats();
        });
    }

    if (voiceHistoryRefreshButton) {
        voiceHistoryRefreshButton.addEventListener('click', () => {
            loadVoiceHistory();
        });
    }
    if (voiceUserApply) {
        voiceUserApply.addEventListener('click', () => {
            const userId = voiceHistoryUser ? voiceHistoryUser.value.trim() : '';
            applyVoiceUserSelection(userId);
        });
    }
    if (voiceUserReset) {
        voiceUserReset.addEventListener('click', () => {
            applyVoiceUserSelection('', true);
        });
    }
    if (voiceModeButtons && voiceModeButtons.length) {
        voiceModeButtons.forEach((btn) => {
            btn.addEventListener('click', () => {
                voiceModeButtons.forEach((b) => b.classList.remove('active'));
                btn.classList.add('active');
                currentVoiceMode = btn.dataset.mode || 'hour';
                updateVoiceModeUI();
                loadVoiceHistory();
            });
        });
    }
    if (dashboardRestartBtn) {
        dashboardRestartBtn.addEventListener('click', async () => {
            dashboardRestartBtn.disabled = true;
            dashboardRestartBtn.textContent = 'Restart l\u00e4uft...';
            try {
                await fetchJSON('/api/dashboard/restart', { method: 'POST', body: JSON.stringify({}) });
                log('Dashboard-Restart ausgel\u00f6st', 'success');
                setTimeout(loadStatus, 1200);
            } catch (err) {
                log('Dashboard-Restart fehlgeschlagen: ' + err.message, 'error');
            } finally {
                setTimeout(() => {
                    dashboardRestartBtn.disabled = false;
                    dashboardRestartBtn.textContent = 'Dashboard neu starten';
                }, 1200);
            }
        });
    }

    function updateHiddenToggleButton() {
        if (!toggleUnmanageableButton) {
            return;
        }
        toggleUnmanageableButton.textContent = showHiddenCogs
            ? 'Nicht-managebare Cogs ausblenden'
            : 'Nicht-managebare Cogs einblenden';
    }
    updateHiddenToggleButton();
    updateVoiceModeUI();

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

    function shouldHideNode(node, nodeType) {
        if (showHiddenCogs) {
            return false;
        }
        if (!node || isDirectoryLike(nodeType)) {
            return false;
        }
        if (!Object.prototype.hasOwnProperty.call(node, 'manageable')) {
            return false;
        }
        return node.manageable === false;
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
            document.getElementById('bot-latency').textContent = 'Latency: ' + formatLatency(data.bot.latency_ms);

            renderDashboard(data.dashboard || {});

            const healthChecks = data.health || [];
            renderHealth(healthChecks);
            const standalone = data.standalone || [];
            renderStandalone(standalone);
            const cogs = data.cogs || {};
            const tree = cogs.tree || null;
            lastTreeData = tree;
            renderTree(tree);
        } catch (err) {
            log('Status konnte nicht geladen werden: ' + err.message, 'error');
        }
    }

    function renderDashboard(info = {}) {
        const restarting = Boolean(info.restart_in_progress);
        const running = info.running === undefined ? true : Boolean(info.running);

        if (dashboardStatusPill) {
            let cls = 'status-pill ';
            let label = 'Status unbekannt';
            if (restarting) {
                cls += 'status-running';
                label = 'Restart l\u00e4uft';
            } else if (running) {
                cls += 'status-success';
                label = 'L\u00e4uft';
            } else {
                cls += 'status-error';
                label = 'Gestoppt';
            }
            dashboardStatusPill.className = cls;
            dashboardStatusPill.textContent = label;
        }

        if (dashboardRestartBtn) {
            dashboardRestartBtn.disabled = restarting;
            dashboardRestartBtn.textContent = restarting ? 'Restart l\u00e4uft...' : 'Dashboard neu starten';
        }

        if (dashboardRestartInfo) {
            const last = info.last_restart || {};
            const when = last.at || last.time || last.timestamp;
            const statusLabel = last.ok === false ? 'Letzter Restart fehlgeschlagen' : 'Letzter Restart';
            const whenLabel = when ? formatTimestamp(when) : '\u2013';
            const errText = last.error ? ' (' + last.error + ')' : '';
            dashboardRestartInfo.textContent = statusLabel + ': ' + whenLabel + errText;
        }

        if (dashboardListen) {
            const listen = info.listen_url || info.listen || '-';
            dashboardListen.textContent = 'Listen: ' + (listen || '-');
        }
        if (dashboardPublic) {
            const pub = info.public_url || info.public || '-';
            dashboardPublic.textContent = 'Public: ' + (pub || '-');
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
        } else {
            const empty = document.createElement('div');
            empty.className = 'tree-empty';
            empty.textContent = showHiddenCogs
                ? 'Keine Daten verfügbar.'
                : 'Alle nicht-managebaren Cogs sind derzeit ausgeblendet.';
            treeContainer.appendChild(empty);
        }
        const hasSelection = applySelection();
        if (selectedNode && !hasSelection) {
            clearSelection();
        }
    }

    function renderHealth(items) {
        if (!healthContainer) {
            return;
        }
        healthContainer.innerHTML = '';
        if (!Array.isArray(items) || items.length === 0) {
            const empty = document.createElement('p');
            empty.className = 'health-meta';
            empty.textContent = 'Keine Health Checks konfiguriert.';
            healthContainer.appendChild(empty);
            return;
        }
        items.forEach((item) => {
            const card = document.createElement('div');
            card.className = 'health-card';

            const statusRow = document.createElement('div');
            statusRow.className = 'health-status';
            const head = document.createElement('div');
            head.className = 'health-status-headline';

            const dot = document.createElement('span');
            dot.className = 'status-dot ' + (item && item.ok ? 'ok' : 'fail');
            head.appendChild(dot);

            const label = document.createElement('span');
            label.textContent = (item && (item.label || item.key || item.url)) || 'Unbekannt';
            head.appendChild(label);

            statusRow.appendChild(head);

            if (item && item.status !== undefined && item.status !== null) {
                const code = document.createElement('span');
                code.className = 'health-status-code ' + (item.ok ? 'ok' : 'fail');
                const reason = item.reason ? ' ' + item.reason : '';
                code.textContent = item.status + reason;
                statusRow.appendChild(code);
            }

            card.appendChild(statusRow);

            if (item && item.url) {
                const urlLink = document.createElement('a');
                urlLink.className = 'health-url';
                urlLink.href = item.resolved_url || item.url;
                urlLink.target = '_blank';
                urlLink.rel = 'noopener';
                urlLink.textContent = item.url;
                card.appendChild(urlLink);
            }

            if (item && item.resolved_url && item.resolved_url !== item.url) {
                const resolved = document.createElement('div');
                resolved.className = 'health-meta';
                resolved.textContent = '-> ' + item.resolved_url;
                card.appendChild(resolved);
            }

            const meta = document.createElement('div');
            meta.className = 'health-meta';
            const statusLabel = (item && item.status !== null && item.status !== undefined) ? item.status : '-';
            const latencyLabel = formatLatency(item ? item.latency_ms : undefined);
            const checkedLabel = formatTimestamp(item ? item.checked_at : undefined);
            meta.textContent = 'Status: ' + statusLabel + ' | Latenz: ' + latencyLabel + ' | Geprüft: ' + checkedLabel;
            card.appendChild(meta);

            if (item && !item.ok && item.error) {
                const error = document.createElement('div');
                error.className = 'health-error';
                error.textContent = item.error;
                card.appendChild(error);
            } else if (item && !item.ok && item.body_excerpt) {
                const excerpt = document.createElement('div');
                excerpt.className = 'health-error';
                excerpt.textContent = item.body_excerpt;
                card.appendChild(excerpt);
            }

            healthContainer.appendChild(card);
        });
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
        return '-';
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

function formatLatency(ms) {
    if (typeof ms !== 'number' || !Number.isFinite(ms)) {
        return '-';
    }
    if (ms >= 1000) {
        const seconds = ms / 1000;
        const digits = seconds >= 10 ? 1 : 2;
        return seconds.toFixed(digits) + ' s';
    }
    if (ms >= 1) {
        return ms.toFixed(0) + ' ms';
    }
    return ms.toFixed(2) + ' ms';
}

function updateVoiceModeUI() {
    if (voiceUserMode) {
        voiceUserMode.style.display = currentVoiceMode === 'user' ? 'block' : 'none';
    }
}

function findActiveUserById(userId) {
    if (!userId || !Array.isArray(voiceActiveUsers)) {
        return null;
    }
    return voiceActiveUsers.find((u) => String(u.user_id) === String(userId)) || null;
}

function highlightVoiceUserChips() {
    if (!voiceUserList) {
        return;
    }
    const chips = voiceUserList.querySelectorAll('.user-chip');
    chips.forEach((chip) => {
        const id = chip.dataset.userId || '';
        chip.classList.toggle('active', Boolean(currentVoiceUser) && id === currentVoiceUser);
    });
}

function renderVoiceUserInsights(user) {
    if (!voiceUserInsights) {
        return;
    }
    voiceUserInsights.innerHTML = '';
    if (!user) {
        const hint = document.createElement('div');
        hint.className = 'voice-meta';
        hint.textContent = currentVoiceUser
            ? 'Keine Daten fьr diesen User gefunden.'
            : 'User auswдhlen, um Details zu sehen.';
        voiceUserInsights.appendChild(hint);
        return;
    }
    const items = [
        { label: 'Gesamtzeit (14 Tage)', value: formatSeconds(user.total_seconds) },
        { label: 'Punkte', value: safeNumber(user.total_points) },
        { label: 'Sessions', value: safeNumber(user.sessions) },
    ];
    if (user.last_seen) {
        items.push({ label: 'Zuletzt gesehen', value: formatTimestamp(user.last_seen) });
    }
    items.forEach((item) => {
        const box = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = item.label;
        const value = document.createElement('div');
        value.textContent = String(item.value);
        box.appendChild(title);
        box.appendChild(value);
        voiceUserInsights.appendChild(box);
    });
}

function renderVoiceActiveUsers(users) {
    voiceActiveUsers = Array.isArray(users) ? users : [];
    if (!voiceUserList) {
        return;
    }
    voiceUserList.innerHTML = '';
    if (voiceUserCount) {
        voiceUserCount.textContent = voiceActiveUsers.length
            ? `${voiceActiveUsers.length} User`
            : 'Keine User';
    }
    if (!voiceActiveUsers.length) {
        const empty = document.createElement('div');
        empty.className = 'voice-meta';
        empty.textContent = 'Keine Voice-Aktivitдt in den letzten 14 Tagen.';
        voiceUserList.appendChild(empty);
        renderVoiceUserInsights(null);
        return;
    }
    voiceActiveUsers.forEach((user) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'user-chip';
        chip.dataset.userId = String(user.user_id);
        chip.textContent = user.display_name || user.user_id;
        const meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = formatSeconds(user.total_seconds);
        chip.appendChild(meta);
        if (String(user.user_id) === currentVoiceUser) {
            chip.classList.add('active');
        }
        chip.addEventListener('click', () => {
            applyVoiceUserSelection(String(user.user_id));
        });
        voiceUserList.appendChild(chip);
    });
    highlightVoiceUserChips();
    let selected = findActiveUserById(currentVoiceUser);
    if (currentVoiceMode === 'user' && !selected && voiceActiveUsers.length) {
        selected = voiceActiveUsers[0];
        currentVoiceUser = String(selected.user_id);
        if (voiceHistoryUser) {
            voiceHistoryUser.value = currentVoiceUser;
        }
        highlightVoiceUserChips();
    }
    renderVoiceUserInsights(selected || null);
}

function applyVoiceUserSelection(userId, triggerLoad = true) {
    currentVoiceUser = userId ? String(userId) : '';
    if (voiceHistoryUser) {
        voiceHistoryUser.value = currentVoiceUser;
    }
    highlightVoiceUserChips();
    const selected = findActiveUserById(currentVoiceUser);
    renderVoiceUserInsights(selected || null);
    if (triggerLoad) {
        loadVoiceHistory();
    }
}

function renderVoiceSummary(summary = {}, liveSummary = {}) {
    if (!voiceSummary) {
        return;
    }
    voiceSummary.innerHTML = '';
    const cards = [
        {
            label: 'Erfasste User',
            value: safeNumber(summary.tracked_users),
            sub: formatTimestamp(summary.last_update) !== '-' ? 'Letztes Update: ' + formatTimestamp(summary.last_update) : '-',
        },
        {
            label: 'Gesamtzeit',
            value: formatSeconds(summary.total_seconds),
            sub: `${safeNumber(summary.total_points)} Punkte`,
        },
        {
            label: '\u00d8 Zeit pro User',
            value: formatSeconds(summary.avg_seconds_per_user),
            sub: 'Alle Eintr\u00e4ge in voice_stats',
        },
        {
            label: 'Live',
            value: `${safeNumber(liveSummary.active_sessions)} aktiv`,
            sub: `${formatSeconds(liveSummary.total_seconds)} laufend`,
        },
    ];
    cards.forEach((card) => {
        const el = document.createElement('div');
        el.className = 'stat-card';
        const label = document.createElement('div');
        label.className = 'stat-label';
        label.textContent = card.label;
        const value = document.createElement('div');
        value.className = 'stat-value';
        value.textContent = card.value;
        const sub = document.createElement('div');
        sub.className = 'stat-sub';
        sub.textContent = card.sub;
        el.appendChild(label);
        el.appendChild(value);
        el.appendChild(sub);
        voiceSummary.appendChild(el);
    });
}

function renderVoiceTable(target, rows, emptyLabel) {
    if (!target) {
        return;
    }
    target.innerHTML = '';
    if (!Array.isArray(rows) || rows.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'voice-meta';
        empty.textContent = emptyLabel || 'Keine Daten verf\u00fcgbar.';
        target.appendChild(empty);
        return;
    }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>#</th><th>User</th><th>Zeit</th><th>Punkte</th><th>Update</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach((row, idx) => {
        const tr = document.createElement('tr');
        const pos = document.createElement('td');
        pos.textContent = idx + 1;
        const user = document.createElement('td');
        user.textContent = row.display_name || row.user_id;
        const time = document.createElement('td');
        time.textContent = formatSeconds(row.total_seconds);
        const points = document.createElement('td');
        points.textContent = safeNumber(row.total_points);
        const updated = document.createElement('td');
        updated.textContent = formatTimestamp(row.last_update);
        tr.appendChild(pos);
        tr.appendChild(user);
        tr.appendChild(time);
        tr.appendChild(points);
        tr.appendChild(updated);
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    target.appendChild(table);
}

function renderVoiceLive(target, sessions) {
    if (!target) {
        return;
    }
    target.innerHTML = '';
    if (!Array.isArray(sessions) || sessions.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'voice-meta';
        empty.textContent = 'Keine aktiven Sessions.';
        target.appendChild(empty);
        return;
    }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>User</th><th>Dauer</th><th>Channel</th><th>Peak</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    sessions.forEach((session) => {
        const tr = document.createElement('tr');
        const user = document.createElement('td');
        user.textContent = session.display_name || session.user_id;
        const duration = document.createElement('td');
        duration.textContent = formatSeconds(session.duration_seconds);
        const channel = document.createElement('td');
        channel.textContent = session.channel_name || session.channel_id || '-';
        if (session.started_at) {
            const meta = document.createElement('div');
            meta.className = 'stat-sub';
            meta.textContent = 'Start: ' + formatTimestamp(session.started_at);
            channel.appendChild(meta);
        }
        const peak = document.createElement('td');
        peak.textContent = safeNumber(session.peak_users) || '-';
        tr.appendChild(user);
        tr.appendChild(duration);
        tr.appendChild(channel);
        tr.appendChild(peak);
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    target.appendChild(table);
}

function renderVoiceStats(data) {
    const summary = (data && data.summary) || {};
    const live = (data && data.live) || {};
    const liveSummary = live.summary || {};
    renderVoiceSummary(summary, liveSummary);
    renderVoiceTable(voiceTopTime, data ? data.top_by_time : [], 'Noch keine Voice-Aktivit\u00e4t.');
    renderVoiceTable(voiceTopPoints, data ? data.top_by_points : [], 'Noch keine Voice-Aktivit\u00e4t.');
    renderVoiceLive(voiceLive, live.sessions || []);
    if (voiceUpdated) {
        const label = summary.last_update ? formatTimestamp(summary.last_update) : new Date().toLocaleTimeString();
        voiceUpdated.textContent = 'Letzte Aktualisierung: ' + label;
    }
}

async function loadVoiceStats() {
    try {
        const data = await fetchJSON('/api/voice-stats');
        renderVoiceStats(data);
    } catch (err) {
        if (voiceSummary) {
            voiceSummary.innerHTML = '<div class=\"voice-meta\">Fehler beim Laden der Voice-Daten.</div>';
        }
        log('Voice Stats konnten nicht geladen werden: ' + err.message, 'error');
    }
}

function renderVoiceHourlyChart(rows, mode, userInfo) {
    console.log('=== renderVoiceHourlyChart START ===');
    console.log('rows:', rows.length, 'mode:', mode, 'userInfo:', userInfo);
    if (!voiceHourlyChartCanvas) {
        console.log('ERROR: voiceHourlyChartCanvas not found!');
        return;
    }
    const formatLabel = (lbl) => {
        if (mode === 'hour') {
            return lbl.toString().padStart(2, '0') + ':00';
        }
        if (mode === 'week') {
            const names = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
            const idx = Number(lbl);
            return Number.isFinite(idx) ? names[(idx + 6) % 7] : lbl;
        }
        return lbl;
    };

    const labels = rows.map((r) => formatLabel(r.label || r.hour));
    const usersData = rows.map((r) => safeNumber(r.users));
    const peakData = rows.map((r) => safeNumber(r.avg_peak));
    console.log('Chart data prepared:');
    console.log('  labels:', labels.length);
    console.log('  usersData sum:', usersData.reduce((a, b) => a + b, 0), 'users');
    console.log('  usersData (first 5):', usersData.slice(0, 5));
    console.log('  peakData (first 5):', peakData.slice(0, 5));
    if (voiceHourlyChart) {
        console.log('Destroying existing chart');
        voiceHourlyChart.destroy();
    }
    const baseLabel = userInfo && userInfo.display_name ? userInfo.display_name : 'Voice';
    const subtitle = mode === 'hour' ? 'Stunde (UTC)' : mode === 'day' ? 'Wochentag' : mode === 'week' ? 'Kalenderwoche' : 'Monat';
    console.log('Creating chart with baseLabel:', baseLabel, 'subtitle:', subtitle);
    voiceHourlyChart = new Chart(voiceHourlyChartCanvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: `${baseLabel} User`,
                    data: usersData,
                    borderColor: '#4dabf7',
                    backgroundColor: 'rgba(77,171,247,0.18)',
                    fill: true,
                    tension: 0.35,
                    yAxisID: 'yUsers',
                    pointRadius: 3,
                },
                {
                    label: `${baseLabel} Peak User`,
                    data: peakData,
                    borderColor: '#e599f7',
                    backgroundColor: 'rgba(229,153,247,0.15)',
                    borderDash: [6, 4],
                    fill: false,
                    tension: 0.35,
                    yAxisID: 'yPeak',
                    pointRadius: 3,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
                yUsers: {
                    position: 'left',
                    title: { display: true, text: 'User' },
                    suggestedMin: 0,
                },
                yPeak: {
                    position: 'right',
                    title: { display: true, text: 'Peak User' },
                    suggestedMin: 0,
                    grid: { drawOnChartArea: false },
                },
                x: {
                    title: { display: true, text: subtitle },
                },
            },
            plugins: {
                legend: { position: 'top' },
                tooltip: {
                    callbacks: {
                        label: function(ctx) {
                            const label = ctx.dataset.label || '';
                            const value = ctx.parsed.y || 0;
                            return `${label}: ${Math.round(value)}`;
                        },
                    },
                },
            },
        },
    });
    console.log('=== renderVoiceHourlyChart END - Chart created successfully ===');
}

function renderVoiceHistory(data) {
    console.log('=== renderVoiceHistory START ===');
    console.log('currentVoiceMode:', currentVoiceMode, 'currentVoiceUser:', currentVoiceUser);
    if (!data) {
        console.log('ERROR: No data provided!');
        return;
    }
    const activeUsers = data.active_users || data.top_users || [];
    console.log('Active users:', activeUsers.length);
    renderVoiceActiveUsers(activeUsers);
    if (currentVoiceMode === 'user' && !currentVoiceUser && voiceActiveUsers.length) {
        console.log('Auto-selecting first user and returning early');
        applyVoiceUserSelection(String(voiceActiveUsers[0].user_id), true);
        return; // wait for the reload to render with user data
    }
    const selectedUser = currentVoiceMode === 'user'
        ? findActiveUserById(currentVoiceUser) || null
        : data.user || findActiveUserById(currentVoiceUser) || null;
    console.log('selectedUser:', selectedUser ? selectedUser.display_name : 'None', selectedUser ? `(${selectedUser.user_id})` : '');
    const chartMode = currentVoiceMode === 'user' ? 'hour' : (data.mode || 'hour');
    const buckets = data.buckets || [];
    const bucketSum = buckets.reduce((sum, b) => sum + (b.total_seconds || 0), 0);
    console.log('Buckets:', buckets.length, 'Total seconds:', bucketSum, 'Mode:', chartMode);
    renderVoiceHourlyChart(buckets, chartMode, selectedUser);
    if (voiceHistoryUpdated) {
        const now = new Date().toLocaleTimeString();
        voiceHistoryUpdated.textContent = 'Letzte Aktualisierung: ' + now + ` (letzte ${data.range_days || 0} Tage)`;
    }
}

async function loadVoiceHistory() {
    console.log('=== loadVoiceHistory START ===');
    console.log('Initial state - currentVoiceMode:', currentVoiceMode, 'currentVoiceUser:', currentVoiceUser);
    try {
        const mode = currentVoiceMode || 'hour';
        const apiMode = mode === 'user' ? 'hour' : mode;
        let userId = currentVoiceUser || (voiceHistoryUser ? voiceHistoryUser.value.trim() : '');
        if (mode === 'user' && !userId && Array.isArray(voiceActiveUsers) && voiceActiveUsers.length) {
            userId = String(voiceActiveUsers[0].user_id);
            currentVoiceUser = userId;
            console.log('Auto-selected first user:', userId);
            if (voiceHistoryUser) {
                voiceHistoryUser.value = userId;
            }
            highlightVoiceUserChips();
        }
        let range = 14;
        if (apiMode === 'day') range = 60;
        if (apiMode === 'week') range = 180;
        if (apiMode === 'month') range = 365;
        const params = new URLSearchParams({ mode: apiMode, range: range.toString(), top: '20' });
        if (userId) {
            params.set('user_id', userId);
        }
        const url = '/api/voice-history?' + params.toString();
        console.log('Fetching:', url);
        const data = await fetchJSON(url);
        console.log('API response received. Buckets:', data.buckets ? data.buckets.length : 0);
        if (mode === 'user') {
            const selected = findActiveUserById(userId) || null;
            const bucketSum = Array.isArray(data.buckets)
                ? data.buckets.reduce((sum, b) => sum + safeNumber(b.total_seconds), 0)
                : 0;
            const hasUserData = selected && safeNumber(selected.total_seconds) > 0;
            console.log('User mode checks - selected:', selected ? selected.display_name : 'None', 'hasUserData:', hasUserData, 'bucketSum:', bucketSum);
            if (hasUserData && bucketSum === 0) {
                console.log('RETRY TRIGGERED - hasUserData but bucketSum is 0');
                const retryParams = new URLSearchParams(params);
                retryParams.set('_', Date.now().toString()); // cache-bust
                const retry = await fetchJSON('/api/voice-history?' + retryParams.toString());
                retry._retry = true;
                console.log('Retry response received. Buckets:', retry.buckets ? retry.buckets.length : 0);
                renderVoiceHistory(retry);
                return;
            }
        }
        renderVoiceHistory(data);
    } catch (err) {
        console.error('loadVoiceHistory ERROR:', err);
        log('Voice Historie konnte nicht geladen werden: ' + err.message, 'error');
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
    const quick = metrics.quick_invites || {};
    const quickCounts = quick.counts || {};
    const tasks = metrics.tasks || {};
    const taskCounts = tasks.counts || {};

    const statusParts = [];
    if (runtime.logged_on) {
        statusParts.push('✅ Eingeloggt');
    } else if (runtime.logging_in) {
        statusParts.push('⏳ Login läuft');
    } else {
        statusParts.push('❌ Abgemeldet');
    }
    if (runtime.guard_required) {
        const guard = runtime.guard_required;
        const guardLabel = guard.type || guard.domain || 'unbekannt';
        statusParts.push(`Guard: ${guardLabel}`);
    }
    const statusDiv = document.createElement('div');
    statusDiv.innerHTML = `<strong>Status:</strong> ${statusParts.join(' • ')}`;
    container.appendChild(statusDiv);

    if (runtime.account_name || runtime.steam_id64) {
        const accountDiv = document.createElement('div');
        accountDiv.innerHTML = `<strong>Konto:</strong> ${runtime.account_name || '–'} (${runtime.steam_id64 || '–'})`;
        container.appendChild(accountDiv);
    }

    if (runtime.last_error && runtime.last_error.message) {
        const errorDiv = document.createElement('div');
        errorDiv.innerHTML = `<strong>Fehler:</strong> ${runtime.last_error.message}`;
        container.appendChild(errorDiv);
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
        const tasks = metrics.tasks || {};
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
                    logsBody.textContent = lines.join('\\n');
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
        if (!node) {
            return null;
        }
        const nodePath = getNodePath(node);
        const nodeType = node.type || (Array.isArray(node.children) ? 'directory' : 'module');
        if (shouldHideNode(node, nodeType)) {
            return null;
        }
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
            let appended = false;
            if (node.children && node.children.length) {
                for (const child of node.children) {
                    const builtChild = buildTreeNode(child, depth + 1);
                    if (builtChild) {
                        childrenContainer.appendChild(builtChild);
                        appended = true;
                    }
                }
            }
            if (!appended) {
                const empty = document.createElement('div');
                empty.className = 'tree-empty';
                empty.textContent = showHiddenCogs ? 'Keine Einträge' : 'Alle Einträge aktuell ausgeblendet';
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
    loadVoiceStats();
    setInterval(loadVoiceStats, 30000);
    loadVoiceHistory();
    setInterval(loadVoiceHistory, 60000);

    // ========= Retention Functions =========
    const retTotal = document.getElementById('ret-total');
    const retRegular = document.getElementById('ret-regular');
    const retInactive = document.getElementById('ret-inactive');
    const retOptout = document.getElementById('ret-optout');
    const retSent = document.getElementById('ret-sent');
    const retFeedback = document.getElementById('ret-feedback');
    const retentionUsers = document.getElementById('retention-users');
    const retentionFeedbackList = document.getElementById('retention-feedback-list');
    const retentionRefreshBtn = document.getElementById('retention-refresh');
    const retentionRunCheckBtn = document.getElementById('retention-run-check');

    async function loadRetentionStats() {
        try {
            const res = await apiFetch('/api/retention/stats');
            retTotal.textContent = res.total_tracked || 0;
            retRegular.textContent = res.regular_users || 0;
            retInactive.textContent = res.inactive_eligible || 0;
            retOptout.textContent = res.opted_out || 0;
            retSent.textContent = res.messages_sent || 0;
            retFeedback.textContent = res.feedback_count || 0;
        } catch (err) {
            log('Retention stats failed: ' + err.message, 'error');
        }
    }

    async function loadRetentionUsers() {
        try {
            const res = await apiFetch('/api/retention/inactive-users?limit=30');
            if (!res.users || res.users.length === 0) {
                retentionUsers.innerHTML = '<p style="color:#888;">Keine inaktiven User gefunden.</p>';
                return;
            }
            let html = '<table style="width:100%;font-size:0.9em;"><tr><th>User</th><th>Tage inaktiv</th><th>Aktion</th></tr>';
            for (const u of res.users) {
                html += '<tr><td>' + (u.display_name || u.user_id) + '</td><td>' + u.days_inactive + '</td>';
                html += '<td><button class="namespace" onclick="sendRetentionMsg(' + u.user_id + ',' + u.guild_id + ')">DM senden</button></td></tr>';
            }
            html += '</table>';
            retentionUsers.innerHTML = html;
        } catch (err) {
            retentionUsers.innerHTML = '<p style="color:#c00;">Fehler: ' + err.message + '</p>';
        }
    }

    async function loadRetentionFeedback() {
        try {
            const res = await apiFetch('/api/retention/feedback?limit=15');
            if (!res.feedback || res.feedback.length === 0) {
                retentionFeedbackList.innerHTML = '<p style="color:#888;">Noch kein Feedback erhalten.</p>';
                return;
            }
            let html = '';
            for (const f of res.feedback) {
                const date = new Date(f.timestamp * 1000).toLocaleString('de-DE');
                html += '<div style="border-bottom:1px solid #444;padding:0.5rem 0;">';
                html += '<strong>User ' + f.user_id + '</strong> <small>(' + date + ')</small><br>';
                html += '<span style="color:#aaa;">' + (f.feedback || 'Kein Text') + '</span></div>';
            }
            retentionFeedbackList.innerHTML = html;
        } catch (err) {
            retentionFeedbackList.innerHTML = '<p style="color:#c00;">Fehler: ' + err.message + '</p>';
        }
    }

    window.sendRetentionMsg = async function(userId, guildId) {
        if (!confirm('Wirklich DM an User ' + userId + ' senden?')) return;
        try {
            const res = await apiFetch('/api/retention/send/' + userId, {
                method: 'POST',
                body: JSON.stringify({ guild_id: guildId })
            });
            if (res.success) {
                log('DM gesendet an User ' + userId, 'success');
            } else {
                log('DM fehlgeschlagen für User ' + userId, 'error');
            }
            loadRetentionStats();
            loadRetentionUsers();
        } catch (err) {
            log('Retention send failed: ' + err.message, 'error');
        }
    };

    retentionRefreshBtn?.addEventListener('click', () => {
        loadRetentionStats();
        loadRetentionUsers();
        loadRetentionFeedback();
        log('Retention data refreshed', 'info');
    });

    retentionRunCheckBtn?.addEventListener('click', async () => {
        if (!confirm('Retention-Check jetzt ausführen? Dies sendet DMs an alle eligible User!')) return;
        retentionRunCheckBtn.disabled = true;
        retentionRunCheckBtn.textContent = 'Läuft...';
        try {
            const res = await apiFetch('/api/retention/run-check', { method: 'POST' });
            log('Retention-Check: ' + res.sent + '/' + res.total_checked + ' gesendet', 'success');
            loadRetentionStats();
            loadRetentionUsers();
        } catch (err) {
            log('Retention-Check failed: ' + err.message, 'error');
        } finally {
            retentionRunCheckBtn.disabled = false;
            retentionRunCheckBtn.textContent = 'Jetzt Check ausführen';
        }
    });

    // Initial load
    loadRetentionStats();
    loadRetentionUsers();
    loadRetentionFeedback();
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
        self._restart_lock = asyncio.Lock()
        self._restart_task: Optional[asyncio.Task] = None
        self._last_restart: Dict[str, Any] = {"at": None, "ok": None, "error": None}
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
        self._steam_return_url = self._derive_steam_return_url()
        self._health_cache: List[Dict[str, Any]] = []
        self._health_cache_expiry = 0.0
        self._health_cache_lock = asyncio.Lock()
        self._health_cache_ttl = self._parse_positive_float(
            os.getenv("DASHBOARD_HEALTHCHECK_CACHE_SECONDS"),
            default=30.0,
            env_name="DASHBOARD_HEALTHCHECK_CACHE_SECONDS",
        )
        self._health_timeout = self._parse_positive_float(
            os.getenv("DASHBOARD_HEALTHCHECK_TIMEOUT_SECONDS"),
            default=6.0,
            env_name="DASHBOARD_HEALTHCHECK_TIMEOUT_SECONDS",
        )
        self._health_targets = self._build_health_targets()

    @staticmethod
    def _sanitize(value: Any) -> Any:
        """Recursively normalise values so the JSON payload never emits NaN/Infinity."""
        if isinstance(value, dict):
            return {key: DashboardServer._sanitize(val) for key, val in value.items()}
        if isinstance(value, list):
            return [DashboardServer._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [DashboardServer._sanitize(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _safe_log_value(value: Any) -> str:
        """
        Sanitize values before logging to avoid log injection via crafted newlines.
        """
        text = "" if value is None else str(value)
        return text.replace("\r", "\\r").replace("\n", "\\n")

    def _json(self, payload: Any, **kwargs: Any) -> web.Response:
        return web.json_response(self._sanitize(payload), **kwargs)

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
                    web.post("/api/dashboard/restart", self._handle_dashboard_restart),
                    web.post("/api/cogs/reload", self._handle_reload),
                    web.post("/api/cogs/load", self._handle_load),
                    web.post("/api/cogs/unload", self._handle_unload),
                    web.post("/api/cogs/reload-all", self._handle_reload_all),
                    web.post("/api/cogs/reload-namespace", self._handle_reload_namespace),
                    web.post("/api/cogs/block", self._handle_block),
                    web.post("/api/cogs/unblock", self._handle_unblock),
                    web.get("/api/voice-stats", self._handle_voice_stats),
                    web.get("/api/voice-history", self._handle_voice_history),
                    web.post("/api/cogs/discover", self._handle_discover),
                    web.get("/api/standalone", self._handle_standalone_list),
                    web.get("/api/standalone/{key}/logs", self._handle_standalone_logs),
                    web.post("/api/standalone/{key}/start", self._handle_standalone_start),
                    web.post("/api/standalone/{key}/stop", self._handle_standalone_stop),
                    web.post("/api/standalone/{key}/restart", self._handle_standalone_restart),
                    web.post("/api/standalone/{key}/autostart", self._handle_standalone_autostart),
                    web.post("/api/standalone/{key}/command", self._handle_standalone_command),
                    # Retention API
                    web.get("/api/retention/stats", self._handle_retention_stats),
                    web.get("/api/retention/inactive-users", self._handle_retention_inactive_users),
                    web.get("/api/retention/feedback", self._handle_retention_feedback),
                    web.post("/api/retention/send/{user_id}", self._handle_retention_send),
                    web.post("/api/retention/run-check", self._handle_retention_run_check),
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

    async def _restart_dashboard(self) -> Dict[str, Any]:
        # Allow the response to be flushed before we tear the server down.
        await asyncio.sleep(0.25)
        stop_error: Optional[str] = None
        try:
            await self.stop()
        except Exception as exc:  # pragma: no cover - defensive restart path
            stop_error = str(exc)
            logging.exception("Stopping dashboard before restart failed: %s", exc)

        await asyncio.sleep(0.1)

        try:
            await self.start()
            result: Dict[str, Any] = {
                "ok": stop_error is None,
                "listen_url": self._listen_base_url,
                "public_url": self._public_base_url,
            }
            if stop_error:
                result["error"] = stop_error
        except Exception as exc:  # pragma: no cover - defensive restart path
            logging.exception("Dashboard start failed during restart: %s", exc)
            result = {"ok": False, "error": str(exc)}

        self._last_restart = {
            "ok": result.get("ok"),
            "error": result.get("error"),
            "at": _dt.datetime.utcnow().isoformat() + "Z",
        }
        return result

    def _on_restart_finished(self, task: asyncio.Task) -> None:
        try:
            result = task.result()
            if isinstance(result, dict) and not result.get("ok", True):
                logging.warning("Dashboard restart finished with errors: %s", result.get("error"))
            else:
                logging.info("Dashboard restart completed")
        except Exception:  # pragma: no cover - defensive restart path
            logging.exception("Dashboard restart task crashed")
        finally:
            self._restart_task = None

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

    @staticmethod
    def _parse_positive_float(raw: Optional[str], *, default: float, env_name: str) -> float:
        if raw is None:
            return default
        value = raw.strip()
        if not value:
            return default
        try:
            parsed = float(value)
        except ValueError:
            logging.warning("%s '%s' invalid – using default %.1fs", env_name, raw, default)
            return default
        if parsed <= 0:
            logging.warning("%s '%s' must be > 0 – using default %.1fs", env_name, raw, default)
            return default
        return parsed

    @staticmethod
    def _coerce_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return None

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

    def _derive_steam_return_url(self) -> Optional[str]:
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        if not base:
            return None
        path = (os.getenv("STEAM_RETURN_PATH") or "/steam/return").strip() or "/steam/return"
        path = "/" + path.lstrip("/")
        return f"{base}{path}"

    def _build_health_targets(self) -> List[Dict[str, Any]]:
        targets: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()

        def _append_query_param(url: str, key: str, value: str) -> str:
            try:
                parsed = urlparse(url)
            except Exception:
                return url
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if query.get(key) == value:
                return url
            query[key] = value
            return urlunparse(parsed._replace(query=urlencode(query)))

        def _add_target(
            label: str,
            url: str,
            *,
            key: Optional[str] = None,
            method: str = "GET",
        ) -> None:
            safe_url = (url or "").strip()
            if not safe_url:
                return
            if safe_url.startswith("http://") or safe_url.startswith("https://"):
                try:
                    safe_url = self._normalize_public_url(safe_url, default_scheme=self._scheme)
                except Exception as exc:
                    logging.warning("Healthcheck URL '%s' invalid (%s) – skipping entry", url, exc)
                    return
            safe_label = (label or safe_url).strip() or safe_url
            safe_method = (method or "GET").strip().upper() or "GET"
            key_base = (key or self._slugify_health_key(safe_label)).strip() or "health"
            unique_key = key_base
            suffix = 2
            while unique_key in seen_keys:
                unique_key = f"{key_base}-{suffix}"
                suffix += 1
            seen_keys.add(unique_key)

            entry: Dict[str, Any] = {
                "key": unique_key,
                "label": safe_label,
                "url": safe_url,
                "method": safe_method,
            }
            targets.append(entry)

        if self._twitch_dashboard_href:
            _add_target("Twitch Dashboard", self._twitch_dashboard_href, key="twitch-dashboard")
        if self._steam_return_url:
            steam_health_url = _append_query_param(self._steam_return_url, "healthcheck", "1")
            _add_target("Steam OAuth Callback", steam_health_url, key="steam-oauth-callback")

        extra_raw = (
            os.getenv("DASHBOARD_HEALTHCHECKS")
            or os.getenv("DASHBOARD_HEALTHCHECK_URLS")
            or os.getenv("MASTER_HEALTHCHECK_URLS")
            or ""
        ).strip()
        if extra_raw:
            for extra in self._parse_healthcheck_env(extra_raw):
                _add_target(
                    extra.get("label") or extra.get("name") or extra.get("title") or extra.get("url", ""),
                    extra.get("url", ""),
                    key=extra.get("key"),
                    method=extra.get("method", "GET"),
                )

        return targets

    def _parse_healthcheck_env(self, raw: str) -> List[Dict[str, Any]]:
        trimmed = raw.strip()
        if not trimmed:
            return []
        try:
            loaded = json.loads(trimmed)
        except json.JSONDecodeError:
            entries: List[Dict[str, Any]] = []
            normalized_raw = trimmed.replace(";", "\n")
            for line in normalized_raw.splitlines():
                item = line.strip()
                if not item:
                    continue
                parts = [part.strip() for part in item.split("|")]
                if len(parts) == 1:
                    label = parts[0]
                    url = parts[0]
                    method = "GET"
                elif len(parts) == 2:
                    label, url = parts
                    method = "GET"
                else:
                    label, method, url = parts[0], parts[1], parts[2]
                    method = method.strip().upper() or "GET"
                if not url:
                    continue
                entries.append({"label": label or url, "url": url, "method": method})
            return entries

        entries: List[Dict[str, Any]] = []
        if isinstance(loaded, dict):
            loaded = [loaded]
        if not isinstance(loaded, list):
            logging.warning("DASHBOARD_HEALTHCHECKS JSON must be a list or object.")
            return entries
        for idx, item in enumerate(loaded):
            if not isinstance(item, dict):
                logging.warning("Healthcheck entry #%s must be an object – skipping", idx)
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                logging.warning("Healthcheck entry #%s missing 'url' – skipping", idx)
                continue
            method = str(item.get("method") or "GET").strip().upper() or "GET"
            label = str(
                item.get("label")
                or item.get("name")
                or item.get("title")
                or url
            ).strip() or url
            entry: Dict[str, Any] = {
                "label": label,
                "url": url,
                "method": method,
            }
            for optional_key in ("key", "timeout", "expect_status", "allow_redirects", "verify_ssl"):
                if optional_key in item:
                    entry[optional_key] = item[optional_key]
            entries.append(entry)
        return entries

    @staticmethod
    def _slugify_health_key(value: str) -> str:
        slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
        pieces = [part for part in slug.split("-") if part]
        return "-".join(pieces) or "health"

    async def _handle_index(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        html_text = _HTML_TEMPLATE.replace("{{TWITCH_URL}}", self._twitch_dashboard_href)
        return web.Response(text=html_text, content_type="text/html")

    def _voice_cog(self) -> Any:
        """
        Try to retrieve the VoiceActivityTrackerCog instance without importing it directly.
        Falls back to name matching to stay resilient if the cog isn't loaded.
        """
        try:
            cog = self.bot.get_cog("VoiceActivityTrackerCog")
            if cog:
                return cog
        except Exception:
            logging.getLogger(__name__).debug(
                "VoiceActivityTrackerCog lookup failed via direct get_cog", exc_info=True
            )
        for cog in self.bot.cogs.values():
            if cog.__class__.__name__ == "VoiceActivityTrackerCog":
                return cog
        return None

    def _resolve_display_names(self, user_ids: Iterable[int]) -> Dict[int, str]:
        names: Dict[int, str] = {}
        for uid in {u for u in user_ids if u}:
            display_name: Optional[str] = None
            for guild in self.bot.guilds:
                try:
                    member = guild.get_member(uid)
                except Exception:
                    member = None
                if member:
                    display_name = getattr(member, "display_name", None) or getattr(member, "name", None)
                    break
            if not display_name:
                user = self.bot.get_user(uid)
                if user:
                    display_name = getattr(user, "display_name", None) or getattr(user, "name", None)
            names[uid] = display_name or f"User {uid}"
        return names

    async def _collect_live_voice_sessions(self) -> List[Dict[str, Any]]:
        cog = self._voice_cog()
        if not cog:
            return []
        try:
            voice_sessions = dict(getattr(cog, "voice_sessions", {}) or {})
        except Exception:
            voice_sessions = {}
        now = _dt.datetime.utcnow()
        sessions: List[Dict[str, Any]] = []
        for session in voice_sessions.values():
            user_id = session.get("user_id")
            start_time = session.get("start_time")
            guild_id = session.get("guild_id")
            channel_id = session.get("channel_id")
            channel_name = session.get("channel_name")
            if not channel_name and guild_id and channel_id:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        channel_name = getattr(channel, "name", None) or channel_name
            started_at: Optional[str]
            if isinstance(start_time, _dt.datetime):
                try:
                    started_at = start_time.replace(tzinfo=_dt.timezone.utc).isoformat()
                except Exception:
                    started_at = start_time.isoformat()
                duration_seconds = max(0, int((now - start_time).total_seconds()))
            else:
                started_at = None
                duration_seconds = 0
            sessions.append(
                {
                    "user_id": str(user_id),  # Convert to string to preserve precision in JavaScript
                    "guild_id": str(guild_id) if guild_id else None,
                    "channel_id": str(channel_id) if channel_id else None,
                    "channel_name": channel_name,
                    "duration_seconds": duration_seconds,
                    "peak_users": session.get("peak_users") or 1,
                    "started_at": started_at,
                }
            )
        sessions.sort(key=lambda s: s.get("duration_seconds", 0), reverse=True)
        return sessions

    async def _handle_voice_stats(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        raw_limit = request.query.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else 10
            if limit <= 0:
                raise ValueError
            limit = min(limit, 50)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer (max 50)")

        try:
            summary_row = db.query_one(
                """
                SELECT COUNT(*) AS user_count,
                       SUM(total_seconds) AS total_seconds,
                       SUM(total_points) AS total_points,
                       MAX(last_update) AS last_update
                FROM voice_stats
                """
            )
            top_time_rows = db.query_all(
                """
                SELECT user_id, total_seconds, total_points, last_update
                FROM voice_stats
                ORDER BY total_seconds DESC, total_points DESC
                LIMIT ?
                """,
                (limit,),
            )
            top_point_rows = db.query_all(
                """
                SELECT user_id, total_seconds, total_points, last_update
                FROM voice_stats
                ORDER BY total_points DESC, total_seconds DESC
                LIMIT ?
                """,
                (limit,),
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to load voice stats: %s", exc)
            raise web.HTTPInternalServerError(text="Voice stats unavailable") from exc

        live_sessions = await self._collect_live_voice_sessions()
        user_ids = set()
        for row in top_time_rows + top_point_rows:
            try:
                uid = row["user_id"]
            except Exception:
                uid = None
            if uid:
                user_ids.add(uid)
        for sess in live_sessions:
            uid = sess.get("user_id")
            if uid:
                user_ids.add(uid)
        name_map = self._resolve_display_names(user_ids)

        def _map_row(row: Any) -> Dict[str, Any]:
            uid = row["user_id"]
            return {
                "user_id": str(uid),  # Convert to string to preserve precision in JavaScript
                "display_name": name_map.get(uid, f"User {uid}"),
                "total_seconds": int(row["total_seconds"] or 0),
                "total_points": int(row["total_points"] or 0),
                "last_update": row["last_update"],
            }

        summary = {
            "tracked_users": int(summary_row["user_count"] or 0) if summary_row else 0,
            "total_seconds": int(summary_row["total_seconds"] or 0) if summary_row else 0,
            "total_points": int(summary_row["total_points"] or 0) if summary_row else 0,
            "last_update": summary_row["last_update"] if summary_row else None,
        }
        if summary["tracked_users"] > 0:
            summary["avg_seconds_per_user"] = summary["total_seconds"] / summary["tracked_users"]
        else:
            summary["avg_seconds_per_user"] = 0

        live_summary = {
            "active_sessions": len(live_sessions),
            "total_seconds": sum(sess.get("duration_seconds", 0) for sess in live_sessions),
        }
        for sess in live_sessions:
            uid = sess.get("user_id")
            if uid:
                sess["display_name"] = name_map.get(uid, f"User {uid}")

        payload = {
            "summary": summary,
            "top_by_time": [_map_row(r) for r in top_time_rows],
            "top_by_points": [_map_row(r) for r in top_point_rows],
            "live": {
                "summary": live_summary,
                "sessions": live_sessions,
            },
        }
        return self._json(payload)

    async def _handle_voice_history(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        range_raw = request.query.get("range")
        top_raw = request.query.get("top")
        mode_raw = request.query.get("mode") or "hour"
        user_raw = request.query.get("user_id")
        try:
            days = int(range_raw) if range_raw else 14
            if days <= 0:
                raise ValueError
            days = min(days, 90)
        except ValueError:
            raise web.HTTPBadRequest(text="range must be a positive integer (days, max 90)")
        try:
            top_limit = int(top_raw) if top_raw else 10
            if top_limit <= 0:
                raise ValueError
            top_limit = min(top_limit, 50)
        except ValueError:
            raise web.HTTPBadRequest(text="top must be a positive integer (max 50)")
        mode = mode_raw.strip().lower()
        if mode not in {"hour", "day", "week", "month"}:
            raise web.HTTPBadRequest(text="mode must be one of hour, day, week, month")
        user_id: Optional[int] = None
        if user_raw:
            try:
                user_id = int(user_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="user_id must be an integer")

        cutoff = f"-{days} day"
        where_clauses = ["started_at >= datetime('now', ?)"]
        params: list[Any] = [cutoff]
        if user_id is not None:
            where_clauses.append("user_id = ?")
            params.append(user_id)
        where_sql = " AND ".join(where_clauses)

        def _group_sql() -> str:
            if mode == "hour":
                return "strftime('%H', started_at)"
            if mode == "day":
                return "strftime('%w', started_at)"
            if mode == "week":
                return "strftime('%Y-%W', started_at)"
            return "strftime('%Y-%m', started_at)"

        active_top_rows: List[Any] = []
        try:
            daily_rows = db.query_all(
                """
                SELECT date(started_at) AS day,
                       SUM(duration_seconds) AS total_seconds,
                       COUNT(*) AS sessions,
                       COUNT(DISTINCT user_id) AS users
                FROM voice_session_log
                WHERE started_at >= datetime('now', ?)
                GROUP BY date(started_at)
                ORDER BY day DESC
                """,
                (cutoff,),
            )
            top_users_rows = db.query_all(
                """
                SELECT user_id,
                       MAX(display_name) AS display_name,
                       SUM(duration_seconds) AS total_seconds,
                       SUM(points) AS total_points,
                       COUNT(*) AS sessions
                FROM voice_session_log
                WHERE """ + where_sql + """
                GROUP BY user_id
                ORDER BY total_seconds DESC, total_points DESC
                LIMIT ?
                """,
                (*params, top_limit),
            )
            active_top_rows = db.query_all(
                """
                SELECT user_id,
                       MAX(display_name) AS display_name,
                       SUM(duration_seconds) AS total_seconds,
                       SUM(points) AS total_points,
                       COUNT(*) AS sessions,
                       MAX(started_at) AS last_seen
                FROM voice_session_log
                WHERE started_at >= datetime('now', '-14 day')
                GROUP BY user_id
                ORDER BY total_seconds DESC, total_points DESC
                LIMIT ?
                """,
                (top_limit,),
            )
            hourly_rows = db.query_all(
                """
                SELECT """ + _group_sql() + """ AS bucket,
                       SUM(duration_seconds) AS total_seconds,
                       COUNT(*) AS sessions,
                       COUNT(DISTINCT user_id) AS users,
                       SUM(COALESCE(peak_users, 0)) AS sum_peak
                FROM voice_session_log
                WHERE """ + where_sql + """
                GROUP BY bucket
                ORDER BY bucket
                """,
                tuple(params),
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to load voice history: %s", exc)
            raise web.HTTPInternalServerError(text="Voice history unavailable") from exc

        user_ids: set[int] = set()
        for row in top_users_rows:
            try:
                uid = row["user_id"]
            except Exception:
                uid = None
            if uid:
                user_ids.add(uid)
        for row in active_top_rows:
            try:
                uid = row["user_id"]
            except Exception:
                uid = None
            if uid:
                user_ids.add(uid)
        if user_id:
            user_ids.add(user_id)
        name_map = self._resolve_display_names(user_ids)

        def _map_top_user(row: Any) -> Dict[str, Any]:
            uid = row["user_id"]
            return {
                "user_id": str(uid),  # Convert to string to preserve precision in JavaScript
                "display_name": row["display_name"] or name_map.get(uid, f"User {uid}"),
                "total_seconds": int(row["total_seconds"] or 0),
                "total_points": int(row["total_points"] or 0),
                "sessions": int(row["sessions"] or 0),
            }

        def _map_active_user(row: Any) -> Dict[str, Any]:
            uid = row["user_id"]
            return {
                "user_id": str(uid),  # Convert to string to preserve precision in JavaScript
                "display_name": row["display_name"] or name_map.get(uid, f"User {uid}"),
                "total_seconds": int(row["total_seconds"] or 0),
                "total_points": int(row["total_points"] or 0),
                "sessions": int(row["sessions"] or 0),
                "last_seen": row["last_seen"] if hasattr(row, "keys") and "last_seen" in row.keys() else None,
            }

        daily = [
            {
                "day": row["day"],
                "total_seconds": int(row["total_seconds"] or 0),
                "sessions": int(row["sessions"] or 0),
                "users": int(row["users"] or 0),
            }
            for row in daily_rows
        ]

        buckets = []
        for row in hourly_rows:
            sessions_count = int(row["sessions"] or 0)
            buckets.append(
                {
                    "label": row["bucket"],
                    "total_seconds": int(row["total_seconds"] or 0),
                    "sessions": sessions_count,
                    "users": int(row["users"] or 0),
                    "avg_peak": (
                        (int(row["sum_peak"] or 0) / sessions_count)
                        if sessions_count > 0
                        else 0
                    ),
                }
            )

        if mode == "hour":
            existing = {b["label"]: b for b in buckets}
            buckets = []
            for h in range(24):
                key = str(h).zfill(2)
                buckets.append(
                    existing.get(
                        key,
                        {"label": key, "total_seconds": 0, "sessions": 0, "users": 0, "avg_peak": 0},
                    )
                )

        if mode == "day":
            existing = {b["label"]: b for b in buckets}
            buckets = []
            weekdays = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"]
            for day in range(7):
                key = str(day)
                data = existing.get(key, {})
                buckets.append({
                    "label": weekdays[day],
                    "total_seconds": data.get("total_seconds", 0),
                    "sessions": data.get("sessions", 0),
                    "users": data.get("users", 0),
                    "avg_peak": data.get("avg_peak", 0),
                })

        payload = {
            "range_days": days,
            "mode": mode,
            "user": ({"user_id": str(user_id), "display_name": name_map.get(user_id)} if user_id else None),
            "daily": daily,
            "top_users": [_map_top_user(r) for r in top_users_rows],
            "active_users": [_map_active_user(r) for r in active_top_rows],
            "buckets": buckets,
        }
        return self._json(payload)

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

        latency = getattr(bot, "latency", None)
        if latency is not None and math.isfinite(latency):
            latency_ms = round(latency * 1000, 2)
        else:
            latency_ms = None

        restart_in_progress = bool(self._restart_task and not self._restart_task.done())
        last_restart = self._last_restart if any(self._last_restart.values()) else None

        payload = {
            "bot": {
                "user": str(bot.user) if bot.user else None,
                "id": getattr(bot.user, "id", None),
                "uptime": uptime,
                "guilds": len(bot.guilds),
                "latency_ms": latency_ms,
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
                "running": self._started,
                "restart_in_progress": restart_in_progress,
                "last_restart": last_restart,
            },
            "settings": {
                "per_cog_unload_timeout": bot.per_cog_unload_timeout,
            },
            "health": await self._collect_health_checks(),
            "standalone": await self._collect_standalone_snapshot(),
        }
        return self._json(payload)

    async def _handle_dashboard_restart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        if self._restart_task and not self._restart_task.done():
            return self._json({"ok": True, "message": "Dashboard restart already running"})

        self._restart_task = asyncio.create_task(self._restart_dashboard())
        self._restart_task.add_done_callback(self._on_restart_finished)
        return self._json({"ok": True, "message": "Dashboard restart scheduled"})

    async def _collect_health_checks(self) -> List[Dict[str, Any]]:
        if not self._health_targets:
            return []
        now = asyncio.get_running_loop().time()
        if self._health_cache and now < self._health_cache_expiry:
            return self._health_cache
        async with self._health_cache_lock:
            if self._health_cache and now < self._health_cache_expiry:
                return self._health_cache
            data = await self._refresh_health_checks()
            self._health_cache = data
            self._health_cache_expiry = now + self._health_cache_ttl
            return data

    async def _refresh_health_checks(self) -> List[Dict[str, Any]]:
        timeout = ClientTimeout(total=self._health_timeout)
        async with ClientSession(timeout=timeout) as session:
            tasks = [self._probe_health_target(session, target) for target in self._health_targets]
            return await asyncio.gather(*tasks)

    async def _probe_health_target(
        self,
        session: ClientSession,
        target: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = target.get("url") or ""
        method = (target.get("method") or "GET").strip().upper() or "GET"
        allow_redirects_value = target.get("allow_redirects")
        allow_redirects = True
        coerced_redirects = self._coerce_bool(allow_redirects_value)
        if coerced_redirects is not None:
            allow_redirects = coerced_redirects

        verify_ssl_value = target.get("verify_ssl")
        ssl_param: Any = None
        coerced_ssl = self._coerce_bool(verify_ssl_value)
        if coerced_ssl is False:
            ssl_param = False

        timeout_value = target.get("timeout")
        request_timeout = None
        if timeout_value is not None:
            try:
                parsed_timeout = float(timeout_value)
                if parsed_timeout > 0:
                    request_timeout = ClientTimeout(total=parsed_timeout)
            except (TypeError, ValueError):
                logging.warning(
                    "Healthcheck target '%s' timeout '%s' invalid – falling back to default",
                    target.get("label") or target.get("key") or url,
                    timeout_value,
                )

        expected_status = target.get("expect_status")

        def _status_ok(status_code: int) -> bool:
            if expected_status is None:
                return 200 <= status_code < 400
            if isinstance(expected_status, int):
                return status_code == expected_status
            if isinstance(expected_status, (list, tuple, set)):
                try:
                    allowed = {int(item) for item in expected_status}
                except (TypeError, ValueError):
                    allowed = set(expected_status)
                return status_code in allowed
            if isinstance(expected_status, str):
                stripped = expected_status.strip()
                if stripped.isdigit():
                    return status_code == int(stripped)
            return 200 <= status_code < 400

        start = time.perf_counter()
        status: Optional[int] = None
        reason: Optional[str] = None
        ok = False
        error: Optional[str] = None
        resolved_url = url
        body_excerpt: Optional[str] = None

        request_kwargs: Dict[str, Any] = {"allow_redirects": allow_redirects}
        if ssl_param is not None:
            request_kwargs["ssl"] = ssl_param
        if request_timeout:
            request_kwargs["timeout"] = request_timeout

        try:
            async with session.request(method, url, **request_kwargs) as resp:
                status = resp.status
                reason = resp.reason
                resolved_url = str(resp.url)
                ok = _status_ok(status)
                if not ok:
                    try:
                        text = await resp.text()
                    except Exception:
                        text = ""
                    if text:
                        body_excerpt = text[:280]
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        result: Dict[str, Any] = {
            "key": target.get("key"),
            "label": target.get("label") or target.get("key") or url,
            "url": url,
            "method": method,
            "ok": ok,
            "status": status,
            "reason": reason,
            "latency_ms": duration_ms,
            "checked_at": _dt.datetime.utcnow().isoformat() + "Z",
        }
        if resolved_url and resolved_url != url:
            result["resolved_url"] = resolved_url
        if error:
            result["error"] = error
        if body_excerpt and not ok:
            result["body_excerpt"] = body_excerpt
        return result


    async def _collect_standalone_snapshot(self) -> List[Dict[str, Any]]:
        manager = getattr(self.bot, "standalone_manager", None)
        if not manager:
            return []
        try:
            return await manager.snapshot()
        except Exception as exc:
            logging.getLogger(__name__).error("Standalone snapshot failed: %s", exc)
            return []

    def _require_standalone_manager(self):
        manager = getattr(self.bot, "standalone_manager", None)
        if not manager:
            raise web.HTTPNotFound(text="Standalone manager unavailable")
        return manager

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

        def is_manageable(path: str) -> bool:
            if path == "cogs":
                return False
            return path in active or path in discovered or path in status_map

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
            manageable_dir = is_manageable(module_path)
            loaded_dir = module_path in active
            discovered_dir = module_path in discovered
            is_package = (
                module_path in discovered
                or module_path in status_map
                or module_path in active
            ) and module_path != "cogs"

            module_count = 1 if is_package else 0
            loaded_count = 1 if is_package and loaded_dir else 0
            discovered_count = 1 if is_package and discovered_dir else 0

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
                manageable_child = is_manageable(mod_path)
                status_child = node_status(mod_path, blocked=blocked_child) or "not_discovered"
                child = {
                    "type": "module",
                    "name": entry.stem,
                    "path": mod_path,
                    "blocked": blocked_child,
                    "loaded": loaded_child,
                    "discovered": discovered_child,
                    "manageable": manageable_child,
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
                "manageable": manageable_dir,
                "loaded": loaded_dir,
                "discovered": discovered_dir,
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
                "manageable": False,
                "loaded": False,
                "discovered": False,
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
        return self._json({"results": results})

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
        return self._json({"results": results})

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
        return self._json({"results": results})

    async def _handle_reload_all(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        async with self._lock:
            ok, summary = await self.bot.reload_all_cogs_with_discovery()
        if ok:
            return self._json({"ok": True, "summary": summary})
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
            return self._json(
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
        return self._json({"ok": ok, "results": results, "message": message})

    async def _handle_discover(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        before = set(self.bot.cogs_list)
        self.bot.auto_discover_cogs()
        after = set(self.bot.cogs_list)
        new = sorted(after - before)
        return self._json({"ok": True, "new": new, "count": len(after)})

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
        return self._json(
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
        return self._json(
            {
                "ok": True,
                "namespace": namespace,
                "changed": changed,
                "message": message,
            }
        )


    async def _handle_standalone_list(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        data = await self._collect_standalone_snapshot()
        return self._json({"bots": data})

    async def _handle_standalone_logs(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        limit_raw = request.query.get("limit")
        try:
            limit = int(limit_raw) if limit_raw else 200
            if limit <= 0:
                raise ValueError
            limit = min(limit, 1000)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer <= 1000")
        try:
            logs = await manager.logs(key, limit=limit)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise
        return self._json({"logs": logs})

    async def _handle_standalone_start(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.start(key)
        except Exception as exc:
            if StandaloneAlreadyRunning and isinstance(exc, StandaloneAlreadyRunning):
                status = await manager.status(key)
            elif StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            elif StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when starting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            else:
                logging.getLogger(__name__).exception(
                    "Unexpected error when starting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_stop(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.stop(key)
        except Exception as exc:
            if StandaloneNotRunning and isinstance(exc, StandaloneNotRunning):
                status = await manager.status(key)
            elif StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            elif StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when stopping standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            else:
                logging.getLogger(__name__).exception(
                    "Unexpected error when stopping standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_restart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.restart(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            if StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when restarting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            logging.getLogger(__name__).exception(
                "Unexpected error when restarting standalone bot (key=%s)", safe_key
            )
            raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_autostart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        try:
            manager.config(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, web.HTTPException):
                raise
            raise web.HTTPBadRequest(text="Invalid JSON payload") from exc

        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Payload must be a JSON object")

        enabled_raw = payload.get("enabled")
        enabled: Optional[bool]
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        elif isinstance(enabled_raw, (int, float)):
            enabled = bool(enabled_raw)
        elif isinstance(enabled_raw, str):
            lowered = enabled_raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                enabled = True
            elif lowered in {"0", "false", "no", "off"}:
                enabled = False
            else:
                enabled = None
        else:
            enabled = None

        if enabled is None:
            raise web.HTTPBadRequest(text="'enabled' must be a boolean")

        try:
            status = await manager.set_autostart(key, enabled)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        return self._json({"standalone": status})

    async def _handle_standalone_command(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            manager.config(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        payload = await request.json()
        command = str(payload.get("command") or "").strip()
        if not command:
            raise web.HTTPBadRequest(text="'command' is required")
        command_payload = payload.get("payload")
        try:
            payload_json = json.dumps(command_payload, ensure_ascii=False) if command_payload is not None else None
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="payload must be JSON-serializable")

        db.execute(
            "INSERT INTO standalone_commands(bot, command, payload, status, created_at) "
            "VALUES(?, ?, ?, 'pending', CURRENT_TIMESTAMP)",
            (key, command, payload_json),
        )
        row = db.query_one("SELECT last_insert_rowid()")
        command_id = row[0] if row else None

        try:
            await manager.ensure_running(key)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Could not ensure %s running after command enqueue: %s",
                safe_key,
                self._safe_log_value(exc),
            )

        status = await manager.status(key)
        return self._json(
            {
                "queued": command_id,
                "standalone": status,
            },
            status=201,
        )

    # ========= Retention API Handlers =========

    def _get_retention_cog(self):
        """Holt den UserRetentionCog wenn verfügbar."""
        if not self.bot:
            return None
        return self.bot.get_cog("UserRetentionCog")

    async def _handle_retention_stats(self, request: web.Request) -> web.Response:
        """GET /api/retention/stats - Statistiken abrufen"""
        self._check_auth(request, required=bool(self.token))
        cog = self._get_retention_cog()
        if not cog:
            return self._json({"error": "UserRetentionCog nicht geladen"}, status=503)
        try:
            stats = await cog.get_retention_stats()
            return self._json(stats)
        except Exception as e:
            logging.getLogger(__name__).error(f"Retention stats error: {e}")
            return self._json({"error": str(e)}, status=500)

    async def _handle_retention_inactive_users(self, request: web.Request) -> web.Response:
        """GET /api/retention/inactive-users - Inaktive User auflisten"""
        self._check_auth(request, required=bool(self.token))
        cog = self._get_retention_cog()
        if not cog:
            return self._json({"error": "UserRetentionCog nicht geladen"}, status=503)
        try:
            limit = int(request.query.get("limit", 50))
            users = await cog.get_inactive_users_list(limit)
            return self._json({"users": users})
        except Exception as e:
            logging.getLogger(__name__).error(f"Retention inactive users error: {e}")
            return self._json({"error": str(e)}, status=500)

    async def _handle_retention_feedback(self, request: web.Request) -> web.Response:
        """GET /api/retention/feedback - Feedbacks abrufen"""
        self._check_auth(request, required=bool(self.token))
        cog = self._get_retention_cog()
        if not cog:
            return self._json({"error": "UserRetentionCog nicht geladen"}, status=503)
        try:
            limit = int(request.query.get("limit", 20))
            feedback = await cog.get_feedback_list(limit)
            return self._json({"feedback": feedback})
        except Exception as e:
            logging.getLogger(__name__).error(f"Retention feedback error: {e}")
            return self._json({"error": str(e)}, status=500)

    async def _handle_retention_send(self, request: web.Request) -> web.Response:
        """POST /api/retention/send/{user_id} - Nachricht an einzelnen User senden"""
        self._check_auth(request, required=bool(self.token))
        cog = self._get_retention_cog()
        if not cog:
            return self._json({"error": "UserRetentionCog nicht geladen"}, status=503)
        try:
            user_id = int(request.match_info.get("user_id", 0))
            payload = await request.json()
            guild_id = int(payload.get("guild_id", 0))
            if not user_id or not guild_id:
                return self._json({"error": "user_id und guild_id erforderlich"}, status=400)
            result = await cog.send_message_to_user(user_id, guild_id)
            return self._json(result)
        except Exception as e:
            logging.getLogger(__name__).error(f"Retention send error: {e}")
            return self._json({"error": str(e)}, status=500)

    async def _handle_retention_run_check(self, request: web.Request) -> web.Response:
        """POST /api/retention/run-check - Retention-Check manuell ausführen"""
        self._check_auth(request, required=bool(self.token))
        cog = self._get_retention_cog()
        if not cog:
            return self._json({"error": "UserRetentionCog nicht geladen"}, status=503)
        try:
            result = await cog.run_retention_check_now()
            return self._json(result)
        except Exception as e:
            logging.getLogger(__name__).error(f"Retention run check error: {e}")
            return self._json({"error": str(e)}, status=500)


if TYPE_CHECKING:  # pragma: no cover - avoid runtime dependency cycle
    from main_bot import MasterBot
