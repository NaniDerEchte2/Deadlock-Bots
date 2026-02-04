import { motion } from 'framer-motion';
import { AlertTriangle, CheckCircle, Info, TrendingUp } from 'lucide-react';
import type { Insight, ActionItem } from '@/types/analytics';

interface InsightsPanelProps {
  findings: Insight[];
  actions?: ActionItem[];
}

const typeConfig = {
  pos: {
    icon: CheckCircle,
    bgClass: 'bg-success/10 border-success/30',
    textClass: 'text-success',
    borderClass: 'border-l-success',
  },
  neg: {
    icon: AlertTriangle,
    bgClass: 'bg-danger/10 border-danger/30',
    textClass: 'text-danger',
    borderClass: 'border-l-danger',
  },
  warn: {
    icon: AlertTriangle,
    bgClass: 'bg-warning/10 border-warning/30',
    textClass: 'text-warning',
    borderClass: 'border-l-warning',
  },
  info: {
    icon: Info,
    bgClass: 'bg-blue-500/10 border-blue-500/30',
    textClass: 'text-blue-400',
    borderClass: 'border-l-blue-500',
  },
};

export function InsightsPanel({ findings, actions }: InsightsPanelProps) {
  if ((!findings || findings.length === 0) && (!actions || actions.length === 0)) {
    return null;
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-6"
    >
      {/* Findings */}
      {findings && findings.length > 0 && (
        <div>
          <h3 className="text-lg font-bold text-white mb-4">Key Findings</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {findings.map((insight, i) => {
              const config = typeConfig[insight.type];
              const Icon = config.icon;

              return (
                <motion.div
                  key={i}
                  initial={{ opacity: 0, x: -20 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.1 }}
                  className={`p-4 rounded-lg border border-l-4 ${config.bgClass} ${config.borderClass}`}
                >
                  <div className="flex items-start gap-3">
                    <Icon className={`w-5 h-5 ${config.textClass} flex-shrink-0 mt-0.5`} />
                    <div>
                      <h4 className={`text-sm font-bold mb-1 ${config.textClass}`}>
                        {insight.title}
                      </h4>
                      <p className="text-text-secondary text-sm">{insight.text}</p>
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </div>
        </div>
      )}

      {/* Actions */}
      {actions && actions.length > 0 && (
        <div className="bg-card p-6 rounded-xl border border-accent/20">
          <h3 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
            <span className="bg-accent/20 p-1.5 rounded text-accent">
              <TrendingUp className="w-5 h-5" />
            </span>
            Action Plan
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {actions.map((action, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.2 + i * 0.1 }}
                className="bg-black/20 p-4 rounded-lg border-l-4 border-accent"
              >
                <span className="text-xs font-bold text-accent uppercase tracking-wider mb-1 block">
                  {action.tag}
                </span>
                <p className="text-text-secondary text-sm">{action.text}</p>
              </motion.div>
            ))}
          </div>
        </div>
      )}
    </motion.div>
  );
}
