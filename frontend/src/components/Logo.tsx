/** 产品标志（与 public/favicon.svg 同一个图形） */
export function Logo({ size = 32, inverted = false }: { size?: number; inverted?: boolean }) {
  const bg = inverted ? '#ffffff' : '#2f5bea';
  const fg = inverted ? '#2f5bea' : '#ffffff';
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="logo">
      <rect width="32" height="32" rx="8" fill={bg} />
      <rect x="8" y="10" width="16" height="12" rx="3" fill="none" stroke={fg} strokeWidth="2" />
      <circle cx="13" cy="16" r="1.6" fill={fg} />
      <circle cx="19" cy="16" r="1.6" fill={fg} />
      <path d="M16 10V6.5" stroke={fg} strokeWidth="2" strokeLinecap="round" />
      <circle cx="16" cy="5.5" r="1.5" fill={fg} />
      <path d="M11 25h10" stroke={fg} strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
