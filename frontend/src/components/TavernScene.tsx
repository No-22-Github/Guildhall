import type { QuestSummary } from '../api'
import { STATE_LABEL } from '../api'
import type { Skin, StationId } from '../skins'
const terminal = new Set(['settled', 'withdrawn'])
export default function TavernScene({
  skin,
  quests,
  onStation,
  onQuest,
}: {
  skin: Skin
  quests: QuestSummary[]
  onStation: (id: StationId) => void
  onQuest: (quest: QuestSummary) => void
}) {
  const runningCount = quests.filter((q) => q.state === 'in_progress').length
  const reviewCount = quests.filter((q) =>
    ['appraised', 'disputed'].includes(q.state),
  ).length
  return (
    <div className="scene-frame">
      <div className="scene" style={{ aspectRatio: '16 / 9' }}>
        <img
          className="room-art"
          src={skin.room}
          style={{ filter: skin.filter }}
          alt="像素酒馆大厅：左侧前台、中央委托板、右侧鉴定台与壁炉"
        />
        <div
          className="board-papers"
          style={{
            left: `${skin.board.x}%`,
            top: `${skin.board.y}%`,
            width: `${skin.board.width}%`,
            height: `${skin.board.height}%`,
          }}
        >
          {quests
            .filter((q) => !terminal.has(q.state) && q.state !== 'drafting')
            .slice(0, 4)
            .map((q) => (
              <button
                title={`${q.project} · ${q.title || q.id} · ${STATE_LABEL[q.state]}`}
                aria-label={`打开委托：${q.title || q.id}`}
                className={`paper state-${q.state}`}
                key={q.id}
                onClick={() => onQuest(q)}
              >
                <span>✦</span>
                <i />
                <i />
                <i />
                <b />
              </button>
            ))}
          {!quests.some(
            (q) => !terminal.has(q.state) && q.state !== 'drafting',
          ) && (
            <button
              className="board-empty"
              onClick={() => onStation('receptionist')}
            >
              等待张贴委托
              <br />
              <span>＋</span>
            </button>
          )}
        </div>
        {skin.stations.map((s) => {
          const count =
            s.id === 'appraiser'
              ? reviewCount
              : s.id === 'adventurer'
                ? runningCount
                : s.id === 'board'
                  ? quests.filter((q) => !terminal.has(q.state)).length
                  : 0
          return (
            <button
              key={s.id}
              className={`station station-${s.id}`}
              style={{ left: `${s.x}%`, top: `${s.y}%` }}
              onClick={() => onStation(s.id)}
              aria-label={`${s.label}：${s.hint}`}
            >
              {s.sprite !== undefined && (
                <span
                  className="character"
                  style={{
                    backgroundImage: `url(${skin.sprites})`,
                    backgroundPosition: `${s.sprite * 50}% 50%`,
                    filter: skin.filter,
                    clipPath: s.clipBottom
                      ? `inset(0 0 ${s.clipBottom}% 0)`
                      : undefined,
                  }}
                />
              )}
              <span className="station-label">
                {s.label}
                {count > 0 && <b>{count}</b>}
              </span>
              <span className="station-hint">{s.hint}</span>
            </button>
          )
        })}
      </div>
      <div className="scene-caption">
        <span>
          <i /> {skin.name}
        </span>
        <p>点击角色或设施，开始处理委托</p>
        <button onClick={() => onStation('board')}>查看全部委托 →</button>
      </div>
    </div>
  )
}
