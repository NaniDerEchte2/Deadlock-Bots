# Agent-Autopilot (Claude Code 2.1.52)
- Nutze je nach Aufgabe automatisch einen Agent aus `.claude/agents`:
  - requirements-engineer.md → Specs/User Stories
  - solution-architect.md → DB- & Cog-Design
  - frontend-dev.md → Embeds/Buttons/Modals
  - backend-dev.md → Cogs + DB-Abfragen
  - qa-engineer.md → Testplan/Checks
  - devops.md → Deploy/Monitoring
- Reine Recherche: Explore-Agent (read-only) einsetzen.
- Größere/mehrschrittige Aufgaben: Plan mode (enhanced, 5 Phasen) starten.
- Tool-Nutzung (Task tool extra notes):
  - Immer absolute Windows-Pfade nennen.
  - Keine Emojis und kein „Tool:“ vor Aufrufen.
- Teamarbeit:
  - Wenn Parallelisierung hilft, TeamCreate + Task + SendMessage nutzen (TeammateTool-Regeln).
  - Idle-Meldungen sind normal; Antworten immer per SendMessage.
- Wenn keiner passt: normal ohne Zusatz-Agenten arbeiten.

## Kurz-Reminders zu neuen Claude-Code-Prompts
- Plan mode (enhanced, 5 Phasen): siehe `plan-mode-enhanced.md`.
- Task tool extra notes: absolute Windows-Pfade, keine Emojis, kein Präfix „Tool:“ vor Aufrufen (`task-tool-extra-notes.md`).
- Teammate Communication: Team-Nachrichten nur via SendMessage; Idle ist normal (`teammate-communication.md`).
- SendMessageTool: `message` gezielt, `broadcast` sparsam (`sendmessage-tool.md`).
- TeammateTool: Teams mit TeamCreate anlegen, Tasks via Task/TaskUpdate claimen; Namen statt IDs.
- Explore-Agent: reine Recherche, keine Schreibaktionen (`explore-readonly.md`).
- Tool usage policy: bevorzugt spezialisierte Tools, parallel wo sinnvoll (`tool-usage-policy.md`).
