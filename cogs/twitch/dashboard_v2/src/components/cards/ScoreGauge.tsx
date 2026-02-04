import { motion } from 'framer-motion';
import { getScoreColor } from '@/utils/formatters';

interface ScoreGaugeProps {
  score: number;
  label: string;
  size?: 'small' | 'medium' | 'large';
  showLabel?: boolean;
}

export function ScoreGauge({
  score,
  label,
  size = 'medium',
  showLabel = true,
}: ScoreGaugeProps) {
  const circumference = 2 * Math.PI * 40;
  const offset = circumference - (score / 100) * circumference;
  const color = getScoreColor(score);

  const sizeClasses = {
    small: 'w-20 h-20',
    medium: 'w-32 h-32',
    large: 'w-40 h-40',
  };

  const textSizes = {
    small: 'text-xl',
    medium: 'text-3xl',
    large: 'text-4xl',
  };

  return (
    <div className="flex flex-col items-center justify-center p-4 bg-card rounded-xl border border-border hover:border-border-hover transition-all">
      <div className={`relative ${sizeClasses[size]} flex items-center justify-center`}>
        <svg className="w-full h-full transform -rotate-90" viewBox="0 0 100 100">
          {/* Background circle */}
          <circle
            cx="50"
            cy="50"
            r="40"
            stroke="#1f2937"
            strokeWidth="8"
            fill="none"
          />
          {/* Progress circle */}
          <motion.circle
            cx="50"
            cy="50"
            r="40"
            stroke={color}
            strokeWidth="8"
            fill="none"
            strokeDasharray={circumference}
            strokeLinecap="round"
            initial={{ strokeDashoffset: circumference }}
            animate={{ strokeDashoffset: offset }}
            transition={{ duration: 1, ease: 'easeOut' }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <motion.span
            className={`${textSizes[size]} font-bold text-white`}
            initial={{ opacity: 0, scale: 0.5 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.3 }}
          >
            {Math.round(score)}
          </motion.span>
        </div>
      </div>
      {showLabel && (
        <span className="mt-2 text-text-secondary font-medium tracking-wide uppercase text-xs">
          {label}
        </span>
      )}
    </div>
  );
}
