export function PageHeader({ title, children }) {
  return (
    <header className="top">
      <h1>{title}</h1>
      {children}
    </header>
  );
}
