export function ApiNotice({ error }) {
  if (error) return <div className="apiNotice warning">{error}</div>;
  return null;
}
