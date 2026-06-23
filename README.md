# Riool Service Planning Portal

Frontend prototype for a planning and ticketing system for a sewer service company. The application is built for the NXTPhase coding case and focuses on showing how planners can manage tickets, technician schedules and simulator scenarios.

## What this project is

This is a React frontend prototype. It demonstrates the main screens and user flows of the planning system without requiring a production backend yet.

The goal of the application is to support a planning process where tickets are assigned to technicians while taking into account:

- Ticket urgency
- Ticket requirements, such as ladder or trekveer
- Technician capabilities
- Travel/planning blocks
- Low-priority tickets that can be moved when urgent work appears
- A simulator for testing different day scenarios

For now, the frontend can be used with local mock data. A backend can be connected later through the API client layer.

## Main screens

### Planning

The planning page gives the planner an overview of the daily schedule per technician. It shows planned tickets, travel time blocks and technician capabilities.

### Tickets

The tickets page shows the ticket overview. It is intended for filtering, reviewing and managing incoming work.

### Simulator

The simulator page is used to test planning behavior with predefined scenarios, such as a normal day, many urgent tickets, ladder-heavy work, technician outage or tickets taking longer than expected.

## Tech stack

- React
- Vite
- JavaScript
- CSS
- lucide-react icons

## Requirements

Make sure these are installed on your machine:

- Node.js 18 or newer
- npm

You can check this with:

```bash
node -v
npm -v
```

## Getting started

Install the dependencies:

```bash
npm install
```

Start the development server:

```bash
npm run dev
```

Vite will show a local URL, usually:

```text
http://localhost:5173
```

Open that URL in your browser.

## Available scripts

```bash
npm run dev
```

Starts the local development server.

```bash
npm run build
```

Creates a production build in the `dist` folder.

```bash
npm run preview
```

Runs a local preview of the production build.

## Responsive layout

The interface is designed to work on different screen sizes, including larger displays such as `1920x1200`.

On wide screens, the application uses the available space to show planning columns and content cards clearly. On smaller screens, the layout wraps or collapses so pages remain usable.

## Current status

This is not a finished production system yet. It is a frontend prototype meant to make the solution tangible and easy to review during the case presentation.

Currently included:

- Modular React setup
- Planning overview
- Ticket overview
- Simulator screen
- Mock data
- Responsive styling
- Reusable UI components

Not included yet:

- Production database
- Authentication and user roles
- Real route optimization
- Real travel-time integration with OpenStreetMap or another routing provider
- Persistent ticket storage
- Final adaptive duration model

## Backend integration

The project already has a separate API client layer in `src/api/client.js`. This keeps backend communication separate from the UI components.

When a backend is added, the frontend should call endpoints for data such as:

- Tickets
- Technicians
- Planning
- Simulator events
- Route and travel-time calculations

A possible future environment variable could be:

```bash
VITE_API_URL=http://localhost:8000
```

## Troubleshooting

### npm install tries to use an internal registry

When npm tries to download from an internal or unavailable registry, reset it to the public npm registry:

```bash
npm config set registry https://registry.npmjs.org/
```

Then remove local install files and install again.

For macOS/Linux:

```bash
rm -f package-lock.json
rm -rf node_modules
npm cache clean --force
npm install
```

### Port already in use

If Vite says the port is already in use, it will usually suggest a different port. You can also stop the process using the current port and run:

```bash
npm run dev
```

## Case context

The case is based on a sewer service company with one initial branch in Den Bosch. The solution should be scalable so that more branches can be added later.

The planning challenge is to complete as many tickets as possible while minimizing travel distance and respecting urgency, technician availability and ticket requirements.

## License

This project is currently intended for the NXTPhase coding case and personal evaluation purposes.
