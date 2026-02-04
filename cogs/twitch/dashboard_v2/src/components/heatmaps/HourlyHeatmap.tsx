import { motion } from 'framer-motion';
import type { HourlyHeatmapData } from '@/types/analytics';
import { getWeekdayLabel, formatNumber, getHeatmapColor } from '@/utils/formatters';

interface HourlyHeatmapProps {
  data: HourlyHeatmapData[];
  title?: string;
}

export function HourlyHeatmap({ data, title = 'Beste Streaming-Zeiten' }: HourlyHeatmapProps) {
  const weekdays = [0, 1, 2, 3, 4, 5, 6]; // Sun-Sat
  const hours = Array.from({ length: 24 }, (_, i) => i);

  // Create lookup map
  const dataMap = new Map<string, HourlyHeatmapData>();
  data.forEach(d => {
    dataMap.set(`${d.weekday}-${d.hour}`, d);
  });

  // Find max for color scaling
  const maxViewers = Math.max(...data.map(d => d.avgViewers), 1);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-5"
    >
      <h3 className="text-lg font-bold text-white mb-4">{title}</h3>

      <div className="overflow-x-auto">
        <div className="min-w-[600px]">
          {/* Hour Labels */}
          <div className="flex mb-1">
            <div className="w-10" /> {/* Spacer for weekday labels */}
            {hours.map(hour => (
              <div
                key={hour}
                className="flex-1 text-center text-xs text-text-secondary"
              >
                {hour % 3 === 0 ? `${hour}h` : ''}
              </div>
            ))}
          </div>

          {/* Grid */}
          {weekdays.map(weekday => (
            <div key={weekday} className="flex mb-1">
              {/* Weekday Label */}
              <div className="w-10 text-xs text-text-secondary flex items-center">
                {getWeekdayLabel(weekday)}
              </div>

              {/* Hour Cells */}
              {hours.map(hour => {
                const cellData = dataMap.get(`${weekday}-${hour}`);
                const viewers = cellData?.avgViewers ?? 0;
                const streamCount = cellData?.streamCount ?? 0;

                return (
                  <motion.div
                    key={hour}
                    initial={{ opacity: 0, scale: 0.8 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ delay: (weekday * 24 + hour) * 0.002 }}
                    className="flex-1 aspect-square mx-0.5 rounded-sm relative group cursor-pointer"
                    style={{ backgroundColor: getHeatmapColor(viewers, maxViewers) }}
                  >
                    {/* Tooltip */}
                    {streamCount > 0 && (
                      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 opacity-0 group-hover:opacity-100 transition-opacity z-10 pointer-events-none">
                        <div className="bg-card border border-border rounded px-2 py-1 text-xs whitespace-nowrap shadow-xl">
                          <div className="text-white font-medium">
                            {getWeekdayLabel(weekday)} {hour}:00
                          </div>
                          <div className="text-text-secondary">
                            {formatNumber(viewers)} Ø Viewer
                          </div>
                          <div className="text-text-secondary">
                            {streamCount} Streams
                          </div>
                        </div>
                      </div>
                    )}
                  </motion.div>
                );
              })}
            </div>
          ))}

          {/* Legend */}
          <div className="flex items-center justify-end mt-4 gap-2 text-xs text-text-secondary">
            <span>Weniger</span>
            <div className="flex gap-1">
              {[0.1, 0.3, 0.5, 0.7, 0.9].map(intensity => (
                <div
                  key={intensity}
                  className="w-4 h-4 rounded"
                  style={{ backgroundColor: `rgba(124, 58, 237, ${intensity})` }}
                />
              ))}
            </div>
            <span>Mehr Viewer</span>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
