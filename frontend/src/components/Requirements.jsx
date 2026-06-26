import { Package, Waves } from 'lucide-react';
import { Ladder } from '../icons/Ladder';

function normalizedRequirements(requirements = '') {
  if (Array.isArray(requirements)) return requirements.map((item) => String(item).toLowerCase());
  return String(requirements || '').toLowerCase().split(/[,\s]+/).filter(Boolean);
}

export function RequirementIcons({ requirements = '' }) {
  const values = normalizedRequirements(requirements);
  return (
    <>
      {values.includes('ladder') && <Ladder size={22} />}
      {(values.includes('veer') || values.includes('waves')) && <Waves size={22} />}
      {values.includes('supplies') && <Package size={22} />}
    </>
  );
}

export function JobRequirementIcon({ kind }) {
  if (kind === 'Ladder') return <Ladder size={22} />;
  if (kind === 'Waves') return <Waves size={22} />;
  if (kind === 'Supplies') return <Package size={22} />;
  return null;
}
