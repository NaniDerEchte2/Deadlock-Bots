import { useState, useEffect, Component, type ReactNode, type ErrorInfo } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Header } from '@/components/layout/Header';
import { TabNavigation, type TabId } from '@/components/layout/TabNavigation';
import { Overview } from '@/pages/Overview';
import { Sessions } from '@/pages/Sessions';
import { ChatAnalytics } from '@/pages/ChatAnalytics';
import { Growth } from '@/pages/Growth';
import { Audience } from '@/pages/Audience';
import { Comparison } from '@/pages/Comparison';
import { Schedule } from '@/pages/Schedule';
import { Coaching } from '@/pages/Coaching';
import { Monetization } from '@/pages/Monetization';
import { Category } from '@/pages/Category';
import { useStreamerList, useAuthStatus } from '@/hooks/useAnalytics';
import type { TimeRange } from '@/types/analytics';
import { Shield, ShieldCheck, ShieldAlert, Wifi, AlertTriangle } from 'lucide-react';

// Error Boundary to prevent white screen on crashes
interface ErrorBoundaryProps {
  children: ReactNode;
}
interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Dashboard Error:', error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-bg flex items-center justify-center p-8">
          <div className="bg-card border border-border rounded-xl p-8 max-w-lg text-center">
            <AlertTriangle className="w-12 h-12 text-warning mx-auto mb-4" />
            <h2 className="text-xl font-bold text-white mb-2">Dashboard-Fehler</h2>
            <p className="text-text-secondary mb-4">
              {this.state.error?.message || 'Ein unerwarteter Fehler ist aufgetreten.'}
            </p>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              className="px-4 py-2 bg-primary text-white rounded-lg hover:bg-primary/80 transition-colors"
            >
              Erneut versuchen
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

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
          canViewAllStreamers={authStatus?.permissions?.viewAllStreamers || false}
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

        {activeTab === 'audience' && (
          <Audience streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'compare' && (
          <Comparison streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'schedule' && (
          <Schedule streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'coaching' && (
          <Coaching streamer={streamer || ''} days={days} />
        )}

        {activeTab === 'monetization' && (
          <Monetization streamer={streamer} days={days} />
        )}

        {activeTab === 'category' && (
          <Category
            streamer={streamer}
            days={days}
            onStreamerSelect={setStreamer}
            onNavigate={setActiveTab}
          />
        )}
      </div>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ErrorBoundary>
        <Dashboard />
      </ErrorBoundary>
    </QueryClientProvider>
  );
}
