import { CalendarDays, ChevronLeft, ChevronRight, Loader2, MapPinned, RefreshCw, Route, Ticket, UserRound } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { api } from '../api/client';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { ServiceMap } from '../components/map/ServiceMap';
import { useApi } from '../hooks/useApi';

const emptyMapOverview = { hq: [], mechanics: [], tickets: [], routes: [], meta: {} };

function parseDateTime(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatPlanningDate(value) {
  const parsed = parseDateTime(value);
  if (!parsed) return 'Geen datum';
  return parsed.toLocaleDateString('nl-NL', { weekday: 'short', day: '2-digit', month: 'short' });
}

function isoDate(value) {
  if (!value) return null;
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const parsed = parseDateTime(value);
  if (!parsed) return null;
  return parsed.toISOString().slice(0, 10);
}

function routeGeometryLabel(value) {
  if (value === 'straight_line') return 'rechte lijnen';
  if (value === 'road_geometry') return 'wegroute';
  return value || 'onbekend';
}

export function MapPage() {
  const [selectedDate, setSelectedDate] = useState(null);
  const loadMapOverview = useCallback(() => api.getMapOverview(null, selectedDate), [selectedDate]);
  const { data, loading, error, reload } = useApi(loadMapOverview, emptyMapOverview);
  const meta = data?.meta || {};
  const availableDates = useMemo(() => data?.available_dates || meta.available_dates || [], [data?.available_dates, meta.available_dates]);
  const activeDate = selectedDate || isoDate(data?.planned_date || meta.planned_date) || availableDates[0] || null;
  const activeDateIndex = availableDates.indexOf(activeDate);
  const canGoPrevious = activeDateIndex > 0;
  const canGoNext = activeDateIndex >= 0 && activeDateIndex < availableDates.length - 1;

  const selectRelativeDay = (offset) => {
    const nextDate = availableDates[activeDateIndex + offset];
    if (nextDate) setSelectedDate(nextDate);
  };

  return (
    <main className="page">
      <PageHeader title="Kaart">
        <div className="actions">
          <Button variant="outline" onClick={() => reload()}>
            {loading ? <Loader2 size={18} className="spin" /> : <RefreshCw size={18} />} Vernieuwen
          </Button>
        </div>
      </PageHeader>

      <div className="stats four mapStats">
        <StatCard icon={Ticket} label="Tickets op kaart" value={meta.ticket_count ?? data?.tickets?.length ?? 0} tone="blue" />
        <StatCard icon={UserRound} label="Monteurs" value={meta.mechanic_count ?? data?.mechanics?.length ?? 0} tone="green" />
        <StatCard icon={Route} label="Routes" value={meta.route_count ?? data?.routes?.length ?? 0} tone="orange" />
        <StatCard icon={MapPinned} label="Route geometrie" value={routeGeometryLabel(meta.route_geometry)} tone="purple" />
      </div>

      <ApiNotice error={error} />

      {availableDates.length > 0 && (
        <section className="planningDateSelector" aria-label="Kaart dag kiezen">
          <Button disabled={!activeDate || !canGoPrevious} onClick={() => selectRelativeDay(-1)}>
            <ChevronLeft size={18} />
            Vorige dag
          </Button>
          <div className="planningDateCurrent">
            <CalendarDays size={18} />
            <b>{formatPlanningDate(activeDate)}</b>
            <small>Dag {Math.max(1, activeDateIndex + 1)} van {availableDates.length}</small>
          </div>
          <Button disabled={!activeDate || !canGoNext} onClick={() => selectRelativeDay(1)}>
            Volgende dag
            <ChevronRight size={18} />
          </Button>
        </section>
      )}

      <section className="mapShell">
        <div className="mapHeader">
          <div>
            <h2>Servicegebied en planning</h2>
            <p>HQ, ticketlocaties, monteurpositie en geplande route voor de gekozen dag.</p>
          </div>
          <div className="mapLegend">
            <span><i className="legendDot hq" /> HQ</span>
            <span><i className="legendDot mechanic" /> Monteur</span>
            <span><i className="legendDot urgent" /> Spoed</span>
            <span><i className="legendDot medium" /> Normaal</span>
            <span><i className="legendDot low" /> Laag</span>
          </div>
        </div>
        <ServiceMap data={data} />
      </section>
    </main>
  );
}
