import { Calendar, CheckCircle2, ChevronDown, Clock, Edit, FastForward, FolderOpen, HelpCircle, Pause, Play, Plus, RotateCw, Save, SlidersHorizontal, Ticket, Trash2 } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { UrgencyChips } from '../components/Filters';
import { SearchBox, SelectInput } from '../components/FormControls';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { Tag } from '../components/Tag';
import { injectionRows } from '../data/simulatorData';

const fallbackInjections = injectionRows.map((row) => ({
  inject_time: row[0], id: row[1], urgency: row[2] === 'Mild' ? 'Normaal' : row[2],
  requires_ladder: row[3] === '✓', requires_spring: row[4] === '✓', subject: row[5], address: row[6], status: row[7],
}));

const fallbackState = {
  scenario: 'Normale dag', current_time: '10:30', speed: 5, status: 'Gepauzeerd',
  stats: { tickets_in_scenario: 28, not_injected: 16, injected_today: 7, last_injection: '10:15' },
  activity_log: [{ time: '08:05', message: 'Ticket T-001 ingeschoten', actor: 'Admin' }, { time: '10:15', message: 'Ticket T-007 ingeschoten', actor: 'Admin' }],
};

function SimulatorToolbar({ scenarios, selectedScenarioId, onScenarioChange, onGenerate, onStart }) {
  const scenarioOptions = scenarios.map((scenario) => ({ value: scenario.id, label: scenario.name }));

  return (
    <div className="toolbar sim">
      <SelectInput value={selectedScenarioId} onChange={onScenarioChange} options={scenarioOptions} />
      <SearchBox text="Zoeken in scenario's of tickets..." />
      <span />
      <Button><FolderOpen size={20} />Scenario laden</Button>
      <Button onClick={onGenerate}><Plus size={20} />Tickets genereren</Button>
      <Button primary onClick={onStart}><Play size={20} />Start simulatie</Button>
    </div>
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
              <td><Tag>{row.urgency}</Tag></td>
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
  const [form, setForm] = useState({ inject_time: '12:00', urgency: 'Normaal', subject: 'Lekkage keuken', address: 'De Ruyterstraat 12, Den Bosch', city: 'Den Bosch', requires_ladder: false, requires_spring: false });
  const setField = (field, value) => setForm((current) => ({ ...current, [field]: value }));

  return (
    <aside className="newTicket">
      <h2>Nieuw ticket</h2>
      <label>Injectietijd</label>
      <input className="fieldInput" value={form.inject_time} onChange={(event) => setField('inject_time', event.target.value)} />
      <label>Urgentie</label>
      <select className="fieldInput" value={form.urgency} onChange={(event) => setField('urgency', event.target.value)}>
        <option>Urgent</option><option>Normaal</option><option>Laag</option>
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
        <Button primary onClick={() => onSave(form)}><Save size={18} />Opslaan</Button>
        <Button onClick={() => setForm({ inject_time: '12:00', urgency: 'Normaal', subject: '', address: '', city: 'Den Bosch', requires_ladder: false, requires_spring: false })}>Annuleren</Button>
      </div>
    </aside>
  );
}

function SimulationControls({ state, onPause, onStep, onReset }) {
  return (
    <section className="controls">
      <h3>Simulatie controls</h3>
      <p><span className="dot greenDot" /> Status: {state.status}</p>
      <Button onClick={onPause}><Pause size={18} />Pauze</Button>
      <Button onClick={onStep}><FastForward size={18} />Stap +15 min</Button>
      <Button onClick={onReset}><RotateCw size={18} />Reset dag</Button>
      <div className="speed">
        <b>Snelheid</b>
        {['1x', '5x', '10x'].map((speed) => <button key={speed} className={speed === `${state.speed}x` ? 'selected' : ''}>{speed}</button>)}
      </div>
    </section>
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
  const scenarioList = useMemo(() => scenarios?.length ? scenarios : [{ id: 'normale_dag', name: 'Normale dag' }], [scenarios]);

  const refresh = async () => { await reloadState(); await reloadInjections(); };
  const runAction = async (action) => { try { await action(); await refresh(); } catch (err) { alert(err.message); } };

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
        onStart={() => runAction(api.startSimulation)}
      />
      <section className="stats four">
        <StatCard icon={Ticket} label="Tickets in scenario" value={stats.tickets_in_scenario} sub="incl. injecties" />
        <StatCard icon={Clock} label="Nog niet ingeschoten" value={stats.not_injected} sub="wacht op injectietijd" tone="orange" />
        <StatCard icon={CheckCircle2} label="Vandaag ingeschoten" value={stats.injected_today} sub={`laatste injectie ${stats.last_injection}`} tone="green" />
        <StatCard icon={RotateCw} label="Huidige simulatietijd" value={state.current_time} sub={`snelheid ${state.speed}x`} tone="blue" />
      </section>
      <div className="filterRow simF"><UrgencyChips showLabel={false} /><div className="rightFilters"><SelectInput text="Alle types" /><Button><SlidersHorizontal size={18} />Meer filters</Button></div></div>
      <div className="simLayout">
        <InjectionTable injections={injections} onDelete={(id) => runAction(() => api.deleteInjection(id))} />
        <NewTicketPanel onSave={(payload) => runAction(() => api.createInjection(payload))} />
        <SimulationControls state={state} onPause={() => runAction(api.pauseSimulation)} onStep={() => runAction(() => api.stepSimulation(15))} onReset={() => runAction(api.resetSimulation)} />
        <ActivityLog items={state.activity_log || []} />
      </div>
    </main>
  );
}
