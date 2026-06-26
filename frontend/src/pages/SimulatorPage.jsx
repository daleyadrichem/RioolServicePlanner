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

const emptySimulatorState = {
  scenario: '',
  current_time: '-',
  speed: 1,
  status: 'Onbekend',
  stats: { tickets_in_scenario: 0, not_injected: 0, injected_today: 0, last_injection: '-' },
  activity_log: [],
};

const emptyScenarios = [];

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
      <Button onClick={onGenerate} disabled={isRunning}><Plus size={20} />Tickets genereren</Button>
      <Button onClick={onStop}><Square size={18} />Stop simulatie</Button>
      <Button primary onClick={onToggleSimulation}>{isRunning ? <Pause size={20} /> : <Play size={20} />}{isRunning ? 'Pauze' : 'Start simulatie'}</Button>
    </div>
  );
}

function isSimulationRunning(status) {
  const normalized = String(status || '').toLowerCase();
  return ['draait', 'running', 'actief', 'active', 'gestart', 'started'].some((value) => normalized.includes(value));
}

function isValidInjectTime(value) {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(String(value || '').trim());
}

function isValidManualAddressFormat(value) {
  return /^\s*.+?\s+\d+[A-Za-z]?(?:[-/][0-9A-Za-z]+)?\s*,\s*[^,]+\s*$/.test(String(value || ''));
}


function requirementCodeSet(row) {
  const codes = new Set((row.requirements || []).map((item) => String(item).toUpperCase()));
  if (row.requires_ladder) codes.add('LADDER');
  if (row.requires_spring) codes.add('VEER');
  if (row.requires_supplies) codes.add('SUPPLIES');
  return codes;
}

function requirementLabel(code, fallback = '') {
  const normalized = String(code || '').toUpperCase();
  if (normalized === 'LADDER') return 'Ladder';
  if (normalized === 'VEER') return 'Trekveer';
  if (normalized === 'SUPPLIES') return 'Benodigdheden';
  return fallback || normalized;
}

function matchesFilter(row, filters) {
  const urgency = toUrgencyApiValue(row.urgency);
  const filter = String(filters.requirement || 'all').toUpperCase();
  const codes = requirementCodeSet(row);

  if (filters.urgency !== 'all' && urgency !== filters.urgency) return false;
  if (filter === 'NONE' || filter === 'GEEN REQUIREMENTS') return codes.size === 0;
  if (filter !== 'ALL' && filter !== 'ALLE REQUIREMENTS' && !codes.has(filter)) return false;

  return true;
}

function SimulatorFilters({ filters, requirements, onChange }) {
  const [showMore, setShowMore] = useState(false);
  const urgencyOptions = [
    { value: 'all', label: 'Alle' },
    { value: 'urgent', label: 'Urgent' },
    { value: 'medium', label: 'Normaal' },
    { value: 'low', label: 'Laag' },
  ];

  const setFilter = (field, value) => onChange((current) => ({ ...current, [field]: value }));
  const requirementOptions = [
    { value: 'all', label: 'Alle requirements' },
    ...(requirements || []).map((requirement) => ({ value: requirement.code, label: requirement.name || requirementLabel(requirement.code) })),
    { value: 'none', label: 'Geen requirements' },
  ];

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
              {requirementOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
        </div>
      )}
    </section>
  );
}

function InjectionTable({ injections, onDelete, onEdit, disabledActions }) {
  return (
    <section className="tableCard simTable">
      <h2>Ticket injecties</h2>
      <table>
        <thead>
          <tr><th>Tijd</th><th>Ticket ID</th><th>Urgentie</th><th>Ladder</th><th>Veer</th><th>Benodigdheden</th><th>Onderwerp</th><th>Adres</th><th>Acties</th></tr>
        </thead>
        <tbody>
          {injections.map((row) => (
            <tr key={row.id}>
              <td>{row.inject_time}</td>
              <td>{row.id}</td>
              <td><Tag>{toUrgencyLabel(row.urgency)}</Tag></td>
              <td>{row.requires_ladder ? <span className="roundCheck">✓</span> : '–'}</td>
              <td>{row.requires_spring ? <span className="roundCheck">✓</span> : '–'}</td>
              <td>{row.requires_supplies ? <span className="roundCheck">✓</span> : '–'}</td>
              <td>{row.subject}</td>
              <td>{row.address}</td>
              <td>
                {disabledActions ? (
                  <span className="mutedActions">–</span>
                ) : (
                  <span className="rowActions">
                    <button type="button" aria-label="Ticket aanpassen" onClick={() => onEdit(row)}><Edit size={18} /></button>
                    <button type="button" aria-label="Ticket verwijderen" onClick={() => onDelete(row.database_id || row.id)}><Trash2 size={18} /></button>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const emptyTicketForm = {
  inject_time: '12:00',
  urgency: 'medium',
  subject: '',
  address: '',
  city: 'Den Bosch',
  requires_ladder: false,
  requires_spring: false,
  requires_supplies: false,
};

function formFromInjection(ticket) {
  if (!ticket) return { ...emptyTicketForm, subject: 'Lekkage keuken', address: 'De Ruyterstraat 12, Den Bosch' };

  return {
    inject_time: ticket.inject_time || '12:00',
    urgency: toUrgencyApiValue(ticket.urgency || 'medium'),
    subject: ticket.subject || '',
    address: ticket.address || '',
    city: ticket.city || 'Den Bosch',
    requires_ladder: Boolean(ticket.requires_ladder),
    requires_spring: Boolean(ticket.requires_spring),
    requires_supplies: Boolean(ticket.requires_supplies || (ticket.requirements || []).includes('SUPPLIES')),
  };
}

function NewTicketPanel({ onSave, editingTicket, onCancelEdit, disabled, requirements }) {
  const [form, setForm] = useState(() => formFromInjection(null));
  const [checkingAddress, setCheckingAddress] = useState(false);
  const isEditing = Boolean(editingTicket);
  const formDisabled = disabled || checkingAddress;

  useEffect(() => {
    setForm(formFromInjection(editingTicket));
  }, [editingTicket]);

  useEffect(() => {
    document.body.classList.toggle('waitingCursor', checkingAddress);
    return () => document.body.classList.remove('waitingCursor');
  }, [checkingAddress]);

  const setField = (field, value) => setForm((current) => ({ ...current, [field]: value }));
  const resetNewForm = () => setForm({ ...emptyTicketForm, subject: 'Lekkage keuken', address: 'De Ruyterstraat 12, Den Bosch' });
  const handleCancel = () => {
    if (!isEditing) return;
    onCancelEdit();
    resetNewForm();
  };
  const handleSave = async () => {
    const injectTime = String(form.inject_time || '').trim();
    const address = String(form.address || '').trim();

    if (!isValidInjectTime(injectTime)) {
      window.alert('Gebruik voor de injectietijd het formaat HH:MM, bijvoorbeeld 09:30.');
      return;
    }

    if (!isValidManualAddressFormat(address)) {
      window.alert("Gebruik voor het adres het formaat 'straat huisnummer, plaats', bijvoorbeeld 'Kerkstraat 12, Den Bosch'.");
      return;
    }

    setCheckingAddress(true);
    try {
      const resolvedAddress = await api.validateSimulatorAddress({ address });
      await onSave({
        ...form,
        ...resolvedAddress,
        address: resolvedAddress.formatted_address || address,
        inject_time: injectTime,
        urgency: toUrgencyApiValue(form.urgency),
      });
      if (!isEditing) resetNewForm();
    } catch (err) {
      window.alert(err.message || 'Adrescontrole mislukt. Het ticket is niet opgeslagen.');
    } finally {
      setCheckingAddress(false);
    }
  };

  return (
    <aside className={`newTicket ${disabled ? 'disabledPanel' : ''}`}>
      <h2>{isEditing ? 'Ticket aanpassen' : 'Nieuw ticket'}</h2>
      {disabled && <p className="panelHint">Pauzeer de simulatie om simulator tickets toe te voegen of aan te passen.</p>}
      {checkingAddress && <p className="panelHint">Adres wordt gecontroleerd...</p>}
      <label>Injectietijd</label>
      <input
        className="fieldInput"
        disabled={formDisabled}
        value={form.inject_time}
        placeholder="HH:MM"
        onChange={(event) => setField('inject_time', event.target.value)}
      />
      <label>Urgentie</label>
      <select className="fieldInput" disabled={formDisabled} value={form.urgency} onChange={(event) => setField('urgency', event.target.value)}>
        <option value="urgent">Urgent</option><option value="medium">Normaal</option><option value="low">Laag</option>
      </select>
      <label>Onderwerp</label>
      <input className="fieldInput" disabled={formDisabled} value={form.subject} onChange={(event) => setField('subject', event.target.value)} />
      <label>Adres</label>
      <input
        className="fieldInput"
        disabled={formDisabled}
        value={form.address}
        placeholder="Kerkstraat 12, Den Bosch"
        onChange={(event) => setField('address', event.target.value)}
      />
      <label>Requirement</label>
      <div className="checks">
        {(requirements || []).map((requirement) => {
          const code = String(requirement.code || '').toUpperCase();
          const field = code === 'LADDER' ? 'requires_ladder' : code === 'VEER' ? 'requires_spring' : code === 'SUPPLIES' ? 'requires_supplies' : null;
          if (!field) return null;
          return <label key={code}><input type="checkbox" disabled={formDisabled} checked={Boolean(form[field])} onChange={(event) => setField(field, event.target.checked)} /> {requirement.name || requirementLabel(code)}</label>;
        })}
      </div>
      <div className="formActions">
        <Button primary disabled={formDisabled} onClick={handleSave}><Save size={18} />{checkingAddress ? 'Controleren...' : 'Opslaan'}</Button>
        <Button disabled={!isEditing || formDisabled} onClick={handleCancel}>Annuleren</Button>
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
  const loadRequirements = useCallback(() => api.getRequirements(), []);
  const { data: state, loading, error, reload: reloadState } = useApi(loadState, emptySimulatorState);
  const { data: injections, reload: reloadInjections } = useApi(loadInjections, []);
  const { data: scenarios } = useApi(loadScenarios, emptyScenarios);
  const { data: requirements } = useApi(loadRequirements, [
    { code: 'LADDER', name: 'Ladder' },
    { code: 'VEER', name: 'Trekveer' },
    { code: 'SUPPLIES', name: 'Benodigdheden' },
  ]);
  const [selectedScenarioId, setSelectedScenarioId] = useState('');
  const [filters, setFilters] = useState({ urgency: 'all', requirement: 'all' });
  const [editingTicket, setEditingTicket] = useState(null);
  const scenarioList = useMemo(() => scenarios || [], [scenarios]);
  const simulationIsRunning = isSimulationRunning(state?.status);

  const refresh = useCallback(async (options = {}) => {
    await reloadState(options);
    await reloadInjections(options);
  }, [reloadState, reloadInjections]);
  const runAction = async (action) => { try { await action(); await refresh(); } catch (err) { alert(err.message); } };

  useEffect(() => {
    const intervalId = window.setInterval(() => refresh({ silent: true }), 1000);
    return () => window.clearInterval(intervalId);
  }, [refresh]);

  useEffect(() => {
    if (simulationIsRunning) setEditingTicket(null);
  }, [simulationIsRunning]);

  const filteredInjections = useMemo(() => (injections || []).filter((row) => matchesFilter(row, filters)), [injections, filters]);
  const stats = state.stats || emptySimulatorState.stats;
  const saveSimulationTicket = async (payload) => {
    if (editingTicket) {
      await runAction(() => api.updateInjection(editingTicket.database_id || editingTicket.id, payload));
      setEditingTicket(null);
      return;
    }
    await runAction(() => api.createInjection(payload));
  };

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
        onGenerate={() => simulationIsRunning ? undefined : runAction(() => api.generateScenarioTickets(selectedScenarioId))}
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
      <SimulatorFilters filters={filters} requirements={requirements} onChange={setFilters} />
      <div className="simLayout">
        <InjectionTable
          injections={filteredInjections}
          disabledActions={simulationIsRunning}
          onEdit={setEditingTicket}
          onDelete={(id) => runAction(() => api.deleteInjection(id))}
        />
        <NewTicketPanel
          disabled={simulationIsRunning}
          editingTicket={editingTicket}
          onCancelEdit={() => setEditingTicket(null)}
          onSave={saveSimulationTicket}
          requirements={requirements}
        />
        <ActivityLog items={state.activity_log || []} />
      </div>
    </main>
  );
}
