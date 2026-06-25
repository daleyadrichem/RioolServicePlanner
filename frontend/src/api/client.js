const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function request(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const rawDetail = await response.text();
    let detail = rawDetail;
    try {
      const parsed = JSON.parse(rawDetail);
      detail = parsed.detail || parsed.message || rawDetail;
      if (Array.isArray(detail)) {
        detail = detail
          .map((item) => item?.msg || item?.message || JSON.stringify(item))
          .join('\n');
      } else if (detail && typeof detail === 'object') {
        detail = detail.message || detail.msg || JSON.stringify(detail);
      }
    } catch {
      // Keep raw response text.
    }
    throw new Error(String(detail || `API request failed: ${response.status}`));
  }
  return response.json();
}

export const api = {
  health: () => request('/health'),
  getTickets: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.urgency && filters.urgency !== 'all') params.set('urgency', filters.urgency);
    if (filters.status && filters.status !== 'all') params.set('status', filters.status);
    const query = params.toString();
    return request(`/tickets${query ? `?${query}` : ''}`);
  },
  getTicketStatistics: () => request('/tickets/statistics'),
  getBranches: () => request('/branches'),
  validateTicketAddress: (payload) => request('/tickets/validate-address', { method: 'POST', body: JSON.stringify(payload) }),
  createTicket: (payload) => request('/tickets', { method: 'POST', body: JSON.stringify(payload) }),
  updateTicket: (id, payload) => request(`/tickets/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTicket: (id) => request(`/tickets/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  assignTicket: (id, technicianId) => request(`/tickets/${encodeURIComponent(id)}/assign`, { method: 'POST', body: JSON.stringify({ technician_id: technicianId }) }),
  generateTickets: (count = 5) => request(`/tickets/generate?count=${count}`, { method: 'POST' }),
  getTechnicians: () => request('/technicians'),
  getPlanning: () => request('/planning'),
  autoPlan: () => request('/planning/auto-plan', { method: 'POST' }),
  replan: () => request('/planning/replan', { method: 'POST' }),
  getSimulatorState: () => request('/simulator/state'),
  getSimulatorStatistics: () => request('/simulator/statistics'),
  getScenarios: () => request('/simulator/scenarios'),
  startSimulation: () => request('/simulator/start', { method: 'POST' }),
  pauseSimulation: () => request('/simulator/pause', { method: 'POST' }),
  stopSimulation: () => request('/simulator/stop', { method: 'POST' }),
  setSimulationSpeed: (speedMultiplier) => request(`/simulator/speed?speed_multiplier=${speedMultiplier}`, { method: 'PATCH' }),
  getInjections: () => request('/simulator/injections'),
  validateSimulatorAddress: (payload) => request('/simulator/validate-address', { method: 'POST', body: JSON.stringify(payload) }),
  createInjection: (payload) => request('/simulator/injections', { method: 'POST', body: JSON.stringify(payload) }),
  updateInjection: (id, payload) => request(`/simulator/injections/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteInjection: (id) => request(`/simulator/injections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  generateScenarioTickets: (scenarioId) => request(`/simulator/generate-tickets?scenario_id=${encodeURIComponent(scenarioId)}`, { method: 'POST' }),
  generateInjections: (count = 5) => request(`/simulator/generate-tickets?count=${count}`, { method: 'POST' }),
};
