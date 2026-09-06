import { useCallback, useEffect, useState } from 'react'
import { settingsApi, type RoleId, type SettingsSnapshot } from '../api'
import { skins } from '../skins'
const roles: { id: RoleId; name: string; description: string }[] = [
  {
    id: 'receptionist',
    name: '前台接待员',
    description: '梳理需求、起草可验收的委托。',
  },
  { id: 'adventurer', name: '冒险者', description: '在独立工作区中完成实现。' },
  { id: 'appraiser', name: '鉴定师', description: '独立检查代码与验收证据。' },
]
type Draft = Record<
  RoleId,
  { model: string; base_url: string; auth_token: string; clear_token: boolean }
>
export default function Settings({
  skinId,
  onSkin,
  reducedMotion,
  onMotion,
  onDirty,
  onBusy,
}: {
  skinId: string
  onSkin: (id: string) => void
  reducedMotion: boolean
  onMotion: (v: boolean) => void
  onDirty: (v: boolean) => void
  onBusy: (v: boolean) => void
}) {
  const [saved, setSaved] = useState<SettingsSnapshot | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const accept = useCallback(
    (data: SettingsSnapshot) => {
      setSaved(data)
      setDraft(
        Object.fromEntries(
          roles.map(({ id }) => [
            id,
            {
              model: data.roles[id].model,
              base_url: data.roles[id].base_url,
              auth_token: '',
              clear_token: false,
            },
          ]),
        ) as Draft,
      )
      setDirty(false)
      onDirty(false)
    },
    [onDirty],
  )
  useEffect(() => {
    settingsApi
      .get()
      .then(accept)
      .catch((e) => setError(String(e)))
  }, [accept])
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  function update(id: RoleId, change: Partial<Draft[RoleId]>) {
    setDraft((prev) => prev && { ...prev, [id]: { ...prev[id], ...change } })
    setDirty(true)
    onDirty(true)
    setNotice('')
  }
  async function save() {
    if (!saved || !draft) return
    setBusy(true)
    onBusy(true)
    setError('')
    setNotice('')
    try {
      accept(
        await settingsApi.save({
          revision: saved.revision,
          roles: Object.fromEntries(
            roles.map(({ id }) => [
              id,
              { ...draft[id], auth_token: draft[id].auth_token || null },
            ]),
          ) as Parameters<typeof settingsApi.save>[0]['roles'],
        }),
      )
      setNotice('配置已保存。模型与 API 配置将在后端重启后生效。')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
      onBusy(false)
    }
  }
  return (
    <div className="settings-page">
      <div className="section-heading">
        <span className="eyebrow">GUILD PREFERENCES</span>
        <h2>公会设置</h2>
        <p>为每位成员选择合适的模型，也让大厅更合你的心意。</p>
      </div>
      <section>
        <h3>大厅外观</h3>
        <div className="skin-options">
          {skins.map((s) => (
            <button
              key={s.id}
              className={`skin-option ${skinId === s.id ? 'selected' : ''}`}
              onClick={() => onSkin(s.id)}
              aria-pressed={skinId === s.id}
            >
              <img src={s.room} style={{ filter: s.filter }} alt="" />
              <strong>
                {s.name}
                {skinId === s.id && ' · 当前'}
              </strong>
              <small>{s.description}</small>
            </button>
          ))}
        </div>
        <label className="check-label">
          <input
            type="checkbox"
            checked={reducedMotion}
            onChange={(e) => onMotion(e.target.checked)}
          />
          减少场景动画
        </label>
        <p className="muted">外观立即生效，仅保存在当前浏览器。</p>
      </section>
      <section>
        <div className="section-heading">
          <h3>模型与 API</h3>
          <p>
            每个角色独立配置。留空项沿用后端进程的环境变量或 Claude
            登录默认值；角色之间不会自动继承配置。API 需兼容当前 Claude Agent 的
            Anthropic 接口。
          </p>
        </div>
        {error && (
          <div role="alert" className="notice error">
            {error}
            {draft && (
              <button
                disabled={busy}
                onClick={() => {
                  if (
                    dirty &&
                    !window.confirm('重新载入会丢弃未保存的模型配置，继续？')
                  )
                    return
                  settingsApi
                    .get()
                    .then((data) => {
                      accept(data)
                      setError('')
                    })
                    .catch((e) => setError(String(e)))
                }}
              >
                重新载入配置
              </button>
            )}
          </div>
        )}
        {notice && (
          <div role="status" className="notice">
            {notice}
          </div>
        )}
        {saved?.restart_required && (
          <div className="notice">
            已保存的配置与当前运行配置不同，等待后端重启生效。
          </div>
        )}
        {!draft && !error && <p>正在读取配置…</p>}
        {!draft && error && (
          <button
            onClick={() => {
              setError('')
              settingsApi
                .get()
                .then(accept)
                .catch((e) => setError(String(e)))
            }}
          >
            重新载入
          </button>
        )}
        {draft && saved && (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void save()
            }}
          >
            <fieldset disabled={busy}>
              {roles.map(({ id, name, description }) => (
                <div className="role-settings" key={id}>
                  <div>
                    <h4>{name}</h4>
                    <p>{description}</p>
                  </div>
                  <div className="role-fields">
                    <label>
                      模型名称
                      <input
                        value={draft[id].model}
                        onChange={(e) => update(id, { model: e.target.value })}
                        placeholder="使用默认模型"
                      />
                    </label>
                    <label>
                      API 地址
                      <input
                        type="url"
                        value={draft[id].base_url}
                        onChange={(e) =>
                          update(id, { base_url: e.target.value })
                        }
                        placeholder="https://api.example.com"
                      />
                    </label>
                    <label>
                      API 密钥
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={draft[id].auth_token}
                        disabled={draft[id].clear_token}
                        onChange={(e) =>
                          update(id, { auth_token: e.target.value })
                        }
                        placeholder={
                          saved.roles[id].has_token
                            ? '已配置 · 留空保留原密钥'
                            : '未配置 · 使用默认凭据'
                        }
                      />
                    </label>
                    {saved.roles[id].has_token && (
                      <label className="check-label">
                        <input
                          type="checkbox"
                          checked={draft[id].clear_token}
                          onChange={(e) =>
                            update(id, {
                              clear_token: e.target.checked,
                              auth_token: '',
                            })
                          }
                        />
                        清除已保存密钥
                      </label>
                    )}
                  </div>
                </div>
              ))}
            </fieldset>
            <div className="settings-actions">
              <span>
                {dirty ? '有未保存的更改' : '配置已同步'} · 密钥不会回显
              </span>
              <button
                type="button"
                disabled={busy || !dirty}
                onClick={() => {
                  accept(saved)
                  setError('')
                }}
              >
                撤销修改
              </button>
              <button className="primary" disabled={busy || !dirty}>
                {busy ? '保存中…' : '保存模型配置'}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  )
}
