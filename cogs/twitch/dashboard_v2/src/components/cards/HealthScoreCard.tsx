import { motion } from 'framer-motion';
import { Zap } from 'lucide-react';
import type { HealthScore } from '@/types/analytics';

interface HealthScoreCardProps {
  scores: HealthScore;
}

export function HealthScoreCard({ scores }: HealthScoreCardProps) {
  const subScores = [
    { label: 'Reach', value: scores.reach },
    { label: 'Ret.', value: scores.retention },
    { label: 'Eng.', value: scores.engagement },
  ];

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="md:col-span-2 lg:col-span-1 bg-gradient-to-br from-card to-accent/10 p-6 rounded-2xl border border-accent/20 flex flex-col items-center justify-center text-center relative overflow-hidden"
    >
      {/* Background Icon */}
      <div className="absolute top-0 right-0 p-4 opacity-10">
        <Zap className="w-16 h-16" />
      </div>

      <h2 className="text-text-secondary font-semibold uppercase tracking-wider text-xs mb-4">
        Channel Health
      </h2>

      {/* Main Score */}
      <div className="relative mb-2">
        <motion.span
          className="text-6xl font-bold text-white"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
        >
          {scores.total}
        </motion.span>
        <span className="text-xl text-accent absolute -top-1 -right-6">/100</span>
      </div>

      {/* Sub Scores */}
      <div className="mt-4 w-full grid grid-cols-3 gap-2 text-xs text-text-secondary">
        {subScores.map((sub, i) => (
          <motion.div
            key={sub.label}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.3 + i * 0.1 }}
            className="bg-black/20 rounded p-2"
          >
            <span className="block">{sub.label}</span>
            <strong className="text-white text-sm">{sub.value}</strong>
          </motion.div>
        ))}
      </div>
    </motion.div>
  );
}
