import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  text: string; visible: boolean; editing: boolean; dirty: boolean; drafting: boolean
  busy: boolean; focused: boolean; error: string
  onEdit: () => void; onChange: (text: string) => void; onSave: () => void
  onPost: () => void; onFocus: () => void; onClose: () => void
}
export default function QuestDocument(p: Props) {
  const metadata = p.text.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/)?.[0]
  const body = metadata ? p.text.slice(metadata.length) : p.text
  const title = metadata?.match(/^title:\s*(.+)$/m)?.[1]
  return <section className="quest-document" hidden={!p.visible} aria-label="委托需求单">
    <header className="document-toolbar">
      <div><h2>委托需求单</h2><span>{p.dirty ? '有未保存的修改' : p.drafting ? '草稿 · 已保存' : '已张贴'}</span></div>
      <nav aria-label="需求单操作">
        {p.drafting && <button disabled={p.busy} onClick={p.onEdit}>{p.editing ? '预览' : '编辑'}</button>}
        <button aria-pressed={p.focused} onClick={p.onFocus}>{p.focused ? '返回分栏 ↙' : '专注阅读 ↗'}</button>
        <button className="document-mobile-close" onClick={p.onClose}>返回对话</button>
      </nav>
    </header>
    {p.error && <div className="document-error" role="alert">{p.error}</div>}
    <div className="document-scroll" hidden={p.editing} tabIndex={0}>
      <div className="document-content markdown-body">{title && !/^#\s/m.test(body) && <h1>{title}</h1>}<ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown></div>
      {metadata && <details className="document-metadata"><summary>文档属性 · quest.md</summary><pre>{metadata}</pre></details>}
    </div>
    <div className="document-editor" hidden={!p.editing}>
      <label htmlFor="quest-source">编辑需求单 · Markdown</label>
      <textarea id="quest-source" spellCheck={false} value={p.text} readOnly={!p.drafting || p.busy} onChange={event => p.onChange(event.target.value)} />
    </div>
    <footer className="document-footer"><span>{p.drafting ? '确认内容后，张贴到委托板' : '委托已张贴，可以派出冒险者。'}</span>
      {p.drafting && <div>{p.dirty && <button disabled={p.busy} onClick={p.onSave}>保存修改</button>}<button className="document-post" disabled={p.busy || !p.text.trim()} onClick={p.onPost}>{p.busy ? '处理中…' : '确认并张贴'}</button></div>}
    </footer>
  </section>
}
