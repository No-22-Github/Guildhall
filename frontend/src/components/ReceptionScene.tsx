/** Separate art and character layers keep motion subtle and honor reduced motion. */
export default function ReceptionScene({ filter, busy, hidden }: { filter?: string; busy: boolean; hidden: boolean }) {
  return <aside className={`reception-scene ${busy ? 'is-busy' : ''}`} hidden={hidden} aria-label="像素酒馆前台" style={{ filter }}>
    <div className="reception-art" aria-hidden="true">
      <div className="reception-backdrop" />
      <div className="reception-person"><div /></div>
      <div className="reception-counter" />
      <div className="reception-lamplight" />
    </div>
    <div className="reception-caption"><i />前台 · {busy ? '查阅中' : '倾听中'}<i /></div>
  </aside>
}
