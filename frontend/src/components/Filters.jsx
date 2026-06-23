import { SlidersHorizontal } from 'lucide-react';
import { Button } from './Button';
import { SelectInput } from './FormControls';

const defaultUrgencies = ['Alle', 'Urgent', 'Normaal', 'Laag'];

export function UrgencyChips({ items = defaultUrgencies, showLabel = true }) {
  return (
    <div className="chips">
      {showLabel && <span>Filter op urgentie:</span>}
      {items.map((item, index) => (
        <button className={`chip ${index === 0 ? 'selected' : ''} ${item.toLowerCase()}`} key={item}>
          {item}
        </button>
      ))}
    </div>
  );
}

export function FilterRow({ showUrgencyLabel = true }) {
  return (
    <div className="filterRow">
      <UrgencyChips showLabel={showUrgencyLabel} />
      <div className="rightFilters">
        <SelectInput text="Alle types" />
        <Button><SlidersHorizontal size={18} />Meer filters</Button>
      </div>
    </div>
  );
}
