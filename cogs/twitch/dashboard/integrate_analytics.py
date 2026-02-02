#!/usr/bin/env python3
"""
Quick Integration Script für das neue Analytics Dashboard
Führt die notwendigen Schritte zur Integration aus
"""

import os
import sys
from pathlib import Path

def print_step(step_num, title):
    """Print schöner Step-Header"""
    print(f"\n{'='*60}")
    print(f"  STEP {step_num}: {title}")
    print(f"{'='*60}\n")

def check_files_exist():
    """Prüft ob alle neuen Dateien vorhanden sind"""
    print_step(1, "Datei-Check")
    
    base_path = Path(__file__).parent
    required_files = [
        "static/js/components/KpiCard.js",
        "static/js/components/ScoreGauge.js",
        "static/js/components/ChartContainer.js",
        "static/js/components/InsightsPanel.js",
        "static/js/components/SessionTable.js",
        "static/js/components/ViewModeTabs.js",
        "static/js/components/ComparisonView.js",
        "static/js/analytics-new.js",
        "static/js/loader.js",
        "../analytics_backend_extended.py",
        "ANALYTICS_DASHBOARD_README.md"
    ]
    
    all_good = True
    for file in required_files:
        file_path = base_path / file
        if file_path.exists():
            print(f"✅ {file}")
        else:
            print(f"❌ FEHLT: {file}")
            all_good = False
    
    if all_good:
        print("\n✅ Alle Dateien vorhanden!")
        return True
    else:
        print("\n❌ Einige Dateien fehlen. Bitte erstelle sie zuerst.")
        return False

def backup_old_analytics():
    """Erstellt Backup der alten analytics.js"""
    print_step(2, "Backup erstellen")
    
    base_path = Path(__file__).parent
    old_file = base_path / "static/js/analytics.js"
    backup_file = base_path / "static/js/analytics.js.backup"
    
    if old_file.exists():
        if backup_file.exists():
            print(f"⚠️  Backup existiert bereits: {backup_file}")
            response = input("Überschreiben? (j/n): ")
            if response.lower() != 'j':
                print("❌ Backup übersprungen")
                return False
        
        import shutil
        shutil.copy2(old_file, backup_file)
        print(f"✅ Backup erstellt: {backup_file}")
        return True
    else:
        print("ℹ️  Keine alte analytics.js gefunden - kein Backup nötig")
        return True

def show_integration_instructions():
    """Zeigt Integrations-Anweisungen"""
    print_step(3, "Integration in dein System")
    
    print("""
Die folgenden Änderungen musst du MANUELL vornehmen:

1️⃣  Backend-Integration (dashboard_mixin.py oder ähnlich):
    
    ```python
    from .analytics_backend_extended import AnalyticsBackendExtended
    
    async def _streamer_analytics_data_extended(streamer_login: str, days: int):
        return await AnalyticsBackendExtended.get_comprehensive_analytics(
            streamer_login=streamer_login,
            days=days
        )
    
    # In deinem Dashboard-Setup:
    dashboard = DashboardBase(
        # ... bestehende Parameter ...
        streamer_analytics_data_cb=_streamer_analytics_data_extended,
    )
    ```

2️⃣  Template-Update (dashboard/analytics.py):
    
    Ersetze in `_build_analytics_html` den Script-Teil mit:
    
    ```python
    <!-- Load Components -->
    <script src="/twitch/static/js/components/KpiCard.js"></script>
    <script src="/twitch/static/js/components/ScoreGauge.js"></script>
    <script src="/twitch/static/js/components/ChartContainer.js"></script>
    <script src="/twitch/static/js/components/InsightsPanel.js"></script>
    <script src="/twitch/static/js/components/SessionTable.js"></script>
    <script src="/twitch/static/js/components/ViewModeTabs.js"></script>
    <script src="/twitch/static/js/components/ComparisonView.js"></script>
    
    <!-- Main App -->
    <script src="/twitch/static/js/analytics-new.js"></script>
    ```

3️⃣  API-Endpoint testen:
    
    curl "http://localhost:8766/twitch/api/analytics?days=30&partner_token=TOKEN"
    
    Sollte JSON mit "metrics", "retention_timeline", etc. zurückgeben.

4️⃣  Frontend testen:
    
    Öffne: http://localhost:8766/twitch/analytics?partner_token=TOKEN
    
    Erwarte: Modernes Dashboard mit 6 Tabs

5️⃣  Wenn alles funktioniert:
    
    mv static/js/analytics.js static/js/analytics.js.old
    mv static/js/analytics-new.js static/js/analytics.js
    
    (Oder passe Template an, um neue Datei direkt zu laden)
""")

def create_test_data_script():
    """Erstellt Test-Skript für Entwicklung"""
    print_step(4, "Test-Utilities erstellen")
    
    test_script = """
// Test-Daten für Frontend-Entwicklung
// Füge dies in Browser DevTools ein, um ohne Backend zu testen

const mockData = {
    empty: false,
    metrics: {
        retention_5m: 0.68,
        retention_10m: 0.62,
        retention_20m: 0.55,
        avg_dropoff: 0.25,
        retention_5m_trend: 5.2,
        avg_peak_viewers: 150,
        avg_avg_viewers: 85,
        total_followers_delta: 420,
        followers_per_session: 14,
        followers_per_hour: 8.5,
        peak_viewers_trend: 12.3,
        followers_trend: 18.5,
        unique_chatters_per_100: 12.5,
        avg_unique_chatters: 45,
        total_first_time_chatters: 156,
        total_returning_chatters: 340,
        chat_engagement_trend: 7.8
    },
    retention_timeline: Array.from({length: 30}, (_, i) => ({
        date: new Date(Date.now() - (29 - i) * 86400000).toISOString().split('T')[0],
        retention_5m: 65 + Math.random() * 10,
        retention_10m: 58 + Math.random() * 10,
        retention_20m: 50 + Math.random() * 10,
        dropoff: 20 + Math.random() * 15
    })),
    discovery_timeline: Array.from({length: 30}, (_, i) => ({
        date: new Date(Date.now() - (29 - i) * 86400000).toISOString().split('T')[0],
        peak_viewers: 100 + Math.floor(Math.random() * 100),
        followers_delta: 10 + Math.floor(Math.random() * 20),
        avg_viewers: 70 + Math.floor(Math.random() * 50)
    })),
    chat_timeline: Array.from({length: 30}, (_, i) => ({
        date: new Date(Date.now() - (29 - i) * 86400000).toISOString().split('T')[0],
        unique_chatters: 30 + Math.random() * 30,
        chat_per_100: 8 + Math.random() * 8,
        first_time: 5 + Math.floor(Math.random() * 10),
        returning: 20 + Math.floor(Math.random() * 25)
    })),
    sessions: Array.from({length: 20}, (_, i) => ({
        id: i,
        date: new Date(Date.now() - i * 86400000).toISOString().split('T')[0],
        startTime: `${14 + Math.floor(Math.random() * 8)}:00`,
        duration: 7200 + Math.floor(Math.random() * 7200),
        peakViewers: 100 + Math.floor(Math.random() * 100),
        avgViewers: 70 + Math.random() * 40,
        retention10m: 55 + Math.random() * 20,
        uniqueChatters: 30 + Math.floor(Math.random() * 30),
        followersStart: 1000 + i * 10,
        followersEnd: 1000 + i * 10 + Math.floor(Math.random() * 20)
    })),
    insights: [
        {
            type: "success",
            title: "Exzellente Retention",
            description: "Deine 10-Min-Retention liegt bei 62%. Das ist überdurchschnittlich!"
        },
        {
            type: "warning",
            title: "Verbesserungspotenzial: Chat",
            description: "Mit 12.5 Chattern/100 Viewern liegt dein Chat-Engagement leicht unter dem Durchschnitt."
        }
    ],
    comparison: {
        topStreamers: [
            { login: "streamer1", avgViewers: 250, peakViewers: 450 },
            { login: "du", avgViewers: 150, peakViewers: 280 },
            { login: "streamer3", avgViewers: 120, peakViewers: 220 }
        ],
        categoryAvg: {
            avgViewers: 95,
            peakViewers: 180,
            retention10m: 58,
            chatHealth: 10.5
        },
        yourStats: {
            avgViewers: 150,
            peakViewers: 280,
            retention10m: 62,
            chatHealth: 12.5
        }
    }
};

// Simuliere API-Fetch
window.originalFetch = window.fetch;
window.fetch = function(url, options) {
    if (url.includes('/twitch/api/analytics')) {
        console.log('🧪 Mock-Daten werden verwendet');
        return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(mockData)
        });
    }
    return window.originalFetch(url, options);
};

console.log('✅ Test-Daten geladen! Reload die Seite.');
"""
    
    base_path = Path(__file__).parent
    test_file = base_path / "static/js/test-data.js"
    
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write(test_script)
    
    print(f"✅ Test-Script erstellt: {test_file}")
    print("   Lade es im Browser vor analytics-new.js um ohne Backend zu testen")

def main():
    """Haupt-Routine"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║  Analytics Dashboard - Integration Helper               ║
    ║  Modulares, modernes Streamer-Analytics Dashboard       ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    # Step 1: File Check
    if not check_files_exist():
        sys.exit(1)
    
    # Step 2: Backup
    backup_old_analytics()
    
    # Step 3: Instructions
    show_integration_instructions()
    
    # Step 4: Test Utils
    create_test_data_script()
    
    print_step(5, "Fertig!")
    print("""
    ✅ Alle Vorbereitungen abgeschlossen!
    
    📝 Nächste Schritte:
    1. Lies ANALYTICS_DASHBOARD_README.md
    2. Führe die manuellen Integrations-Schritte durch
    3. Teste das Dashboard
    4. Bei Problemen: DevTools Console checken
    
    💡 Tipp: Nutze test-data.js für Frontend-Tests ohne Backend
    
    Viel Erfolg! 🚀
    """)

if __name__ == "__main__":
    main()
