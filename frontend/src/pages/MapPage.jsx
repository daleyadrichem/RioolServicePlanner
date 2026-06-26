import { Loader2, MapPinned, RefreshCw, Route, Ticket, UserRound } from 'lucide-react';
import { useCallback } from 'react';
import { api } from '../api/client';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { ServiceMap } from '../components/map/ServiceMap';
import { useApi } from '../hooks/useApi';

const fallbackMapOverview = {
  hq: [
    {
      id: 1,
      name: 'Branch Den Bosch',
      address: 'Den Bosch centrum',
      latitude: 51.6978,
      longitude: 5.3037,
    },
  ],
  mechanics: [
    {
      id: 1,
      name: 'Monteur Bas',
      status: 'ACTIVE',
      address: 'Startlocatie Den Bosch',
      latitude: 51.7042,
      longitude: 5.3166,
      current_location_source: 'start_location',
      requirements: ['LADDER', 'VEER'],
    },
  ],
  tickets: [
    {
      id: 101,
      display_id: 'T-101',
      subject: 'Verstopping keukenafvoer',
      urgency: 'URGENT',
      status: 'PLANNED',
      address: 'Vughterstraat, Den Bosch',
      latitude: 51.6898,
      longitude: 5.3001,
      technician_name: 'Monteur Bas',
      planned_start_at: '2026-06-26T09:15:00',
      planned_end_at: '2026-06-26T10:15:00',
      requirements: ['VEER'],
    },
    {
      id: 102,
      display_id: 'T-102',
      subject: 'Rioollucht badkamer',
      urgency: 'MEDIUM',
      status: 'PLANNED',
      address: 'Rompertpassage, Den Bosch',
      latitude: 51.7156,
      longitude: 5.3192,
      technician_name: 'Monteur Bas',
      planned_start_at: '2026-06-26T11:00:00',
      planned_end_at: '2026-06-26T12:00:00',
      requirements: [],
    },
  ],
  routes: [
    {
      technician_id: 1,
      technician_name: 'Monteur Bas',
      geometry_type: 'straight_line',
      ticket_ids: [101, 102],
      coordinates: [
        [51.7042, 5.3166],
        [51.6898, 5.3001],
        [51.7156, 5.3192],
        [51.7042, 5.3166],
      ],
      stops: [],
    },
  ],
  meta: {
    route_geometry: 'straight_line',
    ticket_count: 2,
    mechanic_count: 1,
    route_count: 1,
  },
};

function routeGeometryLabel(value) {
  if (value === 'straight_line') return 'rechte lijnen';
  if (value === 'road_geometry') return 'wegroute';
  return value || 'onbekend';
}

export function MapPage() {
  const loadMapOverview = useCallback(() => api.getMapOverview(), []);
  const { data, loading, error, reload } = useApi(loadMapOverview, fallbackMapOverview);
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
