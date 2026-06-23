export const urgencyClassByLabel = {
  Urgent: 'urgent',
  Normaal: 'normal',
  Laag: 'low',
  Mild: 'mild',
};

export function toTagClass(label) {
  return urgencyClassByLabel[label] || String(label).toLowerCase();
}
