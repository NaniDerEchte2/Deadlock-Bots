import { motion } from 'framer-motion';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

interface KpiCardProps {
  title: string;
  value: string | number;
  subValue?: string;
  trend?: number;
  icon?: LucideIcon;
  color?: 'blue' | 'green' | 'purple' | 'yellow' | 'red';
  size?: 'normal' | 'large';
}

const colorClasses = {
  blue: 'bg-blue-500/10 text-blue-400',
  green: 'bg-green-500/10 text-green-400',
  purple: 'bg-accent/10 text-accent',
  yellow: 'bg-yellow-500/10 text-yellow-400',
  red: 'bg-red-500/10 text-red-400',
};

export function KpiCard({
  title,
  value,
  subValue,
  trend,
  icon: Icon,
  color = 'blue',
  size = 'normal',
}: KpiCardProps) {
  const TrendIcon = trend && trend > 0 ? TrendingUp : trend && trend < 0 ? TrendingDown : Minus;
  const trendColor =
    trend && trend > 0
      ? 'text-success'
      : trend && trend < 0
      ? 'text-danger'
      : 'text-text-secondary';

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className={`bg-card p-5 rounded-xl border border-border hover:border-border-hover transition-all ${
        size === 'large' ? 'md:col-span-2' : ''
      }`}
    >
      <div className="flex justify-between items-start mb-2">
        <span className="text-text-secondary text-sm font-semibold uppercase tracking-wider">
          {title}
        </span>
        {Icon && (
          <div className={`p-2 rounded-lg ${colorClasses[color]}`}>
            <Icon className="w-4 h-4" />
          </div>
        )}
      </div>

      <div className="flex items-end gap-3 mt-1">
        <span className={`font-bold text-white ${size === 'large' ? 'text-4xl' : 'text-3xl'}`}>
          {value}
        </span>
        {trend != null && (
          <span className={`text-sm font-medium mb-1 flex items-center gap-1 ${trendColor}`}>
            <TrendIcon className="w-3 h-3" />
            {Math.abs(trend).toFixed(1)}%
          </span>
        )}
      </div>

      {subValue && (
        <div className="mt-3 text-xs text-text-secondary font-medium">{subValue}</div>
      )}
    </motion.div>
  );
}
