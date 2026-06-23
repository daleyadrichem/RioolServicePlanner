export function Button({ children, primary = false, danger = false, className = '', ...props }) {
  return (
    <button className={`btn ${primary ? 'primary' : ''} ${danger ? 'danger' : ''} ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}
