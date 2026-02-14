import { TrendingDown, Zap, Gift, Radio, AlertCircle, Loader2 } from 'lucide-react';
import { useMonetization } from '@/hooks/useAnalytics';
import type { TimeRange } from '@/types/analytics';

interface MonetizationProps {
  streamer: string | null;
  days: TimeRange;
}

function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4 flex flex-col gap-1">
      <span className="text-xs text-text-secondary uppercase tracking-wide">{label}</span>
      <span className="text-2xl font-bold text-white">{value}</span>
      {sub && <span className="text-xs text-text-secondary">{sub}</span>}
    </div>
  );
}

function fmt(n: number | null | undefined, fallback = '-'): string {
  if (n === null || n === undefined) return fallback;
  return n.toLocaleString('de-DE');
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)}%`;
}

export function Monetization({ streamer, days }: MonetizationProps) {
  const { data, isLoading, isError } = useMonetization(streamer, days);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-text-secondary">
        <Loader2 className="w-6 h-6 animate-spin mr-2" />
        Lade Monetization-Daten…
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex items-center gap-3 p-6 bg-error/10 border border-error/20 rounded-xl text-error">
        <AlertCircle className="w-5 h-5 shrink-0" />
        <span>Monetization-Daten konnten nicht geladen werden.</span>
      </div>
    );
  }

  const { ads, hype_train, bits, subs, window_days } = data;
  const noAds = ads.total === 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-xl font-bold text-white">Monetization &amp; Hype Train</h2>
        <p className="text-text-secondary text-sm mt-1">Letzte {window_days} Tage</p>
      </div>

      {/* Ad Breaks */}
      <section>
        <div className="flex items-center gap-2 mb-3">
          <Radio className="w-4 h-4 text-accent" />
          <h3 className="font-semibold text-white">Ad Breaks</h3>
        </div>
        {noAds ? (
          <div className="p-4 bg-card border border-border rounded-xl text-text-secondary text-sm">
            Noch keine Ad-Break-Events in den letzten {window_days} Tagen.
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
              <StatTile label="Gesamt" value={fmt(ads.total)} />
              <StatTile label="Automatisch" value={fmt(ads.auto)} sub={`${fmt(ads.manual)} manuell`} />
              <StatTile label="Ø Dauer" value={`${ads.avg_duration_s.toFixed(0)} s`} />
              <StatTile
                label="Ø Viewer-Drop"
                value={ads.avg_viewer_drop_pct !== null ? fmtPct(ads.avg_viewer_drop_pct) : '-'}
                sub="nach Ad-Break"
              />
            </div>

            {/* Worst Ads Table */}
            {ads.worst_ads.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <TrendingDown className="w-4 h-4 text-error" />
                  <span className="text-sm font-medium text-white">Schlechteste Ads (Top 5)</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-text-secondary border-b border-border">
                        <th className="text-left py-2 pr-4 font-medium">Zeitpunkt</th>
                        <th className="text-right py-2 pr-4 font-medium">Dauer</th>
                        <th className="text-center py-2 pr-4 font-medium">Typ</th>
                        <th className="text-right py-2 font-medium">Viewer-Drop</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ads.worst_ads.map((ad, i) => (
                        <tr key={i} className="border-b border-border/50 hover:bg-card/50">
                          <td className="py-2 pr-4 text-white font-mono text-xs">{ad.started_at}</td>
                          <td className="py-2 pr-4 text-right text-text-secondary">{ad.duration_s} s</td>
                          <td className="py-2 pr-4 text-center">
                            <span className={`text-xs px-2 py-0.5 rounded-full ${ad.is_automatic ? 'bg-warning/20 text-warning' : 'bg-primary/20 text-primary'}`}>
                              {ad.is_automatic ? 'Auto' : 'Manuell'}
                            </span>
                          </td>
                          <td className="py-2 text-right font-semibold text-error">
                            {fmtPct(ad.drop_pct)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {/* Hype Train */}
      <section>
        <div className="flex items-center gap-2 mb-3">
          <Zap className="w-4 h-4 text-warning" />
          <h3 className="font-semibold text-white">Hype Trains</h3>
        </div>
        {hype_train.total === 0 ? (
          <div className="p-4 bg-card border border-border rounded-xl text-text-secondary text-sm">
            Noch keine Hype Trains in den letzten {window_days} Tagen.
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <StatTile label="Gesamt" value={fmt(hype_train.total)} />
            <StatTile label="Ø Level" value={hype_train.avg_level.toFixed(1)} />
            <StatTile label="Max Level" value={fmt(hype_train.max_level)} />
            <StatTile label="Ø Dauer" value={`${hype_train.avg_duration_s.toFixed(0)} s`} />
          </div>
        )}
      </section>

      {/* Bits & Subs */}
      <section>
        <div className="flex items-center gap-2 mb-3">
          <Gift className="w-4 h-4 text-success" />
          <h3 className="font-semibold text-white">Bits &amp; Subs</h3>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatTile label="Bits total" value={fmt(bits.total)} sub={`${fmt(bits.cheer_events)} Cheers`} />
          <StatTile label="Sub-Events" value={fmt(subs.total_events)} sub={`${fmt(subs.gifted)} Gifted`} />
        </div>
      </section>
    </div>
  );
}
