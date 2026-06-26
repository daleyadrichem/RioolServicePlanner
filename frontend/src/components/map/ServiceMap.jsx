import 'leaflet/dist/leaflet.css';

import L from 'leaflet';
import { useEffect, useMemo } from 'react';
import { CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap } from 'react-leaflet';

const DEFAULT_CENTER = [51.6978, 5.3037];
const DEFAULT_ZOOM = 11;
const ROUTE_COLORS = ['#0f67ff', '#f97316', '#16a34a', '#7c3aed', '#dc2626', '#0891b2', '#ca8a04', '#db2777'];

function routeColor(route, index) {
  const numericId = Number(route?.technician_id);
  const paletteIndex = Number.isFinite(numericId) ? Math.abs(numericId) % ROUTE_COLORS.length : index % ROUTE_COLORS.length;
  return ROUTE_COLORS[paletteIndex];
}

function isFiniteCoordinate(latitude, longitude) {
  return Number.isFinite(Number(latitude)) && Number.isFinite(Number(longitude));
}

function pointToLatLng(point) {
  if (!point || !isFiniteCoordinate(point.latitude, point.longitude)) return null;
  return [Number(point.latitude), Number(point.longitude)];
}

function createDivIcon(className, label) {
  return L.divIcon({
    className: `serviceMapIcon ${className}`,
    html: `<span>${label}</span>`,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
    popupAnchor: [0, -17],
  });
}

const hqIcon = createDivIcon('hq', 'HQ');
const mechanicIcon = createDivIcon('mechanic', 'M');

function ticketIcon(ticket) {
  const urgency = String(ticket.urgency || '').toLowerCase();
  const className = urgency === 'urgent' ? 'ticket urgent' : urgency === 'low' ? 'ticket low' : 'ticket medium';
  return createDivIcon(className, 'T');
}

function BoundsController({ points }) {
  const map = useMap();

  useEffect(() => {
    const validPoints = points.filter(Boolean);
    if (!validPoints.length) return;
    if (validPoints.length === 1) {
      map.setView(validPoints[0], DEFAULT_ZOOM);
      return;
    }
    map.fitBounds(validPoints, { padding: [42, 42], maxZoom: 14 });
  }, [map, points]);

  return null;
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });
}

function TicketPopup({ ticket }) {
  return (
    <div className="mapPopup">
      <b>{ticket.display_id} · {ticket.subject}</b>
      <span>{ticket.address}</span>
      <small>Urgentie: {ticket.urgency || 'Onbekend'}</small>
      <small>Status: {ticket.status || 'Onbekend'}</small>
      {ticket.technician_name && <small>Monteur: {ticket.technician_name}</small>}
      {ticket.planned_start_at && <small>Planning: {formatTime(ticket.planned_start_at)} - {formatTime(ticket.planned_end_at)}</small>}
      {ticket.requirements?.length > 0 && <small>Kenmerken: {ticket.requirements.join(', ')}</small>}
    </div>
  );
}

function MechanicPopup({ mechanic }) {
  return (
    <div className="mapPopup">
      <b>{mechanic.name}</b>
      <span>{mechanic.address}</span>
      <small>Status: {mechanic.status || 'Onbekend'}</small>
      <small>Locatiebron: {mechanic.current_location_source === 'ticket_in_progress' ? 'ticket in uitvoering' : 'startlocatie'}</small>
      {mechanic.requirements?.length > 0 && <small>Vaardigheden: {mechanic.requirements.join(', ')}</small>}
    </div>
  );
}

function HqPopup({ hq }) {
  return (
    <div className="mapPopup">
      <b>{hq.name}</b>
      <span>{hq.address}</span>
      <small>Hoofdlocatie / branch</small>
    </div>
  );
}

export function ServiceMap({ data }) {
  const tileUrl = import.meta.env.VITE_MAP_TILE_URL || 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
  const attribution = import.meta.env.VITE_MAP_ATTRIBUTION || '&copy; OpenStreetMap contributors';

  const hq = data?.hq || [];
  const tickets = data?.tickets || [];
  const mechanics = data?.mechanics || [];
  const routes = data?.routes || [];

  const boundsPoints = useMemo(() => {
    const markerPoints = [
      ...hq.map(pointToLatLng),
      ...tickets.map(pointToLatLng),
      ...mechanics.map(pointToLatLng),
    ];
    const routePoints = routes.flatMap((route) => route.coordinates || []);
    return [...markerPoints, ...routePoints].filter(Boolean);
  }, [hq, tickets, mechanics, routes]);

  return (
    <MapContainer center={DEFAULT_CENTER} zoom={DEFAULT_ZOOM} scrollWheelZoom className="serviceMapCanvas">
      <TileLayer attribution={attribution} url={tileUrl} />
      <BoundsController points={boundsPoints} />

      {routes.map((route, routeIndex) => {
        const color = routeColor(route, routeIndex);
        return (
          <Polyline
            key={`route-${route.technician_id}`}
            positions={route.coordinates || []}
            className="serviceRouteLine"
            pathOptions={{ color }}
          >
            <Tooltip sticky>{route.technician_name} · {route.ticket_ids?.length || 0} tickets</Tooltip>
          </Polyline>
        );
      })}

      {hq.map((item) => {
        const position = pointToLatLng(item);
        if (!position) return null;
        return (
          <Marker key={`hq-${item.id}`} position={position} icon={hqIcon}>
            <Popup><HqPopup hq={item} /></Popup>
          </Marker>
        );
      })}

      {tickets.map((ticket) => {
        const position = pointToLatLng(ticket);
        if (!position) return null;
        return (
          <Marker key={`ticket-${ticket.id}`} position={position} icon={ticketIcon(ticket)}>
            <Popup><TicketPopup ticket={ticket} /></Popup>
          </Marker>
        );
      })}

      {mechanics.map((mechanic) => {
        const position = pointToLatLng(mechanic);
        if (!position) return null;
        return (
          <Marker key={`mechanic-${mechanic.id}`} position={position} icon={mechanicIcon}>
            <Popup><MechanicPopup mechanic={mechanic} /></Popup>
          </Marker>
        );
      })}

      {routes.flatMap((route, routeIndex) => (route.stops || []).map((stop) => ({ stop, color: routeColor(route, routeIndex) }))).map(({ stop, color }, index) => {
        if (stop.type !== 'ticket') return null;
        const position = pointToLatLng(stop);
        if (!position) return null;
        return (
          <CircleMarker key={`stop-${stop.assignment_id || index}`} center={position} radius={12} className="routeStopMarker" pathOptions={{ color, fillColor: color }}>
            <Tooltip>{stop.sequence_order}. {stop.label}</Tooltip>
          </CircleMarker>
        );
      })}
    </MapContainer>
  );
}
