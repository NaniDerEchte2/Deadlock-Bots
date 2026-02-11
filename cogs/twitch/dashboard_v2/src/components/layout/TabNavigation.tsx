import { motion } from 'framer-motion';
import {
  LayoutDashboard,
  TrendingUp,
  MessageSquare,
  BarChart3,
  Users,
  Calendar,
  Target,
  GraduationCap,
} from 'lucide-react';

export type TabId = 'overview' | 'streams' | 'chat' | 'growth' | 'audience' | 'compare' | 'schedule' | 'coaching';

interface Tab {
  id: TabId;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const tabs: Tab[] = [
  { id: 'overview', label: 'Übersicht', icon: LayoutDashboard },
  { id: 'streams', label: 'Streams', icon: TrendingUp },
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'growth', label: 'Wachstum', icon: BarChart3 },
  { id: 'audience', label: 'Audience', icon: Target },
  { id: 'compare', label: 'Vergleich', icon: Users },
  { id: 'schedule', label: 'Zeitplan', icon: Calendar },
  { id: 'coaching', label: 'Coaching', icon: GraduationCap },
];

interface TabNavigationProps {
  activeTab: TabId;
  onTabChange: (tab: TabId) => void;
}

export function TabNavigation({ activeTab, onTabChange }: TabNavigationProps) {
  return (
    <nav className="mb-8 -mx-4 px-4 overflow-x-auto">
      <div className="flex items-center gap-1 bg-card/50 p-1 rounded-xl border border-border min-w-max">
        {tabs.map(tab => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;

          return (
            <button
              key={tab.id}
              onClick={() => onTabChange(tab.id)}
              className={`relative flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive ? 'text-white' : 'text-text-secondary hover:text-white'
              }`}
            >
              {isActive && (
                <motion.div
                  layoutId="activeTab"
                  className="absolute inset-0 bg-accent rounded-lg"
                  initial={false}
                  transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                />
              )}
              <span className="relative z-10 flex items-center gap-2">
                <Icon className="w-4 h-4" />
                <span className="hidden sm:inline">{tab.label}</span>
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
