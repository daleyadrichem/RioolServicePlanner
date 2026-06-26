import { Loader2, MapPinned, RefreshCw, Route, Ticket, UserRound } from 'lucide-react';
import { useCallback } from 'react';
import { api } from '../api/client';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { ServiceMap } from '../components/map/ServiceMap';
import { useApi } from '../hooks/useApi';

const emptyMapOverview = { hq: [], mechanics: [], tickets: [], routes: [], meta: {} };

function routeGeometryLabel(value) {
  if (value === 'straight_line') return 'rechte lijnen';
  if (value === 'road_geometry') return 'wegroute';
  return value || 'onbekend';
}

export function MapPage() {
  const loadMapOverview = useCallback(() => api.getMapOverview(), []);
  const { data, loading, error, reload } = useApi(loadMapOverview, emptyMapOverview);
  const meta = data?.meta || {};

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

      <section className="mapShell">
        <div className="mapHeader">
          <div>
            <h2>Servicegebied en planning</h2>
            <p>HQ, ticketlocaties, monteurpositie en geplande route in één overzicht.</p>
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
