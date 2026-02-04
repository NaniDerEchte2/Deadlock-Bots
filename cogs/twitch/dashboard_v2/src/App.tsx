import { useState, useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from '@/components/layout/Header';
import { TabNavigation, type TabId } from '@/components/layout/TabNavigation';
import { Overview } from '@/pages/Overview';
import { Sessions } from '@/pages/Sessions';
import { ChatAnalytics } from '@/pages/ChatAnalytics';
import { Growth } from '@/pages/Growth';
import { Comparison } from '@/pages/Comparison';
import { Schedule } from '@/pages/Schedule';
import { useStreamerList, useAuthStatus } from '@/hooks/useAnalytics';
import type { TimeRange } from '@/types/analytics';
import { Shield, ShieldCheck, ShieldAlert, Wifi } from 'lucide-react';

// Create QueryClient
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
});

function Dashboard() {
  const [streamer, setStreamer] = useState<string | null>(null);
  const [days, setDays] = useState<TimeRange>(30);
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  const { data: streamers = [], isLoading: loadingStreamers } = useStreamerList();
  const { data: authStatus, isLoading: loadingAuth, isError: authError } = useAuthStatus();

  // Parse URL params on mount
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlStreamer = params.get('streamer');
    const urlDays = params.get('days');

    if (urlStreamer) setStreamer(urlStreamer);
    if (urlDays) {
      const d = parseInt(urlDays, 10);
      if (d === 7 || d === 30 || d === 90) setDays(d);
    }
  }, []);

  // Update URL when params change
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);

    if (streamer) {
      params.set('streamer', streamer);
    } else {
      params.delete('streamer');
    }
    params.set('days', String(days));

    const newUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, '', newUrl);
  }, [streamer, days]);

  const handleSessionClick = (sessionId: number) => {
    // TODO: Navigate to session detail view
    console.log('Session clicked:', sessionId);
  };

  // Auth badge component
  const AuthBadge = () => {
    if (loadingAuth) return null;

    if (authError || !authStatus?.authenticated) {
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-error/10 border border-error/20 rounded-lg text-error text-sm">
          <ShieldAlert className="w-4 h-4" />
          <span>Nicht authentifiziert</span>
        </div>
      );
    }

    if (authStatus.isLocalhost) {
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-success/10 border border-success/20 rounded-lg text-success text-sm">
          <Wifi className="w-4 h-4" />
          <span>Localhost (Admin)</span>
        </div>
      );
    }

    if (authStatus.isAdmin) {
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-primary/10 border border-primary/20 rounded-lg text-primary text-sm">
          <ShieldCheck className="w-4 h-4" />
          <span>Admin</span>
        </div>
      );
    }

    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-accent/10 border border-accent/20 rounded-lg text-accent text-sm">
        <Shield className="w-4 h-4" />
        <span>Partner</span>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-bg p-4 md:p-8">
      <div className="max-w-[1600px] mx-auto">
        {/* Auth Status Badge */}
        <div className="flex justify-end mb-2">
          <AuthBadge />
        </div>

        <Header
          streamer={streamer}
          streamers={streamers}
          days={days}
          onStreamerChange={setStreamer}
          onDaysChange={setDays}
          isLoading={loadingStreamers}
          isAdmin={authStatus?.isAdmin || authStatus?.isLocalhost || false}
        />

        <TabNavigation activeTab={activeTab} onTabChange={setActiveTab} />

        {/* Tab Content */}
        {activeTab === 'overview' && (
          <Overview
            streamer={streamer}
            days={days}
            onSessionClick={handleSessionClick}
          />
        )}

        {activeTab === 'streams' && (
          <Sessions streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'chat' && (
          <ChatAnalytics streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'growth' && (
          <Growth streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'compare' && (
          <Comparison streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'schedule' && (
          <Schedule streamer={streamer || ''} days={days} />
        )}
      </div>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  );
}
