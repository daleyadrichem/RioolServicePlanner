import { AlertTriangle, CalendarDays, ChevronLeft, ChevronRight, Clock, Gauge, Loader2, MoreVertical, RotateCw, Route, Ticket, User, Waves } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { JobRequirementIcon } from '../components/Requirements';
import { Ladder } from '../icons/Ladder';

const MIN_VISIBLE_TRAVEL_MINUTES = 5;
const TIMELINE_SLOT_MINUTES = 1;
const TIMELINE_SLOT_HEIGHT_PX = 2;

const emptyPlanning = {
  has_plan: false,
  stats: { total_today: 0, planned: 0, urgent_open: 0, kilometers: 0, travel_minutes: 0, free_minutes: 0 },
  columns: [],
  available_dates: [],
  planned_date: null,
};

function parseDateTime(value, fallbackDate) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === 'string' && /^\d{2}:\d{2}$/.test(value)) {
    return new Date(`${fallbackDate || '1970-01-01'}T${value}:00`);
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function minutesFromDayStart(value, fallbackDate) {
  const parsed = parseDateTime(value, fallbackDate);
  if (!parsed) return null;
  return parsed.getHours() * 60 + parsed.getMinutes();
}

function formatTime(value, fallback = '') {
  const parsed = parseDateTime(value);
  if (!parsed) return fallback;
  return parsed.toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });
}

function durationFromTimes(startMinutes, endMinutes) {
  if (startMinutes == null || endMinutes == null) return 0;
  return Math.max(0, endMinutes - startMinutes);
}

function toHourTicks(column) {
  if (Array.isArray(column.hour_ticks) && column.hour_ticks.length) {
    return column.hour_ticks.map((tick) => (typeof tick === 'string' ? tick : formatTime(tick))).filter(Boolean);
  }

  const startMinutes = minutesFromDayStart(column.timeline_start_at) ?? 8 * 60;
  const endMinutes = minutesFromDayStart(column.timeline_end_at) ?? 17 * 60;
  const firstHour = Math.floor(startMinutes / 60);
  const lastHour = Math.ceil(endMinutes / 60);
  return Array.from({ length: Math.max(1, lastHour - firstHour + 1) }, (_, index) => {
    const hour = firstHour + index;
    return `${String(hour).padStart(2, '0')}:00`;
  });
}

function normalizeTimelineItem(rawItem, index, fallbackDate) {
  const type = String(rawItem.type || rawItem.display_variant || 'TICKET').toUpperCase();
  const isTravel = type === 'TRAVEL' || rawItem.display_variant === 'travel';
  const isBreak = type === 'BREAK' || rawItem.display_variant === 'break';
  const isRequirementPickup = type === 'REQUIREMENT_PICKUP' || rawItem.display_variant === 'requirement_pickup';
  const start = rawItem.start || formatTime(rawItem.start_at, rawItem.planned_start_at ? formatTime(rawItem.planned_start_at) : '');
  const end = rawItem.end || formatTime(rawItem.end_at, rawItem.planned_end_at ? formatTime(rawItem.planned_end_at) : '');
  const startMinutes = minutesFromDayStart(rawItem.start_at || rawItem.planned_start_at || start, fallbackDate);
  const endMinutes = minutesFromDayStart(rawItem.end_at || rawItem.planned_end_at || end, fallbackDate);
  const calculatedDuration = durationFromTimes(startMinutes, endMinutes);
  const durationMinutes = rawItem.duration_minutes || rawItem.estimated_duration_minutes || calculatedDuration;

  return {
    ...rawItem,
    id: rawItem.id || `${rawItem.type || 'timeline'}-${rawItem.ticket_id || index}`,
    title: rawItem.title || rawItem.label || rawItem.subject || (isTravel ? 'Rijtijd' : isBreak ? 'Pauze' : isRequirementPickup ? 'Hulpmiddelen ophalen' : 'Ticket'),
    subject: rawItem.subject || rawItem.label || rawItem.title,
    start,
    end,
    start_at: rawItem.start_at || rawItem.planned_start_at || start,
    end_at: rawItem.end_at || rawItem.planned_end_at || end,
    duration_minutes: durationMinutes,
    type: isTravel ? 'TRAVEL' : isBreak ? 'BREAK' : isRequirementPickup ? 'REQUIREMENT_PICKUP' : 'TICKET',
    display_variant: rawItem.display_variant || (isTravel ? 'travel' : isBreak ? 'break' : isRequirementPickup ? 'requirement_pickup' : 'ticket'),
    color_hint: rawItem.color_hint || (isTravel ? 'grey' : isRequirementPickup ? 'blue' : rawItem.urgency?.toLowerCase()),
    requires_ladder: rawItem.requires_ladder ?? (rawItem.required_skills || []).includes('LADDER'),
    requires_spring: rawItem.requires_spring ?? (rawItem.required_skills || []).includes('VEER'),
    characteristics: rawItem.characteristics || rawItem.required_skills || [],
    _startMinutes: startMinutes,
    _endMinutes: endMinutes,
  };
}

function shouldShowTimelineItem(item) {
  return item.type !== 'TRAVEL' || item.duration_minutes >= MIN_VISIBLE_TRAVEL_MINUTES;
}

function normalizeColumn(column) {
  const fallbackDate = column.timeline_start_at?.slice(0, 10);
  const timeline = Array.isArray(column.timeline) && column.timeline.length ? column.timeline : column.items || [];
  const items = timeline
    .map((item, index) => normalizeTimelineItem(item, index, fallbackDate))
    .filter(shouldShowTimelineItem);
  return { ...column, items, hour_ticks: toHourTicks(column) };
}

function toneForItem(item) {
  if (item.type === 'TRAVEL' || item.type === 'travel') return 'travel';
  if (item.type === 'BREAK' || item.type === 'break') return 'break';
  if (item.type === 'REQUIREMENT_PICKUP' || item.type === 'requirement_pickup') return 'pickup';
  if (item.urgency === 'URGENT' || item.urgency === 'Urgent') return 'urgent';
  if (item.urgency === 'LOW' || item.urgency === 'Laag') return 'low';
  if (item.urgency === 'MEDIUM' || item.urgency === 'Normaal') return 'normal';
  return 'blue';
}

function characteristicsLabel(item) {
  const labels = [];
  if (item.requires_ladder) labels.push('Ladder');
  if (item.requires_spring) labels.push('Trekveer');
  const extra = (item.characteristics || [])
    .filter((value) => !['LADDER', 'VEER', 'Ladder', 'Waves'].includes(String(value)))
    .map(String);
  return [...labels, ...extra].join(', ') || 'Geen';
}

function itemGridStyle(item, columnStartMinutes) {
  if (item._startMinutes == null || item._endMinutes == null || columnStartMinutes == null) return undefined;
  const startOffset = Math.max(0, item._startMinutes - columnStartMinutes);
  const endOffset = Math.max(startOffset + 1, item._endMinutes - columnStartMinutes);
  const rowStart = Math.floor(startOffset / TIMELINE_SLOT_MINUTES) + 1;
  const rowEnd = Math.max(rowStart + 1, Math.ceil(endOffset / TIMELINE_SLOT_MINUTES) + 1);
  return { gridRow: `${rowStart} / ${rowEnd}` };
}

function PlanningJob({ item, columnStartMinutes }) {
  const requirement = item.requires_ladder ? 'Ladder' : item.requires_spring ? 'Waves' : null;
  const isTravel = item.type === 'TRAVEL';
  const isTicket = item.type === 'TICKET';
  const isRequirementPickup = item.type === 'REQUIREMENT_PICKUP';

  return (
    <div className={`job ${toneForItem(item)}`} style={itemGridStyle(item, columnStartMinutes)}>
      <div>
        {isTravel ? (
          <b>Rijtijd: {item.duration_minutes}min</b>
        ) : (
          <>
            <b>{isTicket && item.ticket_display_id ? `${item.ticket_display_id} · ${item.title}` : item.title}</b>
            {item.address && <strong>{item.address}</strong>}
            <small>{item.start} - {item.end} · {item.duration_minutes} min</small>
            {isTicket && <small>Kenmerken: {characteristicsLabel(item)}</small>}
            {isRequirementPickup && item.requirements?.length > 0 && <small>Hulpmiddelen: {item.requirements.join(', ')}</small>}
          </>
        )}
      </div>
      {isTicket && <JobRequirementIcon kind={requirement} />}
    </div>
  );
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

function addDays(dateString, days) {
  const parsed = parseDateTime(dateString);
  if (!parsed) return null;
  parsed.setDate(parsed.getDate() + days);
  return parsed.toISOString().slice(0, 10);
}

function TechnicianColumn({ column }) {
  const normalizedColumn = normalizeColumn(column);
  const { technician, items, hour_ticks: hourTicks } = normalizedColumn;
  const columnStartMinutes = minutesFromDayStart(normalizedColumn.timeline_start_at) ?? minutesFromDayStart(hourTicks[0]);
  const columnEndMinutes = minutesFromDayStart(normalizedColumn.timeline_end_at) ?? minutesFromDayStart(hourTicks[hourTicks.length - 1]) ?? 17 * 60;
  const timelineRows = Math.max(108, Math.ceil((columnEndMinutes - columnStartMinutes) / TIMELINE_SLOT_MINUTES));

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

      <div className="timeline hourlyTimeline">
        <div className="timeMarks hourlyTimeMarks" style={{ gridTemplateRows: `repeat(${timelineRows}, ${TIMELINE_SLOT_HEIGHT_PX}px)` }}>
          {hourTicks.map((time) => {
            const tickMinutes = minutesFromDayStart(time);
            const gridRow = tickMinutes == null || columnStartMinutes == null
              ? undefined
              : Math.max(1, Math.round((tickMinutes - columnStartMinutes) / TIMELINE_SLOT_MINUTES) + 1);
            return <span key={time} style={gridRow ? { gridRow } : undefined}>{time}</span>;
          })}
        </div>
        <div className="jobs hourlyJobs" style={{ gridTemplateRows: `repeat(${timelineRows}, ${TIMELINE_SLOT_HEIGHT_PX}px)` }}>
          {items.map((item, itemIndex) => (
            <PlanningJob
              key={`${item.id || item.title}-${itemIndex}`}
              item={item}
              columnStartMinutes={columnStartMinutes}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

export function PlanningPage() {
  const [selectedDate, setSelectedDate] = useState(null);
  const loadPlanning = useCallback(() => api.getPlanning(selectedDate), [selectedDate]);
  const { data: planning, loading, error, reload } = useApi(loadPlanning, emptyPlanning);
  const [planningActionLoading, setPlanningActionLoading] = useState(false);

  const runAction = async (action) => {
    setPlanningActionLoading(true);
    try {
      await action();
      await reload();
    } catch (err) {
      alert(err.message);
    } finally {
      setPlanningActionLoading(false);
    }
  };

  const stats = planning.stats || emptyPlanning.stats;
  const hasPlan = Boolean(planning.has_plan);
  const columns = useMemo(() => (planning.columns || []).map(normalizeColumn), [planning.columns]);
  const availableDates = planning.available_dates || [];
  const activeDate = selectedDate || isoDate(planning.planned_date) || availableDates[0] || null;
  const activeDateIndex = availableDates.indexOf(activeDate);
  const canGoPrevious = activeDateIndex > 0;
  const canGoNext = activeDateIndex >= 0 && activeDateIndex < availableDates.length - 1;
  const planButtonDisabled = planningActionLoading || loading;

  const selectRelativeDay = (offset) => {
    if (activeDateIndex >= 0) {
      const nextDate = availableDates[activeDateIndex + offset];
      if (nextDate) setSelectedDate(nextDate);
      return;
    }
    const fallback = addDays(activeDate, offset);
    if (fallback) setSelectedDate(fallback);
  };

  return (
    <main className="page">
      <PageHeader title="Planning Overzicht">
        <div className="actions">
          <Button primary disabled={planButtonDisabled} onClick={() => runAction(hasPlan ? api.replan : api.autoPlan)}>
            {planningActionLoading ? <Loader2 className="spin" size={20} /> : hasPlan ? <RotateCw size={20} /> : <Clock size={20} />}
            {planningActionLoading ? 'Planning maken...' : hasPlan ? 'Herplannen' : 'Start planning'}
          </Button>
        </div>
      </PageHeader>

      <ApiNotice loading={loading} error={error} />

      {hasPlan && (
        <section className="planningDateSelector" aria-label="Planning dag kiezen">
          <Button disabled={!activeDate || !canGoPrevious} onClick={() => selectRelativeDay(-1)}>
            <ChevronLeft size={18} />
            Vorige dag
          </Button>
          <div className="planningDateCurrent">
            <CalendarDays size={18} />
            <b>{formatPlanningDate(activeDate)}</b>
            {availableDates.length > 0 && <small>Dag {Math.max(1, activeDateIndex + 1)} van {availableDates.length}</small>}
          </div>
          <Button disabled={!activeDate || !canGoNext} onClick={() => selectRelativeDay(1)}>
            Volgende dag
            <ChevronRight size={18} />
          </Button>
        </section>
      )}

      <section className="stats four">
        <StatCard icon={Ticket} label="Open tickets" value={stats.total_today} sub={`• ${stats.planned} toegewezen`} />
        <StatCard icon={AlertTriangle} label="Urgent open" value={stats.urgent_open} sub="• status open" tone="red" />
        <StatCard icon={Route} label="Kilometers gepland" value={`${stats.kilometers} km`} sub={`• ± ${Math.round((stats.travel_minutes || 0) / 60)} u reistijd`} tone="green" />
        <StatCard icon={Gauge} label="Vrije spreedruimte" value={`${Math.floor((stats.free_minutes || 0) / 60)} u ${(stats.free_minutes || 0) % 60}m`} sub="• buffer voor spoed" tone="purple" />
      </section>

      {!hasPlan && (
        <div className="apiNotice">
          Er staat nog geen dagplanning in de database. Gebruik <b>Start planning</b> om de eerste planning te maken.
        </div>
      )}

      <section className="plannerGrid">
        {columns.map((column) => <TechnicianColumn key={column.technician.id} column={column} />)}
      </section>
    </main>
  );
}
