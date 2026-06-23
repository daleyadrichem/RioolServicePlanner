import { AlertTriangle, Calendar, CheckCircle2, Clock, Edit, MapPin, MoreVertical, Plus, SlidersHorizontal, Trash2, Wrench, X, Ticket as TicketIcon } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';
import { ApiNotice } from '../components/ApiNotice';
import { Button } from '../components/Button';
import { SelectInput, SearchBox } from '../components/FormControls';
import { PageHeader } from '../components/PageHeader';
import { RequirementIcons } from '../components/Requirements';
import { StatCard } from '../components/StatCard';
import { Tag } from '../components/Tag';
import { Ladder } from '../icons/Ladder';
import { tickets as fallbackRows } from '../data/ticketsData';

const fallbackTickets = fallbackRows.map((row) => ({
  id: row[0], subject: row[1], address: row[2], created_at: row[3], urgency: row[4],
  requires_ladder: row[5].includes('ladder'), requires_spring: row[5].includes('waves'),
  status: row[6], technician_name: row[7], description: 'Mock ticket uit de frontend fallback data.',
}));

function formatCreated(value) {
  if (!value) return '-';
  if (value.includes?.('Vandaag') || value.includes?.('Gisteren')) return value;
  return new Date(value).toLocaleString('nl-NL', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function requirementsString(ticket) {
  return `${ticket.requires_ladder ? 'ladder ' : ''}${ticket.requires_spring ? 'waves' : ''}`;
}

function TicketFilters() {
  return (
    <div className="ticketFilters">
      <SelectInput icon={Calendar} text="Datum 20 mei 2025" />
      <SearchBox text="Zoeken in tickets..." />
      <Button><SlidersHorizontal size={18} />Filters</Button>
      <span>Urgentie</span>
      {['Alle', 'Urgent', 'Normaal', 'Laag'].map((item, index) => (
        <button className={`chip ${index === 0 ? 'selected' : ''} ${item.toLowerCase()}`} key={item}>{item}</button>
      ))}
      <span>Status</span>
      <SelectInput text="Alle statussen" />
      <span>Vereisten</span>
      <SelectInput text="Alle vereisten" />
    </div>
  );
}

function TicketsTable({ tickets, selectedId, onSelect }) {
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
            <tr className={selectedId === ticket.id ? 'selectedRow' : ''} key={ticket.id} onClick={() => onSelect(ticket.id)}>
              <td><b>{ticket.id}</b></td>
              <td>{ticket.subject}</td>
              <td>{ticket.address}</td>
              <td>{formatCreated(ticket.created_at)}</td>
              <td><Tag>{ticket.urgency}</Tag></td>
              <td><RequirementIcons requirements={requirementsString(ticket)} /></td>
              <td><Tag>{ticket.status}</Tag></td>
              <td>{ticket.technician_name || 'Nog niet toegewezen'}</td>
              <td><MoreVertical size={18} /></td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="pagination">
        <span>1–{tickets.length} van {tickets.length} tickets</span>
        <div><Button>‹</Button><Button className="activePage">1</Button><Button>›</Button><SelectInput text="12 per pagina" /></div>
      </div>
    </div>
  );
}

function TicketDetail({ ticket, onDelete, onAssign, onDone }) {
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
      <Button className="full"><Wrench size={18} />Bewerken</Button>
      <Button danger className="full" onClick={onDelete}><Trash2 size={18} />Verwijderen</Button>
    </aside>
  );
}

export function TicketsPage() {
  const loadTickets = useCallback(() => api.getTickets(), []);
  const { data: tickets, loading, error, reload } = useApi(loadTickets, fallbackTickets);
  const [selectedId, setSelectedId] = useState(fallbackTickets[0]?.id);
  const selected = useMemo(() => tickets.find((ticket) => ticket.id === selectedId) || tickets[0], [tickets, selectedId]);

  const runAction = async (action) => {
    try { await action(); await reload(); } catch (err) { alert(err.message); }
  };

  const createSampleTicket = () => runAction(() => api.createTicket({
    subject: 'Nieuwe rioolverstopping', address: 'Brugstraat 25, Den Bosch', city: 'Den Bosch', urgency: 'Urgent', requires_ladder: false, requires_spring: true, description: 'Aangemaakt via het frontend prototype.',
  }));

  const stats = {
    open: tickets.filter((t) => t.status !== 'Afgerond').length,
    urgent: tickets.filter((t) => t.urgency === 'Urgent' && t.status !== 'Afgerond').length,
    unplanned: tickets.filter((t) => !t.technician_id && t.status !== 'Afgerond').length,
    done: tickets.filter((t) => t.status === 'Afgerond').length,
  };

  return (
    <main className="page">
      <PageHeader title="Tickets"><Button primary onClick={createSampleTicket}><Plus size={20} />Nieuw ticket</Button></PageHeader>
      <ApiNotice loading={loading} error={error} />
      <TicketFilters />
      <section className="stats four">
        <StatCard icon={TicketIcon} label="Open tickets" value={stats.open} sub="• actuele mockdata" />
        <StatCard icon={AlertTriangle} label="Urgent open" value={stats.urgent} sub="• vereist planning" tone="red" />
        <StatCard icon={Clock} label="Ongepland" value={stats.unplanned} sub="• nog toewijzen" tone="orange" />
        <StatCard icon={CheckCircle2} label="Vandaag afgerond" value={stats.done} sub="• mock status" tone="green" />
      </section>
      <div className="ticketsLayout">
        <TicketsTable tickets={tickets} selectedId={selected?.id} onSelect={setSelectedId} />
        <TicketDetail
          ticket={selected}
          onDelete={() => runAction(() => api.deleteTicket(selected.id))}
          onAssign={() => runAction(() => api.autoPlan())}
          onDone={() => runAction(() => api.updateTicket(selected.id, { status: 'Afgerond' }))}
        />
      </div>
    </main>
  );
}
