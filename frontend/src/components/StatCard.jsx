export function StatCard({ icon: Icon, label, value, sub, tone = 'blue' }) {
  return (
    <div className="stat">
      <div className={`statIcon ${tone}`}>
        <Icon size={32} />
      </div>
      <div>
        <small>{label}</small>
        <h2>{value}</h2>
        {sub && <p>{sub}</p>}
      </div>
    </div>
  );
}
