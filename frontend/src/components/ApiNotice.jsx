export function ApiNotice({ loading, error }) {
  if (loading) return <div className="apiNotice">Backend laden...</div>;
  if (error) return <div className="apiNotice warning">{error}</div>;
  return null;
}
