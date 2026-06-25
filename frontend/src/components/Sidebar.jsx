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

    </aside>
  );
}
