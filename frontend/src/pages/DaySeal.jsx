import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function DaySeal() {
  const [info, setInfo] = useState(null)
  const [day, setDay] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [receipt, setReceipt] = useState(null)

  const load = () => Promise.all([api.sealInfo(), api.rollReceipt()]).then(([d, r]) => {
    setInfo(d)
    setReceipt(r?.回执 || null)
    // 默认预填「当前营业日的次日」，避免误填当天或过期日期触发后端拒绝
    const nd = new Date(`${d.营业日}T00:00:00`)
    nd.setDate(nd.getDate() + 1)
    const fmt = `${nd.getFullYear()}-${String(nd.getMonth() + 1).padStart(2, '0')}-${String(nd.getDate()).padStart(2, '0')}`
    setDay(prev => (prev && prev > d.营业日 ? prev : fmt))
  }).catch(e => setErr(e.message))

  useEffect(() => { load() }, [])

  const run = async (fn, ok) => {
    setErr(''); setMsg('')
    try { await fn(); setMsg(ok); await load() } catch (e) { setErr(e.message) }
  }

  const onRoll = async () => {
    setErr(''); setMsg('')
    if (info && info.未结号数 > 0) {
      const yes = window.confirm(
        `当前仍有 ${info.未结号数} 个未结号。切日后这些号（等候/叫号中/办理中/过号）将统一作废并留审计，窗口当前号清空，新日号段从 001 重新开始。确认切日？`
      )
      if (!yes) return
    }
    try {
      // 携带原营业日作为幂等键：响应丢失后重试命中同一回执，不会二次作废
      const r = await api.roll(day || null, info?.营业日)
      setReceipt(r?.回执 || null)
      setMsg(r?.重放 ? `已是同一回执：${r.原营业日} → ${r.营业日}` : `已切日：${r.原营业日} → ${r.营业日}，作废 ${r.作废号数} 个未结号`)
      await load()
    } catch (e) { setErr(e.message) }
  }

  if (!info && !err) return <p className="muted">加载日切…</p>
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">当日写闸</p>
        <h1>日切封口</h1>
        <p className="lede">封口后禁止取号与叫号；须封口后再切营业日。切日采用「强制作废未结」口径。</p>
      </header>
      {info && (
        <section className="panel">
          <div className="stat-grid">
            <div className="stat"><span>营业日</span><strong style={{ fontSize: '1.2rem' }}>{info.营业日}</strong></div>
            <div className="stat"><span>封口</span><strong>{info.已封口 ? '是' : '否'}</strong></div>
            <div className="stat"><span>未结号</span><strong>{info.未结号数}</strong></div>
          </div>
          <p className="muted">{info.备注}{info.封口时刻 ? ` · ${info.封口时刻}` : ''}</p>
          <div className="actions">
            <button type="button" onClick={() => run(() => api.seal(false), '已封口')} disabled={info.已封口}>封口</button>
            <button type="button" className="ghost" onClick={() => run(() => api.seal(true), '已强制封口')} disabled={info.已封口}>强制封口</button>
            <button type="button" className="ghost" onClick={() => run(() => api.unseal(), '已解封')} disabled={!info.已封口}>解封</button>
          </div>
        </section>
      )}
      <section className="panel">
        <h2>切日</h2>
        <div className="form row">
          <label>新营业日
            <input value={day} onChange={e => setDay(e.target.value)} placeholder="年-月-日" />
          </label>
          <button type="button" onClick={onRoll} disabled={!info?.已封口}>切到新营业日</button>
        </div>
        <p className="muted">
          切日将把旧日全部未结号统一作废（可审计），清空窗口当前号指针，号段游标归零；事务失败整笔回滚。
        </p>
        {err && <p className="err">{err}</p>}
        {msg && <p className="muted">{msg}</p>}
      </section>
      {receipt && (
        <section className="panel">
          <h2>最近切日回执</h2>
          <div className="stat-grid">
            <div className="stat"><span>原营业日</span><strong style={{ fontSize: '1.1rem' }}>{receipt.原营业日}</strong></div>
            <div className="stat"><span>新营业日</span><strong style={{ fontSize: '1.1rem' }}>{receipt.营业日}</strong></div>
            <div className="stat"><span>作废号数</span><strong>{receipt.作废?.数量 ?? receipt.作废号数 ?? 0}</strong></div>
          </div>
          <p className="muted">
            回执编号 {receipt.回执编号} · {receipt.切换时刻} · 口径 {receipt.口径 || '强制作废未结'}
            {(receipt.清空窗口编号 || []).length ? ` · 清空窗口 ${receipt.清空窗口编号.join('、')}` : ''}
          </p>
          {receipt.作废?.明细?.length > 0 && (
            <ul className="plain">
              {receipt.作废.明细.map(v => (
                <li key={v.号牌}>{v.号牌} · 原状态 {v.原状态}{v.窗口编号 ? ` · 窗口 ${v.窗口编号}` : ''}</li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  )
}
