import { Waves } from 'lucide-react';
import { Ladder } from '../icons/Ladder';

export function RequirementIcons({ requirements = '' }) {
  return (
    <>
      {requirements.includes('ladder') && <Ladder size={22} />}
      {requirements.includes('waves') && <Waves size={22} />}
    </>
  );
}

export function JobRequirementIcon({ kind }) {
  if (kind === 'Ladder') return <Ladder size={22} />;
  if (kind === 'Waves') return <Waves size={22} />;
  return null;
}
