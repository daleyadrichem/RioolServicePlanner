import { Building2, ChevronDown } from 'lucide-react';
import { navigationItems } from '../data/navigation';
import { Logo } from './Logo';

export function Sidebar({ active, setActive }) {
  return (
    <aside className="sidebar">
      <Logo />

      <nav>
        {navigationItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.key}
              onClick={() => item.key !== 'settings' && setActive(item.key)}
              className={active === item.key ? 'active' : ''}
            >
              <Icon size={22} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="sideFooter">
        <div className="mini">
          <Building2 size={22} />
          <div>
            <small>Vestiging</small>
            <b>Den Bosch</b>
          </div>
          <ChevronDown size={16} />
        </div>

        <div className="mini">
          <span className="avatar">PL</span>
          <div>
            <small>Planner</small>
            <b>planner@rioolservice.nl</b>
          </div>
          <ChevronDown size={16} />
        </div>
      </div>
    </aside>
  );
}
