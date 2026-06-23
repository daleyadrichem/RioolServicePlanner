import { AlertTriangle, Calendar, Clock, Gauge, MoreVertical, Plus, RotateCw, Route, Ticket, User, Waves } from 'lucide-react';
import { useCallback } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { FilterRow } from '../components/Filters';
import { SelectInput, SearchBox } from '../components/FormControls';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { JobRequirementIcon } from '../components/Requirements';
import { Ladder } from '../icons/Ladder';
import { planningColumns, technicians, timelineTimes } from '../data/planningData';

const fallbackPlanning = {
  stats: { total_today: 23, planned: 23, urgent_open: 3, kilometers: 268, travel_minutes: 375, free_minutes: 150 },
  columns: technicians.map((name, index) => ({
    technician: { id: `m${index + 1}`, name, can_use_ladder: true, can_use_spring: index !== 0 },
    items: planningColumns[index].map(([title, address, time, duration, tone, requirement]) => ({
      title,
      address,
      start: time?.split(' - ')[0] || '',
      end: time?.split(' - ')[1] || '',
      duration_minutes: parseInt(duration, 10) || 30,
      urgency: tone === 'urgent' ? 'Urgent' : tone === 'low' ? 'Laag' : tone === 'normal' ? 'Normaal' : null,
      type: tone === 'travel' ? 'travel' : tone === 'break' ? 'break' : 'ticket',
      requires_ladder: requirement === 'Ladder',
      requires_spring: requirement === 'Waves',
    })),
  })),
};

function toneForItem(item) {
  if (item.type === 'travel') return 'travel';
  if (item.type === 'break') return 'break';
  if (item.urgency === 'Urgent') return 'urgent';
  if (item.urgency === 'Laag') return 'low';
  if (item.urgency === 'Normaal') return 'normal';
  return 'blue';
}

function PlanningJob({ item }) {
  const requirement = item.requires_ladder ? 'Ladder' : item.requires_spring ? 'Waves' : null;
  return (
    <div className={`job ${toneForItem(item)}`}>
      <div>
        <b>{item.title}</b>
        {item.address && <strong>{item.address}</strong>}
        <small>{item.start} - {item.end} · {item.duration_minutes} min</small>
      </div>
      <JobRequirementIcon kind={requirement} />
    </div>
  );
}

function TechnicianColumn({ column }) {
  const { technician, items } = column;
  return (
    <div className="col">
      <div className="colHead">
        <User size={18} />
        <b>{technician.name}</b>
        <span />
        {technician.can_use_ladder && <Ladder size={18} />}
        {technician.can_use_spring && <Waves size={18} />}
        <MoreVertical size={18} />
      </div>

      <div className="timeline">
        <div className="timeMarks">
          {timelineTimes.slice(0, 10).map((time) => <span key={time}>{time}</span>)}
        </div>
        <div className="jobs">
          {items.map((item, itemIndex) => <PlanningJob key={`${item.title}-${itemIndex}`} item={item} />)}
        </div>
      </div>
    </div>
  );
}

export function PlanningPage() {
  const loadPlanning = useCallback(() => api.getPlanning(), []);
  const { data: planning, loading, error, reload } = useApi(loadPlanning, fallbackPlanning);

  const runAction = async (action) => {
    try {
      await action();
      await reload();
    } catch (err) {
      alert(err.message);
    }
  };

  const stats = planning.stats || fallbackPlanning.stats;

  return (
    <main className="page">
      <PageHeader title="Planning Overzicht">
        <div className="actions">
          <Button primary onClick={() => runAction(() => api.createTicket({ subject: 'Nieuwe verstopping', address: 'Markt 1, Den Bosch', city: 'Den Bosch', urgency: 'Urgent', requires_ladder: false, requires_spring: true }))}><Plus size={20} />Nieuw ticket</Button>
          <Button onClick={() => runAction(api.autoPlan)}><Clock size={20} />Auto-plan</Button>
          <Button onClick={() => runAction(api.replan)}><RotateCw size={20} />Herplannen</Button>
        </div>
      </PageHeader>

      <ApiNotice loading={loading} error={error} />

      <div className="toolbar">
        <SelectInput icon={Calendar} text="Datum 20 mei 2025" />
        <SearchBox text="Zoeken in tickets..." />
      </div>

      <section className="stats four">
        <StatCard icon={Ticket} label="Totaal tickets vandaag" value={stats.total_today} sub={`• ${stats.planned} gepland`} />
        <StatCard icon={AlertTriangle} label="Urgent open" value={stats.urgent_open} sub="• tickets" tone="red" />
        <StatCard icon={Route} label="Kilometers gepland" value={`${stats.kilometers} km`} sub={`• ± ${Math.round(stats.travel_minutes / 60)} u reistijd`} tone="green" />
        <StatCard icon={Gauge} label="Vrije spreedruimte" value={`${Math.floor(stats.free_minutes / 60)} u ${stats.free_minutes % 60}m`} sub="• buffer voor spoed" tone="purple" />
      </section>

      <FilterRow />

      <section className="plannerGrid">
        {planning.columns.map((column) => <TechnicianColumn key={column.technician.id} column={column} />)}
      </section>
    </main>
  );
}
