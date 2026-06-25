import { AlertTriangle, Calendar, CheckCircle2, Clock, Edit, MapPin, Plus, Save, SlidersHorizontal, Trash2, Wrench, X, Ticket as TicketIcon } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
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

const statusFilterOptions = [
  { value: 'all', label: 'Alle statussen' },
  { value: 'open', label: 'Open' },
  { value: 'urgent_open', label: 'Urgent open' },
  { value: 'unplanned', label: 'Ongepland' },
  { value: 'finished', label: 'Afgerond' },
  { value: 'planned', label: 'Gepland' },
  { value: 'in_progress', label: 'Onderweg / bezig' },
  { value: 'cancelled', label: 'Geannuleerd' },
];

const pageSizeOptions = [12, 25, 50];

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
function normalizeTicketStatus(status) {
  return String(status || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
}

function isTicketTerminal(row) {
  return ['completed', 'cancelled', 'afgerond', 'geannuleerd'].includes(normalizeTicketStatus(row.status));
}

function matchesStatus(row, status) {
  const filter = normalizeTicketStatus(status || 'all');
  if (!filter || filter === 'all' || filter === 'alle') return true;
  if (filter === 'open') return !isTicketTerminal(row);
  if (filter === 'finished') return normalizeTicketStatus(row.status) === 'completed' || normalizeTicketStatus(row.status) === 'afgerond';
  if (filter === 'urgent_open') return toUrgencyApiValue(row.urgency) === 'urgent' && !isTicketTerminal(row);
  if (filter === 'unplanned') return !row.technician_id && !row.technician_name && !isTicketTerminal(row);
  return normalizeTicketStatus(row.status) === filter;
}

function matchesTechnician(row, technicianId) {
  const filter = String(technicianId || 'all');
  if (filter === 'all') return true;
  if (filter === 'unassigned') return !row.technician_id && !row.technician_name;
  return String(row.technician_id || row.technician_name || '') === filter;
}

function ticketTimestamp(ticket) {
  const value = ticket?.created_at;
  if (!value) return Number.MAX_SAFE_INTEGER;
  if (String(value).includes('Vandaag')) return Date.now();
  if (String(value).includes('Gisteren')) return Date.now() - 24 * 60 * 60 * 1000;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? Number.MAX_SAFE_INTEGER : parsed;
}


function isValidManualAddressFormat(address) {
  return /^\s*.+?\s+\d+[A-Za-z]?(?:[-/][0-9A-Za-z]+)?\s*,\s*[^,]+(?:\s*,\s*[^,]+)?\s*$/.test(String(address || ''));
}

function formFromTicket(ticket, defaultBranchId = '') {
  if (!ticket) return { ...emptyTicketForm, branch_id: defaultBranchId };
  return {
    branch_id: ticket.branch_id || defaultBranchId,
    subject: ticket.subject || '',
    address: ticket.address || '',
    urgency: toUrgencyApiValue(ticket.urgency || 'medium'),
    status: String(ticket.status || 'open').toLowerCase(),
    description: ticket.description || '',
    requires_ladder: Boolean(ticket.requires_ladder),
    requires_spring: Boolean(ticket.requires_spring),
  };
}

function TicketFilters({ filters, technicians, onChange }) {
  const [showMore, setShowMore] = useState(false);
  const setFilter = (field, value) => onChange((current) => ({ ...current, [field]: value }));
  const technicianOptions = [
    { value: 'all', label: 'Alle monteurs' },
    { value: 'unassigned', label: 'Nog niet toegewezen' },
    ...(technicians || []).map((technician) => ({ value: String(technician.id), label: technician.name })),
  ];

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
          <Button onClick={() => setShowMore((current) => !current)}><SlidersHorizontal size={18} />Meer filters</Button>
        </div>
      </div>
      {showMore && (
        <div className="moreFilters three">
          <label>
            Requirement
            <select value={filters.requirement} onChange={(event) => setFilter('requirement', event.target.value)}>
              <option>Alle requirements</option>
              <option>Ladder</option>
              <option>Veer</option>
              <option>Geen requirements</option>
            </select>
          </label>
          <label>
            Status
            <select value={filters.status} onChange={(event) => setFilter('status', event.target.value)}>
              {statusFilterOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label>
            Monteur
            <select value={filters.technician} onChange={(event) => setFilter('technician', event.target.value)}>
              {technicianOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
        </div>
      )}
    </section>
  );
}

function TicketsTable({ tickets, selectedId, onSelect, onEdit, page, pageSize, totalTickets, totalPages, onPageChange, onPageSizeChange }) {
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
        <span>{totalTickets ? `${((page - 1) * pageSize) + 1}–${Math.min(page * pageSize, totalTickets)} van ${totalTickets} tickets` : '0 van 0 tickets'}</span>
        <div>
          <Button disabled={page <= 1} onClick={() => onPageChange(page - 1)}>‹</Button>
          <Button className="activePage">{page}</Button>
          <Button disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>›</Button>
          <select className="pageSizeSelect" value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {pageSizeOptions.map((size) => <option key={size} value={size}>{size} per pagina</option>)}
          </select>
        </div>
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
  const [filters, setFilters] = useState({ urgency: 'all', status: 'all', requirement: 'Alle requirements', technician: 'all' });
  const [pageSize, setPageSize] = useState(12);
  const [page, setPage] = useState(1);
  const apiFilters = useMemo(() => ({ urgency: filters.urgency }), [filters.urgency]);
  const loadTickets = useCallback(() => api.getTickets(apiFilters), [apiFilters]);
  const loadStats = useCallback(() => api.getTicketStatistics(), []);
  const loadBranches = useCallback(() => api.getBranches(), []);
  const loadTechnicians = useCallback(() => api.getTechnicians(), []);
  const { data: tickets, loading, error, reload } = useApi(loadTickets, fallbackTickets);
  const { data: stats, reload: reloadStats } = useApi(loadStats, fallbackStats);
  const { data: branches } = useApi(loadBranches, fallbackBranches);
  const { data: technicians } = useApi(loadTechnicians, []);
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
    () => (tickets || [])
      .filter((row) => matchesRequirement(row, filters.requirement))
      .filter((row) => matchesStatus(row, filters.status))
      .filter((row) => matchesTechnician(row, filters.technician))
      .slice()
      .sort((a, b) => ticketTimestamp(a) - ticketTimestamp(b) || Number(apiTicketId(a) || 0) - Number(apiTicketId(b) || 0)),
    [tickets, filters.requirement, filters.status, filters.technician],
  );

  const totalPages = Math.max(1, Math.ceil(filteredTickets.length / pageSize));
  const paginatedTickets = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filteredTickets.slice(start, start + pageSize);
  }, [filteredTickets, page, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [filters, pageSize]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

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
      <TicketFilters filters={filters} technicians={technicians} onChange={setFilters} />
      <div className="ticketsLayout">
        <TicketsTable
          tickets={paginatedTickets}
          selectedId={selected ? ticketKey(selected) : null}
          onSelect={setSelectedId}
          onEdit={openEditTicket}
          page={page}
          pageSize={pageSize}
          totalTickets={filteredTickets.length}
          totalPages={totalPages}
          onPageChange={setPage}
          onPageSizeChange={setPageSize}
        />
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
