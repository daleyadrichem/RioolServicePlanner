const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function request(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `API request failed: ${response.status}`);
  }
  return response.json();
}

export const api = {
  health: () => request('/health'),
  getTickets: () => request('/tickets'),
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
  startSimulation: () => request('/simulator/start', { method: 'POST' }),
  pauseSimulation: () => request('/simulator/pause', { method: 'POST' }),
  stepSimulation: (minutes = 15) => request(`/simulator/step?minutes=${minutes}`, { method: 'POST' }),
  resetSimulation: () => request('/simulator/reset', { method: 'POST' }),
  getInjections: () => request('/simulator/injections'),
  createInjection: (payload) => request('/simulator/injections', { method: 'POST', body: JSON.stringify(payload) }),
  deleteInjection: (id) => request(`/simulator/injections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  generateInjections: (count = 5) => request(`/simulator/generate-tickets?count=${count}`, { method: 'POST' }),
};
