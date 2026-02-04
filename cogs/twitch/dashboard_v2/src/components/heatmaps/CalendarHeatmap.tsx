import { useMemo } from 'react';
import { motion } from 'framer-motion';
import type { CalendarHeatmapData } from '@/types/analytics';
import { getHeatmapColor, formatHours, getMonthLabel } from '@/utils/formatters';

interface CalendarHeatmapProps {
  data: CalendarHeatmapData[];
  title?: string;
  metric?: 'hoursWatched' | 'streamCount';
}

export function CalendarHeatmap({
  data,
  title = 'Stream-Aktivität',
  metric = 'hoursWatched',
}: CalendarHeatmapProps) {
  // Create lookup map and calculate layout
  const { weeks, maxValue, dataMap, monthLabels } = useMemo(() => {
    const map = new Map<string, CalendarHeatmapData>();
    let max = 0;

    data.forEach(d => {
      map.set(d.date, d);
      const value = metric === 'hoursWatched' ? d.hoursWatched : d.streamCount;
      if (value > max) max = value;
    });

    // Generate weeks for the last 365 days
    const weeks: Date[][] = [];
    const today = new Date();
    const startDate = new Date(today);
    startDate.setDate(startDate.getDate() - 364);

    // Adjust to start from Sunday
    while (startDate.getDay() !== 0) {
      startDate.setDate(startDate.getDate() - 1);
    }

    let currentWeek: Date[] = [];
    const endDate = new Date(today);

    for (let d = new Date(startDate); d <= endDate; d.setDate(d.getDate() + 1)) {
      currentWeek.push(new Date(d));
      if (currentWeek.length === 7) {
        weeks.push(currentWeek);
        currentWeek = [];
      }
    }
    if (currentWeek.length > 0) {
      weeks.push(currentWeek);
    }

    // Generate month labels
    const labels: { month: number; weekIndex: number }[] = [];
    let lastMonth = -1;
    weeks.forEach((week, weekIndex) => {
      const firstOfWeek = week[0];
      if (firstOfWeek.getMonth() !== lastMonth) {
        labels.push({ month: firstOfWeek.getMonth() + 1, weekIndex });
        lastMonth = firstOfWeek.getMonth();
      }
    });

    return { weeks, maxValue: max, dataMap: map, monthLabels: labels };
  }, [data, metric]);

  const formatDateKey = (date: Date): string => {
    return date.toISOString().split('T')[0];
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-5"
    >
      <h3 className="text-lg font-bold text-white mb-4">{title}</h3>

      <div className="overflow-x-auto">
        <div className="min-w-[800px]">
          {/* Month Labels */}
          <div className="flex mb-2 ml-8">
            {monthLabels.map(({ month, weekIndex }, i) => (
              <div
                key={i}
                className="text-xs text-text-secondary"
                style={{ marginLeft: i === 0 ? weekIndex * 14 : (weekIndex - monthLabels[i - 1].weekIndex - 1) * 14 }}
              >
                {getMonthLabel(month)}
              </div>
            ))}
          </div>

          {/* Grid */}
          <div className="flex">
            {/* Day Labels */}
            <div className="flex flex-col gap-1 mr-2 text-xs text-text-secondary">
              <div className="h-3"></div>
              <div className="h-3">Mo</div>
              <div className="h-3"></div>
              <div className="h-3">Mi</div>
              <div className="h-3"></div>
              <div className="h-3">Fr</div>
              <div className="h-3"></div>
            </div>

            {/* Weeks */}
            <div className="flex gap-1">
              {weeks.map((week, weekIndex) => (
                <div key={weekIndex} className="flex flex-col gap-1">
                  {week.map((date, dayIndex) => {
                    const dateKey = formatDateKey(date);
                    const cellData = dataMap.get(dateKey);
                    const value = cellData
                      ? metric === 'hoursWatched'
                        ? cellData.hoursWatched
                        : cellData.streamCount
                      : 0;

                    return (
                      <motion.div
                        key={dayIndex}
                        initial={{ opacity: 0, scale: 0.8 }}
                        animate={{ opacity: 1, scale: 1 }}
                        transition={{ delay: weekIndex * 0.01 }}
                        className="w-3 h-3 rounded-sm relative group cursor-pointer"
                        style={{ backgroundColor: getHeatmapColor(value, maxValue) }}
                      >
                        {/* Tooltip */}
                        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 opacity-0 group-hover:opacity-100 transition-opacity z-10 pointer-events-none">
                          <div className="bg-card border border-border rounded px-2 py-1 text-xs whitespace-nowrap shadow-xl">
                            <div className="text-white font-medium">
                              {date.toLocaleDateString('de-DE', { day: '2-digit', month: 'short', year: 'numeric' })}
                            </div>
                            {cellData ? (
                              <>
                                <div className="text-text-secondary">
                                  {formatHours(cellData.hoursWatched)} watched
                                </div>
                                <div className="text-text-secondary">
                                  {cellData.streamCount} Streams
                                </div>
                              </>
                            ) : (
                              <div className="text-text-secondary">Kein Stream</div>
                            )}
                          </div>
                        </div>
                      </motion.div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>

          {/* Legend */}
          <div className="flex items-center justify-between mt-4 text-xs text-text-secondary">
            <div>
              {data.length > 0 && (
                <span>
                  {data.reduce((sum, d) => sum + d.streamCount, 0)} Streams in 365 Tagen
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <span>Weniger</span>
              <div className="flex gap-1">
                {[0.1, 0.3, 0.5, 0.7, 0.9].map(intensity => (
                  <div
                    key={intensity}
                    className="w-3 h-3 rounded-sm"
                    style={{ backgroundColor: `rgba(124, 58, 237, ${intensity})` }}
                  />
                ))}
              </div>
              <span>Mehr</span>
            </div>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
