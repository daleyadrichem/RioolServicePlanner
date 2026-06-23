export function Ladder({ size = 22 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 3v18" />
      <path d="M16 3v18" />
      <path d="M8 7h8" />
      <path d="M8 12h8" />
      <path d="M8 17h8" />
    </svg>
  );
}
