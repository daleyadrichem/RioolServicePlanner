const urgencyLabels = {
  urgent: 'Urgent',
  high: 'Urgent',
  spoed: 'Urgent',
  medium: 'Normaal',
  normal: 'Normaal',
  normaal: 'Normaal',
  mild: 'Normaal',
  mid: 'Normaal',
  low: 'Laag',
  laag: 'Laag',
};

const urgencyApiValues = {
  urgent: 'urgent',
  high: 'urgent',
  spoed: 'urgent',
  medium: 'medium',
  normal: 'medium',
  normaal: 'medium',
  mild: 'medium',
  mid: 'medium',
  laag: 'low',
  low: 'low',
};

const statusLabels = {
  open: 'Open',
  planned: 'Gepland',
  assigned: 'Toegewezen',
  in_progress: 'Bezig',
  completed: 'Afgerond',
  done: 'Afgerond',
  cancelled: 'Geannuleerd',
};

function normalizeKey(value) {
  return String(value || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
}

export function toUrgencyApiValue(value) {
  return urgencyApiValues[normalizeKey(value)] || normalizeKey(value);
}

export function toUrgencyLabel(value) {
  return urgencyLabels[normalizeKey(value)] || value || '-';
}

export function toStatusLabel(value) {
  return statusLabels[normalizeKey(value)] || value || '-';
}

export function toTagLabel(value) {
  const key = normalizeKey(value);
  if (urgencyLabels[key]) return toUrgencyLabel(value);
  return toStatusLabel(value);
}

export function toTagClass(label) {
  const urgency = toUrgencyApiValue(label);
  if (urgency === 'urgent') return 'urgent';
  if (urgency === 'medium') return 'normal';
  if (urgency === 'low') return 'low';

  const status = normalizeKey(label);
  if (status === 'completed' || status === 'done' || status === 'afgerond') return 'done';
  if (status === 'planned' || status === 'gepland' || status === 'assigned' || status === 'toegewezen') return 'planned';
  if (status === 'open') return 'open';

  return status || 'default';
}
