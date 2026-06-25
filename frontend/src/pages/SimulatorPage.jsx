import { CheckCircle2, ChevronDown, Clock, Edit, FolderOpen, HelpCircle, Pause, Play, Plus, Save, SlidersHorizontal, Square, Ticket, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { SelectInput } from '../components/FormControls';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { Tag } from '../components/Tag';
import { toUrgencyApiValue, toUrgencyLabel } from '../utils/status';
import { injectionRows } from '../data/simulatorData';

const fallbackInjections = injectionRows.map((row) => ({
  inject_time: row[0], id: row[1], urgency: toUrgencyApiValue(row[2]),
  requires_ladder: row[3] === '✓', requires_spring: row[4] === '✓', subject: row[5], address: row[6], status: row[7],
}));

const fallbackState = {
  scenario: 'Normale dag', current_time: '10:30', speed: 5, status: 'Gepauzeerd',
  stats: { tickets_in_scenario: 28, not_injected: 16, injected_today: 7, last_injection: '10:15' },
  activity_log: [{ time: '08:05', message: 'Ticket T-001 ingeschoten', actor: 'Admin' }, { time: '10:15', message: 'Ticket T-007 ingeschoten', actor: 'Admin' }],
};

function SimulatorToolbar({ scenarios, selectedScenarioId, onScenarioChange, onGenerate, state, onToggleSimulation, onStop, onSpeedChange }) {
  const scenarioOptions = scenarios.map((scenario) => ({ value: scenario.id, label: scenario.name }));
  const speedOptions = [1, 10, 60, 120];
  const isRunning = isSimulationRunning(state?.status);

  return (
    <div className="toolbar sim">
      <SelectInput value={selectedScenarioId} onChange={onScenarioChange} options={scenarioOptions} />
      <span className="simStatus"><span className={`dot ${isRunning ? 'greenDot' : 'orangeDot'}`} />Status: {state?.status || 'Onbekend'}</span>
      <div className="speed compactSpeed" aria-label="Simulatiesnelheid">
        <b>Snelheid</b>
        {speedOptions.map((speed) => (
          <button
            key={speed}
            type="button"
            className={speed === Number(state?.speed) ? 'selected' : ''}
            onClick={() => onSpeedChange(speed)}
          >
            {speed}x
          </button>
        ))}
      </div>
      <Button><FolderOpen size={20} />Scenario laden</Button>
      <Button onClick={onGenerate}><Plus size={20} />Tickets genereren</Button>
      <Button onClick={onStop}><Square size={18} />Stop simulatie</Button>
      <Button primary onClick={onToggleSimulation}>{isRunning ? <Pause size={20} /> : <Play size={20} />}{isRunning ? 'Pauze' : 'Start simulatie'}</Button>
    </div>
  );
}

function isSimulationRunning(status) {
  const normalized = String(status || '').toLowerCase();
  return ['draait', 'running', 'actief', 'active', 'gestart', 'started'].some((value) => normalized.includes(value));
}

function matchesFilter(row, filters) {
  const urgency = toUrgencyApiValue(row.urgency);

  if (filters.urgency !== 'all' && urgency !== filters.urgency) return false;
  if (filters.requirement === 'Ladder' && !row.requires_ladder) return false;
  if (filters.requirement === 'Veer' && !row.requires_spring) return false;
  if (filters.requirement === 'Geen requirements' && (row.requires_ladder || row.requires_spring)) return false;

  return true;
}

function SimulatorFilters({ filters, onChange }) {
  const [showMore, setShowMore] = useState(false);
  const urgencyOptions = [
    { value: 'all', label: 'Alle' },
    { value: 'urgent', label: 'Urgent' },
    { value: 'medium', label: 'Normaal' },
    { value: 'low', label: 'Laag' },
  ];

  const setFilter = (field, value) => onChange((current) => ({ ...current, [field]: value }));

  return (
    <section className="simFilters">
      <div className="filterRow simF">
        <div className="chips">
          {urgencyOptions.map((item) => (
            <button
              className={`chip ${filters.urgency === item.value ? 'selected' : ''} ${item.value === 'medium' ? 'normal' : item.value === 'all' ? 'alle' : item.value}`}
              key={item.value}
              type="button"
              onClick={() => setFilter('urgency', item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="rightFilters">
          <Button onClick={() => setShowMore((current) => !current)}><SlidersHorizontal size={18} />Meer filters</Button>
        </div>
      </div>
      {showMore && (
        <div className="moreFilters">
          <label>
            Requirement
            <select value={filters.requirement} onChange={(event) => setFilter('requirement', event.target.value)}>
              <option>Alle requirements</option>
              <option>Ladder</option>
              <option>Veer</option>
              <option>Geen requirements</option>
            </select>
          </label>
        </div>
      )}
    </section>
  );
}

function InjectionTable({ injections, onDelete }) {
  return (
    <section className="tableCard simTable">
      <h2>Ticket injecties</h2>
      <table>
        <thead>
          <tr><th>Tijd</th><th>Ticket ID</th><th>Urgentie</th><th>Ladder</th><th>Veer</th><th>Onderwerp</th><th>Adres</th><th>Acties</th></tr>
        </thead>
        <tbody>
          {injections.map((row) => (
            <tr key={row.id}>
              <td>{row.inject_time}</td>
              <td>{row.id}</td>
              <td><Tag>{toUrgencyLabel(row.urgency)}</Tag></td>
              <td>{row.requires_ladder ? <span className="roundCheck">✓</span> : '–'}</td>
              <td>{row.requires_spring ? <span className="roundCheck">✓</span> : '–'}</td>
              <td>{row.subject}</td>
              <td>{row.address}</td>
              <td><Edit size={18} /> <Trash2 size={18} onClick={() => onDelete(row.database_id || row.id)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function NewTicketPanel({ onSave }) {
  const [form, setForm] = useState({ inject_time: '12:00', urgency: 'medium', subject: 'Lekkage keuken', address: 'De Ruyterstraat 12, Den Bosch', city: 'Den Bosch', requires_ladder: false, requires_spring: false });
  const setField = (field, value) => setForm((current) => ({ ...current, [field]: value }));

  return (
    <aside className="newTicket">
      <h2>Nieuw ticket</h2>
      <label>Injectietijd</label>
      <input className="fieldInput" value={form.inject_time} onChange={(event) => setField('inject_time', event.target.value)} />
      <label>Urgentie</label>
      <select className="fieldInput" value={form.urgency} onChange={(event) => setField('urgency', event.target.value)}>
        <option value="urgent">Urgent</option><option value="medium">Normaal</option><option value="low">Laag</option>
      </select>
      <label>Onderwerp</label>
      <input className="fieldInput" value={form.subject} onChange={(event) => setField('subject', event.target.value)} />
      <label>Adres</label>
      <input className="fieldInput" value={form.address} onChange={(event) => setField('address', event.target.value)} />
      <label>Requirement</label>
      <div className="checks">
        <label><input type="checkbox" checked={form.requires_ladder} onChange={(event) => setField('requires_ladder', event.target.checked)} /> Ladder</label>
        <label><input type="checkbox" checked={form.requires_spring} onChange={(event) => setField('requires_spring', event.target.checked)} /> Veer</label>
      </div>
      <div className="formActions">
        <Button primary onClick={() => onSave({ ...form, urgency: toUrgencyApiValue(form.urgency) })}><Save size={18} />Opslaan</Button>
        <Button onClick={() => setForm({ inject_time: '12:00', urgency: 'medium', subject: '', address: '', city: 'Den Bosch', requires_ladder: false, requires_spring: false })}>Annuleren</Button>
      </div>
    </aside>
  );
}

function ActivityLog({ items }) {
  return (
    <section className="activity">
      <h3>Activiteitenlog</h3>
      {items.slice(0, 3).map((item, index) => <p key={`${item.time}-${index}`}><CheckCircle2 /> <b>{item.time}</b> {item.message}<br /><small>door {item.actor}</small></p>)}
      <a>Alle activiteiten bekijken ›</a>
    </section>
  );
}

export function SimulatorPage() {
  const loadState = useCallback(() => api.getSimulatorState(), []);
  const loadInjections = useCallback(() => api.getInjections(), []);
  const loadScenarios = useCallback(() => api.getScenarios(), []);
  const { data: state, loading, error, reload: reloadState } = useApi(loadState, fallbackState);
  const { data: injections, reload: reloadInjections } = useApi(loadInjections, fallbackInjections);
  const { data: scenarios } = useApi(loadScenarios, [{ id: 'normale_dag', name: 'Normale dag' }]);
  const [selectedScenarioId, setSelectedScenarioId] = useState('normale_dag');
  const [filters, setFilters] = useState({ urgency: 'all', requirement: 'Alle requirements' });
  const scenarioList = useMemo(() => scenarios?.length ? scenarios : [{ id: 'normale_dag', name: 'Normale dag' }], [scenarios]);

  const refresh = useCallback(async (options = {}) => {
    await reloadState(options);
    await reloadInjections(options);
  }, [reloadState, reloadInjections]);
  const runAction = async (action) => { try { await action(); await refresh(); } catch (err) { alert(err.message); } };

  useEffect(() => {
    const intervalId = window.setInterval(() => refresh({ silent: true }), 1000);
    return () => window.clearInterval(intervalId);
  }, [refresh]);

  const filteredInjections = useMemo(() => (injections || []).filter((row) => matchesFilter(row, filters)), [injections, filters]);
  const stats = state.stats || fallbackState.stats;

  return (
    <main className="page">
      <PageHeader title="Simulator Overzicht">
        <div className="profile"><Button><HelpCircle size={18} /></Button><span className="avatar">AD</span><b>Admin</b><ChevronDown size={16} /></div>
      </PageHeader>
      <ApiNotice loading={loading} error={error} />
      <SimulatorToolbar
        scenarios={scenarioList}
        selectedScenarioId={selectedScenarioId}
        onScenarioChange={setSelectedScenarioId}
        onGenerate={() => runAction(() => api.generateScenarioTickets(selectedScenarioId))}
        state={state}
        onToggleSimulation={() => runAction(isSimulationRunning(state.status) ? api.pauseSimulation : api.startSimulation)}
        onStop={() => runAction(api.stopSimulation)}
        onSpeedChange={(speed) => runAction(() => api.setSimulationSpeed(speed))}
      />
      <section className="stats four">
        <StatCard icon={Ticket} label="Tickets in scenario" value={stats.tickets_in_scenario} sub="incl. injecties" />
        <StatCard icon={Clock} label="Nog niet ingeschoten" value={stats.not_injected} sub="wacht op injectietijd" tone="orange" />
        <StatCard icon={CheckCircle2} label="Vandaag ingeschoten" value={stats.injected_today} sub={`laatste injectie ${stats.last_injection}`} tone="green" />
        <StatCard icon={Clock} label="Huidige simulatietijd" value={state.current_time} sub={`snelheid ${state.speed}x`} tone="blue" />
      </section>
      <SimulatorFilters filters={filters} onChange={setFilters} />
      <div className="simLayout">
        <InjectionTable injections={filteredInjections} onDelete={(id) => runAction(() => api.deleteInjection(id))} />
        <NewTicketPanel onSave={(payload) => runAction(() => api.createInjection(payload))} />
        <ActivityLog items={state.activity_log || []} />
      </div>
    </main>
  );
}
