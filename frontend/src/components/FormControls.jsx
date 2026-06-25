import { ChevronDown, Search } from 'lucide-react';

export function SelectInput({ icon: Icon, text, wide = false, value, onChange, options }) {
  if (options?.length) {
    return (
      <label className={`input selectInput ${wide ? 'wide' : ''}`}>
        {Icon && <Icon size={22} />}
        <select value={value} onChange={(event) => onChange?.(event.target.value)}>
          {options.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <ChevronDown size={16} />
      </label>
    );
  }

  return (
    <div className={`input ${wide ? 'wide' : ''}`}>
      {Icon && <Icon size={22} />}
      <span>{text}</span>
      <ChevronDown size={16} />
    </div>
  );
}

export function SearchBox({ text }) {
  return (
    <div className="input search">
      <Search size={22} />
      <span>{text}</span>
    </div>
  );
}

export function PlainInput({ children }) {
  return <div className="plainInput">{children}</div>;
}
