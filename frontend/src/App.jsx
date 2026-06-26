import { useState } from 'react';
import { Sidebar } from './components/Sidebar';
import { PlanningPage } from './pages/PlanningPage';
import { SimulatorPage } from './pages/SimulatorPage';
import { TicketsPage } from './pages/TicketsPage';
import { MapPage } from './pages/MapPage';

const screens = {
  planning: PlanningPage,
  tickets: TicketsPage,
  map: MapPage,
  simulator: SimulatorPage,
};

export function App() {
  const [active, setActive] = useState('planning');
  const Screen = screens[active] || PlanningPage;

  return (
    <div className="app">
      <Sidebar active={active} setActive={setActive} />
      <Screen />
    </div>
  );
}
