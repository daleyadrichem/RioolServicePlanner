import { AlertTriangle, Calendar, CheckCircle2, Clock, Edit, MapPin, Plus, Save, SlidersHorizontal, Trash2, Wrench, X, Ticket as TicketIcon } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { SelectInput } from '../components/FormControls';
import { PageHeader } from '../components/PageHeader';
import { RequirementIcons } from '../components/Requirements';
import { StatCard } from '../components/StatCard';
import { Tag } from '../components/Tag';
import { Ladder } from '../icons/Ladder';
import { toUrgencyApiValue } from '../utils/status';
import { tickets as fallbackRows } from '../data/ticketsData';

const fallbackTickets = fallbackRows.map((row) => ({
  id: row[0], database_id: String(row[0]).replace(/\D/g, ''), branch_id: '', branch_name: 'Branch Den Bosch', subject: row[1], address: row[2], created_at: row[3], urgency: toUrgencyApiValue(row[4]),
  requires_ladder: row[5].includes('ladder'), requires_spring: row[5].includes('waves'),
  status: row[6], technician_name: row[7] === 'Nog niet toegewezen' ? null : row[7], description: 'Mock ticket uit de frontend fallback data.',
}));

const fallbackStats = {
  open: fallbackTickets.filter((t) => !['Afgerond', 'COMPLETED', 'completed'].includes(t.status)).length,
  urgent_open: fallbackTickets.filter((t) => toUrgencyApiValue(t.urgency) === 'urgent' && !['Afgerond', 'COMPLETED', 'completed'].includes(t.status)).length,
  unplanned: fallbackTickets.filter((t) => !t.technician_name && !['Afgerond', 'COMPLETED', 'completed'].includes(t.status)).length,
  finished: fallbackTickets.filter((t) => ['Afgerond', 'COMPLETED', 'completed'].includes(t.status)).length,
};

const urgencyOptions = [
  { value: 'all', label: 'Alle' },
  { value: 'urgent', label: 'Urgent' },
  { value: 'medium', label: 'Normaal' },
  { value: 'low', label: 'Laag' },
];

const ticketUrgencyOptions = urgencyOptions.filter((item) => item.value !== 'all');

const statusOptions = [
  { value: 'all', label: 'Alle statussen' },
  { value: 'open', label: 'Open' },
  { value: 'urgent_open', label: 'Urgent open' },
  { value: 'unplanned', label: 'Ongepland' },
  { value: 'finished', label: 'Afgerond' },
];

const editableStatusOptions = [
  { value: 'open', label: 'Open' },
  { value: 'planned', label: 'Gepland' },
  { value: 'in_progress', label: 'Onderweg / bezig' },
  { value: 'completed', label: 'Afgerond' },
  { value: 'cancelled', label: 'Geannuleerd' },
];

const fallbackBranches = [{ id: '', name: 'Branch Den Bosch' }];

const emptyTicketForm = {
  branch_id: '',
  subject: '',
  address: '',
  urgency: 'medium',
  status: 'open',
  description: '',
  requires_ladder: false,
  requires_spring: false,
};

function ticketKey(ticket) {
  return ticket?.database_id || ticket?.id;
}

function apiTicketId(ticket) {
  return ticket?.database_id || ticket?.id;
}

function formatCreated(value) {
  if (!value) return '-';
  if (value.includes?.('Vandaag') || value.includes?.('Gisteren')) return value;
  return new Date(value).toLocaleString('nl-NL', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function requirementsString(ticket) {
  return `${ticket.requires_ladder ? 'ladder ' : ''}${ticket.requires_spring ? 'waves' : ''}`;
}

function matchesRequirement(row, requirement) {
  if (requirement === 'Ladder') return row.requires_ladder;
  if (requirement === 'Veer') return row.requires_spring;
  if (requirement === 'Geen requirements') return !row.requires_ladder && !row.requires_spring;
  return true;
}

function isValidManualAddressFormat(address) {
  return /^\s*.+?\s+\d+[A-Za-z]?(?:[-/][0-9A-Za-z]+)?\s*,\s*[^,]+(?:\s*,\s*[^,]+)?\s*$/.test(String(address || ''));
}

function formFromTicket(ticket) {
  if (!ticket) return { ...emptyTicketForm };
  return {
    subject: ticket.subject || '',
    address: ticket.address || '',
    urgency: toUrgencyApiValue(ticket.urgency || 'medium'),
    status: String(ticket.status || 'open').toLowerCase(),
    description: ticket.description || '',
    requires_ladder: Boolean(ticket.requires_ladder),
    requires_spring: Boolean(ticket.requires_spring),
  };
}

function TicketFilters({ filters, onChange }) {
  const [showMore, setShowMore] = useState(false);
  const setFilter = (field, value) => onChange((current) => ({ ...current, [field]: value }));

  return (
    <section className="simFilters">
      <div className="filterRow simF">
        <div className="chips">
          <span>Urgentie</span>
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
          <SelectInput value={filters.status} onChange={(value) => setFilter('status', value)} options={statusOptions} />
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

function TicketsTable({ tickets, selectedId, onSelect, onEdit }) {
  return (
    <div className="tableCard">
      <table>
        <thead>
          <tr>
            <th>Ticket</th><th>Onderwerp</th><th>Adres</th><th>Aangemaakt</th><th>Urgentie</th><th>Vereisten</th><th>Status</th><th>Monteur</th><th>Acties</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((ticket) => (
            <tr className={selectedId === ticketKey(ticket) ? 'selectedRow' : ''} key={ticketKey(ticket)} onClick={() => onSelect(ticketKey(ticket))}>
              <td><b>{ticket.id}</b></td>
              <td>{ticket.subject}</td>
              <td>{ticket.address}</td>
              <td>{formatCreated(ticket.created_at)}</td>
              <td><Tag>{ticket.urgency}</Tag></td>
              <td><RequirementIcons requirements={requirementsString(ticket)} /></td>
              <td><Tag>{ticket.status}</Tag></td>
              <td>{ticket.technician_name || 'Nog niet toegewezen'}</td>
              <td>
                <span className="rowActions">
                  <button
                    type="button"
                    aria-label="Ticket bewerken"
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(ticketKey(ticket));
                      onEdit(ticket);
                    }}
                  >
                    <Edit size={18} />
                  </button>
                </span>
              </td>
            </tr>
          ))}
          {!tickets.length && (
            <tr><td colSpan="9">Geen tickets gevonden voor deze filters.</td></tr>
          )}
        </tbody>
      </table>

      <div className="pagination">
        <span>1–{tickets.length} van {tickets.length} tickets</span>
        <div><Button>‹</Button><Button className="activePage">1</Button><Button>›</Button><SelectInput text="12 per pagina" /></div>
      </div>
    </div>
  );
}

function TicketDetail({ ticket, onDelete, onAssign, onDone, onEdit }) {
  if (!ticket) return <aside className="detail"><h2>Selecteer een ticket</h2></aside>;
  return (
    <aside className="detail">
      <button className="close"><X size={22} /></button>
      <small>Ticket <b>{ticket.id}</b></small>
      <h2>{ticket.subject}</h2>
      <p><MapPin size={16} />{ticket.address}</p>
      <p><Clock size={16} />{formatCreated(ticket.created_at)}</p>
      <hr />
      <div className="kv"><b>Urgentie</b><Tag>{ticket.urgency}</Tag></div>
      <div className="kv"><b>Status</b><Tag>{ticket.status}</Tag></div>
      <div className="kv"><b>Vereisten</b><span>{ticket.requires_ladder && <><Ladder /> Ladder </>}{ticket.requires_spring && <><RequirementIcons requirements="waves" /> Trekveer</>}</span></div>
      <h3>Omschrijving</h3>
      <p>{ticket.description || 'Geen omschrijving ingevuld.'}</p>
      <h3>Monteur</h3>
      <p>{ticket.technician_name || 'Nog niet toegewezen'}</p>
      <Button primary className="full" onClick={onAssign}><Calendar size={18} />Auto toewijzen</Button>
      <Button className="full" onClick={onDone}><CheckCircle2 size={18} />Markeer afgerond</Button>
      <Button className="full" onClick={onEdit}><Wrench size={18} />Bewerken</Button>
      <Button danger className="full" onClick={onDelete}><Trash2 size={18} />Verwijderen</Button>
    </aside>
  );
}

function TicketModal({ ticket, mode, branches, onClose, onSave }) {
  const defaultBranchId = branches?.[0]?.id || '';
  const [form, setForm] = useState(() => formFromTicket(ticket, defaultBranchId));
  const [checkingAddress, setCheckingAddress] = useState(false);
  const isEditing = mode === 'edit' && Boolean(ticket);
  const formDisabled = checkingAddress;

  useEffect(() => {
    setForm(formFromTicket(ticket, defaultBranchId));
  }, [ticket, defaultBranchId]);

  useEffect(() => {
    document.body.classList.toggle('waitingCursor', checkingAddress);
    return () => document.body.classList.remove('waitingCursor');
  }, [checkingAddress]);

  const setField = (field, value) => setForm((current) => ({ ...current, [field]: value }));

  const handleSave = async () => {
    const address = String(form.address || '').trim();
    const subject = String(form.subject || '').trim();

    if (!subject) {
      window.alert('Vul een onderwerp in.');
      return;
    }

    if (!isValidManualAddressFormat(address)) {
      window.alert("Gebruik voor het adres het formaat 'straat huisnummer, plaats' of 'straat huisnummer, plaats, land'. Bijvoorbeeld: 'Kerkstraat 12, Den Bosch'.");
      return;
    }

    setCheckingAddress(true);
    try {
      const resolvedAddress = await api.validateTicketAddress({ address });
      const selectedBranch = branches?.find((branch) => String(branch.id) === String(form.branch_id));
      await onSave({
        ...form,
        branch_id: form.branch_id || selectedBranch?.id || undefined,
        branch_name: selectedBranch?.name,
        ...resolvedAddress,
        subject,
        address: resolvedAddress.formatted_address || address,
        urgency: toUrgencyApiValue(form.urgency),
        status: form.status || 'open',
      });
    } catch (err) {
      window.alert(err.message || 'Adrescontrole mislukt. Het ticket is niet opgeslagen.');
    } finally {
      setCheckingAddress(false);
    }
  };

  return (
    <div className="modalBackdrop" role="presentation" onMouseDown={onClose}>
      <aside className="newTicket ticketModal" role="dialog" aria-modal="true" aria-label={isEditing ? 'Ticket bewerken' : 'Nieuw ticket'} onMouseDown={(event) => event.stopPropagation()}>
        <button className="modalClose" type="button" onClick={onClose} aria-label="Sluiten"><X size={22} /></button>
        <h2>{isEditing ? `Ticket bewerken ${ticket.id}` : 'Nieuw ticket'}</h2>
        {isEditing && <p className="panelHint">Dit blijft hetzelfde ticket in de database. Alleen de inhoud wordt aangepast.</p>}
        {checkingAddress && <p className="panelHint">Adres wordt gecontroleerd...</p>}

        <label>Vestiging</label>
        <select className="fieldInput" disabled={formDisabled} value={form.branch_id} onChange={(event) => setField('branch_id', event.target.value)}>
          {(branches || []).map((branch) => <option key={branch.id || branch.name} value={branch.id}>{branch.name}</option>)}
        </select>

        <label>Urgentie</label>
        <select className="fieldInput" disabled={formDisabled} value={form.urgency} onChange={(event) => setField('urgency', event.target.value)}>
          {ticketUrgencyOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>

        <label>Status</label>
        <select className="fieldInput" disabled={formDisabled} value={form.status} onChange={(event) => setField('status', event.target.value)}>
          {editableStatusOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>

        <label>Onderwerp</label>
        <input className="fieldInput" disabled={formDisabled} value={form.subject} onChange={(event) => setField('subject', event.target.value)} placeholder="Bijvoorbeeld: Verstopping keuken" />

        <label>Adres</label>
        <input
          className="fieldInput"
          disabled={formDisabled}
          value={form.address}
          placeholder="Kerkstraat 12, Den Bosch"
          onChange={(event) => setField('address', event.target.value)}
        />

        <label>Omschrijving</label>
        <textarea className="fieldInput ticketTextarea" disabled={formDisabled} value={form.description} onChange={(event) => setField('description', event.target.value)} placeholder="Extra informatie voor de planner of monteur" />

        <label>Requirement</label>
        <div className="checks">
          <label><input type="checkbox" disabled={formDisabled} checked={form.requires_ladder} onChange={(event) => setField('requires_ladder', event.target.checked)} /> Ladder</label>
          <label><input type="checkbox" disabled={formDisabled} checked={form.requires_spring} onChange={(event) => setField('requires_spring', event.target.checked)} /> Veer</label>
        </div>

        <div className="formActions">
          <Button primary disabled={formDisabled} onClick={handleSave}><Save size={18} />{checkingAddress ? 'Controleren...' : 'Opslaan'}</Button>
          <Button disabled={formDisabled} onClick={onClose}>Annuleren</Button>
        </div>
      </aside>
    </div>
  );
}

export function TicketsPage() {
  const [filters, setFilters] = useState({ urgency: 'all', status: 'all', requirement: 'Alle requirements' });
  const apiFilters = useMemo(() => ({ urgency: filters.urgency, status: filters.status }), [filters.urgency, filters.status]);
  const loadTickets = useCallback(() => api.getTickets(apiFilters), [apiFilters]);
  const loadStats = useCallback(() => api.getTicketStatistics(), []);
  const loadBranches = useCallback(() => api.getBranches(), []);
  const { data: tickets, loading, error, reload } = useApi(loadTickets, fallbackTickets);
  const { data: stats, reload: reloadStats } = useApi(loadStats, fallbackStats);
  const { data: branches } = useApi(loadBranches, fallbackBranches);
  const [selectedId, setSelectedId] = useState(ticketKey(fallbackTickets[0]));
  const [modalState, setModalState] = useState({ mode: null, ticket: null });

  const refresh = useCallback(async (options = {}) => {
    await reload(options);
    await reloadStats(options);
  }, [reload, reloadStats]);

  useEffect(() => {
    const intervalId = window.setInterval(() => refresh({ silent: true }), 1000);
    return () => window.clearInterval(intervalId);
  }, [refresh]);

  const filteredTickets = useMemo(
    () => (tickets || []).filter((row) => matchesRequirement(row, filters.requirement)),
    [tickets, filters.requirement],
  );

  const selected = useMemo(
    () => filteredTickets.find((ticket) => ticketKey(ticket) === selectedId) || filteredTickets[0] || tickets?.[0],
    [filteredTickets, selectedId, tickets],
  );

  useEffect(() => {
    if (selected && selectedId !== ticketKey(selected)) setSelectedId(ticketKey(selected));
  }, [selected, selectedId]);

  const runAction = async (action) => {
    try { await action(); await refresh(); } catch (err) { alert(err.message); }
  };

  const closeModal = () => setModalState({ mode: null, ticket: null });
  const openNewTicket = () => setModalState({ mode: 'new', ticket: null });
  const openEditTicket = (ticket) => setModalState({ mode: 'edit', ticket });

  const saveModalTicket = async (payload) => {
    if (modalState.mode === 'edit' && modalState.ticket) {
      const id = apiTicketId(modalState.ticket);
      await api.updateTicket(id, payload);
      await refresh();
      setSelectedId(id);
    } else {
      const created = await api.createTicket(payload);
      await refresh();
      setSelectedId(ticketKey(created));
    }
    closeModal();
  };

  return (
    <main className="page">
      <PageHeader title="Tickets"><Button primary onClick={openNewTicket}><Plus size={20} />Nieuw ticket</Button></PageHeader>
      <ApiNotice loading={loading} error={error} />
      <section className="stats four">
        <StatCard icon={TicketIcon} label="Open tickets" value={stats.open} sub="• uit tickets tabel" />
        <StatCard icon={AlertTriangle} label="Urgent open" value={stats.urgent_open} sub="• vereist planning" tone="red" />
        <StatCard icon={Clock} label="Ongepland" value={stats.unplanned} sub="• nog toewijzen" tone="orange" />
        <StatCard icon={CheckCircle2} label="Afgerond" value={stats.finished} sub="• status completed" tone="green" />
      </section>
      <TicketFilters filters={filters} onChange={setFilters} />
      <div className="ticketsLayout">
        <TicketsTable tickets={filteredTickets} selectedId={selected ? ticketKey(selected) : null} onSelect={setSelectedId} onEdit={openEditTicket} />
        <TicketDetail
          ticket={selected}
          onDelete={() => selected && runAction(() => api.deleteTicket(apiTicketId(selected)))}
          onAssign={() => runAction(() => api.autoPlan())}
          onDone={() => selected && runAction(() => api.updateTicket(apiTicketId(selected), { status: 'completed' }))}
          onEdit={() => selected && openEditTicket(selected)}
        />
      </div>
      {modalState.mode && (
        <TicketModal
          mode={modalState.mode}
          ticket={modalState.ticket}
          branches={branches}
          onClose={closeModal}
          onSave={saveModalTicket}
        />
      )}
    </main>
  );
}
